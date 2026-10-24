"""风扇温控核心。

职责：

* 控制循环 —— 按周期读取温度、计算目标转速、写入硬件。
* 四种模式：``default``（内置保守曲线）、``auto``（用户曲线）、
  ``manual``（固定转速）、``full``（全速）。
* 多层安全防护，保证「绝不让风扇停转、绝不让散热失控」。

安全设计
--------
====================  ==================================  ==========================
机制                  触发条件                            动作
====================  ==================================  ==========================
硬下限               始终生效                            转速绝不低于 HARD_FLOOR
温度读取失败          连续 3 次                           立即 100%
温度持续失败          连续 5 次                           降级到 default 模式
PWM/EC 写入失败       连续 3 次                           标记 degraded 并记录
看门狗                控制循环心跳超时                     独立线程直接写 100%
退出释放              服务停止/卸载                       还原接管前 EC 状态
====================  ==================================  ==========================
"""

from __future__ import annotations

import json
import os
import threading
import time

from . import config as config_mod
from . import sensors as sensors_mod
from .ec import DUTY_MAX, ECError, duty_to_speed, speed_to_duty

# 绝对硬下限：任何模式、任何配置都不会低于该转速，防止风扇停转后无法重启
HARD_FLOOR = 10.0

# 达到该温度时忽略滞回死区，立刻执行曲线目标值
CRITICAL_TEMP = 85.0

TEMP_FAIL_FORCE_FULL = 3
TEMP_FAIL_DEGRADE = 5
WRITE_FAIL_DEGRADE = 3
WATCHDOG_FACTOR = 3
WATCHDOG_MIN_TIMEOUT = 15.0


def interpolate(curve: list[dict], temp: float) -> float:
    """在温控曲线上做线性插值。"""
    if not curve:
        return HARD_FLOOR
    if temp <= curve[0]["temp"]:
        return float(curve[0]["speed"])
    if temp >= curve[-1]["temp"]:
        return float(curve[-1]["speed"])
    for left, right in zip(curve, curve[1:]):
        if left["temp"] <= temp <= right["temp"]:
            span = right["temp"] - left["temp"]
            if span <= 0:
                return float(right["speed"])
            ratio = (temp - left["temp"]) / span
            return left["speed"] + ratio * (right["speed"] - left["speed"])
    return float(curve[-1]["speed"])


def generate_curve(points: int = 6, temp_min: int = 35, temp_max: int = 85) -> list[dict]:
    """自动生成一条「低温平缓、高温陡峭」的曲线。"""
    points = max(config_mod.CURVE_MIN_POINTS, min(int(points), config_mod.CURVE_MAX_POINTS))
    temp_min = max(0, min(int(temp_min), 110))
    temp_max = max(temp_min + 5, min(int(temp_max), 120))
    curve: list[dict] = []
    for i in range(points):
        ratio = i / (points - 1)
        temp = round(temp_min + ratio * (temp_max - temp_min))
        # 二次曲线：低温段缓慢上升，高温段快速拉满
        speed = HARD_FLOOR + (100.0 - HARD_FLOOR) * (ratio ** 1.8)
        curve.append({"temp": int(temp), "speed": int(round(speed))})
    curve[0]["speed"] = int(HARD_FLOOR)
    curve[-1]["speed"] = 100
    return curve


class FanController:
    """把温度映射成转速并驱动硬件后端。"""

    def __init__(self, backend, store: config_mod.ConfigStore,
                 reader: sensors_mod.TemperatureReader | None = None,
                 log_sink=None, snapshot_path: str | None = None):
        self.backend = backend
        self.store = store
        self.reader = reader or sensors_mod.TemperatureReader()
        self.log_sink = log_sink
        self.snapshot_path = snapshot_path

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None

        self._running = False
        self._started_at: float | None = None
        self._last_cycle_ts: float | None = None
        self._last_error: str | None = None

        self._current_speed: float | None = None      # 实际写入的百分比
        self._last_temp: float | None = None
        self._last_duties: list[int | None] = []
        self._temp_fail = 0
        self._write_fail = 0
        self._degraded = False
        self._degrade_reason: str | None = None
        self._release_note: str | None = None

        self._events: list[dict] = []
        self._events_lock = threading.Lock()

    # ------------------------------------------------------------------ 日志
    def log(self, level: str, message: str) -> None:
        entry = {"ts": time.time(), "level": level, "message": message}
        with self._events_lock:
            self._events.append(entry)
            keep = int(self.store.get().get("log_keep", 400))
            if len(self._events) > keep:
                del self._events[:-keep]
        if level in ("warn", "error", "critical"):
            self._last_error = message
        if self.log_sink is not None:
            try:
                self.log_sink(entry)
            except Exception:  # noqa: BLE001 - 日志失败不得影响控制
                pass

    def events(self, limit: int = 200) -> list[dict]:
        with self._events_lock:
            return list(self._events[-limit:])

    # ------------------------------------------------------------------ 快照持久化
    def _persist_snapshot(self, snapshot) -> None:
        """把接管前的硬件状态落盘。

        若进程随后被 SIGKILL（无法执行优雅退出），cmd/main 可读取该文件
        调用 ``--release`` 把风扇还原给固件，避免风扇停在未知转速。
        """
        if not self.snapshot_path:
            return
        try:
            tmp = self.snapshot_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.snapshot_path)
        except OSError as exc:
            self.log("warn", f"快照落盘失败（不影响控制）: {exc}")

    def _clear_snapshot(self) -> None:
        if not self.snapshot_path:
            return
        try:
            os.unlink(self.snapshot_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            self.log("warn", f"清理快照文件失败: {exc}")

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            if not getattr(self.backend, "is_open", False):
                self.backend.open()
            snapshot = self.backend.take_manual_control()
            self._persist_snapshot(snapshot)
            self.log("info", f"已接管风扇控制，接管前状态: {snapshot}")
            self._running = True
            self._started_at = time.time()
            self._last_cycle_ts = time.time()
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, name="fan-control", daemon=True)
            self._thread.start()
            self._watchdog = threading.Thread(target=self._watchdog_loop,
                                              name="fan-watchdog", daemon=True)
            self._watchdog.start()

    def stop(self, release: bool = True) -> None:
        with self._lock:
            if not self._running:
                if release and getattr(self.backend, "is_open", False):
                    self._release()
                return
            self._running = False
            self._stop_event.set()
        for thread in (self._thread, self._watchdog):
            if thread and thread.is_alive():
                thread.join(timeout=5)
        if release:
            self._release()

    def _release(self) -> None:
        try:
            note = self.backend.release(float(self.store.get().get("release_speed", 100)))
            self._release_note = note
            self.log("info", f"释放风扇控制权：{note}")
        except Exception as exc:  # noqa: BLE001 - 释放路径必须尽力而为
            self._release_note = f"释放失败: {exc}"
            self.log("error", self._release_note)
        self._clear_snapshot()

    # ------------------------------------------------------------------ 控制循环
    def _watchdog_timeout(self) -> float:
        interval = float(self.store.get().get("interval", 5))
        return max(WATCHDOG_MIN_TIMEOUT, interval * WATCHDOG_FACTOR)

    def _watchdog_loop(self) -> None:
        """独立心跳看门狗：主循环卡死时直接把风扇拉到全速。"""
        while not self._stop_event.wait(2.0):
            last = self._last_cycle_ts
            if last is None:
                continue
            if time.time() - last <= self._watchdog_timeout():
                continue
            self.log("critical", "控制循环心跳超时，看门狗介入：风扇全速")
            try:
                self.backend.set_speed(100.0)
                self._current_speed = 100.0
            except Exception as exc:  # noqa: BLE001
                self.log("error", f"看门狗写入全速失败: {exc}")
            self._last_cycle_ts = time.time()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            started = time.time()
            try:
                self._cycle()
            except Exception as exc:  # noqa: BLE001 - 控制循环不允许退出
                self.log("error", f"控制周期异常: {type(exc).__name__}: {exc}")
            self._last_cycle_ts = time.time()
            interval = float(self.store.get().get("interval", 5))
            elapsed = time.time() - started
            self._stop_event.wait(max(0.5, interval - elapsed))

    def _effective_bounds(self, cfg: dict) -> tuple[float, float]:
        low = max(float(cfg.get("min_speed", 0)), HARD_FLOOR)
        high = float(cfg.get("max_speed", 100))
        if high < low:
            high = low
        return low, high

    def _target_speed(self, cfg: dict, temp: float | None) -> float:
        mode = cfg.get("mode", "default")
        low, high = self._effective_bounds(cfg)

        if mode == "full":
            return 100.0
        if mode == "manual":
            speed = float(cfg.get("manual_speed", 50))
        elif mode == "auto":
            curve = cfg.get("curve") or config_mod.DEFAULT_CURVE
            speed = interpolate(curve, temp) if temp is not None else HARD_FLOOR
        else:  # default
            speed = interpolate(config_mod.DEFAULT_CURVE, temp) if temp is not None else HARD_FLOOR
        return max(low, min(speed, high))

    def _cycle(self) -> None:
        cfg = self.store.get()
        temp = self.reader.read_source(cfg.get("temp_source", "cpu"))

        # --- 温度读取失败处理 ---
        if temp is None:
            self._temp_fail += 1
            if self._temp_fail >= TEMP_FAIL_FORCE_FULL:
                self.log("warn",
                         f"温度连续 {self._temp_fail} 次读取失败，强制全速保护")
                target = 100.0
            else:
                target = self._target_speed(cfg, None)
            if self._temp_fail >= TEMP_FAIL_DEGRADE and not self._degraded:
                self._degraded = True
                self._degrade_reason = "温度持续读取失败，已降级到 default 模式"
                self.log("error", self._degrade_reason)
                self.store.update({"mode": "default"})
                cfg = self.store.get()
        else:
            if self._temp_fail:
                self.log("info", "温度读取恢复正常")
            self._temp_fail = 0
            target = self._target_speed(cfg, temp)

        self._last_temp = temp

        # --- 滞回：对称死区，抑制小幅抖动 ---
        # 之所以用对称死区而不是「只对降温做死区」：后者会让转速在温度噪声
        # 下逐次向上爬升（棘轮效应），最终偏离曲线。临界温度与 100% 目标
        # 不受死区限制，保证散热响应不被延迟。
        hysteresis = float(cfg.get("hysteresis", 0))
        if self._current_speed is not None and hysteresis > 0:
            delta = target - self._current_speed
            critical = target >= 100.0 or (temp is not None and temp >= CRITICAL_TEMP)
            if abs(delta) < hysteresis and not critical:
                target = self._current_speed

        target = max(HARD_FLOOR, min(target, 100.0))

        if self._current_speed is not None and abs(target - self._current_speed) < 0.01:
            return

        try:
            self.backend.set_speed(target)
            self._current_speed = target
            self._write_fail = 0
            self.log("debug",
                     f"温度 {temp if temp is not None else 'N/A'}°C -> 转速 {target:.0f}%")
        except (OSError, ECError, RuntimeError) as exc:
            self._write_fail += 1
            self.log("error", f"写入转速失败 ({self._write_fail}): {exc}")
            if self._write_fail >= WRITE_FAIL_DEGRADE and not self._degraded:
                self._degraded = True
                self._degrade_reason = f"连续写入失败: {exc}"
                self.log("error", self._degrade_reason)

    def _read_duties_safe(self) -> list[int | None]:
        """按需回读硬件占空比。

        只在查询状态时回读，不在每个控制周期回读：写入失败已由端口异常体现，
        每轮额外回读既让 EC 访问量翻倍，也会用回读值覆盖刚写入的结果。
        """
        try:
            duties = list(self.backend.read_duties())
        except Exception:  # noqa: BLE001
            return []
        # 过滤不合理读数，避免界面显示垃圾值
        return [d if isinstance(d, int) and 0 <= d <= DUTY_MAX else None for d in duties]

    # ------------------------------------------------------------------ 对外操作
    def set_mode(self, mode: str) -> dict:
        if mode not in config_mod.MODES:
            raise ValueError(f"未知模式: {mode}")
        self._degraded = False
        self._degrade_reason = None
        cfg = self.store.update({"mode": mode})
        self.log("info", f"切换模式 -> {mode}")
        # 模式切换后立刻执行一次，避免等待一个周期
        threading.Thread(target=self._safe_cycle, daemon=True).start()
        return cfg

    def set_manual_speed(self, speed: float) -> dict:
        cfg = self.store.update({"manual_speed": speed, "mode": "manual"})
        self.log("info", f"手动模式转速 -> {speed}%")
        threading.Thread(target=self._safe_cycle, daemon=True).start()
        return cfg

    def _safe_cycle(self) -> None:
        try:
            self._cycle()
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"立即控制周期失败: {exc}")

    # ------------------------------------------------------------------ 状态
    def status(self) -> dict:
        cfg = self.store.get()
        temps = self.reader.read_all()
        low, high = self._effective_bounds(cfg)
        duties = self._read_duties_safe() if getattr(self.backend, "is_open", False) else []
        readback = [duty_to_speed(d) for d in duties if d is not None]
        return {
            "running": self._running,
            "mode": cfg.get("mode"),
            "temp_source": cfg.get("temp_source"),
            "temperature": self._last_temp if self._last_temp is not None else temps.get(
                cfg.get("temp_source", "cpu")),
            "temperatures": temps,
            "target_speed": self._current_speed,
            "hardware_speed": (sum(readback) / len(readback)) if readback else None,
            "duties": duties,
            "min_speed": low,
            "max_speed": high,
            "hard_floor": HARD_FLOOR,
            "manual_speed": cfg.get("manual_speed"),
            "interval": cfg.get("interval"),
            "curve": cfg.get("curve"),
            "degraded": self._degraded,
            "degrade_reason": self._degrade_reason,
            "last_error": self._last_error,
            "release_note": self._release_note,
            "uptime": int(time.time() - self._started_at) if self._started_at else 0,
            "temp_fail_count": self._temp_fail,
            "write_fail_count": self._write_fail,
            "backend": type(self.backend).__name__,
        }
