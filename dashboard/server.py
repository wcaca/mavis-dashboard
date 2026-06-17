#!/usr/bin/env python3
"""
dashboard-server.py - Mavis Agent Dashboard HTTP server
支持：
  - GET  /              → index.html
  - GET  /state/*       → state 文件
  - GET  /events        → SSE 实时推送
  - POST /publish       → 接收推送（agent 调用）
  - GET  /history       → 推送历史
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
from datetime import datetime, timezone
from http.cookies import SimpleCookie

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
SESSION_COOKIE = "mavis_session"
SESSION_TTL = 7 * 24 * 3600
PASSWORD_SALT = "mavis-dashboard-v1"

sessions = {}
sessions_lock = threading.Lock()

def hash_password(password):
    return hashlib.sha256((PASSWORD_SALT + password).encode()).hexdigest()

def check_auth(handler):
    cookie_str = handler.headers.get('Cookie', '')
    cookie = SimpleCookie(cookie_str)
    token = cookie.get(SESSION_COOKIE)
    if not token:
        return False
    token = token.value
    with sessions_lock:
        sess = sessions.get(token)
        if not sess:
            return False
        if time.time() - sess['created'] > SESSION_TTL:
            del sessions[token]
            return False
    return True

def create_session(handler):
    token = secrets.token_urlsafe(32)
    with sessions_lock:
        sessions[token] = {'created': time.time(), 'user': DASHBOARD_USER}
    handler.send_header('Set-Cookie', f'{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}')

def destroy_session(handler):
    cookie_str = handler.headers.get('Cookie', '')
    cookie = SimpleCookie(cookie_str)
    token = cookie.get(SESSION_COOKIE)
    if token:
        with sessions_lock:
            sessions.pop(token.value, None)
    handler.send_header('Set-Cookie', f'{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')

# 推送历史（最近 50 条）
push_history = []
push_history_lock = threading.Lock()

# 订阅者列表（SSE 客户端）
subscribers = []
subscribers_lock = threading.Lock()


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def broadcast_event(event_type, data):
    """向所有 SSE 订阅者推送事件"""
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


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        # 简化日志
        pass

    def end_headers(self):
        # CORS
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        # Auth gate
        if self.path == '/login' or self.path == '/login.html':
            # serve login page
            login_path = os.path.join(DASHBOARD_DIR, 'dashboard', 'login.html')
            if os.path.exists(login_path):
                self.send_file(login_path)
            else:
                self.send_error(404, "login.html not found")
            return

        if not check_auth(self):
            if self.path == '/' or self.path.startswith('/dashboard/') or self.path == '/index.html':
                # serve login page
                login_path = os.path.join(DASHBOARD_DIR, 'dashboard', 'login.html')
                if os.path.exists(login_path):
                    self.send_file(login_path)
                else:
                    self.send_error(404, "login.html not found")
            else:
                self.send_error(401, "unauthorized")
            return

        if self.path == '/':
            # dashboard 主页面
            self.path = '/dashboard/index.html'

        if self.path.startswith('/state/'):
            # state 文件
            rel = self.path[1:]  # state/latest.json
            full = os.path.join(ROOT, rel)
            if os.path.isfile(full):
                self.send_file(full)
                return
            self.send_error(404, "state file not found")

        elif self.path == '/events':
            # SSE
            self.handle_sse()
            return

        elif self.path == '/history':
            # 推送历史
            self.send_json({
                "history": list(push_history)
            })
            return

        elif self.path == '/health':
            # 服务健康
            self.send_json({
                "status": "ok",
                "subscribers": len(subscribers),
                "history_size": len(push_history),
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            return

        elif self.path == '/status.json':
            # mavis-status 缓存
            try:
                with open('/workspace/agent-memory/state/mavis-status.json') as f:
                    import json as _json
                    data = _json.load(f)
                self.send_json(data)
            except FileNotFoundError:
                self.send_json({"overall": "unknown", "message": "尚未运行 mavis-status"})
            return

        else:
            # 普通静态文件
            full = os.path.join(DASHBOARD_DIR, self.path[1:])
            if os.path.isfile(full):
                self.send_file(full)
                return
            self.send_error(404, "not found")

    def do_POST(self):
        if self.path == '/login':
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length)
                data = json.loads(body)
                username = data.get('username', '')
                password = data.get('password', '')
                if username == DASHBOARD_USER and DASHBOARD_PASSWORD_HASH and hash_password(password) == DASHBOARD_PASSWORD_HASH:
                    self.send_response(200)
                    create_session(self)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "ok", "user": username}).encode())
                else:
                    self.send_response(401)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "error", "message": "invalid credentials"}).encode())
            except Exception as e:
                self.send_error(400, str(e))
            return

        if self.path == '/logout':
            self.send_response(200)
            destroy_session(self)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
            return

        if not check_auth(self):
            self.send_error(401, "unauthorized")
            return

        if self.path == '/publish':
            self.handle_publish()
            return
        self.send_error(404, "endpoint not found")

    def send_file(self, full_path):
        try:
            with open(full_path, 'rb') as f:
                content = f.read()
            # 判断 content type
            if full_path.endswith('.html'):
                ctype = 'text/html; charset=utf-8'
            elif full_path.endswith('.json'):
                ctype = 'application/json'
            elif full_path.endswith('.md'):
                ctype = 'text/markdown; charset=utf-8'
            else:
                ctype = 'application/octet-stream'

            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(500, str(e))

    def send_json(self, data):
        content = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def handle_sse(self):
        """Server-Sent Events"""
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()

        # 客户端消息队列
        client_queue = queue.Queue(maxsize=100)

        with subscribers_lock:
            subscribers.append(client_queue)

        log(f"SSE 客户端连接（当前 {len(subscribers)} 个）")

        try:
            # 发送初始 hello
            hello = f"event: hello\ndata: {json.dumps({'timestamp': datetime.now(timezone.utc).isoformat(), 'subscribers': len(subscribers)})}\n\n"
            self.wfile.write(hello.encode('utf-8'))
            self.wfile.flush()

            # 维护心跳
            last_ping = time.time()

            while True:
                try:
                    # 阻塞等待消息（最多 30 秒）
                    msg = client_queue.get(timeout=30)
                    self.wfile.write(msg.encode('utf-8'))
                    self.wfile.flush()
                except queue.Empty:
                    # 30 秒没消息，发送心跳
                    ping = f"event: ping\ndata: {json.dumps({'time': datetime.now(timezone.utc).isoformat()})}\n\n"
                    self.wfile.write(ping.encode('utf-8'))
                    self.wfile.flush()
                    last_ping = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with subscribers_lock:
                if client_queue in subscribers:
                    subscribers.remove(client_queue)
            log(f"SSE 客户端断开（剩余 {len(subscribers)} 个）")

    def handle_publish(self):
        """接收推送（agent 调用）"""
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8')
            data = json.loads(body) if body else {}
        except Exception as e:
            self.send_error(400, f"invalid JSON: {e}")
            return

        event_type = data.get('type', 'message')
        event_data = data.get('data', {})

        # 记录历史
        add_to_history(event_type, event_data)

        # 广播
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
    log(f"🚀 Mavis Dashboard server starting on port {PORT}")
    log(f"   Root: {ROOT}")
    log(f"   Dashboard: http://localhost:{PORT}/")
    log(f"   SSE: http://localhost:{PORT}/events")
    log(f"   Publish: POST http://localhost:{PORT}/publish")

    # Bind logic + SSL support
    use_ssl = bool(os.environ.get("ENABLE_SSL") == "1" and SSL_CERT and SSL_KEY and os.path.exists(SSL_CERT) and os.path.exists(SSL_KEY))
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
