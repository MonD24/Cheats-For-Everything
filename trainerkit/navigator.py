"""Get from the title screen to gameplay without an LLM round-trip per key press.

Strategy (game-agnostic): the screen is hashed after every action; each distinct screen is a
"state". In a state the navigator tries actions in order (accept, move-down-then-accept, start,
move-right-then-accept, ...). An action that leads to a new state is kept; one that loops back to a
known state (a toggle) or kills the game (a "Quit" item) is marked bad for that state. It stops as
soon as the trainer sees a live player object, and returns the exact step list so it can be saved
and replayed next time.
"""
import time

from PIL import Image

from . import play

ALIASES = {"return": "enter", "escape": "esc", "uparrow": "up", "downarrow": "down",
           "leftarrow": "left", "rightarrow": "right", "keypadenter": "enter", "spacebar": "space"}


def norm(key):
    k = (key or "").lower().replace(" ", "")
    return ALIASES.get(k, k) if ALIASES.get(k, k) in play.SC else None


def keyset(keymap):
    """Pick accept / start / down / right from the game's bindings (Rewired names), with fallbacks."""
    km = {a.lower(): v.split("/") for a, v in (keymap or {}).items()}
    def first(*names, default=None):
        for n in names:
            for k in km.get(n, []):
                if norm(k):
                    return norm(k)
        return default
    vert = [norm(k) for k in km.get("movevertical", []) if norm(k)]
    horiz = [norm(k) for k in km.get("movehorizontal", []) if norm(k)]
    accept = first("faceaccept", "accept", "submit", "confirm", "ui_submit", "select", default="enter")
    back = first("faceback", "back", "cancel", "ui_cancel", default="esc")
    return {"accept": accept, "start": first("start", "pause", "menu", default="enter"),
            "down": vert[1] if len(vert) > 1 else "down", "right": horiz[1] if len(horiz) > 1 else "right",
            "back": back}


def ahash(path):
    im = Image.open(path).convert("L").resize((16, 9))
    px = list(im.getdata())
    avg = sum(px) / len(px)
    return sum(1 << i for i, p in enumerate(px) if p > avg)


def same(a, b, tol=14):
    return bin(a ^ b).count("1") <= tol


class Navigator:
    def __init__(self, ug, keymap, shots_dir, log=print):
        self.ug, self.k, self.dir, self.log = ug, keyset(keymap), shots_dir, log
        self.dir.mkdir(parents=True, exist_ok=True)
        self.n = 0

    def screen(self):
        self.n += 1
        p = play.shot(self.ug.process, self.dir / f"nav_{self.n:03d}.png", 0.25)
        if not p:
            raise RuntimeError("could not capture the game screen")
        return ahash(p)

    def alive(self, player_class):
        out = self.ug.cmd(f"types {player_class}")
        import re
        for full, n in re.findall(r"^type (\S+) live=(\d+)", out, re.M):
            if (full == player_class or full.endswith("." + player_class)) and int(n) > 0:
                return True
        return False

    def actions(self):
        k = self.k
        a = [[f"tap {k['accept']}", "sleep 1.2"],
             [f"hold {k['accept']} 1.5", "sleep 1.2"],
             [f"hold {k['accept']} 3", "sleep 1.2"]]
        return a

    def run_steps(self, steps):
        play.run(self.ug.process, steps)

    def replay(self, route, player_class, settle=20):
        self.log(f"replaying saved route ({len(route)} steps)")
        for s in route:
            try:
                self.run_steps([s])
            except Exception as e:
                self.log(f"replay step failed: {s}: {e}")
                return False
            if self.alive(player_class):
                return True
        t = time.time()
        while time.time() - t < settle:
            if self.alive(player_class):
                return True
            time.sleep(1)
        return False

    def explore(self, player_class, budget_s=300, is_running=lambda: True, avoid=()):
        """Returns (ok, route_steps, fatal) where fatal = [screen_hash, action_index] if an action closed the game.
        avoid: earlier fatal pairs, never tried again."""
        route, states, bad = [], [], {}
        self.fatal = None
        acts = self.actions()
        t0 = time.time()
        cur = self.screen()
        idle_same = 0
        while time.time() - t0 < budget_s:
            if self.alive(player_class):
                self.log("player is live: gameplay reached")
                return True, route, None
            sid = next((i for i, h in enumerate(states) if same(h, cur)), None)
            if sid is None:
                states.append(cur); sid = len(states) - 1
            tried = bad.setdefault(sid, set())
            for h, a_i in avoid:
                if same(h, cur):
                    tried.add(a_i)
            choice = next((i for i in range(len(acts)) if i not in tried), None)
            if choice is None:
                return False, route, None
            steps = acts[choice]
            self.run_steps(steps)
            if not is_running():
                self.log(f"action {steps} closed the game: will avoid it")
                return False, route, [cur, choice]
            new = self.screen()
            if same(new, cur):
                idle_same += 1
                tried.add(choice)              # did nothing here
                if idle_same >= 3 and choice == 0:
                    time.sleep(1.5)             # maybe loading / cutscene
            elif any(same(new, h) for h in states[:sid + 1]) and choice != 0:
                tried.add(choice)              # went back to an earlier screen: a toggle or "back"
                route += steps
            else:
                idle_same = 0
                route += steps
                tried.add(choice) if choice != 0 else None
            cur = new
        return False, route, None
