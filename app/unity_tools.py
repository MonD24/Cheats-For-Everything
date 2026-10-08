"""Agent tools for Unity (Mono) games: discover features from the game's code, install the
UniversalTrainer plugin, launch/drive the game, read live values, verify cheats, package a release."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "unity"))
from trainerkit import discover_unity, play  # noqa: E402
from trainerkit.unity_session import UnityGame, parse_cheats  # noqa: E402

CTX = {"S": None, "ug": None, "keys": {}, "features": None, "ideas": None, "startup": None}


def on_select(info, S):
    CTX["S"], CTX["ug"], CTX["keys"] = S, None, {}
    CTX["features"], CTX["ideas"] = None, None
    CTX["startup"] = None
    if (info.get("engine") or {}).get("key") == "unity-mono":
        CTX["ug"] = UnityGame(info["dir"], S.process or "")
        gd = gdir()
        gd.mkdir(parents=True, exist_ok=True)
        meta = {"name": info["name"], "dir": info["dir"],
                "process": S.process, "tested_version": (info.get("engine") or {}).get("version")}
        (gd / "game.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        S.playbook = playbook()


def pb_path():
    return gdir() / "playbook.json"


def playbook():
    p = pb_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_playbook(**upd):
    pb = playbook()
    pb.update(upd)
    pb["updated"] = time.strftime("%Y-%m-%d %H:%M")
    pb_path().write_text(json.dumps(pb, indent=1, ensure_ascii=False), encoding="utf-8")
    return pb


def gdir():
    return ROOT / "games" / CTX["S"].slug()


def need_unity():
    S = CTX["S"]
    if not S or not S.game:
        raise RuntimeError("call select_game first")
    if S.blocked:
        raise RuntimeError(f"not allowed: {S.blocked}")
    if not CTX["ug"]:
        raise RuntimeError("not a Unity Mono game: use the memory-scan tools instead")
    CTX["ug"].process = S.process
    return CTX["ug"]


def game_process_running(process):
    import psutil
    return any((item.info["name"] or "").lower() == (process or "").lower()
               for item in psutil.process_iter(["name"]))


def _startup_screen(ug, player_class, scene="unknown", plugin_error=""):
    shot_path = ROOT / "app" / "shots" / f"startup_{int(time.time() * 1000)}.png"
    try:
        captured = play.shot(ug.process, shot_path, 0.5)
    except Exception as e:
        return {"error": f"plugin unavailable ({plugin_error or e}); screenshot failed ({e}); no input was sent"}
    if not captured:
        return {"error": f"plugin unavailable ({plugin_error or 'no response'}); screenshot failed; no input was sent"}
    CTX["startup"] = {"screen": captured, "attempted": []}
    import base64
    result = {"ok": False, "needs_visual_decision": True, "player_class": player_class,
              "scene": scene, "hint": "No keys were sent. Inspect the screen before one deliberate input.",
              "image_b64": base64.b64encode(Path(captured).read_bytes()).decode()}
    if plugin_error:
        result["plugin_error"] = plugin_error
    return result


# ------------------------------------------------------------------ tools

def _page(items, offset, limit, max_limit=20):
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), max_limit))
    end = min(offset + limit, len(items))
    return {"total": len(items), "offset": offset,
            "next_offset": end if end < len(items) else None, "candidates": items[offset:end]}


def discover_features(offset: int = 0, limit: int = 20, refresh: bool = False):
    """Scan all ranked stat candidates once, then return them in pages of up to 20."""
    ug = need_unity()
    if refresh or CTX["features"] is None:
        CTX["features"] = discover_unity.scan(discover_unity.find_assembly(ug.dir))
        (gdir() / "features.json").write_text(json.dumps(CTX["features"], indent=1), encoding="utf-8")
    items = [{"feature": feature, **candidate}
             for feature, candidates in CTX["features"].items() for candidate in candidates]
    return _page(items, offset, limit)


def suggest_fun_cheats(offset: int = 0, limit: int = 20, refresh: bool = False):
    """Unusual / fun cheat ideas for this game: hidden developer flags, gameplay levers worth bending,
    and engine tricks (slow-mo, turbo, low gravity) that work in any Unity game. Pitch them to the user."""
    ug = need_unity()
    if refresh or CTX["ideas"] is None:
        from trainerkit import fun_ideas
        CTX["ideas"] = fun_ideas.scan(ug.dir)
        (gdir() / "fun_ideas.json").write_text(json.dumps(CTX["ideas"], indent=1, ensure_ascii=False), encoding="utf-8")
    page = _page(CTX["ideas"]["game"], offset, limit)
    page["engine_tricks"] = ([{"name": e["name"], "pitch": e["pitch"], "class": e["class"],
                               "member": e["member"], "mode": e["mode"], "value": e["value"]}
                              for e in CTX["ideas"]["engine"]] if page["offset"] == 0 else [])
    return page


def install_trainer():
    """Install BepInEx + the UniversalTrainer plugin into the game folder (only adds files)."""
    ug = need_unity()
    if game_process_running(ug.process):
        return {"error": "game is already running; refusing to kill it for trainer installation"}
    cheats = gdir() / "cheats.txt"
    if not cheats.exists():
        cheats.write_text("# name | hotkey | class | member | mode | value\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "unity" / "build_plugin.py"), str(ug.dir)], capture_output=True, text=True, encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if r.returncode:
        return {"error": r.stdout[-500:]}
    r = subprocess.run([sys.executable, str(ROOT / "unity" / "install.py"), "install", str(ug.dir), str(cheats)],
                       capture_output=True, text=True)
    return {"ok": r.returncode == 0, "log": (r.stdout + r.stderr)[-300:]}


def kill_game(ug):
    import psutil
    killed = False
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower() == (ug.process or "").lower():
            p.kill(); killed = True
    if killed:
        time.sleep(2.5)


def launch_game():
    """Start the game only if it is not already running, then wait for the trainer plugin."""
    ug = need_unity()
    if game_process_running(ug.process):
        return {"error": "game is already running; refusing to kill or relaunch it"}
    log = ug.bep / "LogOutput.log"
    if log.exists():
        log.unlink()
    subprocess.Popen([str(ug.dir / ug.process)], cwd=str(ug.dir))
    for _ in range(60):
        if ug.loaded():
            break
        time.sleep(2)
    else:
        return {"error": "plugin did not load (is BepInEx installed? call install_trainer)"}
    # the plugin answers only once the game's first scene runs Update (slow loaders take a while)
    for _ in range(30):
        try:
            ug.cmd("dump", timeout=4)
            break
        except TimeoutError:
            time.sleep(2)
    time.sleep(3)
    try:
        CTX["keys"] = ug.keymap() or playbook().get("keys", {})
    except TimeoutError:
        CTX["keys"] = playbook().get("keys", {})
    if CTX["keys"]:
        save_playbook(keys=CTX["keys"], process=ug.process)
    return {"ok": True, "scene": ug.scene(), "keys": CTX["keys"] or "no Rewired map: use enter/space/esc/arrows/wasd",
            "next": "call reach_gameplay (it replays the saved route or finds one itself)"}


def guess_player_class():
    """Best live candidate for 'the player' class: playbook, then features, then name search."""
    pb = playbook()
    if pb.get("player_class"):
        return pb["player_class"]
    ug = need_unity()
    names = []
    fpath = gdir() / "features.json"
    if fpath.exists():
        for cands in json.loads(fpath.read_text(encoding="utf-8")).values():
            names += [c["class"] for c in cands]
    for full in sorted(set(names), key=lambda c: (("player" not in c.lower()), len(c))):
        if "player" in full.lower() or "hero" in full.lower():
            return full
    return "Player"


def reach_gameplay(player_class: str = "", budget_seconds: int = 90):
    """Check whether gameplay is active; otherwise return a screenshot before sending any menu input."""
    ug = need_unity()
    from trainerkit.navigator import Navigator
    import psutil
    pc = player_class or guess_player_class()
    running = lambda: any((p.info["name"] or "").lower() == ug.process.lower() for p in psutil.process_iter(["name"]))
    if not running():
        r = launch_game()
        if "error" in r:
            return r
    keys = CTX["keys"] or playbook().get("keys", {})
    nav = Navigator(ug, keys, ROOT / "app" / "shots" / "nav", log=lambda m: CTX["S"].events.append(
        {"tool": "reach_gameplay", "args": {}, "ok": True, "summary": m, "ms": 0}))
    try:
        if nav.alive(pc):
            CTX["startup"] = None
            return {"ok": True, "how": "player already live", "player_class": pc, "scene": ug.scene()}
        scene = ug.scene()
    except Exception as e:
        return _startup_screen(ug, pc, plugin_error=str(e))
    return _startup_screen(ug, pc, scene=scene)


def save_note(note: str):
    """Remember something about THIS game for next time (how to launch, menus, where values live, what failed)."""
    notes = playbook().get("notes", [])
    if note not in notes:
        notes.append(note)
    save_playbook(notes=notes[-40:])
    return {"saved": True, "notes": len(notes)}


def game_state(player_class: str = ""):
    """Current scene and whether the player object exists yet."""
    ug = need_unity()
    pc = player_class or guess_player_class()
    try:
        scene = ug.scene()
        out = ug.cmd(f"types {pc}")
    except Exception as e:
        return _startup_screen(ug, pc, plugin_error=str(e))
    import re
    matches = [int(n) for full, n in re.findall(r"^type (\S+) live=(\d+)", out, re.M)
               if full == pc or full.endswith("." + pc)]
    player_alive = bool(matches and matches[0] > 0)
    if player_alive:
        CTX["startup"] = None
        return {"scene": scene, "player_alive": True, "player_class": pc}
    return _startup_screen(ug, pc, scene=scene)


def press_keys(steps: list):
    """Validate input and require screenshot review between startup-menu actions."""
    ug = need_unity()
    if not isinstance(steps, list):
        return {"error": "steps must be a list of commands", "input_sent": False}
    try:
        parsed = [play.validate_step(step) for step in steps]
    except ValueError as e:
        return {"error": str(e), "input_sent": False}

    actions = [(command, parts) for command, parts in parsed
               if command not in ("sleep", "shot", "screenshot")]
    startup = CTX.get("startup")
    if startup and len(actions) > 1:
        return {"error": "startup guard: send one key/click action, then inspect the screenshot",
                "input_sent": False}

    screen_before = None
    action_name = " ".join(actions[0][1]).lower() if actions else None
    if startup:
        before_path = ROOT / "app" / "shots" / f"startup_before_{int(time.time() * 1000)}.png"
        before_capture = play.shot(ug.process, before_path, 0.5)
        if not before_capture:
            return {"error": "could not inspect current screen; no input was sent", "input_sent": False}
        from trainerkit import screen as scr
        screen_before = before_capture
        if scr.changed(screen_before, startup["screen"]):
            startup["screen"], startup["attempted"] = screen_before, []
        if action_name in startup["attempted"]:
            return {"error": "screen is unchanged; this input was already tried. Choose a different action or ask the user.",
                    "input_sent": False, "screen_changed": False}
        if action_name:
            startup["attempted"].append(action_name)

    shot = ROOT / "app" / "shots" / f"play_{int(time.time() * 1000)}.png"
    shot.parent.mkdir(exist_ok=True)
    input_steps = [step for step, (command, _) in zip(steps, parsed)
                   if command not in ("shot", "screenshot")]
    play.run(ug.process, input_steps + [f"shot {shot}"])
    import base64
    small = shot.with_name(shot.stem + "_small.png")
    pic = small if small.exists() else shot
    if not pic.exists():
        return {"error": "game screenshot could not be captured", "input_sent": bool(actions)}
    result = {"scene": ug.scene(), "image_b64": base64.b64encode(pic.read_bytes()).decode()}
    if startup:
        from trainerkit import screen as scr
        # 64x36 cell diff: a toggled option or a moved cursor counts (the old 16x9 hash missed both)
        changed = scr.changed(screen_before, str(pic))
        if changed:
            CTX["startup"] = {"screen": str(pic), "attempted": []}
        else:
            startup["screen"] = str(pic)
            CTX["startup"] = startup
        result["screen_changed"] = changed
        result["next_step"] = "inspect screenshot before next input" if changed else \
            "screen unchanged; do not repeat this input"
    return result


def find_key(candidates: str = "uiojklnmhgfecvbxqrtypz123456"):
    """Find which keyboard key changes the current screen (for menus without a readable key map)."""
    ug = need_unity()
    from PIL import Image, ImageChops
    def grab(n):
        return Image.open(play.shot(ug.process, ROOT / "app" / "shots" / f"fk_{n}.png", 0.25)).convert("L").resize((160, 90))
    play.focus(ug.process)
    base = grab("base")
    for k in list(candidates) + ["enter", "space"]:
        if k not in play.SC:
            continue
        play.tap(k); time.sleep(1.0)
        d = sum(ImageChops.difference(base, grab(k)).getdata()) / (160 * 90)
        if d > 12:
            return {"key": k, "diff": round(d, 1)}
    return {"key": None}


def inspect_class(class_name: str, offset: int = 0, limit: int = 40):
    """Page through live fields of the first object of a class."""
    out = need_unity().cmd(f"inspect {class_name}")
    lines = out.splitlines()
    status = next((line.strip() for line in lines if line.startswith("inspect")), "")
    fields = [line.strip() for line in lines if line.startswith("  ")]
    return {"status": status, **_page(fields, offset, limit, max_limit=40)}


def find_classes(name_part: str, offset: int = 0, limit: int = 40):
    out = need_unity().cmd(f"types {name_part}")
    classes = [l[5:] for l in out.splitlines() if l.startswith("type ")]
    return _page(classes, offset, limit, max_limit=40)


def get_value(class_name: str, member: str):
    return {"value": need_unity().get(class_name, member)}


def set_value(class_name: str, member: str, value: str):
    out = need_unity().cmd(f"set {class_name} {member} {value}")
    return {"result": [l for l in out.splitlines() if l.startswith("set ")][-1:]}


def add_unity_cheat(name: str, hotkey: str, class_name: str, member: str, mode: str, value: str = "0", check: str = ""):
    """mode: lock (infinite health/ammo: keeps current value), freeze (keep at value or @Member),
    set / add / multiply (one-shot). Hotkeys F1..F12 (NUM0..9, CTRL+F1 ...).
    check: for one-shot cheats that go through a helper/setter, WHERE the real value lives so it can be
    verified, as "Class Member.path[index]" (e.g. "PersistentData Money.m_PropertyArray[1]")."""
    ug = need_unity()
    f = gdir() / "cheats.txt"
    lines = [l for l in (f.read_text(encoding="utf-8").splitlines() if f.exists() else ["# name | hotkey | class | member | mode | value"])
             if not l.split("|")[0].strip().lower() == name.lower()]
    lines.append(f"{name} | {hotkey.upper()} | {class_name} | {member} | {mode} | {value}" + (f" | {check}" if check else ""))
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cfg = ug.bep / "config" / "universal_trainer.txt"
    if cfg.parent.exists():
        cfg.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        try:
            ug.cmd("reload")
        except TimeoutError:
            pass
    return {"cheats": [c["name"] for c in parse_cheats(f)]}


def verify_cheats(health_cheat: str = "", health_class: str = "", health_member: str = ""):
    """Switch every cheat on in the running game and check it really works; saves verify.json and
    screenshots for the release. Optionally a behaviour test: fight with/without the health cheat."""
    ug = need_unity()
    cheats = parse_cheats(gdir() / "cheats.txt")
    shots = gdir() / "shots"
    shots.mkdir(exist_ok=True)
    res = {}
    for c in cheats:
        try:
            res[c["name"]] = ug.verify(c, CTX["keys"])
        except Exception as e:
            res[c["name"]] = {"ok": False, "how": f"error: {e}"}
    if health_cheat and health_class and health_member:
        try:
            res[health_cheat]["fight_test"] = ug.damage_test(health_class, health_member, health_cheat, CTX["keys"] or {})
        except Exception as e:
            res[health_cheat]["fight_test"] = {"error": str(e)}
    for c in cheats:
        ug.cmd(f"on {c['name']}")
    time.sleep(0.8)
    play.shot(ug.process, shots / "trainer_on.png", 1.0)
    for c in cheats:
        ug.cmd(f"off {c['name']}")
    (gdir() / "verify.json").write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"summary": {n: r.get("ok") for n, r in res.items()}, "details": res}


def ensure_running():
    """Game running with the CURRENT plugin build (rebuild, compare, restart only when outdated).
    Returns the game's key map."""
    import install as unity_install
    ug = need_unity()
    if game_process_running(ug.process):
        try:
            ug.cmd("dump", timeout=4)
            if unity_install.installed_hash(ug.dir) == unity_install.source_hash():
                CTX["keys"] = ug.keymap() or playbook().get("keys", {})
                return CTX["keys"]
            CTX["S"].events.append({"tool": "ensure_running", "args": {}, "ok": True, "ms": 0,
                                    "summary": "game runs an older plugin build: restarting it"})
        except Exception:
            pass
        kill_game(ug)
    r = install_trainer()
    if not r.get("ok"):
        raise RuntimeError(f"install_trainer failed: {r}")
    r = launch_game()
    if "error" in r:
        raise RuntimeError(r["error"])
    return CTX["keys"]


def autopilot(nav_budget_seconds: int = 900, use_vision: bool = True):
    """Whole Unity pipeline: plugin, reach gameplay, find values by experiment, god-mode hooks, fun cheats,
    verification. Writes only verified cheats; returns the report."""
    from trainerkit.autopilot import Autopilot
    keys = ensure_running()
    log = lambda m: CTX["S"].events.append({"tool": "autopilot", "args": {}, "ok": True, "summary": str(m)[:300], "ms": 0})
    ap = Autopilot(need_unity(), gdir(), keys, log=log, use_vision=use_vision)
    rep = ap.run_all(nav_budget=max(60, min(int(nav_budget_seconds), 1800)))
    return {k: rep.get(k) for k in ("error", "cheats", "skipped", "seconds")} | {"report": str(gdir() / "autopilot_report.json")}


def snapshot_diff(before_tag: str, after_tag: str = "", condition: str = "changed", filter: str = ""):
    """Reflection-level memory scan. Call with only before_tag to take a snapshot of every numeric field of
    every live game object; do something in game; call again with after_tag to diff (decreased/increased/
    changed/unchanged). filter: substrings of 'Class#id|field.path' separated by '|'."""
    ug = need_unity()
    if not after_tag:
        out = ug.cmd(f"snap {before_tag}", timeout=60)
        return {"snapshot": before_tag, "result": [l for l in out.splitlines() if l.startswith("snap")]}
    out = ug.cmd(f"snap {after_tag}", f"diff {before_tag} {after_tag} {condition} {filter}".strip(), timeout=60)
    rows = [l[2:] for l in out.splitlines() if l.startswith("d ")]
    return {"matches": len(rows), "rows": rows[:60], "note": "row = key type before after; set with set_value(class, path, v)"}


def find_value(value: float, filter: str = ""):
    """Exact-value search over a fresh snapshot (like a first scan for the number on screen)."""
    ug = need_unity()
    out = ug.cmd("snap find", f"find find {value} {filter}".strip(), timeout=60)
    rows = [l[2:] for l in out.splitlines() if l.startswith("f ")]
    return {"matches": len(rows), "rows": rows[:60]}


def package_release():
    """Zip the verified trainer (BepInEx + plugin + cheat list) with a README listing how each cheat was checked."""
    need_unity()
    from trainerkit.package import build
    out = build(gdir())
    return {"release": str(out), "files": [p.name for p in out.iterdir()]}


def tools(S, _p, S_STR, S_NUM):
    lst = {"type": "array", "items": {"type": "string"}, "description": "steps"}
    return {
          "discover_features": (discover_features, "Unity: page through ALL ranked candidates; keep calling with next_offset until null. Page size max 20; rescans only with refresh=true.",
                                        _p(offset={"type": "integer", "description": "page offset", "optional": True},
                                            limit={"type": "integer", "description": "page size, max 20", "optional": True},
                                            refresh={"type": "boolean", "description": "rescan assembly", "optional": True})),
          "suggest_fun_cheats": (suggest_fun_cheats, "Page through ALL unusual game-code levers; continue until next_offset is null.",
                                         _p(offset={"type": "integer", "description": "page offset", "optional": True},
                                             limit={"type": "integer", "description": "page size, max 20", "optional": True},
                                             refresh={"type": "boolean", "description": "rescan assembly", "optional": True})),
        "install_trainer": (install_trainer, "Unity: install BepInEx + trainer plugin into the game.", _p()),
        "launch_game": (launch_game, "Unity: (re)start the game, wait for the trainer, read the key bindings.", _p()),
        "reach_gameplay": (reach_gameplay, "Check whether the player is live; otherwise capture the current screen WITHOUT pressing keys.",
                           _p(player_class=S_STR("player class if known", optional=True),
                              budget_seconds={"type": "integer", "description": "total time limit, max 90 seconds", "optional": True})),
        "save_note": (save_note, save_note.__doc__, _p(note=S_STR("one short fact"))),
        "game_state": (game_state, "Unity: scene name and whether the player object exists.",
                       _p(player_class=S_STR("player class, e.g. Game.Player", optional=True))),
        "press_keys": (press_keys, "At startup send one key/click action per call and inspect the screenshot. Use 'hold h 3'; hold releases automatically. Repeated input on an unchanged screen is blocked.",
                       _p(steps=lst)),
        "find_key": (find_key, "Find which key advances the current menu screen.", _p()),
          "inspect_class": (inspect_class, "Unity: page through all live fields; continue until next_offset is null.",
                                    _p(class_name=S_STR("class"),
                                        offset={"type": "integer", "description": "page offset", "optional": True},
                                        limit={"type": "integer", "description": "page size, max 40", "optional": True})),
        "find_classes": (find_classes, "Unity: classes whose name contains text, with live object counts.",
                                 _p(name_part=S_STR("text"),
                                     offset={"type": "integer", "description": "page offset", "optional": True},
                                     limit={"type": "integer", "description": "page size, max 40", "optional": True})),
        "get_value": (get_value, "Unity: read a live field/property; member may be a path like Money.m_PropertyArray[1].",
                      _p(class_name=S_STR("class"), member=S_STR("member or path"))),
        "set_value": (set_value, "Unity: write a live field/property (to test).",
                      _p(class_name=S_STR("class"), member=S_STR("member"), value=S_STR("value"))),
        "add_unity_cheat": (add_unity_cheat, add_unity_cheat.__doc__,
                            _p(name=S_STR("cheat name"), hotkey=S_STR("hotkey"), class_name=S_STR("class"),
                               member=S_STR("field or property"),
                               mode=S_STR("mode", enum=["lock", "freeze", "set", "add", "multiply"]),
                               value=S_STR("value or @OtherMember", optional=True),
                               check=S_STR("one-shot cheats: 'Class Member.path' where the real value is stored", optional=True))),
        "verify_cheats": (verify_cheats, verify_cheats.__doc__,
                          _p(health_cheat=S_STR("health cheat name", optional=True),
                             health_class=S_STR("class", optional=True), health_member=S_STR("member", optional=True))),
        "package_release": (package_release, package_release.__doc__, _p()),
        "autopilot": (autopilot, autopilot.__doc__,
                      _p(nav_budget_seconds={"type": "integer", "description": "time limit for menu navigation", "optional": True},
                         use_vision={"type": "boolean", "description": "ask the vision model on new menu screens", "optional": True})),
        "snapshot_diff": (snapshot_diff, snapshot_diff.__doc__,
                          _p(before_tag=S_STR("snapshot name"), after_tag=S_STR("second snapshot name", optional=True),
                             condition=S_STR("filter", enum=["decreased", "increased", "changed", "unchanged"], optional=True),
                             filter=S_STR("substrings, e.g. 'Player#|money'", optional=True))),
        "find_value": (find_value, find_value.__doc__,
                       _p(value=S_NUM("number shown in game"), filter=S_STR("substrings", optional=True))),
    }
