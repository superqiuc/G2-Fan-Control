#!/usr/bin/env python3
"""EC 协议一致性测试。

黄金基准来自对厂商 fanctrl 二进制的经验性捕获
（把其中的 "/dev/port" 字符串补丁为 FIFO 后记录真实端口写入序列）：

    speed=50  -> 2E 11 2F 02 2E 10 2F F1 2E 12 2F AA
                 2E 11 2F 18 2E 10 2F 07 2E 12 2F 5C
                 2E 11 2F 18 2E 10 2F 09 2E 12 2F 5C

本测试断言我们的原生实现产生**逐字节相同**的序列，并验证真实
lseek/write 路径确实写到了正确的端口。
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


from g2fan import ec  # noqa: E402

# 厂商工具 speed=50 时捕获到的完整写入序列（36 字节）
VENDOR_SEQ_50 = bytes([
    0x2E, 0x11, 0x2F, 0x02, 0x2E, 0x10, 0x2F, 0xF1, 0x2E, 0x12, 0x2F, 0xAA,
    0x2E, 0x11, 0x2F, 0x18, 0x2E, 0x10, 0x2F, 0x07, 0x2E, 0x12, 0x2F, 0x5C,
    0x2E, 0x11, 0x2F, 0x18, 0x2E, 0x10, 0x2F, 0x09, 0x2E, 0x12, 0x2F, 0x5C,
])
# 对应的端口序列：0x4E / 0x4F 交替
VENDOR_PORTS = [0x4E if i % 2 == 0 else 0x4F for i in range(len(VENDOR_SEQ_50))]


class RecordingEC(ec.G2EC):
    """记录每一次端口写，用于与厂商序列比对。"""

    def __init__(self):
        super().__init__(port_path="/dev/null")
        self.fd = 0  # 避免真正打开设备
        self.writes = []

    def _outb(self, port, value):
        self.writes.append((port, value & 0xFF))

    def _inb(self, port):
        self.writes.append((port, None))
        return 0x5C


class TestProtocolMatchesVendor(unittest.TestCase):
    def test_speed_50_sequence_is_byte_identical(self):
        dev = RecordingEC()
        # 复现厂商 main() 的完整动作
        dev.ec_write(ec.REG_MANUAL_ENABLE, ec.MANUAL_ENABLE_VALUE)
        dev.set_speed(50)

        ports = [p for p, _ in dev.writes]
        values = bytes(v for _, v in dev.writes)

        self.assertEqual(len(dev.writes), 36)
        self.assertEqual(ports, VENDOR_PORTS)
        self.assertEqual(values, VENDOR_SEQ_50)

    def test_speed_to_duty_matches_vendor_truncation(self):
        # 厂商：value = (int)(speed / 100.0 * 184.0)，截断，上限 184
        cases = {0: 0, 1: 1, 30: 55, 50: 92, 70: 128, 99: 182, 100: 184,
                 150: 184, 300: 184}
        for speed, expected in cases.items():
            self.assertEqual(ec.speed_to_duty(speed), expected, f"speed={speed}")

    def test_duty_to_speed_roundtrip(self):
        self.assertEqual(ec.duty_to_speed(184), 100.0)
        self.assertEqual(ec.duty_to_speed(0), 0.0)
        self.assertAlmostEqual(ec.duty_to_speed(92), 50.0, places=1)

    def test_read_opens_data_window_before_reading(self):
        """回归测试：真机上曾漏掉「打开数据窗口」这一步。

        厂商 fanctrl 的读函数（0x4020a0）在 10 次选择写之后，还会再执行
        `outb(0x4E, 0x2F)`，然后才 `inb(0x4F)`。漏掉它只会读回上一次写到
        0x4F 的字节（选择序列末尾的 0x12），在真机上表现为
        0x2F1 / 0x1807 / 0x1809 全都读成 0x12。
        """
        dev = RecordingEC()
        value = dev.ec_read(0x1807)
        self.assertEqual(value, 0x5C)
        # 11 次写 + 1 次读
        self.assertEqual(len(dev.writes), 12)
        self.assertEqual(dev.writes[-1], (0x4F, None))
        self.assertEqual([p for p, _ in dev.writes[:11]], VENDOR_PORTS[:11])

        writes = bytes(v for _, v in dev.writes[:11])
        self.assertEqual(
            writes,
            bytes([0x2E, 0x11, 0x2F, 0x18, 0x2E, 0x10, 0x2F, 0x07,
                   0x2E, 0x12, 0x2F]),
            "读序列应为 10 次地址选择 + 1 次数据窗口命令")
        # 第 11 次写必须是「数据窗口」命令 0x2F
        self.assertEqual(dev.writes[10], (0x4E, 0x2F))

        # 读 0x2F1 应复用厂商第 1 帧的地址选择前缀
        dev2 = RecordingEC()
        dev2.ec_read(ec.REG_MANUAL_ENABLE)
        self.assertEqual(bytes(v for _, v in dev2.writes[:10]), VENDOR_SEQ_50[:10])
        self.assertEqual(dev2.writes[10], (0x4E, 0x2F))

    def test_write_and_read_differ_only_in_final_step(self):
        """写与读的前 11 步必须完全一致，区别只在最后一步。"""
        writer, reader = RecordingEC(), RecordingEC()
        writer.ec_write(0x1807, 0x5C)
        reader.ec_read(0x1807)
        self.assertEqual(writer.writes[:11], reader.writes[:11])
        self.assertEqual(writer.writes[11], (0x4F, 0x5C))   # 写：数据口写值
        self.assertEqual(reader.writes[11], (0x4F, None))   # 读：数据口读值


class TestRealPortPath(unittest.TestCase):
    """用真实文件验证 lseek(port)+write(byte) 的 I/O 路径。"""

    def test_writes_land_on_ports_0x4e_and_0x4f(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fake_port")
            with open(path, "wb") as fh:
                fh.write(b"\x00" * 0x60)

            dev = ec.G2EC(port_path=path)
            dev.open()
            try:
                dev.ec_write(ec.REG_MANUAL_ENABLE, ec.MANUAL_ENABLE_VALUE)
                dev.set_speed(50)
            finally:
                dev.close()

            with open(path, "rb") as fh:
                content = fh.read()

            # 每帧最后一次写 0x4E 的都是 0x2F；0x4F 最后写入的是 duty
            self.assertEqual(content[0x4E], 0x2F)
            self.assertEqual(content[0x4F], 0x5C)   # 92 = floor(50/100*184)
            # 未触碰的端口区域保持原样
            self.assertEqual(content[0x00], 0x00)
            self.assertEqual(content[0x5F], 0x00)

    def test_speed_100_writes_0xb8(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fake_port")
            with open(path, "wb") as fh:
                fh.write(b"\x00" * 0x60)
            dev = ec.G2EC(port_path=path)
            dev.open()
            try:
                dev.set_speed(100)
            finally:
                dev.close()
            with open(path, "rb") as fh:
                content = fh.read()
            self.assertEqual(content[0x4F], 0xB8)


class TestSnapshotRelease(unittest.TestCase):
    """RecordingEC._inb 固定返回 0x5C，用于模拟「回读到某个值」。"""

    def test_release_restores_snapshot_and_verifies_readback(self):
        dev = RecordingEC()
        # 快照值设为 0x5C，与回读值一致 -> 还原成功
        dev._snapshot = {ec.REG_MANUAL_ENABLE: 0x5C,
                         ec.FAN_REGISTERS[0]: 0xB8,
                         ec.FAN_REGISTERS[1]: 0xB8}
        note = dev.release()
        self.assertIn("已还原", note)
        self.assertIn("0x2F1=0x5C", note)
        # 快照值必须真的写到数据口（其后还会跟随一次回读的选择写）
        self.assertIn((0x4F, 0x5C), dev.writes, "应把快照的 0x2F1 值写回 EC")
        self.assertEqual(dev.writes[-1], (0x4F, None), "还原后应回读校验")

    def test_release_readback_mismatch_falls_back_to_safe_speed(self):
        """还原后回读不一致 -> 不能把风扇留在未知状态，改为全速兜底。"""
        dev = RecordingEC()
        dev._snapshot = {ec.REG_MANUAL_ENABLE: 0x00,     # 回读会得到 0x5C，不一致
                         ec.FAN_REGISTERS[0]: 0x10,
                         ec.FAN_REGISTERS[1]: 0x10}
        note = dev.release(fallback_speed_pct=100)
        self.assertIn("回读校验不一致", note)
        self.assertIn("兜底", note)
        tail = [w for w in dev.writes if w[0] == 0x4F and w[1] is not None]
        self.assertEqual(tail[-1][1], 0xB8, "应写入 100% 兜底转速")

    def test_release_without_snapshot_uses_safe_fallback(self):
        dev = RecordingEC()
        dev._snapshot = None
        note = dev.release(fallback_speed_pct=100)
        self.assertIn("兜底", note)
        self.assertEqual([w for w in dev.writes if w[0] == 0x4F][-1][1], 0xB8)

    def test_restore_accepts_persisted_snapshot_shape(self):
        """落盘的快照键是字符串，restore 必须能正确解析。"""
        dev = RecordingEC()
        note = dev.restore({"backend": "sgw_ec",
                            "regs": {"753": 0x5C, "6151": 0x40, "6153": 0x40}})
        self.assertIn("已还原", note)
        self.assertIn("0x2F1=0x5C", note)


class TestProbeDiagnostics(unittest.TestCase):
    def test_probe_flags_sentinel_read_path(self):
        """文件型假端口天然表现为「读回退到哨兵值」，正好用于验证自检。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fake_port")
            with open(path, "wb") as fh:
                fh.write(b"\x00" * 0x2000)
            result = ec.probe(path)
        self.assertTrue(result["accessible"])
        # 文件后端下所有读都返回最后一次写入 0x4F 的 0x12
        self.assertEqual(result["enable"], ec.SUB_SEL_DATA)
        self.assertTrue(result["read_path_suspect"], "应识别出读路径哨兵特征")
        self.assertTrue(any("哨兵" in n for n in result["notes"]))

    def test_probe_reports_missing_device(self):
        result = ec.probe("/nonexistent/dev/port")
        self.assertFalse(result["accessible"])
        self.assertIsNotNone(result["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
