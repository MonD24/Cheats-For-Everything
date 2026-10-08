"""Drive a game: focus its window, press/hold keys (hardware scan codes, which games read),
click, and take window screenshots. Used by the agent to explore and to verify cheats.

  python -m trainerkit.play <Game.exe> "enter" "sleep 1.5" "hold d 0.8" "tap j" "shot out.png"
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time
from pathlib import Path

import psutil

user32 = ctypes.windll.user32

# key name -> (scan code, extended)
SC = {**{c: s for c, s in zip("1234567890", range(0x02, 0x0C))},
      **dict(zip("qwertyuiop", range(0x10, 0x1A))), **dict(zip("asdfghjkl", range(0x1E, 0x27))),
      **dict(zip("zxcvbnm", range(0x2C, 0x33))),
      "esc": (0x01, 0), "enter": (0x1C, 0), "space": (0x39, 0), "tab": (0x0F, 0), "backspace": (0x0E, 0),
      "lshift": (0x2A, 0), "lctrl": (0x1D, 0), "lalt": (0x38, 0),
      "up": (0x48, 1), "down": (0x50, 1), "left": (0x4B, 1), "right": (0x4D, 1),
      **{f"f{i}": (0x3A + i, 0) for i in range(1, 11)}, "f11": (0x57, 0), "f12": (0x58, 0),
      "insert": (0x52, 1), "delete": (0x53, 1)}
SC = {k: (v if isinstance(v, tuple) else (v, 0)) for k, v in SC.items()}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


def _key(name, up):
    sc, ext = SC[name.lower()]
    flags = 0x0008 | (0x0002 if up else 0) | (0x0001 if ext else 0)  # SCANCODE | KEYUP | EXTENDED
    i = INPUT(type=1)
    i.ki = KEYBDINPUT(0, sc, flags, 0, None)
    user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(i))


def window(process):
    pids = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == process.lower()}
    found = []

    def cb(h, _):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(h):
            found.append(h)
        return True
    user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)(cb), 0)
    return found[0] if found else None


def focus(process):
    h = window(process)
    if not h:
        raise RuntimeError(f"no window for {process}")
    if user32.IsIconic(h):
        user32.ShowWindow(h, 9)          # restore only if minimized: a maximized game keeps its size
    user32.keybd_event(0x12, 0, 0, 0); user32.keybd_event(0x12, 0, 2, 0)  # Alt nudge lets SetForegroundWindow work
    user32.SetForegroundWindow(h)
    time.sleep(0.15)
    return h


def tap(name, secs=0.08):
    _key(name, False); time.sleep(secs); _key(name, True)


def hold(names, secs):
    for n in names:
        _key(n, False)
    time.sleep(secs)
    for n in reversed(names):
        _key(n, True)


def click(process, x, y):
    h = focus(process)
    pt = wt.POINT(x, y)
    user32.ClientToScreen(h, ctypes.byref(pt))
    user32.SetCursorPos(pt.x, pt.y)
    for flag in (0x0002, 0x0004):
        i = INPUT(type=0); i.mi = MOUSEINPUT(0, 0, 0, flag, 0, None)
        user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(i)); time.sleep(0.05)


def shot(process, out, scale=0.5):
    """Window screenshot: writes `out` and <out>_small.png; returns the small one (None if no window)."""
    from .winshot import shot as window_shot
    return window_shot(process, out, scale)


def validate_step(step):
    if not isinstance(step, str) or not step.strip():
        raise ValueError("each input step must be a non-empty string")
    parts = step.split()
    command = parts[0].lower()
    if command == "tap":
        if len(parts) != 2 or parts[1].lower() not in SC:
            raise ValueError(f"use 'tap <key>' with a supported key: {step}")
    elif command == "hold":
        if len(parts) != 3 or not all(key.lower() in SC for key in parts[1].split("+")):
            raise ValueError(f"use 'hold <key> <seconds>'; hold releases automatically: {step}")
        try:
            duration = float(parts[2])
        except ValueError as e:
            raise ValueError(f"hold duration must be numeric: {step}") from e
        if not 0 < duration <= 10:
            raise ValueError("hold duration must be between 0 and 10 seconds")
    elif command == "sleep":
        if len(parts) != 2:
            raise ValueError(f"use 'sleep <seconds>': {step}")
        try:
            duration = float(parts[1])
        except ValueError as e:
            raise ValueError(f"sleep duration must be numeric: {step}") from e
        if not 0 <= duration <= 30:
            raise ValueError("sleep duration must be between 0 and 30 seconds")
    elif command == "click":
        if len(parts) != 3:
            raise ValueError(f"use 'click <x> <y>': {step}")
        try:
            int(parts[1]); int(parts[2])
        except ValueError as e:
            raise ValueError(f"click coordinates must be integers: {step}") from e
    elif command in ("shot", "screenshot"):
        if len(parts) not in (1, 2):
            raise ValueError(f"use 'shot' or 'shot <file>': {step}")
    elif command == "release":
        raise ValueError("release is not needed; hold <key> <seconds> releases automatically")
    elif len(parts) != 1 or command not in SC:
        raise ValueError(f"unknown input step: {step}")
    return command, parts


def run(process, steps):
    """steps: 'tap j', 'hold d 0.8', 'hold d+j 0.5', 'sleep 1', 'click 640 360', 'shot file.png'"""
    parsed = [validate_step(step) for step in steps]
    focus(process)
    log = []
    for command, p in parsed:
        if command == "sleep":
            time.sleep(float(p[1]))
        elif command == "tap":
            tap(p[1])
        elif command == "hold":
            hold(p[1].split("+"), float(p[2]))
        elif command == "click":
            click(process, int(p[1]), int(p[2]))
        elif command in ("shot", "screenshot"):
            out = p[1] if len(p) > 1 else Path.cwd() / f"shot_{int(time.time() * 1000)}.png"
            log.append(shot(process, out))
        else:
            tap(command)
    return log


if __name__ == "__main__":
    print(run(sys.argv[1], sys.argv[2:]))
