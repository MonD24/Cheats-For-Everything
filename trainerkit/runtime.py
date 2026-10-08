"""
Trainer runtime: everything a generated trainer needs at run time.

A trainer is a dict (loaded from trainer.json) like:
{
  "title": "Some Game +3 Trainer",
  "process": "Game.exe",
  "cheats": [
    {"name": "Infinite Health", "hotkey": "F1", "type": "freeze",
     "target": {"module": "Game.exe", "offset": "0x1A2B3C0", "pointers": ["0x58", "0x120"]},
     "value_type": "float", "value": 100.0},
    {"name": "Add 1000 Gold", "hotkey": "F2", "type": "add",
     "target": {"aob": "48 8B 05 ?? ?? ?? ?? 89 88", "aob_module": "Game.exe", "rip_relative": 3, "offset": "0x10"},
     "value_type": "int32", "value": 1000},
    {"name": "No Ammo Decrease", "hotkey": "F3", "type": "patch",
     "target": {"aob": "FF 48 10 8B 40 10", "aob_module": "Game.exe"},
     "bytes": "90 90 90"}
  ]
}

Cheat types:
  freeze  - keep a value fixed while ON
  set     - write a value once when the hotkey is pressed
  add     - add to a value once when the hotkey is pressed
  patch   - overwrite code bytes while ON, original bytes restored when OFF / on exit
Targets:
  absolute  {"address": "0x..."}
  module    {"module": "Game.exe", "offset": "0x...", "pointers": [...]}  (pointer chain)
  chains    {"chains": [<module targets>...]}  first chain that resolves wins (pointer scan + restart check)
  aob       {"aob": "pattern", "aob_module": "Game.exe" | null(all memory), "aob_offset": n,
             "rip_relative": n (read a RIP-relative disp at match+n), "offset": "0x..", "pointers": [...]}
"""
import ctypes
import ctypes.wintypes as wt
import re
import struct
import sys
import time

import pymem
import pymem.pattern
import pymem.process

VALUE_TYPES = {
    "byte": ("<B", 1), "int16": ("<h", 2), "int32": ("<i", 4), "int64": ("<q", 8),
    "uint32": ("<I", 4), "float": ("<f", 4), "double": ("<d", 8),
}

VK = {**{f"F{i}": 0x6F + i for i in range(1, 13)},
      **{f"NUM{i}": 0x60 + i for i in range(10)},
      **{chr(c): c for c in range(0x30, 0x3A)}, **{chr(c): c for c in range(0x41, 0x5B)},
      "INSERT": 0x2D, "DELETE": 0x2E, "HOME": 0x24, "END": 0x23, "PGUP": 0x21, "PGDN": 0x22}
MODS = {"CTRL": 0x11, "SHIFT": 0x10, "ALT": 0x12}

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
PAGE_EXECUTE_READWRITE = 0x40


def num(v):
    return int(v, 16) if isinstance(v, str) else int(v)


def aob_regex(pattern):
    out = b""
    for tok in pattern.split():
        out += b"." if tok in ("?", "??") else re.escape(bytes([int(tok, 16)]))
    return out


class Hotkey:
    def __init__(self, spec):
        parts = spec.upper().replace(" ", "").split("+")
        self.mods = [MODS[p] for p in parts[:-1]]
        self.key = VK[parts[-1]]
        self.was_down = False

    def pressed(self):
        down = bool(user32.GetAsyncKeyState(self.key) & 0x8000) and all(
            user32.GetAsyncKeyState(m) & 0x8000 for m in self.mods)
        edge = down and not self.was_down
        self.was_down = down
        return edge


class Game:
    def __init__(self, process_name):
        self.name = process_name
        self.pm = None

    def attach(self, wait=True):
        while True:
            try:
                self.pm = pymem.Pymem(self.name)
                self.is64 = pymem.process.is_64_bit(self.pm.process_handle)
                return
            except pymem.exception.ProcessNotFound:
                if not wait:
                    raise
                time.sleep(1)

    def alive(self):
        try:
            code = wt.DWORD()
            kernel32.GetExitCodeProcess(self.pm.process_handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        except Exception:
            return False

    def module_base(self, name):
        return pymem.process.module_from_name(self.pm.process_handle, name).lpBaseOfDll

    def read_ptr(self, addr):
        return self.pm.read_ulonglong(addr) if self.is64 else self.pm.read_uint(addr)

    def aob(self, pattern, module=None):
        rx = aob_regex(pattern)
        if module:
            mod = pymem.process.module_from_name(self.pm.process_handle, module)
            hit = pymem.pattern.pattern_scan_module(self.pm.process_handle, mod, rx)
        else:
            hits = pymem.pattern.pattern_scan_all(self.pm.process_handle, rx, return_multiple=True)
            if len(hits) > 1:
                # an ambiguous signature would patch the wrong bytes: refuse instead
                raise LookupError(f"AOB matches {len(hits)} places, make it longer: {pattern}")
            hit = hits[0] if hits else None
        if not hit:
            raise LookupError(f"AOB not found: {pattern}")
        return hit

    def resolve(self, t):
        if "chains" in t:
            # pointer chains validated across a game restart, best first: use the first one that resolves
            last = None
            for c in t["chains"]:
                try:
                    a = self.resolve(c)
                    self.pm.read_bytes(a, 4)
                    return a
                except Exception as e:
                    last = e
            raise LookupError(f"no pointer chain resolves yet ({last})")
        if "address" in t:
            addr = num(t["address"])
        elif "aob" in t:
            addr = self.aob(t["aob"], t.get("aob_module")) + int(t.get("aob_offset", 0))
            if "rip_relative" in t:
                at = addr + int(t["rip_relative"])
                disp = struct.unpack("<i", self.pm.read_bytes(at, 4))[0]
                addr = at + 4 + disp
            addr += num(t.get("offset", 0))
        else:
            addr = self.module_base(t["module"]) + num(t.get("offset", 0))
        for off in t.get("pointers", []):
            addr = self.read_ptr(addr) + num(off)
        return addr

    def read(self, addr, vtype):
        fmt, size = VALUE_TYPES[vtype]
        return struct.unpack(fmt, self.pm.read_bytes(addr, size))[0]

    def write(self, addr, vtype, value):
        fmt, _ = VALUE_TYPES[vtype]
        if vtype in ("float", "double"):
            value = float(value)
        else:
            value = int(value)
        self.pm.write_bytes(addr, struct.pack(fmt, value), struct.calcsize(fmt))

    def write_code(self, addr, data):
        old = wt.DWORD()
        kernel32.VirtualProtectEx(self.pm.process_handle, ctypes.c_void_p(addr), len(data),
                                  PAGE_EXECUTE_READWRITE, ctypes.byref(old))
        self.pm.write_bytes(addr, data, len(data))
        kernel32.VirtualProtectEx(self.pm.process_handle, ctypes.c_void_p(addr), len(data),
                                  old.value, ctypes.byref(old))


class Cheat:
    def __init__(self, game, spec):
        self.g, self.s = game, spec
        self.name, self.kind = spec["name"], spec["type"]
        self.hotkey = Hotkey(spec["hotkey"])
        self.on = False
        self.error = None
        self.original = None
        self.patch_addr = None

    def addr(self):
        return self.g.resolve(self.s["target"])

    def press(self):
        try:
            self.error = None
            if self.kind == "freeze":
                self.on = not self.on
            elif self.kind == "set":
                self.g.write(self.addr(), self.s["value_type"], self.s["value"])
            elif self.kind == "add":
                a, vt = self.addr(), self.s["value_type"]
                self.g.write(a, vt, self.g.read(a, vt) + self.s["value"])
            elif self.kind == "patch":
                self.toggle_patch(not self.on)
            beep(self.on or self.kind in ("set", "add"))
        except Exception as e:  # the game may still be loading the value
            self.error = str(e)[:60]
            self.on = False
            beep(False)

    def toggle_patch(self, enable):
        data = bytes.fromhex(self.s["bytes"].replace(" ", ""))
        if enable:
            self.patch_addr = self.addr()
            self.original = self.g.pm.read_bytes(self.patch_addr, len(data))
            self.g.write_code(self.patch_addr, data)
        elif self.original is not None:
            self.g.write_code(self.patch_addr, self.original)
            self.original = None
        self.on = enable

    def tick(self):
        if self.on and self.kind == "freeze":
            try:
                self.g.write(self.addr(), self.s["value_type"], self.s["value"])
            except Exception as e:
                self.error = str(e)[:60]

    def restore(self):
        if self.kind == "patch" and self.on:
            try:
                self.toggle_patch(False)
            except Exception:
                pass


def beep(ok):
    try:
        kernel32.Beep(880 if ok else 440, 80)
    except Exception:
        pass


def draw(title, game, cheats, status):
    sys.stdout.write("\x1b[H\x1b[2J")
    print(f"  {title}\n  {'=' * len(title)}\n")
    print(f"  Game: {game.name}  [{status}]\n")
    for c in cheats:
        state = "ON " if c.on else ("   " if c.kind in ("set", "add") else "OFF")
        line = f"  [{c.s['hotkey']:>9}]  {state}  {c.name}"
        if c.error:
            line += f"   (! {c.error})"
        print(line)
    print("\n  Hotkeys work anywhere (also in game). Ctrl+C here to quit (code patches are restored).")
    sys.stdout.flush()


def run(trainer):
    kernel32.SetConsoleTitleW(trainer["title"])
    # enable ANSI escapes in the Windows console
    h = kernel32.GetStdHandle(-11)
    mode = wt.DWORD()
    kernel32.GetConsoleMode(h, ctypes.byref(mode))
    kernel32.SetConsoleMode(h, mode.value | 0x0004)

    game = Game(trainer["process"])
    print(f"Waiting for {trainer['process']} ... start the game.")
    game.attach(wait=True)
    cheats = [Cheat(game, c) for c in trainer["cheats"]]
    last_draw = 0
    try:
        while True:
            if not game.alive():
                for c in cheats:
                    c.on = False
                draw(trainer["title"], game, cheats, "game closed - waiting")
                game.attach(wait=True)
            dirty = False
            for c in cheats:
                if c.hotkey.pressed():
                    c.press()
                    dirty = True
                c.tick()
            if dirty or time.time() - last_draw > 2:
                draw(trainer["title"], game, cheats, "attached")
                last_draw = time.time()
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        for c in cheats:
            c.restore()
