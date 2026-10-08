"""Offline test that navigation avoids blind player-count selection (no game, no GPU)."""
import sys
import tempfile
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trainerkit import navigator, play  # noqa: E402


class Sim:
    def __init__(self):
        self.screen, self.cursor, self.toggle, self.dead, self.alive = "splash", 0, 0, False, False

    def press(self, key):
        if self.dead:
            return
        s = self.screen
        if s == "splash":
            pass
        elif s == "title" and key == "h":
            self.screen = "save"
        elif s == "save" and key == "h":
            self.screen = "mode"
        elif s == "mode" and key == "h" and self.cursor == 0:
            self.screen = "cut"
    def hold(self, key, seconds):
        if self.screen == "splash" and key == "h" and seconds >= 1.5:
            self.screen = "title"
        elif self.screen == "cut" and key == "h" and seconds >= 1.5:
            self.screen, self.alive = "game", True
        elif self.screen != "cut":
            self.press(key)

    def image(self, path):
        colors = {"splash": 20, "title": 30, "save": 90, "mode": 150, "cut": 200, "game": 250}
        im = Image.new("L", (160, 90), colors[self.screen])
        marker = list(colors).index(self.screen)
        left, top = marker % 4 * 40, marker // 4 * 45
        for x in range(left, left + 40):
            for y in range(top, top + 45):
                im.putpixel((x, y), 0)
        if self.screen == "mode":  # cursor + toggle drawn so the screen changes a bit
            for x in range(10):
                im.putpixel((140 + x, 10 + self.cursor * 12), 0)
            im.putpixel((5, 5), 255 * self.toggle)
        im.save(path)
        return str(path)


sim = Sim()
def run_steps(proc, steps):
    for step in steps:
        parts = step.split()
        if parts[0] == "tap":
            sim.press(parts[1])
        elif parts[0] == "hold":
            sim.hold(parts[1], float(parts[2]))

play.run = run_steps
play.shot = lambda proc, out, scale=0.5: sim.image(out)
navigator.time.sleep = lambda s: None


class FakeUG:
    process = "Fake.exe"
    def cmd(self, c):
        return "type Game.Player live=1" if sim.alive else "type Game.Player live=0"


keymap = {"FaceAccept": "h", "FaceBack": "u", "Start": "space", "MoveVertical": "w/s", "MoveHorizontal": "a/d"}
print("keys:", navigator.keyset(keymap))
nav = navigator.Navigator(FakeUG(), keymap, Path(tempfile.mkdtemp()), log=print)
ok, route, fatal = nav.explore("Game.Player", budget_s=30, is_running=lambda: not sim.dead)
print("run 1:", ok, "fatal:", fatal is not None)
avoid = [tuple(fatal)] if fatal else []
if not ok:
    sim.__init__()
    ok, route, fatal = nav.explore("Game.Player", budget_s=30, is_running=lambda: not sim.dead, avoid=avoid)
    print("run 2 (after avoiding the quit item):", ok)
assert ok, "navigator failed"
assert not any(step.startswith(("tap s", "tap d", "hold s", "hold d")) for step in route), route
print("route:", route)
sim.__init__()
assert nav.replay(route, "Game.Player", settle=0), "replay failed"
print("replay: OK")
