#!/usr/bin/env python3
"""端到端集成测试：以假 /dev/port 启动真实守护进程，验证完整链路。

覆盖：
  启动 → 探测 EC → 快照落盘 → 接管 → 温控写入 → REST API 调速
  → SIGTERM 优雅退出 → 还原快照并清理

关于假设备：真实 /dev/port 的读写是独立的端口操作；此处用一个普通文件替代，
读操作会看到刚写入的选择字节（0x12），因此不对「回读值」做断言 ——
回读语义由 test_ec_protocol.py 的单元测试用记录式后端验证。
本测试断言的是**写入侧**的可观察行为，这正是真实设备上的关键路径。
"""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

def _project_root():
    """兼容两种仓库布局：仓库根即 g2fan，或 g2fan 为子目录。"""
    import os as _os
    p = _os.path.abspath(_os.path.join(HERE, ".."))
    if _os.path.exists(_os.path.join(p, "manifest")):
        return p
    return _os.path.abspath(_PROJECT_ROOT)

PROJECT_ROOT = _project_root()

APP_DIR = os.path.abspath(os.path.join(PROJECT_ROOT, "app"))
RUNNER = os.path.join(APP_DIR, "server", "g2fan-run.py")

PORT_CMD = 0x4E
PORT_DATA = 0x4F
DUTY_MAX = 184
# 协议中「选择数据窗口」子命令的值，写操作的最后一次 0x4E 写入为 0x2F
REG_DATA = 0x2F


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def duty(pct: float) -> int:
    return min(int(pct / 100.0 * DUTY_MAX), DUTY_MAX)


class TestDaemonEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pkgvar = os.path.join(self.tmp.name, "var")
        os.makedirs(self.pkgvar)
        self.port_file = os.path.join(self.tmp.name, "fake_port")
        with open(self.port_file, "wb") as fh:
            fh.write(b"\x00" * 0x2000)
        self.http_port = free_port()
        with open(os.path.join(self.pkgvar, "config.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "backend": "sgw_ec",
                "mode": "manual",
                "manual_speed": 60,
                "interval": 1,
                "hysteresis": 0,
                "min_speed": 10,
                "bind": "127.0.0.1",
                "port": self.http_port,
                "drop_privileges": False,
            }, fh)
        env = dict(os.environ)
        env.update({
            "G2FAN_DEV_PORT": self.port_file,
            "TRIM_PKGVAR": self.pkgvar,
            "TRIM_APPDEST": APP_DIR,
            "G2FAN_PORT": str(self.http_port),   # 显式覆盖，避免被入口端口覆盖
        })
        self.proc = subprocess.Popen(
            [sys.executable, RUNNER], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self._wait_http()

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.proc.stdout:
            self.proc.stdout.close()
        self.tmp.cleanup()

    # --- 辅助 ---
    def _wait_http(self, timeout=20):
        """就绪探测使用 /api/version：不触碰硬件，避免干扰写入断言。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read() if self.proc.stdout else ""
                self.fail(f"守护进程提前退出 (rc={self.proc.returncode})\n{out}")
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.http_port}/api/version", timeout=1) as r:
                    if r.status == 200:
                        return
            except (urllib.error.URLError, OSError, ConnectionError):
                time.sleep(0.25)
        self.fail("等待 HTTP 服务就绪超时")

    def api(self, path, payload=None):
        url = f"http://127.0.0.1:{self.http_port}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"},
            method="POST" if data is not None else "GET")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def port_byte(self, offset):
        with open(self.port_file, "rb") as fh:
            fh.seek(offset)
            return fh.read(1)[0]

    def wait_for_port(self, offset, expected, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.port_byte(offset) == expected:
                return True
            time.sleep(0.25)
        return False

    def log_text(self):
        path = os.path.join(self.pkgvar, "fan.log")
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        return ""

    # --- 用例 ---
    def test_01_status_reports_running_g2ec_backend(self):
        data = self.api("/api/status")["data"]
        self.assertTrue(data["running"])
        self.assertEqual(data["backend"], "G2EC", "应自动选中 G2 原生 EC 后端")
        self.assertEqual(data["mode"], "manual")

    def test_02_control_cycle_writes_duty_to_ec_ports(self):
        self.assertTrue(self.wait_for_port(PORT_DATA, duty(60)),
                        f"未把 60% 占空比写入端口 0x4F\n{self.log_text()}")
        self.assertEqual(self.port_byte(PORT_CMD), REG_DATA,
                         "端口 0x4E 最后一次写入应为数据窗口选择值 0x2F")

    def test_03_snapshot_persisted_while_running(self):
        snap = os.path.join(self.pkgvar, "ec_snapshot.json")
        deadline = time.time() + 10
        while time.time() < deadline and not os.path.exists(snap):
            time.sleep(0.2)
        self.assertTrue(os.path.exists(snap), "运行期间应存在 EC 快照，供崩溃后还原")
        with open(snap, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload.get("backend"), "sgw_ec")
        self.assertIn("regs", payload)

    def test_04_api_changes_speed_end_to_end(self):
        self.api("/api/mode", {"mode": "manual", "speed": 100})
        self.assertTrue(self.wait_for_port(PORT_DATA, 184),
                        f"100% 应写入 0xB8\n{self.log_text()}")
        # 再验证模式接口
        self.api("/api/mode", {"mode": "full"})
        data = self.api("/api/status")["data"]
        self.assertEqual(data["mode"], "full")
        self.assertEqual(data["target_speed"], 100.0)

    def test_05_graceful_stop_restores_and_cleans_snapshot(self):
        self.assertTrue(self.wait_for_port(PORT_DATA, duty(60)))
        self.proc.send_signal(signal.SIGTERM)
        self.proc.wait(timeout=15)
        self.assertEqual(self.proc.returncode, 0)
        text = self.log_text()
        self.assertIn("还原", text, f"退出时应还原接管前状态\n{text}")
        self.assertFalse(os.path.exists(os.path.join(self.pkgvar, "ec_snapshot.json")),
                         "优雅退出后应删除快照文件")
        # 还原过程以写入结束
        self.assertEqual(self.port_byte(PORT_CMD), REG_DATA)

    def test_06_web_ui_served(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.http_port}/", timeout=5) as r:
            body = r.read().decode("utf-8")
        self.assertIn("风扇控制", body)
        self.assertIn("温控曲线", body)

    def test_07_traversal_blocked(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.http_port, timeout=5)
        conn.request("GET", "/%2e%2e%2f%2e%2e%2fetc%2fpasswd")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        self.assertIn(resp.status, (403, 404))
        self.assertNotIn(b"root:", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
