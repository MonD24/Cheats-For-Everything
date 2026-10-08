"""Talk to UniversalTrainer inside a running Unity game: send commands, read state,
and verify cheats automatically (value checks + behaviour checks with real input)."""
import re
import time
from pathlib import Path

from . import play


class UnityGame:
    def __init__(self, game_dir, process):
        self.dir = Path(game_dir)
        self.process = process
        self.bep = self.dir / "BepInEx"

    # ---------------------------------------------------------------- channel
    def cmd(self, *lines, timeout=6.0):
        state = self.bep / "trainer_state.txt"
        before = state.stat().st_mtime if state.exists() else 0
        (self.bep / "trainer_cmd.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        end = time.time() + timeout
        while time.time() < end:
            if state.exists() and state.stat().st_mtime > before:
                time.sleep(0.05)
                return state.read_text(encoding="utf-8", errors="replace")
            time.sleep(0.1)
        raise TimeoutError("trainer did not answer: is the game running with the plugin loaded?")

    def loaded(self):
        log = self.bep / "LogOutput.log"
        return log.exists() and "Universal Trainer loaded" in log.read_text(errors="replace")

    def scene(self):
        m = re.search(r"scene (\S+)", self.cmd("dump"))
        return m.group(1) if m else None

    def get(self, cls, member):
        out = self.cmd(f"get {cls} {member}")
        m = re.search(r"\[0\] = (.+)", out)
        return m.group(1).strip() if m else None

    def cheats(self):
        out = self.cmd("dump")
        res = {}
        for name, state, value, err in re.findall(r"^cheat (.+?) \| (ON|off) \| value=([^|\n]*)(?: \| error=(.*))?$", out, re.M):
            res[name] = {"on": state == "ON", "value": value.strip(), "error": err or None}
        return res

    def keymap(self):
        out = self.cmd("keymap", timeout=20)
        keys = {}
        for action, key in re.findall(r"^key player=Player0 action=(\S+) key=(\S+)", out, re.M):
            k = key.lower()
            keys[action] = k if action not in keys else (keys[action] + "/" + k)
        return keys

    # ---------------------------------------------------------------- verification
    def verify(self, spec, keys=None, fight_secs=8):
        """spec: one cheat line parsed to dict(name, class, member, mode, value).
        Returns dict(ok, how, details)."""
        n, cls, mem, mode = spec["name"], spec["class"], spec["member"], spec["mode"]
        num = lambda v: float(v) if v not in (None, "-", "?") and re.match(r"^-?[\d.]+(E-?\d+)?$", str(v), re.I) else None
        self.cmd(f"off {n}")
        before = self.get(cls, mem)
        if mode in ("freeze", "lock"):
            self.cmd(f"on {n}")
            time.sleep(0.6)
            if mode == "lock" and num(before) is not None:
                # knock the value down; a working lock puts it straight back
                self.cmd(f"set {cls} {mem} {max(1, int(num(before) // 3))}")
                time.sleep(0.8)
                after = self.get(cls, mem)
                ok = num(after) == num(before)
                return {"ok": ok, "how": "value forced down, lock restored it" if ok else "lock did not restore",
                        "details": {"before": before, "after_knockdown": after}}
            after = self.get(cls, mem)
            target = spec["value"]
            if target.startswith("@"):
                target = self.get(cls, target[1:])
            ok = str(after).lower() == str(target).lower() or (num(after) is not None and num(after) == num(target))
            return {"ok": ok, "how": "frozen value read back", "details": {"before": before, "after": after, "target": target}}
        # one-shot: set/add/multiply. Only a change at the place the value really lives counts.
        chk_cls, chk_mem = (spec.get("check") or f"{cls} {mem}").split(" ", 1)
        b = self.get(chk_cls, chk_mem)
        self.cmd(f"on {n}")
        time.sleep(0.8)
        a = self.get(chk_cls, chk_mem)
        changed = a is not None and a != b
        return {"ok": changed if spec.get("check") or mode != "set" else None,
                "how": "stored value changed" if changed else
                       ("no change seen at the checked place" if spec.get("check") else
                        "UNVERIFIED: give add_unity_cheat a 'check' path where the value is stored"),
                "details": {"checked": f"{chk_cls} {chk_mem}", "before": b, "after": a}}

    def fight(self, keys, secs=8):
        """Walk around and attack so enemies engage; uses the game's own key bindings."""
        play.focus(self.process)
        k = {a.lower(): v for a, v in keys.items()}
        attack = (k.get("quickattack") or k.get("attack") or "j").split("/")[0]
        horiz = (k.get("movehorizontal") or "a/d").split("/")
        dirs = (horiz + horiz)[:2] if len(horiz) >= 2 else ["d", "a"]
        steps = []
        t_end = time.time() + secs
        i = 0
        while time.time() < t_end:
            play.hold([dirs[i % 2]], 0.6)
            for _ in range(3):
                play.tap(attack)
                time.sleep(0.15)
            i += 1
        return steps

    def damage_test(self, health_cls, health_mem, cheat_name, keys, secs=10):
        """Behaviour check: health drops while fighting without the cheat, does not with it."""
        self.cmd(f"off {cheat_name}")
        h0 = float(self.get(health_cls, health_mem))
        self.fight(keys, secs)
        h1 = float(self.get(health_cls, health_mem))
        self.cmd(f"on {cheat_name}")
        time.sleep(0.5)
        h2 = float(self.get(health_cls, health_mem))
        self.fight(keys, secs)
        h3 = float(self.get(health_cls, health_mem))
        self.cmd(f"off {cheat_name}")
        took_damage = h1 < h0
        return {"ok": took_damage and h3 >= h2, "conclusive": took_damage,
                "without_cheat": [h0, h1], "with_cheat": [h2, h3]}


def parse_cheats(path):
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = [x.strip() for x in line.split("|")]
        if len(p) >= 6:
            out.append({"name": p[0], "hotkey": p[1], "class": p[2], "member": p[3], "mode": p[4].lower(), "value": p[5],
                        "check": p[6] if len(p) > 6 and p[6] != "-" else "", "then": p[7] if len(p) > 7 else ""})
    return out
