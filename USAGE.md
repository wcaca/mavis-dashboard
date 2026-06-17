# Mavis Dashboard 使用文档

> 通用 Agent 团队（General / Coder / Verifier）的实时监控 + 告警面板

## 📦 这是什么

一个 Python 单文件 HTTP server + 静态 HTML 前端，提供：

- **实时事件流** (SSE) — Agent 推送消息，前端秒级接收
- **状态快照** — 拉取 `state/*.json` 文件，展示系统状态
- **告警广播** — Agent 调用 `/publish` 推送到所有连接的浏览器
- **历史回放** — 最近 50 条推送记录
- **鉴权登录** — SHA-256 密码 hash，会话 cookie (7 天)

部署在 `163.7.3.92:8443` (HTTP)，通过 nginx 反代做 HTTPS。

---

## 🚀 快速访问

```
URL:      http://dashboard.noteverse.space:8443/
用户名:    admin
密码:     <见 wcaca/secrets 仓库的 README 或问 Mavis>
```

> ⚠️ **首次部署后必须改密码！** 默认密码 hash 在仓库里是公开的，攻击者可以爆破。

---

## 🔐 鉴权机制

- 算法: `sha256(PASSWORD_SALT + password)`，其中 `PASSWORD_SALT = "mavis-dashboard-v1"`
- 密码 hash 不存数据库，每次登录时实时计算
- 会话: HttpOnly cookie `mavis_session`，TTL 7 天
- 登出: `POST /logout`

### 改密码

```bash
# 在沙箱生成新 hash
NEW_PWD="your-new-password"
HASH=$(python3 -c "import hashlib; print(hashlib.sha256(('mavis-dashboard-v1' + '$NEW_PWD').encode()).hexdigest())")
echo "新 hash: $HASH"

# 更新 systemd 环境变量
ssh root@163.7.3.92 "sed -i 's/DASHBOARD_PASSWORD_HASH=.*/DASHBOARD_PASSWORD_HASH=$HASH/' /etc/systemd/system/mavis-dashboard.service"

# 重启服务
ssh root@163.7.3.92 "systemctl daemon-reload && systemctl restart mavis-dashboard"
```

或者直接用仓库里的脚本：

```bash
bash scripts/reset-password.sh "new-password"
```

---

## 📡 API 一览

### 公开接口（无需登录）

| Method | Path | 说明 |
|--------|------|------|
| GET    | `/health` | 服务健康 (subscribers 数 + history 数) |
| GET    | `/login` 或 `/login.html` | 登录页 |

### 登录 / 登出

| Method | Path | Body | 说明 |
|--------|------|------|------|
| POST   | `/login` | `{username, password}` | 验证后 set cookie |
| POST   | `/logout` | — | 清 cookie |

**登录示例**:
```bash
curl -c cookies.txt -X POST http://localhost:8443/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"YOUR_PASSWORD"}'
# 返回 {"status":"ok","user":"admin"} + Set-Cookie
```

### 推送 / 接收事件（需要登录）

| Method | Path | 说明 |
|--------|------|------|
| GET    | `/events` | SSE 长连接，订阅实时事件 |
| POST   | `/publish` | 推送事件到所有 SSE 客户端 |
| GET    | `/history` | 获取最近 50 条推送 |

**推送示例**:
```bash
curl -b cookies.txt -X POST http://localhost:8443/publish \
  -H "Content-Type: application/json" \
  -d '{
    "type": "alert",
    "data": {
      "level": "critical",
      "message": "redis 容器挂了",
      "host": "163.7.3.92"
    }
  }'
```

**响应**:
```json
{
  "success": true,
  "subscribers": 3,
  "timestamp": "2026-06-17T08:30:00Z"
}
```

**支持的 event type**:
- `alert` — 红色告警（critical / warning / info 三级）
- `info` — 普通信息
- `success` — 成功事件
- `health-check` — 健康检查结果（含 status: ok/warn/fail）
- `state` — 状态变化
- 自定义 — 前端会原样显示

### 状态文件（需要登录）

| Method | Path | 说明 |
|--------|------|------|
| GET    | `/state/*` | 读取 `state/<path>` 文件 |
| GET    | `/status.json` | `mavis-status` 的 JSON 缓存 |

`/state/` 路径映射到 `DASHBOARD_DIR/state/` 目录（默认 `/opt/mavis-dashboard/state/`）。
Agent 通过写文件 + dashboard 读取的方式暴露系统状态。

---

## 🤖 Agent 调用

### 方式 1: 用脚本（推荐）

```bash
# 仓库自带 scripts/dashboard-push.sh
DASHBOARD_URL="http://localhost:8443" bash scripts/dashboard-push.sh \
  alert "磁盘使用 92%" --level critical --disk-pct 92

DASHBOARD_URL="http://localhost:8443" bash scripts/dashboard-push.sh \
  health-check --status ok

DASHBOARD_URL="http://localhost:8443" bash scripts/dashboard-push.sh \
  success "部署完成"
```

### 方式 2: 直接 curl

```bash
# 1. 登录拿 cookie
COOKIE=$(curl -s -c - -X POST http://localhost:8443/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"YOUR_PASSWORD"}' \
  | grep mavis_session | awk '{print $7}')

# 2. push 事件
curl -X POST http://localhost:8443/publish \
  -H "Content-Type: application/json" \
  -H "Cookie: mavis_session=$COOKIE" \
  -d '{"type":"alert","data":{"level":"critical","message":"..."}}'
```

### 方式 3: 在代码里调用

**Python**:
```python
import requests
s = requests.Session()
s.post('http://localhost:8443/login',
       json={'username': 'admin', 'password': 'YOUR_PASSWORD'})
s.post('http://localhost:8443/publish',
       json={'type': 'alert', 'data': {'level': 'warning', 'message': 'X'}})
```

**Node.js**:
```javascript
const r = await fetch('http://localhost:8443/login', {
  method: 'POST',
  headers: {'Content-Type':'application/json'},
  body: JSON.stringify({username:'admin', password:'YOUR_PASSWORD'})
});
const setCookie = r.headers.get('set-cookie');
await fetch('http://localhost:8443/publish', {
  method: 'POST',
  headers: {'Content-Type':'application/json', 'Cookie': setCookie},
  body: JSON.stringify({type:'info', data:{message:'hello'}})
});
```

---

## 🚢 部署

### 从 GitHub 拉取并启动

```bash
# 沙箱里
cd /workspace
git clone https://github.com/wcaca/mavis-dashboard.git
cd mavis-dashboard

# 推到 server
rsync -av -e "ssh -i /root/.ssh/id_rsa" \
  --exclude='.git' --exclude='__pycache__' \
  ./ root@163.7.3.92:/opt/mavis-dashboard/

# server 上启动
ssh root@163.7.3.92 "systemctl daemon-reload && systemctl restart mavis-dashboard"
```

### systemd 单元（示例）

`/etc/systemd/system/mavis-dashboard.service`:

```ini
[Unit]
Description=Mavis Agent Dashboard
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/mavis-dashboard
Environment="PORT=8443"
Environment="DASHBOARD_USER=admin"
Environment="DASHBOARD_PASSWORD_HASH=<sha256-hash>"
# 可选 HTTPS:
# Environment="ENABLE_SSL=1"
# Environment="SSL_CERT=/etc/nginx/ssl/dashboard.crt"
# Environment="SSL_KEY=/etc/nginx/ssl/dashboard.key"
ExecStart=/usr/bin/python3 /opt/mavis-dashboard/dashboard/server.py
Restart=always
RestartSec=5
StandardOutput=append:/opt/mavis-dashboard/logs/dashboard.log
StandardError=append:/opt/mavis-dashboard/logs/dashboard.log

[Install]
WantedBy=multi-user.target
```

启用 + 启动:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mavis-dashboard
sudo systemctl status mavis-dashboard
```

### nginx 反代（HTTPS 推荐）

`/etc/nginx/sites-enabled/dashboard`:

```nginx
server {
  listen 443 ssl;
  server_name dashboard.noteverse.space;

  ssl_certificate     /etc/letsencrypt/live/dashboard.noteverse.space/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/dashboard.noteverse.space/privkey.pem;

  # SSE 需要禁用缓冲
  proxy_buffering off;
  proxy_cache off;
  proxy_read_timeout 86400s;

  location / {
    proxy_pass http://127.0.0.1:8443;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
  }
}
```

---

## 🛠 维护

### 查日志

```bash
ssh root@163.7.3.92 "tail -f /opt/mavis-dashboard/logs/dashboard.log"
```

### 重启服务

```bash
ssh root@163.7.3.92 "systemctl restart mavis-dashboard"
```

### 验证运行状态

```bash
# 健康检查（无需登录）
curl -s http://localhost:8443/health
# {"status":"ok","subscribers":0,"history_size":12,"timestamp":"..."}

# 推一条测试消息
DASHBOARD_URL=http://localhost:8443 bash scripts/dashboard-push.sh \
  info "Mavis 测试消息 $(date)"
```

### 同步 GitHub → Server

```bash
# 在沙箱
cd /workspace/mavis-dashboard
git pull  # 拉新
rsync -av -e "ssh -i /root/.ssh/id_rsa" \
  --exclude='.git' --exclude='__pycache__' \
  ./ root@163.7.3.92:/opt/mavis-dashboard/
ssh root@163.7.3.92 "systemctl restart mavis-dashboard"
```

---

## 🔌 集成 Mavis Agent 记忆系统

把 `agent-memory` 仓库里的脚本桥接过来：

```bash
# 沙箱里
cd /workspace/agent-memory
ls scripts/dashboard-*.sh scripts/mavis-status.sh
# dashboard-push.sh, dashboard-server.sh, mavis-status.sh
```

`mavis-status.sh` 是 Mavis Agent 全景状态检查脚本，可以：

```bash
# 加 --push 自动推送到 dashboard
mavis-status.sh --push
```

详见 `agent-memory` 仓库 README 和 `scripts/mavis-status.sh` 源码。

---

## 🐛 故障排查

| 问题 | 排查 |
|------|------|
| 登录返回 401 | 检查 `DASHBOARD_PASSWORD_HASH` 是否正确，hash 算法: `sha256("mavis-dashboard-v1" + password)` |
| SSE 收不到事件 | 检查 nginx `proxy_buffering off`、浏览器 console 有无 EventSource 错误 |
| `/publish` 401 | Cookie 过期 (7 天)，重新 `/login` |
| 服务挂了 | `systemctl status mavis-dashboard` + `journalctl -u mavis-dashboard -n 50` |
| `state/*` 404 | 检查 `DASHBOARD_DIR/state/` 目录存在，文件权限可读 |

---

## 📂 目录结构

```
mavis-dashboard/
├── README.md                 # 本文件
├── USAGE.md                  # 本文档
├── dashboard/
│   ├── server.py             # Python HTTP server
│   ├── index.html            # 主面板 UI
│   ├── login.html            # 登录页
│   ├── manifest.json         # PWA manifest
│   └── sw.js                 # Service worker
├── scripts/
│   ├── dashboard-push.sh     # 推送事件（agent 调用）
│   ├── dashboard-server.sh   # 启动脚本（沙箱用）
│   ├── reset-password.sh     # 密码重置
│   └── mavis-status.sh       # 系统全景状态检查
└── state/                    # 状态文件目录（运行时生成）
```

---

## 🔗 相关仓库

- [`wcaca/agent-memory`](https://github.com/wcaca/agent-memory) — Mavis Agent 跨会话记忆 + 系统脚本
- [`wcaca/beauty-crm`](https://github.com/wcaca/beauty-crm) — 主业务项目
- [`wcaca/atm-redirector`](https://github.com/wcaca/atm-redirector) — ATM 跳转器
