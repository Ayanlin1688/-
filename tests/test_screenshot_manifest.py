import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class ScreenshotManifestTests(unittest.TestCase):
    def test_six_named_screenshots_exist_and_are_nonempty(self):
        root = Path(__file__).resolve().parent.parent / "screenshots"
        names = [
            "screenshot_1_workspace.png",
            "screenshot_2_match_dialog.png",
            "screenshot_3_params_collapsed.png",
            "screenshot_4_history.png",
            "screenshot_5_settings.png",
            "screenshot_6_log_collapsed.png",
        ]
        missing = [name for name in names if not (root / name).is_file() or (root / name).stat().st_size == 0]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
