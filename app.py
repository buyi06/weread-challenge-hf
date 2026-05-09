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
NOTIF_CONFIG = DATA_DIR / "notification_config.json"

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


def _is_paused(pid: int | None) -> bool:
    """Check if a process is in stopped (paused) state."""
    if pid is None:
        return False
    try:
        with open(f"/proc/{pid}/status", "r") as f:
            for line in f:
                if line.startswith("State:"):
                    return "stopped" in line.lower()
    except (FileNotFoundError, PermissionError):
        return False
    return False


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
        "is_paused": _is_paused(pid),
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

    if os.environ.get("PUSHPLUS_TOKEN"):
        channels.append({"name": "PushPlus", "configured": True})
    else:
        channels.append({"name": "PushPlus", "configured": False})

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
    _kill_reader(pid)
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    # Delete cookies + old QR so the new run generates a fresh login QR
    for f in [COOKIES_JSON, LOGIN_PNG]:
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass
    # Give the killed process time to fully exit
    for _ in range(20):
        time.sleep(0.1)
        if _pid_alive() != pid:
            break
    new_pid = _spawn_reader("manual")
    return jsonify({"ok": True, "killed_pid": pid, "spawned_pid": new_pid})


@app.route("/stop", methods=["POST"])
def route_stop() -> Response:
    """Pause: terminate the running reader without spawning a new one."""
    pid = _pid_alive()
    if pid is None:
        return jsonify({"ok": False, "reason": "not running"}), 409
    _kill_reader(pid)
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        last = _last_run() or {}
        last["status"] = "stopped"
        last["ended_at"] = _utcnow().isoformat()
        STATE_FILE.write_text(json.dumps(last, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return jsonify({"ok": True, "killed_pid": pid})


def _kill_reader(pid: int | None) -> None:
    """Terminate the reading process and its children."""
    if pid is None:
        return
    # Kill direct children first, then the main process
    try:
        # Find child PIDs via /proc
        child_pids = []
        try:
            for entry in os.listdir(f"/proc/{pid}/task"):
                try:
                    with open(f"/proc/{pid}/task/{entry}/children", "r") as f:
                        child_pids.extend(int(x) for x in f.read().split() if x.strip())
                except (FileNotFoundError, ValueError):
                    pass
        except (FileNotFoundError, PermissionError):
            pass
        # Kill children then parent
        for cpid in child_pids:
            try:
                os.kill(cpid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    # Delete the flock lockfile so next spawn isn't blocked
    LOCK_FILE = WEREAD_DATA_DIR / ".reading.lock"
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    # Wait in background thread so we don't block the Flask request
    def _wait_and_force():
        for _ in range(30):
            time.sleep(0.1)
            if _pid_alive() != pid:
                return
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    threading.Thread(target=_wait_and_force, daemon=True).start()


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


# ─── Notification config ──────────────────────────────────────────────────────
NOTIF_SCHEMA = {
    "bark": {"bark_key": "", "bark_url": "https://api.day.app"},
    "pushplus": {"pushplus_token": ""},
    "email": {
        "email_user": "", "email_pass": "", "email_to": "",
        "email_from": "", "email_smtp": "", "email_port": "465",
    },
    "webhook": {"webhook_url": ""},
}

_NOTIF_SECRET_FIELDS = {"bark_key", "pushplus_token", "email_pass"}
_NOTIF_ENV_MAP = {
    "bark_key": "BARK_KEY",
    "pushplus_token": "PUSHPLUS_TOKEN",
    "email_user": "EMAIL_USER", "email_pass": "EMAIL_PASS",
    "email_to": "EMAIL_TO",     "email_from": "EMAIL_FROM",
    "email_smtp": "EMAIL_SMTP", "email_port": "EMAIL_PORT",
    "webhook_url": "WEBHOOK_URL",
}


def _load_notif_config() -> dict:
    """Read saved notification config from disk; env vars override saved values."""
    cfg = {ch: dict(fields) for ch, fields in NOTIF_SCHEMA.items()}

    if NOTIF_CONFIG.exists():
        try:
            saved = json.loads(NOTIF_CONFIG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            saved = {}
        for ch, fields in cfg.items():
            saved_ch = saved.get(ch) or {}
            for field in fields:
                val = saved_ch.get(field, "")
                if isinstance(val, str) and val and val != "***set***":
                    cfg[ch][field] = val

    for ch, fields in cfg.items():
        for field in fields:
            env_key = _NOTIF_ENV_MAP.get(field)
            if env_key:
                env_val = os.environ.get(env_key, "")
                if env_val:
                    cfg[ch][field] = env_val
    return cfg


@app.route("/api/notification", methods=["GET"])
def route_notif_get() -> Response:
    """Return current notification config with secret fields masked."""
    cfg = _load_notif_config()
    masked: dict = {}
    for ch, fields in cfg.items():
        masked[ch] = {}
        for k, v in fields.items():
            if k in _NOTIF_SECRET_FIELDS:
                masked[ch][k] = "***set***" if v else ""
            else:
                masked[ch][k] = v
    return jsonify({"ok": True, "config": masked, "schema": NOTIF_SCHEMA})


@app.route("/api/notification", methods=["POST"])
def route_notif_save() -> Response:
    """Persist notification config; ``***set***`` keeps the existing secret value."""
    incoming = request.get_json(silent=True) or {}
    existing: dict = {}
    if NOTIF_CONFIG.exists():
        try:
            existing = json.loads(NOTIF_CONFIG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    cleaned: dict = {}
    for ch, fields in NOTIF_SCHEMA.items():
        cleaned[ch] = {}
        sub_in = incoming.get(ch) or {}
        sub_old = (existing.get(ch) or {}) if isinstance(existing.get(ch), dict) else {}
        for field in fields:
            val = sub_in.get(field, "")
            if not isinstance(val, str):
                val = str(val)
            val = val.strip()
            if val == "***set***" and field in _NOTIF_SECRET_FIELDS:
                val = sub_old.get(field, "") if isinstance(sub_old.get(field, ""), str) else ""
            cleaned[ch][field] = val

    try:
        NOTIF_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        with NOTIF_CONFIG.open("w", encoding="utf-8") as f:
            json.dump(cleaned, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        return jsonify({"ok": False, "reason": str(exc)}), 500

    return jsonify({"ok": True})




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
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>WeRead Challenge · 登录</title>
<style>
  :root {
    --bg-0: #f5f5f7;
    --card: #ffffff;
    --border: rgba(0, 0, 0, 0.06);
    --border-strong: rgba(0, 0, 0, 0.10);
    --text: #1d1d1f;
    --muted: #6e6e73;
    --muted-2: #98989d;
    --accent: #07C160;
    --accent-2: #04a04f;
    --accent-glow: rgba(7, 193, 96, 0.20);
    --err: #ff3b30;
    --err-bg: #fff2f1;
    --field-bg: #fafafa;
    --field-bg-focus: #ffffff;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg-0: #0a0a0b;
      --card: rgba(28, 28, 32, 0.86);
      --border: rgba(255, 255, 255, 0.08);
      --border-strong: rgba(255, 255, 255, 0.14);
      --text: #f4f4f7;
      --muted: #a1a1a6;
      --muted-2: #636366;
      --accent-glow: rgba(7, 193, 96, 0.32);
      --err: #ff6b60;
      --err-bg: rgba(255, 59, 48, 0.10);
      --field-bg: rgba(0, 0, 0, 0.32);
      --field-bg-focus: rgba(0, 0, 0, 0.5);
    }
  }
  * { box-sizing: border-box; -webkit-font-smoothing: antialiased; }
  html, body { height: 100%; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text",
                 "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    color: var(--text);
    background:
      radial-gradient(1200px 600px at 50% -10%, rgba(7,193,96,0.08), transparent 60%),
      radial-gradient(900px 600px at 90% 110%, rgba(10, 132, 255, 0.06), transparent 60%),
      var(--bg-0);
    background-attachment: fixed;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh; padding: 24px;
  }
  .card {
    width: min(380px, 100%);
    background: var(--card);
    backdrop-filter: blur(20px) saturate(160%);
    -webkit-backdrop-filter: blur(20px) saturate(160%);
    border: 1px solid var(--border);
    border-radius: 18px;
    padding: 36px 32px 28px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.03), 0 18px 48px -16px rgba(0,0,0,0.10);
    animation: fadeUp 0.5s cubic-bezier(0.16, 1, 0.3, 1);
  }
  @keyframes fadeUp {
    from { opacity: 0; transform: translateY(8px) scale(0.98); }
    to   { opacity: 1; transform: translateY(0)   scale(1);    }
  }
  .logo {
    width: 56px; height: 56px;
    border-radius: 16px;
    background: linear-gradient(135deg, var(--accent) 0%, var(--accent-2) 100%);
    box-shadow: 0 10px 24px -8px var(--accent-glow);
    display: flex; align-items: center; justify-content: center;
    margin: 0 auto 20px;
    font-size: 28px;
  }
  h1 { margin: 0 0 6px; text-align: center; font-size: 21px; font-weight: 600; letter-spacing: -0.015em; }
  .sub { text-align: center; color: var(--muted); font-size: 13px; margin-bottom: 26px; }
  form { display: flex; flex-direction: column; gap: 14px; }
  input[type="password"] {
    width: 100%; padding: 12px 14px;
    background: var(--field-bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    color: var(--text);
    font-size: 14px; font-family: inherit;
    outline: none;
    transition: border-color 0.2s, box-shadow 0.2s, background 0.2s;
    min-height: 44px;
  }
  input[type="password"]::placeholder { color: var(--muted-2); }
  input[type="password"]:focus {
    border-color: var(--accent);
    background: var(--field-bg-focus);
    box-shadow: 0 0 0 4px var(--accent-glow);
  }
  button {
    width: 100%; padding: 12px;
    border: none; border-radius: 10px;
    font-size: 14px; font-weight: 600; font-family: inherit;
    color: #fff;
    background: linear-gradient(180deg, var(--accent) 0%, var(--accent-2) 100%);
    cursor: pointer;
    transition: transform 0.12s ease, box-shadow 0.2s ease, filter 0.15s;
    box-shadow: 0 6px 16px -6px var(--accent-glow);
    min-height: 44px;
    letter-spacing: 0.05em;
  }
  button:hover  { transform: translateY(-1px); box-shadow: 0 10px 22px -8px var(--accent-glow); filter: brightness(1.04); }
  button:active { transform: translateY(0);    filter: brightness(0.96); }
  .err {
    color: var(--err);
    font-size: 13px;
    background: var(--err-bg);
    border: 1px solid rgba(255, 59, 48, 0.18);
    padding: 9px 12px;
    border-radius: 8px;
    text-align: center;
    animation: shake 0.45s cubic-bezier(0.36, 0.07, 0.19, 0.97);
  }
  @keyframes shake {
    10%, 90% { transform: translateX(-1px); }
    20%, 80% { transform: translateX(2px);  }
    30%, 50%, 70% { transform: translateX(-4px); }
    40%, 60% { transform: translateX(4px);  }
  }
  .hint { color: var(--muted-2); font-size: 11px; text-align: center; margin-top: 18px; }
  .hint code {
    background: rgba(0,0,0,0.04);
    padding: 1px 6px; border-radius: 4px;
    font-family: ui-monospace, "SF Mono", monospace;
    font-size: 11px; color: var(--muted);
  }
  @media (prefers-color-scheme: dark) {
    .hint code { background: rgba(255,255,255,0.08); }
  }
</style>
</head>
<body>
  <div class="card">
    <div class="logo">📖</div>
    <h1>WeRead Challenge</h1>
    <div class="sub">输入访问密码以继续</div>
    {% if error %}<div class="err">密码错误，请重试</div>{% endif %}
    <form method="POST" autocomplete="off">
      <input type="password" name="password" placeholder="访问密码" autofocus required>
      <button type="submit">登 录</button>
    </form>
    <div class="hint">通过环境变量 <code>WEB_PASSWORD</code> 修改</div>
  </div>
</body>
</html>
"""


# ─── Dashboard HTML ───────────────────────────────────────────────────────────
_INDEX_HTML = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>WeRead Challenge</title>
<script>
  (function(){
    try {
      var t = localStorage.getItem("weread-theme");
      if (t === "light" || t === "dark") {
        document.documentElement.setAttribute("data-theme", t);
      }
    } catch(e) {}
  })();
</script>
<style>
  :root, [data-theme="light"] {
    --bg-0: #f5f5f7;
    --bg-1: #fbfbfd;
    --card: #ffffff;
    --card-2: #fafafa;
    --border: rgba(0, 0, 0, 0.07);
    --border-strong: rgba(0, 0, 0, 0.14);
    --hairline: rgba(0, 0, 0, 0.05);
    --text: #1d1d1f;
    --text-2: #424245;
    --muted: #6e6e73;
    --muted-2: #98989d;
    --accent: #07C160;
    --accent-2: #04a04f;
    --accent-soft: rgba(7, 193, 96, 0.10);
    --accent-glow: rgba(7, 193, 96, 0.22);
    --warn: #ff9500;
    --warn-2: #cc7700;
    --warn-soft: rgba(255, 149, 0, 0.12);
    --warn-glow: rgba(255, 149, 0, 0.20);
    --err: #ff3b30;
    --err-2: #d92e25;
    --err-soft: rgba(255, 59, 48, 0.10);
    --err-glow: rgba(255, 59, 48, 0.20);
    --info: #0a84ff;
    --info-soft: rgba(10, 132, 255, 0.08);
    --shadow-sm: 0 1px 2px rgba(0,0,0,0.04);
    --shadow: 0 1px 3px rgba(0,0,0,0.04), 0 8px 24px -8px rgba(0,0,0,0.08);
    --shadow-lg: 0 12px 36px -8px rgba(0,0,0,0.12);
    --field-bg: #f5f5f7;
    --field-bg-focus: #ffffff;
    --pill-bg: rgba(0, 0, 0, 0.04);
    --code-bg: rgba(0, 0, 0, 0.05);
    --header-bg: rgba(255, 255, 255, 0.78);
    --log-bg: #fbfbfd;
    --scrollbar: rgba(0, 0, 0, 0.18);
  }
  [data-theme="dark"] {
    --bg-0: #0a0a0b;
    --bg-1: #111114;
    --card: rgba(28, 28, 32, 0.78);
    --card-2: rgba(34, 34, 40, 0.78);
    --border: rgba(255, 255, 255, 0.08);
    --border-strong: rgba(255, 255, 255, 0.16);
    --hairline: rgba(255, 255, 255, 0.06);
    --text: #f4f4f7;
    --text-2: #d4d4d8;
    --muted: #a1a1a6;
    --muted-2: #636366;
    --accent-soft: rgba(7, 193, 96, 0.14);
    --accent-glow: rgba(7, 193, 96, 0.36);
    --warn: #ffa726;
    --warn-2: #ff9500;
    --warn-soft: rgba(255, 167, 38, 0.14);
    --warn-glow: rgba(255, 167, 38, 0.32);
    --err: #ff6b60;
    --err-soft: rgba(255, 91, 91, 0.14);
    --err-glow: rgba(255, 91, 91, 0.32);
    --info: #4f8cff;
    --info-soft: rgba(79, 140, 255, 0.14);
    --shadow-sm: 0 1px 2px rgba(0,0,0,0.5);
    --shadow: 0 4px 12px rgba(0,0,0,0.4), 0 16px 48px -12px rgba(0,0,0,0.4);
    --shadow-lg: 0 24px 64px -16px rgba(0,0,0,0.6);
    --field-bg: rgba(0, 0, 0, 0.32);
    --field-bg-focus: rgba(0, 0, 0, 0.5);
    --pill-bg: rgba(255, 255, 255, 0.05);
    --code-bg: rgba(255, 255, 255, 0.08);
    --header-bg: rgba(10, 10, 11, 0.72);
    --log-bg: rgba(0, 0, 0, 0.28);
    --scrollbar: rgba(255, 255, 255, 0.18);
  }
  * { box-sizing: border-box; -webkit-font-smoothing: antialiased; }
  html, body { height: 100%; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text",
                 "Segoe UI", "PingFang SC", "Microsoft YaHei", "Helvetica Neue", sans-serif;
    color: var(--text);
    background:
      radial-gradient(1200px 600px at 50% -200px, rgba(7,193,96,0.05), transparent 70%),
      radial-gradient(900px 600px at 100% 100%, rgba(10, 132, 255, 0.05), transparent 60%),
      var(--bg-0);
    background-attachment: fixed;
    min-height: 100vh;
    line-height: 1.5;
    transition: background 0.3s, color 0.3s;
  }
  a { color: var(--info); text-decoration: none; transition: color 0.15s; }
  a:hover { opacity: 0.78; }

  /* ───── Header ───── */
  header {
    position: sticky; top: 0; z-index: 50;
    backdrop-filter: blur(20px) saturate(180%);
    -webkit-backdrop-filter: blur(20px) saturate(180%);
    background: var(--header-bg);
    border-bottom: 1px solid var(--hairline);
    padding: 12px 20px;
    transition: background 0.3s, border-color 0.3s;
  }
  .header-inner {
    max-width: 880px; margin: 0 auto;
    display: flex; align-items: center; justify-content: space-between;
    gap: 12px;
  }
  .brand {
    display: flex; align-items: center; gap: 10px;
    font-weight: 600; font-size: 15px; letter-spacing: -0.015em;
  }
  .brand-icon {
    width: 30px; height: 30px;
    border-radius: 9px;
    background: linear-gradient(135deg, var(--accent) 0%, var(--accent-2) 100%);
    box-shadow: 0 4px 12px -2px var(--accent-glow);
    display: flex; align-items: center; justify-content: center;
    font-size: 17px;
  }
  .header-actions { display: flex; align-items: center; gap: 8px; }
  .icon-btn {
    background: var(--pill-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 6px 12px;
    color: var(--text-2);
    font-size: 12px; font-family: inherit; font-weight: 500;
    cursor: pointer;
    transition: all 0.15s;
    white-space: nowrap; line-height: 1.2;
    text-decoration: none;
  }
  .icon-btn:hover {
    background: var(--card-2);
    border-color: var(--border-strong);
    color: var(--text);
  }
  #theme-toggle {
    width: 32px; height: 32px; padding: 0;
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 14px;
  }

  /* ───── Layout ───── */
  main {
    max-width: 880px; margin: 0 auto;
    padding: 18px 20px 40px;
    display: grid; gap: 14px;
  }

  /* ───── Card ───── */
  .card {
    background: var(--card);
    backdrop-filter: blur(20px) saturate(160%);
    -webkit-backdrop-filter: blur(20px) saturate(160%);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 20px;
    box-shadow: var(--shadow-sm);
    transition: border-color 0.2s, box-shadow 0.2s, background 0.3s;
    animation: fadeUp 0.45s cubic-bezier(0.16, 1, 0.3, 1) backwards;
  }
  .card:hover { border-color: var(--border-strong); box-shadow: var(--shadow); }
  .card-title {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 14px; gap: 10px;
  }
  .card-title h2 {
    margin: 0; font-size: 11px; font-weight: 600;
    color: var(--muted); text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  .card-title .right { display: flex; gap: 8px; align-items: center; }
  @keyframes fadeUp {
    from { opacity: 0; transform: translateY(6px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  main > .card:nth-child(1) { animation-delay: 0.00s; }
  main > .card:nth-child(2) { animation-delay: 0.05s; }
  main > .card:nth-child(3) { animation-delay: 0.10s; }
  main > .card:nth-child(4) { animation-delay: 0.15s; }
  main > .card:nth-child(5) { animation-delay: 0.20s; }

  /* ───── Phase pill ───── */
  .phase {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 6px 12px; border-radius: 100px;
    font-size: 13px; font-weight: 500;
    background: var(--pill-bg);
    border: 1px solid var(--border);
    transition: color 0.3s, border-color 0.3s, background 0.3s;
    white-space: nowrap;
  }
  .phase .dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--muted-2);
    transition: background 0.3s;
    position: relative;
  }
  .phase.running       { color: var(--accent); border-color: rgba(7,193,96,0.30); background: var(--accent-soft); }
  .phase.running .dot  { background: var(--accent); }
  .phase.running .dot::after {
    content: ""; position: absolute; inset: -3px;
    border-radius: 50%; border: 2px solid var(--accent);
    animation: ping 1.8s cubic-bezier(0, 0, 0.2, 1) infinite;
  }
  .phase.waiting_login { color: var(--warn-2); border-color: rgba(255,149,0,0.30); background: var(--warn-soft); }
  .phase.waiting_login .dot { background: var(--warn); }
  .phase.waiting_login .dot::after {
    content: ""; position: absolute; inset: -3px;
    border-radius: 50%; border: 2px solid var(--warn);
    animation: ping 1.8s cubic-bezier(0, 0, 0.2, 1) infinite;
  }
  .phase.idle          { color: var(--muted); }
  .phase.idle .dot     { background: var(--muted-2); }
  .phase.failed        { color: var(--err);    border-color: rgba(255,59,48,0.30); background: var(--err-soft); }
  .phase.failed .dot   { background: var(--err); }
  @keyframes ping {
    0%   { opacity: 0.7; transform: scale(1);   }
    80%  { opacity: 0;   transform: scale(2.2); }
    100% { opacity: 0;   transform: scale(2.2); }
  }
  #header-phase { font-size: 12px; padding: 4px 10px; }

  /* ───── Hero ───── */
  .hero-top {
    display: flex; align-items: flex-start; justify-content: space-between;
    gap: 16px; flex-wrap: wrap;
  }
  .phase-block { min-width: 0; flex: 1 1 200px; }
  .countdown-wrap { text-align: right; flex: 0 0 auto; }
  .countdown-label {
    color: var(--muted); font-size: 11px;
    text-transform: uppercase; letter-spacing: 0.08em;
    margin-bottom: 4px;
  }
  .countdown {
    font-family: ui-monospace, "SF Mono", "JetBrains Mono", monospace;
    font-size: 38px; font-weight: 600; letter-spacing: -0.03em;
    line-height: 1.0; font-variant-numeric: tabular-nums;
    color: var(--text);
  }
  .countdown.dim { color: var(--muted-2); font-size: 24px; font-weight: 400; }
  #login-status { font-size: 13px; color: var(--muted); margin-top: 10px; }

  .hero-meta {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 14px;
    padding: 16px 0;
    margin: 18px 0 16px;
    border-top: 1px solid var(--hairline);
    border-bottom: 1px solid var(--hairline);
  }
  .meta-item .k {
    font-size: 11px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.06em;
    margin-bottom: 5px; font-weight: 500;
  }
  .meta-item .v {
    font-size: 13px; color: var(--text);
    font-variant-numeric: tabular-nums;
  }
  .meta-item .v.mono { font-family: ui-monospace, "SF Mono", monospace; font-size: 12px; }
  .meta-item .v.ok   { color: var(--accent); }
  .meta-item .v.warn { color: var(--warn-2); }
  .meta-item .v.err  { color: var(--err); }

  /* ───── Buttons ───── */
  .btn-row { display: flex; gap: 10px; flex-wrap: wrap; }
  .btn {
    display: inline-flex; align-items: center; justify-content: center; gap: 8px;
    padding: 11px 18px;
    border: 1px solid transparent;
    border-radius: 10px;
    font-size: 13px; font-weight: 600; font-family: inherit;
    cursor: pointer;
    transition: transform 0.12s ease, box-shadow 0.2s ease, filter 0.15s, background 0.15s, border-color 0.15s;
    flex: 1 1 0; min-width: 110px; min-height: 44px;
    letter-spacing: -0.005em;
    color: #fff;
  }
  .btn:hover:not(:disabled)  { transform: translateY(-1px); filter: brightness(1.04); }
  .btn:active:not(:disabled) { transform: translateY(0);    filter: brightness(0.95); }
  .btn:disabled { opacity: 0.42; cursor: not-allowed; }
  .btn.primary {
    background: linear-gradient(180deg, var(--accent) 0%, var(--accent-2) 100%);
    box-shadow: 0 1px 2px rgba(0,0,0,0.04), 0 6px 16px -8px var(--accent-glow);
  }
  .btn.warn {
    background: linear-gradient(180deg, var(--warn) 0%, var(--warn-2) 100%);
    box-shadow: 0 1px 2px rgba(0,0,0,0.04), 0 6px 16px -8px var(--warn-glow);
  }
  .btn.danger {
    background: linear-gradient(180deg, var(--err) 0%, var(--err-2) 100%);
    box-shadow: 0 1px 2px rgba(0,0,0,0.04), 0 6px 16px -8px var(--err-glow);
  }
  .btn.ghost {
    background: var(--card); color: var(--text-2);
    border-color: var(--border);
    box-shadow: var(--shadow-sm);
  }
  .btn.ghost:hover:not(:disabled) {
    background: var(--card-2); color: var(--text);
    border-color: var(--border-strong);
  }
  .btn .spinner {
    display: none;
    width: 14px; height: 14px;
    border: 2px solid rgba(255,255,255,0.32);
    border-top-color: #fff; border-radius: 50%;
    animation: spin 0.7s linear infinite; flex: 0 0 auto;
  }
  .btn.ghost .spinner { border-color: rgba(0,0,0,0.18); border-top-color: var(--text); }
  [data-theme="dark"] .btn.ghost .spinner { border-color: rgba(255,255,255,0.18); border-top-color: var(--text); }
  .btn.loading .spinner { display: inline-block; }
  .btn.loading .label   { opacity: 0.72; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ───── QR ───── */
  .qr-wrap {
    display: flex; flex-direction: column; align-items: center;
    padding: 6px 0;
  }
  .qr-img {
    width: min(240px, 80%);
    background: #fff;
    border-radius: 14px;
    padding: 14px;
    margin-bottom: 14px;
    box-shadow: var(--shadow-lg);
    border: 1px solid var(--hairline);
  }
  .qr-img img { display: block; width: 100%; height: auto; }
  .qr-title { font-size: 14px; font-weight: 500; margin-bottom: 4px; }
  .qr-msg { color: var(--muted); font-size: 12px; text-align: center; line-height: 1.5; }
  .qr-empty {
    text-align: center; color: var(--muted); font-size: 13px;
    padding: 28px 0;
  }
  .qr-empty .big { font-size: 36px; margin-bottom: 10px; }

  /* ───── Logs ───── */
  .log-list {
    display: flex; flex-direction: column; gap: 4px;
    max-height: 320px; overflow-y: auto;
    padding: 4px;
    background: var(--log-bg);
    border-radius: 10px;
    border: 1px solid var(--hairline);
  }
  .log-list::-webkit-scrollbar { width: 6px; }
  .log-list::-webkit-scrollbar-thumb { background: var(--scrollbar); border-radius: 3px; }
  .log-list::-webkit-scrollbar-track { background: transparent; }
  .log-entry {
    display: block;
    padding: 6px 10px; border-radius: 6px;
    font-family: ui-monospace, "SF Mono", monospace;
    font-size: 12px; line-height: 1.55;
    color: var(--text-2);
    background: var(--card);
    border-left: 2px solid var(--muted-2);
    word-break: break-all;
    transition: background 0.15s;
  }
  .log-entry:hover { background: var(--card-2); }
  .log-entry.success { border-left-color: var(--accent); }
  .log-entry.error   { border-left-color: var(--err);   color: var(--err-2); }
  .log-entry.warning { border-left-color: var(--warn);  color: var(--warn-2); }
  .log-entry.info    { border-left-color: var(--info); }
  [data-theme="dark"] .log-entry.error   { color: #ffd9d9; }
  [data-theme="dark"] .log-entry.warning { color: #ffe4be; }
  .log-empty { color: var(--muted); font-size: 13px; padding: 20px; text-align: center; }

  /* ───── Collapsible ───── */
  .collapsible-head { cursor: pointer; user-select: none; }
  .chev {
    display: inline-block; width: 12px; height: 12px;
    transform: rotate(180deg); transition: transform 0.25s ease;
    color: var(--muted); margin-left: 4px; font-size: 10px; line-height: 1;
  }
  .collapsed .chev { transform: rotate(90deg); }
  .collapsible-body {
    overflow: hidden;
    transition: grid-template-rows 0.3s ease, opacity 0.25s ease, margin-top 0.25s ease;
    display: grid; grid-template-rows: 1fr;
    margin-top: 14px; opacity: 1;
  }
  .collapsed .collapsible-body { grid-template-rows: 0fr; opacity: 0; margin-top: 0; }
  .collapsible-inner { min-height: 0; overflow: hidden; }

  /* ───── Notification config ───── */
  .notif-channel {
    background: var(--card-2);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 14px 16px;
    margin-bottom: 10px;
    transition: border-color 0.2s, box-shadow 0.2s;
  }
  .notif-channel:hover { border-color: var(--border-strong); box-shadow: var(--shadow-sm); }
  .notif-head {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 12px; gap: 8px;
  }
  .notif-name { font-weight: 600; font-size: 14px; display: flex; align-items: center; gap: 8px; }
  .notif-status {
    font-size: 11px; padding: 2px 9px; border-radius: 6px;
    border: 1px solid transparent; font-weight: 500;
  }
  .notif-status.ok { color: var(--accent); background: var(--accent-soft); border-color: rgba(7,193,96,0.22); }
  .notif-status.no { color: var(--muted);  background: var(--pill-bg); border-color: var(--border); }
  .notif-fields { display: grid; grid-template-columns: 1fr; gap: 10px; }
  @media (min-width: 640px) { .notif-fields.two { grid-template-columns: 1fr 1fr; } }
  .notif-field label {
    display: block; color: var(--muted);
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
    margin-bottom: 5px; font-weight: 500;
  }
  .notif-field input {
    width: 100%; padding: 9px 12px;
    background: var(--field-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-size: 13px; font-family: inherit;
    outline: none;
    transition: border-color 0.2s, box-shadow 0.2s, background 0.2s;
    min-height: 38px;
  }
  .notif-field input::placeholder { color: var(--muted-2); }
  .notif-field input:focus {
    border-color: var(--accent);
    background: var(--field-bg-focus);
    box-shadow: 0 0 0 4px var(--accent-glow);
  }
  .save-bar { display: flex; justify-content: flex-end; margin-top: 12px; }
  .save-bar .btn { flex: 0 0 auto; min-width: 140px; }

  /* ───── Env table ───── */
  .env-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 8px 4px;
    border-bottom: 1px solid var(--hairline);
    gap: 10px;
  }
  .env-row:last-child { border-bottom: none; }
  .env-row .k { color: var(--muted); font-family: ui-monospace, "SF Mono", monospace; font-size: 12px; }
  .env-row .v { color: var(--text);  font-family: ui-monospace, "SF Mono", monospace; font-size: 12px;
                text-align: right; word-break: break-all; }
  .env-row .v.muted { color: var(--muted-2); }
  .env-row .v.set   { color: var(--accent); font-weight: 500; }

  /* ───── Footer ───── */
  footer {
    color: var(--muted-2); font-size: 11px;
    text-align: center;
    padding: 12px 20px 28px;
  }
  footer a { color: var(--muted); }
  footer a:hover { color: var(--text); opacity: 1; }

  /* ───── Toast ───── */
  .toast-stack {
    position: fixed; top: 18px; right: 18px; z-index: 200;
    display: flex; flex-direction: column; gap: 8px;
    pointer-events: none;
  }
  .toast {
    pointer-events: auto;
    min-width: 220px; max-width: 360px;
    background: var(--card);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border: 1px solid var(--border-strong);
    border-radius: 10px;
    padding: 11px 14px;
    color: var(--text);
    font-size: 13px; font-weight: 500;
    box-shadow: var(--shadow-lg);
    animation: toastIn 0.28s cubic-bezier(0.16, 1, 0.3, 1);
  }
  .toast.fade { animation: toastOut 0.25s forwards; }
  .toast.success { border-left: 3px solid var(--accent); }
  .toast.error   { border-left: 3px solid var(--err); }
  .toast.info    { border-left: 3px solid var(--info); }
  .toast.warn    { border-left: 3px solid var(--warn); }
  @keyframes toastIn  { from { transform: translateX(110%); opacity: 0; } }
  @keyframes toastOut { to   { transform: translateX(110%); opacity: 0; } }

  /* ───── Modal ───── */
  .modal-mask {
    position: fixed; inset: 0; z-index: 150;
    background: rgba(0,0,0,0.45);
    backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
    display: none; align-items: center; justify-content: center;
    padding: 20px;
    animation: maskIn 0.2s ease;
  }
  .modal-mask.show { display: flex; }
  .modal {
    background: var(--card);
    border: 1px solid var(--border-strong);
    border-radius: 14px;
    padding: 22px;
    max-width: 360px; width: 100%;
    animation: modalIn 0.28s cubic-bezier(0.16, 1, 0.3, 1);
    box-shadow: var(--shadow-lg);
  }
  .modal h3 { margin: 0 0 8px; font-size: 16px; font-weight: 600; letter-spacing: -0.01em; }
  .modal p  { margin: 0 0 18px; font-size: 13px; color: var(--muted); line-height: 1.6; }
  .modal .btn-row  { justify-content: flex-end; gap: 8px; }
  .modal .btn      { flex: 0 0 auto; min-width: 96px; }
  @keyframes maskIn  { from { opacity: 0; } }
  @keyframes modalIn { from { opacity: 0; transform: translateY(8px) scale(0.98); } }

  @media (max-width: 600px) {
    main { padding: 14px; }
    .countdown-wrap { text-align: left; }
    .countdown { font-size: 30px; }
    .btn { flex: 1 1 100%; }
    .toast-stack { left: 14px; right: 14px; top: 14px; }
    .toast { max-width: none; min-width: 0; }
  }
</style>
</head>
<body>
  <header>
    <div class="header-inner">
      <div class="brand">
        <div class="brand-icon">📖</div>
        <span>WeRead Challenge</span>
      </div>
      <div class="header-actions">
        <span id="header-phase" class="phase idle"><span class="dot"></span><span class="label">空闲</span></span>
        <button id="theme-toggle" class="icon-btn" title="切换主题" aria-label="切换主题">⚙</button>
        <a class="icon-btn" href="/logout">退出</a>
      </div>
    </div>
  </header>

  <main>
    <!-- 1. Status hero -->
    <section class="card">
      <div class="hero-top">
        <div class="phase-block">
          <div class="card-title"><h2>当前状态</h2></div>
          <div id="phase" class="phase idle"><span class="dot"></span><span class="label">空闲</span></div>
          <div id="login-status">—</div>
        </div>
        <div class="countdown-wrap">
          <div class="countdown-label">本次剩余</div>
          <div id="eta" class="countdown dim">—</div>
        </div>
      </div>

      <div class="hero-meta">
        <div class="meta-item"><div class="k">触发</div><div class="v" id="trigger">—</div></div>
        <div class="meta-item"><div class="k">开始</div><div class="v mono" id="started_at">—</div></div>
        <div class="meta-item"><div class="k">结束</div><div class="v mono" id="ended_at">—</div></div>
        <div class="meta-item"><div class="k">结果</div><div class="v" id="last_status">—</div></div>
      </div>

      <div class="btn-row">
        <button id="start-btn" class="btn primary">
          <span class="spinner"></span><span class="label">▶ 开始</span>
        </button>
        <button id="pause-btn" class="btn warn">
          <span class="spinner"></span><span class="label">⏹ 停止</span>
        </button>
        <button id="restart-btn" class="btn ghost">
          <span class="spinner"></span><span class="label">🔄 重启</span>
        </button>
      </div>
    </section>

    <!-- 2. QR -->
    <section class="card" id="qr-card">
      <div class="card-title"><h2>登录</h2></div>
      <div id="qr-block">
        <div class="qr-empty">
          <div class="big">⏳</div>
          <div>等待状态…</div>
        </div>
      </div>
    </section>

    <!-- 3. Logs -->
    <section class="card">
      <div class="card-title">
        <h2>运行日志</h2>
        <div class="right">
          <a class="icon-btn" href="/logs?n=500" target="_blank" rel="noopener">完整日志</a>
        </div>
      </div>
      <div id="logs" class="log-list"><div class="log-empty">加载中…</div></div>
    </section>

    <!-- 4. Notification config -->
    <section class="card" id="notif-card">
      <div class="card-title collapsible-head" data-toggle="notif-card">
        <h2>通知配置</h2>
        <div class="right">
          <span id="notif-summary" style="font-size:11px; color: var(--muted);">—</span>
          <span class="chev">▾</span>
        </div>
      </div>
      <div class="collapsible-body"><div class="collapsible-inner">
        <div class="notif-channel">
          <div class="notif-head">
            <div class="notif-name">🔔 Bark</div>
            <span id="bark-status" class="notif-status no">未配置</span>
          </div>
          <div class="notif-fields two">
            <div class="notif-field"><label>Bark Key</label><input id="bark_key" type="password" autocomplete="off" placeholder="Bark Key"></div>
            <div class="notif-field"><label>Bark URL</label><input id="bark_url" type="text" placeholder="https://api.day.app"></div>
          </div>
        </div>

        <div class="notif-channel">
          <div class="notif-head">
            <div class="notif-name">💬 PushPlus</div>
            <span id="pushplus-status" class="notif-status no">未配置</span>
          </div>
          <div class="notif-fields">
            <div class="notif-field"><label>Token</label><input id="pushplus_token" type="password" autocomplete="off" placeholder="微信扫码关注 PushPlus 后获取"></div>
          </div>
        </div>

        <div class="notif-channel">
          <div class="notif-head">
            <div class="notif-name">✉️ 邮件</div>
            <span id="email-status" class="notif-status no">未配置</span>
          </div>
          <div class="notif-fields two">
            <div class="notif-field"><label>SMTP 服务器</label><input id="email_smtp" type="text" placeholder="smtp.qq.com"></div>
            <div class="notif-field"><label>SMTP 端口</label><input id="email_port" type="text" placeholder="465"></div>
            <div class="notif-field"><label>邮箱用户</label><input id="email_user" type="text" autocomplete="off" placeholder="user@example.com"></div>
            <div class="notif-field"><label>邮箱密码</label><input id="email_pass" type="password" autocomplete="new-password" placeholder="授权码或密码"></div>
            <div class="notif-field"><label>发件邮箱</label><input id="email_from" type="text" placeholder="from@example.com"></div>
            <div class="notif-field"><label>收件邮箱</label><input id="email_to" type="text" placeholder="to@example.com"></div>
          </div>
        </div>

        <div class="notif-channel">
          <div class="notif-head">
            <div class="notif-name">🪝 Webhook</div>
            <span id="webhook-status" class="notif-status no">未配置</span>
          </div>
          <div class="notif-fields">
            <div class="notif-field"><label>Webhook URL</label><input id="webhook_url" type="text" placeholder="https://..."></div>
          </div>
        </div>

        <div class="save-bar">
          <button id="notif-save" class="btn primary">
            <span class="spinner"></span><span class="label">保存通知配置</span>
          </button>
        </div>
      </div></div>
    </section>

    <!-- 5. Env vars (collapsed by default) -->
    <section class="card collapsed" id="env-card">
      <div class="card-title collapsible-head" data-toggle="env-card">
        <h2>环境变量</h2>
        <div class="right">
          <span style="font-size:11px; color: var(--muted);">敏感值已隐藏</span>
          <span class="chev">▾</span>
        </div>
      </div>
      <div class="collapsible-body"><div class="collapsible-inner">
        <div id="env"></div>
      </div></div>
    </section>
  </main>

  <footer>
    weread-challenge-hf · auto-refresh 30s · <span id="now">—</span>
    · <a href="/status" target="_blank">/status</a>
    · <a href="/healthz" target="_blank">/healthz</a>
    · <a href="/logout">退出登录</a>
  </footer>

  <div class="toast-stack" id="toasts"></div>
  <div class="modal-mask" id="modal-mask">
    <div class="modal">
      <h3 id="modal-title">确认</h3>
      <p id="modal-body"></p>
      <div class="btn-row">
        <button class="btn ghost" id="modal-cancel">取消</button>
        <button class="btn danger" id="modal-ok">确定</button>
      </div>
    </div>
  </div>

{% raw %}
<script>
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const PHASE_LABELS   = { running: "阅读中", waiting_login: "等待登录", idle: "空闲", failed: "上次失败" };
  const TRIGGER_LABELS = { initial: "启动", manual: "手动", scheduler: "定时" };
  const SECRET_KEYS    = new Set(["bark_key", "pushplus_token", "email_pass"]);
  const THEME_ORDER    = ["auto", "light", "dark"];
  const THEME_ICON     = { auto: "⚙", light: "☀", dark: "🌙" };
  const THEME_LABEL    = { auto: "跟随系统", light: "浅色", dark: "深色" };
  let lastNotifConfig  = {};
  let currentPhase     = "idle";

  /* ---------- Theme ---------- */
  function getStoredTheme() {
    try { return localStorage.getItem("weread-theme") || "auto"; }
    catch (e) { return "auto"; }
  }
  function applyTheme(theme) {
    const html = document.documentElement;
    if (theme === "auto") html.removeAttribute("data-theme");
    else                  html.setAttribute("data-theme", theme);
    const btn = $("theme-toggle");
    if (btn) {
      btn.textContent = THEME_ICON[theme];
      btn.title = "主题：" + THEME_LABEL[theme] + "（点击切换）";
    }
  }
  function cycleTheme() {
    const cur = getStoredTheme();
    const next = THEME_ORDER[(THEME_ORDER.indexOf(cur) + 1) % THEME_ORDER.length];
    try { localStorage.setItem("weread-theme", next); } catch (e) {}
    applyTheme(next);
    toast("主题：" + THEME_LABEL[next], "info");
  }
  applyTheme(getStoredTheme());
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (getStoredTheme() === "auto") applyTheme("auto");
    });
  }

  /* ---------- Formatters ---------- */
  function fmtSeconds(s) {
    if (s == null) return "—";
    s = Math.max(0, Math.round(s));
    const m = Math.floor(s / 60), sec = s % 60;
    return String(m).padStart(2, "0") + ":" + String(sec).padStart(2, "0");
  }
  function fmtAge(s) {
    if (s == null) return "—";
    if (s < 60)    return Math.round(s) + " 秒前";
    if (s < 3600)  return Math.round(s / 60) + " 分钟前";
    if (s < 86400) return Math.round(s / 3600) + " 小时前";
    return Math.round(s / 86400) + " 天前";
  }
  function fmtTime(iso) {
    if (!iso) return "—";
    try {
      const d = new Date(iso);
      const now = new Date();
      const sameDay = d.toDateString() === now.toDateString();
      const opts = sameDay
        ? { hour: "2-digit", minute: "2-digit" }
        : { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" };
      return d.toLocaleString(undefined, opts);
    } catch (e) { return iso; }
  }
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* ---------- Toast & Modal ---------- */
  function toast(msg, type) {
    type = type || "info";
    const el = document.createElement("div");
    el.className = "toast " + type;
    el.textContent = msg;
    $("toasts").appendChild(el);
    setTimeout(() => {
      el.classList.add("fade");
      setTimeout(() => el.remove(), 280);
    }, 3000);
  }
  function confirmModal(title, body, okText, isDanger) {
    return new Promise((resolve) => {
      $("modal-title").textContent = title;
      $("modal-body").textContent  = body;
      const okBtn = $("modal-ok");
      okBtn.textContent = okText || "确定";
      okBtn.className   = "btn " + (isDanger ? "danger" : "primary");
      $("modal-mask").classList.add("show");
      const close = (val) => {
        $("modal-mask").classList.remove("show");
        okBtn.removeEventListener("click", onOk);
        $("modal-cancel").removeEventListener("click", onCancel);
        resolve(val);
      };
      const onOk = () => close(true);
      const onCancel = () => close(false);
      okBtn.addEventListener("click", onOk);
      $("modal-cancel").addEventListener("click", onCancel);
    });
  }

  /* ---------- Collapsible ---------- */
  document.addEventListener("click", (ev) => {
    const head = ev.target.closest(".collapsible-head");
    if (!head) return;
    const id = head.getAttribute("data-toggle");
    if (id) $(id).classList.toggle("collapsed");
  });

  /* ---------- Render ---------- */
  function applyPhase(el, phaseKey) {
    el.className = "phase " + phaseKey;
    el.innerHTML =
      '<span class="dot"></span><span class="label">' +
      escapeHtml(PHASE_LABELS[phaseKey] || phaseKey) + "</span>";
  }
  function applyButtonState(phaseKey) {
    const startBtn = $("start-btn");
    const pauseBtn = $("pause-btn");
    if (startBtn && !startBtn.classList.contains("loading")) {
      startBtn.disabled = (phaseKey === "running" || phaseKey === "waiting_login");
    }
    if (pauseBtn && !pauseBtn.classList.contains("loading")) {
      pauseBtn.disabled = !(phaseKey === "running" || phaseKey === "waiting_login");
    }
  }

  async function refresh() {
    $("now").textContent = new Date().toLocaleString();
    let data;
    try {
      const resp = await fetch("/status", { cache: "no-store" });
      if (resp.status === 401) { window.location.href = "/login"; return; }
      data = await resp.json();
    } catch (e) { return; }

    const r  = data.reading || {};
    const lr = r.last_run   || {};
    const phaseKey = r.phase || "idle";
    currentPhase = phaseKey;
    applyPhase($("phase"),        phaseKey);
    applyPhase($("header-phase"), phaseKey);

    const etaEl = $("eta");
    if (phaseKey === "running" && r.eta_seconds != null) {
      etaEl.textContent = fmtSeconds(r.eta_seconds);
      etaEl.classList.remove("dim");
    } else {
      etaEl.classList.add("dim");
      etaEl.textContent = phaseKey === "waiting_login" ? "扫码中" : "—";
    }

    const c = r.cookies || {};
    const ls = $("login-status");
    if (c.status === "valid") {
      ls.textContent = "✅ 已登录 · " + (c.age_days != null ? c.age_days.toFixed(1) + " 天前" : "近期");
    } else if (c.status === "expired") {
      ls.textContent = "⚠️ 登录已过期" + (c.age_days != null ? " (" + c.age_days.toFixed(0) + " 天)" : "");
    } else {
      ls.textContent = "❌ 未登录";
    }

    $("trigger").textContent     = TRIGGER_LABELS[lr.trigger] || lr.trigger || "—";
    $("started_at").textContent  = fmtTime(lr.started_at);
    $("ended_at").textContent    = fmtTime(lr.ended_at);
    const last = $("last_status");
    if (!lr.status) {
      last.textContent = "—"; last.className = "v";
    } else if (lr.status === "completed") {
      last.textContent = "✓ 成功"; last.className = "v ok";
    } else if (lr.status === "failed") {
      last.textContent = "✗ 失败 (" + (lr.exit_code != null ? lr.exit_code : "?") + ")";
      last.className   = "v err";
    } else if (lr.status === "stopped") {
      last.textContent = "⏸ 已暂停"; last.className = "v warn";
    } else if (lr.status === "running") {
      last.textContent = "⏳ 运行中"; last.className = "v warn";
    } else {
      last.textContent = lr.status; last.className = "v";
    }

    /* QR card */
    const qr      = r.login_qr || {};
    const qrCard  = $("qr-card");
    const qrBlock = $("qr-block");
    if (c.status === "valid") {
      qrCard.style.display = "";
      qrBlock.innerHTML = '<div class="qr-empty"><div class="big">✅</div><div>已登录，无需扫码</div><div style="margin-top:8px;color:var(--muted-2);font-size:11px">点击「重启」可刷新二维码重新登录</div></div>';
    } else {
      qrCard.style.display = "";
      if (qr.present) {
        qrBlock.innerHTML =
          '<div class="qr-wrap">' +
            '<div class="qr-img"><img src="/login.png?t=' + Date.now() + '" alt="login QR"></div>' +
            '<div class="qr-title">微信扫码登录</div>' +
            '<div class="qr-msg">二维码 5 分钟内有效，过期请点击「重启」</div>' +
            '<div class="qr-msg" style="margin-top:6px;">最后更新：' + escapeHtml(fmtAge(qr.age_seconds)) + "</div>" +
          "</div>";
      } else {
        const big = c.status === "valid" ? "✅" : "⏳";
        const msg = c.status === "valid" ? "已登录，无需扫码" : "尚未生成二维码，请等待或点击「开始」";
        qrBlock.innerHTML =
          '<div class="qr-empty"><div class="big">' + big + "</div><div>" + msg + "</div></div>";
      }
    }

    /* Env */
    const envEl = $("env");
    envEl.innerHTML = "";
    Object.entries(data.env || {}).forEach(([k, v]) => {
      const cls = !v ? "v muted" : (v === "***set***" ? "v set" : "v");
      const text = v || "—";
      const row  = document.createElement("div");
      row.className = "env-row";
      row.innerHTML = '<span class="k">' + escapeHtml(k) + '</span>' +
                      '<span class="' + cls + '">' + escapeHtml(text) + "</span>";
      envEl.appendChild(row);
    });

    /* Notif summary */
    const notifs = (data.notifications || {}).channels || [];
    const okN = notifs.filter((n) => n.configured).length;
    $("notif-summary").textContent = okN > 0 ? okN + " 个已启用" : "未配置";

    applyButtonState(phaseKey);
  }

  /* ---------- Logs ---------- */
  async function loadLogs() {
    const wrap = $("logs");
    try {
      const resp = await fetch("/logs/clean?n=30", { cache: "no-store" });
      if (resp.status === 401) { window.location.href = "/login"; return; }
      const data = await resp.json();
      const entries = data.entries || [];
      if (entries.length === 0) {
        wrap.innerHTML = '<div class="log-empty">暂无日志</div>';
        return;
      }
      wrap.innerHTML = "";
      entries.forEach((e) => {
        const div = document.createElement("div");
        div.className   = "log-entry " + (e.level || "info");
        div.textContent = e.text;
        wrap.appendChild(div);
      });
      wrap.scrollTop = wrap.scrollHeight;
    } catch (e) {
      wrap.innerHTML = '<div class="log-empty">日志加载失败</div>';
    }
  }

  /* ---------- Notification config ---------- */
  function renderChannelStatus(channelKey, requiredFields) {
    const cfg = lastNotifConfig[channelKey] || {};
    const filled = (k) => { const v = cfg[k]; return v && v !== ""; };
    const allSet  = requiredFields.every(filled);
    const anySet  = requiredFields.some(filled);
    const el = $(channelKey + "-status");
    if (allSet)      { el.textContent = "已配置";   el.className = "notif-status ok"; }
    else if (anySet) { el.textContent = "部分配置"; el.className = "notif-status no"; }
    else             { el.textContent = "未配置";   el.className = "notif-status no"; }
  }
  async function loadNotif() {
    try {
      const resp = await fetch("/api/notification", { cache: "no-store" });
      if (!resp.ok) return;
      const data = await resp.json();
      lastNotifConfig = data.config || {};
      Object.entries(lastNotifConfig).forEach(([ch, fields]) => {
        Object.entries(fields).forEach(([k, v]) => {
          const input = $(k);
          if (!input) return;
          if (v === "***set***") {
            input.value = "";
            input.placeholder = "已配置（留空则保留）";
          } else {
            input.value = v || "";
          }
        });
      });
      renderChannelStatus("bark",     ["bark_key"]);
      renderChannelStatus("pushplus", ["pushplus_token"]);
      renderChannelStatus("email",    ["email_user", "email_pass", "email_to", "email_smtp"]);
      renderChannelStatus("webhook",  ["webhook_url"]);
    } catch (e) { /* ignore */ }
  }
  function collectChannel(keys, channelKey) {
    const out  = {};
    const orig = lastNotifConfig[channelKey] || {};
    keys.forEach((k) => {
      const el = $(k);
      const val = (el ? el.value : "").trim();
      if (!val && SECRET_KEYS.has(k) && orig[k] === "***set***") out[k] = "***set***";
      else out[k] = val;
    });
    return out;
  }
  async function saveNotif() {
    const btn = $("notif-save");
    btn.classList.add("loading"); btn.disabled = true;
    const body = {
      bark:     collectChannel(["bark_key", "bark_url"], "bark"),
      pushplus: collectChannel(["pushplus_token"], "pushplus"),
      email:    collectChannel(["email_user", "email_pass", "email_to", "email_from", "email_smtp", "email_port"], "email"),
      webhook:  collectChannel(["webhook_url"], "webhook"),
    };
    try {
      const resp = await fetch("/api/notification", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (resp.ok) { toast("通知配置已保存", "success"); await loadNotif(); }
      else         { toast("保存失败：" + resp.status, "error"); }
    } catch (e) { toast("请求失败", "error"); }
    finally     { btn.classList.remove("loading"); btn.disabled = false; }
  }

  /* ---------- Action helper ---------- */
  async function postJSON(url) {
    try {
      const resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
      });
      let body = null;
      try { body = await resp.json(); } catch (e) {}
      return { ok: resp.ok, status: resp.status, body: body || {} };
    } catch (e) {
      return { ok: false, status: 0, body: { reason: String(e) } };
    }
  }

  function runAction(btn, loadingLabel, normalLabel, fn, refreshDelay) {
    if (btn.disabled) return;
    const labelEl = btn.querySelector(".label");
    btn.classList.add("loading"); btn.disabled = true;
    labelEl.textContent = loadingLabel;
    Promise.resolve(fn()).catch(() => toast("请求失败", "error")).finally(() => {
      labelEl.textContent = normalLabel;
      btn.classList.remove("loading");
      // immediate refresh to reflect new phase
      refresh(); loadLogs();
      // re-enable based on phase after the action settles
      setTimeout(() => {
        btn.disabled = false;
        applyButtonState(currentPhase);
        refresh(); loadLogs();
      }, refreshDelay || 1500);
    });
  }

  /* ---------- Buttons ---------- */
  $("theme-toggle").addEventListener("click", cycleTheme);

  $("start-btn").addEventListener("click", () => {
    runAction($("start-btn"), "▶ 启动中…", "▶ 开始", async () => {
      const r = await postJSON("/start");
      if (r.ok && r.body.ok) {
        toast("已开始阅读 · PID " + r.body.spawned_pid, "success");
      } else {
        const reason = r.body.reason === "already running" ? "已经在阅读中" : (r.body.reason || ("HTTP " + r.status));
        toast("触发失败：" + reason, "error");
      }
    }, 1500);
  });

  $("pause-btn").addEventListener("click", async () => {
    const ok = await confirmModal("停止本次阅读", "将终止当前阅读进程，cookies 不会丢失。下次调度时间会自动继续。", "停止", false);
    if (!ok) return;
    runAction($("pause-btn"), "⏹ 停止中…", "⏹ 停止", async () => {
      const r = await postJSON("/stop");
      if (r.ok && r.body.ok) {
        toast("已停止 · 终止 PID " + r.body.killed_pid, "warn");
      } else {
        const reason = r.body.reason === "not running" ? "当前没有运行的进程" : (r.body.reason || ("HTTP " + r.status));
        toast("停止失败：" + reason, "error");
      }
    }, 2000);
  });

  $("restart-btn").addEventListener("click", async () => {
    const ok = await confirmModal("重启阅读", "将终止当前阅读，清除登录状态并重新生成二维码。继续？", "重启", true);
    if (!ok) return;
    runAction($("restart-btn"), "🔄 重启中…", "🔄 重启", async () => {
      const r = await postJSON("/restart");
      if (r.ok && r.body.ok) toast("已重启 · 新 PID " + (r.body.spawned_pid || "?") + "，等待二维码生成…", "success");
      else toast("重启失败：" + (r.body.reason || ("HTTP " + r.status)), "error");
    }, 8000);
  });

  $("notif-save").addEventListener("click", saveNotif);

  /* ---------- Init ---------- */
  refresh(); loadLogs(); loadNotif();
  setInterval(refresh,  30000);
  setInterval(loadLogs, 30000);
})();
</script>
{% endraw %}
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
