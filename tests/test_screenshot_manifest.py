import os
import unittest
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class ScreenshotManifestTests(unittest.TestCase):
    def test_six_named_screenshots_exist_and_are_nonempty(self):
        root = Path(__file__).resolve().parent.parent / "screenshots"
        # Generated screenshots are intentionally absent from a fresh Git clone.
        if not (root / 'screenshot_3_params.png').exists() or not (root / 'screenshot_6_log.png').exists():
            subprocess.run([sys.executable, str(root.parent / 'scripts' / 'capture_screenshots.py')],
                           cwd=root.parent, check=True, timeout=90, capture_output=True)
        names = [
            "screenshot_1_workspace.png",
            "screenshot_2_match_dialog.png",
            "screenshot_3_params.png",
            "screenshot_4_history.png",
            "screenshot_5_settings.png",
            "screenshot_6_log.png",
        ]
        missing = [name for name in names if not (root / name).is_file() or (root / name).stat().st_size == 0]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
