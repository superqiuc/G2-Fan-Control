#!/usr/bin/env python3
"""硬盘温度识别测试。

真机问题：机器重启后 ``drivetemp`` 内核模块没有自动加载，
``/sys/class/hwmon`` 里就没有硬盘温度节点，界面「硬盘温度」一直显示 ``--``。
本文件覆盖两条修复路径：启动时按需 modprobe，以及传感器懒重扫。
"""

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
from unittest import mock


from g2fan import daemon as dmod  # noqa: E402
from g2fan import sensors as smod  # noqa: E402


class ScriptedReader:
    """按共享脚本队列返回磁盘温度的假 TemperatureReader。

    ``ensure_disk_sensor`` 每次探测都会新建 reader，因此脚本队列必须
    在多次实例化之间共享，而不是每次拷贝一份。
    """

    def __init__(self, queue):
        self._queue = queue
        self._last = None

    def read_role(self, role):
        if role != "disk":
            return None
        if self._queue:
            self._last = self._queue.pop(0)
        return self._last


class TestEnsureDiskSensor(unittest.TestCase):
    def _run(self, values, euid=0, which="/sbin/modprobe", rc=0, stderr=""):
        logs = []
        queue = list(values)          # 跨实例共享的可变队列
        with mock.patch.object(dmod.sensors_mod, "TemperatureReader",
                               side_effect=lambda: ScriptedReader(queue)), \
             mock.patch.object(dmod.os, "geteuid", return_value=euid), \
             mock.patch.object(dmod.shutil, "which", return_value=which), \
             mock.patch.object(dmod.subprocess, "run",
                               return_value=mock.Mock(returncode=rc, stderr=stderr,
                                                      stdout="")), \
             mock.patch.object(dmod.time, "sleep", return_value=None):
            result = dmod.ensure_disk_sensor(lambda lvl, msg: logs.append(msg))
        return result, logs

    def test_no_modprobe_when_disk_temperature_already_available(self):
        result, _ = self._run([35.0])
        self.assertTrue(result["available"])
        self.assertFalse(result["loaded"], "已有温度时不应再加载模块")
        self.assertEqual(result["temperature"], 35.0)

    def test_non_root_cannot_load_module(self):
        result, _ = self._run([None], euid=1000)
        self.assertFalse(result["available"])
        self.assertIn("非 root", result["reason"])

    def test_missing_modprobe_reported(self):
        result, _ = self._run([None], which=None)
        self.assertFalse(result["available"])
        self.assertIn("modprobe", result["reason"])

    def test_modprobe_failure_reason_surfaces_stderr(self):
        result, _ = self._run([None], rc=1, stderr="FATAL: Module drivetemp not found")
        self.assertFalse(result["available"])
        self.assertIn("drivetemp not found", result["reason"])

    def test_modprobe_then_temperature_appears(self):
        # 第一次探测前调用 read_role 返回 None，模块加载后返回 36.0
        result, _ = self._run([None, 36.0])
        self.assertTrue(result["available"])
        self.assertTrue(result["loaded"])
        self.assertEqual(result["temperature"], 36.0)

    def test_modprobe_loaded_but_still_no_temperature(self):
        # 模块加载后一直读不到（磁盘休眠 / 不支持 SMART 温度）
        result, _ = self._run([None] + [None] * 30)
        self.assertTrue(result["loaded"])
        self.assertFalse(result["available"])
        self.assertIn("休眠", result["reason"])


class TestLazyRediscovery(unittest.TestCase):
    def test_refresh_is_throttled(self):
        reader = smod.TemperatureReader(sensors=[])
        with mock.patch.object(smod, "discover", return_value=[]) as disc:
            reader.read_role("disk")
            reader.read_role("disk")
            # 距上次刷新不足 REFRESH_INTERVAL，不应重复扫描
            self.assertEqual(disc.call_count, 0, "刚构造过传感器表，不应立刻重扫")
            reader._last_refresh -= smod.TemperatureReader.REFRESH_INTERVAL + 1
            reader.read_role("disk")
            self.assertEqual(disc.call_count, 1)

    def test_disk_sensor_picked_up_without_restart(self):
        """模拟运行期才 modprobe：重扫后应立刻拿到硬盘温度。"""
        with tempfile.TemporaryDirectory() as tmp:
            # 构造一个最小可读的 hwmon 目录结构
            hw = os.path.join(tmp, "hwmon9")
            os.makedirs(hw)
            with open(os.path.join(hw, "name"), "w") as fh:
                fh.write("drivetemp\n")
            with open(os.path.join(hw, "temp1_input"), "w") as fh:
                fh.write("41000\n")

            reader = smod.TemperatureReader(sensors=[])
            self.assertIsNone(reader.read_role("disk"))

            with mock.patch.object(smod, "HWMON_ROOT", tmp), \
                 mock.patch.object(smod, "THERMAL_ROOT", os.path.join(tmp, "none")):
                reader._last_refresh -= smod.TemperatureReader.REFRESH_INTERVAL + 1
                value = reader.read_role("disk")
                self.assertEqual(value, 41.0, "重扫后应立即读到硬盘温度")


class TestSensorClassification(unittest.TestCase):
    def test_drivetemp_maps_to_disk_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            hw = os.path.join(tmp, "hwmon0")
            os.makedirs(hw)
            with open(os.path.join(hw, "name"), "w") as fh:
                fh.write("drivetemp\n")
            with open(os.path.join(hw, "temp1_input"), "w") as fh:
                fh.write("37000\n")
            with mock.patch.object(smod, "HWMON_ROOT", tmp), \
                 mock.patch.object(smod, "THERMAL_ROOT", os.path.join(tmp, "none")):
                grouped = smod.discover_roles()
        self.assertEqual(len(grouped["disk"]), 1)
        self.assertEqual(grouped["disk"][0].driver, "drivetemp")

    def test_zero_and_negative_readings_are_rejected(self):
        """休眠或异常时驱动可能给出 0 / 负值，不能当成有效温度。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "temp1_input")
            cases = [("0\n", None), ("-1000\n", None),
                     ("300\n", 0.3),            # 非零，按有效处理
                     ("36500\n", 36.5)]
            for raw, expected in cases:
                with open(path, "w") as fh:
                    fh.write(raw)
                sensor = smod.Sensor("t", "disk", "t", path, "drivetemp", 100)
                got = sensor.read()
                if expected is None:
                    self.assertIsNone(got, f"读数 {raw.strip()} 应被判为无效")
                else:
                    self.assertEqual(got, expected, f"读数 {raw.strip()}")

    def test_implausibly_hot_readings_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "temp1_input")
            with open(path, "w") as fh:
                fh.write("999000\n")            # 999°C，明显异常
            sensor = smod.Sensor("t", "disk", "t", path, "drivetemp", 100)
            self.assertIsNone(sensor.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
