"""Compile UniversalTrainer.cs against a Unity (Mono) game with the C# compiler that ships with
Windows (.NET Framework 4.x csc, C# 5) - no .NET SDK needed.
  python unity/build_plugin.py "<game folder>" [out.dll]  ->  unity/UniversalTrainer.dll"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORE = HERE / "vendor" / "bepinex" / "BepInEx" / "core"
CSC = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")


def build(game_dir, out=None):
    managed = next(Path(game_dir).glob("*_Data/Managed"))
    # newer Unity (2021+) builds game code against netstandard 2.1: its facade must be referenced too
    refs = ["mscorlib.dll", "System.dll", "System.Core.dll", "netstandard.dll"] + \
           [p.name for p in managed.glob("UnityEngine*.dll")]
    out = Path(out) if out else HERE / "UniversalTrainer.dll"
    cmd = [str(CSC), "/noconfig", "/nologo", "/target:library", "/optimize+", f"/out:{out}", "/nostdlib",
           f"/r:{CORE / 'BepInEx.dll'}", f"/r:{CORE / '0Harmony.dll'}"] + \
          [f"/r:{managed / r}" for r in refs if (managed / r).exists()] + [str(HERE / "UniversalTrainer.cs")]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="cp866", errors="replace")
    errors = [l for l in r.stdout.splitlines() if " error " in l]
    if r.returncode or errors:
        print("\n".join(errors or r.stdout.splitlines()[-20:]))
        sys.exit("build failed")
    print(f"built {out}")
    return out


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
