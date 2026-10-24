"""拾光坞 G2 风扇 EC 后端（原生实现，无厂商二进制依赖）。

硬件机制
--------
G2 的风扇不受标准 Linux hwmon / Super-IO 驱动管辖，因此系统里看不到任何
``pwm*`` 节点。风扇挂在内嵌控制器（EC）上，只能通过裸 I/O 端口访问 EC RAM。

访问通道：``/dev/port``（需 CONFIG_DEVPORT=y，本机内核已启用）。
端口 0x4E = 命令/索引口，0x4F = 数据口。一次 EC RAM 访问由 5 个 (寄存器, 数据)
对组成，共 10 次端口写；写操作再追加 1 对（共 12 次），读操作改为读 0x4F。

    0x4E <- 0x2E   0x4F <- 0x11      # 选择地址高字节窗口
    0x4E <- 0x2F   0x4F <- addr_hi
    0x4E <- 0x2E   0x4F <- 0x10      # 选择地址低字节窗口
    0x4E <- 0x2F   0x4F <- addr_lo
    0x4E <- 0x2E   0x4F <- 0x12      # 选择数据窗口
    0x4E <- 0x2F   0x4F <- value     # 写：写入数据 / 读：改为 inb(0x4F)

风扇相关寄存器：

===================  ==========  ==========================================
寄存器地址            典型值      含义
===================  ==========  ==========================================
0x2F1                0xAA        切到 EC 手动调速（覆盖固件自动曲线）
0x1807               duty        风扇 1 占空比
0x1809               duty        风扇 2 占空比
===================  ==========  ==========================================

占空比取值范围 0..184（0xB8），百分比换算为 ``floor(pct / 100 * 184)``，
与厂商工具完全一致（截断而非四舍五入）。

上述序列是通过对厂商 ``fanctrl`` 二进制做静态反汇编 + 把 ``/dev/port``
补丁为 FIFO 后捕获真实写入序列，双向交叉验证得到的，而非猜测。
"""

from __future__ import annotations

import os
import threading

# --- 端口与子寄存器 ---
EC_CMD_PORT = 0x4E
EC_DATA_PORT = 0x4F
SUB_REG_CMD = 0x2E
SUB_REG_DATA = 0x2F
SUB_SEL_ADDR_HI = 0x11
SUB_SEL_ADDR_LO = 0x10
SUB_SEL_DATA = 0x12

# --- 风扇寄存器 ---
REG_MANUAL_ENABLE = 0x2F1
MANUAL_ENABLE_VALUE = 0xAA
FAN_REGISTERS = (0x1807, 0x1809)

# --- 占空比范围 ---
DUTY_MAX = 184  # 0xB8

DEV_PORT_PATH = os.environ.get("G2FAN_DEV_PORT") or "/dev/port"


class ECError(RuntimeError):
    """EC 访问失败。"""


def speed_to_duty(speed_pct: float) -> int:
    """百分比 -> EC 占空比（0..184）。截断，与厂商工具一致。"""
    if speed_pct <= 0:
        return 0
    duty = int(speed_pct / 100.0 * DUTY_MAX)
    return DUTY_MAX if duty > DUTY_MAX else duty


def duty_to_speed(duty: int) -> float:
    """EC 占空比 -> 百分比。"""
    if duty <= 0:
        return 0.0
    if duty >= DUTY_MAX:
        return 100.0
    return round(duty * 100.0 / DUTY_MAX, 1)


class G2EC:
    """通过 /dev/port 访问 G2 风扇 EC。

    线程安全：所有端口操作由内部锁串行化。控制循环与看门狗可能并发访问，
    必须保证一次 12 步序列不被交错。
    """

    def __init__(self, port_path: str = DEV_PORT_PATH):
        self.port_path = port_path
        self.fd: int | None = None
        self._lock = threading.RLock()
        self._snapshot: dict[int, int | None] | None = None

    # ------------------------------------------------------------------ 生命周期
    def open(self) -> "G2EC":
        if self.fd is not None:
            return self
        try:
            self.fd = os.open(self.port_path, os.O_RDWR)
        except PermissionError as exc:
            raise ECError(
                f"无法打开 {self.port_path}（权限不足）。"
                "需要 root，或将应用用户加入 kmem 组。"
            ) from exc
        except FileNotFoundError as exc:
            raise ECError(
                f"{self.port_path} 不存在。内核需要启用 CONFIG_DEVPORT。"
            ) from exc
        except OSError as exc:
            raise ECError(f"打开 {self.port_path} 失败: {exc}") from exc
        return self

    def close(self) -> None:
        with self._lock:
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None

    @property
    def is_open(self) -> bool:
        return self.fd is not None

    def __enter__(self) -> "G2EC":
        return self.open()

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------------ 端口原语
    def _ensure_open(self) -> int:
        if self.fd is None:
            raise ECError("EC 端口未打开")
        return self.fd

    def _outb(self, port: int, value: int) -> None:
        fd = self._ensure_open()
        os.lseek(fd, port, os.SEEK_SET)
        os.write(fd, bytes((value & 0xFF,)))

    def _inb(self, port: int) -> int:
        fd = self._ensure_open()
        os.lseek(fd, port, os.SEEK_SET)
        chunk = os.read(fd, 1)
        if not chunk:
            raise ECError("读取端口返回空数据")
        return chunk[0]

    # ------------------------------------------------------------------ EC RAM 访问
    def _select(self, addr: int) -> None:
        """执行 10 次端口写，把 EC RAM 地址装载进窗口。"""
        addr_hi = (addr >> 8) & 0xFF
        addr_lo = addr & 0xFF
        self._outb(EC_CMD_PORT, SUB_REG_CMD)
        self._outb(EC_DATA_PORT, SUB_SEL_ADDR_HI)
        self._outb(EC_CMD_PORT, SUB_REG_DATA)
        self._outb(EC_DATA_PORT, addr_hi)
        self._outb(EC_CMD_PORT, SUB_REG_CMD)
        self._outb(EC_DATA_PORT, SUB_SEL_ADDR_LO)
        self._outb(EC_CMD_PORT, SUB_REG_DATA)
        self._outb(EC_DATA_PORT, addr_lo)
        self._outb(EC_CMD_PORT, SUB_REG_CMD)
        self._outb(EC_DATA_PORT, SUB_SEL_DATA)

    def ec_write(self, addr: int, value: int) -> None:
        """写一个字节到 EC RAM（12 次端口写）。"""
        with self._lock:
            self._select(addr)
            self._outb(EC_CMD_PORT, SUB_REG_DATA)
            self._outb(EC_DATA_PORT, value & 0xFF)

    def ec_read(self, addr: int) -> int:
        """从 EC RAM 读一个字节（11 次端口写 + 1 次端口读）。

        注意：`_select()` 之后**必须**再向命令口写一次 0x2F 打开数据窗口，
        然后才能读数据口。漏掉这一步只会读回上一次写到 0x4F 的字节
        （即选择序列末尾的 0x12），而不是 EC 的真实数据 —— 这一点已在
        拾光坞 G2 真机上实测确认。
        """
        with self._lock:
            self._select(addr)
            self._outb(EC_CMD_PORT, SUB_REG_DATA)
            return self._inb(EC_DATA_PORT)

    # ------------------------------------------------------------------ 风扇控制
    def read_duties(self) -> list[int | None]:
        """读取两个风扇当前占空比；失败项为 None。"""
        out: list[int | None] = []
        for reg in FAN_REGISTERS:
            try:
                out.append(self.ec_read(reg))
            except (OSError, ECError):
                out.append(None)
        return out

    def read_enable(self) -> int | None:
        try:
            return self.ec_read(REG_MANUAL_ENABLE)
        except (OSError, ECError):
            return None

    def snapshot(self) -> dict[int, int | None]:
        """记录接管前的 EC 状态，供 release() 还原固件行为。"""
        snap: dict[int, int | None] = {REG_MANUAL_ENABLE: self.read_enable()}
        for reg in FAN_REGISTERS:
            try:
                snap[reg] = self.ec_read(reg)
            except (OSError, ECError):
                snap[reg] = None
        self._snapshot = snap
        return snap

    def set_duty(self, duty: int) -> None:
        """把同一个占空比写入两个风扇。"""
        duty = max(0, min(int(duty), DUTY_MAX))
        for reg in FAN_REGISTERS:
            self.ec_write(reg, duty)

    def set_speed(self, speed_pct: float) -> int:
        """按百分比设置两个风扇，返回实际写入的占空比。"""
        duty = speed_to_duty(speed_pct)
        self.set_duty(duty)
        return duty

    def take_manual_control(self) -> dict:
        """接管风扇：先快照，再切换到手动调速。

        返回可 JSON 序列化的快照，便于进程被强杀后由外部还原。
        """
        snap = self.snapshot()
        self.ec_write(REG_MANUAL_ENABLE, MANUAL_ENABLE_VALUE)
        return {"backend": "sgw_ec",
                "regs": {str(k): v for k, v in snap.items()}}

    @staticmethod
    def _parse_snapshot(snapshot) -> dict:
        regs: dict[int, int | None] = {}
        if isinstance(snapshot, dict):
            raw = snapshot.get("regs", snapshot)
            if isinstance(raw, dict):
                for key, value in raw.items():
                    try:
                        regs[int(key)] = None if value is None else int(value)
                    except (TypeError, ValueError):
                        continue
        return regs

    def restore(self, snapshot, fallback_speed_pct: float = 100.0) -> str:
        """按给定快照还原 EC 状态；还原不可靠时退回安全转速。

        还原后会回读使能寄存器校验。若写入未生效，说明还原不可靠，
        宁可让风扇保持全速，也不能把它留在未知的手动转速上。
        """
        regs = self._parse_snapshot(snapshot)
        enable = regs.get(REG_MANUAL_ENABLE)

        if enable is not None:
            # 先还原占空比，最后还原使能寄存器，让固件重新接管
            for reg in FAN_REGISTERS:
                duty = regs.get(reg)
                if duty is not None:
                    try:
                        self.ec_write(reg, duty)
                    except (OSError, ECError):
                        pass
            try:
                self.ec_write(REG_MANUAL_ENABLE, enable)
            except (OSError, ECError) as exc:
                return self._safe_fallback(fallback_speed_pct,
                                           f"还原 EC 使能寄存器失败: {exc}")
            # 回读校验：确认还原真的落到了硬件上
            try:
                readback = self.ec_read(REG_MANUAL_ENABLE)
            except (OSError, ECError):
                readback = None
            if readback == enable:
                return f"已还原接管前 EC 状态 (0x2F1=0x{enable:02X})"
            shown = "读取失败" if readback is None else f"0x{readback:02X}"
            return self._safe_fallback(
                fallback_speed_pct,
                f"还原后回读校验不一致（写入 0x{enable:02X}，读到 {shown}）")

        return self._safe_fallback(fallback_speed_pct, "无可用快照")

    def _safe_fallback(self, fallback_speed_pct: float, reason: str) -> str:
        """安全兜底：把风扇拉到给定转速，绝不停在未知状态。"""
        duty = speed_to_duty(fallback_speed_pct)
        try:
            self.set_duty(duty)
            return f"{reason}，已设为兜底转速 {fallback_speed_pct:.0f}%"
        except (OSError, ECError) as exc:
            return f"{reason}，且兜底设置失败: {exc}"

    def release(self, fallback_speed_pct: float = 100.0) -> str:
        """交还控制权（使用本进程内的快照）。"""
        return self.restore(self._snapshot or {}, fallback_speed_pct)


def probe(port_path: str = DEV_PORT_PATH) -> dict:
    """探测 G2 EC 是否可用（不改变风扇状态）。

    除了可用性，还检查一个具体的故障特征：如果读路径缺少「打开数据窗口」
    这一步，读任何寄存器都会返回选择序列末尾写入的 SUB_SEL_DATA(0x12)，
    表现为 0x2F1 / 0x1807 / 0x1809 全部等于 0x12。这种一致性在真实硬件上
    不可能是巧合，因此可作为读路径失效的判据。
    """
    result = {
        "backend": "sgw_ec",
        "port_path": port_path,
        "accessible": False,
        "error": None,
        "enable": None,
        "fan_duties": None,
        "read_path_suspect": False,
        "notes": [],
    }
    ec = G2EC(port_path)
    try:
        ec.open()
        result["accessible"] = True
        result["enable"] = ec.read_enable()
        result["fan_duties"] = ec.read_duties()

        values = [v for v in ([result["enable"]] + list(result["fan_duties"] or []))
                  if v is not None]
        if values and all(v == SUB_SEL_DATA for v in values):
            result["read_path_suspect"] = True
            result["notes"].append(
                f"所有寄存器都读成 0x{SUB_SEL_DATA:02X}（选择序列的哨兵值），"
                "高度怀疑读路径缺少数据窗口命令，回读值不可信")
        if result["accessible"] and not values:
            result["notes"].append("EC 可打开但寄存器读取全部失败")
    except ECError as exc:
        result["error"] = str(exc)
    except OSError as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        ec.close()
    return result


if __name__ == "__main__":  # 手动探测
    import json

    print(json.dumps(probe(), indent=2, ensure_ascii=False))
