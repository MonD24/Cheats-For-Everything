"""
Minimal Cheat-Engine-style value scanner (for finding simple values).
State is kept in scan_state.bin / scan_state.json next to this file between calls.

  python scanner.py first Game.exe int32 250 [tol]    # exact-value (or +-tol range) first scan
  python scanner.py next exact 230                   # filter: equals
  python scanner.py next decreased | increased | changed | unchanged
  python scanner.py list [n]                         # show candidates with current values
  python scanner.py write 0x1234ABCD int32 9999      # poke a value to confirm
  python scanner.py info 0x1234ABCD                  # which module/region an address is in
  python scanner.py aob Game.exe "48 8B ?? 05" [module] # count matches: a trainer AOB must be unique

For pointers, code AOBs and "what writes to this address", use Cheat Engine.
"""
import array
import ctypes
import ctypes.wintypes as wt
import json
import struct
import sys
from pathlib import Path

import pymem
import pymem.process

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trainerkit.runtime import VALUE_TYPES  # noqa: E402

STATE = Path(__file__).resolve().parent / "scan_state"
MEM_COMMIT, MEM_PRIVATE, MEM_IMAGE = 0x1000, 0x20000, 0x1000000
WRITABLE = {0x04, 0x08, 0x40, 0x80}  # RW, WC, ERW, EWC


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_ulonglong), ("AllocationBase", ctypes.c_ulonglong),
                ("AllocationProtect", wt.DWORD), ("__a", wt.DWORD), ("RegionSize", ctypes.c_ulonglong),
                ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD), ("__b", wt.DWORD)]


def regions(handle):
    addr, mbi = 0, MBI()
    q = ctypes.windll.kernel32.VirtualQueryEx
    while q(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
        if mbi.State == MEM_COMMIT and (mbi.Protect & 0xFF) in WRITABLE and not mbi.Protect & 0x100 \
                and mbi.Type in (MEM_PRIVATE, MEM_IMAGE):
            yield mbi.BaseAddress, mbi.RegionSize
        addr = mbi.BaseAddress + mbi.RegionSize
        if addr >= 0x7FFFFFFFFFFF:
            break


def load():
    meta = json.loads(STATE.with_suffix(".json").read_text())
    addrs = array.array("Q")
    addrs.frombytes(STATE.with_suffix(".bin").read_bytes())
    vals = meta.pop("values")
    return meta, addrs, vals


def save(meta, addrs, vals):
    STATE.with_suffix(".bin").write_bytes(addrs.tobytes())
    STATE.with_suffix(".json").write_text(json.dumps({**meta, "values": vals}))
    print(f"{len(addrs)} candidates")


def read_val(pm, addr, vt):
    fmt, size = VALUE_TYPES[vt]
    return struct.unpack(fmt, pm.read_bytes(addr, size))[0]


NP = {"byte": "u1", "int16": "<i2", "int32": "<i4", "int64": "<i8", "uint32": "<u4", "float": "<f4", "double": "<f8"}


def first(proc, vt, value, tol=0.0):
    """Exact (tol=0) or range scan [value-tol, value+tol] over writable memory, aligned to the type size
    (max 4). A range catches values that change while the scan runs (health draining, timers)."""
    import numpy as np
    pm = pymem.Pymem(proc)
    fmt, size = VALUE_TYPES[vt]
    v, tol = float(value), float(tol)
    lo, hi = (v - tol, v + tol) if tol or vt in ("float", "double") else (v, v)
    if vt in ("float", "double") and not tol:
        lo, hi = v - 0.01, v + 0.01
    step = min(size, 4)
    addrs, total = array.array("Q"), 0
    for base, size_r in regions(pm.process_handle):
        try:
            buf = pm.read_bytes(base, size_r)
        except Exception:
            continue
        total += size_r
        for off in range(0, size, step):
            n = (len(buf) - off) // size
            if n <= 0:
                continue
            arr = np.frombuffer(buf, dtype=NP[vt], count=n, offset=off)
            hits = np.nonzero((arr >= lo) & (arr <= hi))[0]
            addrs.extend((base + off + hits * size).tolist())
    addrs = array.array("Q", sorted(set(addrs)))
    print(f"scanned {total / 2**20:.0f} MB")
    vals = [read_val(pm, x, vt) for x in addrs] if len(addrs) < 200000 else []
    save({"process": proc, "type": vt}, addrs, vals)


def next_scan(cond, value=None):
    meta, addrs, vals = load()
    pm = pymem.Pymem(meta["process"])
    vt = meta["type"]
    keep, keep_vals = array.array("Q"), []
    old_vals = vals if vals else None
    for i, a in enumerate(addrs):
        try:
            cur = read_val(pm, a, vt)
        except Exception:
            continue
        prev = old_vals[i] if old_vals else None
        ok = {
            "exact": lambda: abs(cur - float(value)) < 0.01 if vt in ("float", "double") else cur == int(value),
            "changed": lambda: prev is None or cur != prev,
            "unchanged": lambda: prev is None or cur == prev,
            "increased": lambda: prev is None or cur > prev,
            "decreased": lambda: prev is None or cur < prev,
        }[cond]()
        if ok:
            keep.append(a)
            keep_vals.append(cur)
    save(meta, keep, keep_vals)


def show(n=20):
    meta, addrs, _ = load()
    pm = pymem.Pymem(meta["process"])
    for a in addrs[:n]:
        try:
            print(f"0x{a:X}  {read_val(pm, a, meta['type'])}  {where(pm, a)}")
        except Exception:
            print(f"0x{a:X}  <unreadable>")


def where(pm, addr):
    for m in pm.list_modules():
        if m.lpBaseOfDll <= addr < m.lpBaseOfDll + m.SizeOfImage:
            return f"{m.name}+0x{addr - m.lpBaseOfDll:X}  (static: usable as module+offset)"
    return "heap (needs a pointer chain or AOB)"


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__)
    elif a[0] == "first":
        first(a[1], a[2], a[3], float(a[4]) if len(a) > 4 else 0.0)
    elif a[0] == "next":
        next_scan(a[1], a[2] if len(a) > 2 else None)
    elif a[0] == "list":
        show(int(a[1]) if len(a) > 1 else 20)
    elif a[0] == "write":
        meta, _, _ = load() if STATE.with_suffix(".json").exists() else ({"process": None}, 0, 0)
        pm = pymem.Pymem(meta["process"])
        fmt, size = VALUE_TYPES[a[2]]
        pm.write_bytes(int(a[1], 16), struct.pack(fmt, float(a[3]) if a[2] in ("float", "double") else int(a[3])), size)
        print("written")
    elif a[0] == "aob":
        sys.path.insert(0, str(Path(__file__).resolve().parent / "trainerkit"))
        from runtime import aob_regex
        import pymem.pattern
        pm = pymem.Pymem(a[1])
        rx = aob_regex(a[2])
        if len(a) > 3:
            mod = pymem.process.module_from_name(pm.process_handle, a[3])
            hits = pymem.pattern.pattern_scan_module(pm.process_handle, mod, rx, return_multiple=True)
        else:
            hits = pymem.pattern.pattern_scan_all(pm.process_handle, rx, return_multiple=True)
        print(f"{len(hits)} match(es): " + " ".join(f"0x{h:X}" for h in hits[:10]))
        print("OK: unique" if len(hits) == 1 else "NOT usable as-is: make the pattern longer/more specific")
    elif a[0] == "info":
        meta, _, _ = load()
        print(where(pymem.Pymem(meta["process"]), int(a[1], 16)))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
