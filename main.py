"""StoryboardVideoStudio entry point."""

from __future__ import annotations

import sys
import subprocess

try:
    from qfluentwidgets import Theme, setTheme
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "PyQt-Fluent-Widgets", "--upgrade"])
    from qfluentwidgets import Theme, setTheme

from PyQt5.QtWidgets import QApplication

from core.config_manager import ConfigManager
from ui.components.custom_widgets import ensure_ui_font


def main() -> int:
    app = QApplication(sys.argv)
    setTheme(Theme.DARK, save=False)
    ensure_ui_font()
    from ui.main_window import MainWindow

    window = MainWindow(ConfigManager())
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
