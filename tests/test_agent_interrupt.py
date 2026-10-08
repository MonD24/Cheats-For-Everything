import json
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import agent  # noqa: E402
import tools  # noqa: E402
import tools  # noqa: E402


class AgentInterruptTests(unittest.TestCase):
    @staticmethod
    def tool_call(name, call_id):
        return {"id": call_id, "function": {"name": name, "arguments": "{}"}}

    def test_interrupt_discards_model_response_before_tools_run(self):
        current = agent.Agent("fake")
        completion = lambda: (current.request_interrupt() or {
            "tool_calls": [self.tool_call("stale_action", "one")]})

        with patch.object(agent, "call") as call_tool:
            result = current.send("start", complete=completion)

        call_tool.assert_not_called()
        self.assertIn("Остановил", result)
        self.assertIn("Остановил", current.messages[-1]["content"])

    def test_interrupt_skips_remaining_tools_and_keeps_all_tool_replies(self):
        current = agent.Agent("fake")
        ran = []
        original_game = tools.S.game
        tools.S.game = {"name": "Fake Game"}

        def run_tool(name, _args):
            ran.append(name)
            if name == "first":
                current.request_interrupt()
            return {"ok": True}

        response = {"tool_calls": [self.tool_call("first", "one"), self.tool_call("stale", "two")]}
        try:
            with patch.object(agent, "call", side_effect=run_tool):
                result = current.send("start", complete=lambda: response)
        finally:
            tools.S.game = original_game

        replies = [message for message in current.messages if message.get("role") == "tool"]
        self.assertEqual(ran, ["first"])
        self.assertEqual([message["tool_call_id"] for message in replies], ["one", "two"])
        self.assertEqual(json.loads(replies[1]["content"])["error"], "tool skipped: interrupted by user")
        self.assertIn("Остановил", result)

    def test_step_limit_returns_visible_result_and_checkpoint(self):
        current = agent.Agent("fake")
        response = {"tool_calls": [self.tool_call("inspect_class", "one")]}
        
        with patch.object(agent, "MAX_STEPS", 1), \
             patch.object(agent, "call", return_value={"fields": ["HP=20"]}), \
             patch.object(agent, "save_agent_checkpoint") as checkpoint:
            result = current.send("inspect my player", complete=lambda: response)

        self.assertIn("Не завершил задачу за 1 шагов", result)
        self.assertEqual(current.messages[-1]["role"], "assistant")
        self.assertEqual(checkpoint.call_args.args[-1], "incomplete")

    def test_first_turn_hint_is_saved_after_game_selection(self):
        original_root, original_game, original_blocked = tools.ROOT, tools.S.game, tools.S.blocked
        try:
            with tempfile.TemporaryDirectory() as folder:
                tools.ROOT = Path(folder)
                tools.S.game = None
                tools.S.blocked = None

                def select_game(name, _args):
                    tools.S.game = {"name": "Example Game"}
                    tools.S.process = "ExampleGame.exe"
                    tools.S.blocked = None
                    return {"name": name}

                responses = iter([
                    {"tool_calls": [self.tool_call("select_game", "one")]},
                    {"content": "Продолжаю по сохранённой подсказке."},
                ])
                with patch.object(agent, "call", side_effect=select_game):
                    agent.Agent("fake").send(
                        "Example Game: зажми H подольше, затем нажми H для выбора.",
                        complete=lambda: next(responses))

                lessons = tools.read_lessons(tools.S.lessons_path())
                self.assertEqual(len(lessons), 1)
                self.assertEqual(lessons[0]["evidence"], "user_hint")
                self.assertIn("зажми H подольше", lessons[0]["lesson"])
        finally:
            tools.ROOT, tools.S.game, tools.S.blocked = original_root, original_game, original_blocked

    def test_explicit_executable_is_selected_and_backed_up_before_model_call(self):
        original_game, original_process, original_blocked = tools.S.game, tools.S.process, tools.S.blocked
        calls = []
        executable = r"D:\Games\Example Game\ExampleGame.exe"

        def preflight_call(name, args):
            calls.append((name, args))
            if name == "select_game":
                tools.S.game = {"name": "Example Game", "dir": str(Path(executable).parent)}
                tools.S.process = "ExampleGame.exe"
                tools.S.blocked = None
                return {"name": "Example Game", "blocked": None}
            return {"backups": ["test-backup.zip"]}

        tools.S.game, tools.S.process, tools.S.blocked = None, None, None
        try:
            current = agent.Agent("fake")

            def complete():
                self.assertEqual([name for name, _ in calls], ["select_game", "backup_saves"])
                self.assertEqual(calls[0][1]["path"], executable)
                return {"content": "Игра проверена до начала tool-цикла."}

            result = None
            with patch.object(agent, "call", side_effect=preflight_call):
                result = current.send(f'Запусти тест "{executable}"', complete=complete)

            self.assertIn("проверена", result)
        finally:
            tools.S.game, tools.S.process, tools.S.blocked = original_game, original_process, original_blocked

    def test_exact_path_preflight_persists_control_hint_for_selected_game(self):
        original_root, original_game, original_process, original_blocked = \
            tools.ROOT, tools.S.game, tools.S.process, tools.S.blocked
        executable = r"D:\Games\Example Game\ExampleGame.exe"
        try:
            with tempfile.TemporaryDirectory() as folder:
                tools.ROOT = Path(folder)
                tools.S.game, tools.S.process, tools.S.blocked = None, None, None

                def select_and_backup(name, args):
                    if name == "select_game":
                        tools.S.game = {"name": "Example Game", "dir": str(Path(executable).parent)}
                        tools.S.process = "ExampleGame.exe"
                        return {"name": "Example Game", "blocked": None}
                    return {"backups": ["test-backup.zip"]}

                with patch.object(agent, "call", side_effect=select_and_backup):
                    agent.Agent("fake").send(
                        f'{executable} Example Game: зажми H подольше, затем нажми H для подтверждения.',
                        complete=lambda: {"content": "Подсказка принята."})

                lessons = tools.read_lessons(tools.S.lessons_path())
                self.assertEqual(len(lessons), 1)
                self.assertEqual(lessons[0]["evidence"], "user_hint")
                self.assertIn("зажми H подольше", lessons[0]["lesson"])
        finally:
            tools.ROOT, tools.S.game, tools.S.process, tools.S.blocked = \
                original_root, original_game, original_process, original_blocked

    def test_game_tools_are_blocked_until_a_game_is_selected(self):
        original_game = tools.S.game
        tools.S.game = None
        response = {"tool_calls": [self.tool_call("screenshot", "one")]}
        responses = iter([response, {"content": "Сначала нужно выбрать игру."}])
        current = agent.Agent("fake")
        try:
            with patch.object(agent, "call") as call_tool:
                result = current.send("Проверь экран", complete=lambda: next(responses))

            call_tool.assert_not_called()
            tool_reply = next(item for item in current.messages if item.get("role") == "tool")
            self.assertIn("No game selected", json.loads(tool_reply["content"])["error"])
            self.assertIn("Сначала", result)
        finally:
            tools.S.game = original_game

    def test_explicit_user_ban_blocks_launch_tool(self):
        original_game = tools.S.game
        tools.S.game = {"name": "Example Game"}
        current = agent.Agent("fake")
        response = {"tool_calls": [self.tool_call("launch_game", "one")]}
        answers = iter([response, {"content": "Оставляю уже запущенную игру как есть."}])
        try:
            with patch.object(agent, "call") as call_tool:
                result = current.send("Не вызывай launch_game, игра уже запущена.",
                                     complete=lambda: next(answers))
            call_tool.assert_not_called()
            reply = next(item for item in current.messages if item.get("role") == "tool")
            self.assertIn("Blocked by the user's explicit instruction", json.loads(reply["content"])["error"])
            self.assertIn("Оставляю", result)
        finally:
            tools.S.game = original_game

    def test_duplicate_exact_game_selection_is_not_scanned_again(self):
        original_game = tools.S.game
        executable = r"D:\Games\Example Game\ExampleGame.exe"
        tools.S.game = {"name": "Example Game", "dir": str(Path(executable).parent)}
        current = agent.Agent("fake")
        response = {"tool_calls": [{"id": "one", "function": {
            "name": "select_game", "arguments": json.dumps({"path": executable})}}]}
        answers = iter([response, {"content": "Игра уже выбрана."}])
        try:
            with patch.object(agent, "call") as call_tool:
                result = current.send(f'Повтори выбор "{executable}"', complete=lambda: next(answers))
            call_tool.assert_not_called()
            reply = next(item for item in current.messages if item.get("role") == "tool")
            self.assertTrue(json.loads(reply["content"]).get("already_selected"))
            self.assertIn("уже выбрана", result)
        finally:
            tools.S.game = original_game

    def test_identical_failed_tool_call_is_not_retried_in_same_turn(self):
        original_game = tools.S.game
        tools.S.game = {"name": "Fake Game"}
        current = agent.Agent("fake")
        repeated_call = {"tool_calls": [self.tool_call("probe", "one")]}
        answers = iter([repeated_call, repeated_call, {"content": "Остановился после ошибки."}])
        try:
            with patch.object(agent, "call", return_value={"error": "no response"}) as call_tool:
                result = current.send("Проверь игру", complete=lambda: next(answers))
            call_tool.assert_called_once()
            tool_results = [json.loads(item["content"]) for item in current.messages
                            if item.get("role") == "tool"]
            self.assertEqual(tool_results[1]["error"], "Repeated failed call blocked: probe. Change the hypothesis or stop.")
            self.assertIn("Остановился", result)
        finally:
            tools.S.game = original_game


if __name__ == "__main__":
    unittest.main()
