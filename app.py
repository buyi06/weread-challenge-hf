#!/usr/bin/env python3
"""Flask Web UI for weread-selenium-cli on HuggingFace Spaces.

Features:
  - Password-protected Web UI (default: linuxdo123)
  - Dark-themed status dashboard with auto-refresh
  - QR code display + manual restart button
  - Manual trigger reading / restart reading
  - Background scheduler (every N hours)

Endpoints:
  GET  /          dashboard (requires login)
  GET  /status    JSON status (requires login)
  GET  /login.png QR code image (requires login)
  POST /start     trigger reading (requires login)
  POST /restart   kill + restart reading (requires login)
  GET  /logs      tail app.log (requires login)
  GET  /healthz   health check (no auth, for external ping)
  GET  /login     login page
  POST /login     authenticate
  GET  /logout    clear session
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Flask, Response, abort, jsonify, redirect,
    render_template_string, request, send_file, session,
)

# ─── Configuration ─────────────────────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("WEREAD_DATA_DIR", "/data/.weread"))
PORT = int(os.environ.get("PORT", "7860"))
READING_INTERVAL_HOURS = float(os.environ.get("READING_INTERVAL_HOURS", "12"))
START_SCRIPT = Path(os.environ.get("START_SCRIPT", "/app/start_reading.sh"))
COOKIE_TTL_DAYS = 30
LOGIN_QR_FRESH_MINUTES = 5
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "linuxdo123")

DATA_DIR.mkdir(parents=True, exist_ok=True)

LOGIN_PNG = DATA_DIR / "login.png"
COOKIES_JSON = DATA_DIR / "cookies.json"
PID_FILE = DATA_DIR / "run.pid"
STATE_FILE = DATA_DIR / "last_run.json"
APP_LOG = DATA_DIR / "app.log"

PUBLIC_ENV_KEYS = (
    "WEREAD_BROWSER", "WEREAD_DATA_DIR", "WEREAD_DURATION", "WEREAD_SPEED",
    "WEREAD_SELECTION", "WEREAD_SCREENSHOT", "WEREAD_AGREE_TERMS",
    "DEFAULT_BOOK_URL", "READING_INTERVAL_HOURS",
)
SECRET_ENV_KEYS = (
    "BARK_KEY", "EMAIL_PASS", "EMAIL_USER", "EMAIL_TO", "EMAIL_FROM", "EMAIL_SMTP",
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "weread-challenge-secret-key-2024")


# ─── Auth ──────────────────────────────────────────────────────────────────────
_NO_AUTH_PATHS = {"/healthz", "/login"}


@app.before_request
def _check_auth():
    if request.path in _NO_AUTH_PATHS:
        return None
    if session.get("authed"):
        return None
    if request.accept_mimetypes.accept_html and not request.is_json:
        return redirect("/login")
    return jsonify({"ok": False, "reason": "unauthorized"}), 401


@app.route("/login", methods=["GET", "POST"])
def route_login():
    if request.method == "GET":
        return render_template_string(_LOGIN_HTML)
    pwd = (request.form.get("password") or "").strip()
    if pwd == WEB_PASSWORD:
        session["authed"] = True
        return redirect("/")
    return render_template_string(_LOGIN_HTML, error=True)


@app.route("/logout")
def route_logout():
    session.clear()
    return redirect("/login")


# ─── Status helpers ────────────────────────────────────────────────────────────
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _file_age_seconds(p: Path) -> float | None:
    try:
        return (_utcnow().timestamp() - p.stat().st_mtime)
    except FileNotFoundError:
        return None


def _pid_alive() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None
    try:
        os.kill(pid, 0)
        return pid
    except (ProcessLookupError, PermissionError):
        return None


def _last_run() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _cookies_status() -> tuple[str, float | None]:
    if not COOKIES_JSON.exists():
        return ("missing", None)
    age_days = _file_age_seconds(COOKIES_JSON) / 86400.0
    if age_days > COOKIE_TTL_DAYS:
        return ("expired", age_days)
    return ("valid", age_days)


def _qr_status() -> tuple[bool, float | None]:
    age = _file_age_seconds(LOGIN_PNG)
    if age is None:
        return (False, None)
    return (age <= LOGIN_QR_FRESH_MINUTES * 60, age)


def _reading_state() -> dict:
    last = _last_run()
    pid = _pid_alive()
    cookies, cookies_age_days = _cookies_status()
    qr_fresh, qr_age = _qr_status()

    if pid is not None:
        if qr_fresh and cookies != "valid":
            phase = "waiting_login"
        else:
            phase = "running"
    else:
        if qr_fresh and cookies != "valid":
            phase = "waiting_login"
        else:
            phase = "idle"

    started_at = last.get("started_at")
    duration_min = float(last.get("duration_minutes", 68))
    eta_seconds: float | None = None
    if phase == "running" and started_at:
        try:
            t0 = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            elapsed = (_utcnow() - t0).total_seconds()
            eta_seconds = max(0.0, duration_min * 60 - elapsed)
        except ValueError:
            pass

    return {
        "phase": phase,
        "pid": pid,
        "cookies": {"status": cookies, "age_days": cookies_age_days},
        "login_qr": {"present": LOGIN_PNG.exists(), "fresh": qr_fresh, "age_seconds": qr_age},
        "last_run": last,
        "eta_seconds": eta_seconds,
        "now": _utcnow().isoformat(),
    }


def _public_env() -> dict:
    out = {k: os.environ.get(k, "") for k in PUBLIC_ENV_KEYS}
    for k in SECRET_ENV_KEYS:
        v = os.environ.get(k, "")
        out[k] = "***set***" if v else ""
    return out


def _notification_status() -> dict:
    """Check which notification channels are configured."""
    channels = []
    if os.environ.get("BARK_KEY"):
        channels.append({"name": "Bark", "configured": True})
    else:
        channels.append({"name": "Bark", "configured": False})

    email_fields = ["EMAIL_USER", "EMAIL_PASS", "EMAIL_TO", "EMAIL_SMTP"]
    email_set = all(os.environ.get(f) for f in email_fields)
    channels.append({"name": "Email", "configured": email_set, "missing_fields": [f for f in email_fields if not os.environ.get(f)]})

    if os.environ.get("WEBHOOK_URL"):
        channels.append({"name": "Webhook", "configured": True})
    else:
        channels.append({"name": "Webhook", "configured": False})

    return {"channels": channels, "any_configured": any(c["configured"] for c in channels)}


# ─── Background workers ───────────────────────────────────────────────────────
def _spawn_reader(trigger: str) -> int:
    proc = subprocess.Popen(
        [str(START_SCRIPT), trigger],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid


def _scheduler_loop() -> None:
    interval = max(60.0, READING_INTERVAL_HOURS * 3600)
    time.sleep(interval)
    while True:
        try:
            if _pid_alive() is None:
                _spawn_reader("scheduler")
        except Exception as exc:
            print(f"[scheduler] error: {exc}", flush=True)
        time.sleep(interval)


def _start_background_threads() -> None:
    threading.Thread(target=_scheduler_loop, name="reader-scheduler", daemon=True).start()


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route("/status")
def route_status() -> Response:
    return jsonify({"reading": _reading_state(), "env": _public_env(), "notifications": _notification_status()})


@app.route("/login.png")
def route_login_png() -> Response:
    if not LOGIN_PNG.exists():
        abort(404)
    resp = send_file(LOGIN_PNG, mimetype="image/png", max_age=0)
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/start", methods=["POST"])
def route_start() -> Response:
    if _pid_alive() is not None:
        return jsonify({"ok": False, "reason": "already running"}), 409
    pid = _spawn_reader("manual")
    return jsonify({"ok": True, "spawned_pid": pid})


@app.route("/restart", methods=["POST"])
def route_restart() -> Response:
    pid = _pid_alive()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
            if _pid_alive() == pid:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    time.sleep(1)
    new_pid = _spawn_reader("manual")
    return jsonify({"ok": True, "killed_pid": pid, "spawned_pid": new_pid})


@app.route("/logs")
def route_logs() -> Response:
    n = max(1, min(int(request.args.get("n", 200)), 5000))
    if not APP_LOG.exists():
        return Response("", mimetype="text/plain")
    with APP_LOG.open("rb") as f:
        try:
            f.seek(-min(256_000, n * 400), os.SEEK_END)
        except OSError:
            f.seek(0)
        tail = f.read().decode("utf-8", errors="replace").splitlines()[-n:]
    return Response("\n".join(tail), mimetype="text/plain")




@app.route("/logs/clean")
def route_logs_clean() -> Response:
    """Return filtered, structured log entries."""
    n = max(1, min(int(request.args.get("n", 50)), 500))
    if not APP_LOG.exists():
        return jsonify({"entries": [], "total": 0})

    import re
    # Keywords that indicate meaningful log lines
    meaningful = re.compile(
        r'(start_reading|login|cookie|reading|error|fail|success|complet|warn|'
        r'screenshot|book|duration|prune|✓|✗|▶|initial|manual|scheduler|'
        r'starting|finished|exited|run )',
        re.IGNORECASE,
    )

    with APP_LOG.open("r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()

    entries = []
    for line in all_lines:
        stripped = line.rstrip("\n")
        if not stripped.strip():
            continue
        if not meaningful.search(stripped):
            continue

        # Determine level
        lower = stripped.lower()
        if "error" in lower or "fail" in lower or "✗" in stripped:
            level = "error"
        elif "success" in lower or "complet" in lower or "✓" in stripped:
            level = "success"
        elif "warn" in lower:
            level = "warning"
        else:
            level = "info"

        entries.append({"text": stripped, "level": level})

    # Take last N entries
    total = len(entries)
    entries = entries[-n:]
    return jsonify({"entries": entries, "total": total})

@app.route("/healthz")
def route_health() -> Response:
    return jsonify({"ok": True, "now": _utcnow().isoformat()})


@app.route("/")
def route_index() -> str:
    return render_template_string(_INDEX_HTML, env=_public_env())


# ─── Login page HTML ──────────────────────────────────────────────────────────
_LOGIN_HTML = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WeRead Challenge - Login</title>
<style>
  :root { --bg: #0d1117; --panel: #161b22; --border: #30363d; --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff; --err: #f85149; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; background: var(--bg); color: var(--text); display: flex; justify-content: center; align-items: center; min-height: 100vh; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 32px; width: 320px; text-align: center; }
  h1 { font-size: 20px; margin: 0 0 24px; }
  input { width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg); color: var(--text); font-size: 14px; margin-bottom: 16px; outline: none; }
  input:focus { border-color: var(--accent); }
  button { width: 100%; padding: 10px; background: var(--accent); color: #fff; border: none; border-radius: 6px; font-size: 14px; cursor: pointer; }
  .err { color: var(--err); font-size: 13px; margin-bottom: 12px; }
</style>
</head>
<body>
<div class="card">
  <h1>📖 WeRead Challenge</h1>
  {% if error %}<div class="err">密码错误，请重试</div>{% endif %}
  <form method="POST">
    <input type="password" name="password" placeholder="输入密码" autofocus>
    <button type="submit">登录</button>
  </form>
</div>
</body>
</html>
"""


# ─── Dashboard HTML ───────────────────────────────────────────────────────────
_INDEX_HTML = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WeRead Challenge</title>
<style>
  :root {
    --bg: #0d1117; --panel: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff;
    --ok: #3fb950; --warn: #d29922; --err: #f85149;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
         background: var(--bg); color: var(--text); padding: 16px; }
  h1 { margin: 0 0 12px; font-size: 18px; font-weight: 600; }
  h1 small { color: var(--muted); font-weight: 400; font-size: 12px; margin-left: 8px; }
  .grid { display: grid; gap: 12px; max-width: 720px; margin: 0 auto;
          grid-template-columns: 1fr; }
  @media (min-width: 720px) { .grid { grid-template-columns: 1fr 1fr; } }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
          padding: 14px 16px; }
  .card h2 { margin: 0 0 10px; font-size: 13px; color: var(--muted);
             font-weight: 500; text-transform: uppercase; letter-spacing: 0.4px; }
  .row { display: flex; justify-content: space-between; padding: 4px 0;
         border-bottom: 1px dashed #21262d; font-size: 14px; }
  .row:last-child { border-bottom: none; }
  .row .k { color: var(--muted); }
  .row .v { font-family: ui-monospace, "SF Mono", monospace; font-size: 13px; }
  .pill { display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 12px;
          font-weight: 500; }
  .pill.running { background: rgba(63,185,80,0.16); color: var(--ok); }
  .pill.waiting_login { background: rgba(210,153,34,0.18); color: var(--warn); }
  .pill.idle { background: rgba(139,148,158,0.2); color: var(--muted); }
  .pill.failed { background: rgba(248,81,73,0.2); color: var(--err); }
  img.qr { display: block; max-width: 280px; width: 100%; border-radius: 6px;
           background: #fff; padding: 8px; margin-top: 8px; }
  .qr-empty { color: var(--muted); font-size: 13px; padding: 14px 0; }
  button { background: var(--accent); color: #fff; border: none; border-radius: 6px;
           padding: 7px 14px; font-size: 13px; cursor: pointer; margin-right: 6px; margin-top: 6px; }
  button:disabled { background: #30363d; color: var(--muted); cursor: not-allowed; }
  button.danger { background: var(--warn); }
  pre.log { background: #010409; border: 1px solid var(--border); border-radius: 6px;
            color: #c9d1d9; font-size: 12px; padding: 10px; max-height: 240px;
            overflow: auto; margin: 0; white-space: pre-wrap; word-break: break-all; }
  .footer { color: var(--muted); font-size: 11px; text-align: center;
            padding-top: 16px; }
  a { color: var(--accent); text-decoration: none; }
  .countdown { font-size: 22px; font-family: ui-monospace, monospace; color: var(--accent); }
</style>
</head>
<body>
  <h1>📖 WeRead Challenge <small id="now"></small></h1>
  <div class="grid">
    <div class="card">
      <h2>状态</h2>
      <div class="row"><span class="k">阶段</span><span class="v"><span id="phase" class="pill idle">—</span></span></div>
      <div class="row"><span class="k">PID</span><span class="v" id="pid">—</span></div>
      <div class="row"><span class="k">本次剩余</span><span class="v countdown" id="eta">—</span></div>
      <div class="row"><span class="k">上次开始</span><span class="v" id="started_at">—</span></div>
      <div class="row"><span class="k">上次结束</span><span class="v" id="ended_at">—</span></div>
      <div class="row"><span class="k">上次结果</span><span class="v" id="last_status">—</span></div>
      <div style="margin-top:10px">
        <button id="start-btn">▶ 手动触发阅读</button>
        <button id="restart-btn" class="danger">🔄 重启阅读</button>
      </div>
    </div>

    <div class="card">
      <h2>登录二维码</h2>
      <div id="qr-block">
        <div class="qr-empty">等待容器首次启动后生成…</div>
      </div>
      <div class="row" style="margin-top:6px">
        <span class="k">二维码时间</span><span class="v" id="qr_age">—</span>
      </div>
      <div class="row">
        <span class="k">cookies</span><span class="v" id="cookies">—</span>
      </div>
    </div>

    <div class="card" style="grid-column: 1 / -1">
      <h2>配置 (敏感值已隐藏)</h2>
      <div id="env"></div>
    </div>

    <div class="card" style="grid-column: 1 / -1">
      <h2>最近日志 <a href="/logs?n=500" target="_blank" style="float:right;font-size:12px">查看更多</a></h2>
      <pre class="log" id="logs">加载中…</pre>
    </div>
  </div>
  <div class="footer">
    <a href="/logout">退出登录</a> · auto-refresh 30s · <a href="/status">/status</a> · <a href="/healthz">/healthz</a>
  </div>

<script>
const $ = (id) => document.getElementById(id);

function fmtSeconds(s) {
  if (s == null) return "—";
  s = Math.max(0, Math.round(s));
  const m = Math.floor(s / 60), sec = s % 60;
  return String(m).padStart(2,"0") + ":" + String(sec).padStart(2,"0");
}
function fmtAge(s) {
  if (s == null) return "—";
  if (s < 60) return Math.round(s) + "s 前";
  if (s < 3600) return Math.round(s/60) + "min 前";
  if (s < 86400) return Math.round(s/3600) + "h 前";
  return Math.round(s/86400) + "d 前";
}
function fmtTime(iso) {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

async function refresh() {
  $("now").textContent = new Date().toLocaleTimeString();
  let data;
  try {
    const resp = await fetch("/status", {cache: "no-store"});
    if (resp.status === 401) { window.location.href = "/login"; return; }
    data = await resp.json();
  } catch (e) {
    return;
  }
  const r = data.reading || {};
  const lr = r.last_run || {};
  const phaseEl = $("phase");
  phaseEl.className = "pill " + (r.phase || "idle");
  phaseEl.textContent = ({
    running: "运行中", waiting_login: "等待登录", idle: "空闲", failed: "上次失败"
  })[r.phase] || r.phase || "—";

  $("pid").textContent = r.pid || "—";
  $("eta").textContent = fmtSeconds(r.eta_seconds);
  $("started_at").textContent = fmtTime(lr.started_at);
  $("ended_at").textContent = fmtTime(lr.ended_at);
  $("last_status").textContent = lr.status
    ? lr.status + (lr.exit_code != null ? " (code " + lr.exit_code + ")" : "")
    : "—";

  const c = r.cookies || {};
  $("cookies").textContent = c.status === "valid"
    ? "valid · " + (c.age_days ? c.age_days.toFixed(1) : "?") + " 天前"
    : c.status || "—";

  const qr = r.login_qr || {};
  const block = $("qr-block");
  if (qr.present) {
    block.innerHTML = '<img class="qr" src="/login.png?t=' + Date.now() + '" alt="login QR">';
  } else {
    block.innerHTML = '<div class="qr-empty">尚未生成二维码（cookies 有效时不会生成）</div>';
  }
  $("qr_age").textContent = fmtAge(qr.age_seconds);

  const envEl = $("env");
  envEl.innerHTML = "";
  Object.entries(data.env || {}).forEach(([k, v]) => {
    const div = document.createElement("div");
    div.className = "row";
    div.innerHTML = '<span class="k">' + k + '</span><span class="v">' + (v || "—") + '</span>';
    envEl.appendChild(div);
  });

  $("start-btn").disabled = (r.phase === "running");
  $("restart-btn").disabled = false;
}

async function loadLogs() {
  try {
    const txt = await (await fetch("/logs?n=120", {cache: "no-store"})).text();
    $("logs").textContent = txt || "(空)";
    const pre = $("logs"); pre.scrollTop = pre.scrollHeight;
  } catch {}
}

$("start-btn").addEventListener("click", async () => {
  $("start-btn").disabled = true;
  try {
    const r = await fetch("/start", {method: "POST"});
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      alert("触发失败：" + (j.reason || r.status));
    }
  } catch (e) {
    alert("请求失败：" + e);
  } finally { setTimeout(refresh, 1000); }
});

$("restart-btn").addEventListener("click", async () => {
  $("restart-btn").disabled = true;
  $("restart-btn").textContent = "重启中…";
  try {
    const r = await fetch("/restart", {method: "POST"});
    if (!r.ok) alert("重启失败：" + r.status);
  } catch (e) {
    alert("请求失败：" + e);
  } finally {
    setTimeout(() => {
      $("restart-btn").disabled = false;
      $("restart-btn").textContent = "🔄 重启阅读";
      refresh();
      loadLogs();
    }, 6000);
  }
});

refresh(); loadLogs();
setInterval(refresh, 30000);
setInterval(loadLogs, 30000);
</script>
</body>
</html>
"""


# ─── Main ─────────────────────────────────────────────────────────────────────
def _install_signal_handlers() -> None:
    def _shutdown(signum, _frame):
        print(f"[app] received signal {signum}, exiting", flush=True)
        os._exit(0)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            pass


if __name__ == "__main__":
    _install_signal_handlers()
    _start_background_threads()
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)
