"""Local web UI for the trainer agent:  python app/server.py  ->  http://localhost:8765"""
import json
import os
import subprocess
import sys
import threading
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import agent as agent_mod  # noqa: E402
import tools  # noqa: E402

PORT = int(os.environ.get("TRAINER_PORT", "8765"))
STATE = {"agent": agent_mod.Agent(), "busy": False, "queue": [], "worker": None}
LOCK = threading.Lock()
MAX_PENDING_MESSAGES = 32
MAX_MESSAGE_LENGTH = 4000


def _drain_chat_queue():
    while True:
        with LOCK:
            if not STATE["queue"]:
                STATE["busy"] = False
                return
            item = STATE["queue"].pop(0)
            current_agent = STATE["agent"]
            clear_interrupt = getattr(current_agent, "clear_interrupt", None)
            if clear_interrupt:
                clear_interrupt()
        try:
            current_agent.send(item["text"])
        except Exception as e:
            current_agent.messages.append(
                {"role": "assistant", "content": f"⚠ Нет связи с LM Studio или ошибка модели: {e}"})
            if tools.S.game:
                tools.save_agent_checkpoint(item["text"], "server_error", "agent.send", str(e), "error")


def queue_chat(text, interrupt=False):
    if not isinstance(text, str) or not text.strip():
        return {"error": "сообщение пустое"}, 400
    text = text.strip()
    if len(text) > MAX_MESSAGE_LENGTH:
        return {"error": f"сообщение длиннее {MAX_MESSAGE_LENGTH} символов"}, 413
    with LOCK:
        if len(STATE["queue"]) >= MAX_PENDING_MESSAGES:
            return {"error": "очередь заполнена; дождитесь обработки сообщений"}, 429
        item = {"text": text, "interrupt": bool(interrupt and STATE["busy"]), "hint_saved": False}
        if tools.S.game and tools.should_remember_user_hint(text):
            saved = tools.save_lesson(text, "user_hint")
            item["hint_saved"] = bool(saved.get("saved"))
        if item["interrupt"]:
            STATE["queue"].insert(0, item)
            request_interrupt = getattr(STATE["agent"], "request_interrupt", None)
            if request_interrupt:
                request_interrupt()
        else:
            STATE["queue"].append(item)
        position = len(STATE["queue"]) + (1 if STATE["busy"] else 0)
        if not STATE["busy"]:
            STATE["busy"] = True
            worker = threading.Thread(target=_drain_chat_queue, daemon=True)
            STATE["worker"] = worker
            worker.start()
        return {"ok": True, "queued": True, "position": position,
            "interrupt_requested": item["interrupt"], "hint_saved": item["hint_saved"]}, 202


def snapshot():
    S = tools.S
    with LOCK:
        current_agent = STATE["agent"]
        busy = STATE["busy"]
        pending = list(STATE["queue"])
        model = current_agent.model
        interrupt_requested = getattr(current_agent, "interrupt_requested", lambda: False)()
        messages = list(current_agent.messages[1:])
    chat = []
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], str):
            chat.append({"role": "user", "text": m["content"].split("\n", 1)[-1]})
        elif m["role"] == "assistant" and m.get("content"):
            chat.append({"role": "assistant", "text": m["content"]})
        elif m["role"] == "tool":
            chat.append({"role": "tool"})
    ev = iter(S.events)
    for c in chat:
        if c["role"] == "tool":
            c.update(next(ev, {}))
    chat.extend({"role": "user", "text": item["text"], "queued": True,
                 "interrupt": item["interrupt"], "hint_saved": item["hint_saved"]} for item in pending)
    g = S.game
    return {
        "busy": busy, "queued": len(pending), "interrupt_requested": interrupt_requested,
        "model": model, "chat": chat,
        "game": {"name": g["name"], "engine": (g.get("engine") or {}).get("label"), "process": S.process,
                 "blocked": S.blocked, "anti_cheat": g.get("anti_cheat") or [],
                 "path": g.get("path")} if g else None,
        "cheats": (S.trainer or {}).get("cheats", []),
        "dist": str(S.trainer_path().parent / "dist") if S.game else None,
    }


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            data = (HERE / "static" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/api/state":
            self._json(snapshot())
        elif self.path.startswith("/api/games"):
            self._json(tools.list_games())
        elif self.path == "/api/models":
            self._json({"models": agent_mod.models(), "current": STATE["agent"].model})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        b = self._body()
        if self.path == "/api/chat":
            result, status = queue_chat(b.get("text"), interrupt=b.get("interrupt", False))
            self._json(result, status)
        elif self.path == "/api/select":
            res = tools.call("select_game", {"name": b.get("name", ""), "path": b.get("path", "")})
            self._json(res)
        elif self.path == "/api/model":
            STATE["agent"].model = b["model"]
            self._json({"model": b["model"]})
        elif self.path == "/api/reset":
            with LOCK:
                if STATE["busy"]:
                    return self._json({"error": "дождитесь обработки очереди"}, 409)
                STATE["agent"] = agent_mod.Agent(STATE["agent"].model)
                tools.S.events.clear()
            self._json({"ok": True})
        elif self.path == "/api/test":
            self._json(tools.call("test_cheat", {"name": b["name"]}))
        elif self.path == "/api/remove":
            self._json(tools.call("remove_cheat", {"name": b["name"]}))
        elif self.path == "/api/build":
            self._json(tools.call("build_trainer", {}))
        elif self.path == "/api/open":
            p = Path(b["path"])
            if p.exists():
                subprocess.Popen(["explorer", str(p)])
            self._json({"ok": p.exists()})
        else:
            self._json({"error": "not found"}, 404)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    url = f"http://localhost:{PORT}"
    print(f"Cheats For Everything: {url}  (LM Studio: {agent_mod.LMSTUDIO}, model {STATE['agent'].model})")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
