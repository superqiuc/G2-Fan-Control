#!/usr/bin/env python3
"""Static analysis of the vendor fanctrl binary to recover the EC protocol."""
import struct, sys
from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86 import X86_OP_MEM, X86_REG_RIP

PATH = 'fanctrl_vendor'
data = open(PATH, 'rb').read()

# ---------- minimal ELF64 parser ----------
assert data[:4] == b'\x7fELF'
e_shoff, = struct.unpack_from('<Q', data, 0x28)
e_shentsize, e_shnum, e_shstrndx = struct.unpack_from('<HHH', data, 0x3A)
secs = []
for i in range(e_shnum):
    off = e_shoff + i * e_shentsize
    name, typ, flags, addr, offset, size = struct.unpack_from('<IIQQQQ', data, off)
    secs.append(dict(name_off=name, type=typ, flags=flags, addr=addr,
                     offset=offset, size=size))
shstr = secs[e_shstrndx]
for s in secs:
    end = data.index(b'\0', shstr['offset'] + s['name_off'])
    s['name'] = data[shstr['offset'] + s['name_off']:end].decode()

def off2va(off):
    for s in secs:
        if s['type'] != 8 and s['offset'] <= off < s['offset'] + s['size']:
            return s['addr'] + (off - s['offset'])
    return None

def va2off(va):
    for s in secs:
        if s['type'] != 8 and s['addr'] <= va < s['addr'] + s['size']:
            return s['offset'] + (va - s['addr'])
    return None

print("=== sections ===")
for s in secs:
    if s['size']:
        print(f"  {s['name']:<20} addr=0x{s['addr']:08x} off=0x{s['offset']:08x} size=0x{s['size']:x}")

text = next(s for s in secs if s['name'] == '.text')
code = data[text['offset']:text['offset'] + text['size']]
print(f"\n.text: va=0x{text['addr']:x} size=0x{text['size']:x}")

# ---------- locate the key strings ----------
targets = {}
for needle in [b'ec open failed, has /dev/port?', b'/dev/port', b'Usage: %s speed(1~100)']:
    start = 0
    while True:
        i = data.find(needle, start)
        if i < 0:
            break
        va = off2va(i)
        if va:
            targets.setdefault(needle.decode(), []).append(va)
        start = i + 1
print("\n=== string addresses ===")
for k, v in targets.items():
    print(f"  {k!r}: {[hex(x) for x in v]}")

want = set()
for v in targets.values():
    want.update(v)

# ---------- disassemble .text, find RIP-relative xrefs ----------
md = Cs(CS_ARCH_X86, CS_MODE_64)
md.detail = True
xrefs = []
portio = []
for insn in md.disasm(code, text['addr']):
    m = insn.mnemonic
    # direct port I/O with immediate
    if m in ('out', 'in') and insn.operands:
        portio.append((insn.address, insn.mnemonic, insn.op_str))
    for op in insn.operands:
        if op.type == X86_OP_MEM and op.mem.base == X86_REG_RIP:
            tgt = insn.address + insn.size + op.mem.disp
            if tgt in want:
                xrefs.append((insn.address, insn.mnemonic, insn.op_str, tgt))

print("\n=== xrefs to key strings ===")
for a, mn, ops, t in xrefs:
    print(f"  0x{a:x}: {mn} {ops}   -> 0x{t:x}")

print(f"\n=== direct port I/O instructions: {len(portio)} ===")
for a, mn, ops in portio[:60]:
    print(f"  0x{a:x}: {mn} {ops}")

# ---------- also scan for in/out opcodes manually (in case capstone missed) ----------
print("\n=== raw opcode scan for 0xE4-0xE7 / 0xEC-0xEF ===")
hits = {}
for i in range(len(code) - 2):
    b = code[i]
    if b in (0xE4, 0xE5, 0xE6, 0xE7):
        va = text['addr'] + i
        hits.setdefault(va, (hex(b), hex(code[i + 1])))
for k in sorted(hits)[:40]:
    print(f"  0x{k:x}: opcode={hits[k][0]} imm={hits[k][1]}")
print(f"  total: {len(hits)}")

# ---------- dump functions referencing the EC string ----------
if xrefs:
    print("\n\n=== disassembly around first EC xref ===")
    first = xrefs[0][0]
    lo, hi = first - 0x180, first + 0x220
    lo_off = va2off(lo) or 0
    for i in md.disasm(code[lo_off - text['offset']:hi - text['offset']], lo):
        mark = '  <<<' if i.address == first else ''
        print(f"  0x{i.address:08x}  {i.bytes.hex():<18} {i.mnemonic:<8} {i.op_str}{mark}")
