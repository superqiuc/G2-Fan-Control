"""通用 hwmon PWM 后端。

适用于风扇由标准 Super-IO/hwmon 芯片（ITE IT87xx、Nuvoton NCT67xx、
Fintek F718xx 等）控制的主板。G2 不使用该后端 —— G2 的风扇在 EC 上，
没有 pwm 节点 —— 但保留它可以让本工具在其他 fnOS 设备上同样可用。

安全要点：

* 每个控制周期校验 ``pwmN_enable``，若被 BIOS 抢回自动模式则改回手动
  （参考社区经验：部分 BIOS 会周期性重置该寄存器）。
* 退出时恢复接管前的 ``pwmN_enable``，把风扇交还固件。
"""

from __future__ import annotations

import glob
import os
import threading

HWMON_ROOT = "/sys/class/hwmon"

PWM_ENABLE_MANUAL = 1  # 绝大多数芯片：1 = 手动
PWM_ENABLE_AUTO = 2    # 2 = 芯片自动/BIOS 控制


class HwmonError(RuntimeError):
    pass


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _write_text(path: str, value) -> bool:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(value))
        return True
    except OSError:
        return False


class PwmChannel:
    """一个 PWM 通道，可能带转速反馈。"""

    def __init__(self, chip: str, index: int, pwm_path: str,
                 enable_path: str | None, fan_path: str | None):
        self.chip = chip
        self.index = index
        self.pwm_path = pwm_path
        self.enable_path = enable_path
        self.fan_path = fan_path
        self.original_enable: int | None = None

    @property
    def name(self) -> str:
        return f"{self.chip}/pwm{self.index}"

    def read_pwm(self) -> int | None:
        txt = _read_text(self.pwm_path)
        try:
            return int(txt) if txt is not None else None
        except ValueError:
            return None

    def read_rpm(self) -> int | None:
        if not self.fan_path:
            return None
        txt = _read_text(self.fan_path)
        try:
            return int(txt) if txt is not None else None
        except ValueError:
            return None

    def snapshot(self) -> None:
        if self.enable_path:
            txt = _read_text(self.enable_path)
            try:
                self.original_enable = int(txt) if txt is not None else None
            except ValueError:
                self.original_enable = None

    def set_enable(self, value: int) -> bool:
        if not self.enable_path:
            return False
        return _write_text(self.enable_path, value)

    def ensure_manual(self) -> None:
        """pwm_enable 自愈：被改回自动时重新置为手动。"""
        if not self.enable_path:
            return
        txt = _read_text(self.enable_path)
        try:
            current = int(txt) if txt is not None else None
        except ValueError:
            current = None
        if current != PWM_ENABLE_MANUAL:
            self.set_enable(PWM_ENABLE_MANUAL)

    def set_duty(self, duty: int) -> bool:
        return _write_text(self.pwm_path, max(0, min(int(duty), 255)))

    def to_dict(self) -> dict:
        return {
            "chip": self.chip,
            "index": self.index,
            "name": self.name,
            "pwm": self.read_pwm(),
            "rpm": self.read_rpm(),
        }


class HwmonBackend:
    """与 G2EC 保持同一控制接口的 hwmon 后端。"""

    def __init__(self, channels: list[PwmChannel] | None = None):
        self.channels = channels if channels is not None else discover_channels()
        self._lock = threading.RLock()
        self._taken = False

    # --- 控制接口（与 G2EC 对齐）------------------------------------------
    def open(self) -> "HwmonBackend":
        if not self.channels:
            raise HwmonError("未找到任何带 pwm 文件的 hwmon 芯片")
        return self

    def close(self) -> None:
        return None

    @property
    def is_open(self) -> bool:
        return bool(self.channels)

    def take_manual_control(self) -> dict:
        with self._lock:
            for ch in self.channels:
                ch.snapshot()
            for ch in self.channels:
                ch.set_enable(PWM_ENABLE_MANUAL)
            self._taken = True
        return {"backend": "hwmon",
                "enable": {ch.name: ch.original_enable for ch in self.channels}}

    def restore(self, snapshot, fallback_speed_pct: float = 100.0) -> str:
        """按快照还原 pwm_enable，把风扇交还固件。"""
        saved: dict = {}
        if isinstance(snapshot, dict):
            raw = snapshot.get("enable")
            if isinstance(raw, dict):
                saved = raw
        restored = []
        with self._lock:
            for ch in self.channels:
                target = saved.get(ch.name)
                if target is None:
                    target = (ch.original_enable if ch.original_enable is not None
                              else PWM_ENABLE_AUTO)
                try:
                    target = int(target)
                except (TypeError, ValueError):
                    target = PWM_ENABLE_AUTO
                if ch.set_enable(target):
                    restored.append(f"{ch.name}->{target}")
            self._taken = False
        if restored:
            return "已还原 pwm_enable：" + ", ".join(restored)
        return "未能还原 pwm_enable（可能无写权限）"

    def release(self, fallback_speed_pct: float = 100.0) -> str:
        return self.restore({"enable": {ch.name: ch.original_enable
                                        for ch in self.channels}}, fallback_speed_pct)

    def set_speed(self, speed_pct: float) -> int:
        duty = max(0, min(int(round(speed_pct / 100.0 * 255.0)), 255))
        with self._lock:
            for ch in self.channels:
                ch.ensure_manual()
                ch.set_duty(duty)
        return duty

    def set_duty(self, duty: int) -> None:
        with self._lock:
            for ch in self.channels:
                ch.ensure_manual()
                ch.set_duty(duty)

    def read_duties(self) -> list[int | None]:
        return [ch.read_pwm() for ch in self.channels]

    def describe(self) -> dict:
        return {
            "backend": "hwmon",
            "channels": [ch.to_dict() for ch in self.channels],
        }


def discover_channels() -> list[PwmChannel]:
    channels: list[PwmChannel] = []
    for base in sorted(glob.glob(os.path.join(HWMON_ROOT, "hwmon*"))):
        chip = _read_text(os.path.join(base, "name")) or os.path.basename(base)
        chip = chip.strip()
        for pwm_path in sorted(glob.glob(os.path.join(base, "pwm[0-9]*"))):
            fname = os.path.basename(pwm_path)
            if not fname[len("pwm"):].isdigit():
                continue  # 跳过 pwm1_enable 之类
            index = int(fname[len("pwm"):])
            enable_path = os.path.join(base, f"pwm{index}_enable")
            fan_path = os.path.join(base, f"fan{index}_input")
            channels.append(PwmChannel(
                chip=chip,
                index=index,
                pwm_path=pwm_path,
                enable_path=enable_path if os.path.exists(enable_path) else None,
                fan_path=fan_path if os.path.exists(fan_path) else None,
            ))
    return channels


def probe() -> dict:
    channels = discover_channels()
    return {
        "backend": "hwmon",
        "accessible": bool(channels),
        "channels": [ch.to_dict() for ch in channels],
        "error": None if channels else "未发现带 pwm 文件的 hwmon 芯片",
    }
