"""Web 管理界面与 REST API。

使用标准库 ``http.server``，零第三方依赖。

安全约束
--------
* 静态文件严格限制在 www 根目录内，解析真实路径后校验前缀，拒绝目录穿越。
* 请求体大小上限，只接受 JSON。
* 配置类接口只做「有限字段 + 数值钳制」，绝不执行命令、绝不读写任意路径。
* 配置了密码时，所有 ``/api/*`` 与页面都要求令牌；登录失败按 IP 限速。
* 服务只提供风扇控制与状态查询，不暴露文件系统与进程操作。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from . import config as config_mod
from . import control as control_mod
from . import sensors as sensors_mod

MAX_BODY = 64 * 1024
COOKIE_NAME = "g2fan_token"
LOGIN_MAX_ATTEMPTS = 8
LOGIN_WINDOW = 300.0


def token_for(password: str) -> str:
    return hashlib.sha256(("g2fan:auth:" + password).encode("utf-8")).hexdigest()


class AppContext:
    """处理器需要的一切。"""

    def __init__(self, controller: control_mod.FanController,
                 store: config_mod.ConfigStore, www_dir: str,
                 version: str = "1.0.0"):
        self.controller = controller
        self.store = store
        self.www_dir = os.path.realpath(www_dir)
        self.version = version
        self._attempts: dict[str, list[float]] = {}
        self._attempt_lock = threading.Lock()

    # --- 认证 ---
    @property
    def password(self) -> str:
        return str(self.store.get().get("password") or "")

    def check_token(self, token: str | None) -> bool:
        if not self.password:
            return True
        if not token:
            return False
        return _consteq(token, token_for(self.password))

    def note_attempt(self, ip: str) -> bool:
        """记录一次失败尝试；返回是否仍允许再试。"""
        now = time.time()
        with self._attempt_lock:
            bucket = [t for t in self._attempts.get(ip, []) if now - t < LOGIN_WINDOW]
            bucket.append(now)
            self._attempts[ip] = bucket
            return len(bucket) <= LOGIN_MAX_ATTEMPTS

    def clear_attempts(self, ip: str) -> None:
        with self._attempt_lock:
            self._attempts.pop(ip, None)


def _consteq(a: str, b: str) -> bool:
    return hashlib.sha256(a.encode()).digest() == hashlib.sha256(b.encode()).digest()


class Handler(BaseHTTPRequestHandler):
    server_version = "g2fan"
    protocol_version = "HTTP/1.1"

    ctx: AppContext  # 由 serve() 注入到子类

    # ------------------------------------------------------------------ 工具
    def log_message(self, fmt, *args):  # 静音默认 stderr 日志
        return

    def _send_json(self, payload, status: int = HTTPStatus.OK, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, status: int = HTTPStatus.OK):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str):
        self._send_json({"ok": False, "error": message}, status=status)

    def _read_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY:
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else "?"

    def _token_from_request(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        header = self.headers.get("X-Fan-Token")
        if header:
            return header.strip()
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            key, _, value = part.strip().partition("=")
            if key == COOKIE_NAME:
                return value
        return None

    def _authorized(self) -> bool:
        return self.ctx.check_token(self._token_from_request())

    # ------------------------------------------------------------------ 路由
    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            # 认证状态查询必须免认证，否则前端无法判断是否需要显示登录框
            if path != "/api/auth/status" and not self._authorized():
                self._error(HTTPStatus.UNAUTHORIZED, "未认证")
                return
            self._route_api_get(path)
            return
        if path == "/api" or path == "/api/":
            self._error(HTTPStatus.NOT_FOUND, "未知接口")
            return
        self._serve_static(path)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/auth/login":
            self._handle_login()
            return
        if not path.startswith("/api/"):
            self._error(HTTPStatus.NOT_FOUND, "未知接口")
            return
        if not self._authorized():
            self._error(HTTPStatus.UNAUTHORIZED, "未认证")
            return
        self._route_api_post(path)

    # ------------------------------------------------------------------ API: GET
    def _route_api_get(self, path: str):
        if path == "/api/status":
            self._send_json({"ok": True, "data": self.ctx.controller.status()})
        elif path == "/api/config":
            cfg = dict(self.ctx.store.get())
            cfg["password"] = ""  # 不下发密码本体
            cfg["has_password"] = bool(self.ctx.password)
            self._send_json({"ok": True, "data": cfg})
        elif path == "/api/hardware":
            self._send_json({"ok": True, "data": self._hardware_info()})
        elif path == "/api/logs":
            self._send_json({"ok": True, "data": self.ctx.controller.events()})
        elif path == "/api/auth/status":
            self._send_json({"ok": True, "data": {
                "required": bool(self.ctx.password),
                "authenticated": self._authorized(),
            }})
        elif path == "/api/version":
            self._send_json({"ok": True, "data": {"version": self.ctx.version}})
        else:
            self._error(HTTPStatus.NOT_FOUND, "未知接口")

    # ------------------------------------------------------------------ API: POST
    def _route_api_post(self, path: str):
        if path == "/api/mode":
            data = self._read_body()
            if data is None:
                self._error(HTTPStatus.BAD_REQUEST, "请求体非法")
                return
            mode = data.get("mode")
            if mode not in config_mod.MODES:
                self._error(HTTPStatus.BAD_REQUEST, "模式非法")
                return
            try:
                if mode == "manual" and "speed" in data:
                    self.ctx.controller.set_manual_speed(data["speed"])
                cfg = self.ctx.controller.set_mode(mode)
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._send_json({"ok": True, "data": cfg})

        elif path == "/api/config":
            data = self._read_body()
            if data is None:
                self._error(HTTPStatus.BAD_REQUEST, "请求体非法")
                return
            allowed = {"mode", "temp_source", "manual_speed", "min_speed", "max_speed",
                       "interval", "hysteresis", "release_speed", "curve", "password",
                       "bind", "port", "backend", "log_keep"}
            patch = {k: v for k, v in data.items() if k in allowed}
            if "mode" in patch and patch["mode"] not in config_mod.MODES:
                patch.pop("mode")
            if "temp_source" in patch and patch["temp_source"] not in config_mod.TEMP_SOURCES:
                patch.pop("temp_source")
            try:
                cfg = self.ctx.store.update(patch)
            except OSError as exc:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"配置保存失败: {exc}")
                return
            self.ctx.controller.log("info", f"配置已更新: {', '.join(sorted(patch)) or '无'}")
            out = dict(cfg)
            out["password"] = ""
            out["has_password"] = bool(cfg.get("password"))
            self._send_json({"ok": True, "data": out})

        elif path == "/api/curve/generate":
            data = self._read_body() or {}
            curve = control_mod.generate_curve(
                points=data.get("points", 6),
                temp_min=data.get("temp_min", 35),
                temp_max=data.get("temp_max", 85),
            )
            self._send_json({"ok": True, "data": {"curve": curve}})

        elif path == "/api/curve":
            data = self._read_body()
            if data is None or "curve" not in data:
                self._error(HTTPStatus.BAD_REQUEST, "缺少 curve 字段")
                return
            curve = config_mod.sanitize_curve(data["curve"])
            cfg = self.ctx.store.update({"curve": curve, "mode": "auto"})
            self._send_json({"ok": True, "data": {"curve": cfg["curve"], "mode": cfg["mode"]}})

        elif path == "/api/auth/logout":
            self._send_json({"ok": True}, extra_headers={
                "Set-Cookie": f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
            })

        else:
            self._error(HTTPStatus.NOT_FOUND, "未知接口")

    def _handle_login(self):
        data = self._read_body()
        ip = self._client_ip()
        if not self.ctx.password:
            self._send_json({"ok": True, "data": {"required": False}})
            return
        if data is None or not isinstance(data.get("password"), str):
            self._error(HTTPStatus.BAD_REQUEST, "请求体非法")
            return
        if not self.ctx.check_token(token_for(data["password"])):
            if not self.ctx.note_attempt(ip):
                self._error(HTTPStatus.TOO_MANY_REQUESTS, "尝试过于频繁，请稍后再试")
                return
            self._error(HTTPStatus.UNAUTHORIZED, "密码错误")
            return
        self.ctx.clear_attempts(ip)
        self._send_json({"ok": True}, extra_headers={
            "Set-Cookie": (f"{COOKIE_NAME}={token_for(data['password'])}; "
                           "Path=/; HttpOnly; SameSite=Lax; Max-Age=604800")
        })

    # ------------------------------------------------------------------ 硬件信息
    def _hardware_info(self) -> dict:
        info = {
            "controller_backend": type(self.ctx.controller.backend).__name__,
            "sensors": self.ctx.controller.reader.read_detail(),
            "sensor_roles": {
                role: [s.to_dict() for s in sensors]
                for role, sensors in sensors_mod.discover_roles().items()
            },
        }
        describe = getattr(self.ctx.controller.backend, "describe", None)
        info["backend_detail"] = describe() if callable(describe) else {}
        return info

    # ------------------------------------------------------------------ 静态文件
    _MIME = {
        ".html": "text/html; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".json": "application/json; charset=utf-8",
        ".woff2": "font/woff2",
    }

    def _serve_static(self, path: str):
        rel = unquote(path).lstrip("/")
        if not rel:
            rel = "index.html"
        if "\x00" in rel:
            self._error(HTTPStatus.BAD_REQUEST, "路径非法")
            return
        candidate = os.path.realpath(os.path.join(self.ctx.www_dir, rel))
        # 目录穿越防护：解析后的真实路径必须仍在 www 根目录内
        if candidate != self.ctx.www_dir and not candidate.startswith(self.ctx.www_dir + os.sep):
            self._error(HTTPStatus.FORBIDDEN, "禁止访问")
            return
        if not os.path.isfile(candidate):
            self._error(HTTPStatus.NOT_FOUND, "文件不存在")
            return
        ext = os.path.splitext(candidate)[1].lower()
        try:
            with open(candidate, "rb") as fh:
                body = fh.read()
        except OSError:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "读取失败")
            return
        self._send_bytes(body, self._MIME.get(ext, "application/octet-stream"))


def build_server(ctx: AppContext, bind: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"ctx": ctx})
    server = ThreadingHTTPServer((bind, port), handler)
    server.daemon_threads = True
    return server


def serve(ctx: AppContext, bind: str, port: int) -> ThreadingHTTPServer:
    server = build_server(ctx, bind, port)
    thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    thread.start()
    return server
