#!/usr/bin/env python3
"""温控核心测试：曲线插值、模式、钳制与安全降级。"""

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


from g2fan import config as cfg  # noqa: E402
from g2fan import control as ctl  # noqa: E402


class FakeBackend:
    is_open = True

    def __init__(self, fail=False):
        self.speed = None
        self.duties = [0, 0]
        self.taken = False
        self.released = False
        self.fail = fail
        self.write_count = 0

    def open(self):
        self.is_open = True
        return self

    def close(self):
        pass

    def take_manual_control(self):
        self.taken = True
        return {}

    def release(self, fallback_speed_pct=100):
        self.released = True
        return "fake released"

    def set_speed(self, pct):
        if self.fail:
            raise OSError("simulated write failure")
        self.speed = float(pct)
        self.write_count += 1
        self.duties = [int(pct / 100.0 * 184)] * 2
        return self.duties[0]

    def read_duties(self):
        return list(self.duties)

    def describe(self):
        return {"backend": "fake"}


class FakeReader:
    def __init__(self, cpu=None, disk=None, board=None):
        self.values = {"cpu": cpu, "disk": disk, "board": board}

    def read_all(self):
        return dict(self.values)

    def read_source(self, source):
        if source == "cpu":
            return self.values.get("cpu")
        if source == "disk":
            return self.values.get("disk")
        vals = [v for v in self.values.values() if v is not None]
        return max(vals) if vals else None

    def read_detail(self):
        return []


def make(store_patch=None, reader=None, backend=None):
    tmp = tempfile.mkdtemp()
    store = cfg.ConfigStore(os.path.join(tmp, "config.json"))
    if store_patch:
        store.update(store_patch)
    be = backend or FakeBackend()
    rd = reader or FakeReader(cpu=45.0)
    return ctl.FanController(be, store, rd), store, be, rd


class TestInterpolation(unittest.TestCase):
    def setUp(self):
        self.curve = [{"temp": 40, "speed": 20}, {"temp": 60, "speed": 60},
                      {"temp": 80, "speed": 100}]

    def test_below_first_and_above_last(self):
        self.assertEqual(ctl.interpolate(self.curve, 10), 20)
        self.assertEqual(ctl.interpolate(self.curve, 200), 100)

    def test_exact_nodes(self):
        self.assertEqual(ctl.interpolate(self.curve, 60), 60)

    def test_midpoint_linear(self):
        self.assertAlmostEqual(ctl.interpolate(self.curve, 50), 40.0, places=3)

    def test_flat_segment_does_not_divide_by_zero(self):
        flat = [{"temp": 50, "speed": 30}, {"temp": 50, "speed": 80}]
        self.assertIn(ctl.interpolate(flat, 50), (30, 80))

    def test_empty_curve_returns_hard_floor(self):
        self.assertEqual(ctl.interpolate([], 50), ctl.HARD_FLOOR)


class TestGenerateCurve(unittest.TestCase):
    def test_shape_and_bounds(self):
        curve = ctl.generate_curve(points=6, temp_min=35, temp_max=85)
        self.assertEqual(len(curve), 6)
        self.assertEqual(curve[0]["temp"], 35)
        self.assertEqual(curve[-1]["temp"], 85)
        self.assertEqual(curve[0]["speed"], int(ctl.HARD_FLOOR))
        self.assertEqual(curve[-1]["speed"], 100)
        speeds = [p["speed"] for p in curve]
        self.assertEqual(speeds, sorted(speeds))

    def test_point_count_clamped(self):
        self.assertGreaterEqual(len(ctl.generate_curve(points=1)), cfg.CURVE_MIN_POINTS)
        self.assertLessEqual(len(ctl.generate_curve(points=99)), cfg.CURVE_MAX_POINTS)


class TestControllerModes(unittest.TestCase):
    def test_default_mode_uses_builtin_curve(self):
        controller, _, backend, _ = make({"mode": "default"}, FakeReader(cpu=55.0))
        controller._cycle()
        expected = ctl.interpolate(cfg.DEFAULT_CURVE, 55.0)
        self.assertAlmostEqual(backend.speed, expected, places=1)

    def test_manual_mode_uses_fixed_speed(self):
        controller, _, backend, _ = make({"mode": "manual", "manual_speed": 42})
        controller._cycle()
        self.assertEqual(backend.speed, 42.0)

    def test_full_mode_is_100(self):
        controller, _, backend, _ = make({"mode": "full"})
        controller._cycle()
        self.assertEqual(backend.speed, 100.0)

    def test_auto_mode_uses_user_curve(self):
        curve = [{"temp": 30, "speed": 25}, {"temp": 90, "speed": 95}]
        controller, _, backend, _ = make({"mode": "auto", "curve": curve,
                                          "hysteresis": 0},
                                         FakeReader(cpu=60.0))
        controller._cycle()
        self.assertAlmostEqual(backend.speed, ctl.interpolate(curve, 60.0), places=1)


class TestSafety(unittest.TestCase):
    def test_never_below_hard_floor(self):
        controller, _, backend, _ = make({"mode": "manual", "manual_speed": 0,
                                          "min_speed": 0, "hysteresis": 0},
                                         FakeReader(cpu=20.0))
        controller._cycle()
        self.assertGreaterEqual(backend.speed, ctl.HARD_FLOOR)

    def test_temp_failure_forces_full_after_three(self):
        controller, _, backend, _ = make({"mode": "manual", "manual_speed": 20,
                                          "hysteresis": 0},
                                         FakeReader(cpu=None))
        for _ in range(2):
            controller._cycle()
        self.assertLess(backend.speed, 100.0)   # 前两次仍按兜底策略
        controller._cycle()                     # 第三次
        self.assertEqual(backend.speed, 100.0)

    def test_persistent_temp_failure_degrades_mode(self):
        controller, store, _, _ = make({"mode": "manual", "manual_speed": 20},
                                       FakeReader(cpu=None))
        for _ in range(int(ctl.TEMP_FAIL_DEGRADE)):
            controller._cycle()
        self.assertEqual(store.get()["mode"], "default")
        self.assertTrue(controller.status()["degraded"])

    def test_write_failure_marks_degraded(self):
        controller, _, backend, _ = make({"mode": "manual", "manual_speed": 50},
                                         FakeReader(cpu=50.0),
                                         FakeBackend(fail=True))
        for _ in range(int(ctl.WRITE_FAIL_DEGRADE)):
            controller._cycle()
        self.assertTrue(controller.status()["degraded"])

    def test_hysteresis_suppresses_small_change_both_directions(self):
        reader = FakeReader(cpu=50.0)
        controller, store, backend, _ = make({"mode": "manual", "manual_speed": 50,
                                              "hysteresis": 5}, reader)
        controller._cycle()
        first = backend.speed

        store.update({"manual_speed": 52})      # 上升 2 < 5 -> 抑制
        controller._cycle()
        self.assertEqual(backend.speed, first)

        store.update({"manual_speed": 47})      # 下降 3 < 5 -> 抑制
        controller._cycle()
        self.assertEqual(backend.speed, first)

        store.update({"manual_speed": 60})      # 上升 10 >= 5 -> 生效
        controller._cycle()
        self.assertEqual(backend.speed, 60.0)

    def test_critical_temperature_bypasses_hysteresis(self):
        curve = [{"temp": 30, "speed": 50}, {"temp": 90, "speed": 55}]
        controller, _, backend, _ = make(
            {"mode": "auto", "curve": curve, "hysteresis": 20},
            FakeReader(cpu=88.0))
        controller._cycle()                     # 首次建立基准
        baseline = backend.speed
        controller._cycle()                     # 88°C >= CRITICAL_TEMP，应绕过死区
        self.assertGreaterEqual(backend.speed, baseline)
        self.assertAlmostEqual(backend.speed, 54.0, delta=2.0)

    def test_temp_recovery_clears_fail_counter(self):
        reader = FakeReader(cpu=None)
        controller, _, backend, _ = make({"mode": "manual", "manual_speed": 30,
                                          "hysteresis": 0}, reader)
        controller._cycle()
        controller._cycle()
        reader.values["cpu"] = 45.0
        controller._cycle()
        self.assertEqual(controller.status()["temp_fail_count"], 0)


class TestLifecycle(unittest.TestCase):
    def test_start_takes_control_and_stop_releases(self):
        controller, _, backend, _ = make({"mode": "manual", "manual_speed": 40})
        controller.start()
        try:
            self.assertTrue(backend.taken)
        finally:
            controller.stop()
        self.assertTrue(backend.released)
        self.assertFalse(controller.status()["running"])

    def test_stop_without_start_still_releases(self):
        controller, _, backend, _ = make()
        controller.stop()
        self.assertTrue(backend.released)


if __name__ == "__main__":
    unittest.main(verbosity=2)
