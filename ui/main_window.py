"""Fluent main window using the package's native navigation and title bar."""

from __future__ import annotations

import sys

from PyQt5.QtGui import QColor, QLinearGradient, QPainter
from PyQt5.QtCore import QMargins, Qt, QTimer, QRectF
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
from .materials import ACCENT, paint_background
from .motion import PageTransition
from .model_catalog_controller import ModelCatalogController


class MainWindow(FluentWindow):
    def __init__(self, config_manager: ConfigManager | None = None, network_time=True, model_sync=None) -> None:
        ensure_ui_font()
        apply_palette(QApplication.instance())
        setTheme(Theme.DARK, save=False)
        setThemeColor(QColor(ACCENT), save=False)
        super().__init__()
        self._glass_material = 'gradient'
        self.setMicaEffectEnabled(False)
        self.setCustomBackgroundColor("#0A0B12", "#0A0B12")
        self.config_manager = config_manager or ConfigManager()
        self.config_manager.load_config()
        self.model_catalog = ModelCatalogController(
            self.config_manager, lambda *args: self.workspace_page.append_log(*args), self,
            auto_sync=network_time if model_sync is None else model_sync)
        self.setWindowTitle("Yanlin Smart-Creation Matrix")
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
        navigation_style = 'NavigationPanel {background:rgba(10,11,18,0.45); border:0;}'
        setCustomStyleSheet(self.navigationInterface.panel, navigation_style, navigation_style)
        self._setup_navigation_chrome()
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
        self.model_catalog.changed.connect(self.workspace_page.refresh_catalog)
        self.model_catalog.changed.connect(self.settings_page.refresh_catalog)
        self.model_catalog.state_changed.connect(self.settings_page.refresh_sync_state)
        self.model_catalog.sync_finished.connect(self.settings_page.model_sync_finished)
        self.model_catalog.sync_finished.connect(lambda ok, count, message: self.workspace_page.set_api_connection(ok, message))
        self.model_catalog.state_changed.connect(self.workspace_page.refresh_api_state)
        self.settings_page.sync_models_button.clicked.connect(self.model_catalog.refresh)
        self.settings_page.base_url.textChanged.connect(self.model_catalog.credentials_changed)
        self.settings_page.api_key.textChanged.connect(self.model_catalog.credentials_changed)
        self.settings_page.detection_changed.connect(self.workspace_page.scan_sources)
        QTimer.singleShot(0, self.model_catalog.start)
        QTimer.singleShot(0, self._apply_windows_material)

    def _setup_navigation_chrome(self) -> None:
        """64px icon rail: brand logo on top, user avatar pinned at the bottom."""
        from .components.brand_widgets import BrandLogo, UserAvatar
        panel = self.navigationInterface.panel
        panel.setFixedWidth(64)
        panel.menuButton.hide()
        return_button = getattr(panel, 'returnButton', None)
        if return_button is not None:
            return_button.hide()
        self.brand_logo = BrandLogo(self)
        self.navigationInterface.insertWidget(0, 'brandLogo', self.brand_logo,
                                              onClick=lambda: self.switchTo(self.workspace_page),
                                              position=NavigationItemPosition.TOP,
                                              tooltip='Yanlin Smart-Creation Matrix')
        self.user_avatar = UserAvatar(self)
        self.navigationInterface.addWidget('userAvatar', self.user_avatar, onClick=self.show_about,
                                           position=NavigationItemPosition.BOTTOM,
                                           tooltip='当前用户 · 版本信息')
        routes = [self.workspace_page.objectName(), self.history_page.objectName(),
                  self.settings_page.objectName(), 'about']
        for route in routes:
            try:
                item = self.navigationInterface.widget(route)
            except Exception:
                continue
            item.setFixedSize(56, 40)
            inner = getattr(item, 'itemWidget', item)
            inner.setFixedSize(56, 40)
            original = inner._margins
            inner._margins = lambda original=original: self._rail_margins(original())
            setter = getattr(inner, 'setIndicatorColor', None)
            if setter:
                setter('#5B8DEF', '#7C6CF0')
            self._decorate_rail_item(inner)
        # 关于入口由底部头像承担，导航栏保持纯图标三入口。
        try:
            self.navigationInterface.widget('about').hide()
        except Exception:
            pass

    @staticmethod
    def _decorate_rail_item(inner) -> None:
        """Selected chip background + the 3px blue/purple gradient indicator bar."""
        original = inner.paintEvent

        def paint(event, inner=inner, original=original):
            if inner.isSelected or inner.isEnter:
                painter = QPainter(inner)
                painter.setRenderHint(QPainter.Antialiasing)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(255, 255, 255, 12))
                painter.drawRoundedRect(QRectF(inner.rect()), 8, 8)
                painter.end()
            original(event)
            if inner.isSelected:
                painter = QPainter(inner)
                painter.setRenderHint(QPainter.Antialiasing)
                painter.setPen(Qt.NoPen)
                gradient = QLinearGradient(8, 10, 8, 26)
                gradient.setColorAt(0, QColor('#5B8DEF'))
                gradient.setColorAt(1, QColor('#7C6CF0'))
                painter.setBrush(gradient)
                painter.drawRoundedRect(QRectF(8, 10, 3, 16), 1.5, 1.5)
                painter.end()

        inner.paintEvent = paint

    @staticmethod
    def _rail_margins(margins: QMargins) -> QMargins:
        # Centre the compact icon inside the 56px rail button.
        return QMargins(margins.left() + 8, margins.top(), margins.right(), margins.bottom())

    def _apply_windows_material(self) -> str:
        """Detect Win11 Mica / Win10 Acrylic, falling back to the painted gradient."""
        if sys.platform != 'win32' or QApplication.platformName() == 'offscreen':
            return self._glass_material
        try:
            from qframelesswindow.utils.win32_utils import isGreaterEqualWin11, isGreaterEqualWin10
            if isGreaterEqualWin11():
                self.setMicaEffectEnabled(True)
                self._glass_material = 'mica'
            elif isGreaterEqualWin10():
                self.windowEffect.setAcrylicEffect(int(self.winId()), '0A0B12B4', False)
                self._glass_material = 'acrylic'
        except Exception:
            self._glass_material = 'gradient'
        self.update()
        return self._glass_material

    def paintEvent(self, event):
        painter = QPainter(self)
        paint_background(painter, QRectF(self.rect()), opaque=self._glass_material == 'gradient')

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
        self.history_page.resolve_requested.connect(self.workspace_page.resolve_submission)
        self.history_page.regenerate_requested.connect(self.workspace_page.regenerate)
        self.addSubInterface(self.workspace_page, FIF.HOME, "工作台")
        self.addSubInterface(self.history_page, FIF.HISTORY, "任务历史")
        self.addSubInterface(self.settings_page, FIF.SETTING, "设置")
        self.workspace_page.navigation_requested.connect(lambda route: self.switchTo({
            'workspace': self.workspace_page, 'history': self.history_page, 'settings': self.settings_page}[route]))
        self.navigationInterface.addItem(
            routeKey="about",
            icon=FIF.INFO,
            text="关于",
            onClick=self.show_about,
            selectable=False,
            position=NavigationItemPosition.BOTTOM,
        )

    def show_about(self) -> None:
        dialog = Dialog('关于 Yanlin Smart-Creation Matrix', '版本 v3.1\n产品批处理 · 定时执行 · GitHub同步\n多模型调度 · 自动重试 · 并发生成 · 自动下载', self)
        dialog.exec_()

    def _setup_schedule(self, network_time):
        self.clock = NetworkClock()
        self.schedule_engine = ScheduleEngine(self.config_manager, now=self.clock.now, log=self.workspace_page.append_log)
        self.next_schedule_label = CaptionLabel(self.schedule_engine.next_text())
        self.workspace_page.status_row.addWidget(self.next_schedule_label)
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
        return (self.workspace_page.task_manager.is_running or self.workspace_page.jobs.busy
                or self.settings_page.jobs.busy or self.model_catalog.jobs.busy)

    def closeEvent(self, event):
        self.schedule_timer.stop()
        self.model_catalog.shutdown()
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
