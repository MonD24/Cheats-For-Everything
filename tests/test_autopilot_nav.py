"""Offline test of the autopilot's menu exploration (no game, no GPU): a simulated menu with a
"Quit" item, toggles that cycle on confirm, and a cutscene. Run:  python tests/test_autopilot_nav.py"""
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trainerkit import autopilot  # noqa: E402

autopilot.time.sleep = lambda s: None


class Menu:
    """splash -> title(Start/Quit) -> save -> mode(count, difficulty, next) -> character -> cutscene -> game"""

    def __init__(self):
        self.screen, self.cursor, self.count, self.diff, self.quit, self.ticks = "splash", 0, 2, 0, False, 0

    def key(self, k):
        s = self.screen
        if s == "splash":
            return
        if s == "title":
            if k in ("down", "up"):
                self.cursor = 1 - self.cursor
            elif k == "accept":
                if self.cursor == 1:
                    self.quit = True
                else:
                    self.screen, self.cursor = "save", 0
        elif s == "save" and k == "accept":
            self.screen = "mode"
        elif s == "mode":
            if k == "down":
                self.cursor = min(2, self.cursor + 1)
            elif k == "up":
                self.cursor = max(0, self.cursor - 1)
            elif k in ("left", "right", "accept") and self.cursor == 0:
                self.count = 3 - self.count
            elif k in ("left", "right", "accept") and self.cursor == 1:
                self.diff = (self.diff + 1) % 3
            elif k == "accept" and self.cursor == 2:
                self.screen = "character"
        elif s == "character" and k == "accept":
            self.screen = "cutscene"
        elif s == "cutscene" and k == "skip":
            self.screen = "game"

    def wait(self):
        self.ticks += 1
        if self.screen == "splash" and self.ticks >= 2:
            self.screen = "title"

    def draw(self, path):
        names = ["splash", "title", "save", "mode", "character", "cutscene", "game"]
        im = Image.new("L", (640, 360), 30 + 30 * names.index(self.screen))
        d = ImageDraw.Draw(im)
        d.rectangle([20 + 80 * names.index(self.screen), 20, 80 + 80 * names.index(self.screen), 80], fill=255)
        if self.screen in ("title", "mode"):
            d.rectangle([500, 100 + 60 * self.cursor, 620, 150 + 60 * self.cursor], fill=0)   # cursor
        if self.screen == "mode":
            d.rectangle([300, 100, 300 + 60 * self.count, 150], fill=250)                      # player count
            d.rectangle([300, 170, 330 + 40 * self.diff, 210], fill=200)                       # difficulty
        im.save(path)
        return str(path)


class FakeUG:
    process = "sim.exe"

    def scene(self):
        return "MainMenu"


def make(menu, ask, use_vision=True):
    tmp = Path(tempfile.mkdtemp())
    ap = autopilot.Autopilot(FakeUG(), tmp, {"FaceAccept": "h", "MoveVertical": "w/s", "MoveHorizontal": "a/d"},
                             log=lambda m: None, ask=ask, use_vision=use_vision)
    keymap = {"tap h": "accept", "tap w": "up", "tap s": "down", "tap a": "left", "tap d": "right", "hold h 3": "skip"}

    def run(steps):
        for st in steps:
            if st.startswith("sleep"):
                menu.wait()
            elif st in keymap:
                menu.key(keymap[st])
    n = [0]

    def shot(tag="s", scale=0.5):
        n[0] += 1
        return menu.draw(tmp / f"{n[0]}.png")
    ap.run, ap.shot = run, shot
    ap.running = lambda: not menu.quit
    ap.player_candidates = lambda: {}
    ap.in_gameplay = lambda baseline: "Sim.Player" if menu.screen == "game" else None
    return ap


class AutopilotNavTest(unittest.TestCase):
    def test_dumb_model_that_always_confirms_still_gets_through(self):
        menu = Menu()
        ap = make(menu, lambda prompt, image: ({"action": "accept"}, ""))
        r = ap.reach_gameplay(budget_s=60)
        self.assertTrue(r["ok"], r)
        self.assertFalse(menu.quit)

    def test_model_that_misreads_the_cursor_does_not_loop(self):
        # like the real Qwen run: it believes "Next" is selected and confirms, toggling 1 <-> 2 players
        menu = Menu()
        calls = []

        def ask(prompt, image):
            calls.append(1)
            if menu.screen == "splash":
                return {"action": "wait"}, ""
            if menu.screen == "cutscene":
                return {"action": "skip"}, ""
            if menu.screen == "mode" and menu.count == 2:
                return {"action": "left"}, ""
            return {"action": "accept"}, ""
        r = make(menu, ask).reach_gameplay(budget_s=60)
        self.assertTrue(r["ok"], r)
        self.assertLess(len(calls), 40, "exploration should not ping-pong between two screens")
        self.assertEqual(menu.count, 1, "must start with one player")
        self.assertEqual(menu.diff, 0, "difficulty must not be cycled by blind confirms")
        self.assertNotIn("skip", r["route"][:-1])

    def test_no_vision(self):
        menu = Menu()
        r = make(menu, None, use_vision=False).reach_gameplay(budget_s=60)
        self.assertTrue(r["ok"], r)
        self.assertFalse(menu.quit)

    def test_route_is_loop_free_and_replays(self):
        menu = Menu()
        ap = make(menu, lambda prompt, image: ({"action": "accept"}, ""))
        r = ap.reach_gameplay(budget_s=60)
        menu2 = Menu()
        ap2 = make(menu2, None, use_vision=False)
        ap2.pb["route"] = r["route"]
        r2 = ap2.reach_gameplay(budget_s=5)
        self.assertTrue(r2["ok"] and r2["how"] == "saved route", (r["route"], r2))


if __name__ == "__main__":
    unittest.main()
