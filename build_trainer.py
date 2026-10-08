"""
Build a standalone trainer .exe from a trainer.json.

  python build_trainer.py games/<game>/trainer.json            # -> games/<game>/dist/<Title>.exe
  python build_trainer.py games/<game>/trainer.json --run-py   # run without building (for testing)
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KIT = ROOT / "trainerkit"

TEMPLATE = '''import json, sys
sys.path.insert(0, r"{kit}")
from runtime import run
TRAINER = json.loads({blob!r})
if __name__ == "__main__":
    run(TRAINER)
'''


def validate(t):
    from trainerkit.runtime import VALUE_TYPES, Hotkey
    for k in ("title", "process", "cheats"):
        if not t.get(k):
            sys.exit(f"trainer.json: '{k}' is required")
    seen = set()
    for c in t["cheats"]:
        if c["type"] not in ("freeze", "set", "add", "patch"):
            sys.exit(f"{c['name']}: unknown type {c['type']}")
        Hotkey(c["hotkey"])  # raises on an unknown key
        if c["hotkey"].upper() in seen:
            sys.exit(f"{c['name']}: hotkey {c['hotkey']} used twice")
        seen.add(c["hotkey"].upper())
        if c["type"] != "patch" and c.get("value_type") not in VALUE_TYPES:
            sys.exit(f"{c['name']}: value_type must be one of {list(VALUE_TYPES)}")
        if c["type"] == "patch" and not c.get("bytes"):
            sys.exit(f"{c['name']}: patch needs 'bytes'")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trainer_json")
    ap.add_argument("--run-py", action="store_true", help="run the trainer from source instead of building")
    a = ap.parse_args()
    src = Path(a.trainer_json).resolve()
    t = json.loads(src.read_text(encoding="utf-8"))
    sys.path.insert(0, str(ROOT))
    validate(t)

    if a.run_py:
        from trainerkit.runtime import run
        run(t)
        return

    safe = re.sub(r"[^\w\-+ ]", "", t["title"]).strip().replace(" ", "_") or "Trainer"
    out = src.parent / "dist"
    with tempfile.TemporaryDirectory() as tmp:
        entry = Path(tmp) / f"{safe}.py"
        entry.write_text(TEMPLATE.format(kit=str(KIT), blob=json.dumps(t)), encoding="utf-8")
        cmd = [sys.executable, "-m", "PyInstaller", "--onefile", "--console", "--noconfirm",
               "--name", safe, "--distpath", str(out), "--workpath", str(Path(tmp) / "build"),
               "--specpath", tmp, "--paths", str(KIT), "--hidden-import", "runtime", str(entry)]
        if t.get("uac_admin"):
            cmd.insert(-1, "--uac-admin")
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            print(r.stdout[-3000:], r.stderr[-3000:])
            sys.exit("PyInstaller failed")
    shutil.copy(src, out / "trainer.json")
    print(f"built {out / (safe + '.exe')}")


if __name__ == "__main__":
    main()
