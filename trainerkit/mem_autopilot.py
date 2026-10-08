"""Memory autopilot for games without a reflection plugin (any engine): the HUD is the oracle.

  1. the vision model reads the numbers on the HUD (health 87, money 1520, ammo 30 ...);
  2. exact first scan for that number (int32, then float), like a Cheat Engine first scan;
  3. while the value changes by itself (timers, regen, damage over time) re-read the HUD and rescan
     for the new number; otherwise filter "unchanged" to drop noise;
  4. write-bisection: write a distinct number into half of the remaining addresses, ask the HUD whether
     it shows that number, restore at once, keep the half that moved the HUD (~log2(n) rounds);
  5. verify: the final address written to a test value must show on the HUD, then is restored.

The result is a session address plus where it lives (module+offset = durable, heap = this session only,
needs a pointer chain). HUDs that only refresh on game events (not on a raw write) make step 4
inconclusive: then the run reports that and the agent asks the player to change the value in game.
"""
import array
import io
import struct
import time
from contextlib import redirect_stdout
from pathlib import Path

import pymem

from . import play, vision
from .runtime import VALUE_TYPES

ROOT = Path(__file__).resolve().parent.parent


def numstr(v):
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def quiet(fn, *a):
    with redirect_stdout(io.StringIO()):
        return fn(*a)


class MemAutopilot:
    def __init__(self, process, gdir, log=print, ask=vision.ask):
        import scanner
        self.scanner = scanner
        self.process, self.gdir, self.log, self.ask = process, Path(gdir), log, ask
        self.gdir.mkdir(parents=True, exist_ok=True)
        self.shots = self.gdir / "mem_shots"
        self.shots.mkdir(exist_ok=True)
        self.pm = pymem.Pymem(process)
        self._n = 0

    # ---------------------------------------------------------------- HUD oracle
    def shot(self):
        self._n += 1
        return play.shot(self.process, self.shots / f"{self._n:04d}.png", 0.75)

    MENU_KEYS = ("i", "tab", "c", "k", "j", "m", "p", "esc")
    WANTED = r"(?i)health|hp|life|money|gold|cash|coin|crown|credit|gem|soul|rune|scrap|cap|ammo|lives|mana|stamina|xp|exp"

    def hud_all(self, image=None):
        ans, raw = self.ask('List every number shown on this game screen\'s HUD/status text (health, money, gold, '
                            'ammo, lives, score, time, level...). JSON only: {"values": [{"name": "<lowercase english '
                            'name>", "value": <number>}]}', image or self.shot())
        out = {}
        for item in (ans or {}).get("values", []) if isinstance(ans, dict) else []:
            try:
                out[str(item["name"]).lower()] = float(item["value"])
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def hud(self, name):
        view = getattr(self, "views", {}).get(name)
        if view:                                  # the number lives in a menu: open it, read, close it
            play.run(self.process, [f"tap {view}", "sleep 1.3"])
        ans, _ = self.ask(f'Read the {name} value shown on this game screen. JSON only: {{"{name}": <number or null>}}', self.shot())
        if view:
            play.run(self.process, [f"tap {view}", "sleep 0.9"])
        try:
            return float((ans or {}).get(name))
        except (TypeError, ValueError):
            return None

    def find_value_screens(self):
        """Numbers that are not on the HUD often sit in a menu (inventory: money, character sheet: XP).
        Try the usual menu keys; a key counts when it changes the screen and closes again with the same key."""
        from . import screen
        play.focus(self.process)
        base = self.shot()
        views = {}
        for key in self.MENU_KEYS:
            play.run(self.process, [f"tap {key}", "sleep 1.5"])
            img = self.shot()
            if not screen.changed(base, img):
                continue
            nums = self.hud_all(img)
            play.run(self.process, [f"tap {key}", "sleep 1.2"])
            back = self.shot()
            if screen.changed_pct(base, back) > 25:      # did not close with the same key: try Esc once
                play.run(self.process, ["tap esc", "sleep 1.2"])
                if screen.changed_pct(base, self.shot()) > 25:
                    self.log(f"  menu key {key}: could not get back to gameplay, stopping the menu search")
                    break
            self.log(f"  menu key {key}: {nums}")
            for name, v in nums.items():
                views.setdefault(name, key)
        return views

    # ---------------------------------------------------------------- memory
    def read(self, addr, vt):
        fmt, size = VALUE_TYPES[vt]
        return struct.unpack(fmt, self.pm.read_bytes(addr, size))[0]

    def write(self, addr, vt, value):
        fmt, size = VALUE_TYPES[vt]
        self.pm.write_bytes(addr, struct.pack(fmt, float(value) if vt in ("float", "double") else int(value)), size)

    def candidates(self):
        meta, addrs, _ = self.scanner.load()
        return meta["type"], list(addrs)

    def first(self, value, tol=0.0):
        types = ["float", "double"] if value != int(value) else ["int32", "float", "int16", "double"]
        for vt in types:
            quiet(self.scanner.first, self.process, vt, numstr(value), tol)
            _, addrs = self.candidates()
            self.log(f"  first scan {vt} {value}: {len(addrs)} addresses")
            if addrs:
                return vt, addrs
        return None, []

    # ---------------------------------------------------------------- locate one value
    def locate(self, name, rescans=6, bisect_limit=6000):
        v = self.hud(name)
        if v is None:
            return {"name": name, "ok": False, "error": "the HUD value could not be read"}
        # a value that moves while we look (drain, regen, timer) gets a range scan instead of an exact one
        time.sleep(1.5)
        drift = abs((self.hud(name) or v) - v)
        vt, addrs = self.first(v, tol=drift * 3 if drift else 0.0)
        if not addrs:
            return {"name": name, "ok": False, "error": f"no memory holds {v} as int/float (bar or encoded value?)"}
        for _ in range(rescans):
            if len(addrs) <= 32:
                break
            time.sleep(2)
            v2 = self.hud(name)
            # direction, not the exact number: the value may move again between reading the HUD and scanning
            cond = "unchanged" if v2 is None or v2 == v else ("decreased" if v2 < v else "increased")
            quiet(self.scanner.next_scan, cond)
            v = v2 if v2 is not None else v
            _, addrs = self.candidates()
            self.log(f"  rescan {name}={v}: {len(addrs)} left")
        if len(addrs) > bisect_limit:
            return {"name": name, "ok": False, "error": f"{len(addrs)} candidates: change the value in game, then rescan",
                    "type": vt, "value": v}
        found = self.bisect(name, vt, addrs, v)
        if not found:
            return {"name": name, "ok": False, "error": "the HUD did not follow any write (it may refresh only on game events)",
                    "type": vt, "value": v, "left": len(addrs)}
        return self.verify(name, vt, found)

    def bisect(self, name, vt, addrs, value, rounds=24):
        test = value + 1234 if value < 50000 else value - 1234
        cands = list(addrs)
        for _ in range(rounds):
            if len(cands) <= 1:
                break
            half = cands[: len(cands) // 2]
            moved = self.try_write(name, vt, half, test)
            if moved is None:
                return None
            cands = half if moved else cands[len(cands) // 2:]
            self.log(f"  bisect {name}: {len(cands)} left")
        if len(cands) == 1 and self.try_write(name, vt, cands, test):
            return cands[0]
        return None

    def try_write(self, name, vt, addrs, test):
        """Write `test` to addrs, read the HUD, restore. True = HUD showed test, False = it did not,
        None = the HUD cannot be read."""
        saved = []
        for a in addrs:
            try:
                saved.append((a, self.read(a, vt)))
                self.write(a, vt, test)
            except Exception:
                pass
        time.sleep(0.5)
        shown = self.hud(name)
        for a, old in saved:
            try:
                self.write(a, vt, old)
            except Exception:
                pass
        if shown is None:
            return None
        return abs(shown - test) <= max(2.0, abs(test) * 0.01)

    def verify(self, name, vt, addr):
        where = self.scanner.where(self.pm, addr)
        val = self.read(addr, vt)
        ok = self.try_write(name, vt, [addr], val + 777)
        self.log(f"  {name}: 0x{addr:X} {vt} = {val}  ({where}); HUD follows writes: {ok}")
        return {"name": name, "ok": bool(ok), "address": f"0x{addr:X}", "type": vt, "value": val, "where": where,
                "durable": "static" in where}

    # ---------------------------------------------------------------- durability
    def chains_for(self, result, keep=60):
        """Pointer chains from static module data to a found heap address (this session)."""
        from . import pointers
        if not result.get("ok") or result.get("durable"):
            return result
        chains = pointers.scan(self.pm, int(result["address"], 16), log=self.log)
        result["chains"] = chains[:keep]
        self.log(f"  {result['name']}: {len(chains)} pointer chains (kept {len(result['chains'])})")
        return result

    def revalidate(self, result):
        """After a game restart: keep the chains that show the HUD's current number, then prove one by a write."""
        from . import pointers
        if not result.get("chains"):
            return result
        shown = self.hud(result["name"])
        good = []
        for c in result["chains"]:
            a = pointers.resolve(self.pm, c)
            if a is None:
                continue
            try:
                v = self.read(a, result["type"])
            except Exception:
                continue
            if shown is not None and abs(v - shown) <= max(1.0, abs(shown) * 0.01):
                good.append((c, a))
        self.log(f"  {result['name']}: {len(good)}/{len(result['chains'])} chains survive the restart (HUD {shown})")
        if good and self.try_write(result["name"], result["type"], [good[0][1]], (shown or 0) + 777):
            result.update(durable=True, chains=[c for c, _ in good][:8], where="pointer chain (checked after a restart)")
        else:
            result.update(durable=False, chains=[c for c, _ in good][:8])
        return result

    # ---------------------------------------------------------------- all
    def run_all(self):
        import re
        shown = self.hud_all()
        self.log(f"HUD numbers: {shown}")
        self.views = {}
        if not any(re.search(r"(?i)money|gold|cash|coin|crown|credit|gem|soul|rune|scrap", n) for n in shown):
            self.views = {n: k for n, k in self.find_value_screens().items() if n not in shown}
            self.log(f"numbers in menus: {self.views}")
        names = [n for n in list(shown) + list(self.views) if re.search(self.WANTED, n)] or list(shown)[:3]
        names = list(dict.fromkeys(names))[:6]
        results = {n: self.chains_for(self.locate(n)) for n in names}
        return {"hud": shown, "values": results}
