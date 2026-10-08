"""Frame comparison that notices small menu changes (a toggled option, a moved cursor, a HUD number)
and ignores background animation.

The old check was a 16x9 average hash with a 14-bit tolerance: switching "2 players" to "1 player" or
moving a menu cursor changed 2-6% of the pixels and still counted as "the same screen", so the startup
guard refused to press the key again. Here a frame is a 64x36 grid of cells; a cell counts as changed when
its mean brightness moves by more than CELL_DELTA.
"""
from PIL import Image, ImageChops

GRID = (64, 36)
CELL_DELTA = 12


def load(path):
    return Image.open(path).convert("L").resize(GRID, Image.BILINEAR)


def changed_cells(a, b, delta=CELL_DELTA):
    """Set of (x, y) cells that differ between two frames (paths or loaded images)."""
    a = load(a) if isinstance(a, str) else a
    b = load(b) if isinstance(b, str) else b
    d = ImageChops.difference(a, b).getdata()
    w = GRID[0]
    return {(i % w, i // w) for i, v in enumerate(d) if v > delta}


def changed_pct(a, b):
    return 100.0 * len(changed_cells(a, b)) / (GRID[0] * GRID[1])


def changed(a, b, min_pct=0.4):
    """Did anything visible change? 0.4% = ~9 cells: a menu cursor or a toggled label."""
    return changed_pct(a, b) >= min_pct


def same_screen(a, b, max_pct=12.0):
    """Same menu page (cursor moves allowed), for loop detection."""
    return changed_pct(a, b) < max_pct


def effect_cells(before, control, after, restored=None):
    """Cells that changed because of an action, not because of animation.
    control: a second frame taken before acting (measures the background noise).
    restored: optional frame after undoing the action; real effects change back."""
    noise = changed_cells(before, control)
    eff = changed_cells(control, after) - noise
    if restored is not None:
        eff &= changed_cells(after, restored)
    return eff


def encode(img):
    """64x36 grayscale frame -> short base64 string (stored in playbook routes)."""
    import base64
    return base64.b64encode(img.tobytes()).decode()


def decode(text):
    import base64
    return Image.frombytes("L", GRID, base64.b64decode(text))
