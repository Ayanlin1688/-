"""Fluent main window using the package's native navigation and title bar."""

from __future__ import annotations

from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtCore import QTimer, QRectF
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import CaptionLabel, Dialog, FluentIcon as FIF, FluentWindow, NavigationItemPosition, Theme, setTheme, setThemeColor

from core.config_manager import ConfigManager
from core.network_clock import NetworkClock
from core.scheduler import ScheduleEngine
from .components.custom_widgets import ensure_ui_font
from .pages.history_page import HistoryPage
from .pages.settings_page import SettingsPage
from .pages.workspace_page import WorkspacePage
from .theme import style_controls, style_page, apply_palette
from .materials import ACCENT, background_brush
from .motion import PageTransition


class MainWindow(FluentWindow):
    def __init__(self, config_manager: ConfigManager | None = None, network_time=True) -> None:
        ensure_ui_font()
        apply_palette(QApplication.instance())
        setTheme(Theme.DARK, save=False)
        setThemeColor(QColor(ACCENT), save=False)
        super().__init__()
        self.setMicaEffectEnabled(False)
        self.setCustomBackgroundColor("#0a0a0b", "#0a0a0b")
        self.config_manager = config_manager or ConfigManager()
        self.config_manager.load_config()
        self.setWindowTitle("StoryboardVideoStudio")
        self.resize(1400, 900)
        self.setMinimumSize(1100, 750)
        self._setup_pages()
        self.stackedWidget.setAnimationEnabled(False)
        self.page_transition = PageTransition(self.stackedWidget)
        self.navigationInterface.setMinimumExpandWidth(100000)
        self.navigationInterface.panel.collapse()
        from qfluentwidgets import setCustomStyleSheet
        from qfluentwidgets.components.widgets.acrylic_label import isAcrylicAvailable
        self.navigationInterface.panel.setAcrylicEnabled(isAcrylicAvailable)
        navigation_style = 'NavigationPanel {background:rgba(0,0,0,0.3); border:0;}'
        setCustomStyleSheet(self.navigationInterface.panel, navigation_style, navigation_style)
        self.config_manager.error_callback = self.workspace_page.append_log
        self._closing = False
        self._close_timer = QTimer(self)
        self._close_timer.setInterval(100)
        self._close_timer.timeout.connect(self._finish_close)
        self._setup_schedule(network_time)
        for page in (self.workspace_page, self.history_page, self.settings_page):
            style_page(page)
        self._updateStackedBackground()
        style_controls(self)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), background_brush(QRectF(self.rect())))

    def _setup_pages(self) -> None:
        self.workspace_page = WorkspacePage(self.config_manager)
        self.history_page = HistoryPage(self.workspace_page.append_log, self.config_manager)
        self.settings_page = SettingsPage(self.config_manager, self.workspace_page.append_log)
        self.settings_page.debug_mode_changed.connect(self.workspace_page.set_debug_mode)
        self.workspace_page.task_manager.pool_updated.connect(self.settings_page.update_pool_state)
        self.settings_page.task_settings_changed.connect(self.workspace_page.params_card.refresh_task_settings)
        self.workspace_page.params_card.values_changed.connect(self.settings_page.refresh_task_settings)
        self.settings_page.pool_enabled.checkedChanged.connect(self.workspace_page.params_card.refresh_pool_hint)
        self.settings_page.strategy.currentTextChanged.connect(self.workspace_page.params_card.refresh_pool_hint)
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
        dialog = Dialog('关于 StoryboardVideoStudio', '版本 v3.1\n产品批处理 · 定时执行 · GitHub同步\n多模型调度 · 自动重试 · 并发生成 · 自动下载', self)
        dialog.exec_()

    def _setup_schedule(self, network_time):
        self.clock = NetworkClock()
        self.schedule_engine = ScheduleEngine(self.config_manager, now=self.clock.now, log=self.workspace_page.append_log)
        self.next_schedule_label = CaptionLabel(self.schedule_engine.next_text())
        self.workspace_page.layout().insertWidget(1, self.next_schedule_label)
        self._scheduled_batch = False
        self.settings_page.schedule_changed.connect(self._schedule_changed)
        self.workspace_page.task_manager.all_finished.connect(self._scheduled_finished)
        self.schedule_timer = QTimer(self)
        self.schedule_timer.setInterval(1000)
        self.schedule_timer.timeout.connect(self._schedule_tick)
        self.schedule_timer.start()
        if network_time:
            QTimer.singleShot(0, self._calibrate_clock)
        else:
            self.clock.status = '系统时间（未启用网络校时）'
        self.settings_page.update_schedule_state(self.clock.now(), self.clock.status)

    def _calibrate_clock(self):
        if self._closing:
            return
        def done(sample):
            self.clock.apply(sample)
            if abs(sample['offset']) >= 60:
                self.schedule_engine.configure(reset=True)
            self.workspace_page.append_log(self.clock.status, 'info')
            self._schedule_tick()
        def failed(message):
            self.clock.use_system_time()
            self.workspace_page.append_log(message, 'warning')
            self.settings_page.update_schedule_state(self.clock.now(), self.clock.status)
        self.settings_page.jobs.start(self.clock.sample, done, failed)

    def _schedule_changed(self):
        self.schedule_engine.configure(reset=True)
        self._schedule_tick()

    def _schedule_tick(self):
        if self._closing:
            return
        workspace = self.workspace_page
        busy = workspace.task_manager.is_running or workspace._redownloading
        if self.schedule_engine.tick(busy=busy):
            workspace.start_generation()
            self._scheduled_batch = workspace.task_manager.is_running
        self.next_schedule_label.setText(self.schedule_engine.next_text())
        self.settings_page.update_schedule_state(self.clock.now(), self.clock.status)

    def _scheduled_finished(self, *_):
        scheduled, self._scheduled_batch = self._scheduled_batch, False
        tasks = self.workspace_page.task_manager.tasks
        if scheduled and tasks and self.config_manager.config['schedule']['after_finish'] == 'close' and not any(t.get('status') == 'cancelled' for t in tasks):
            self.workspace_page.append_log('定时队列全部结束，自动关闭软件', 'info')
            QTimer.singleShot(100, self.close)

    def _background_busy(self):
        return self.workspace_page.task_manager.is_running or self.workspace_page.jobs.busy or self.settings_page.jobs.busy

    def closeEvent(self, event):
        self.schedule_timer.stop()
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
