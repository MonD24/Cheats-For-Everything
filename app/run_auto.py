"""Headless autonomous run:  python app/run_auto.py "<path to game exe>" ["what you want"] [--no-vision]

Unity (Mono) games go through the deterministic autopilot (trainerkit/autopilot.py): the local vision model
is asked only to pick menu actions on new screens. Other engines fall back to the LLM agent loop."""
import json
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import tools  # noqa: E402
import unity_tools  # noqa: E402


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def watch(stop):
    seen = 0
    while not stop.is_set():
        ev = tools.S.events
        while seen < len(ev):
            e = ev[seen]; seen += 1
            print(f"  {'OK ' if e['ok'] else 'ERR'} {e['tool']:<18} {json.dumps(e['args'], ensure_ascii=False)[:90]}  -> {e['summary'][:160]}", flush=True)
        time.sleep(0.3)


ROOT = HERE.parent


def run_unity(use_vision):
    from trainerkit.autopilot import Autopilot
    ug = unity_tools.need_unity()
    keys = unity_tools.ensure_running()
    log(f"key map: {keys}")
    ap = Autopilot(ug, unity_tools.gdir(), keys, log=log, use_vision=use_vision)
    report = ap.run_all()
    if report.get("cheats"):
        rel = tools.call("package_release", {})
        report["release"] = rel.get("release")
    return report


def start_game(path, proc):
    """Start the exe; return the process that really owns the game window. Launch stubs (Unreal's
    Game.exe -> Game-Win64-Shipping.exe, launchers) hand over to a child process."""
    import subprocess
    import psutil
    from trainerkit import play
    log(f"starting {Path(path).name}")
    root = subprocess.Popen([path], cwd=str(Path(path).parent))
    found = None
    for _ in range(120):
        try:
            tree = [psutil.Process(root.pid)] + psutil.Process(root.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            tree = []
        # stubs exit after spawning: also look for any new process from the game's folder
        folder = str(Path(path).parent).lower()
        for pr in psutil.process_iter(["name", "exe"]):
            if (pr.info["exe"] or "").lower().startswith(folder) and pr not in tree:
                tree.append(pr)
        for pr in tree:
            try:
                name = pr.name()
            except psutil.NoSuchProcess:
                continue
            if play.window(name) and "crash" not in name.lower():
                found = name
        if found:
            break
        time.sleep(1)
    time.sleep(8)
    found = found or proc
    if found.lower() != proc.lower():
        log(f"game process is {found} (started through {Path(path).name})")
        tools.set_process(found)
    return found


def memory_navigator(path, proc, gdir):
    """Menu explorer without a plugin: 'gameplay' is decided by the vision model."""
    from types import SimpleNamespace
    from trainerkit.autopilot import Autopilot
    stub = SimpleNamespace(process=proc, scene=lambda: "", dir=Path(path).parent)
    ap = Autopilot(stub, gdir, {}, log=log)
    ap.player_candidates = lambda: {}

    def gameplay(_baseline):
        ans, _ = ap.ask('Is this live gameplay: the player character is controllable and the HUD is visible '
                        '(not a menu, logo, loading screen, dialogue or cutscene)? JSON only: {"gameplay": true|false}',
                        ap.shot("gp"))
        return "gameplay" if (ans or {}).get("gameplay") is True else None
    ap.in_gameplay = gameplay
    return ap


def run_memory(path):
    """Any engine without a plugin: vision-driven menus, HUD-oracle memory scan, pointer chains checked across
    a restart, trainer .exe."""
    from trainerkit.mem_autopilot import MemAutopilot
    S = tools.S
    proc = S.process or Path(path).name
    gdir = S.trainer_path().parent
    shipping = sorted(Path(path).parent.glob("*/Binaries/Win64/*-Shipping.exe"))
    if shipping and not unity_tools.game_process_running(proc):
        # Unreal: the real game is <Project>-Win64-Shipping.exe (the top exe is a launch stub)
        running = [x.name for x in shipping if unity_tools.game_process_running(x.name)]
        proc = running[0] if running else proc
    if not unity_tools.game_process_running(proc):
        proc = start_game(path, proc)
    else:
        tools.set_process(proc)
    g = memory_navigator(path, proc, gdir).reach_gameplay(900)
    if not g.get("ok"):
        return {"error": g.get("error")}
    res = MemAutopilot(proc, gdir, log=log).run_all()
    found = {n: r for n, r in res["values"].items() if r.get("ok")}
    if any(r.get("chains") and not r.get("durable") for r in found.values()):
        # the Cheat Engine routine: restart the game, keep only the chains that still lead to the value
        log("restarting the game to check which pointer chains survive")
        import psutil
        for pr in psutil.process_iter(["name"]):
            if (pr.info["name"] or "").lower() == proc.lower():
                pr.kill()
        time.sleep(3)
        proc = start_game(path, proc)
        g = memory_navigator(path, proc, gdir).reach_gameplay(900)
        if g.get("ok"):
            mem = MemAutopilot(proc, gdir, log=log)
            found = {n: mem.revalidate(r) for n, r in found.items()}
    S.trainer["cheats"] = []
    hotkey = 1
    for name, r in found.items():
        if r.get("durable") and r.get("chains"):
            target = {"chains": r["chains"]}
        elif "static" in r["where"]:
            mod, off = r["where"].split()[0].split("+")
            target = {"module": mod, "offset": off}
        else:
            target = {"address": r["address"]}
        if any(w in name for w in ("money", "gold", "cash", "coin", "credit")):
            tools.add_cheat(f"+1000 {name.title()}", f"F{hotkey}", "add", target, r["type"], 1000)
        else:
            tools.add_cheat(f"Freeze {name.title()}", f"F{hotkey}", "freeze", target, r["type"], r["value"])
        hotkey += 1
    report = {"values": {n: {k: v for k, v in r.items() if k != "chains"} for n, r in res["values"].items()},
              "cheats": [c["name"] for c in S.trainer["cheats"]]}
    if S.trainer["cheats"]:
        report["build"] = tools.call("build_trainer", {})
        weak = [n for n, r in found.items() if not r.get("durable")]
        if weak:
            report["warning"] = f"{weak}: address valid for this game session only (no pointer chain survived a restart)"
        try:
            from trainerkit.package import build
            report["release"] = str(build(gdir))
        except Exception as e:
            report["release_error"] = str(e)
    return report


def run_agent(path, wish):
    import agent
    a = agent.Agent()
    log(f"model: {a.model}")
    stop = threading.Event()
    threading.Thread(target=watch, args=(stop,), daemon=True).start()
    reply = a.send(f"Сделай трейнер для игры {path}. Пожелания: {wish}. Сделай всё сам до готового релиза.")
    time.sleep(0.5); stop.set()
    return reply


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0]
    wish = args[1] if len(args) > 1 else "сам выбери полезные читы"
    t = time.time()
    if "--no-vision" not in sys.argv:
        from trainerkit import lmstudio
        model = lmstudio.ensure(log)
        log(f"vision model: {model or 'none - menus explored without vision, HUD checks off'}")
        if not model:
            sys.argv.append("--no-vision")
    sel = tools.call("select_game", {"path": path})
    if "error" in sel:
        sys.exit(f"select_game: {sel['error']}")
    if sel.get("blocked"):
        sys.exit(f"Трейнер не делаю: {sel['blocked']}")
    log(f"game: {sel['name']}  engine: {sel['engine']}  route: {sel['route']}  process: {sel['process']}")
    b = tools.call("backup_saves", {})
    log(f"save backup: {b.get('backups') or b.get('error')}")
    if sel["route"] == "unity":
        report = run_unity("--no-vision" not in sys.argv)
        log("=== autopilot report")
        print(json.dumps({k: report.get(k) for k in ("error", "cheats", "skipped", "release", "seconds")},
                         indent=1, ensure_ascii=False), flush=True)
    elif "--agent" in sys.argv:
        print(f"\n=== agent: {run_agent(path, wish)}", flush=True)
    else:
        report = run_memory(path)
        log("=== memory autopilot report")
        print(json.dumps(report, indent=1, ensure_ascii=False, default=str), flush=True)
    log(f"total {time.time() - t:.0f}s")


if __name__ == "__main__":
    main()
