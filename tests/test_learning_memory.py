import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import tools  # noqa: E402
import agent  # noqa: E402


class LearningMemoryTests(unittest.TestCase):
    def test_user_hint_classifier_accepts_controls_not_generic_stop_text(self):
        self.assertTrue(tools.should_remember_user_hint("зажми H подольше и нажми для подтверждения"))
        self.assertTrue(tools.should_remember_user_hint("choose 1 Player before accepting"))
        self.assertFalse(tools.should_remember_user_hint("остановись, ты делаешь не то"))

    def test_unfinished_checkpoint_is_visible_only_for_its_game(self):
        original_root, original_game = tools.ROOT, tools.S.game
        try:
            with tempfile.TemporaryDirectory() as folder:
                tools.ROOT = Path(folder)
                tools.S.game = {"name": "Example Game"}
                self.assertTrue(tools.save_agent_checkpoint(
                    "Build a trainer", "inspect live player", "inspect_class", "HP=40", "incomplete"))
                context = agent.Agent("fake").context()
                self.assertIn("unfinished_task", context)
                self.assertIn("inspect live player", context)

                tools.S.game = {"name": "Other Game"}
                self.assertNotIn("unfinished_task", agent.Agent("fake").context())
        finally:
            tools.ROOT, tools.S.game = original_root, original_game

    def test_session_paths_isolate_lessons_by_game(self):
        original_root, original_game = tools.ROOT, tools.S.game
        try:
            with tempfile.TemporaryDirectory() as folder:
                tools.ROOT = Path(folder)
                tools.S.game = {"name": "Example Game"}
                first_path = tools.S.lessons_path()
                tools.remember_lesson(first_path, "Hold H to skip", "user_hint")

                tools.S.game = {"name": "Other Game"}
                second_path = tools.S.lessons_path()

                self.assertNotEqual(first_path, second_path)
                self.assertTrue(first_path.exists())
                self.assertFalse(second_path.exists())
        finally:
            tools.ROOT, tools.S.game = original_root, original_game

    def test_agent_context_contains_only_selected_game_memory(self):
        original_root, original_game = tools.ROOT, tools.S.game
        try:
            with tempfile.TemporaryDirectory() as folder:
                tools.ROOT = Path(folder)
                tools.S.game = {"name": "Example Game"}
                tools.remember_lesson(tools.S.lessons_path(), "Hold H to skip", "user_hint")
                context = agent.Agent("fake").context()
                self.assertIn("Hold H to skip", context)

                tools.S.game = {"name": "Other Game"}
                other_context = agent.Agent("fake").context()
                self.assertNotIn("Hold H to skip", other_context)
        finally:
            tools.ROOT, tools.S.game = original_root, original_game

    def test_lessons_are_labeled_deduplicated_and_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "lessons.json"
            saved = tools.remember_lesson(path, "Hold H to skip the cutscene", "user_hint")
            repeated = tools.remember_lesson(path, " hold h to skip the cutscene ", "verified")

            lessons = tools.read_lessons(path)
            self.assertTrue(saved["saved"])
            self.assertTrue(repeated["saved"])
            self.assertEqual(len(lessons), 1)
            self.assertEqual(lessons[0]["evidence"], "verified")
            self.assertEqual(lessons[0]["occurrences"], 2)

    def test_unfinished_checkpoint_is_visible_only_for_its_game(self):
        original_root, original_game = tools.ROOT, tools.S.game
        try:
            with tempfile.TemporaryDirectory() as folder:
                tools.ROOT = Path(folder)
                tools.S.game = {"name": "Example Game"}
                self.assertTrue(tools.save_agent_checkpoint(
                    "Build trainer", "verify health", "verify_cheats", "health field missing", "incomplete"))
                context = agent.Agent("fake").context()
                self.assertIn("unfinished_task", context)
                self.assertIn("health field missing", context)

                tools.S.game = {"name": "Another Game"}
                self.assertNotIn("unfinished_task", agent.Agent("fake").context())
        finally:
            tools.ROOT, tools.S.game = original_root, original_game

    def test_game_hint_classifier_avoids_generic_stop_message(self):
        self.assertTrue(tools.should_remember_user_hint("зажми H подольше и нажми для подтверждения"))
        self.assertTrue(tools.should_remember_user_hint("choose 1 Player before accepting"))
        self.assertFalse(tools.should_remember_user_hint("остановись, ты делаешь не то"))
        self.assertFalse(tools.should_remember_user_hint("Проведи короткий smoke test harness, выбери 1 Player"))

    def test_recent_experience_keeps_useful_outcomes_and_drops_noise(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "agent_log.jsonl"
            events = [
                {"tool": "screenshot", "ok": True, "args": {}, "summary": "noise"},
                {"tool": "reach_gameplay", "ok": True, "args": {"player_class": "Game.Player"},
                 "summary": "player already live"},
                {"tool": "press_keys", "ok": False, "args": {"steps": ["screenshot"]},
                 "summary": "unknown step"},
            ]
            path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")

            experience = tools.recent_experience(path)

            self.assertEqual([item["tool"] for item in experience], ["reach_gameplay", "press_keys"])
            self.assertEqual(experience[1]["args"], {})
            self.assertFalse(experience[1]["ok"])


if __name__ == "__main__":
    unittest.main()