"""Find installed games and fingerprint one: engine, executables, anti-cheat, save folders.

  python -m trainerkit.gamescan --list            installed games (Steam, Epic, GOG)
  python -m trainerkit.gamescan "<name or folder>" fingerprint as JSON

The engine decides the route: Unity Mono gets the reflection plugin, everything else the memory route.
Anti-cheat files make the game off limits.
"""
import json
import re
import struct
import sys
import winreg
from pathlib import Path

ENGINES = [
    # key, label, test(folder) -> evidence or None
    ("unity-mono", "Unity (Mono)", lambda d: _glob(d, "*_Data/Managed/Assembly-CSharp.dll")),
    ("unity-il2cpp", "Unity (IL2CPP)", lambda d: _glob(d, "GameAssembly.dll") and _glob(d, "*_Data/il2cpp_data")),
    ("unreal", "Unreal Engine", lambda d: _glob(d, "*/Binaries/Win64/*.exe") or _glob(d, "*/Content/Paks/*.pak")),
    ("gamemaker", "GameMaker", lambda d: _glob(d, "data.win")),
    ("godot", "Godot", lambda d: _glob(d, "*.pck")),
    ("xna-fna", "XNA/FNA/MonoGame (.NET)", lambda d: _glob(d, "FNA.dll") or _glob(d, "MonoGame.Framework.dll")
     or _glob(d, "Microsoft.Xna.Framework*.dll") or _glob(d, "*/FNA.dll")),
    ("renpy", "Ren'Py", lambda d: _glob(d, "renpy")),
    ("rpgmaker", "RPG Maker MV/MZ", lambda d: _glob(d, "www/js/rpg_core.js") or _glob(d, "js/rmmz_core.js")),
    ("redengine", "CD Projekt REDengine", lambda d: _glob(d, "bin/x64*/witcher3.exe") or _glob(d, "engine/config/base")),
    ("source", "Source", lambda d: _glob(d, "*/gameinfo.txt")),
]
ANTI_CHEAT = [
    ("EasyAntiCheat", ["EasyAntiCheat", "EasyAntiCheat_EOS*", "start_protected_game.exe"]),
    ("BattlEye", ["BattlEye", "BEService*"]),
    ("Vanguard", ["vgk.sys", "vgc.exe"]),
    ("nProtect GameGuard", ["GameGuard", "GameMon*.des"]),
    ("XIGNCODE", ["XIGNCODE*", "x3.xem"]),
    ("Denuvo Anti-Cheat", ["denuvo-anti-cheat*", "anti_cheat*"]),
    ("EQU8", ["equ8*"]),
]
SKIP_EXE = re.compile(r"(?i)crash|unins|setup|redist|vcredist|dxsetup|launcherhelper|report|helper|update|"
                      r"install|ue4prereq|dotnet|cefprocess|steamerrorreporter")


def _glob(d, pattern):
    try:
        hit = next(Path(d).glob(pattern), None)
    except OSError:
        return None
    return str(hit.relative_to(d)) if hit else None


# ------------------------------------------------------------------ library
def _reg(root, path, name):
    try:
        with winreg.OpenKey(root, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def steam_games():
    steam = _reg(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath")
    if not steam:
        return []
    vdf = Path(steam) / "steamapps" / "libraryfolders.vdf"
    libs = {Path(steam)}
    if vdf.exists():
        libs |= {Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace"))}
    out = []
    for lib in libs:
        for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
            t = acf.read_text(encoding="utf-8", errors="replace")
            name = re.search(r'"name"\s+"([^"]+)"', t)
            inst = re.search(r'"installdir"\s+"([^"]+)"', t)
            appid = re.search(r'"appid"\s+"(\d+)"', t)
            if name and inst:
                path = lib / "steamapps" / "common" / inst.group(1)
                if path.is_dir():
                    out.append({"store": "steam", "appid": appid.group(1) if appid else None,
                                "name": name.group(1), "path": str(path)})
    return out


def epic_games():
    out = []
    for item in Path(r"C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests").glob("*.item"):
        try:
            d = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if Path(d.get("InstallLocation", "")).is_dir():
            out.append({"store": "epic", "appid": d.get("AppName"), "name": d.get("DisplayName"), "path": d["InstallLocation"]})
    return out


def gog_games():
    out = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GOG.com\Games")
    except OSError:
        return out
    i = 0
    while True:
        try:
            gid = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1
        path = _reg(root, gid, "path")
        name = _reg(root, gid, "gameName")
        if path and name and Path(path).is_dir():
            out.append({"store": "gog", "appid": gid, "name": name, "path": path})
    return out


def library():
    seen, out = set(), []
    for g in steam_games() + epic_games() + gog_games():
        if g["path"].lower() not in seen and not re.search(r"(?i)redistributable|steamworks|proton|runtime", g["name"]):
            seen.add(g["path"].lower())
            out.append(g)
    return sorted(out, key=lambda g: g["name"].lower())


# ------------------------------------------------------------------ fingerprint
def arch(exe):
    try:
        with open(exe, "rb") as f:
            head = f.read(4096)
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        machine = struct.unpack_from("<H", head, pe + 4)[0]
        return {0x8664: "x64", 0x14C: "x86", 0xAA64: "arm64"}.get(machine, "?")
    except (OSError, struct.error):
        return "?"


def executables(folder):
    d = Path(folder)
    exes = list(d.glob("*.exe")) + list(d.glob("*/Binaries/Win64/*.exe")) + list(d.glob("bin/*/*.exe")) + \
        list(d.glob("bin/*.exe")) + list(d.glob("x64*/*.exe"))
    return {e.name.lower(): {"arch": arch(e)} for e in exes if not SKIP_EXE.search(e.name)}


def unity_info(folder):
    """Company/product from <Game>_Data/app.info, version from globalgamemanagers."""
    data = next(Path(folder).glob("*_Data"), None)
    info = {}
    if not data:
        return info
    try:
        company, product = (data / "app.info").read_text(encoding="utf-8", errors="replace").splitlines()[:2]
        info.update(company=company.strip(), product=product.strip())
    except (OSError, ValueError):
        pass
    for f in ("globalgamemanagers", "data.unity3d", "mainData"):
        try:
            head = (data / f).read_bytes()[:8192]
        except OSError:
            continue
        m = re.search(rb"(20\d\d\.\d+\.\d+[abfp]\d+|[56]\.\d+\.\d+[abfp]\d+)", head)
        if m:
            info["version"] = m.group(1).decode()
            break
    info["data_dir"] = data.name.lower()
    return info


def anti_cheat(folder):
    found = []
    for name, patterns in ANTI_CHEAT:
        if any(_glob(folder, p) or _glob(folder, "*/" + p) for p in patterns):
            found.append(name)
    return found


def unity_saves(info):
    if info.get("company") and info.get("product"):
        p = Path.home() / "AppData" / "LocalLow" / info["company"] / info["product"]
        if p.is_dir():
            return [str(p)]
    return []


def scan(target):
    """Fingerprint a game by library name or folder (or exe) path."""
    lib = library()
    p = Path(target)
    if p.suffix.lower() == ".exe":
        p = p.parent
    entry = None
    if p.is_dir():
        entry = next((g for g in lib if str(p).lower().startswith(g["path"].lower())), None)
        folder = p
    else:
        exact = [g for g in lib if g["name"].lower() == target.lower()]
        loose = [g for g in lib if target.lower() in g["name"].lower()]
        entry = (exact or loose or [None])[0]
        if not entry:
            raise FileNotFoundError(f"game not found: {target}")
        folder = Path(entry["path"])
    engine = {"key": "native", "label": "Unknown native engine", "evidence": []}
    for key, label, test in ENGINES:
        ev = test(folder)
        if ev:
            engine = {"key": key, "label": label, "evidence": [ev]}
            break
    if engine["key"].startswith("unity"):
        engine.update(unity_info(folder))
    saves = unity_saves(engine) if engine["key"].startswith("unity") else []
    return {"name": (entry or {}).get("name") or engine.get("product") or folder.name,
            "store": (entry or {}).get("store"), "appid": (entry or {}).get("appid"),
            "path": str(folder), "engine": engine, "anti_cheat": anti_cheat(folder),
            "saves": saves, "executables": executables(folder)}


if __name__ == "__main__":
    if sys.argv[1:] == ["--list"]:
        print(json.dumps(library(), indent=1, ensure_ascii=False))
    else:
        print(json.dumps(scan(sys.argv[1]), indent=1, ensure_ascii=False))
