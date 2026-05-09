<p align="center">
    <a href="https://linux.do" alt="LINUX DO">
        <img
            src="https://img.shields.io/badge/LINUX-DO-FFB003.svg?logo=data:image/svg%2bxml;base64,DQo8c3ZnIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyIgd2lkdGg9IjEwMCIgaGVpZ2h0PSIxMDAiPjxwYXRoIGQ9Ik00Ni44Mi0uMDU1aDYuMjVxMjMuOTY5IDIuMDYyIDM4IDIxLjQyNmM1LjI1OCA3LjY3NiA4LjIxNSAxNi4xNTYgOC44NzUgMjUuNDV2Ni4yNXEtMi4wNjQgMjMuOTY4LTIxLjQzIDM4LTExLjUxMiA3Ljg4NS0yNS40NDUgOC44NzRoLTYuMjVxLTIzLjk3LTIuMDY0LTM4LjAwNC0yMS40M1EuOTcxIDY3LjA1Ni0uMDU0IDUzLjE4di02LjQ3M0MxLjM2MiAzMC43ODEgOC41MDMgMTguMTQ4IDIxLjM3IDguODE3IDI5LjA0NyAzLjU2MiAzNy41MjcuNjA0IDQ2LjgyMS0uMDU2IiBzdHlsZT0ic3Ryb2tlOm5vbmU7ZmlsbC1ydWxlOmV2ZW5vZGQ7ZmlsbDojZWNlY2VjO2ZpbGwtb3BhY2l0eToxIi8+PHBhdGggZD0iTTQ3LjI2NiAyLjk1N3EyMi41My0uNjUgMzcuNzc3IDE1LjczOGE0OS43IDQ5LjcgMCAwIDEgNi44NjcgMTAuMTU3cS00MS45NjQuMjIyLTgzLjkzIDAgOS43NS0xOC42MTYgMzAuMDI0LTI0LjM4N2E2MSA2MSAwIDAgMSA5LjI2Mi0xLjUwOCIgc3R5bGU9InN0cm9rZTpub25lO2ZpbGwtcnVsZTpldmVub2RkO2ZpbGw6IzE5MTkxOTtmaWxsLW9wYWNpdHk6MSIvPjxwYXRoIGQ9Ik03Ljk4IDcwLjkyNmMyNy45NzctLjAzNSA1NS45NTQgMCA4My45My4xMTNRODMuNDI2IDg3LjQ3MyA2Ni4xMyA5NC4wODZxLTE4LjgxIDYuNTQ0LTM2LjgzMi0xLjg5OC0xNC4yMDMtNy4wOS0yMS4zMTctMjEuMjYyIiBzdHlsZT0ic3Ryb2tlOm5vbmU7ZmlsbC1ydWxlOmV2ZW5vZGQ7ZmlsbDojZjlhZjAwO2ZpbGwtb3BhY2l0eToxIi8+PC9zdmc+" /></a>
</p>

<h1 align="center">weread-challenge-hf</h1>


将 [weread-challenge-selenium](https://github.com/jqknono/weread-challenge-selenium) 改造为适配 HuggingFace Spaces 的单容器版本。

## 与原项目的区别

| | 原项目 | 本项目 |
|---|---|---|
| 架构 | 双容器（app + selenium-standalone） | 单容器（内置 Chromium + Xvfb） |
| 部署 | VPS / docker-compose | HuggingFace Spaces (Docker SDK) |
| 管理 | 无 Web UI | Flask 暗色面板 + 密码保护 |
| 定时 | 外部 crontab | 内置 12 小时调度器 |
| 防休眠 | 无 | 自 ping /healthz |
| 持久化 | 本地目录 | HF Storage Bucket (/data) |

## 功能

- 微信读书自动刷时长（默认 68 分钟/次）
- 每 12 小时自动运行一次
- Web 管理面板：状态监控、二维码查看、手动触发、重启阅读
- 密码保护（默认 `linux123`，可通过 `WEB_PASSWORD` 环境变量修改）
- 登录二维码实时显示 + 手动刷新
- 数据持久化到 HF Storage Bucket

## 部署到 HuggingFace Spaces

1. 创建一个新的 Docker Space
2. 上传本项目所有文件
3. 在 Space Settings → Variables and secrets 中设置：
   - `WEB_PASSWORD`（可选，默认 `114114aa`）
   - `SECRET_KEY`（可选，Flask session 加密）
4. 创建 Storage Bucket 并挂载到 `/data`（读写模式）

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WEREAD_DURATION` | 68 | 每次阅读时长（分钟） |
| `WEREAD_SPEED` | slow | 阅读速度 |
| `WEREAD_SELECTION` | 2 | 书籍选择方式 |
| `READING_INTERVAL_HOURS` | 12 | 自动阅读间隔（小时） |
| `SELF_PING_MINUTES` | 5 | 自 ping 间隔（分钟） |
| `WEB_PASSWORD` | 114114aa | Web 面板登录密码 |

## Web 端点

| 路径 | 方法 | 认证 | 说明 |
|---|---|---|---|
| `/` | GET | ✅ | 管理面板 |
| `/login` | GET/POST | ❌ | 登录页 |
| `/logout` | GET | ✅ | 退出登录 |
| `/status` | GET | ✅ | JSON 状态 |
| `/login.png` | GET | ✅ | 二维码图片 |
| `/start` | POST | ✅ | 触发阅读 |
| `/restart` | POST | ✅ | 重启阅读（刷新二维码） |
| `/logs` | GET | ✅ | 查看日志 |
| `/healthz` | GET | ❌ | 健康检查 |

## 本地运行

```bash
docker build -t weread-challenge-hf .
docker run -d \
  -p 7860:7860 \
  -v weread-data:/data \
  -e WEB_PASSWORD=your_password \
  weread-challenge-hf
```

访问 `http://localhost:7860`，输入密码登录后扫码。
