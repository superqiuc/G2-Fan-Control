#!/usr/bin/env python3
"""配置模块测试：默认值、校验与钳制、曲线规范化、损坏恢复。"""

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


from g2fan import config as cfg  # noqa: E402


class TestSanitize(unittest.TestCase):
    def test_none_yields_defaults(self):
        out = cfg.sanitize(None)
        self.assertEqual(out["mode"], "default")
        self.assertEqual(out["port"], 8798)
        self.assertEqual(len(out["curve"]), len(cfg.DEFAULT_CURVE))

    def test_clamps_out_of_range(self):
        out = cfg.sanitize({
            "manual_speed": 999, "min_speed": -50, "max_speed": 1e9,
            "interval": 0, "port": 999999, "hysteresis": 1e6,
        })
        self.assertEqual(out["manual_speed"], 100)
        self.assertEqual(out["min_speed"], 0)
        self.assertEqual(out["max_speed"], 100)
        self.assertEqual(out["interval"], 1)
        self.assertEqual(out["port"], 65535)
        self.assertEqual(out["hysteresis"], 50)

    def test_max_below_min_is_repaired(self):
        out = cfg.sanitize({"min_speed": 80, "max_speed": 20})
        self.assertGreaterEqual(out["max_speed"], out["min_speed"])

    def test_invalid_mode_and_source_fall_back(self):
        out = cfg.sanitize({"mode": "turbo", "temp_source": "gpu"})
        self.assertEqual(out["mode"], "default")
        self.assertEqual(out["temp_source"], "cpu")

    def test_non_numeric_values_fall_back(self):
        out = cfg.sanitize({"manual_speed": "abc", "interval": None,
                            "hysteresis": float("nan")})
        self.assertEqual(out["manual_speed"], cfg.DEFAULTS["manual_speed"])
        self.assertEqual(out["interval"], cfg.DEFAULTS["interval"])
        self.assertEqual(out["hysteresis"], cfg.DEFAULTS["hysteresis"])

    def test_v1_migration(self):
        out = cfg.sanitize({"temp_curve": [{"temp": 40, "speed": 30},
                                           {"temp": 80, "speed": 100}]})
        self.assertEqual(len(out["curve"]), 2)
        self.assertEqual(out["curve"][0]["speed"], 30)


class TestCurve(unittest.TestCase):
    def test_curve_sorted_and_deduped(self):
        out = cfg.sanitize_curve([
            {"temp": 70, "speed": 60},
            {"temp": 40, "speed": 20},
            {"temp": 40, "speed": 25},
        ])
        self.assertEqual([p["temp"] for p in out], [40, 70])
        self.assertEqual(out[0]["speed"], 25)

    def test_too_few_points_falls_back_to_default(self):
        out = cfg.sanitize_curve([{"temp": 50, "speed": 50}])
        self.assertEqual(len(out), len(cfg.DEFAULT_CURVE))

    def test_garbage_points_dropped(self):
        out = cfg.sanitize_curve([
            {"temp": 40, "speed": 20}, "junk", {"nope": 1}, {"temp": 80, "speed": 100},
        ])
        self.assertEqual(len(out), 2)

    def test_curve_capped_at_ten_points(self):
        raw = [{"temp": 30 + i, "speed": i * 5} for i in range(20)]
        out = cfg.sanitize_curve(raw)
        self.assertLessEqual(len(out), cfg.CURVE_MAX_POINTS)


class TestStore(unittest.TestCase):
    def test_roundtrip_and_atomic_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            store = cfg.ConfigStore(path)
            store.update({"mode": "manual", "manual_speed": 42})
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["manual_speed"], 42)
            reloaded = cfg.ConfigStore(path)
            self.assertEqual(reloaded.get()["mode"], "manual")
            self.assertEqual(reloaded.get()["manual_speed"], 42)

    def test_corrupt_file_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json at all")
            store = cfg.ConfigStore(path)
            self.assertEqual(store.get()["mode"], "default")

    def test_update_ignores_unknown_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = cfg.ConfigStore(os.path.join(tmp, "c.json"))
            out = store.update({"evil": "payload", "mode": "full"})
            self.assertNotIn("evil", out)
            self.assertEqual(out["mode"], "full")


if __name__ == "__main__":
    unittest.main(verbosity=2)
