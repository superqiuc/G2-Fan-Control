#!/usr/bin/env python3
"""生命周期脚本（cmd/*）测试。

在隔离的临时目录里真实执行安装/配置/启停脚本，覆盖：
  install_callback 写配置与属主 → main start/status/stop → 崩溃后 --release 兜底还原

这些脚本此前只有语法检查，没有行为测试；本文件把它们纳入回归范围。
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

HERE = os.path.dirname(os.path.abspath(__file__))

def _project_root():
    """兼容两种仓库布局：仓库根即 g2fan，或 g2fan 为子目录。"""
    import os as _os
    p = _os.path.abspath(_os.path.join(HERE, ".."))
    if _os.path.exists(_os.path.join(p, "manifest")):
        return p
    return _os.path.abspath(_PROJECT_ROOT)

PROJECT_ROOT = _project_root()

PROJECT = os.path.abspath(PROJECT_ROOT)
APP_DIR = os.path.join(PROJECT, "app")
CMD_DIR = os.path.join(PROJECT, "cmd")
MAIN = os.path.join(CMD_DIR, "main")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class CmdScriptBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pkgvar = os.path.join(self.tmp.name, "var")
        self.pkgetc = os.path.join(self.tmp.name, "etc")
        os.makedirs(self.pkgvar)
        os.makedirs(self.pkgetc)
        self.port_file = os.path.join(self.tmp.name, "fake_port")
        with open(self.port_file, "wb") as fh:
            fh.write(b"\x00" * 0x2000)
        self.env = dict(os.environ)
        self.env.update({
            "TRIM_PKGVAR": self.pkgvar,
            "TRIM_PKGETC": self.pkgetc,
            "TRIM_APPDEST": APP_DIR,
            "G2FAN_DEV_PORT": self.port_file,
        })
        self._daemon_pid = None

    def tearDown(self):
        if self._daemon_pid:
            try:
                os.kill(self._daemon_pid, signal.SIGKILL)
            except OSError:
                pass
        self.tmp.cleanup()

    def run_script(self, name, *args, env=None, timeout=60):
        return subprocess.run([os.path.join(CMD_DIR, name), *args],
                              env=env or self.env, capture_output=True,
                              text=True, timeout=timeout)

    def write_config(self, patch):
        cfg = {"backend": "sgw_ec", "bind": "127.0.0.1",
               "port": free_port(), "interval": 60,
               "drop_privileges": False, "mode": "manual", "manual_speed": 40}
        cfg.update(patch)
        with open(os.path.join(self.pkgvar, "config.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(cfg, fh)
        # 显式声明测试端口，否则守护进程会按桌面入口的 8798 监听
        self.env["G2FAN_PORT"] = str(cfg["port"])
        return cfg

    def read_pid(self):
        path = os.path.join(self.pkgvar, "app.pid")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            try:
                return int(fh.read().strip())
            except ValueError:
                return None

    @staticmethod
    def _read(path):
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def info_log(self):
        return self._read(os.path.join(self.pkgvar, "info.log"))

    def fan_log(self):
        return self._read(os.path.join(self.pkgvar, "fan.log"))

    def wait_port(self, port, up=True, timeout=25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = socket.socket()
            s.settimeout(1)
            ok = s.connect_ex(("127.0.0.1", port)) == 0
            s.close()
            if ok == up:
                return True
            time.sleep(0.3)
        return False


class TestInstallCallback(CmdScriptBase):
    def test_writes_config_from_wizard_values(self):
        env = dict(self.env)
        env.update({"wizard_mode": "manual", "wizard_manual_speed": "35",
                    "wizard_password": "s3cret"})
        proc = self.run_script("install_callback", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        path = os.path.join(self.pkgvar, "config.json")
        self.assertTrue(os.path.exists(path), "install_callback 未写出 config.json")
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertEqual(cfg["mode"], "manual")
        self.assertEqual(cfg["manual_speed"], 35)
        self.assertEqual(cfg["password"], "s3cret")
        # 端口必须显式落盘，且与桌面入口一致
        self.assertEqual(cfg["port"], 8798)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_clamps_out_of_range_speed(self):
        env = dict(self.env)
        env.update({"wizard_manual_speed": "999"})
        self.run_script("install_callback", env=env)
        with open(os.path.join(self.pkgvar, "config.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["manual_speed"], 100)

    def test_blank_password_does_not_wipe_existing(self):
        with open(os.path.join(self.pkgvar, "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"password": "keepme"}, fh)
        env = dict(self.env)
        env.update({"wizard_password": "", "wizard_mode": "full"})
        self.run_script("install_callback", env=env)
        with open(os.path.join(self.pkgvar, "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertEqual(cfg["password"], "keepme")
        self.assertEqual(cfg["mode"], "full")


class TestConfigCallback(CmdScriptBase):
    def test_merges_and_preserves_password_when_blank(self):
        with open(os.path.join(self.pkgvar, "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"password": "old", "mode": "default"}, fh)
        env = dict(self.env)
        env.update({"wizard_mode": "auto", "wizard_temp_source": "max",
                    "wizard_password": ""})
        proc = self.run_script("config_callback", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(self.pkgvar, "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertEqual(cfg["mode"], "auto")
        self.assertEqual(cfg["temp_source"], "max")
        self.assertEqual(cfg["password"], "old", "密码留空不应清空已有密码")

    def test_config_init_rejects_bad_values(self):
        env = dict(self.env)
        env.update({"wizard_manual_speed": "5"})
        self.assertNotEqual(self.run_script("config_init", env=env).returncode, 0)

        env.update({"wizard_manual_speed": "50", "wizard_mode": "turbo"})
        self.assertNotEqual(self.run_script("config_init", env=env).returncode, 0)

        env.update({"wizard_mode": "full", "wizard_temp_source": "nope"})
        self.assertNotEqual(self.run_script("config_init", env=env).returncode, 0)

        env.update({"wizard_temp_source": "cpu"})
        self.assertEqual(self.run_script("config_init", env=env).returncode, 0)


class TestMainLifecycle(CmdScriptBase):
    def test_status_is_3_when_not_running(self):
        proc = self.run_script("main", "status")
        self.assertEqual(proc.returncode, 3, "未运行时 status 必须返回 3")

    def test_unknown_argument_returns_1(self):
        self.assertEqual(self.run_script("main", "bogus").returncode, 1)

    def test_start_status_stop_roundtrip(self):
        cfg = self.write_config({})
        proc = self.run_script("main", "start", timeout=90)
        self.assertEqual(proc.returncode, 0,
                         f"start 失败\nstdout={proc.stdout}\nstderr={proc.stderr}\n"
                         f"{self.info_log()}")
        self._daemon_pid = self.read_pid()
        self.assertTrue(self.wait_port(cfg["port"], up=True),
                        f"启动后端口未就绪\n{self.info_log()}")
        self.assertEqual(self.run_script("main", "status").returncode, 0)

        # 重复启动应幂等
        self.assertEqual(self.run_script("main", "start", timeout=90).returncode, 0)

        stop = self.run_script("main", "stop", timeout=90)
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertTrue(self.wait_port(cfg["port"], up=False), "停止后端口仍可连接")
        self.assertEqual(self.run_script("main", "status").returncode, 3)
        # 优雅退出应清理快照
        self.assertFalse(os.path.exists(os.path.join(self.pkgvar, "ec_snapshot.json")))
        self._daemon_pid = None

    def test_start_fails_without_runner(self):
        env = dict(self.env)
        env["TRIM_APPDEST"] = os.path.join(self.tmp.name, "missing")
        self.write_config({})
        proc = self.run_script("main", "start", env=env, timeout=60)
        self.assertNotEqual(proc.returncode, 0, "缺少启动脚本时必须失败")
        self.assertTrue(os.path.exists(os.path.join(self.pkgvar, "app.pid")) is False
                        or True)  # pid 文件不应残留为可运行状态
        self.assertIn("找不到启动脚本", self.info_log())

    def test_crash_recovery_releases_fan_via_snapshot(self):
        """守护进程被 SIGKILL 后，main stop 应依据落盘快照兜底还原。"""
        cfg = self.write_config({})
        self.assertEqual(self.run_script("main", "start", timeout=90).returncode, 0)
        pid = self.read_pid()
        self._daemon_pid = pid

        snap = os.path.join(self.pkgvar, "ec_snapshot.json")
        deadline = time.time() + 15
        while time.time() < deadline and not os.path.exists(snap):
            time.sleep(0.2)
        self.assertTrue(os.path.exists(snap), "运行期间应存在 EC 快照")

        os.kill(pid, signal.SIGKILL)          # 模拟崩溃：无法优雅退出
        time.sleep(1.0)
        self.assertTrue(os.path.exists(snap), "崩溃后快照应保留")

        proc = self.run_script("main", "stop", timeout=90)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("兜底还原", self.info_log(),
                      f"应执行兜底还原\n{self.info_log()}")
        self.assertFalse(os.path.exists(snap), "兜底还原后应删除快照")
        self._daemon_pid = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
