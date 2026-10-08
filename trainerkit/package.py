"""Package a verified trainer as a zip you can hand to someone.

  python -m trainerkit.package games/<Game>

Two kinds of trainer:
  - Unity plugin: cheats.txt (+ cheats.json with descriptions and evidence from the autopilot) -> zip with
    BepInEx + UniversalTrainer.dll that extracts into the game folder;
  - standalone .exe (memory route): dist/*.exe + trainer.json -> zip that runs next to the game.
The README lists every cheat with what it does and how it was verified (fight test / measured / value check).
"""
import json
import shutil
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNITY = ROOT / "unity"


def level(info):
    """Short, honest verification level for one cheat."""
    ev = (info.get("evidence") or "").lower()
    if not info.get("ok", True):
        return "not verified"
    if "fight:" in ev and "inconclusive" not in ev and "failed" not in ev:
        return "tested in a real fight"
    if any(w in ev for w in ("jump height", "walked", "hud shows", "screen cells", "hud follows", "measured")):
        return "measured in game"
    if "developer flag" in ev and "react" not in ev:
        return "developer switch (effect not seen during testing)"
    return "checked in game (value)"


def load_cheats(game_dir):
    """[{hotkey, name, desc, level}] from cheats.json (autopilot) or cheats.txt / trainer.json."""
    verify = json.loads((game_dir / "verify.json").read_text(encoding="utf-8")) if (game_dir / "verify.json").exists() else {}
    rich = game_dir / "cheats.json"
    if rich.exists():
        items = json.loads(rich.read_text(encoding="utf-8"))
    elif (game_dir / "cheats.txt").exists():
        sys.path.insert(0, str(ROOT))
        from trainerkit.unity_session import parse_cheats
        items = parse_cheats(game_dir / "cheats.txt")
    else:
        items = json.loads((game_dir / "trainer.json").read_text(encoding="utf-8"))["cheats"]
    out = []
    for c in items:
        info = {**verify.get(c["name"], {}), **({"evidence": c["evidence"]} if c.get("evidence") else {})}
        out.append({"hotkey": c["hotkey"], "name": c["name"], "desc": c.get("desc", ""), "level": level(info)})
    return out


def build(game_dir):
    game_dir = Path(game_dir).resolve()
    meta_path = game_dir / "game.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {"name": game_dir.name.replace("_", " ")}
    cheats = load_cheats(game_dir)
    kind = "plugin" if (game_dir / "cheats.txt").exists() else "exe"
    slug = "".join(ch for ch in meta["name"] if ch.isalnum()) + "Trainer"
    out = game_dir / "release"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    readme = readme_md(meta, cheats, kind)

    zpath = out / f"{slug}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        if kind == "plugin":
            # extracts straight into the game folder; BepInEx's own text files go under BepInEx/ so nothing
            # of the game is overwritten
            for f in (UNITY / "vendor" / "bepinex").rglob("*"):
                if f.is_file():
                    rel = f.relative_to(UNITY / "vendor" / "bepinex")
                    if rel.parent == Path(".") and f.suffix == ".txt":
                        rel = Path("BepInEx") / rel.name
                    z.write(f, rel)
            # the DLL compiled against THIS game's Unity (the shared unity/ copy is from the last build)
            own = Path(meta.get("dir", "")) / "BepInEx" / "plugins" / "UniversalTrainer.dll"
            z.write(own if meta.get("dir") and own.exists() else UNITY / "UniversalTrainer.dll",
                    "BepInEx/plugins/UniversalTrainer.dll")
            z.write(game_dir / "cheats.txt", "BepInEx/config/universal_trainer.txt")
            z.writestr("BepInEx/BepInEx-LICENSE-notice.txt",
                       "BepInEx (LGPL-2.1) https://github.com/BepInEx/BepInEx - included unmodified.\n")
        else:
            exe = next(iter(sorted((game_dir / "dist").glob("*.exe"))), None) if (game_dir / "dist").exists() else None
            if not exe:
                raise FileNotFoundError("no dist/*.exe: build the trainer first")
            z.write(exe, exe.name)
            z.write(game_dir / "trainer.json", "trainer.json")
        z.writestr("TRAINER_README.md", readme)
    (out / "README.md").write_text(readme, encoding="utf-8")
    print(f"release ready: {zpath}")
    return out


def readme_md(meta, cheats, kind):
    if kind == "plugin":
        steps = ["Extract the archive into the game folder (next to the game .exe).",
                 "Start the game. The cheat list appears top-left; Insert hides it."]
        uninstall = "Delete winhttp.dll, doorstop_config.ini, .doorstop_version and the BepInEx folder."
    else:
        steps = ["Start the game.", "Run the trainer .exe; it waits for the game and shows the cheat list."]
        uninstall = "Delete the trainer .exe and trainer.json."
    tested = f"{meta.get('tested_version', '')} {time.strftime('%Y-%m-%d')}".strip()
    return "\n".join([
        f"# {meta['name']} trainer", "",
        "| Hotkey | Cheat | What it does | How it was checked |", "|---|---|---|---|",
        *[f"| `{c['hotkey']}` | {c['name']} | {c['desc']} | {c['level']} |" for c in cheats], "",
        "## Install", *[f"{i}. {s}" for i, s in enumerate(steps, 1)], "",
        "## Uninstall", uninstall, "",
        "## Notes", "- Single-player only. Back up your saves first.", f"- Tested on: {tested}.",
        "- Made with Cheats For Everything (https://github.com/MonD24/Cheats-For-Everything).", ""])


if __name__ == "__main__":
    build(sys.argv[1])
