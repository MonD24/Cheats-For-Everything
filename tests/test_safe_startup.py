import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import unity_tools  # noqa: E402
from trainerkit import navigator  # noqa: E402


class FakeGame:
    process = "FakeGame.exe"
    dir = ROOT

    @staticmethod
    def scene():
        return "MainMenu"


class FakeNavigator:
    def __init__(self, *args, **kwargs):
        pass

    @staticmethod
    def alive(_player_class):
        return False

    @staticmethod
    def replay(*_args):
        raise AssertionError("saved route must not be replayed blindly")


class SafeStartupTests(unittest.TestCase):
    def test_gameplay_check_only_returns_screen_and_sends_no_keys(self):
        process = type("Process", (), {"info": {"name": "FakeGame.exe"}})()
        with tempfile.TemporaryDirectory() as folder:
            screenshot = Path(folder) / "screen.png"
            Image.new("L", (160, 90), 100).save(screenshot)
            with (
                patch.object(unity_tools, "need_unity", return_value=FakeGame()),
                patch.object(unity_tools, "guess_player_class", return_value="Game.Player"),
                patch.object(unity_tools, "playbook", return_value={"route": ["tap s", "tap h"]}),
                patch.object(unity_tools, "launch_game", side_effect=AssertionError("game should remain running")),
                patch.object(unity_tools.play, "shot", return_value=str(screenshot)),
                patch.object(unity_tools.play, "run", side_effect=AssertionError("must not send keys")),
                patch.object(navigator, "Navigator", FakeNavigator),
                patch("psutil.process_iter", return_value=[process]),
            ):
                result = unity_tools.reach_gameplay()

        self.assertFalse(result["ok"])
        self.assertTrue(result["needs_visual_decision"])
        self.assertIn("No keys were sent", result["hint"])
        self.assertTrue(base64.b64decode(result["image_b64"]))

    def test_startup_input_is_one_action_and_cannot_repeat_without_screen_change(self):
        original_startup = unity_tools.CTX.get("startup")
        with tempfile.TemporaryDirectory() as folder:
            baseline = Path(folder) / "baseline.png"
            Image.new("L", (160, 90), 100).save(baseline)
            unity_tools.CTX["startup"] = {"screen": str(baseline), "attempted": []}

            def capture(_process, output, _scale=0.5):
                Image.new("L", (160, 90), 100).save(output)
                return str(output)

            def run_without_game(_process, steps):
                Image.new("L", (160, 90), 100).save(steps[-1].split(" ", 1)[1])

            try:
                with (
                    patch.object(unity_tools, "need_unity", return_value=FakeGame()),
                    patch.object(unity_tools.play, "shot", side_effect=capture),
                    patch.object(unity_tools.play, "run", side_effect=run_without_game) as run,
                ):
                    multiple = unity_tools.press_keys(["tap h", "tap s"])
                    first = unity_tools.press_keys(["tap h"])
                    repeated = unity_tools.press_keys(["tap h"])
            finally:
                unity_tools.CTX["startup"] = original_startup

        self.assertFalse(multiple["input_sent"])
        self.assertTrue(first.get("image_b64"))
        self.assertFalse(first["screen_changed"])
        self.assertFalse(repeated["input_sent"])
        self.assertIn("already tried", repeated["error"])
        self.assertEqual(run.call_count, 1)


class SmallMenuChangeTest(unittest.TestCase):
    def test_toggled_option_counts_as_a_change(self):
        """Run 1 regression: '2 players' -> '1 player' changed ~2% of the screen and the old 16x9 hash
        called it unchanged, so the startup guard blocked the key."""
        from trainerkit import screen
        with tempfile.TemporaryDirectory() as folder:
            a, b = Path(folder) / "a.png", Path(folder) / "b.png"
            im = Image.new("L", (640, 360), 90)
            im.save(a)
            for x in range(560, 620):          # a small label changes
                for y in range(40, 70):
                    im.putpixel((x, y), 250)
            im.save(b)
            self.assertTrue(screen.changed(str(a), str(b)))
            self.assertFalse(screen.changed(str(a), str(a)))


if __name__ == "__main__":
    unittest.main()
