"""Tools the local agent can call. Each returns a JSON-serialisable dict.
Game-touching tools are refused while the selected game is flagged (anti-cheat / online)."""
import base64
import io
import json
import re
import shutil
import subprocess
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import scanner  # noqa: E402
from trainerkit.runtime import Game, Cheat, VALUE_TYPES, Hotkey  # noqa: E402

ONLINE_ONLY = {"dota 2", "counter-strike 2", "battlefield", "dead by daylight", "fall guys", "valorant",
               "apex legends", "pubg", "fortnite", "the crew motorfest", "the outlast trials", "peak", "r.e.p.o."}


class Session:
    def __init__(self):
        self.game = None          # dict from trainerkit.gamescan.scan()
        self.process = None
        self.blocked = None       # reason string if trainers are not allowed
        self.trainer = None       # dict, saved to games/<slug>/trainer.json
        self.events = []          # tool log for the UI
        self._games = None

    # ------------------------------------------------------------ helpers
    def slug(self):
        return re.sub(r"[^\w\-]+", "_", self.game["name"]).strip("_") if self.game else "unknown"

    def trainer_path(self):
        return ROOT / "games" / self.slug() / "trainer.json"

    def lessons_path(self):
        return ROOT / "games" / self.slug() / "lessons.json"

    def checkpoint_path(self):
        return ROOT / "games" / self.slug() / "agent_checkpoint.json"

    def save_trainer(self):
        p = self.trainer_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.trainer, indent=2, ensure_ascii=False), encoding="utf-8")

    def need_game(self):
        if not self.game:
            raise RuntimeError("no game selected: call select_game first")
        if self.blocked:
            raise RuntimeError(f"trainers are not allowed for this game: {self.blocked}")
        if not self.process:
            raise RuntimeError("game process unknown: start the game, then call set_process")

    def games(self):
        if self._games is None:
            from trainerkit import gamescan
            self._games = gamescan.library()
        return self._games


S = Session()


def quiet(fn, *a):
    buf = io.StringIO()
    with redirect_stdout(buf):
        r = fn(*a)
    return r, buf.getvalue().strip()


def candidates():
    meta, addrs, _ = scanner.load()
    return meta, addrs


LEARNED_SUCCESS_TOOLS = {
    "reach_gameplay", "discover_features", "suggest_fun_cheats", "inspect_class", "find_classes",
    "verify_cheats", "package_release", "test_cheat", "build_trainer", "save_note", "save_lesson",
}
LEARNED_ARGUMENTS = {"name", "class_name", "member", "player_class", "hotkey", "mode", "condition",
                     "value_type", "offset", "limit"}


def read_lessons(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        lessons = data.get("lessons", [])
        return [item for item in lessons if isinstance(item, dict)][-40:] if isinstance(lessons, list) else []
    except (OSError, ValueError, AttributeError):
        return []


def remember_lesson(path, lesson, evidence):
    lesson = " ".join(str(lesson).split())[:500]
    if not lesson:
        return {"error": "lesson cannot be empty"}
    if evidence not in {"verified", "user_hint", "observed_failure"}:
        return {"error": "evidence must be verified, user_hint, or observed_failure"}
    path = Path(path)
    lessons = read_lessons(path)
    existing = next((item for item in lessons if item.get("lesson", "").casefold() == lesson.casefold()), None)
    now = time.strftime("%Y-%m-%d %H:%M")
    if existing:
        existing["evidence"] = evidence
        existing["updated"] = now
        existing["occurrences"] = int(existing.get("occurrences", 1)) + 1
    else:
        lessons.append({"lesson": lesson, "evidence": evidence, "updated": now, "occurrences": 1})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"lessons": lessons[-40:]}, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"saved": True, "evidence": evidence, "lessons": len(lessons[-40:])}


def recent_experience(path, limit=8):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()[-200:]
    except OSError:
        return []
    relevant = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        tool = event.get("tool")
        if event.get("ok") and tool not in LEARNED_SUCCESS_TOOLS:
            continue
        args = event.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        relevant.append({"tool": tool, "ok": bool(event.get("ok")),
                         "args": {key: value for key, value in args.items() if key in LEARNED_ARGUMENTS},
                         "result": str(event.get("summary", ""))[:240]})
    return relevant[-max(1, min(int(limit), 12)):]


def learning_for_game():
    if not S.game:
        return {}
    folder = S.trainer_path().parent
    lessons = read_lessons(folder / "lessons.json")[-5:]
    memory = {"lessons": [{"lesson": item.get("lesson", "")[:300], "evidence": item.get("evidence", "unknown")}
                           for item in lessons],
              "recent_experience": recent_experience(folder / "agent_log.jsonl", limit=5)}
    checkpoint = read_agent_checkpoint(folder / "agent_checkpoint.json")
    if checkpoint and checkpoint.get("status") in {"active", "interrupted", "incomplete", "error"}:
        memory["unfinished_task"] = checkpoint
    return memory


def should_remember_user_hint(text):
    lowered = str(text).casefold()
    if any(marker in lowered for marker in ("smoke test", "harness test", "тестируй", "тестовый прогон")):
        return False
    cues = ("hold ", "press ", "tap ", "click ", "choose ", "select ", "skip ", "don't press",
            "do not press", "one player", "single-player", "зажм", "удерж", "нажм", "выбер",
            "пропуст", "скип", "не нажим", "не жми", "не выбира", "один игрок", "дольше держ")
    return any(cue in lowered for cue in cues)


def save_agent_checkpoint(task, phase, last_tool="", result="", status="active"):
    if not S.game:
        return False
    checkpoint = {
        "task": " ".join(str(task).split())[:500],
        "phase": " ".join(str(phase).split())[:100],
        "last_tool": str(last_tool)[:80],
        "result": " ".join(str(result).split())[:400],
        "status": status if status in {"active", "interrupted", "incomplete", "error", "completed"} else "active",
        "updated": time.strftime("%Y-%m-%d %H:%M"),
    }
    path = S.checkpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(checkpoint, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def read_agent_checkpoint(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return {key: str(data.get(key, ""))[:500] for key in
                ("task", "phase", "last_tool", "result", "status", "updated")}
    except (OSError, ValueError, TypeError):
        return None


# ------------------------------------------------------------------ tools

def list_games(filter: str = ""):
    gs = [g for g in S.games() if filter.lower() in g["name"].lower()]
    return {"games": [{"name": g["name"], "store": g["store"], "path": g["path"]} for g in gs[:40]]}


BIN_DIR = re.compile(r"(?i)^(bin|bin\d*|x64|x86|win64|win32|x64_dx\d+|x64_vk|binaries|retail|shipping|game|exe)$")


def game_root(exe_dir):
    """Steam/Epic library folder of the game (steamapps/common/<Game>), else the first non-binary ancestor."""
    p = Path(exe_dir)
    for anc in [p, *p.parents]:
        if anc.parent.name.lower() == "common":
            return anc
    while BIN_DIR.match(p.name) and p.parent != p:
        p = p.parent
    return p


def select_game(name: str = "", path: str = ""):
    exe = None
    if path and path.lower().endswith(".exe"):
        exe, path = Path(path).name, str(Path(path).parent)
    target = path or name
    if not path:
        exact = [g for g in S.games() if g["name"].lower() == name.lower()]
        loose = [g for g in S.games() if name.lower() in g["name"].lower()]
        pick = (exact or loose or [None])[0]
        if pick:
            target = pick["name"]
    from trainerkit import gamescan
    try:
        info = gamescan.scan(target)
    except FileNotFoundError:
        return {"error": f"game not found: {target}"}
    S.game, S.process, S.blocked = info, None, None
    prod = (info.get("engine") or {}).get("product")
    if path and prod:
        info["name"] = prod
    info["dir"] = path or info.get("path")
    if path and BIN_DIR.match(info["name"]):
        # exe deep inside bin/x64_dx12 or Binaries/Win64: name the game after its root folder
        info["name"] = game_root(path).name
    ac = info.get("anti_cheat") or []
    if ac:
        S.blocked = "anti-cheat: " + ", ".join(ac)
    elif any(k in info["name"].lower() for k in ONLINE_ONLY):
        S.blocked = "online / multiplayer game"
    exes = [e for e in (info.get("executables") or {}).keys() if "crashhandler" not in e.lower()]
    if exe:
        S.process = exe
    elif len(exes) == 1:
        S.process = Path(exes[0]).name
    p = S.trainer_path()
    S.trainer = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {
        "title": f"{info['name']} Trainer", "process": S.process or "", "cheats": []}
    try:
        import unity_tools
        unity_tools.on_select(info, S)
    except Exception as e:
        pass
    return {"name": info["name"], "engine": (info.get("engine") or {}).get("label"),
            "route": "unity" if (info.get("engine") or {}).get("key") == "unity-mono" else "memory",
            "executables": exes, "process": S.process, "anti_cheat": ac,
            "blocked": S.blocked, "saves": info.get("saves", []),
            "existing_cheats": [c["name"] for c in S.trainer["cheats"]],
            "playbook": getattr(S, "playbook", None) or "none yet (first time with this game)",
            **learning_for_game()}


def running_processes(filter: str = ""):
    import psutil
    names = sorted({p.info["name"] for p in psutil.process_iter(["name"])
                    if p.info["name"] and filter.lower() in p.info["name"].lower()})
    return {"processes": names[:60]}


def set_process(process: str):
    S.process = process
    if S.trainer is not None:
        S.trainer["process"] = process
    return {"process": process}


SAVE_FILE = re.compile(r"(?i)(save|sav|slot|profile|progress|\.sav$|\.dat$|\.json$)")


def guess_save_folders(name, exe_names=()):
    """Save folders when the engine fingerprint knows none: folders named like the game (or its exe) under the usual
    roots that hold save-like files. Witcher 3 keeps them in Documents/The Witcher 3/gamesaves, for example."""
    def norm(x):
        return re.sub(r"[^a-z0-9]", "", x.lower())
    keys = {norm(name)} | {norm(Path(e).stem) for e in exe_names if e}
    keys |= {norm(re.split(r"[:\-—–(]", name)[0])}
    keys = {k for k in keys if len(k) >= 4}
    home = Path.home()
    roots = [home / "Documents", home / "Documents" / "My Games", home / "Saved Games",
             home / "AppData" / "Local", home / "AppData" / "Roaming", home / "OneDrive" / "Documents"]
    roots += [d for d in (home / "AppData" / "LocalLow").glob("*") if d.is_dir()]
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            subdirs = [d for d in root.iterdir() if d.is_dir()]
        except OSError:
            continue
        for d in subdirs:
            nd = norm(d.name)
            if any(k in nd or (len(nd) >= 4 and nd in k) for k in keys):
                try:
                    files = [f for f in d.rglob("*") if f.is_file()][:400]
                except OSError:
                    continue
                if any(SAVE_FILE.search(f.name) for f in files):
                    found.append(str(d))
    return sorted(set(found))


def backup_saves():
    if S.game and not S.game.get("saves"):
        S.game["saves"] = guess_save_folders(S.game.get("name", ""), list((S.game.get("executables") or {}).keys()) + [S.process or ""])
    if not S.game or not S.game.get("saves"):
        return {"error": "no save folder known for this game"}
    done = []
    for i, folder in enumerate(S.game["saves"]):
        done.append(backup_folder(folder, f"{S.slug()}-saves{i}"))
    return {"backups": done}


BACKUPS = Path.home() / ".cheats-for-everything" / "backups"


def backup_folder(folder, name):
    """Zip a save folder to ~/.cheats-for-everything/backups/<name>/<timestamp>.zip; returns a summary."""
    import zipfile
    src = Path(folder)
    out = BACKUPS / name / time.strftime("%Y%m%d-%H%M%S.zip")
    out.parent.mkdir(parents=True, exist_ok=True)
    n = size = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in src.rglob("*"):
            if f.is_file():
                try:
                    z.write(f, f.relative_to(src))
                    n += 1; size += f.stat().st_size
                except OSError:
                    continue
    return f"{out}  ({n} files, {size / 2**20:.1f} MB)"


def first_scan(value_type: str, value: float):
    S.need_game()
    if value_type not in VALUE_TYPES:
        return {"error": f"value_type must be one of {list(VALUE_TYPES)}"}
    _, log = quiet(scanner.first, S.process, value_type, str(value))
    _, addrs = candidates()
    return {"candidates": len(addrs), "log": log}


def next_scan(condition: str, value: float = None):
    S.need_game()
    _, log = quiet(scanner.next_scan, condition, None if value is None else str(value))
    _, addrs = candidates()
    return {"candidates": len(addrs)}


def list_candidates(limit: int = 10):
    S.need_game()
    meta, addrs = candidates()
    g = Game(S.process); g.attach(wait=False)
    rows = []
    for a in addrs[:limit]:
        try:
            rows.append({"address": f"0x{a:X}", "value": g.read(a, meta["type"]), "where": scanner.where(g.pm, a)})
        except Exception:
            rows.append({"address": f"0x{a:X}", "value": None})
    return {"type": meta["type"], "total": len(addrs), "candidates": rows}


def write_value(address: str, value_type: str, value: float):
    S.need_game()
    g = Game(S.process); g.attach(wait=False)
    a = int(address, 16)
    g.write(a, value_type, value)
    return {"written": True, "read_back": g.read(a, value_type)}


def check_aob(pattern: str, module: str = ""):
    S.need_game()
    import pymem.pattern, pymem.process
    from trainerkit.runtime import aob_regex
    g = Game(S.process); g.attach(wait=False)
    rx = aob_regex(pattern)
    if module:
        mod = pymem.process.module_from_name(g.pm.process_handle, module)
        hits = pymem.pattern.pattern_scan_module(g.pm.process_handle, mod, rx, return_multiple=True)
    else:
        hits = pymem.pattern.pattern_scan_all(g.pm.process_handle, rx, return_multiple=True)
    return {"matches": len(hits), "unique": len(hits) == 1, "first": [f"0x{h:X}" for h in hits[:5]]}


def screenshot():
    """Screenshot of the game window; the image is attached for the model to look at."""
    if not S.process:
        return {"error": "no game process"}
    out = ROOT / "app" / "shots" / f"{int(time.time())}.png"
    out.parent.mkdir(exist_ok=True)
    from trainerkit.winshot import shot
    pic = shot(S.process, out, 0.5)
    if not pic:
        return {"error": f"no visible window for {S.process}"}
    pic = Path(pic)
    return {"image_b64": base64.b64encode(pic.read_bytes()).decode(), "file": str(pic)}


def add_cheat(name: str, hotkey: str, type: str, target: dict, value_type: str = "", value: float = 0,
              bytes: str = ""):
    if S.trainer is None:
        return {"error": "select a game first"}
    Hotkey(hotkey)
    c = {"name": name, "hotkey": hotkey.upper(), "type": type, "target": target}
    if type == "patch":
        c["bytes"] = bytes
    else:
        if value_type not in VALUE_TYPES:
            return {"error": f"value_type must be one of {list(VALUE_TYPES)}"}
        c["value_type"], c["value"] = value_type, value
    S.trainer["cheats"] = [x for x in S.trainer["cheats"] if x["name"] != name and x["hotkey"] != c["hotkey"]] + [c]
    S.trainer["process"] = S.process or S.trainer.get("process", "")
    S.save_trainer()
    return {"saved": True, "cheats": [x["name"] for x in S.trainer["cheats"]]}


def remove_cheat(name: str):
    S.trainer["cheats"] = [x for x in S.trainer["cheats"] if x["name"] != name]
    S.save_trainer()
    return {"cheats": [x["name"] for x in S.trainer["cheats"]]}


def test_cheat(name: str):
    """Resolve the cheat's target in the running game and apply it once; report before/after."""
    S.need_game()
    spec = next((c for c in S.trainer["cheats"] if c["name"] == name), None)
    if not spec:
        return {"error": f"no cheat named {name}"}
    g = Game(S.process); g.attach(wait=False)
    c = Cheat(g, spec)
    addr = c.addr()
    res = {"address": f"0x{addr:X}"}
    if spec["type"] == "patch":
        n = len(bytes.fromhex(spec["bytes"].replace(" ", "")))
        res["original_bytes"] = g.pm.read_bytes(addr, n).hex(" ")
        return res
    res["before"] = g.read(addr, spec["value_type"])
    if spec["type"] == "freeze":
        c.on = True; c.tick()
    else:
        c.press()
    res["after"] = g.read(addr, spec["value_type"])
    res["ok"] = c.error is None
    return res


def build_trainer():
    if not S.trainer or not S.trainer["cheats"]:
        return {"error": "no cheats yet"}
    S.save_trainer()
    r = subprocess.run([sys.executable, str(ROOT / "build_trainer.py"), str(S.trainer_path())],
                       capture_output=True, text=True, timeout=900)
    out = (r.stdout + r.stderr).strip()
    exe = re.search(r"built (.+\.exe)", out)
    return {"ok": bool(exe), "exe": exe.group(1) if exe else None, "log": out[-400:]}


def memory_autopilot():
    """Any engine without a plugin: reach gameplay, read numbers off the HUD (or menus), find them in memory,
    turn heap addresses into pointer chains checked across a game restart, build and package the .exe trainer."""
    if not S.game:
        return {"error": "select a game first"}
    import run_auto
    exe = Path(S.game.get("dir") or S.game.get("path", "")) / (S.process or "")
    return run_auto.run_memory(str(exe))


def save_lesson(lesson: str, evidence: str):
    """Persist a concise game-specific lesson and label whether it was verified or user-provided."""
    if not S.game:
        return {"error": "select a game first"}
    return remember_lesson(S.lessons_path(), lesson, evidence)


# ------------------------------------------------------------------ schemas for the model

def _p(**props):
    req = [k for k, v in props.items() if not v.pop("optional", False)]
    return {"type": "object", "properties": props, "required": req}


S_STR = lambda d, **k: {"type": "string", "description": d, **k}
S_NUM = lambda d, **k: {"type": "number", "description": d, **k}

TOOLS = {
    "list_games": (list_games, "Installed games (Steam/Epic/...).", _p(filter=S_STR("name filter", optional=True))),
    "select_game": (select_game, "Pick the game to work on; reports engine, exe, anti-cheat. Must be called first.",
                    _p(name=S_STR("game name"), path=S_STR("or an install folder / exe path", optional=True))),
    "running_processes": (running_processes, "Running process names (to find the game exe).",
                          _p(filter=S_STR("filter", optional=True))),
    "set_process": (set_process, "Set the game's process name, e.g. Game.exe.", _p(process=S_STR("exe name"))),
    "backup_saves": (backup_saves, "Back up the game's save folders before touching anything.", _p()),
    "first_scan": (first_scan, "First memory scan for an exact value the player sees.",
                   _p(value_type=S_STR("int32 for whole numbers, float for most health/stamina", enum=list(VALUE_TYPES)),
                      value=S_NUM("value shown in game"))),
    "next_scan": (next_scan, "Filter candidates after the value changed in game.",
                  _p(condition=S_STR("filter", enum=["exact", "increased", "decreased", "changed", "unchanged"]),
                     value=S_NUM("new value (for exact)", optional=True))),
    "list_candidates": (list_candidates, "Show remaining candidate addresses with live values.",
                        _p(limit={"type": "integer", "description": "how many", "optional": True})),
    "write_value": (write_value, "Write a test value to an address (confirm with the user it changed on screen).",
                    _p(address=S_STR("hex address"), value_type=S_STR("type", enum=list(VALUE_TYPES)), value=S_NUM("value"))),
    "check_aob": (check_aob, "Check a byte signature is unique in the game (required before using it).",
                  _p(pattern=S_STR("e.g. '48 8B ?? 05'"), module=S_STR("module name", optional=True))),
    "screenshot": (screenshot, "Look at the game screen.", _p()),
    "add_cheat": (add_cheat, "Add/replace a cheat in the trainer.",
                  _p(name=S_STR("e.g. Infinite Health"), hotkey=S_STR("F1..F12, NUM0..9, CTRL+F1 ..."),
                     type=S_STR("cheat type", enum=["freeze", "set", "add", "patch"]),
                     target={"type": "object", "description": "{'address':'0x..'} or {'module':'Game.exe','offset':'0x..','pointers':[..]} or {'aob':'..','aob_offset':0}"},
                     value_type=S_STR("value type", enum=list(VALUE_TYPES), optional=True),
                     value=S_NUM("value for freeze/set/add", optional=True),
                     bytes=S_STR("patch bytes, e.g. '90 90 90'", optional=True))),
    "remove_cheat": (remove_cheat, "Remove a cheat.", _p(name=S_STR("cheat name"))),
    "test_cheat": (test_cheat, "Apply a cheat once in the running game and read the value back.", _p(name=S_STR("cheat name"))),
    "build_trainer": (build_trainer, "Build the standalone trainer .exe.", _p()),
    "memory_autopilot": (memory_autopilot, memory_autopilot.__doc__, _p()),
    "save_lesson": (save_lesson, save_lesson.__doc__,
                    _p(lesson=S_STR("concise reusable game-specific lesson"),
                       evidence=S_STR("source", enum=["verified", "user_hint", "observed_failure"]))),
}


import unity_tools  # noqa: E402
TOOLS.update(unity_tools.tools(S, _p, S_STR, S_NUM))


def schemas():
    return [{"type": "function", "function": {"name": n, "description": d, "parameters": p}}
            for n, (_, d, p) in TOOLS.items()]


def call(name, args):
    fn = TOOLS[name][0]
    t0 = time.time()
    try:
        res = fn(**(args or {}))
    except Exception as e:
        res = {"error": f"{type(e).__name__}: {e}"}
    ev = {"tool": name, "args": args, "ok": "error" not in res,
          "summary": json.dumps({k: v for k, v in res.items() if k != "image_b64"}, ensure_ascii=False)[:600],
          "ms": int((time.time() - t0) * 1000), "time": time.strftime("%H:%M:%S")}
    if S.game:  # per-game journal: what the agent did, for debugging and for the user to send
        try:
            p = S.trainer_path().parent / "agent_log.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception:
            pass
    S.events.append({"tool": name, "args": args, "ok": "error" not in res,
                     "summary": json.dumps({k: v for k, v in res.items() if k != "image_b64"}, ensure_ascii=False)[:300],
                     "ms": int((time.time() - t0) * 1000)})
    return res
