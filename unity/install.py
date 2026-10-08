"""Install / uninstall BepInEx + UniversalTrainer into a Unity (Mono) game. Only adds files;
the list of added files is kept in <game>/BepInEx/.trainer_installed so uninstall removes exactly them.
  python unity/install.py install   "<game folder>" [cheats.txt]
  python unity/install.py uninstall "<game folder>"
  python unity/install.py cheats    "<game folder>" cheats.txt     (update the cheat list only)"""
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDOR = HERE / "vendor" / "bepinex"


def install(game, cheats=None):
    game = Path(game)
    if not list(game.glob("*_Data/Managed/Assembly-CSharp.dll")):
        sys.exit("not a Unity Mono game")
    added = []
    for src in VENDOR.rglob("*"):
        if src.is_file():
            dst = game / src.relative_to(VENDOR)
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                added.append(str(dst.relative_to(game)))
    plug = game / "BepInEx" / "plugins" / "UniversalTrainer.dll"
    plug.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HERE / "UniversalTrainer.dll", plug)
    added.append(str(plug.relative_to(game)))
    # which source the installed DLL was built from (csc output differs on every build, the source does not)
    stamp = plug.with_suffix(".src")
    stamp.write_text(source_hash(), encoding="utf-8")
    added.append(str(stamp.relative_to(game)))
    (game / "BepInEx" / "config").mkdir(parents=True, exist_ok=True)
    hide_manager(game / "BepInEx" / "config" / "BepInEx.cfg")
    if cheats:
        set_cheats(game, cheats)
    manifest = game / "BepInEx" / ".trainer_installed"
    old = manifest.read_text().splitlines() if manifest.exists() else []
    manifest.write_text("\n".join(sorted(set(old + added))))
    print(f"installed into {game} ({len(added)} files added)")


def hide_manager(cfg):
    """Some games destroy every GameObject they did not create (or on scene load), plugins with them;
    BepInEx's HideManagerGameObject keeps the plugin host out of their reach."""
    import re
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    if re.search(r"(?m)^HideManagerGameObject\s*=", text):
        text = re.sub(r"(?m)^HideManagerGameObject\s*=.*$", "HideManagerGameObject = true", text)
    elif "[Chainloader]" in text:
        text = text.replace("[Chainloader]", "[Chainloader]\n\nHideManagerGameObject = true", 1)
    else:
        text += "\n[Chainloader]\n\nHideManagerGameObject = true\n"
    cfg.write_text(text, encoding="utf-8")


def source_hash():
    import hashlib
    return hashlib.md5((HERE / "UniversalTrainer.cs").read_bytes()).hexdigest()


def installed_hash(game):
    stamp = Path(game) / "BepInEx" / "plugins" / "UniversalTrainer.src"
    return stamp.read_text(encoding="utf-8").strip() if stamp.exists() else None


def set_cheats(game, cheats):
    dst = Path(game) / "BepInEx" / "config" / "universal_trainer.txt"
    shutil.copy2(cheats, dst)
    print(f"cheats -> {dst}")


def uninstall(game):
    game = Path(game)
    manifest = game / "BepInEx" / ".trainer_installed"
    if not manifest.exists():
        sys.exit("nothing to uninstall (no install manifest)")
    for rel in manifest.read_text().splitlines():
        p = game / rel
        if p.exists():
            p.unlink()
    shutil.rmtree(game / "BepInEx", ignore_errors=True)
    print("uninstalled: game folder is back to vanilla")


if __name__ == "__main__":
    cmd, game = sys.argv[1], sys.argv[2]
    {"install": lambda: install(game, sys.argv[3] if len(sys.argv) > 3 else None),
     "uninstall": lambda: uninstall(game),
     "cheats": lambda: set_cheats(game, sys.argv[3])}[cmd]()
