"""温度传感器发现与读取。

不依赖 lm-sensors，直接读取 hwmon 与 thermal zone，避免引入第三方依赖。

按角色归类：

* ``cpu``   —— CPU 封装温度（coretemp / k10temp / zenpower / x86_pkg_temp / cpu-thermal）
* ``disk``  —— 硬盘与 NVMe 温度（drivetemp / nvme），取最大值
* ``board`` —— 主板/环境温度（acpitz 等）

角色是温控曲线的输入，用户可选择跟随 CPU、硬盘或两者最大值。
"""

from __future__ import annotations

import glob
import os
import time

HWMON_ROOT = "/sys/class/hwmon"
THERMAL_ROOT = "/sys/class/thermal"

# hwmon 驱动名 -> CPU 温度传感器优先级（越大越优先）
_CPU_DRIVERS = {
    "coretemp": 100,
    "k10temp": 90,
    "zenpower": 95,
    "cpu_thermal": 80,
    "soc_thermal": 70,
}

_DISK_DRIVERS = {"drivetemp": 100, "nvme": 90}

_BOARD_DRIVERS = {"acpitz": 60, "acpitz_acpi": 60}

# 优先选择的标签（Intel/AMD 常见命名）
_CPU_PREFERRED_LABELS = ("package id 0", "tdie", "tctl", "physical id 0", "cpu")


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _read_int(path: str) -> int | None:
    txt = _read_text(path)
    if txt is None:
        return None
    try:
        return int(txt)
    except ValueError:
        return None


class Sensor:
    """单个温度传感器。"""

    __slots__ = ("sensor_id", "role", "label", "path", "driver", "priority")

    def __init__(self, sensor_id: str, role: str, label: str, path: str,
                 driver: str, priority: int):
        self.sensor_id = sensor_id
        self.role = role
        self.label = label
        self.path = path
        self.driver = driver
        self.priority = priority

    def read(self) -> float | None:
        """返回摄氏度；读取失败或数值明显异常时返回 None。"""
        milli = _read_int(self.path)
        if milli is None:
            return None
        celsius = milli / 1000.0
        # 过滤占位值：0 / 负值 / 明显不合理的读数
        if celsius <= 0.0 or celsius > 150.0:
            return None
        return celsius

    def to_dict(self) -> dict:
        return {
            "id": self.sensor_id,
            "role": self.role,
            "label": self.label,
            "driver": self.driver,
            "path": self.path,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Sensor {self.sensor_id} {self.role} {self.label}>"


def _hwmon_label(base: str, index: int) -> str:
    return _read_text(os.path.join(base, f"temp{index}_label")) or f"temp{index}"


def _scan_hwmon() -> list[Sensor]:
    sensors: list[Sensor] = []
    for base in sorted(glob.glob(os.path.join(HWMON_ROOT, "hwmon*"))):
        driver = _read_text(os.path.join(base, "name"))
        if not driver:
            continue
        driver = driver.strip()
        role: str | None = None
        priority = 0
        if driver in _CPU_DRIVERS:
            role, priority = "cpu", _CPU_DRIVERS[driver]
        elif driver in _DISK_DRIVERS:
            role, priority = "disk", _DISK_DRIVERS[driver]
        elif driver in _BOARD_DRIVERS:
            role, priority = "board", _BOARD_DRIVERS[driver]
        if role is None:
            continue

        for input_path in sorted(glob.glob(os.path.join(base, "temp*_input"))):
            fname = os.path.basename(input_path)
            try:
                index = int(fname[len("temp"):-len("_input")])
            except ValueError:
                continue
            label = _hwmon_label(base, index)
            # CPU 驱动按标签偏好加权，保证优先选中封装温度
            eff_priority = priority
            if role == "cpu":
                low = label.lower()
                if any(pref in low for pref in _CPU_PREFERRED_LABELS):
                    eff_priority += 50
            sensors.append(Sensor(
                sensor_id=f"{driver}:{label}",
                role=role,
                label=label,
                path=input_path,
                driver=driver,
                priority=eff_priority,
            ))
    return sensors


def _scan_thermal_zones() -> list[Sensor]:
    sensors: list[Sensor] = []
    for base in sorted(glob.glob(os.path.join(THERMAL_ROOT, "thermal_zone*"))):
        ztype = _read_text(os.path.join(base, "type"))
        if not ztype:
            continue
        ztype = ztype.strip()
        role: str | None = None
        priority = 0
        if ztype in ("x86_pkg_temp", "cpu-thermal", "cpu_thermal"):
            role, priority = "cpu", 60
        elif ztype.startswith("acpitz"):
            role, priority = "board", 40
        if role is None:
            continue
        sensors.append(Sensor(
            sensor_id=f"thermal:{ztype}",
            role=role,
            label=ztype,
            path=os.path.join(base, "temp"),
            driver=ztype,
            priority=priority,
        ))
    return sensors


def discover() -> list[Sensor]:
    """枚举所有可用温度传感器。"""
    return _scan_hwmon() + _scan_thermal_zones()


def discover_roles() -> dict[str, list[Sensor]]:
    grouped: dict[str, list[Sensor]] = {"cpu": [], "disk": [], "board": []}
    for sensor in discover():
        grouped.setdefault(sensor.role, []).append(sensor)
    for sensors in grouped.values():
        sensors.sort(key=lambda s: s.priority, reverse=True)
    return grouped


class TemperatureReader:
    """按角色聚合温度读数。"""

    REFRESH_INTERVAL = 10.0

    def __init__(self, sensors: list[Sensor] | None = None):
        self.sensors = sensors if sensors is not None else discover()
        self._last_refresh = time.monotonic()

    def refresh(self) -> None:
        self.sensors = discover()
        self._last_refresh = time.monotonic()

    def _maybe_refresh_for(self, role: str) -> None:
        """该角色一个传感器都没有时，节流地重新扫描 sysfs。

        硬盘温度来自 ``drivetemp`` 内核模块，该模块不保证开机自动加载，
        也可能在应用运行期间才被加载。懒重扫让后到的模块能在几秒内被
        发现，不必重启应用。
        """
        if any(s.role == role for s in self.sensors):
            return
        if time.monotonic() - self._last_refresh < self.REFRESH_INTERVAL:
            return
        self.refresh()

    def read_role(self, role: str) -> float | None:
        """返回该角色下的代表温度。

        cpu/board 取优先级最高的传感器；disk 取所有硬盘中的最大值。
        """
        candidates = [s for s in self.sensors if s.role == role]
        if not candidates:
            self._maybe_refresh_for(role)
            candidates = [s for s in self.sensors if s.role == role]
        if not candidates:
            return None
        if role == "disk":
            values = [v for v in (s.read() for s in candidates) if v is not None]
            return max(values) if values else None
        candidates.sort(key=lambda s: s.priority, reverse=True)
        for sensor in candidates:
            value = sensor.read()
            if value is not None:
                return value
        return None

    def read_all(self) -> dict[str, float | None]:
        return {role: self.read_role(role) for role in ("cpu", "disk", "board")}

    def read_detail(self) -> list[dict]:
        out = []
        for sensor in self.sensors:
            value = sensor.read()
            if value is None:
                continue
            item = sensor.to_dict()
            item["value"] = round(value, 1)
            out.append(item)
        out.sort(key=lambda d: (d["role"], -d.get("value", 0)))
        return out

    def read_source(self, source: str) -> float | None:
        """按配置的温度来源取温度。

        source: ``cpu`` | ``disk`` | ``max``
        """
        if source == "cpu":
            return self.read_role("cpu")
        if source == "disk":
            return self.read_role("disk")
        values = [v for v in self.read_all().values() if v is not None]
        return max(values) if values else None
