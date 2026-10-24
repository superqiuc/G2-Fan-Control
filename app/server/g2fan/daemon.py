"""g2fan 守护进程入口。

启动顺序（安全相关，不可颠倒）：

1. 以 root 身份加载配置。
2. 打开 ``/dev/port``，完成 EC 探测 —— 这一步需要特权。
3. 打开日志文件并持有 fd（降权后仍可写）。
4. 降权到 fnOS 包用户（``TRIM_UID``/``TRIM_GID``），此后所有对外服务
   与配置读写都以非 root 身份运行。
5. 启动控制循环与 Web 服务。

这样既保证了对 EC 的可靠访问，又避免长期以 root 暴露网络服务。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

from . import __version__
from . import config as config_mod
from . import control as control_mod
from . import ec as ec_mod
from . import hwmon as hwmon_mod
from . import sensors as sensors_mod
from . import web as web_mod

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP_DIR = os.path.dirname(os.path.dirname(_HERE))  # .../app


# ---------------------------------------------------------------------- 路径
def resolve_paths() -> dict:
    """优先使用 fnOS 注入的运行时目录，开发时回退到仓库内目录。"""
    appdest = os.environ.get("TRIM_APPDEST") or _APP_DIR
    pkgvar = os.environ.get("TRIM_PKGVAR") or os.path.join(_APP_DIR, "devdata", "var")
    pkgetc = os.environ.get("TRIM_PKGETC") or os.path.join(_APP_DIR, "devdata", "etc")
    os.makedirs(pkgvar, exist_ok=True)
    os.makedirs(pkgetc, exist_ok=True)
    return {
        "appdest": appdest,
        "pkgvar": pkgvar,
        "pkgetc": pkgetc,
        "www": os.path.join(appdest, "www"),
        "config": os.path.join(pkgvar, "config.json"),
        "log": os.path.join(pkgvar, "fan.log"),
        "snapshot": os.path.join(pkgvar, "ec_snapshot.json"),
    }


# ---------------------------------------------------------------------- 端口
def desktop_entry_port(appdest: str) -> int | None:
    """读取 ``app/ui/config`` 中桌面入口声明的端口。

    桌面入口的端口决定应用图标能否打开，因此把它当作权威来源。
    只要入口端口合法，就用它覆盖配置文件里的值 —— 否则一旦配置漂移
    （例如旧版本把自带默认值写进了配置），图标就会指向一个没人监听的端口。
    """
    path = os.path.join(appdest, "ui", "config")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    entries = data.get(".url")
    if not isinstance(entries, dict):
        return None
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        raw = entry.get("port")
        if raw is None:
            continue
        text = str(raw).strip()
        if not text or "${" in text:      # 未展开的向导占位符，跳过
            continue
        try:
            port = int(text)
        except ValueError:
            continue
        if 1 <= port <= 65535:
            return port
    return None


def reconcile_port(store, appdest: str, log) -> dict:
    """决定最终监听端口，并返回生效的配置。

    优先级：``G2FAN_PORT`` 环境变量 > 桌面入口声明 > 配置文件 > 内置默认值。

    之所以让桌面入口优先于配置文件：入口端口决定应用图标能否打开，
    一旦配置漂移（例如旧版本把自带默认值写进了配置），图标就会指向
    一个没人监听的端口。``G2FAN_PORT`` 作为显式覆盖，供测试或
    确实需要换端口（并同步改了入口）的场景使用。
    """
    cfg = store.get()

    override = (os.environ.get("G2FAN_PORT") or "").strip()
    if override:
        try:
            port = int(override)
        except ValueError:
            port = -1
        if 1 <= port <= 65535:
            if int(cfg.get("port", 0) or 0) != port:
                log("info", f"按 G2FAN_PORT 覆盖监听端口 -> {port}")
                try:
                    return store.update({"port": port})
                except OSError as exc:
                    log("warn", f"端口回写失败，本次仍按 {port} 监听: {exc}")
                    merged = dict(cfg)
                    merged["port"] = port
                    return merged
            return cfg
        log("warn", f"忽略非法的 G2FAN_PORT={override!r}")

    entry_port = desktop_entry_port(appdest)
    if entry_port is None:
        return cfg
    try:
        current = int(cfg.get("port", 0))
    except (TypeError, ValueError):
        current = -1
    if current == entry_port:
        return cfg
    log("warn", f"配置端口 {cfg.get('port')} 与桌面入口 {entry_port} 不一致，"
                f"以桌面入口为准（否则应用图标会指向空端口）")
    try:
        return store.update({"port": entry_port})
    except OSError as exc:
        log("warn", f"端口回写失败，本次仍按 {entry_port} 监听: {exc}")
        merged = dict(cfg)
        merged["port"] = entry_port
        return merged


# ---------------------------------------------------------------------- 硬盘温度
def ensure_disk_sensor(log) -> dict:
    """确保硬盘温度可用：必要时加载 ``drivetemp`` 内核模块。

    背景：Linux 通过 ``drivetemp`` 驱动把 ATA 盘的 SMART 温度暴露成 hwmon
    节点。该模块**不保证开机自动加载**（本机型就没有 modules-load 配置），
    没加载时侧栏「硬盘温度」会一直显示 ``--``。

    拾光坞 G2 的系统盘是 eMMC（本身没有温度传感器），硬盘温度来自
    SATA 位上的 2.5" 盘，因此这里值得为它做一次兜底尝试。

    需要 root 权限，必须在降权之前调用。
    """
    result = {"available": False, "loaded": False, "temperature": None, "reason": ""}

    value = sensors_mod.TemperatureReader().read_role("disk")
    if value is not None:
        result.update(available=True, temperature=value, reason="drivetemp 已在运行")
        return result

    if os.geteuid() != 0:
        result["reason"] = "非 root，无法加载内核模块 drivetemp"
        return result

    exe = shutil.which("modprobe")
    if not exe:
        result["reason"] = "未找到 modprobe，无法加载 drivetemp"
        return result

    try:
        proc = subprocess.run([exe, "drivetemp"], capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        result["reason"] = f"执行 modprobe 失败: {exc}"
        return result

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        result["reason"] = f"modprobe drivetemp 返回 {proc.returncode}: {detail}"
        return result

    result["loaded"] = True
    # 等 hwmon 节点出现（驱动加载后会重新扫描 ATA/SCSI 设备）
    for _ in range(20):
        value = sensors_mod.TemperatureReader().read_role("disk")
        if value is not None:
            result.update(available=True, temperature=value, reason="已加载 drivetemp")
            return result
        time.sleep(0.25)

    result["reason"] = ("drivetemp 已加载但仍未读到温度："
                        "磁盘可能处于休眠，或该盘不支持 SMART 温度上报")
    return result


# ---------------------------------------------------------------------- 日志
class FileLogger:
    """先用 root 打开文件并长期持有 fd，降权后依然可写。"""

    MAX_BYTES = 8 * 1024 * 1024

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        try:
            self._fh = open(path, "a", encoding="utf-8")
        except OSError:
            self._fh = None

    def __call__(self, entry: dict) -> None:
        if self._fh is None:
            return
        with self._lock:
            try:
                if self._fh.tell() > self.MAX_BYTES:
                    return  # 超过上限后停止写入，避免撑爆应用数据目录
                stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["ts"]))
                self._fh.write(f"[{stamp}] {entry['level'].upper():<8} {entry['message']}\n")
                self._fh.flush()
            except OSError:
                self._fh = None

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


# ---------------------------------------------------------------------- 后端
def build_backend(cfg: dict, log) -> object:
    """按配置选择控制后端；``auto`` 优先 G2 EC，其次 hwmon。"""
    choice = cfg.get("backend", "auto")
    port_path = ec_mod.DEV_PORT_PATH

    if choice in ("auto", "sgw_ec"):
        if os.path.exists(port_path):
            try:
                backend = ec_mod.G2EC(port_path)
                backend.open()
                # 读一次寄存器确认通道真的可用，而不是只打开了设备节点
                enable = backend.read_enable()
                duties = backend.read_duties()
                log("info", f"已启用 G2 EC 后端 (0x2F1=0x{enable:02X})" if enable is not None
                    else "已启用 G2 EC 后端（使能寄存器读取为空）")
                log("info", f"当前风扇占空比: {duties}")
                return backend
            except (ec_mod.ECError, OSError) as exc:
                if choice == "sgw_ec":
                    raise
                log("warn", f"G2 EC 后端不可用，尝试 hwmon 回退: {exc}")
        else:
            if choice == "sgw_ec":
                raise ec_mod.ECError(f"{port_path} 不存在，无法使用 G2 EC 后端")
            log("info", f"{port_path} 不存在，改用 hwmon 后端")

    backend = hwmon_mod.HwmonBackend()
    backend.open()
    log("info", f"已启用 hwmon 后端，{len(backend.channels)} 个 PWM 通道")
    return backend


# ---------------------------------------------------------------------- 权限
def drop_privileges(log) -> bool:
    """从 root 降权到 fnOS 包用户。失败时保持原状但不中断服务。"""
    if os.geteuid() != 0:
        return False
    uid = os.environ.get("TRIM_UID") or os.environ.get("TRIM_RUN_UID")
    gid = os.environ.get("TRIM_GID") or os.environ.get("TRIM_RUN_GID")
    if not uid or not gid:
        log("warn", "未取得包用户信息（TRIM_UID/TRIM_GID），保持 root 运行")
        return False
    try:
        os.setgroups([int(gid)])
        os.setgid(int(gid))
        os.setuid(int(uid))
        log("info", f"已降权到包用户 uid={uid} gid={gid}")
        return True
    except OSError as exc:
        log("warn", f"降权失败，保持 root 运行: {exc}")
        return False


# ---------------------------------------------------------------------- 命令
def cmd_probe() -> int:
    """只读探测：报告后端可用性与传感器，不改变风扇状态。"""
    report = {
        "version": __version__,
        "paths": resolve_paths(),
        "euid": os.geteuid(),
        "dev_port_exists": os.path.exists(ec_mod.DEV_PORT_PATH),
        "ec": ec_mod.probe(),
        "hwmon": hwmon_mod.probe(),
        "sensors": sensors_mod.TemperatureReader().read_detail(),
        "sensor_roles": {
            role: [s.to_dict() for s in group]
            for role, group in sensors_mod.discover_roles().items()
        },
        "disk_temperature": sensors_mod.TemperatureReader().read_role("disk"),
        "drivetemp_loaded": os.path.isdir("/sys/module/drivetemp"),
        "disk_hint": ("硬盘温度依赖 drivetemp 内核模块；若为 null，"
                      "以 root 执行 `modprobe drivetemp` 后重试"),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def cmd_release() -> int:
    """崩溃恢复：按落盘的快照把风扇还原给固件。

    正常退出时快照文件会被删除，因此此处「没有快照」即表示无需还原。
    """
    paths = resolve_paths()
    snap_path = paths["snapshot"]
    if not os.path.exists(snap_path):
        print("没有待还原的快照，风扇未被本应用接管。")
        return 0

    try:
        with open(snap_path, "r", encoding="utf-8") as fh:
            snapshot = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"快照文件不可用（{exc}），将按安全兜底处理")
        snapshot = {}

    store = config_mod.ConfigStore(paths["config"])
    cfg = dict(store.get())
    snap_backend = snapshot.get("backend") if isinstance(snapshot, dict) else None
    if snap_backend in ("sgw_ec", "hwmon"):
        cfg["backend"] = snap_backend

    try:
        backend = build_backend(cfg, lambda lvl, msg: print(msg, flush=True))
    except Exception as exc:  # noqa: BLE001
        print(f"无法打开风扇后端: {exc}")
        return 1

    try:
        print(backend.restore(snapshot, float(store.get().get("release_speed", 100))))
    finally:
        try:
            backend.close()
        except Exception:  # noqa: BLE001
            pass

    try:
        os.unlink(snap_path)
    except OSError:
        pass
    return 0


def cmd_run() -> int:
    paths = resolve_paths()
    logger = FileLogger(paths["log"])

    def log(level: str, message: str) -> None:
        logger({"ts": time.time(), "level": level, "message": message})
        print(f"{time.strftime('%H:%M:%S')} {level.upper():<8} {message}", flush=True)

    store = config_mod.ConfigStore(paths["config"])
    cfg = store.get()
    log("info", f"g2fan {__version__} 启动；配置目录 {paths['pkgvar']}")
    # 端口以桌面入口声明为准，避免配置漂移后图标指向空端口
    cfg = reconcile_port(store, paths["appdest"], log)

    # --- 1. 打开后端（需要特权）---
    try:
        backend = build_backend(cfg, log)
    except Exception as exc:  # noqa: BLE001
        log("error", f"无法初始化风扇后端: {exc}")
        logger.close()
        return 1

    # --- 2. 硬盘温度：必要时加载 drivetemp（需在降权前，root 才能 modprobe）---
    disk = ensure_disk_sensor(log)
    if disk["available"]:
        log("info", f"硬盘温度可用：{disk['temperature']}°C（{disk['reason']}）")
    else:
        log("warn", f"硬盘温度不可用：{disk['reason']}")

    # --- 3. 降权 ---
    if cfg.get("drop_privileges", True):
        drop_privileges(log)
    else:
        log("info", "配置要求保持当前权限，跳过降权")

    # --- 4. 控制循环 ---
    reader = sensors_mod.TemperatureReader()
    if not reader.sensors:
        log("warn", "未发现任何温度传感器，温控将依赖降级保护")
    if reader.read_role("disk") is None:
        log("warn", "当前读不到硬盘温度；若温度来源选的是「硬盘」，温控会走降级保护")
    controller = control_mod.FanController(backend, store, reader, log_sink=logger,
                                           snapshot_path=paths["snapshot"])
    try:
        controller.start()
    except Exception as exc:  # noqa: BLE001
        log("error", f"接管风扇失败: {exc}")
        try:
            backend.close()
        except Exception:  # noqa: BLE001
            pass
        logger.close()
        return 1

    # --- 4. Web 服务 ---
    # 端口必须来自 reconcile_port 的结果：即使回写配置失败，本次也按入口端口监听
    bind_addr = str(store.get().get("bind") or cfg.get("bind") or "0.0.0.0")
    service_port = int(cfg["port"])
    web_ctx = web_mod.AppContext(controller, store, paths["www"], version=__version__)
    try:
        server = web_mod.build_server(web_ctx, bind_addr, service_port)
    except OSError as exc:
        log("error", f"无法监听 {bind_addr}:{service_port} - {exc}")
        controller.stop()
        backend.close()
        logger.close()
        return 1
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    log("info", f"Web 管理界面已就绪: http://{bind_addr}:{service_port}/")

    # --- 5. 等待退出信号 ---
    stop_event = threading.Event()

    def _handle(signum, _frame):
        log("info", f"收到信号 {signum}，准备退出")
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    try:
        while not stop_event.wait(1.0):
            pass
    finally:
        log("info", "正在停止服务")
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        controller.stop()          # 释放风扇控制权，交还固件
        try:
            backend.close()
        except Exception:  # noqa: BLE001
            pass
        logger.close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="g2fan", description="拾光坞 G2 风扇控制")
    parser.add_argument("--probe", action="store_true",
                        help="只读探测硬件后端与传感器，不修改风扇状态")
    parser.add_argument("--release", action="store_true",
                        help="按落盘快照把风扇还原给固件（供崩溃恢复使用）")
    parser.add_argument("--version", action="version", version=f"g2fan {__version__}")
    args = parser.parse_args(argv)
    if args.probe:
        return cmd_probe()
    if args.release:
        return cmd_release()
    return cmd_run()


if __name__ == "__main__":
    sys.exit(main())
