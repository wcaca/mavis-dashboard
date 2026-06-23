#!/usr/bin/env python3
"""
dashboard-server.py - Mavis Agent Dashboard HTTP server

支持：
  - GET  /              → index.html
  - GET  /login         → login.html
  - POST /login         → 登录 (支持 remember, 失败次数限制)
  - POST /logout        → 登出
  - GET  /api/me        → 当前用户信息 (登录后)
  - POST /api/refresh   → 主动续期 cookie
  - GET  /api/state/*   → state 文件
  - GET  /api/events    → SSE 实时推送
  - POST /api/publish   → 推送事件（agent 调用）
  - GET  /api/history   → 推送历史
  - GET  /api/projects  → GitHub 项目进展 dashboard（v25cf 新增）
  - GET  /api/repos/<name>  → 单个 repo 详情（v25cg 新增）
  - GET  /health        → 服务健康（公开）
"""
import http.server
import socketserver
import json
import os
import sys
import time
import threading
import queue
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from http.cookies import SimpleCookie

# GitHub API 客户端（项目进展 dashboard）
try:
    from github_api import get_client as get_github_client
    GITHUB_API_AVAILABLE = True
except ImportError as e:
    GITHUB_API_AVAILABLE = False
    _GH_IMPORT_ERR = str(e)

PORT = int(os.environ.get('PORT', 8765))

# SSL config
SSL_CERT = os.environ.get("SSL_CERT", "/etc/nginx/ssl/dashboard.crt")
SSL_KEY = os.environ.get("SSL_KEY", "/etc/nginx/ssl/dashboard.key")
DASHBOARD_DIR = os.environ.get("DASHBOARD_DIR", "/opt/mavis-dashboard")
ROOT = DASHBOARD_DIR
DASHBOARD_SUBDIR = os.path.join(DASHBOARD_DIR, "dashboard")

# Auth config
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD_HASH = os.environ.get("DASHBOARD_PASSWORD_HASH", "")
PASSWORD_SALT = "mavis-dashboard-v1"

# Session config
SESSION_COOKIE = "mavis_session"
SESSION_TTL_DEFAULT = 7 * 24 * 3600         # 7 天
SESSION_TTL_REMEMBER = 30 * 24 * 3600       # 30 天
SESSION_REFRESH_THRESHOLD = 0.5             # 剩一半时间时自动续期
SESSION_FILE = os.path.join(DASHBOARD_DIR, "state", "sessions.json")

# Rate limit config
LOGIN_MAX_FAILS = int(os.environ.get("LOGIN_MAX_FAILS", "5"))   # 5 次
LOGIN_WINDOW = int(os.environ.get("LOGIN_WINDOW", "900"))        # 15 分钟
RATE_LIMIT_FILE = os.path.join(DASHBOARD_DIR, "state", "login_attempts.json")


def hash_password(password):
    return hashlib.sha256((PASSWORD_SALT + password).encode()).hexdigest()


# ============ Session 管理（持久化） ============

sessions = {}              # token -> {created, last_seen, user, remember}
sessions_lock = threading.Lock()


def _load_sessions():
    """从文件加载 sessions（启动时调用）"""
    global sessions
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, 'r') as f:
                data = json.load(f)
                now = time.time()
                # 过滤已过期
                loaded = {}
                for token, sess in data.items():
                    ttl = sess.get('ttl', SESSION_TTL_DEFAULT)
                    if now - sess['last_seen'] < ttl:
                        loaded[token] = sess
                with sessions_lock:
                    sessions.update(loaded)
                log(f"加载 {len(loaded)} 个有效 session（清理 {len(data) - len(loaded)} 个过期）")
        except Exception as e:
            log(f"⚠️  加载 sessions 失败: {e}")


def _save_sessions():
    """把 sessions 持久化到文件（异步）"""
    def _do():
        try:
            os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
            # 拷一份快照避免在主线程修改时遍历 dict 死循环
            with sessions_lock:
                snapshot = dict(sessions)
            with open(SESSION_FILE, 'w') as f:
                json.dump(snapshot, f, indent=2)
        except Exception as e:
            log(f"⚠️  保存 sessions 失败: {e}")
    threading.Thread(target=_do, daemon=True).start()


def create_session(user, remember=False):
    token = secrets.token_urlsafe(32)
    ttl = SESSION_TTL_REMEMBER if remember else SESSION_TTL_DEFAULT
    now = time.time()
    with sessions_lock:
        sessions[token] = {
            'created': now,
            'last_seen': now,
            'user': user,
            'remember': remember,
            'ttl': ttl
        }
    _save_sessions()
    return token, ttl


def get_session(token):
    """获取 session，如果过期或不存在返回 None"""
    if not token:
        return None
    with sessions_lock:
        sess = sessions.get(token)
        if not sess:
            return None
        now = time.time()
        if now - sess['last_seen'] > sess['ttl']:
            del sessions[token]
            _save_sessions()
            return None
        return sess


def touch_session(token, sess):
    """刷新 session 的 last_seen，必要时续期 token

    返回 (new_token_or_None, sess)
      - new_token_or_None: 续期后新 token（未续期时为 None）
      - sess: 更新后的 session（last_seen 刷新）
    """
    now = time.time()
    elapsed = now - sess['last_seen']
    sess['last_seen'] = now
    new_token = None
    if elapsed > sess['ttl'] * SESSION_REFRESH_THRESHOLD:
        # 续期：重新生成 token
        new_token = secrets.token_urlsafe(32)
        with sessions_lock:
            del sessions[token]
            sessions[new_token] = sess
        _save_sessions()
    else:
        # 只刷新 last_seen
        with sessions_lock:
            sessions[token] = sess
    return new_token, sess


def destroy_session(token):
    with sessions_lock:
        if token in sessions:
            del sessions[token]
    _save_sessions()


# ============ 登录失败次数限制 ============

login_attempts = {}        # ip -> [(timestamp, success)]
attempts_lock = threading.Lock()


def _load_attempts():
    global login_attempts
    if os.path.exists(RATE_LIMIT_FILE):
        try:
            with open(RATE_LIMIT_FILE, 'r') as f:
                login_attempts = json.load(f)
        except Exception:
            login_attempts = {}


def _save_attempts():
    def _do():
        try:
            os.makedirs(os.path.dirname(RATE_LIMIT_FILE), exist_ok=True)
            with open(RATE_LIMIT_FILE, 'w') as f:
                json.dump(login_attempts, f, indent=2)
        except Exception:
            pass
    threading.Thread(target=_do, daemon=True).start()


def get_client_ip(handler):
    """拿真实 IP（支持 X-Forwarded-For）"""
    xff = handler.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return handler.client_address[0] if handler.client_address else 'unknown'


def is_rate_limited(ip):
    """检查 IP 是否被限速（5 次/15 分钟）"""
    with attempts_lock:
        attempts = login_attempts.get(ip, [])
        now = time.time()
        # 清理过期
        attempts = [(t, s) for t, s in attempts if now - t < LOGIN_WINDOW]
        login_attempts[ip] = attempts
        # 数失败
        fails = sum(1 for t, s in attempts if not s)
        return fails >= LOGIN_MAX_FAILS


def record_attempt(ip, success):
    with attempts_lock:
        if ip not in login_attempts:
            login_attempts[ip] = []
        login_attempts[ip].append((time.time(), success))
        # 清理
        now = time.time()
        login_attempts[ip] = [(t, s) for t, s in login_attempts[ip] if now - t < LOGIN_WINDOW]
    _save_attempts()


# ============ 推送历史 / 订阅者 ============

push_history = []
push_history_lock = threading.Lock()
subscribers = []
subscribers_lock = threading.Lock()


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def broadcast_event(event_type, data):
    msg = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    dead = []
    with subscribers_lock:
        for q in subscribers:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            subscribers.remove(q)
    if dead:
        log(f"清理 {len(dead)} 个断开连接")


def add_to_history(event_type, data):
    with push_history_lock:
        push_history.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "data": data
        })
        if len(push_history) > 50:
            push_history.pop(0)


def check_auth(handler):
    """返回 (sess, new_token) — new_token 不为 None 时表示需要重发 cookie"""
    cookie_str = handler.headers.get('Cookie', '')
    cookie = SimpleCookie(cookie_str)
    token_cookie = cookie.get(SESSION_COOKIE)
    if not token_cookie:
        return None, None
    token = token_cookie.value
    sess = get_session(token)
    if not sess:
        return None, None
    new_token, sess = touch_session(token, sess)
    return sess, new_token


def set_session_cookie(handler, token, ttl):
    """返回 Set-Cookie 字符串。调用方应通过 send_json(extra_headers=...) 传入。

    如果不传 extra_headers，Set-Cookie 也会被存到 handler._pending_cookies
    让 send_json 自动加上去（保持向后兼容）。
    """
    cookie = f'{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={ttl}'
    if not hasattr(handler, '_pending_cookies'):
        handler._pending_cookies = []
    handler._pending_cookies.append(cookie)
    return cookie


def clear_session_cookie(handler):
    """返回清空 session 的 Set-Cookie 字符串。"""
    cookie = f'{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'
    if not hasattr(handler, '_pending_cookies'):
        handler._pending_cookies = []
    handler._pending_cookies.append(cookie)
    return cookie


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    # Python stdlib 配合 cloudflared tunnel 的最佳实践：
    # - HTTP/1.1（CF 期望）
    # - 强制 Connection: close（避免 stdlib 不自动加 Content-Length 导致 CF 解析失败）
    protocol_version = 'HTTP/1.1'
    def log_message(self, format, *args):
        pass

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        # 防止搜索引擎索引（个人面板，2026-06-19 加）
        self.send_header('X-Robots-Tag', 'noindex, nofollow')
        # 强制 Connection: close — 避免 HTTP/1.1 keep-alive 模式下
        # Python stdlib 不自动加 Content-Length/chunked encoding，
        # cloudflared 解析会报 'malformed MIME header: missing colon'
        if not self.headers.get('Connection'):
            self.send_header('Connection', 'close')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    # ---------- GET ----------

    def do_GET(self):
        # 解析 path（去掉 query string，所有路由都用 _path 比较）
        from urllib.parse import urlparse
        _path = urlparse(self.path).path

        # 公开：/health
        if _path == '/health':
            self.send_json({
                "status": "ok",
                "subscribers": len(subscribers),
                "history_size": len(push_history),
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            return

        # 公开：登录页（_path 已在 do_GET 开头定义）
        if _path == '/login' or _path == '/login.html':
            self.serve_file(os.path.join(DASHBOARD_DIR, 'dashboard', 'login.html'), 'text/html; charset=utf-8')
            return

        # 公开：dashboard 静态资源（main.js, sw.js, manifest.json, icons...）
        if self.path.startswith('/static/') or self.path in ('/manifest.json', '/sw.js', '/favicon.ico'):
            full = os.path.join(DASHBOARD_DIR, 'dashboard', self.path.lstrip('/'))
            if os.path.isfile(full):
                self.serve_file(full)
            else:
                self.send_error(404, "not found")
            return

        # 鉴权 gate
        sess, new_token = check_auth(self)
        if not sess:
            # 区分 API 和页面请求（用 path 不带 query）
            if _path.startswith('/api/') or _path in ('/events', '/history', '/publish'):
                self.send_json({"error": "unauthorized", "code": "AUTH_REQUIRED"}, status=401)
            else:
                # 浏览器访问页面 → 跳 login.html（带 next 参数）
                self.redirect(f'/login?next={_path}')
            return

        # 续期 cookie
        if new_token:
            set_session_cookie(self, new_token, sess['ttl'])

        # API 路由
        if _path == '/api/me':
            self.send_json({
                "user": sess['user'],
                "logged_in_at": datetime.fromtimestamp(sess['created'], timezone.utc).isoformat(),
                "last_seen": datetime.fromtimestamp(sess['last_seen'], timezone.utc).isoformat(),
                "remember": sess['remember'],
                "ttl": sess['ttl'],
                "expires_at": datetime.fromtimestamp(sess['last_seen'] + sess['ttl'], timezone.utc).isoformat()
            })
            return

        if _path == '/api/state' or self.path.startswith('/api/state/'):
            rel = self.path[len('/api/'):]   # state/xxx
            full = os.path.join(ROOT, rel)
            if os.path.isfile(full):
                self.serve_file(full)
            else:
                self.send_json({"error": "not found"}, status=404)
            return

        if _path == '/api/events':
            self.handle_sse()
            return

        if _path == '/api/history':
            self.send_json({"history": list(push_history)})
            return

        if _path == '/api/agent-memory':
            self.handle_agent_memory()
            return

        if _path == '/api/projects' or _path == '/api/projects/':
            self.handle_projects()
            return

        if _path == '/api/projects/cache/stats':
            self.handle_projects_cache_stats()
            return

        if _path == '/api/projects/cache/clear':
            self.handle_projects_cache_clear()
            return

        # 单个 repo 详情（v25cg 新增）
        if _path.startswith('/api/repos/') or _path == '/api/repos':
            self.handle_repo_detail()
            return

        # 兼容老路径（/state/*, /events, /history）— 同样鉴权后响应
        if self.path.startswith('/state/'):
            rel = self.path[1:]
            full = os.path.join(ROOT, rel)
            if os.path.isfile(full):
                self.serve_file(full)
                return
            self.send_error(404, "state file not found")

        elif _path == '/events':
            self.handle_sse()
            return

        elif _path == '/history':
            self.send_json({"history": list(push_history)})
            return

        elif _path == '/status.json' or _path == '/api/status':
            # mavis-status 缓存（多路径 fallback）
            candidates = [
                os.path.join(DASHBOARD_DIR, 'state', 'mavis-status.json'),
                '/workspace/agent-memory/state/mavis-status.json',
                os.path.join(DASHBOARD_DIR, 'dashboard', 'state', 'mavis-status.json'),
            ]
            data = None
            for path in candidates:
                if os.path.exists(path):
                    try:
                        with open(path) as f:
                            data = json.load(f)
                        break
                    except Exception:
                        pass
            if data:
                self.send_json(data)
            else:
                self.send_json({
                    "overall": "unknown",
                    "message": "尚未运行 mavis-status.sh",
                    "hint": "bash /workspace/agent-memory/scripts/mavis-status.sh"
                })
            return

        # 静态文件
        if _path == '/' or _path == '/index.html':
            self.serve_file(os.path.join(DASHBOARD_DIR, 'dashboard', 'index.html'), 'text/html; charset=utf-8')
            return

        # 项目进展页面（v25cf 新增）
        if _path == '/projects' or _path == '/projects.html':
            self.serve_file(os.path.join(DASHBOARD_DIR, 'dashboard', 'projects.html'), 'text/html; charset=utf-8')
            return

        # 单个 repo 详情页（v25cg 新增）
        if _path == '/repo' or _path == '/repo.html' or _path.startswith('/repo/'):
            self.serve_file(os.path.join(DASHBOARD_DIR, 'dashboard', 'repo.html'), 'text/html; charset=utf-8')
            return

        # 其他静态资源（dashboard 下的所有文件）
        full = os.path.join(DASHBOARD_DIR, 'dashboard', self.path.lstrip('/'))
        if os.path.isfile(full):
            self.serve_file(full)
            return
        self.send_error(404, "not found")

    # ---------- POST ----------

    def do_POST(self):
        from urllib.parse import urlparse
        _path = urlparse(self.path).path
        ip = get_client_ip(self)

        # 登录（公开）
        if _path == '/login':
            # 限速检查
            if is_rate_limited(ip):
                retry_after = LOGIN_WINDOW
                self.send_json({
                    "error": "too_many_attempts",
                    "code": "RATE_LIMITED",
                    "message": f"登录失败次数过多，请 {LOGIN_WINDOW//60} 分钟后再试",
                    "retry_after": retry_after
                }, status=429)
                return

            try:
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length)
                data = json.loads(body) if body else {}
            except Exception:
                record_attempt(ip, False)
                self.send_json({"error": "invalid_request", "code": "BAD_JSON", "message": "无效的 JSON"}, status=400)
                return

            username = data.get('username', '').strip()
            password = data.get('password', '')
            remember = bool(data.get('remember', False))

            # 用户名
            if username != DASHBOARD_USER:
                record_attempt(ip, False)
                self.send_json({
                    "error": "invalid_credentials",
                    "code": "BAD_USERNAME",
                    "message": "用户名或密码错误"
                }, status=401)
                return

            # 密码
            if not DASHBOARD_PASSWORD_HASH or hash_password(password) != DASHBOARD_PASSWORD_HASH:
                record_attempt(ip, False)
                self.send_json({
                    "error": "invalid_credentials",
                    "code": "BAD_PASSWORD",
                    "message": "用户名或密码错误"
                }, status=401)
                return

            # 成功
            record_attempt(ip, True)
            token, ttl = create_session(username, remember)
            cookie = set_session_cookie(self, token, ttl)
            self.send_json({
                "success": True,
                "user": username,
                "remember": remember,
                "ttl": ttl,
                "expires_in": ttl,
                "next": data.get('next', '/')
            }, extra_headers={'Set-Cookie': cookie})
            log(f"✅ 登录: {username} (remember={remember}, ttl={ttl}s) from {ip}")
            return

        # 登出
        if _path == '/logout':
            cookie_str = self.headers.get('Cookie', '')
            cookie = SimpleCookie(cookie_str)
            token_cookie = cookie.get(SESSION_COOKIE)
            if token_cookie:
                destroy_session(token_cookie.value)
            clear_cookie = clear_session_cookie(self)
            self.send_json({"success": True, "message": "已登出"}, extra_headers={'Set-Cookie': clear_cookie})
            return

        # 主动续期
        if _path == '/api/refresh':
            sess, new_token = check_auth(self)
            if not sess:
                self.send_json({"error": "unauthorized"}, status=401)
                return
            cookie = None
            if new_token:
                cookie = set_session_cookie(self, new_token, sess['ttl'])
            extra = {'Set-Cookie': cookie} if cookie else None
            self.send_json({
                "success": True,
                "expires_in": sess['ttl'],
                "expires_at": datetime.fromtimestamp(sess['last_seen'] + sess['ttl'], timezone.utc).isoformat()
            }, extra_headers=extra)
            return

        # 鉴权 gate
        sess, new_token = check_auth(self)
        if not sess:
            self.send_json({"error": "unauthorized"}, status=401)
            return
        if new_token:
            set_session_cookie(self, new_token, sess['ttl'])

        if _path == '/api/publish':
            self.handle_publish()
            return

        self.send_error(404, "endpoint not found")

    # ---------- helpers ----------

    def serve_file(self, full_path, default_type='application/octet-stream'):
        try:
            with open(full_path, 'rb') as f:
                content = f.read()
            if full_path.endswith('.html'):
                ctype = 'text/html; charset=utf-8'
            elif full_path.endswith('.json'):
                ctype = 'application/json'
            elif full_path.endswith('.md'):
                ctype = 'text/markdown; charset=utf-8'
            elif full_path.endswith('.js'):
                ctype = 'application/javascript; charset=utf-8'
            elif full_path.endswith('.css'):
                ctype = 'text/css; charset=utf-8'
            elif full_path.endswith('.svg'):
                ctype = 'image/svg+xml'
            else:
                ctype = default_type
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(500, str(e))

    def send_json(self, data, status=200, extra_headers=None):
        """发送 JSON 响应。

        重要：Python stdlib BaseHTTPRequestHandler.send_response 把 status line
        append 到 _headers_buffer 末尾，flush 时按 list 顺序写出。如果在
        send_response 之前调 send_header（如 set_session_cookie），那个 header
        会排在 status line 之前，导致响应无效。

        所以这里改用 _send_full_response：自己手动组装 status line + headers + body。
        extra_headers: dict 包含额外 header（Set-Cookie 等）
        会自动合并 self._pending_cookies 里的 Set-Cookie。
        """
        content = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        # 合并 _pending_cookies
        pending = getattr(self, '_pending_cookies', None)
        if pending:
            extra_headers = dict(extra_headers) if extra_headers else {}
            for c in pending:
                # 如果 extra_headers 已有 Set-Cookie，会被覆盖 — 多个 Set-Cookie 浏览器不识别
                # 简单起见，只用第一个
                if 'Set-Cookie' not in extra_headers:
                    extra_headers['Set-Cookie'] = c
            self._pending_cookies = []
        self._send_full_response(status, 'application/json', content, extra_headers)

    def _send_full_response(self, status, content_type, body, extra_headers=None):
        """完全控制响应：status line 在最前"""
        # 收集所有 headers
        headers = []
        # 1) extra_headers 先（保证 Set-Cookie 等在 status line 之后）
        if extra_headers:
            for k, v in extra_headers.items():
                headers.append((k, str(v)))
        # 2) Content-Type
        headers.append(('Content-Type', content_type))
        # 3) Content-Length
        headers.append(('Content-Length', str(len(body))))
        # 4) 通用头（Date / Server / Connection）
        headers.append(('Server', self.version_string() if hasattr(self, 'version_string') else 'SimpleHTTP'))
        from email.utils import formatdate
        headers.append(('Date', formatdate(usegmt=True)))
        # 不再依赖 stdlib 的 _headers_buffer：直接构造 status line + headers + body
        status_msg = self.responses.get(status, (''))[0]
        status_line = ("%s %d %s\r\n" % (self.protocol_version, status, status_msg)).encode('latin-1', 'strict')
        head = b''.join(("%s: %s\r\n" % (k, v)).encode('latin-1', 'strict') for k, v in headers)
        # CORS（保留原有行为）
        head = b"Access-Control-Allow-Origin: *\r\n" \
               b"Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n" \
               b"Access-Control-Allow-Headers: Content-Type\r\n" \
               b"Connection: close\r\n" + head
        self.wfile.write(status_line + head + b"\r\n" + body)

    def redirect(self, location):
        self.send_response(302)
        self.send_header('Location', location)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def handle_sse(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()

        client_queue = queue.Queue(maxsize=100)

        with subscribers_lock:
            subscribers.append(client_queue)

        log(f"SSE 客户端连接（当前 {len(subscribers)} 个）")

        try:
            hello = f"event: hello\ndata: {json.dumps({'timestamp': datetime.now(timezone.utc).isoformat(), 'subscribers': len(subscribers)})}\n\n"
            self.wfile.write(hello.encode('utf-8'))
            self.wfile.flush()

            while True:
                try:
                    msg = client_queue.get(timeout=30)
                    self.wfile.write(msg.encode('utf-8'))
                    self.wfile.flush()
                except queue.Empty:
                    ping = f"event: ping\ndata: {json.dumps({'time': datetime.now(timezone.utc).isoformat()})}\n\n"
                    self.wfile.write(ping.encode('utf-8'))
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with subscribers_lock:
                if client_queue in subscribers:
                    subscribers.remove(client_queue)
            log(f"SSE 客户端断开（剩余 {len(subscribers)} 个）")

    def handle_agent_memory(self):
        """集成 wcaca/agent-memory 状态：profile + 最近决策 + 远程 compact + server health"""
        try:
            import urllib.request
            import re as _re

            result = {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'sandbox': {
                    'has_local_memory': os.path.isdir('/workspace/agent-memory'),
                    'has_remote_compact': os.path.exists('/workspace/.memory-remote.md'),
                },
                'profile': {},
                'recent_decisions': [],
                'tools': [],
                'remote_compact_size': 0,
                'memory_server': {'status': 'unknown'},
                'cloudflared': {'status': 'unknown'},
            }

            # 1. 读 profile.md 关键字段
            profile_file = '/workspace/agent-memory/profile.md'
            if os.path.isfile(profile_file):
                with open(profile_file) as f:
                    pf = f.read()
                # 提取关键字段（### xxx 或 **xxx**:）
                for line in pf.splitlines():
                    m = _re.match(r'^\*\*([^*]+)\*\*:\s*(.+)', line)
                    if m:
                        key = m.group(1).strip().lower().replace(' ', '_')
                        val = m.group(2).strip()[:200]  # 限制长度
                        result['profile'][key] = val
                    m = _re.match(r'^##\s+(.+)', line)
                    if m and ':' not in line and not line.startswith('## 备注'):
                        # 段落标题
                        pass

            # 2. 读最近 3 决策
            dec_file = '/workspace/agent-memory/context/decisions-log.md'
            if os.path.isfile(dec_file):
                with open(dec_file) as f:
                    df = f.read()
                # 匹配 ### DEC-2026-XX-XX: ... 标题
                decisions = _re.findall(r'^### (DEC-[^\n]+)', df, _re.MULTILINE)
                result['recent_decisions'] = decisions[-3:][::-1]  # 最新 3 条

            # 3. 远程 compact 大小
            rc = '/workspace/.memory-remote.md'
            if os.path.isfile(rc):
                result['remote_compact_size'] = os.path.getsize(rc)

            # 4. memory-server 健康（公网）
            try:
                resp = urllib.request.urlopen('https://memory.noteverse.space/health', timeout=5)
                health = json.loads(resp.read())
                result['memory_server'] = health
            except Exception as e:
                result['memory_server'] = {'status': 'unreachable', 'error': str(e)[:100]}

            # 5. cloudflared 通过 SSH 检查
            try:
                ssh_host = os.environ.get('SSH_HOST', '163.7.3.92')
                cmd = "pid=$(pgrep -f 'cloudflared tunnel' | head -1); routes=$(grep -c hostname /root/.cloudflared/config.yml 2>/dev/null || echo 0); [ -n \"$pid\" ] && echo \"pid=$pid routes=$routes\" || echo DOWN"
                cf_status = os.popen(f'ssh -o ConnectTimeout=3 -o StrictHostKeyChecking=no -o BatchMode=yes {os.environ.get("SSH_USER", "root")}@{ssh_host} "{cmd}" 2>/dev/null').read().strip()
                if cf_status == 'DOWN':
                    result['cloudflared'] = {'status': 'down'}
                elif cf_status:
                    result['cloudflared'] = {'status': 'up', 'detail': cf_status}
                else:
                    result['cloudflared'] = {'status': 'unknown'}
            except Exception as e:
                result['cloudflared'] = {'status': 'ssh-fail', 'error': str(e)[:100]}

            # 6. 工具列表
            bin_dir = '/workspace/bin'
            if os.path.isdir(bin_dir):
                result['tools'] = sorted([f for f in os.listdir(bin_dir) if not f.startswith('.')])

            self.send_json(result)
        except Exception as e:
            self.send_json({'error': str(e)}, status=500)


    # ---------- GitHub Projects 端点 ----------

    def handle_projects(self):
        """返回项目进展 dashboard 数据"""
        if not GITHUB_API_AVAILABLE:
            self.send_json({
                "error": "github_api module not available",
                "detail": _GH_IMPORT_ERR,
            }, status=500)
            return
        try:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            username = (qs.get('username', [None]) or [None])[0]
            include_forks = (qs.get('include_forks', ['false'])[0].lower() in ('1', 'true', 'yes'))

            client = get_github_client()
            data = client.get_dashboard_data(
                username=username,
                include_forks=include_forks,
            )
            self.send_json(data)
        except Exception as e:
            log(f"❌ /api/projects 失败: {e}")
            self.send_json({"error": str(e)}, status=500)

    def handle_projects_cache_stats(self):
        if not GITHUB_API_AVAILABLE:
            self.send_json({"error": "not available"}, status=500)
            return
        try:
            self.send_json(get_github_client().cache.stats())
        except Exception as e:
            self.send_json({"error": str(e)}, status=500)

    def handle_projects_cache_clear(self):
        if not GITHUB_API_AVAILABLE:
            self.send_json({"error": "not available"}, status=500)
            return
        try:
            client = get_github_client()
            # 重置缓存对象
            client.cache = type(client.cache)(ttl=client.cache.ttl)
            self.send_json({"ok": True, "message": "cache cleared"})
        except Exception as e:
            self.send_json({"error": str(e)}, status=500)

    def handle_repo_detail(self):
        """返回单个 repo 详情 + commits/issues/PRs
        URL: /api/repos/<full_name>  其中 <full_name> 可包含 / 用 url encode
        也支持 /api/repos?repo=<name> 简化
        """
        if not GITHUB_API_AVAILABLE:
            self.send_json({
                "error": "github_api module not available",
                "detail": _GH_IMPORT_ERR,
            }, status=500)
            return
        from urllib.parse import urlparse, parse_qs, unquote
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        try:
            limit = int((qs.get('limit', ['20'])[0]))
        except (ValueError, TypeError):
            limit = 20

        # 从 path 拿 repo name：/api/repos/<name>
        parts = parsed.path.split('/api/repos/', 1)
        if len(parts) == 2 and parts[1]:
            repo_name = unquote(parts[1].rstrip('/'))
        else:
            repo_name = (qs.get('repo', [None]) or [None])[0]
        if not repo_name:
            self.send_json({"error": "repo name required", "hint": "用 /api/repos/<name> 或 /api/repos?repo=<name>"}, status=400)
            return

        try:
            client = get_github_client()
            data = client.get_repo_full(repo_name, activity_limit=limit)
            self.send_json(data)
        except Exception as e:
            log(f"❌ /api/repos/{repo_name} 失败: {e}")
            self.send_json({"error": str(e), "repo": repo_name}, status=500)

    def handle_publish(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8')
            data = json.loads(body) if body else {}
        except Exception as e:
            self.send_error(400, f"invalid JSON: {e}")
            return

        event_type = data.get('type', 'message')
        event_data = data.get('data', {})

        add_to_history(event_type, event_data)
        broadcast_event(event_type, event_data)

        log(f"📤 推送 [{event_type}]: {json.dumps(event_data, ensure_ascii=False)[:100]}")

        self.send_json({
            "success": True,
            "subscribers": len(subscribers),
            "timestamp": datetime.now(timezone.utc).isoformat()
        })


class ReusingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    # 启动时加载持久化数据
    _load_sessions()
    _load_attempts()

    log(f"🚀 Mavis Dashboard server starting on port {PORT}")
    log(f"   Root: {ROOT}")
    log(f"   User: {DASHBOARD_USER}")
    log(f"   Sessions: {len(sessions)} loaded")
    log(f"   Login: http://localhost:{PORT}/login")
    log(f"   Dashboard: http://localhost:{PORT}/")
    log(f"   SSE: http://localhost:{PORT}/api/events")
    log(f"   Publish: POST http://localhost:{PORT}/api/publish")
    log(f"   Health: http://localhost:{PORT}/health")
    log(f"   Projects: http://localhost:{PORT}/api/projects")
    log(f"   GitHub API: {'✓' if GITHUB_API_AVAILABLE else '✗ ' + _GH_IMPORT_ERR}")

    use_ssl = bool(os.environ.get("ENABLE_SSL") == "1" and SSL_CERT and SSL_KEY
                   and os.path.exists(SSL_CERT) and os.path.exists(SSL_KEY))
    if os.environ.get("LOCAL_ONLY") == "1":
        BIND_HOST = "127.0.0.1"
    else:
        BIND_HOST = "0.0.0.0"
    log(f"   Bind: {BIND_HOST}:{PORT} (SSL: {use_ssl})")

    class SecureTCPServer(ReusingTCPServer):
        pass

    if use_ssl:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(SSL_CERT, SSL_KEY)
        httpd = SecureTCPServer((BIND_HOST, PORT), DashboardHandler)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        log(f"   🔒 SSL enabled")
    else:
        httpd = SecureTCPServer((BIND_HOST, PORT), DashboardHandler)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("Stopping...")


if __name__ == "__main__":
    main()
