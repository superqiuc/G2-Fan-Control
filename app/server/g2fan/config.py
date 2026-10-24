"""配置加载、校验与持久化。

配置文件为 JSON，存放在 ``$TRIM_PKGVAR/config.json``（重启后保留）。
所有字段都经过严格校验与钳制，损坏的配置会安全回退到默认值，
绝不让非法值（例如 0% 转速或巨大端口号）流到硬件层。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading

CONFIG_VERSION = 2

MODES = ("default", "auto", "manual", "full")
TEMP_SOURCES = ("cpu", "disk", "max")

# 保守的出厂曲线：低温安静，高温积极散热
DEFAULT_CURVE: list[dict] = [
    {"temp": 35, "speed": 20},
    {"temp": 45, "speed": 25},
    {"temp": 55, "speed": 40},
    {"temp": 65, "speed": 60},
    {"temp": 75, "speed": 85},
    {"temp": 85, "speed": 100},
]

DEFAULTS: dict = {
    "version": CONFIG_VERSION,
    "mode": "default",
    "temp_source": "cpu",
    "manual_speed": 50,
    "min_speed": 20,
    "max_speed": 100,
    "interval": 5,
    "hysteresis": 1.5,
    "release_speed": 100,
    "curve": DEFAULT_CURVE,
    "password": "",
    "bind": "0.0.0.0",
    "port": 8798,
    "backend": "auto",          # auto | sgw_ec | hwmon
    "log_keep": 400,
    "drop_privileges": True,
}

CURVE_MIN_POINTS = 2
CURVE_MAX_POINTS = 10


def _clamp(value, low, high, fallback):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return fallback
    if num != num:  # NaN
        return fallback
    if num < low:
        return low
    if num > high:
        return high
    return num


def _as_int(value, low, high, fallback) -> int:
    return int(_clamp(value, low, high, fallback))


def sanitize_curve(raw) -> list[dict]:
    """校验并规范化温控曲线。"""
    if not isinstance(raw, list):
        return [dict(p) for p in DEFAULT_CURVE]
    points: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        temp = _clamp(item.get("temp"), 0, 120, None)
        speed = _clamp(item.get("speed"), 0, 100, None)
        if temp is None or speed is None:
            continue
        points.append({"temp": int(round(temp)), "speed": int(round(speed))})
    points.sort(key=lambda p: p["temp"])
    # 去掉重复温度点
    deduped: list[dict] = []
    for point in points:
        if deduped and deduped[-1]["temp"] == point["temp"]:
            deduped[-1] = point
        else:
            deduped.append(point)
    if len(deduped) < CURVE_MIN_POINTS:
        return [dict(p) for p in DEFAULT_CURVE]
    if len(deduped) > CURVE_MAX_POINTS:
        deduped = deduped[:CURVE_MAX_POINTS]
    return deduped


def sanitize(raw) -> dict:
    """把任意输入整理成合法配置。"""
    cfg = dict(DEFAULTS)
    if not isinstance(raw, dict):
        cfg["curve"] = [dict(p) for p in DEFAULT_CURVE]
        return cfg

    # --- v1 -> v2 迁移：早期版本没有 curve/temp_source ---
    if "curve" not in raw and "temp_curve" in raw:
        raw = dict(raw)
        raw["curve"] = raw.get("temp_curve")

    mode = raw.get("mode")
    cfg["mode"] = mode if mode in MODES else DEFAULTS["mode"]

    source = raw.get("temp_source")
    cfg["temp_source"] = source if source in TEMP_SOURCES else DEFAULTS["temp_source"]

    cfg["manual_speed"] = _as_int(raw.get("manual_speed", DEFAULTS["manual_speed"]), 0, 100,
                                  DEFAULTS["manual_speed"])
    cfg["min_speed"] = _as_int(raw.get("min_speed", DEFAULTS["min_speed"]), 0, 100,
                               DEFAULTS["min_speed"])
    cfg["max_speed"] = _as_int(raw.get("max_speed", DEFAULTS["max_speed"]), 0, 100,
                               DEFAULTS["max_speed"])
    if cfg["max_speed"] < cfg["min_speed"]:
        cfg["max_speed"] = cfg["min_speed"]

    cfg["interval"] = _as_int(raw.get("interval", DEFAULTS["interval"]), 1, 300,
                              DEFAULTS["interval"])
    cfg["hysteresis"] = round(_clamp(raw.get("hysteresis", DEFAULTS["hysteresis"]), 0, 50,
                                     DEFAULTS["hysteresis"]), 2)
    cfg["release_speed"] = _as_int(raw.get("release_speed", DEFAULTS["release_speed"]), 0, 100,
                                   DEFAULTS["release_speed"])

    cfg["curve"] = sanitize_curve(raw.get("curve"))

    password = raw.get("password", "")
    cfg["password"] = password if isinstance(password, str) else ""

    bind = raw.get("bind", DEFAULTS["bind"])
    cfg["bind"] = bind if isinstance(bind, str) and bind.strip() else DEFAULTS["bind"]

    cfg["port"] = _as_int(raw.get("port", DEFAULTS["port"]), 1, 65535, DEFAULTS["port"])

    backend = raw.get("backend", DEFAULTS["backend"])
    cfg["backend"] = backend if backend in ("auto", "sgw_ec", "hwmon") else "auto"

    cfg["log_keep"] = _as_int(raw.get("log_keep", DEFAULTS["log_keep"]), 50, 5000,
                              DEFAULTS["log_keep"])
    cfg["drop_privileges"] = bool(raw.get("drop_privileges", DEFAULTS["drop_privileges"]))
    cfg["version"] = CONFIG_VERSION
    return cfg


class ConfigStore:
    """线程安全的配置存储。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data = dict(DEFAULTS)
        self._data["curve"] = [dict(p) for p in DEFAULT_CURVE]
        self.load()

    def load(self) -> dict:
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                self._data = sanitize(raw)
            except FileNotFoundError:
                self._data = sanitize(None)
            except (OSError, json.JSONDecodeError):
                # 配置损坏：回退默认值，但保留原文件以便排查
                self._data = sanitize(None)
            return dict(self._data)

    def save(self, data: dict | None = None) -> dict:
        """原子写入，避免断电/崩溃留下半个文件。"""
        with self._lock:
            if data is not None:
                self._data = sanitize(data)
            payload = dict(self._data)
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix=".config-", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2, ensure_ascii=False)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, self.path)
            except OSError:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            return payload

    def get(self) -> dict:
        with self._lock:
            return dict(self._data)

    def update(self, patch: dict) -> dict:
        with self._lock:
            merged = dict(self._data)
            merged.update(patch or {})
            return self.save(merged)

    def set_value(self, key: str, value) -> dict:
        return self.update({key: value})
