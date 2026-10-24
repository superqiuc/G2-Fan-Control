#!/usr/bin/env python3
"""打包一致性测试 —— 防止「服务监听端口 ≠ 桌面入口端口」这类问题。

真机首次安装时曾出现：守护进程按 config.DEFAULTS 监听 8898，
而 manifest / app/ui/config 声明的是 8798，导致桌面图标指向空端口。
本文件把这些跨文件约定固化成断言。
"""

import json
import os
import subprocess
import sys
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
SERVER = os.path.join(PROJECT, "app", "server")

sys.path.insert(0, SERVER)
from g2fan import config as cfg  # noqa: E402

REQUIRED_CMD_SCRIPTS = [
    "main", "install_init", "install_callback",
    "upgrade_init", "upgrade_callback",
    "uninstall_init", "uninstall_callback",
    "config_init", "config_callback",
]


def read_manifest(path=None):
    """manifest 是 key = value 文本，不是 JSON。"""
    out = {}
    with open(path or os.path.join(PROJECT, "manifest"), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


class TestManifest(unittest.TestCase):
    def setUp(self):
        self.m = read_manifest()

    def test_required_fields_present(self):
        for key in ("appname", "version", "display_name", "desc", "platform",
                    "source", "maintainer", "maintainer_url", "desktop_uidir",
                    "desktop_applaunchname"):
            self.assertIn(key, self.m, f"manifest 缺少字段 {key}")
            self.assertTrue(self.m[key], f"manifest 字段 {key} 为空")

    def test_appname_and_source(self):
        self.assertEqual(self.m["appname"], "g2fan")
        self.assertEqual(self.m["source"], "thirdparty")

    def test_platform_is_x86(self):
        # 无架构相关二进制也仍依赖 x86 的 /dev/port 端口布局，故不能声明 all
        self.assertEqual(self.m["platform"], "x86")

    def test_version_is_semver_like(self):
        parts = self.m["version"].split(".")
        self.assertGreaterEqual(len(parts), 2)
        for part in parts[:3]:
            self.assertTrue(part.split("-")[0].isdigit(), self.m["version"])


class TestPortConsistency(unittest.TestCase):
    """守护进程默认端口必须与桌面入口、manifest 声明一致。"""

    def test_default_port_agrees_across_files(self):
        default_port = cfg.DEFAULTS["port"]
        manifest = read_manifest()
        with open(os.path.join(PROJECT, "app", "ui", "config"), encoding="utf-8") as fh:
            ui = json.load(fh)
        entries = ui[".url"]
        entry = entries[manifest["desktop_applaunchname"]]
        ui_port = int(entry["port"])

        self.assertEqual(default_port, ui_port,
                         f"config.DEFAULTS['port']={default_port} 与 "
                         f"app/ui/config 的 {ui_port} 不一致，桌面图标会指向空端口")
        self.assertEqual(int(manifest["service_port"]), ui_port,
                         "manifest.service_port 与 app/ui/config 的端口不一致")

    def test_install_callback_pins_same_port(self):
        """安装脚本必须显式写入端口，不能依赖两边各自的默认值。"""
        with open(os.path.join(PROJECT, "cmd", "install_callback"),
                  encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn(f'cfg.setdefault("port", {cfg.DEFAULTS["port"]})', body)

    def test_cmd_main_default_port_matches(self):
        with open(os.path.join(PROJECT, "cmd", "main"), encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn(f"DEFAULT_PORT={cfg.DEFAULTS['port']}", body)


class TestUiEntry(unittest.TestCase):
    def setUp(self):
        self.manifest = read_manifest()
        with open(os.path.join(PROJECT, "app", "ui", "config"), encoding="utf-8") as fh:
            self.ui = json.load(fh)

    def test_applaunchname_resolves_to_entry(self):
        name = self.manifest["desktop_applaunchname"]
        self.assertIn(name, self.ui[".url"],
                      f"desktop_applaunchname={name} 在 app/ui/config 中没有对应入口")
        self.assertTrue(name.startswith(self.manifest["appname"] + "."),
                        "入口 ID 应以 appname 为前缀")

    def test_entry_targets_root_path(self):
        entry = self.ui[".url"][self.manifest["desktop_applaunchname"]]
        self.assertEqual(entry["url"], "/")
        self.assertEqual(entry["protocol"], "http")
        self.assertEqual(entry["type"], "url")

    def test_icons_referenced_and_present(self):
        entry = self.ui[".url"][self.manifest["desktop_applaunchname"]]
        pattern = entry["icon"]                       # images/icon_{0}.png
        ui_dir = os.path.join(PROJECT, "app", self.manifest["desktop_uidir"])
        for size in ("64", "256"):
            rel = pattern.replace("{0}", size)
            path = os.path.join(ui_dir, rel)
            self.assertTrue(os.path.isfile(path), f"缺少入口图标 {rel}")
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(8), b"\x89PNG\r\n\x1a\n", f"{rel} 不是 PNG")


class TestPackageFiles(unittest.TestCase):
    def test_root_icons_are_png_with_expected_size(self):
        import struct
        for name, expect in (("ICON.PNG", 64), ("ICON_256.PNG", 256)):
            path = os.path.join(PROJECT, name)
            self.assertTrue(os.path.isfile(path), f"缺少 {name}")
            with open(path, "rb") as fh:
                head = fh.read(24)
            self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n", f"{name} 不是 PNG")
            width, height = struct.unpack(">II", head[16:24])
            self.assertEqual((width, height), (expect, expect),
                             f"{name} 尺寸应为 {expect}x{expect}")

    def test_cmd_scripts_present_and_executable(self):
        cmd_dir = os.path.join(PROJECT, "cmd")
        for name in REQUIRED_CMD_SCRIPTS:
            path = os.path.join(cmd_dir, name)
            self.assertTrue(os.path.isfile(path), f"缺少生命周期脚本 cmd/{name}")
            self.assertTrue(os.access(path, os.X_OK), f"cmd/{name} 不可执行")

    def test_cmd_scripts_pass_bash_syntax_check(self):
        cmd_dir = os.path.join(PROJECT, "cmd")
        for name in REQUIRED_CMD_SCRIPTS:
            proc = subprocess.run(["bash", "-n", os.path.join(cmd_dir, name)],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0,
                             f"cmd/{name} 语法错误: {proc.stderr}")

    def test_privilege_and_resource_valid(self):
        for name in ("privilege", "resource"):
            path = os.path.join(PROJECT, "config", name)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertIsInstance(data, dict, f"config/{name} 应为 JSON 对象")
        with open(os.path.join(PROJECT, "config", "privilege"), encoding="utf-8") as fh:
            privilege = json.load(fh)
        self.assertIn(privilege["defaults"]["run-as"], ("root", "package"))

    def test_wizard_files_are_valid_json_arrays(self):
        wizard_dir = os.path.join(PROJECT, "wizard")
        for name in ("install", "config"):
            path = os.path.join(wizard_dir, name)
            self.assertTrue(os.path.isfile(path), f"缺少向导文件 wizard/{name}")
            with open(path, encoding="utf-8") as fh:
                steps = json.load(fh)
            self.assertIsInstance(steps, list)
            for step in steps:
                self.assertIn("items", step)
                for item in step["items"]:
                    self.assertIn(item.get("type"), (
                        "text", "password", "radio", "checkbox", "select",
                        "switch", "tips"))
                    if item["type"] != "tips":
                        self.assertTrue(item.get("field", "").startswith("wizard_"),
                                        f"向导字段 {item.get('field')} 应以 wizard_ 前缀命名")

    def test_install_wizard_fields_match_script_readers(self):
        """安装脚本读取的字段必须在向导里真的存在，否则静默失效。"""
        with open(os.path.join(PROJECT, "wizard", "install"), encoding="utf-8") as fh:
            steps = json.load(fh)
        fields = {item["field"] for step in steps for item in step["items"]
                  if item.get("field")}
        with open(os.path.join(PROJECT, "cmd", "install_callback"),
                  encoding="utf-8") as fh:
            body = fh.read()
        for field in fields:
            self.assertIn(field, body,
                          f"cmd/install_callback 没有读取向导字段 {field}")

    def test_app_payload_present(self):
        for rel in ("app/server/g2fan-run.py", "app/server/g2fan/ec.py",
                    "app/server/g2fan/daemon.py", "app/www/index.html"):
            self.assertTrue(os.path.isfile(os.path.join(PROJECT, rel)),
                            f"缺少 {rel}")


class TestEntryPortIsAuthoritative(unittest.TestCase):
    """桌面入口声明的端口必须是权威来源。

    真机上出现过：旧版把自带默认端口写进 config.json，升级后新版照着配置
    监听旧端口，而图标仍按入口端口打开 -> 图标指向空端口。
    """

    def setUp(self):
        from g2fan import daemon
        self.daemon = daemon

    def test_reads_port_from_real_project(self):
        self.assertEqual(self.daemon.desktop_entry_port(os.path.join(PROJECT, "app")),
                         8798)

    def test_missing_ui_config_returns_none(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(self.daemon.desktop_entry_port(tmp))

    def test_skips_unexpanded_wizard_placeholder(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ui = os.path.join(tmp, "ui")
            os.makedirs(ui)
            with open(os.path.join(ui, "config"), "w", encoding="utf-8") as fh:
                json.dump({".url": {"a.main": {"port": "${wizard_port}"}}}, fh)
            self.assertIsNone(self.daemon.desktop_entry_port(tmp))

    def test_reconcile_port_overrides_stale_config(self):
        import tempfile
        from g2fan import config as cfgmod
        app = os.path.join(PROJECT, "app")
        with tempfile.TemporaryDirectory() as tmp:
            store = cfgmod.ConfigStore(os.path.join(tmp, "config.json"))
            store.update({"port": 8898})           # 模拟被旧版污染的配置
            logged = []
            result = self.daemon.reconcile_port(store, app, lambda lvl, msg: logged.append(msg))
            self.assertEqual(result["port"], 8798, "应以桌面入口端口为准")
            self.assertEqual(store.get()["port"], 8798, "应把修正结果回写配置")
            self.assertTrue(any("不一致" in m for m in logged), "应记录一条告警")

    def test_reconcile_port_is_noop_when_already_consistent(self):
        import tempfile
        from g2fan import config as cfgmod
        app = os.path.join(PROJECT, "app")
        with tempfile.TemporaryDirectory() as tmp:
            store = cfgmod.ConfigStore(os.path.join(tmp, "config.json"))
            store.update({"port": 8798})
            logged = []
            result = self.daemon.reconcile_port(store, app, lambda lvl, msg: logged.append(msg))
            self.assertEqual(result["port"], 8798)
            self.assertEqual(logged, [], "一致时不应产生告警")


if __name__ == "__main__":
    unittest.main(verbosity=2)
