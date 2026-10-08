"""Agent loop against a local OpenAI-compatible server (LM Studio)."""
import json
import os
import re
import threading
import urllib.request
from pathlib import Path

from tools import S, schemas, call, learning_for_game, save_agent_checkpoint, should_remember_user_hint, save_lesson

LMSTUDIO = os.environ.get("LMSTUDIO_URL", "http://localhost:1234/v1")
# best first: Qwen3.6-35B-A3B (MoE, 3B active: smart and still fast with part of it in RAM),
# then the light 9B, then Gemma 4. All have vision + tool calling.
PREFERRED = ["qwen/qwen3.6-35b-a3b", "qwen/qwen3.5-9b", "google/gemma-4-12b-qat"]
DEFAULT_MODEL = os.environ.get("TRAINER_MODEL", "")
MAX_STEPS = int(os.environ.get("TRAINER_MAX_STEPS", "60"))
HISTORY_TURNS = 5
# Qwen3.x "thinks" ~1k tokens per step (≈50-75 s when the model shares the GPU with the game) unless
# reasoning_effort is "none"; low/medium are not distinguished by LM Studio. Set TRAINER_REASONING=on to think.
REASONING = os.environ.get("TRAINER_REASONING", "none")
EXE_PATH = re.compile(r"(?i)([a-z]:\\[^\"\r\n]*?\.exe)")

SYSTEM = """Ты — автономный агент, который делает трейнеры (читы) для ОДИНОЧНЫХ компьютерных игр и
собирает готовый релиз. Отвечай по-русски, коротко. Работай сам, инструментами; пользователя
спрашивай только если без него никак (например, какие читы он хочет — если он не сказал, выбери сам:
бесконечное здоровье, деньги, опыт, спецшкала, неуязвимость, урон).

ОБЩЕЕ
- Сначала select_game (можно путь к exe). Если blocked (античит/онлайн) — откажись и объясни.
- Сразу после select_game вызови backup_saves, если известна папка сохранений; до любых чтений/изменений игры.
    Если в текущем сообщении есть путь к exe, runtime мог уже выбрать игру и создать backup до первого ответа;
    проверь preflight-отчёт и не дублируй успешный backup_saves.
- Ничего не выдумывай: адреса, классы и результаты бери только из инструментов.
- Используй lessons/recent_experience только выбранной игры. user_hint считай подсказкой игрока,
    observed_failure — причиной не повторять те же аргументы, verified — прежним подтверждённым фактом,
    который всё равно нужно перепроверить в текущей сессии.
- Если в памяти есть unfinished_task, сверяй его с текущим запросом. Продолжай только ту же задачу;
    сначала перечитай состояние игры и проверь последний результат. Не повторяй side effect/tool вслепую.
- Явные управляющие подсказки пользователя автоматически сохраняются как user_hint после выбора игры.
    Не дублируй такую подсказку отдельным save_lesson. Новые проверенные факты сохраняй с evidence=verified;
    непроверенные гипотезы не сохраняй как проверенные.
- Если пользователь просит «всё», не показывай меню вариантов и не останавливайся на первых удачных читах.
- Не повторяй неудачный вызов без новой гипотезы или изменённых аргументов.

ЕСЛИ route = "unity" (Unity Mono):
1. Сначала вызови autopilot — он сам установит плагин, дойдёт до геймплея (по сохранённому маршруту или
   исследуя меню), найдёт значения (снимки памяти + эксперименты с проверкой по экрану), подберёт хуки
   god mode, необычные читы, проверит всё и запишет только проверенное. Это долго (минуты) — это нормально.
2. Прочитай отчёт: cheats (проверенные), skipped (почему отброшены), error. Если error про меню —
   посмотри screenshot, сделай одно осмысленное нажатие press_keys и вызови autopilot снова (маршрут
   дописывается). Если чего-то не хватает (например, денег) — ищи вручную: snapshot_diff/find_value
   (как сканер памяти, но с именами полей), inspect_class, get_value/set_value; добавь через add_unity_cheat
   и проверь verify_cheats. Игровые подсказки бери из памяти этой игры (lessons/playbook), не выдумывай.
3. package_release, затем save_lesson с проверенными фактами.

ЕСЛИ route = "memory" (любой другой движок): вызови memory_autopilot — он дойдёт до геймплея, прочитает
числа с HUD (или из меню), найдёт их в памяти, проверит цепочки указателей перезапуском игры и соберёт .exe.
Если чего-то не хватает — first_scan/next_scan по значению на экране, list_candidates, write_value на пробу,
add_cheat, test_cheat, build_trainer. Адрес без цепочки указателей живёт только до перезапуска — скажи честно.
"""


def pick_model():
    have = models()
    if DEFAULT_MODEL:
        return DEFAULT_MODEL
    return next((m for m in PREFERRED if m in have), have[0] if have else PREFERRED[1])


class Agent:
    def __init__(self, model=None):
        self.model = model or pick_model()
        self.messages = [{"role": "system", "content": SYSTEM}]
        self._interrupt_event = threading.Event()
        self._active_task = ""
        self._last_tool = ""
        self._backup_done = False
        self._denied_tools = set()

    def request_interrupt(self):
        self._interrupt_event.set()

    def clear_interrupt(self):
        self._interrupt_event.clear()

    def interrupt_requested(self):
        return self._interrupt_event.is_set()

    def _acknowledge_interrupt(self):
        if not self._interrupt_event.is_set():
            return False
        self.messages.append({"role": "assistant", "content":
                              "Остановил текущий проход по вашему сообщению. Продолжаю с вашей коррекцией."})
        self._save_checkpoint("interrupted", "Остановка по коррекции пользователя")
        return True

    def _save_checkpoint(self, status, result=""):
        try:
            save_agent_checkpoint(self._active_task, self._last_tool or "starting",
                                  self._last_tool, result, status)
        except Exception:
            pass

    def _prepare_explicit_game(self, user_text):
        match = EXE_PATH.search(user_text)
        if not match:
            return ""
        path = match.group(1).strip()
        requested_dir = os.path.normcase(os.path.abspath(str(Path(path).parent)))
        current_dir = (S.game or {}).get("dir") or (S.game or {}).get("path") or ""
        if current_dir and os.path.normcase(os.path.abspath(current_dir)) == requested_dir:
            return ""

        selected = call("select_game", {"path": path})
        if "error" in selected:
            return f"Game preflight failed: {selected['error']}. Do not call game tools until selection succeeds."
        if selected.get("blocked"):
            return f"Game is blocked: {selected['blocked']}. Do not use game tools."
        backup = call("backup_saves", {})
        self._backup_done = bool(backup.get("backups"))
        self._last_tool = "backup_saves"
        self._save_checkpoint("active", "Selected exact executable and backed up saves")
        return "Deterministic preflight selected the exact executable path and requested a save backup. " \
            f"Selection: {json.dumps(selected, ensure_ascii=False)}. Backup: {json.dumps(backup, ensure_ascii=False)}."

    @staticmethod
    def _explicitly_denied_tools(user_text):
        known = {"install_trainer", "launch_game", "kill_game", "add_unity_cheat",
                 "verify_cheats", "package_release", "build_trainer"}
        denied = set()
        for match in re.finditer(
                r"(?:не\s+(?:вызывай|используй|запускай|делай)|do not\s+(?:call|use|run)|don't\s+(?:call|use|run))\s+([^.!?\n]+)",
                str(user_text), re.I):
            clause = match.group(1).lower()
            denied.update(name for name in known if name in clause)
        return denied

    @staticmethod
    def _selected_game_matches(args):
        if not S.game:
            return False
        requested = args.get("path") or args.get("name", "")
        current = S.game.get("dir") or S.game.get("path", "")
        if requested.lower().endswith(".exe"):
            requested = str(Path(requested).parent)
        if requested and current and (os.path.normcase(os.path.abspath(requested)) ==
                                      os.path.normcase(os.path.abspath(current))):
            return True
        return bool(args.get("name") and args["name"].casefold() == S.game.get("name", "").casefold())

    @staticmethod
    def _process_is_running():
        if not S.process:
            return False
        try:
            import psutil
            return any((process.info["name"] or "").lower() == S.process.lower()
                       for process in psutil.process_iter(["name"]))
        except Exception:
            return False

    def _complete(self):
        body = json.dumps({"model": self.model, "messages": self.messages, "tools": schemas(),
                           "temperature": 0.3, "max_tokens": 2048,
                           **({"reasoning_effort": "none"} if REASONING == "none" else {})}).encode()
        req = urllib.request.Request(f"{LMSTUDIO}/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read())["choices"][0]["message"]

    def context(self):
        g = S.game["name"] if S.game else "не выбрана"
        state = f"[Состояние: игра={g}, процесс={S.process or '?'}, читов={len(S.trainer['cheats']) if S.trainer else 0}]"
        memory = learning_for_game()
        if memory:
            state += "\n[Накопленная память только этой игры: " + json.dumps(memory, ensure_ascii=False) + "]"
        return state

    def _compact_history(self):
        turns, current = [], []
        for message in self.messages[1:]:
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                if current:
                    turns.append(current)
                current = [message]
            elif message.get("role") == "assistant" and not message.get("tool_calls") and current:
                current.append(message)
        if current:
            turns.append(current)
        self.messages = [self.messages[0]] + [message for turn in turns[-HISTORY_TURNS:] for message in turn]

    def send(self, user_text, complete=None):
        """One user turn. Returns the assistant's final text. `complete` lets tests inject a fake model."""
        complete = complete or self._complete
        pending_hint = str(user_text).strip() if not S.game and should_remember_user_hint(user_text) else ""
        self._active_task = str(user_text)[:500]
        self._last_tool = ""
        self._backup_done = False
        self._denied_tools = self._explicitly_denied_tools(user_text)
        self._compact_history()
        preflight = self._prepare_explicit_game(user_text)
        if pending_hint and S.game and not S.blocked:
            save_lesson(pending_hint, "user_hint")
            pending_hint = ""
        prefix = self.context()
        if preflight:
            prefix += "\n[Preflight: " + preflight + "]"
        self.messages.append({"role": "user", "content": f"{prefix}\n{user_text}"})
        self._save_checkpoint("active", "Запрос принят")
        failed_calls = set()
        for _ in range(MAX_STEPS):
            if self._acknowledge_interrupt():
                return "Остановил текущий проход; обрабатываю ваше сообщение."
            msg = complete()
            if self._acknowledge_interrupt():
                return "Остановил текущий проход; обрабатываю ваше сообщение."
            calls = msg.get("tool_calls") or []
            self.messages.append({"role": "assistant", "content": msg.get("content") or "",
                                  **({"tool_calls": calls} if calls else {})})
            if not calls:
                result = msg.get("content") or "Задача завершена без текстового отчёта."
                self._save_checkpoint("completed", result)
                return result
            images = []
            for tc in calls:
                fn = tc["function"]
                tool_name = fn["name"]
                if self._interrupt_event.is_set():
                    res = {"error": "tool skipped: interrupted by user"}
                elif tool_name in self._denied_tools:
                    res = {"error": f"Blocked by the user's explicit instruction: {tool_name} is not allowed in this run."}
                elif not S.game and tool_name not in {"select_game", "list_games", "running_processes"}:
                    res = {"error": "No game selected. Call select_game with the user's exact executable path before any game tool.",
                           "input_sent": False}
                else:
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    fingerprint = (tool_name, json.dumps(args, sort_keys=True, ensure_ascii=False))
                    if tool_name == "select_game" and self._selected_game_matches(args):
                        res = {"name": S.game.get("name"), "process": S.process,
                               "already_selected": True, "note": "game already selected; skipping duplicate scan"}
                    elif tool_name == "backup_saves" and self._backup_done:
                        res = {"already_backed_up": True, "note": "save backup already created in preflight"}
                    elif fingerprint in failed_calls:
                        res = {"error": f"Repeated failed call blocked: {tool_name}. Change the hypothesis or stop."}
                    elif tool_name in {"install_trainer", "launch_game"} and self._process_is_running():
                        res = {"error": f"{tool_name} blocked: the game is already running. Do not kill/relaunch it to recover a plugin timeout."}
                    else:
                        res = call(tool_name, args)
                        if res.get("error"):
                            failed_calls.add(fingerprint)
                    if tool_name == "backup_saves" and res.get("backups"):
                        self._backup_done = True
                    if tool_name == "select_game" and pending_hint and "error" not in res \
                            and S.game and not S.blocked:
                        save_lesson(pending_hint, "user_hint")
                        pending_hint = ""
                    if "image_b64" in res:
                        images.append(res.pop("image_b64"))
                limit = 12000 if tool_name in {"discover_features", "suggest_fun_cheats",
                                                  "inspect_class", "find_classes"} else 4000
                tool_content = json.dumps(res, ensure_ascii=False)[:limit]
                self.messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                      "content": tool_content})
                self._last_tool = tool_name
                self._save_checkpoint("error" if "error" in res else "active",
                                      tool_content[:300])
            if self._acknowledge_interrupt():
                return "Остановил текущий проход; обрабатываю ваше сообщение."
            for img in images:  # vision models get the screenshot as a user image
                self.messages.append({"role": "user", "content": [
                    {"type": "text", "text": "Скриншот игры:"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}}]})
        result = f"Не завершил задачу за {MAX_STEPS} шагов. Последний инструмент: {self._last_tool or 'нет'}. " \
            "Остановился, чтобы не зациклиться; задача сохранена для продолжения."
        self.messages.append({"role": "assistant", "content": result})
        self._save_checkpoint("incomplete", result)
        return result


def models():
    try:
        with urllib.request.urlopen(f"{LMSTUDIO}/models", timeout=5) as r:
            return [m["id"] for m in json.loads(r.read())["data"] if "embed" not in m["id"]]
    except Exception:
        return []
