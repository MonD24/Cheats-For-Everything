"""Autopilot: build a verified trainer for a Unity (Mono) game without an LLM in the loop for every step.

Stages (each one saves what it learned to games/<slug>/playbook.json, so a rerun is fast):
  1. reach_gameplay  replay the saved menu route, else explore: semantic actions from the game's own key
                     map (accept/back/up/down/...), the local vision model picks one per NEW screen,
                     "live player object appeared" (via the plugin) is the success test.
  2. find_values     reflection-level memory scan: snapshot every numeric field of every live game object,
                     rank by name + structure (X with a MaxX sibling = a resource bar), then EXPERIMENT:
                     write a value, check it sticks and that the HUD visibly reacts.
  3. hooks           Harmony method patches for god mode (CanGetHit -> false, TakeDamage -> skip),
                     limited to the player's class so enemies still take damage.
  4. fun             game levers and engine tricks, kept only when an experiment shows an effect
                     (e.g. speed x2 is measured by how far the player moves in 0.6 s).
  5. verify          every cheat is switched on through the plugin and checked; only checked ones ship.

The plugin channel commands used here are documented at the top of unity/UniversalTrainer.cs.
"""
import json
import re
import time
from pathlib import Path

from . import play, screen, vision

# ----------------------------------------------------------------------------- name heuristics
PLAYER_CLASS = re.compile(r"(?i)(player|hero|character|avatar|protagonist)")
NOT_PLAYER = re.compile(r"(?i)ui|menu|select|save|data|input|camera|cam$|spawn|portrait|icon|audio|sound|hud|profile|"
                        r"setting|stat|manager|pref|start|lobby|join|slot|card|name|prefab|pool|vfx|fx$|effect|anim|"
                        r"shadow|marker|indicator|tutorial|dialog|cutscene|cinematic|loader|count|list|info|button|config|"
                        r"trigger|zone|area|volume|collider|detector|sensor|region|inside|follow|inventory|moves|"
                        r"attributes|global|movie|video|global|achievement|unlock|ai$|bot$|npc|enemy|attack")
HEALTH = re.compile(r"(?i)(^|[._])(m_?|_)?(cur(rent)?_?)?(health|hp|hit_?points?|life|lives|vitality|stamina|vigor|hitpoint)(_?hidden|_?current|_?cur|_?value|_?amount)?$")
MONEY = re.compile(r"(?i)money|cash|coins?|gold|bucks|currency|wallet|funds|credits?|gems?|souls?|scrap|dollars?")
XP = re.compile(r"(?i)(^|[._])(m_?|_)?(exp|xp|experience)(points?)?$")
LEVEL = re.compile(r"(?i)(^|[._])(m_?|_)?(player)?level$")
# Words (not substrings: "stamina" contains "min", "property" contains "per") that mark a field as a
# setting, modifier or display copy rather than the live value itself.
NOISE_WORDS = {"max", "min", "start", "starting", "base", "default", "regen", "rate", "percent", "pct", "chance",
               "mult", "multiplier", "scale", "duration", "time", "timer", "delay", "add", "to", "return", "spent",
               "cost", "price", "reward", "give", "drop", "bonus", "threshold", "ratio", "display", "shown", "ui",
               "fx", "sfx", "vfx", "anim", "id", "index", "count", "key", "hash", "debug", "cheat", "old", "prev",
               "last", "target", "save", "initial", "temp", "tween", "lerp", "speed", "offset", "radius", "limit",
               "cap", "inc", "dec", "step", "per", "extra", "loss", "lost", "decrease", "increase", "modifier"}


def words(name):
    """'_maxHPValue' -> {'max', 'hp', 'value'}; 'm_PropertyArray' -> {'property', 'array'}"""
    name = re.sub(r"^m_|^_+|^m(?=[A-Z])", "", name.split("[")[0])
    parts = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", name)
    return {w.lower() for w in parts}


class _Noise:
    @staticmethod
    def search(name):
        return bool(words(name) & NOISE_WORDS)


NOISE = _Noise()
GOD_BOOL_FALSE = re.compile(r"(?i)^(can(get|be|take)(hit|damage|damaged|hurt|attacked|killed)|is(damageable|hittable|vulnerable|killable)|candie)$")
GOD_BOOL_TRUE = re.compile(r"(?i)^is(invincible|invulnerable|immortal|godmode|immune)$")
GOD_VOID_SKIP = re.compile(r"(?i)^(take|apply|receive|deal|on)?(damage|hurt|hit|gethit|takehit)(ed)?$|^(gethit|takedamage|applydamage|receivedamage|hurt|ondamage|ondamaged|onhit|damageevent)$")
SPEED = re.compile(r"(?i)(^|[._])(m_?|_)?(walk|run|move|movement|sprint)_?speed$|speed_?mult(iplier)?$")
JUMP = re.compile(r"(?i)jump_?(force|height|power|velocity|speed|impulse)$")
DEV_FLAG = re.compile(r"(?i)debug|cheat|god|invinc|noclip|unlockall|allunlocked|infinite|nodamage|freecam")


def human(identifier):
    """'_debugIgnoreSpecialValue' -> 'Ignore Special Value' (debug/m prefixes dropped)."""
    ws = [w for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", re.sub(r"^m_|^_+", "", identifier))
          if w.lower() not in ("debug", "dev", "m", "b", "is")]
    return " ".join(w.capitalize() if not w.isupper() else w for w in ws) or identifier


DESCRIPTIONS = {
    "Infinite Health": "health stays where it was when you switched it on",
    "God Mode": "enemies cannot hit you (only your character is affected)",
    "+500 XP": "adds 500 experience",
    "+1000 Money": "adds 1000 money",
    "Super Speed": "doubles walking speed",
    "Slow Motion x0.5": "the whole game runs at half speed",
    "Turbo x2": "the whole game runs twice as fast",
    "Super Jump": "jumps about twice as high",
    "One-Hit Kills": "enemies are left with 1 HP, so any hit finishes them",
    "Freeze Enemies": "enemies stop moving and attacking while it is on",
    "Wide Camera": "zooms the camera out to see more of the level",
}


def describe(c):
    if c["name"] in DESCRIPTIONS:
        return DESCRIPTIONS[c["name"]]
    if c["name"].startswith("Full "):
        return f"keeps the {c['name'][5:].lower()} bar full"
    if c["name"].startswith("Dev Switch:"):
        return f"turns on a developer switch left in the game ({c['member'].split('.')[-1]})"
    return ""


PROMPT_KEYS = {"return": "enter", "escape": "esc", "spacebar": "space", "space bar": "space", "leftshift": "lshift",
               "shift": "lshift", "lshift": "lshift", "ctrl": "lctrl", "control": "lctrl", "leftctrl": "lctrl",
               "alt": "lalt", "leftalt": "lalt", "uparrow": "up", "downarrow": "down", "leftarrow": "left",
               "rightarrow": "right", "del": "delete", "ins": "insert", "bksp": "backspace"}


def prompt_key(text):
    """'[ENTER]', 'Press Space', 'Left Shift', 'E' -> a key play.run can press, else None."""
    from . import play
    k = re.sub(r"[^a-z0-9 ]", "", str(text or "").lower()).strip()
    k = re.sub(r"^(press|hold|tap|key)\s+", "", k).replace(" key", "").strip()
    k = PROMPT_KEYS.get(k.replace(" ", ""), PROMPT_KEYS.get(k, k.replace(" ", "")))
    return k if k in play.SC else None


def hotkey(i):
    """F1..F12, then Ctrl+F1..Ctrl+F12."""
    return f"F{i + 1}" if i < 12 else f"CTRL+F{i - 11}"


def num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def fmt(v):
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return repr(v) if isinstance(v, float) else str(v)


class Autopilot:
    def __init__(self, ug, gdir, keys=None, log=print, ask=vision.ask, use_vision=True):
        self.ug, self.gdir, self.log, self.ask = ug, Path(gdir), log, ask
        self.use_vision = use_vision
        self.gdir.mkdir(parents=True, exist_ok=True)
        self.shots = self.gdir / "autopilot_shots"
        self.shots.mkdir(exist_ok=True)
        self.keys = keys or {}
        self.k = self._keyset(self.keys)
        self.pb = self._load("playbook.json", {})
        self.report = {"stages": {}, "cheats": [], "skipped": []}
        self._n = 0

    # ------------------------------------------------------------------------- files
    def _load(self, name, default):
        p = self.gdir / name
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def save_playbook(self, **upd):
        self.pb.update(upd)
        self.pb["updated"] = time.strftime("%Y-%m-%d %H:%M")
        (self.gdir / "playbook.json").write_text(json.dumps(self.pb, indent=1, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------------- input / screen
    @staticmethod
    def _keyset(keymap):
        """Semantic keys from the game's bindings: Rewired ('FaceAccept': 'h', 'MoveVertical': 'w/s') or the
        new Input System ('UI.Submit': 'enter', 'Player.Move.up': 'w'); keyboard defaults otherwise."""
        from .navigator import norm
        km = {a.lower(): [norm(x) for x in v.split("/") if norm(x)] for a, v in (keymap or {}).items()}

        def find(words, prefer=("ui.", "menu")):
            hits = [(name, ks) for name, ks in km.items() if ks and name.split(".")[-1] in words]
            hits.sort(key=lambda h: not any(h[0].startswith(p) for p in prefer))
            return hits[0][1][0] if hits else None

        def part(direction, axis, idx, ui=True):
            for name, ks in sorted(km.items(), key=lambda kv: (not kv[0].startswith("ui.")) if ui else kv[0].startswith("ui.")):
                segs = name.split(".")
                if ks and segs[-1] == direction and len(segs) > 1 and segs[-2] in ("navigate", "move", "movement", "nav", "dpad"):
                    return ks[0]
            ks = km.get(axis, [])
            return ks[idx] if len(ks) > idx else None
        return {
            "accept": find(("submit", "accept", "confirm", "faceaccept", "select", "ok")) or "enter",
            "back": find(("cancel", "back", "faceback", "return")) or "esc",
            "start": find(("start", "pause", "menu", "options"), prefer=("player", "game")) or "enter",
            "up": part("up", "movevertical", 0) or "up", "down": part("down", "movevertical", 1) or "down",
            "left": part("left", "movehorizontal", 0) or "left", "right": part("right", "movehorizontal", 1) or "right",
            "attack": find(("quickattack", "attack", "fire", "shoot", "lightattack", "hit", "punch", "primary"),
                           prefer=("player", "game")) or "j",
            "jump": find(("jump",), prefer=("player", "game")),
            # walking in gameplay (Input System games often map menus to arrows and walking to WASD)
            "move_up": part("up", "movevertical", 0, ui=False) or "up",
            "move_down": part("down", "movevertical", 1, ui=False) or "down",
            "move_left": part("left", "movehorizontal", 0, ui=False) or "left",
            "move_right": part("right", "movehorizontal", 1, ui=False) or "right",
        }

    ACTIONS = ("accept", "back", "up", "down", "left", "right", "start", "skip", "wait",
               "accept_enter", "accept_space", "back_esc", "arrow_up", "arrow_down", "arrow_left", "arrow_right")
    CONFIRMS = ("accept", "accept_enter", "accept_space", "start")   # not "skip": holding through a video is fine
    SAFE_ORDER = ("wait", "accept", "down", "right", "up", "left", "skip", "start", "accept_enter",
                  "accept_space", "arrow_down", "arrow_right", "arrow_up", "arrow_left", "back", "back_esc")

    def steps_for(self, action):
        k = self.k
        if action.startswith(("key:", "hold:")):
            kind, key = action.split(":", 1)
            return [f"tap {key}"] if kind == "key" else [f"hold {key} 2.5"]
        return {"accept": [f"tap {k['accept']}"], "back": [f"tap {k['back']}"], "start": [f"tap {k['start']}"],
                "up": [f"tap {k['up']}"], "down": [f"tap {k['down']}"], "left": [f"tap {k['left']}"],
                "right": [f"tap {k['right']}"], "skip": [f"hold {k['accept']} 3"], "wait": ["sleep 3"],
                "accept_enter": ["tap enter"], "accept_space": ["tap space"], "back_esc": ["tap esc"],
                "arrow_up": ["tap up"], "arrow_down": ["tap down"], "arrow_left": ["tap left"],
                "arrow_right": ["tap right"]}[action]

    def focus(self):
        """Bring the game to the front: many games stop rendering (or pause) while unfocused, and then
        no experiment can show a visible effect."""
        try:
            play.focus(self.ug.process)
        except Exception:
            pass

    def shot(self, tag="s", scale=0.5):
        self._n += 1
        return play.shot(self.ug.process, self.shots / f"{self._n:04d}_{tag}.png", scale)

    def run(self, steps):
        play.run(self.ug.process, steps)

    def running(self):
        import psutil
        return any((p.info["name"] or "").lower() == self.ug.process.lower() for p in psutil.process_iter(["name"]))

    # ------------------------------------------------------------------------- plugin helpers
    def cmd(self, *lines, timeout=20):
        return self.ug.cmd(*lines, timeout=timeout)

    def live_counts(self, word):
        out = self.cmd(f"types {word}")
        return {full: int(n) for full, n in re.findall(r"^type (\S+) live=(\d+)", out, re.M)}

    def player_candidates(self):
        counts = {}
        for word in ("Player", "Hero", "Character", "Avatar"):
            counts.update(self.live_counts(word))
        return {c: n for c, n in counts.items()
                if PLAYER_CLASS.search(c.split(".")[-1]) and not NOT_PLAYER.search(c.split(".")[-1].replace("Player", "").replace("Character", "").replace("Hero", ""))}

    def values(self, tag, filt):
        """{key: (type, value)} from snapshot `tag` whose key contains any of filt ('a|b')."""
        out = self.cmd(f"diff {tag} {tag} unchanged {filt}")
        rows = {k: (t, float(v)) for k, t, v, _ in re.findall(r"^d (\S+) (\S+) (\S+) (\S+)$", out, re.M)}
        if filt.endswith("#") and "|" not in filt:
            # 'Player#' is a class filter: exact class, not every '...Player#' (SoundPlayer, AudioPlayer)
            rows = {k: v for k, v in rows.items() if k.split("#", 1)[0] == filt[:-1]}
        return rows

    @staticmethod
    def split_key(key):
        """'Game.Player#-13164|_stats.Exp' -> ('Game.Player', '-13164', '_stats.Exp')"""
        head, path = key.split("|", 1)
        cls, inst = head.rsplit("#", 1)
        return cls, inst, path

    def get(self, cls, path):
        v = self.ug.get(cls, path)
        return v

    def set(self, cls, path, value):
        out = self.cmd(f"set {cls} {path} {value}")
        m = re.search(r"set on (\d+)", out)
        return bool(m and int(m.group(1)) > 0)

    # ------------------------------------------------------------------------- stage 1: gameplay
    def player_score(self, cls):
        """How much a live class looks like the controllable player: health-like fields, bars with a
        maximum, many numeric fields. 0 = not a player (no stats at all)."""
        self.cmd("snap who", timeout=60)
        rows = self.values("who", cls + "#")
        if not rows:
            return 0, 0
        inst = self.split_key(next(iter(rows)))[1]
        paths = {self.split_key(k)[2].lower() for k in rows if self.split_key(k)[1] == inst}
        leafs = {p.split(".")[-1] for p in paths}
        health = any(HEALTH.search(p) and not NOISE.search(p.split(".")[-1]) for p in paths)
        bars = sum(1 for l in leafs if f"max{l}" in leafs or f"{l}max" in leafs)
        score = (5 if health else 0) + 3 * min(bars, 2) + min(len(paths), 100) / 25
        short = cls.split(".")[-1]
        if re.fullmatch(r"(?i)(the)?(player|hero|character|avatar)(controller|character|entity|unit|pawn)?\d*", short):
            score += 2
        return (score if health or bars else 0), len(paths)

    def time_running(self):
        try:
            return (num(self.get("UnityEngine.Time", "timeScale")) or 0) > 0
        except Exception:
            return True

    def in_gameplay(self, baseline):
        pc = self.pb.get("player_class")
        if pc:
            # a game-over or pause screen keeps the player object but stops time
            if not (self.live_counts(pc.split(".")[-1]).get(pc, 0) > 0 and self.time_running()):
                return None
            if self.pb.get("gameplay_check") == "vision" and self.use_vision:
                # this game keeps its player object alive in the menus too: the screen decides
                return pc if self.looks_like_gameplay() else None
            return pc
        fresh = [c for c, n in self.player_candidates().items() if n > 0 and baseline.get(c, 0) == 0]
        if fresh:
            scored = sorted(((self.player_score(c), c) for c in fresh), reverse=True)
            self.log(f"  player candidates: {[(c, round(sc[0], 1)) for sc, c in scored]}")
            best_score, best = scored[0]
            if best_score[0] >= 5 and self.time_running():
                return best
        # games without health bars, or whose player object already lives in the menu: ask the vision model
        # whether this is gameplay, then the player is whatever moves with the movement keys
        if not self.use_vision or not self.time_running():
            return None
        frame_path = self.shot("gp")
        frame = screen.load(frame_path)
        if getattr(self, "_gp_seen", None) is not None and not screen.changed(frame, self._gp_seen):
            return None
        self._gp_seen = frame
        if not self.vision_gameplay(frame_path):
            self._gp_seen = None if getattr(self, "_gp_seen", None) is frame else self._gp_seen
            return None
        mover = self.find_mover()
        self.log(f"  vision says gameplay; object moved by the movement keys: {mover}")
        if mover:
            self.save_playbook(gameplay_check="vision")
        return mover

    def looks_like_gameplay(self):
        """Vision check with a per-frame cache (the same picture is not asked about twice)."""
        frame_path = self.shot("gp")
        frame = screen.load(frame_path)
        cached = getattr(self, "_gp_cache", None)
        if cached and not screen.changed(frame, cached[0]):
            return cached[1]
        ok = self.vision_gameplay(frame_path)
        self._gp_cache = (frame, ok)
        return ok

    PART = re.compile(r"(?i)bone|collider|renderer|animator|anim|ik$|physics|rig|ragdoll|cloth|trail|particle|"
                      r"footstep|sfx|vfx|effect|sprite|mesh|look|aim|attach|socket|joint|spring|wobble|jiggle")
    CAMERA = re.compile(r"(?i)cam|follow|parallax|background|ui|hud|cursor|pointer|shadow|light|audio|sound")

    def find_mover(self):
        """Class of the object that walks right on move_right and back on move_left (the player)."""
        self.focus()
        self.cmd("possnap m0")
        self.run([f"hold {self.k['move_right']} 0.5", "sleep 0.2"])
        self.cmd("possnap m1")
        self.run([f"hold {self.k['move_left']} 0.5", "sleep 0.2"])
        out1 = self.cmd("possnap m2", "posdiff m0 m1")
        out2 = self.cmd("posdiff m1 m2")
        moved = lambda out: {k: float(d) for k, d in re.findall(r"^moved (\S+) (\S+)", out, re.M)}
        a, b = moved(out1), moved(out2)
        both = [(min(a[k], b[k]), k.split("#")[0]) for k in a if k in b
                and not self.CAMERA.search(k.split("#")[0].split(".")[-1]) and not self.PART.search(k.split("#")[0].split(".")[-1])]
        if not both:
            return None
        top = max(d for d, _ in both)
        group = sorted({c for d, c in both if d >= top * 0.5})   # every script on the player moves alike
        self.save_playbook(player_parts=group[:12])
        # the main script: named like a player, else the one carrying the most game fields
        named = [c for c in group if PLAYER_CLASS.search(c.split(".")[-1]) and not NOT_PLAYER.search(c.split(".")[-1])]
        if named:
            return min(named, key=len)
        self.cmd("snap mv", timeout=60)
        sizes = {c: len(self.values("mv", c + "#")) for c in group[:12]}
        self.log(f"  movers: {sizes}")
        return max(sizes, key=sizes.get) if sizes else None

    def reach_gameplay(self, budget_s=900, save_route=True):
        t0 = time.time()
        self._save_route = save_route
        baseline = {} if self.pb.get("player_class") else self.player_candidates()
        # started mid-game? then the player is among the live classes right now
        found = self.in_gameplay({}) if baseline else None
        if found:
            return self._gameplay_found(found, [], "already in gameplay")
        self.log(f"menu-time player-like classes (not the player): {sorted(c for c, n in baseline.items() if n)}")
        found = self.in_gameplay(baseline)
        if found:
            return self._gameplay_found(found, [], "already in gameplay")
        route = self.pb.get("route") or []
        if route:
            found = self.replay(route, baseline)
            if found:
                return self._gameplay_found(found, route, "saved route")
            self.log("saved route did not reach gameplay; exploring from here")
        # Menu graph. A state is one exact picture (cursor position and option values included); an action
        # that leads back to an already seen state is a toggle or a "back" -> banned in that state, so
        # A --accept--> B --left--> A loops cannot repeat. route_to[state] = loop-free actions to reach it.
        frames, bad, notes, route_to, waits, toggle_pages = [], {}, {}, {}, {}, []
        history = []

        def state_of(path):
            f = screen.load(path)
            for i, g in enumerate(frames):
                if screen.changed_pct(f, g) < 1.0:
                    return i
            frames.append(f)
            return len(frames) - 1

        prev = self.shot("nav")
        cur_state = state_of(prev)
        route_to[cur_state] = []
        while time.time() - t0 < budget_s:
            found = self.in_gameplay(baseline)
            if found:
                return self._gameplay_found(found, route_to.get(cur_state, []), "explored")
            if not self.running():
                return {"ok": False, "error": "the game closed during navigation", "route": route_to.get(cur_state, [])}
            banned = bad.setdefault(cur_state, set())
            # confirm only changed a value on this page (an option row) and the cursor has not moved since:
            # confirming again would just cycle the option (difficulty Normal -> Hard -> Easy ...)
            on_toggle_page = any(screen.same_screen(frames[cur_state], t) for t in toggle_pages)
            last = history[-1]["action"] if history else ""
            hold = set(self.CONFIRMS) if on_toggle_page and last in self.CONFIRMS + ("left", "right", "arrow_left", "arrow_right") else set()
            action, why = self.choose_action(prev, history[-6:], banned | hold, notes.get(cur_state, []))
            if on_toggle_page and action in self.CONFIRMS and "down" not in banned:
                # confirming on an option page cycles values; its Next/Start/OK button is at the bottom
                action, why = "down", f"option page: walk to the bottom button instead of '{action}'"
            if action is None:
                return {"ok": False, "error": "no untried action left on this screen", "route": route_to.get(cur_state, []),
                        "screen": prev, "history": history[-10:]}
            self.run(self.steps_for(action) + ["sleep 1.0"])
            cur = self.shot("nav")
            nxt = state_of(cur)
            if nxt == cur_state:
                effect = "no visible change"
                waits[cur_state] = waits.get(cur_state, 0) + (action == "wait")
                if action != "wait" or waits[cur_state] >= 3:   # a static screen does not change by waiting
                    banned.add(action)
            elif nxt in route_to:
                effect = "went back to an earlier screen (toggle/back)"
                banned.add(action)
            else:
                effect = "new screen"
                route_to[nxt] = route_to[cur_state] + [{"action": action, "frame": screen.encode(frames[cur_state])}]
            if action in self.CONFIRMS and nxt != cur_state and screen.same_screen(frames[cur_state], frames[nxt]):
                toggle_pages.append(frames[cur_state])
                effect += "; only a value on this page changed (option row), move the cursor"
            notes.setdefault(cur_state, []).append(f"{action}: {effect}")
            history.append({"action": action, "why": why, "effect": effect})
            self.log(f"  nav: s{cur_state}->s{nxt} {action:<12} {effect[:22]:<22} {why[:80]}")
            prev, cur_state = cur, nxt
        return {"ok": False, "error": f"gameplay not reached in {budget_s}s", "route": route_to.get(cur_state, []),
                "history": history[-10:]}

    def replay(self, route, baseline, limit_s=240):
        """Saved route = [{"action", "frame"}]: do a step when the screen looks like the frame it was taken
        on. Timing (logos, loading) and remembered settings (a menu that already shows "1 player") do not
        matter: the first not-yet-done step whose frame matches the current picture is the one to do."""
        steps = [r if isinstance(r, dict) else {"action": r, "frame": None} for r in route]
        frames = [screen.decode(st["frame"]) if st.get("frame") else None for st in steps]
        self.log(f"replaying saved route: {len(steps)} steps")
        i, idle, t0 = 0, 0, time.time()
        while i < len(steps) and time.time() - t0 < limit_s:
            found = self.in_gameplay(baseline)
            if found:
                return found
            if frames[i] is None:                      # old route format: blind, one step at a time
                self.run(self.steps_for(steps[i]["action"]) + ["sleep 1.5"]); i += 1
                continue
            cur = screen.load(self.shot("replay"))
            # closest remaining step; animated menus (fire, idle characters) drift a few % between frames,
            # menu pages that differ only by cursor position are 2-6% apart, so take the minimum, not the first
            diffs = [(screen.changed_pct(cur, frames[j]), j) for j in range(i, len(steps)) if frames[j] is not None]
            diffs.sort()
            best = diffs[0] if diffs else (100, None)
            second = diffs[1][0] if len(diffs) > 1 else 100
            # near-identical, or clearly the closest (animated title screens drift 10-20% from themselves)
            j = best[1] if best[0] < 6.0 or (best[0] < 30.0 and second - best[0] >= 15.0) else None
            if j is None:
                idle += 1
                if idle > 20:                           # ~45 s on screens the route never saw (logos, videos)
                    return None
                self.run(["sleep 2"])
                continue
            idle = 0
            self.run(self.steps_for(steps[j]["action"]) + ["sleep 1.2"])
            i = j + 1
        t1 = time.time()
        while time.time() - t1 < 20:                  # last step is often "skip the intro"; loading follows
            found = self.in_gameplay(baseline)
            if found:
                return found
            self.run(["sleep 2"])
        return None

    def choose_action(self, image, history, bad, here=()):
        allowed = [a for a in self.ACTIONS if a not in bad]
        if not allowed:
            return None, ""
        scene = ""
        try:
            scene = self.ug.scene() or ""
        except Exception:
            pass
        if re.search(r"(?i)cinematic|cutscene|intro|splash|logo|movie|video", scene) and "skip" in allowed and \
                not any(h["action"] == "skip" and h["effect"] == "no visible change" for h in history[-2:]):
            return "skip", f"scene '{scene}' looks like a cutscene"
        if not self.use_vision:
            return next(a for a in self.SAFE_ORDER if a in allowed), "no vision model: deterministic exploration"
        hist = "; ".join(f"{h['action']} -> {h['effect']}" for h in history) or "none"
        prompt = (
            "You control a PC game's menus with the keyboard. Goal: reach real gameplay with ONE player "
            "(single-player), by starting a new game or continuing.\n"
            "Rules: never select Quit/Exit/Delete/Erase/Overwrite/Credits/Online; if a player-count option "
            "shows 2 or more players, change it to 1 player (left/right) BEFORE confirming; leave difficulty "
            "and other settings as they are (Normal); confirming an option row may cycle its value, so move "
            "to the Next/Start/OK item instead; skip intros, logos and cutscenes; pick the first empty/new save slot.\n"
            f"Unity scene: {scene or 'unknown'}. Recent actions (oldest first): {hist}.\n"
            f"Already tried on THIS exact screen: {'; '.join(here) or 'nothing'}. If confirming here only "
            "toggled a value, the cursor is on an option row, not on the Next/Start item: move with up/down.\n"
            f"Allowed actions: {', '.join(allowed)}. 'skip' holds confirm for 3 s, 'wait' waits 3 s. "
            "accept/back/up/down/left/right use the game's own key bindings; *_enter/*_space/*_esc/arrow_* are "
            "plain keyboard keys for menus that ignore the game bindings.\n"
            'If the screen shows a key prompt (e.g. "[ENTER] OK", "Press Space to skip", "Hold E to skip"), you may '
            'instead answer "key:<that key>" or "hold:<that key>".\n'
            'Reply with JSON only: {"screen": "<what this screen is>", "highlighted": "<selected item>", '
            '"action": "<one allowed action>", "why": "<short reason>"}')
        ans, raw = self.ask(prompt, image)
        action = self.normal_action((ans or {}).get("action"), bad)
        if action not in allowed and not (action or "").startswith(("key:", "hold:")):
            why_not = (f"'{action}' is not allowed here: it was already tried on this screen with no progress. "
                       "Look again at which item is really highlighted and pick a different action. ") if action else ""
            ans, raw = self.ask(prompt + f"\n{why_not}Allowed: {', '.join(allowed)}. Reply with the JSON object only.", image)
            action = (ans or {}).get("action")
        if action not in allowed:
            # never confirm blindly: an unread menu may be Quit, a difficulty toggle or "overwrite save"
            self.log(f"  vision gave no usable action ({str(raw)[:120]}); waiting instead")
            # deterministic order: confirm what the game pre-selects (usually Start/New game) before moving the
            # cursor - moving first could land on Quit
            return next((a for a in self.SAFE_ORDER if a in allowed), allowed[0]), "no usable vision answer"
        return action, f"{(ans or {}).get('screen', '')} | {(ans or {}).get('highlighted', '')} | {(ans or {}).get('why', '')}"

    @staticmethod
    def normal_action(action, bad):
        """'key:[ENTER]' -> 'key:enter'; unknown keys and banned prompt keys -> None."""
        if isinstance(action, str) and action.startswith(("key:", "hold:")):
            kind, raw = action.split(":", 1)
            key = prompt_key(raw)
            a = f"{kind}:{key}" if key else None
            return None if a in bad else a
        return action

    def vision_gameplay(self, frame_path):
        """Is this controllable gameplay? A popup over the game (tutorial box, '[ENTER] OK', reward screen)
        is dismissed with the key its prompt names, and the answer is 'not yet'."""
        ans, _ = self.ask('Is this live gameplay: the player character is visible and controllable, with NO popup, '
                          'dialog box, tutorial window, menu, logo, loading screen or cutscene on top? If a popup or '
                          'message box shows a key prompt (e.g. "[ENTER] OK", "Press E to continue"), give that key. '
                          'JSON only: {"gameplay": true|false, "popup_key": "<key or null>"}', frame_path)
        key = prompt_key((ans or {}).get("popup_key"))
        if key and (ans or {}).get("gameplay") is not True:
            self.log(f"  popup over the game: pressing {key}")
            self.run([f"tap {key}", "sleep 1.2"])
            return False
        if key:
            self.log(f"  popup ({key}) reported together with gameplay: dismissing it first")
            self.run([f"tap {key}", "sleep 1.2"])
            return False
        return (ans or {}).get("gameplay") is True

    def _gameplay_found(self, player_class, route, how):
        self.log(f"gameplay reached ({how}); player class = {player_class}")
        upd = {"player_class": player_class}
        if route and self._save_route:
            upd["route"] = route
        self.save_playbook(**upd)
        self.report["stages"]["gameplay"] = {"ok": True, "how": how, "player_class": player_class, "route": route}
        return {"ok": True, "player_class": player_class, "route": route, "how": how}

    # ------------------------------------------------------------------------- experiments
    def visible_effect(self, apply, restore=None, settle=0.8):
        """Did `apply` change the picture beyond background animation? Returns the number of grid cells
        that changed after apply AND changed back after restore, outside a noise mask built from three
        idle frames (dilated by one cell, so walking NPCs and flickering lights do not count)."""
        self.focus()
        idle = [self.shot("fx_idle")]
        for _ in range(2):
            time.sleep(0.25)
            idle.append(self.shot("fx_idle"))
        noise = set()
        for x, y in [(0, 1), (1, 2), (0, 2)]:
            noise |= screen.changed_cells(idle[x], idle[y])
        noise = {(i + dx, j + dy) for i, j in noise for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
        apply()
        time.sleep(settle)
        after = self.shot("fx_on")
        eff = screen.changed_cells(idle[-1], after) - noise
        if restore:
            restore()
            time.sleep(settle)
            eff &= screen.changed_cells(after, self.shot("fx_off"))
        return len(eff)

    def hud_number(self, what):
        """Ask the vision model to read a number off the HUD (money, ammo...). None if it cannot."""
        if not self.use_vision:
            return None
        self.focus()
        img = self.shot("hud", 0.75)
        try:
            ans, _ = self.ask(f'Read the {what} amount shown on the game HUD. JSON only: {{"{what}": <number or null>}}', img, width=1280)
        except TypeError:                     # test doubles without the width parameter
            ans, _ = self.ask(f'Read the {what} amount shown on the game HUD. JSON only: {{"{what}": <number or null>}}', img)
        return num((ans or {}).get(what))

    def probe_slot(self, cls, path, typ, value, new_value, check_visible=True, refresh=""):
        """Write new_value, check it sticks and whether the screen reacts; restore the original.
        refresh: 'Class Method' the game uses to redraw the HUD (called after every write)."""
        orig = fmt(value)
        res = {"class": cls, "path": path, "type": typ, "value": value}
        holder = {}

        def apply():
            holder["set"] = self.set(cls, path, fmt(new_value))
            if refresh:
                self.cmd(f"call {refresh}")

        def restore():
            self.set(cls, path, orig)
            if refresh:
                self.cmd(f"call {refresh}")
        if check_visible:
            res["visible_cells"] = self.visible_effect(apply, restore)
            self.set(cls, path, fmt(new_value))
        else:
            apply()
        time.sleep(0.6)
        after = num(self.get(cls, path))
        res["sticks"] = after is not None and abs(after - new_value) < 1e-3
        res["written"] = bool(holder.get("set", True))
        self.set(cls, path, orig)
        return res

    # ------------------------------------------------------------------------- stage 2: values
    def hud_trace(self):
        """(fields feeding the HUD money label, 'Class Method' that redraws the HUD) via IL scan; cached."""
        if hasattr(self, "_hud"):
            return self._hud
        # follow the HUD: fields read right before the game calls its money label setter (IL scan)
        hud_fields, refresh = set(), ""
        try:
            from . import il_trace
            src = il_trace.hud_sources(self.ug.dir)
            hud_fields = set(src["fields"])
            self.log(f"HUD money label fed by: {src['call_sites']} reads {src['fields'][:8]}")
            # the method that calls the label setter redraws the HUD; usable when it takes no arguments
            for site in src["call_sites"]:
                caller = site.split(" -> ")[0]
                if "<" in caller:
                    continue
                cls_name, meth = caller.rsplit(".", 1)
                if re.search(rf"^method \S+\.{re.escape(meth)}\(\) : Void", self.cmd(f"methods {cls_name} {meth}"), re.M):
                    refresh = f"{cls_name} {meth}"
                    break
            self.log(f"HUD refresh method: {refresh or 'none (HUD must update by itself)'}")
        except Exception as e:
            self.log(f"IL trace unavailable: {e}")
        self._hud = (hud_fields, refresh)
        return self._hud

    def find_values(self):
        pc = self.pb["player_class"]
        for attempt in range(4):
            self.cmd("snap ap0", timeout=60)
            mine = self.values("ap0", pc + "#")
            # several objects right after a respawn (the old one is still being destroyed): take the fullest
            per_inst = {}
            for k in mine:
                per_inst.setdefault(self.split_key(k)[1], []).append(k)
            inst = max(per_inst, key=lambda i: len(per_inst[i])) if per_inst else None
            player = {self.split_key(k)[2]: mine[k] for k in per_inst.get(inst, [])}
            if any(HEALTH.search(p) for p in player) or (len(player) >= 20 and len(per_inst) <= 2):
                break
            self.log(f"  player stats not ready ({len(player)} fields, {len(per_inst)} objects); retrying")
            time.sleep(3)
        if not player:
            return {"ok": False, "error": f"no numeric fields on the live {pc}"}
        found = {}
        _, refresh = self.hud_trace()        # HUDs that redraw only on events need this after a write

        # resource bars: X with a MaxX sibling (special, mana, ammo...); health is one of them or a name match
        bars = []
        lower = {p.lower(): p for p in player}
        for path, (t, v) in player.items():
            leaf = path.split(".")[-1]
            base = path[: -len(leaf)]
            for mx in (f"max{leaf}", f"{leaf}max", f"max_{leaf}", f"{leaf}_max", f"m_max{leaf.lstrip('m_')}"):
                full = (base + mx).lower()
                if full in lower and t != "Boolean" and v >= 0:
                    bars.append({"path": path, "type": t, "value": v, "max_path": lower[full],
                                 "max": player[lower[full]][1]})
                    break
        health = [{"path": p, "type": t, "value": v} for p, (t, v) in player.items()
                  if t != "Boolean" and v > 0 and HEALTH.search(p) and not NOISE.search(p.split(".")[-1])]
        health.sort(key=lambda c: (("hidden" not in c["path"].lower()), ("." in c["path"]), -c["value"]))
        self.log(f"health candidates: {[c['path'] for c in health][:8]}; bars: {[b['path'] + '/' + b['max_path'] for b in bars]}")

        # health: the candidate whose knock-down shows on screen and that the game does not overwrite
        for c in health[:6]:
            r = self.probe_slot(pc, c["path"], c["type"], c["value"], max(1.0, c["value"] // 3 if c["type"] != "Single" else c["value"] / 3), refresh=refresh)
            self.log(f"  health probe {c['path']}: visible={r.get('visible_cells')} sticks={r['sticks']}")
            if r.get("visible_cells", 0) > 0 and r["sticks"]:
                found["health"] = {**c, "probe": r}
                break
            found.setdefault("health_weak", {**c, "probe": r})
        if "health" not in found and "health_weak" in found:
            found["health"] = found.pop("health_weak")
            found["health"]["weak"] = True
        found.pop("health_weak", None)

        for b in bars:
            if found.get("health", {}).get("path") == b["path"]:
                continue
            r = self.probe_slot(pc, b["path"], b["type"], b["value"], b["max"] if b["value"] != b["max"] else max(0, b["max"] // 2), refresh=refresh)
            self.log(f"  bar probe {b['path']}: visible={r.get('visible_cells')} sticks={r['sticks']}")
            if r["sticks"]:
                found.setdefault("bars", []).append({**b, "probe": r})

        # experience / level on the player (incl. nested stat objects)
        for name, rx in (("xp", XP), ("level", LEVEL)):
            cands = [{"path": p, "type": t, "value": v} for p, (t, v) in player.items()
                     if t != "Boolean" and rx.search(p) and not NOISE.search(p.split(".")[-1])]
            for c in cands[:3]:
                r = self.probe_slot(pc, c["path"], c["type"], c["value"], c["value"] + (100 if name == "xp" else 1), refresh=refresh)
                self.log(f"  {name} probe {c['path']}: visible={r.get('visible_cells')} sticks={r['sticks']}")
                if r["sticks"]:
                    found[name] = {**c, "probe": r}
                    break

        # money: anywhere (player, singletons, statics, per-character arrays); the HUD decides
        # The wallet belongs to the player, a singleton (one live object) or a static field: a class with
        # many live objects holding "coins" is loot (what an enemy drops), not the purse.
        hud_fields, refresh = self.hud_trace()
        rows = self.values("ap0", "money|cash|coin|gold|bucks|currency|wallet|funds|credit|gem")
        instances = {}
        for k in rows:
            cls, inst_id, _ = self.split_key(k)
            instances.setdefault(cls, set()).add(inst_id)
        cands = []
        for k, (t, v) in rows.items():
            cls, inst_id, path = self.split_key(k)
            leaf = path.split(".")[-1].split("[")[0]
            if t == "Boolean" or not MONEY.search(path) or v < 0 or any(NOISE.search(part) for part in path.split(".")):
                continue
            if (cls == pc and inst_id != inst) or (cls != pc and len(instances[cls]) > 1):
                continue
            idx = re.search(r"\[(\d+)\]$", path)
            parts = [x.split("[")[0] for x in path.split(".")]
            cands.append({"class": cls, "path": path, "type": t, "value": v,
                          "score": (2 if cls == pc else 0) + (1 if MONEY.search(leaf) else 0) + (1 if v > 0 else 0)
                                   + (5 if parts[-1] in hud_fields else 0) + (2 if len(set(parts[:-1]) & hud_fields) else 0)
                                   - (int(idx.group(1)) * 0.1 if idx else 0) - sum(int(n) for n in re.findall(r"\[(\d+)\]", path)) * 0.1})
        cands.sort(key=lambda c: -c["score"])
        self.log(f"money candidates: {[c['class'] + ' ' + c['path'] for c in cands][:10]}")
        shown = self.hud_number("money")
        self.log(f"  HUD money (vision): {shown}")
        if shown is not None:   # the exact number on screen beats name guesses
            exact = [c for c in cands if abs(c["value"] - shown) < 0.01]
            if 0 < len(exact) < len(cands):
                cands = exact + [c for c in cands if c not in exact]
        for c in cands[:12]:
            r = self.probe_slot(c["class"], c["path"], c["type"], c["value"], c["value"] + 1234, refresh=refresh)
            if r["sticks"] and r.get("visible_cells", 0) > 0 and shown is not None:
                self.set(c["class"], c["path"], fmt(c["value"] + 1234))
                if refresh:
                    self.cmd(f"call {refresh}")
                time.sleep(0.6)
                read = self.hud_number("money")
                self.set(c["class"], c["path"], fmt(c["value"]))
                if refresh:
                    self.cmd(f"call {refresh}")
                r["hud_after"] = read
                r["hud_confirms"] = read is not None and abs(read - (shown + 1234)) < 1.5
            self.log(f"  money probe {c['class']} {c['path']}: visible={r.get('visible_cells')} sticks={r['sticks']} hud={r.get('hud_after')}")
            if r["sticks"] and r.get("visible_cells", 0) > 0 and r.get("hud_confirms", shown is None):
                found["money"] = {**c, "probe": r, "refresh": refresh}
                break
        self.report["stages"]["values"] = {k: {kk: vv for kk, vv in v.items() if kk != "probe"} if isinstance(v, dict) else v
                                          for k, v in found.items()}
        self.save_playbook(values={k: (v if not isinstance(v, dict) else {kk: vv for kk, vv in v.items() if kk != "probe"})
                                   for k, v in found.items()})
        self.found = found
        return {"ok": True, "found": found, "player_fields": len(player)}

    # ------------------------------------------------------------------------- stage 3: hooks
    def find_hooks(self):
        pc = self.pb["player_class"]
        out = self.cmd(f"methods {pc}")
        hooks = []
        for decl, name, params, ret, static in re.findall(r"^method (\S+)\.(\w+)\(([^)]*)\) : (\w+)( static)?$", out, re.M):
            if static:
                continue
            if ret == "Boolean" and GOD_BOOL_FALSE.match(name):
                hooks.append({"method": name, "mode": "return_false", "declared": decl})
            elif ret == "Boolean" and GOD_BOOL_TRUE.match(name):
                hooks.append({"method": name, "mode": "return_true", "declared": decl})
            elif ret == "Void" and params and GOD_VOID_SKIP.match(name):
                hooks.append({"method": name, "mode": "skip", "declared": decl})
        seen, uniq = set(), []
        for h in hooks:
            if (h["method"], h["mode"]) not in seen:
                seen.add((h["method"], h["mode"])); uniq.append(h)
        self.log(f"god-mode hook candidates on {pc}: {[(h['method'], h['mode']) for h in uniq]}")
        self.report["stages"]["hooks"] = uniq
        return uniq

    # ------------------------------------------------------------------------- stage 4: fun
    def pos(self):
        out = self.cmd(f"pos {self.pb['player_class']}")
        m = re.search(r"^pos (\S+) (\S+) (\S+)", out, re.M)
        return tuple(float(x) for x in m.groups()) if m else None

    def move_distance(self, key, secs=0.6):
        p0 = self.pos()
        self.run([f"hold {key} {secs}", "sleep 0.3"])
        p1 = self.pos()
        if not p0 or not p1:
            return None
        return ((p1[0] - p0[0]) ** 2 + (p1[1] - p0[1]) ** 2 + (p1[2] - p0[2]) ** 2) ** 0.5

    def fun_camera(self):
        """Wider view: Camera.main zoomed out by 1.5 (orthographic size for 2D, field of view for 3D)."""
        ortho = str(self.get("UnityEngine.Camera", "main.orthographic")).lower() == "true"
        member = "main.orthographicSize" if ortho else "main.fieldOfView"
        v = num(self.get("UnityEngine.Camera", member))
        if not v:
            return []
        wide = v * 1.5 if ortho else min(v * 1.4, 110.0)
        cells = self.visible_effect(lambda: self.set("UnityEngine.Camera", member, fmt(round(wide, 2))),
                                    lambda: self.set("UnityEngine.Camera", member, fmt(v)))
        self.log(f"  camera {member} {v} -> {wide:.2f}: visible cells {cells}")
        if cells < 20:
            return []
        return [{"name": "Wide Camera", "class": "UnityEngine.Camera", "member": member, "mode": "freeze",
                 "value": fmt(round(wide, 2)), "evidence": f"zoomed out x1.5, {cells} screen cells change"}]

    def jump_height(self, key, secs=1.2):
        p0 = self.pos()
        if not p0:
            return None
        peak = 0.0
        self.run([f"tap {key}"])
        t = time.time()
        while time.time() - t < secs:
            p = self.pos()
            if p:
                peak = max(peak, abs(p[1] - p0[1]))
        time.sleep(0.6)
        return peak

    def fun_jump(self, player):
        key = self.k.get("jump")
        if not key:
            return []
        pc = self.pb["player_class"]
        base = self.jump_height(key)
        if not base:
            self.log("  jump: player does not leave the ground on the jump key (or jumps are not transform-based)")
            return []
        for path, (t, v) in player.items():
            if t not in ("Single", "Double") or v <= 0 or not JUMP.search(path):
                continue
            self.set(pc, path, fmt(v * 2))
            high = self.jump_height(key)
            self.set(pc, path, fmt(v))
            self.log(f"  jump probe {path}: height {base:.2f} -> {high}")
            if high and base > 0.2 and high > base * 1.4:
                return [{"name": "Super Jump", "class": pc, "member": path, "mode": "freeze", "value": fmt(v * 2),
                         "evidence": f"jump height {base:.2f} -> {high:.2f}"}]
        return []

    def fun_enemies(self):
        """Enemies = another class carrying the player's health field, with several live objects."""
        h = getattr(self, "found", {}).get("health")
        pc = self.pb["player_class"]
        if not h:
            return []
        leaf = h["path"].split(".")[-1]
        rows = self.values("ap0", "|" + leaf)
        per_class = {}
        for k, (t, v) in rows.items():
            cls, inst, path = self.split_key(k)
            if path == h["path"] and cls != pc:
                per_class.setdefault(cls, []).append(v)
        ranked = sorted(per_class.items(), key=lambda kv: (not re.search(r"(?i)enemy|boss|mob|monster|foe|hostile", kv[0]), -len(kv[1])))
        out = []
        for cls, vals in ranked[:1]:
            if not re.search(r"(?i)enemy|boss|mob|monster|foe|hostile", cls) and len(vals) < 3:
                continue
            out.append({"name": "One-Hit Kills", "class": cls, "member": h["path"], "mode": "freeze", "value": "!1",
                        "evidence": f"{len(vals)} live {cls} share the player's health field; frozen at 1"})
            # statues: enemy behaviour scripts switched off (AI and movement stop; restores when OFF)
            p0 = self.cmd(f"pos {cls}")
            out.append({"name": "Freeze Enemies", "class": cls, "member": "enabled", "mode": "freeze", "value": "false",
                        "evidence": f"{cls}.enabled = false stops their Update (AI) while ON"})
            self.log(f"  enemies: {cls} x{len(vals)} ({p0.splitlines()[-1] if p0 else ''})")
        return out

    def find_fun(self):
        pc = self.pb["player_class"]
        fun = []
        player = {self.split_key(k)[2]: (t, v) for k, (t, v) in self.values("ap0", pc + "#").items()}
        parts = [c for c in self.pb.get("player_parts", []) if c != pc]
        levers = [(pc, path, t, v) for path, (t, v) in player.items()]
        for part in parts:
            levers += [(part, self.split_key(k)[2], t, v) for k, (t, v) in self.values("ap0", part + "#").items()]
        # speed multiplier: measured by distance walked with the original value and with x2
        right, left = self.k["move_right"], self.k["move_left"]
        for owner, path, t, v in levers:
            if t not in ("Single", "Double") or v <= 0 or not SPEED.search(path):
                continue
            d0 = self.move_distance(right)
            self.set(owner, path, fmt(v * 2))
            d1 = self.move_distance(left)
            self.set(owner, path, fmt(v))
            self.log(f"  speed probe {owner}.{path}: {d0} -> {d1}")
            if d0 and d1 and d0 > 0.3 and d1 > d0 * 1.4:   # the first walk must really move
                fun.append({"name": "Super Speed", "class": owner, "member": path, "mode": "multiply_freeze",
                            "value": "@x2", "evidence": f"walked {d0:.2f} -> {d1:.2f} units in 0.6 s"})
                break
        # engine trick, always available in Unity: time scale (verified by reading it back)
        for name, val in (("Slow Motion x0.5", "0.5"), ("Turbo x2", "2")):
            fun.append({"name": name, "class": "UnityEngine.Time", "member": "timeScale", "mode": "freeze", "value": val,
                        "evidence": "engine-level, works in every Unity game"})
        fun += self.fun_camera() + self.fun_jump(player) + self.fun_enemies()
        # developer flags (static or on the player) that visibly change something when switched on
        flags = {k: (t, v) for k, (t, v) in self.values("ap0", "debug|cheat|god|invinc|noclip|unlock|infinite").items()
                 if t == "Boolean" and v == 0 and DEV_FLAG.search(self.split_key(k)[2])}
        tested = 0
        for k in sorted(flags, key=lambda k: ("#static" not in k, not re.search(r"(?i)invinc|god|unlock|infinite", k))):
            cls, inst_id, path = self.split_key(k)
            if (cls != pc and inst_id != "static") or re.search(r"(?i)(^|[._])ui|menu|screen|hud|debugmenu|editor|log", cls):
                continue
            if tested >= 8:
                break
            tested += 1
            cells = self.visible_effect(lambda: self.set(cls, path, "true"), lambda: self.set(cls, path, "false"))
            self.log(f"  dev flag {cls}.{path}: visible cells {cells}")
            leaf = path.split(".")[-1]
            overlay = re.search(r"(?i)text|sprite|extent|gizmo|draw|log|show|display|info|len(g)?th|check|ray|"
                                r"line|box|bounds|collider|label|print|verbose|tmp|prefab|owned|value$", leaf)
            if overlay and not re.search(r"(?i)invinc|god|ignore|infinite|unlock|noclip|nodamage", leaf):
                continue                       # debug visualisation, not a gameplay cheat
            if re.search(r"(?i)invinc|god|nodamage|ignore|infinite|unlock", leaf) or cells >= 8:
                fun.append({"name": "Dev Switch: " + human(path.split(".")[-1]), "class": cls, "member": path,
                            "mode": "freeze", "value": "true",
                            "evidence": f"developer flag; {cells} screen cells react" if cells else
                                        f"developer flag named '{leaf}' (effect not visible in a calm scene)"})
        self.report["stages"]["fun"] = fun
        return fun

    # ------------------------------------------------------------------------- stage 5: compose + verify
    def compose(self, values, hooks, fun):
        pc = self.pb["player_class"]
        cheats = []
        h = values.get("health")
        if h:
            cheats.append({"name": "Infinite Health", "class": pc, "member": h["path"], "mode": "lock", "value": "0",
                           "evidence": "HUD reacts, value sticks" if not h.get("weak") else "value sticks (HUD reaction not seen)"})
        for b in values.get("bars", []):
            leaf = b["path"].split(".")[-1].strip("_").replace("m", "", 1) if b["path"].startswith("m") else b["path"].split(".")[-1].strip("_")
            cheats.append({"name": f"Full {leaf}", "class": pc, "member": b["path"], "mode": "freeze",
                           "value": "@" + b["max_path"].split(".")[-1], "evidence": f"bar with maximum {b['max_path']}"})
        for hk in hooks[:1]:
            cheats.append({"name": "God Mode", "class": pc, "member": hk["method"], "mode": hk["mode"], "value": pc,
                           "evidence": f"Harmony hook {hk['declared']}.{hk['method']} for {pc} only"})
        if values.get("xp"):
            cheats.append({"name": "+500 XP", "class": pc, "member": values["xp"]["path"], "mode": "add", "value": "500",
                           "check": f"{pc} {values['xp']['path']}", "evidence": "value sticks"})
        m = values.get("money")
        if m:
            cheats.append({"name": "+1000 Money", "class": m["class"], "member": m["path"], "mode": "add", "value": "1000",
                           "check": f"{m['class']} {m['path']}", "then": m.get("refresh", ""),
                           "evidence": "HUD shows the written amount" + (f" (after {m['refresh']})" if m.get("refresh") else "")})
        for f in fun:
            mode = "freeze" if f["mode"] == "multiply_freeze" else f["mode"]
            cheats.append({**f, "mode": mode})
        seen = set()
        for c in cheats:                       # names are the plugin's keys: make them unique
            if c["name"] in seen:
                c["name"] = f"{c['name']} ({c['member'].split('.')[-1].strip('_')})"
            seen.add(c["name"])
        for i, c in enumerate(cheats[:24]):
            c["hotkey"] = hotkey(i)
        return cheats[:24]

    def write_cheats(self, cheats):
        lines = ["# name | hotkey | class | member | mode | value | check   (generated by autopilot)"]
        for c in cheats:
            extra = [c.get("check") or "-", c["then"]] if c.get("then") else ([c["check"]] if c.get("check") else [])
            lines.append(" | ".join([c["name"], c["hotkey"], c["class"], c["member"], c["mode"], c["value"]] + extra))
        text = "\n".join(lines) + "\n"
        (self.gdir / "cheats.txt").write_text(text, encoding="utf-8")
        (self.gdir / "cheats.json").write_text(json.dumps(
            [{"name": c["name"], "hotkey": c["hotkey"], "desc": describe(c), "evidence": c.get("evidence", ""),
              "class": c["class"], "member": c["member"], "mode": c["mode"], "value": c["value"]} for c in cheats],
            indent=1, ensure_ascii=False), encoding="utf-8")
        cfg = self.ug.bep / "config" / "universal_trainer.txt"
        cfg.write_text(text, encoding="utf-8")
        self.cmd("reload")

    def verify(self, cheats):
        """Value checks through the plugin; hooks are checked by calling the patched bool method."""
        results = {}
        for c in cheats:
            self.cmd(f"off {c['name']}")
            if c["mode"] in ("return_false", "return_true", "skip"):
                out = self.cmd(f"on {c['name']}")
                err = re.search(rf"^on {re.escape(c['name'])} -> error (.+)$", out, re.M)
                ok, how = (False, err.group(1)) if err else (True, "hook installed")
                if c["mode"] != "skip" and ok:
                    out = self.cmd(f"call {c['class']} {c['member']}")
                    want = "False" if c["mode"] == "return_false" else "True"
                    ok = f"-> {want}" in out
                    how = f"{c['member']}() returns {want} while ON" if ok else f"call result: {out.splitlines()[-1]}"
                self.cmd(f"off {c['name']}")
                results[c["name"]] = {"ok": ok, "how": how}
                continue
            if c["mode"] == "freeze" and c["value"].startswith("@"):
                parent = c["member"].rsplit(".", 1)[0] + "." if "." in c["member"] else ""
                before = num(self.get(c["class"], c["member"]))
                self.cmd(f"on {c['name']}")
                time.sleep(0.6)
                after = num(self.get(c["class"], c["member"]))
                if c["value"].startswith("@x"):
                    want = before * float(c["value"][2:]) if before is not None else None
                else:
                    want = num(self.get(c["class"], parent + c["value"][1:]))
                self.cmd(f"off {c['name']}")
                if c["value"].startswith("@x") and before is not None:
                    self.set(c["class"], c["member"], fmt(before))   # relative freezes leave the value changed
                ok = after is not None and want is not None and abs(after - want) < 1e-3
                results[c["name"]] = {"ok": ok, "how": f"frozen at {after}, expected {want}", "details": {"before": before}}
                continue
            spec = {"name": c["name"], "class": c["class"], "member": c["member"], "mode": c["mode"],
                    "value": c["value"].lstrip("!"), "check": c.get("check", "")}
            try:
                results[c["name"]] = self.ug.verify(spec, self.keys)
            except Exception as e:
                results[c["name"]] = {"ok": False, "how": f"error: {e}"}
            self.cmd(f"off {c['name']}")
            r = results[c["name"]]
            if c["mode"] in ("add", "set", "multiply") and r.get("details", {}).get("before") not in (None, "-", "?"):
                # put the value back: verifying "+1000 money" must not hand out money in the player's save
                chk_cls, chk_mem = (c.get("check") or f"{c['class']} {c['member']}").split(" ", 1)
                self.set(chk_cls, chk_mem, r["details"]["before"])
                if c.get("then"):
                    self.cmd(f"call {c['then']}")
            if c["value"].startswith("!") and r.get("details", {}).get("before") not in (None, "-", "?"):
                # no-restore freezes (enemy HP) are for play; the verification itself must leave no trace
                self.set(c["class"], c["member"], r["details"]["before"])
            if c["mode"] == "freeze" and r.get("ok") and not c["value"].startswith("!"):
                # a modifier (time scale, flag, multiplier) must give the old value back when switched off
                time.sleep(0.4)
                before, back = r.get("details", {}).get("before"), self.get(c["class"], c["member"])
                if str(back).lower() != str(before).lower() and num(back) != num(before):
                    r["ok"], r["how"] = False, f"switching off left {back} instead of {before}"
        self.report["stages"]["verify"] = results
        return results

    # ------------------------------------------------------------------------- all
    def safety(self, on, hooks):
        """God-mode hooks limited to the player's class while we knock values down and walk around:
        a fight that starts mid-probe (enemies wake up) must not end the run on a game-over screen."""
        pc = self.pb["player_class"]
        gentle = [h for h in hooks if h["mode"] != "skip"][:3] or hooks[:2]
        names = [f"__safety{i}" for i in range(len(gentle))]
        if on and gentle:
            text = "".join(f"{n} | | {pc} | {h['method']} | {h['mode']} | {pc}\n" for n, h in zip(names, gentle))
            (self.ug.bep / "config" / "universal_trainer.txt").write_text(text, encoding="utf-8")
            self.cmd("reload")
            self.cmd(*[f"on {n}" for n in names])
            self.log(f"safety net on: {[h['method'] for h in gentle]}")
        elif not on:
            try:
                self.cmd(*[f"off {n}" for n in names])
            except Exception:
                pass

    def ensure_playable(self, budget):
        """Back to gameplay after a game over / pause / scene change (the explorer picks Continue/Retry)."""
        if self.in_gameplay({}):
            return True
        self.log("not in gameplay any more (game over, pause or loading): navigating back")
        return self.reach_gameplay(budget, save_route=False).get("ok")

    def run_all(self, nav_budget=900):
        t0 = time.time()
        g = self.reach_gameplay(nav_budget)
        if not g.get("ok"):
            self.report["error"] = g.get("error")
            self.report["stages"]["gameplay"] = g
            return self.finish(t0)
        hooks = self.find_hooks()
        self.safety(True, hooks)              # the player must survive our experiments
        try:
            self.ensure_playable(nav_budget)
            values = self.find_values()
            if not values.get("ok"):
                self.log(f"find_values: {values.get('error')}")
            self.ensure_playable(nav_budget)
            fun = self.find_fun()
            self.ensure_playable(nav_budget)
        finally:
            self.safety(False, hooks)
        cheats = self.compose(values.get("found", {}), hooks, fun)
        self.write_cheats(cheats)
        res = self.verify(cheats)
        good = [c for c in cheats if res.get(c["name"], {}).get("ok")]
        bad = [c for c in cheats if not res.get(c["name"], {}).get("ok")]
        for i, c in enumerate(good):
            c["hotkey"] = hotkey(i)
        self.write_cheats(good)
        try:
            self.report["stages"]["fight"] = self.fight_check(good, res)
        except Exception as e:
            self.report["stages"]["fight"] = {"error": str(e)}
            self.log(f"fight check failed: {e}")
        dropped = [c for c in good if not res.get(c["name"], {}).get("ok")]
        good = [c for c in good if res.get(c["name"], {}).get("ok")]
        bad += dropped
        for i, c in enumerate(good):
            c["hotkey"] = hotkey(i)
        self.write_cheats(good)
        self.report["cheats"] = good
        for c in good:
            res.setdefault(c["name"], {})["evidence"] = c.get("evidence", "")
        (self.gdir / "verify.json").write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
        self.release_shots(good)
        self.report["skipped"] = [{"name": c["name"], "why": res.get(c["name"], {}).get("how")} for c in bad]
        return self.finish(t0)

    # ------------------------------------------------------------------------- behaviour: a real fight
    def obj_pos(self, cls):
        out = self.cmd(f"pos {cls}")
        m = re.search(r"^pos (\S+) (\S+) (\S+)", out, re.M)
        return tuple(float(x) for x in m.groups()) if m else None

    def learn_axes(self):
        """Which world axis each movement key drives (side-scrollers: x; beat'em-ups: x and z; 3D: x/z)."""
        axes = {}
        for key in ("right", "up"):
            p0 = self.pos()
            self.run([f"hold {self.k['move_' + key]} 0.35", "sleep 0.2"])
            p1 = self.pos()
            if p0 and p1:
                d = [p1[i] - p0[i] for i in range(3)]
                # Unity convention first (x = right, z = depth/forward, y = height; a landing or a jump moves
                # y on its own), the largest movement only when the conventional axis did not move
                prior = 0 if key == "right" else 2
                i = prior if abs(d[prior]) > 0.05 else max((0, 2, 1), key=lambda i: abs(d[i]))
                if abs(d[i]) > 1e-3:
                    axes[key] = (i, 1 if d[i] > 0 else -1)
        return axes

    def fight(self, enemy_cls, secs, axes):
        """Walk to the nearest live enemy and attack it for `secs` seconds."""
        t = time.time()
        while time.time() - t < secs:
            p, e = self.pos(), self.obj_pos(enemy_cls)
            if not p or not e:
                self.run([f"tap {self.k['attack']}", "sleep 0.2"])
                continue
            steps = []
            for key, back in (("right", "left"), ("up", "down")):
                if key in axes:
                    i, sign = axes[key]
                    d = (e[i] - p[i]) * sign
                    if abs(d) > 0.6:
                        steps.append(f"hold {self.k['move_' + key] if d > 0 else self.k['move_' + back]} {min(0.5, abs(d) * 0.2):.2f}")
            steps += [f"tap {self.k['attack']}", "sleep 0.15", f"tap {self.k['attack']}", "sleep 0.15"]
            self.run(steps)

    def fight_check(self, cheats, res):
        """Behaviour evidence where values alone prove little: does the player take damage without the
        cheats, none with God Mode / Infinite Health, and do enemies die from one hit with One-Hit Kills?"""
        pc = self.pb["player_class"]
        h = getattr(self, "found", {}).get("health")
        enemy = next((c for c in cheats if c["name"] == "One-Hit Kills"), None)
        if not h or not enemy:
            return {"skipped": "needs a health field and a live enemy class"}
        ecls = enemy["class"]
        axes = self.learn_axes()
        self.log(f"fight: movement axes {axes}")
        if not axes:
            return {"skipped": "movement keys do not move the player"}
        out = {}
        hp = lambda: num(self.get(pc, h["path"]))
        h0 = hp()
        self.fight(ecls, 8, axes)                       # no cheats: can the enemies hurt us at all?
        h1 = hp()
        out["without_cheats"] = [h0, h1]
        self.set(pc, h["path"], fmt(h0))                # heal back
        hurt = h1 is not None and h0 is not None and h1 < h0
        for name in ("God Mode", "Infinite Health"):
            c = next((c for c in cheats if c["name"] == name), None)
            if not c:
                continue
            self.cmd(f"on {name}")
            a = hp()
            self.fight(ecls, 8, axes)
            b = hp()
            self.cmd(f"off {name}")
            ok = a is not None and b is not None and b >= a
            out[name] = {"health": [a, b], "conclusive": hurt, "ok": ok}
            res[name]["fight"] = out[name]
            c["evidence"] = (f"fight: health {a} -> {b} while enemies hit (without it {h0} -> {h1})" if hurt and ok
                             else c["evidence"] + "; fight test inconclusive (enemies did not hit)" if not hurt
                             else c["evidence"] + f"; FIGHT FAILED: health {a} -> {b}")
            if hurt and not ok:
                res[name]["ok"], res[name]["how"] = False, f"fight: health still dropped {a} -> {b}"
            self.log(f"  fight {name}: health {a} -> {b} (enemies {'did' if hurt else 'did NOT'} hurt us without it)")
        def alive():
            self.cmd("snap fc", timeout=60)
            return sum(1 for k, (t, v) in self.values("fc", "|" + h["path"]).items()
                       if self.split_key(k)[0] == ecls and self.split_key(k)[2] == h["path"] and v > 0)
        n0 = alive()
        self.cmd("on One-Hit Kills")
        self.fight(ecls, 10, axes)
        self.cmd("off One-Hit Kills")
        time.sleep(1.5)
        n1 = alive()
        out["One-Hit Kills"] = {"enemies": [n0, n1], "ok": n1 < n0}
        enemy["evidence"] += (f"; fight: living enemies {n0} -> {n1}" if n1 < n0 else
                              "; fight test inconclusive (no enemy died in reach)")
        self.log(f"  fight One-Hit Kills: live enemies {n0} -> {n1}")
        self.set(pc, h["path"], fmt(h0))
        return out

    def release_shots(self, cheats):
        """Gameplay screenshot with the trainer overlay and the bars/flags switched on (timescale stays off)."""
        shots = self.gdir / "shots"
        shots.mkdir(exist_ok=True)
        on = [c["name"] for c in cheats if c["mode"] in ("freeze", "lock") and c["class"] != "UnityEngine.Time"]
        try:
            for n in on:
                self.cmd(f"on {n}")
            time.sleep(1.0)
            self.focus()
            play.shot(self.ug.process, shots / "trainer_on.png", 1.0)
        finally:
            for n in on:
                self.cmd(f"off {n}")
        money = next((c for c in cheats if c["name"].startswith("+") and "Money" in c["name"]), None)
        if money:                              # the HUD after a money cheat, then rolled back
            before = self.get(money["class"], money["member"])
            self.cmd(f"on {money['name']}")
            time.sleep(1.0)
            play.shot(self.ug.process, shots / "money_cheat.png", 1.0)
            self.set(money["class"], money["member"], before)
            if money.get("then"):
                self.cmd(f"call {money['then']}")
        for f in shots.glob("*_small.png"):
            f.unlink()

    def finish(self, t0):
        self.report["seconds"] = int(time.time() - t0)
        (self.gdir / "autopilot_report.json").write_text(json.dumps(self.report, indent=1, ensure_ascii=False), encoding="utf-8")
        return self.report
