#!/usr/bin/env python3
"""Web 层测试：认证、鉴权、目录穿越防护、API 校验。"""

import http.client
import json
import os
import sys
import tempfile
import unittest

def _project_root():
    import os as _os
    here = _os.path.dirname(_os.path.abspath(__file__))
    p = _os.path.abspath(_os.path.join(here, ".."))
    if _os.path.exists(_os.path.join(p, "manifest")):
        return p
    return _os.path.abspath(_os.path.join(here, "..", "g2fan"))

PROJECT_ROOT = _project_root()
sys.path.insert(0, os.path.join(PROJECT_ROOT, "app", "server"))
import urllib.parse


from g2fan import config as cfg  # noqa: E402
from g2fan import control as ctl  # noqa: E402
from g2fan import web as web_mod  # noqa: E402
from test_control import FakeBackend, FakeReader  # noqa: E402


class FakeController:
    def __init__(self, store):
        self.store = store
        self.backend = FakeBackend()
        self.reader = FakeReader(cpu=46.0)
        self._events = []

    def status(self):
        return {"running": True, "mode": self.store.get()["mode"], "temperature": 46.0}

    def events(self):
        return list(self._events)

    def log(self, level, message):
        self._events.append({"level": level, "message": message})

    def set_mode(self, mode):
        return self.store.update({"mode": mode})

    def set_manual_speed(self, speed):
        return self.store.update({"mode": "manual", "manual_speed": speed})


class WebTestBase(unittest.TestCase):
    password = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.www = os.path.join(self.tmp.name, "www")
        os.makedirs(self.www)
        with open(os.path.join(self.www, "index.html"), "w", encoding="utf-8") as fh:
            fh.write("<html>ok</html>")
        secret = os.path.join(self.tmp.name, "secret.txt")
        with open(secret, "w", encoding="utf-8") as fh:
            fh.write("TOP-SECRET")
        self.secret = secret

        self.store = cfg.ConfigStore(os.path.join(self.tmp.name, "config.json"))
        self.store.update({"password": self.password, "port": 0})
        self.controller = FakeController(self.store)
        ctx = web_mod.AppContext(self.controller, self.store, self.www)
        self.server = web_mod.build_server(ctx, "127.0.0.1", 0)
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.ctx = ctx

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        return resp.status, raw, parsed


class TestOpenMode(WebTestBase):
    password = ""

    def test_index_served(self):
        status, raw, _ = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"ok", raw)

    def test_status_without_auth(self):
        status, _, body = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_config_never_leaks_password(self):
        self.store.update({"password": "hunter2"})
        status, _, body = self.request(
            "GET", "/api/config",
            headers={"Authorization": f"Bearer {web_mod.token_for('hunter2')}"})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["password"], "")
        self.assertTrue(body["data"]["has_password"])

    def test_invalid_mode_rejected(self):
        status, _, body = self.request("POST", "/api/mode", {"mode": "turbo"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_unknown_field_ignored_on_config_patch(self):
        status, _, body = self.request("POST", "/api/config",
                                       {"evil": "x", "interval": 7})
        self.assertEqual(status, 200)
        self.assertNotIn("evil", body["data"])
        self.assertEqual(body["data"]["interval"], 7)


class TestTraversalProtection(WebTestBase):
    password = ""

    def test_encoded_traversal_blocked(self):
        # 直接构造原始路径，绕过客户端规范化
        for path in ("/%2e%2e/secret.txt",
                     "/..%2fsecret.txt",
                     "/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
                     "/subdir/../../secret.txt"):
            status, raw, _ = self.request("GET", path)
            self.assertIn(status, (403, 404), f"{path} -> {status}")
            self.assertNotIn(b"TOP-SECRET", raw, f"{path} 泄漏了 www 之外的文件")

    def test_missing_file_404(self):
        status, _, _ = self.request("GET", "/nope.html")
        self.assertEqual(status, 404)


class TestAuthRequired(WebTestBase):
    password = "s3cret"

    def test_api_denied_without_token(self):
        status, _, _ = self.request("GET", "/api/status")
        self.assertEqual(status, 401)

    def test_login_with_wrong_password(self):
        status, _, _ = self.request("POST", "/api/auth/login", {"password": "nope"})
        self.assertEqual(status, 401)

    def test_login_then_access_with_cookie(self):
        status, _, _ = self.request("POST", "/api/auth/login", {"password": "s3cret"})
        self.assertEqual(status, 200)
        token = web_mod.token_for("s3cret")
        status, _, body = self.request("GET", "/api/status",
                                       headers={"Cookie": f"{web_mod.COOKIE_NAME}={token}"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_bearer_token_accepted(self):
        status, _, body = self.request(
            "GET", "/api/status",
            headers={"Authorization": f"Bearer {web_mod.token_for('s3cret')}"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_login_rate_limited(self):
        codes = []
        for _ in range(web_mod.LOGIN_MAX_ATTEMPTS + 3):
            status, _, _ = self.request("POST", "/api/auth/login", {"password": "bad"})
            codes.append(status)
        self.assertIn(429, codes)


class TestAuthStatusEndpoint(WebTestBase):
    password = "s3cret"

    def test_auth_status_reports_requirement(self):
        status, _, body = self.request(
            "GET", "/api/auth/status",
            headers={"Authorization": f"Bearer {web_mod.token_for('s3cret')}"})
        self.assertEqual(status, 200)
        self.assertTrue(body["data"]["required"])
        self.assertTrue(body["data"]["authenticated"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
