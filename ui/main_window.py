"""Fluent main window using the package's native navigation and title bar."""

from __future__ import annotations

from PyQt5.QtGui import QColor
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import Dialog, FluentIcon as FIF, FluentWindow, NavigationItemPosition, Theme, setTheme, setThemeColor

from core.config_manager import ConfigManager
from .components.custom_widgets import ensure_ui_font
from .pages.history_page import HistoryPage
from .pages.settings_page import SettingsPage
from .pages.workspace_page import WorkspacePage
from .theme import style_controls, style_page, apply_palette


class MainWindow(FluentWindow):
    def __init__(self, config_manager: ConfigManager | None = None) -> None:
        ensure_ui_font()
        apply_palette(QApplication.instance())
        setTheme(Theme.DARK, save=False)
        setThemeColor(QColor("#5e6ad2"), save=False)
        super().__init__()
        self.setMicaEffectEnabled(False)
        self.setCustomBackgroundColor("#0a0a0b", "#0a0a0b")
        self.config_manager = config_manager or ConfigManager()
        self.config_manager.load_config()
        self.setWindowTitle("StoryboardVideoStudio")
        self.resize(1400, 900)
        self.setMinimumSize(1100, 750)
        self._setup_pages()
        self.navigationInterface.setMinimumExpandWidth(100000)
        self.navigationInterface.panel.collapse()
        self.config_manager.error_callback = self.workspace_page.append_log
        self._closing = False
        self._close_timer = QTimer(self)
        self._close_timer.setInterval(100)
        self._close_timer.timeout.connect(self._finish_close)
        for page in (self.workspace_page, self.history_page, self.settings_page):
            style_page(page)
        style_controls(self)

    def _setup_pages(self) -> None:
        self.workspace_page = WorkspacePage(self.config_manager)
        self.history_page = HistoryPage(self.workspace_page.append_log, self.config_manager)
        self.settings_page = SettingsPage(self.config_manager, self.workspace_page.append_log)
        self.settings_page.debug_mode_changed.connect(self.workspace_page.set_debug_mode)
        self.workspace_page.history_changed.connect(self.history_page.update_history)
        self.history_page.redownload_requested.connect(self.workspace_page.redownload)
        self.addSubInterface(self.workspace_page, FIF.HOME, "工作台")
        self.addSubInterface(self.history_page, FIF.HISTORY, "任务历史")
        self.addSubInterface(self.settings_page, FIF.SETTING, "设置")
        self.navigationInterface.addItem(
            routeKey="about",
            icon=FIF.INFO,
            text="关于",
            onClick=self.show_about,
            selectable=False,
            position=NavigationItemPosition.BOTTOM,
        )

    def show_about(self) -> None:
        dialog = Dialog('关于 StoryboardVideoStudio', '版本 v2.0A\n扫描匹配 · 单模型提交 · 轮询 · 自动下载\n模型池和自动重试将在阶段2B启用', self)
        dialog.exec_()

    def _background_busy(self):
        return self.workspace_page.task_manager.is_running or self.workspace_page.jobs.busy or self.settings_page.jobs.busy

    def closeEvent(self, event):
        if self._background_busy():
            event.ignore()
            if not self._closing:
                self._closing = True
                self.workspace_page.shutdown()
                self.settings_page.setEnabled(False)
                self.workspace_page.append_log('正在安全退出，等待后台请求返回或超时...', 'warning')
                self._close_timer.start()
            return
        self.workspace_page.shutdown()
        super().closeEvent(event)

    def _finish_close(self):
        if not self._background_busy():
            self._close_timer.stop()
            self.close()
