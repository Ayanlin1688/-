"""Yanlin Smart-Creation Matrix entry point."""

from __future__ import annotations

import sys
import subprocess
from pathlib import Path

from core.crash_reporter import install_default

try:
    from qfluentwidgets import Theme, setTheme
except ImportError:
    if getattr(sys, 'frozen', False):
        raise  # 冻结版内置依赖：缺失属构建事故，直接暴露而不是尝试 pip
    # 开发态兜底：自动安装缺失的界面库（冻结版永不执行）。
    subprocess.check_call([sys.executable, "-m", "pip", "install", "PyQt-Fluent-Widgets", "--upgrade"])
    from qfluentwidgets import Theme, setTheme

from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication

from core.config_manager import ConfigManager
from ui.components.custom_widgets import ensure_ui_font


def _asset_path(name: str) -> Path:
    """兼容开发态与 PyInstaller 冻结态的资源定位。"""
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    return base / 'assets' / name


def main() -> int:
    try:
        config_manager = ConfigManager(migrate=True)
        logs_dir = config_manager.path.parent / 'logs'
    except Exception:
        # 配置目录不可用也要留痕：回退系统临时目录，再抛出原始异常。
        import tempfile
        logs_dir = Path(tempfile.gettempdir()) / 'YanlinMatrix-logs'
        install_default(logs_dir)
        raise
    # 无人值守兜底：任何未捕获异常先落盘留痕，不让进程静默退出。
    install_default(logs_dir)
    app = QApplication(sys.argv)
    app.setApplicationName('YanlinMatrix')
    icon_path = _asset_path('app.ico')
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    setTheme(Theme.DARK, save=False)
    ensure_ui_font()
    from ui.main_window import MainWindow
    from ui.splash import show_splash

    splash = show_splash()
    if splash is not None:
        app.processEvents()
    window = MainWindow(config_manager)
    window.show()
    if splash is not None:
        splash.finish(window)
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
