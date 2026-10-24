#!/usr/bin/env python3
"""Empirically capture the vendor fanctrl port-write sequence.

The vendor binary opens "/dev/port" (O_RDWR) and performs lseek(port) + write(1 byte).
By patching that path string to a FIFO we control, every byte it would have written
to an I/O port is appended to the FIFO in order. lseek() fails harmlessly on a FIFO
and its return value is ignored by the binary.
"""
import os, struct, subprocess, sys, time, fcntl, shutil

SRC = 'fanctrl_vendor'
PATCHED = 'fanctrl_patched'
FIFO = 'fakeport'

data = bytearray(open(SRC, 'rb').read())

# --- parse ELF to map vaddr -> file offset ---
e_shoff, = struct.unpack_from('<Q', data, 0x28)
e_shentsize, e_shnum, e_shstrndx = struct.unpack_from('<HHH', data, 0x3A)
secs = []
for i in range(e_shnum):
    o = e_shoff + i * e_shentsize
    nm, typ, fl, addr, off, sz = struct.unpack_from('<IIQQQQ', data, o)
    secs.append((nm, typ, addr, off, sz))

def va2off(va):
    for nm, typ, addr, off, sz in secs:
        if typ != 8 and addr <= va < addr + sz:
            return off + (va - addr)
    raise ValueError(hex(va))

# string table for names
shstr_off = secs[e_shstrndx][3]
def secname(nm):
    e = data.index(b'\0', shstr_off + nm)
    return data[shstr_off + nm:e].decode()

ro = next(s for s in secs if secname(s[0]) == '.rodata')
ro_va, ro_off, ro_sz = ro[2], ro[3], ro[4]
print("=== .rodata window 0x497000-0x497060 ===")
for va in range(0x497000, 0x497060, 16):
    o = va2off(va)
    chunk = bytes(data[o:o + 16])
    printable = ''.join(chr(c) if 32 <= c < 127 else '.' for c in chunk)
    print(f"  0x{va:06x}  {chunk.hex(' ')}  |{printable}|")

# --- patch the real "/dev/port" path at 0x497050 to a short relative name ---
TARGET_VA = 0x497050
o = va2off(TARGET_VA)
orig = bytes(data[o:o + 10])
print(f"\nvendor path @0x{TARGET_VA:x}: {orig!r}")
assert orig.startswith(b'/dev/port'), orig
NEW = b'fakeport\0\0'          # same 9 bytes + NUL, relative path resolved from cwd
data[o:o + len(NEW)] = NEW
open(PATCHED, 'wb').write(data)
os.chmod(PATCHED, 0o755)
print(f"patched -> {NEW!r} (relative, resolved from cwd)")

# --- run through a FIFO to capture the ordered byte stream ---
for p in (FIFO,):
    if os.path.exists(p):
        os.remove(p)
os.mkfifo(FIFO)

read_fd = os.open(FIFO, os.O_RDWR | os.O_NONBLOCK)
captured = bytearray()
try:
    for speed in [int(x) for x in (sys.argv[1:] or ['50'])]:
        captured.clear()
        while True:                      # drain
            try:
                b = os.read(read_fd, 4096)
            except BlockingIOError:
                break
            if not b:
                break
            captured += b
        captured.clear()
        r = subprocess.run([f'./{PATCHED}', str(speed)], capture_output=True, text=True)
        time.sleep(0.3)
        while True:
            try:
                b = os.read(read_fd, 4096)
            except BlockingIOError:
                break
            if not b:
                break
            captured += b
        print(f"\n=== speed={speed} rc={r.returncode} captured {len(captured)} bytes ===")
        if r.stdout.strip(): print("  stdout:", r.stdout.strip())
        if r.stderr.strip(): print("  stderr:", r.stderr.strip())
        print("  bytes:", ' '.join(f'{x:02X}' for x in captured))
        # decode into (port, value) pairs is impossible from bytes alone; but grouped
        # in 12-byte frames we can see the structure.
        for i in range(0, len(captured), 12):
            fr = captured[i:i + 12]
            if len(fr) == 12:
                print(f"    frame{i//12}: "
                      f"reg={fr[0]:#04x} v0={fr[1]:#04x} "
                      f"reg={fr[2]:#04x} v1={fr[3]:#04x} "
                      f"reg={fr[4]:#04x} v2={fr[5]:#04x} "
                      f"reg={fr[6]:#04x} v3={fr[7]:#04x} "
                      f"reg={fr[8]:#04x} v4={fr[9]:#04x} "
                      f"reg={fr[10]:#04x} v5={fr[11]:#04x}")
finally:
    os.close(read_fd)
    os.remove(FIFO)
