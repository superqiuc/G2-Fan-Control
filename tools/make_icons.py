#!/usr/bin/env python3
"""生成 g2fan 应用图标（纯标准库，无第三方依赖）。

绘制一个圆角深色底 + 四叶风扇，4 倍超采样后降采样得到抗锯齿效果。
输出：
  g2fan/ICON.PNG                 64x64   （应用中心列表图标）
  g2fan/ICON_256.PNG             256x256 （应用详情图标）
  g2fan/app/ui/images/icon_64.png
  g2fan/app/ui/images/icon_256.png
"""

import math
import os
import struct
import zlib

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "g2fan")

# 配色
BG_TOP = (18, 24, 31)
BG_BOTTOM = (11, 15, 20)
BLADE_IN = (63, 185, 80)      # #3fb950 绿
BLADE_OUT = (88, 166, 255)    # #58a6ff 蓝
HUB = (230, 237, 243)
RING = (48, 60, 74)

N_BLADES = 4
SECTOR = 2 * math.pi / N_BLADES
HUB_R = 0.17
BLADE_R0 = 0.21
BLADE_R1 = 0.86
TWIST = 1.45
WEDGE = 0.52          # 每个扇区内叶片所占比例


def _lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _rounded_box_alpha(px, py, half, radius):
    """圆角方形的覆盖度（px/py 为相对中心的坐标）。"""
    qx = abs(px) - (half - radius)
    qy = abs(py) - (half - radius)
    if qx < 0 and qy < 0:
        d = max(qx, qy) - radius
    else:
        d = math.hypot(max(qx, 0.0), max(qy, 0.0)) - radius
    return d


def render(size, ss=4):
    W = size * ss
    half = 0.5
    radius = 0.22
    acc = [[0.0, 0.0, 0.0, 0.0] for _ in range(size * size)]

    for y in range(W):
        v = (y + 0.5) / W
        for x in range(W):
            u = (x + 0.5) / W
            px, py = u - 0.5, v - 0.5

            d = _rounded_box_alpha(px, py, half, radius)
            if d > 0:
                continue  # 圆角外保持透明

            # 背景竖直渐变 + 轻微中心提亮
            base = _lerp(BG_TOP, BG_BOTTOM, v)
            dist_c = math.hypot(px, py)
            glow = max(0.0, 1.0 - dist_c / 0.72) ** 2 * 0.16
            color = tuple(min(255, int(c + glow * 90)) for c in base)

            r = dist_c / 0.5
            if r <= HUB_R:
                color = _lerp(color, HUB, 0.92)
            elif r < BLADE_R0:
                color = _lerp(color, RING, 0.35)
            elif r <= BLADE_R1:
                theta = math.atan2(py, px)
                theta += TWIST * (r - BLADE_R0)
                t = theta % SECTOR
                if t < SECTOR * WEDGE:
                    # 叶片：根部细、外缘宽，颜色由绿到蓝
                    edge = (r - BLADE_R0) / (BLADE_R1 - BLADE_R0)
                    blade = _lerp(BLADE_IN, BLADE_OUT, edge)
                    inner = min(1.0, (r - BLADE_R0) / 0.06) if r < BLADE_R0 + 0.06 else 1.0
                    color = _lerp(color, blade, 0.55 + 0.45 * inner)
            elif r <= BLADE_R1 + 0.05:
                color = _lerp(color, RING, 0.55)

            idx = (y // ss) * size + (x // ss)
            cell = acc[idx]
            cell[0] += color[0]
            cell[1] += color[1]
            cell[2] += color[2]
            cell[3] += 1.0          # 仅统计落在圆角矩形内的子像素

    n = ss * ss
    rows = []
    for j in range(size):
        row = bytearray()
        for i in range(size):
            rsum, gsum, bsum, hits = acc[j * size + i]
            if hits <= 0:
                row += bytes((0, 0, 0, 0))
                continue
            row += bytes((
                min(255, int(rsum / hits + 0.5)),
                min(255, int(gsum / hits + 0.5)),
                min(255, int(bsum / hits + 0.5)),
                min(255, int(255.0 * hits / n + 0.5)),
            ))
        rows.append(bytes(row))
    return rows


def png_bytes(size, rows):
    raw = b"".join(b"\x00" + row for row in rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def main():
    targets = [
        (64, os.path.join(ROOT, "ICON.PNG")),
        (256, os.path.join(ROOT, "ICON_256.PNG")),
        (64, os.path.join(ROOT, "app", "ui", "images", "icon_64.png")),
        (256, os.path.join(ROOT, "app", "ui", "images", "icon_256.png")),
    ]
    cache = {}
    for size, path in targets:
        if size not in cache:
            print(f"渲染 {size}x{size} ...")
            cache[size] = png_bytes(size, render(size))
        data = cache[size]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        print(f"  写出 {path} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
