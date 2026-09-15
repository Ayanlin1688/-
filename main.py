"""Yanlin Smart-Creation Matrix entry point."""

from __future__ import annotations

import sys
import subprocess
from pathlib import Path

from core.crash_reporter import install_default

try:
    from qfluentwidgets import Theme, setTheme
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "PyQt-Fluent-Widgets", "--upgrade"])
    from qfluentwidgets import Theme, setTheme

from PyQt5.QtWidgets import QApplication

from core.config_manager import ConfigManager
from ui.components.custom_widgets import ensure_ui_font


def main() -> int:
    # 无人值守兜底：任何未捕获异常先落盘留痕，不让进程静默退出。
    install_default(Path(__file__).resolve().parent / 'logs')
    app = QApplication(sys.argv)
    setTheme(Theme.DARK, save=False)
    ensure_ui_font()
    from ui.main_window import MainWindow

    window = MainWindow(ConfigManager(migrate=True))
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
