"""Make sure the local model server is up before an autonomous run (no human to click LM Studio).

- VS Code / Electron terminals export ELECTRON_RUN_AS_NODE=1; LM Studio is an Electron app and then starts
  as bare node and exits, so `lms server start` times out. The variable is removed for child processes.
- The model shares the GPU with the game (and whatever else runs, e.g. ComfyUI): the GPU offload ratio is
  chosen from the free VRAM, leaving room for the game. A MoE model with 3B active parameters runs fine
  mostly from system RAM.
"""
import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

LMS = shutil.which("lms") or str(Path.home() / ".lmstudio" / "bin" / "lms.exe")
URL = os.environ.get("LMSTUDIO_URL", "http://localhost:1234/v1")
PREFERRED = [("qwen/qwen3.6-35b-a3b", 21.0), ("qwen/qwen3.5-9b", 6.6), ("google/gemma-4-12b-qat", 7.2)]
GAME_RESERVE_GB = float(os.environ.get("TRAINER_GAME_VRAM_GB", "3"))


def env():
    e = dict(os.environ)
    e.pop("ELECTRON_RUN_AS_NODE", None)
    return e


def loaded():
    try:
        with urllib.request.urlopen(f"{URL}/models", timeout=5) as r:
            ids = [m["id"] for m in json.loads(r.read())["data"]]
    except Exception:
        return None
    try:
        out = subprocess.run([LMS, "ps"], capture_output=True, text=True, env=env(), timeout=60).stdout
        return [m for m in ids if m in out]
    except Exception:
        return ids


def free_vram_gb():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20).stdout
        return float(out.split()[0]) / 1024
    except Exception:
        return 0.0


def ensure(log=print):
    """Server running and a vision model loaded. Returns the model id or None."""
    have = loaded()
    if have is None:
        log("starting LM Studio server")
        subprocess.run([LMS, "server", "start"], capture_output=True, text=True, env=env(), timeout=300)
        have = loaded()
        if have is None:
            log("LM Studio server did not start")
            return None
    pick = next((m for m, _ in PREFERRED if m in have), None)
    if pick:
        return pick
    avail = subprocess.run([LMS, "ls"], capture_output=True, text=True, env=env(), timeout=60).stdout
    for model, size in PREFERRED:
        if model.split("/")[-1] not in avail:
            continue
        spare = max(0.0, free_vram_gb() - GAME_RESERVE_GB)
        ratio = max(0.0, min(1.0, spare / size))
        gpu = "off" if ratio < 0.05 else f"{ratio:.2f}"
        log(f"loading {model} (GPU offload {gpu}, {spare:.1f} GB VRAM spare after reserving {GAME_RESERVE_GB} GB for the game)")
        r = subprocess.run([LMS, "load", model, "--gpu", gpu, "-c", "32768", "--ttl", "3600", "-y"],
                           capture_output=True, text=True, env=env(), timeout=900)
        if r.returncode == 0:
            return model
        log(f"load failed: {(r.stderr or r.stdout)[-300:]}")
    return None
