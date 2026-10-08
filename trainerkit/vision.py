"""Small, tool-free questions to the local vision model (LM Studio, OpenAI-compatible API).

The agent loop sends ~30 tool schemas and the whole history with every request; on a machine where the
model shares the GPU with the game that costs minutes per step. Navigation and HUD reading only need
"look at this frame, pick one of these options", so they go through here: one downscaled image, a short
prompt, a JSON answer.
"""
import base64
import io
import json
import os
import re
import urllib.request

from PIL import Image

LMSTUDIO = os.environ.get("LMSTUDIO_URL", "http://localhost:1234/v1")
PREFERRED = ["qwen/qwen3.6-35b-a3b", "qwen/qwen3.5-9b", "google/gemma-4-12b-qat"]


def loaded_models():
    try:
        with urllib.request.urlopen(f"{LMSTUDIO}/models", timeout=5) as r:
            return [m["id"] for m in json.loads(r.read())["data"] if "embed" not in m["id"]]
    except Exception:
        return []


def pick_model():
    have = loaded_models()
    if os.environ.get("TRAINER_MODEL"):
        return os.environ["TRAINER_MODEL"]
    return next((m for m in PREFERRED if m in have), have[0] if have else None)


def image_url(path, width=640):
    im = Image.open(path).convert("RGB")
    if im.width > width:
        im = im.resize((width, int(im.height * width / im.width)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def parse_json(text):
    """Last {...} object in the reply (models may think out loud before it)."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    # outermost balanced {...} blocks, last one first (nested objects/lists stay intact)
    blocks, depth, start = [], 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                blocks.append(text[start:i + 1])
    for b in reversed(blocks):
        try:
            return json.loads(b)
        except ValueError:
            continue
    return None


# Qwen3.x thinks for 1-2k tokens before answering unless told not to: 75 s vs 2 s per question on a
# GPU shared with the game. LM Studio honours the OpenAI-style "reasoning_effort" field (the
# chat_template_kwargs / "reasoning" variants are ignored).
REASONING = os.environ.get("TRAINER_VISION_REASONING", "none")


def ask(prompt, image=None, model=None, max_tokens=400, timeout=300, width=640):
    """Returns (parsed_json_or_None, raw_text). Never raises on model errors: returns (None, error)."""
    model = model or pick_model()
    if not model:
        return None, "no model loaded in LM Studio"
    content = [{"type": "text", "text": prompt}]
    if image:
        content.append({"type": "image_url", "image_url": {"url": image_url(image, width)}})
    body = {"model": model, "temperature": 0, "max_tokens": max_tokens, "reasoning_effort": REASONING,
            "messages": [{"role": "system", "content": "You are a precise game-screen reader. Answer with one JSON object only."},
                         {"role": "user", "content": content}]}
    req = urllib.request.Request(f"{LMSTUDIO}/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            msg = json.loads(r.read())["choices"][0]["message"]
    except Exception as e:
        return None, f"model error: {e}"
    text = msg.get("content") or ""
    return parse_json(text) or parse_json(msg.get("reasoning_content") or ""), text
