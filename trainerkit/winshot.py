"""Screenshot of a game's window (client area), for the vision model and for screen-change checks.

The window is brought to the front and its screen area is grabbed: DirectX games present through flip-model
swap chains, and PrintWindow then returns a frozen frame (it looked identical while the player walked).
PrintWindow(PW_RENDERFULLCONTENT) stays as the fallback when the screen grab is black (exclusive fullscreen).

  shot("Game.exe", "out.png", scale=0.5)  ->  writes out.png (full size) and out_small.png (scaled)
"""
import ctypes
import ctypes.wintypes as wt
from pathlib import Path

from PIL import Image, ImageGrab, ImageStat

user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)      # real pixels, not DPI-scaled ones
except Exception:
    pass


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def client_box(hwnd):
    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    p = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(p))
    return p.x, p.y, p.x + r.right, p.y + r.bottom


def print_window(hwnd):
    wr = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wr))
    w, h = wr.right - wr.left, wr.bottom - wr.top
    if w <= 0 or h <= 0:
        return None
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    ok = user32.PrintWindow(hwnd, mem, 2)                # PW_RENDERFULLCONTENT
    bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)
    if not ok:
        return None
    img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    x0, y0, x1, y1 = client_box(hwnd)                    # crop the frame/title bar away
    return img.crop((x0 - wr.left, y0 - wr.top, x1 - wr.left, y1 - wr.top))


def black(img):
    return img is None or ImageStat.Stat(img.convert("L")).extrema[0][1] < 8


def capture(hwnd):
    if user32.GetForegroundWindow() != hwnd:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)                   # restore only if minimized: a maximized game keeps its size
        user32.keybd_event(0x12, 0, 0, 0); user32.keybd_event(0x12, 0, 2, 0)   # Alt nudge allows SetForegroundWindow
        user32.SetForegroundWindow(hwnd)
        import time
        time.sleep(0.15)
    img = ImageGrab.grab(bbox=client_box(hwnd), all_screens=True)
    if black(img):
        alt = print_window(hwnd)
        img = alt if not black(alt) else img
    return img


def shot(process, out, scale=0.5):
    """Save the game window to `out` and a scaled copy to <out>_small.png. Returns the small path or None."""
    from .play import window
    hwnd = window(process)
    if not hwnd:
        return None
    img = capture(hwnd)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    small = out.with_name(out.stem + "_small.png")
    if scale and scale != 1:
        img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale)))).save(small)
        return str(small)
    return str(out)
