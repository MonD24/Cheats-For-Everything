import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trainerkit import play  # noqa: E402


class PlayInputValidationTests(unittest.TestCase):
    def test_malformed_hold_is_rejected_before_window_focus_or_input(self):
        with patch.object(play, "focus") as focus, patch.object(play, "tap") as tap:
            with self.assertRaisesRegex(ValueError, "hold <key> <seconds>"):
                play.run("Fake.exe", ["hold h"])

        focus.assert_not_called()
        tap.assert_not_called()

    def test_hold_with_duration_is_valid_and_releases_through_hold_helper(self):
        with patch.object(play, "focus"), patch.object(play, "hold") as hold:
            play.run("Fake.exe", ["hold h 3"])

        hold.assert_called_once_with(["h"], 3.0)

    def test_release_is_rejected_with_actionable_guidance(self):
        with self.assertRaisesRegex(ValueError, "releases automatically"):
            play.validate_step("release h")


if __name__ == "__main__":
    unittest.main()