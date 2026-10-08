"""Pointer scanner: turn a heap address (valid for one game session) into module+offset+pointer chains that
survive a restart - the Cheat Engine "pointer scan, then rescan after restarting the game" method.

  chains = scan(pm, target, max_depth=4, max_offset=0x1000)
  # each chain: {"module": "Game.exe", "offset": "0x1A2B3C0", "pointers": ["0x58", "0x120"]}
  # runtime: addr = module_base + offset; for o in pointers: addr = read_ptr(addr) + o  -> target

Pointer map: every aligned pointer-sized value in writable memory that points into committed memory, kept as
two sorted numpy arrays, so "who points to [t - max_offset, t]" is a binary search. Static roots are
addresses inside a module image (its .data/.bss), which do not move between launches (ASLR moves the base;
chains are stored relative to it).
"""
import ctypes
import ctypes.wintypes as wt
import struct
import time

import numpy as np
import pymem.process

MEM_COMMIT, MEM_IMAGE = 0x1000, 0x1000000
WRITABLE = {0x04, 0x08, 0x40, 0x80}


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_ulonglong), ("AllocationBase", ctypes.c_ulonglong),
                ("AllocationProtect", wt.DWORD), ("__a", wt.DWORD), ("RegionSize", ctypes.c_ulonglong),
                ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD), ("__b", wt.DWORD)]


def regions(handle):
    addr, mbi = 0, MBI()
    q = ctypes.windll.kernel32.VirtualQueryEx
    while q(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
        if mbi.State == MEM_COMMIT and not mbi.Protect & 0x100:
            yield mbi.BaseAddress, mbi.RegionSize, (mbi.Protect & 0xFF) in WRITABLE, mbi.Type == MEM_IMAGE
        addr = mbi.BaseAddress + mbi.RegionSize
        if addr >= 0x7FFFFFFFFFFF:
            break


class PointerMap:
    def __init__(self, pm, log=print, max_region=512 << 20):
        t0 = time.time()
        self.pm = pm
        self.is64 = pymem.process.is_64_bit(pm.process_handle)
        self.size = 8 if self.is64 else 4
        dtype = np.uint64 if self.is64 else np.uint32
        regs = list(regions(pm.process_handle))
        self.committed = np.array(sorted((b, b + s) for b, s, _, _ in regs), dtype=np.uint64)
        self.modules = [(m.lpBaseOfDll, m.lpBaseOfDll + m.SizeOfImage, m.name) for m in pm.list_modules()]
        addrs, vals, total = [], [], 0
        lo, hi = self.committed[0][0], self.committed[-1][1]
        for base, size, writable, image in regs:
            if not writable or size > max_region:
                continue
            try:
                buf = self.pm.read_bytes(base, size)
            except Exception:
                continue
            total += size
            arr = np.frombuffer(buf, dtype=dtype, count=size // self.size).astype(np.uint64)
            keep = np.nonzero((arr >= lo) & (arr < hi))[0]
            if keep.size:
                v = arr[keep]
                ok = self.valid(v)
                addrs.append(base + keep[ok].astype(np.uint64) * self.size)
                vals.append(v[ok])
        self.addr = np.concatenate(addrs) if addrs else np.zeros(0, np.uint64)
        self.val = np.concatenate(vals) if vals else np.zeros(0, np.uint64)
        order = np.argsort(self.val, kind="stable")
        self.addr, self.val = self.addr[order], self.val[order]
        log(f"pointer map: {len(self.val):,} pointers from {total / 2**20:.0f} MB in {time.time() - t0:.1f}s")

    def valid(self, v):
        """Values that land inside a committed region."""
        starts, ends = self.committed[:, 0], self.committed[:, 1]
        i = np.searchsorted(starts, v, side="right") - 1
        return (i >= 0) & (v < ends[np.clip(i, 0, len(ends) - 1)])

    def module_of(self, a):
        for lo, hi, name in self.modules:
            if lo <= a < hi:
                return name, a - lo
        return None

    def pointing_into(self, target, max_offset):
        """(address, offset) of pointers whose value is in [target - max_offset, target]."""
        lo = np.searchsorted(self.val, np.uint64(max(0, target - max_offset)), side="left")
        hi = np.searchsorted(self.val, np.uint64(target), side="right")
        return [(int(a), int(target - v)) for a, v in zip(self.addr[lo:hi], self.val[lo:hi])]


def scan(pm, target, max_depth=4, max_offset=0x1000, max_nodes=4000, max_chains=200, log=print, pmap=None):
    """Backward breadth-first search from the target to static module addresses."""
    pmap = pmap or PointerMap(pm, log)
    chains = []
    frontier = [(target, [])]          # (address, offsets from it to the target, forward order)
    seen = {target}
    for depth in range(1, max_depth + 1):
        nxt = []
        for t, offs in frontier:
            for a, off in pmap.pointing_into(t, max_offset):
                path = [off] + offs
                mod = pmap.module_of(a)
                if mod:
                    chains.append({"module": mod[0], "offset": hex(mod[1]), "pointers": [hex(o) for o in path]})
                    if len(chains) >= max_chains:
                        return rank(chains)
                elif a not in seen and len(nxt) < max_nodes:
                    seen.add(a)
                    nxt.append((a, path))
        log(f"  depth {depth}: {len(chains)} chains, {len(nxt)} heap nodes to follow")
        frontier = nxt
        if not frontier:
            break
    return rank(chains)


def rank(chains):
    """Short chains with small offsets in the game's own module first (they break least on updates)."""
    def key(c):
        own = c["module"].lower().endswith(".exe")
        return (len(c["pointers"]), not own, sum(int(o, 16) for o in c["pointers"]))
    return sorted(chains, key=key)


def resolve(pm, chain):
    """Follow a chain in the running game; None if a link is unreadable."""
    try:
        is64 = pymem.process.is_64_bit(pm.process_handle)
        a = pymem.process.module_from_name(pm.process_handle, chain["module"]).lpBaseOfDll + int(chain["offset"], 16)
        for o in chain["pointers"]:
            raw = pm.read_bytes(a, 8 if is64 else 4)
            a = struct.unpack("<Q" if is64 else "<I", raw)[0] + int(o, 16)
        return a
    except Exception:
        return None


def survivors(pm, chains, target):
    """Chains that still lead to `target` (same session: the value's current address)."""
    return [c for c in chains if resolve(pm, c) == target]
