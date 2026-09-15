"""Fluent main window using the package's native navigation and title bar."""

from __future__ import annotations

import sys

from PyQt5.QtGui import QColor, QLinearGradient, QPainter, QPainterPath
from PyQt5.QtCore import QMargins, Qt, QTimer, QRectF, QEvent
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
        # 自绘圆角窗口：无边框 + 透明外圈，在 Win10 上也能获得一致的 12px 圆角与高光描边。
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._window_margin = 8
        self._window_radius = 12
        self._frame_margin = None
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
        self.settings_page.appearance_changed.connect(self.apply_appearance)
        QTimer.singleShot(0, self.model_catalog.start)
        QTimer.singleShot(0, self._sync_window_frame)
        QTimer.singleShot(80, self.apply_appearance)

    def apply_appearance(self):
        """应用外观设置（主题模式 / 高斯模糊），切换立即生效。"""
        config = self.settings_page.config_manager.config.get('appearance', {})
        try:
            from .theme import apply_ui_mode
            apply_ui_mode(config.get('theme', 'dark'))
        except Exception:
            pass
        self._apply_blur_setting(config)

    def _apply_blur_setting(self, config=None):
        config = config if config is not None else self.settings_page.config_manager.config.get('appearance', {})
        enabled = bool(config.get('blur', False))
        try:
            hwnd = int(self.winId())
            if enabled:
                from . import materials
                tint = 'F3F6FCA8' if materials.is_light() else '12141CA8'
                self.windowEffect.setAcrylicEffect(hwnd, tint, True)
            else:
                self.windowEffect.removeBackgroundEffect(hwnd)
        except Exception:
            pass
        self.update()

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
            item.setFixedSize(56, 44)
            inner = getattr(item, 'itemWidget', item)
            inner.setFixedSize(56, 44)
            original = inner._margins
            inner._margins = lambda original=original: self._rail_margins(original())
            setter = getattr(inner, 'setIndicatorColor', None)
            if setter:
                setter('#5B8DEF', '#7C6CF0')
            self._decorate_rail_item(inner)
        # 图标项之间的呼吸感：加大顶部布局间距并统一底部留白。
        panel = self.navigationInterface.panel
        for layout_name, spacing in (('topLayout', 6),):
            layout = getattr(panel, layout_name, None)
            try:
                if layout is not None:
                    layout.setSpacing(spacing)
            except Exception:
                pass
        try:
            panel.bottomLayout.setContentsMargins(0, 0, 0, 10)
        except Exception:
            pass
        # 标题栏呼吸位：等窗口标题写入后再应用图标/标题间距。
        QTimer.singleShot(30, self._apply_titlebar_tweak)
        # 最大化按钮：接管为带校验重试的切换，避免个别环境下点击被系统动画吞掉。
        try:
            max_button = self.titleBar.maxBtn
            try:
                max_button.clicked.disconnect()
            except Exception:
                pass
            max_button.clicked.connect(self._studio_toggle_maximized)
        except Exception:
            pass
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
                from .materials import ink
                painter.setBrush(ink(12))
                painter.drawRoundedRect(QRectF(inner.rect()), 8, 8)
                painter.end()
            original(event)
            if inner.isSelected:
                painter = QPainter(inner)
                painter.setRenderHint(QPainter.Antialiasing)
                painter.setPen(Qt.NoPen)
                bar_y = (inner.height() - 16) / 2
                gradient = QLinearGradient(8, bar_y, 8, bar_y + 16)
                gradient.setColorAt(0, QColor('#5B8DEF'))
                gradient.setColorAt(1, QColor('#7C6CF0'))
                painter.setBrush(gradient)
                painter.drawRoundedRect(QRectF(8, bar_y, 3, 16), 1.5, 1.5)
                painter.end()

        inner.paintEvent = paint

    @staticmethod
    def _rail_margins(margins: QMargins) -> QMargins:
        # Centre the compact icon inside the 56px rail button.
        return QMargins(margins.left() + 8, margins.top(), margins.right(), margins.bottom())

    def _apply_windows_material(self) -> str:
        """系统 Acrylic/Mica 与自绘圆角相互冲突，统一由自绘材质接管。"""
        self._glass_material = 'gradient'
        self.update()
        return self._glass_material

    def _window_margins_px(self):
        return 0 if (self.isMaximized() or self.isFullScreen()) else self._window_margin

    def _sync_window_frame(self) -> None:
        """把标题栏与内容同步到圆角内缩区，并在最大化时放大到整屏。"""
        margin = self._window_margins_px()
        if self._frame_margin != margin:
            self._frame_margin = margin
            try:
                self.hBoxLayout.setContentsMargins(margin, margin, margin, margin)
            except Exception:
                pass
        try:
            self.titleBar.setGeometry(margin, margin, max(0, self.width() - 2 * margin), self.titleBar.height())
        except Exception:
            pass
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_window_frame()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            QTimer.singleShot(0, self._sync_window_frame)

    def _apply_titlebar_tweak(self):
        """标题从导航徽标右侧开始（目标绝对 x=88），空白图标隐藏，标题 13px/600。"""
        try:
            title_bar = self.titleBar
            if getattr(title_bar, '_studio_gap', False):
                return
            layout = getattr(title_bar, 'hBoxLayout', None) or title_bar.layout()
            if layout is None:
                return
            title_widget = None
            want = self.windowTitle()
            for index in range(layout.count()):
                item = layout.itemAt(index)
                widget = item.widget() if item is not None else None
                if widget is not None and hasattr(widget, 'text') and callable(getattr(widget, 'text', None)) and widget.text() == want:
                    title_widget = widget
                    break
            if title_widget is None:
                for index in range(layout.count()):
                    item = layout.itemAt(index)
                    widget = item.widget() if item is not None else None
                    if widget is not None and hasattr(widget, 'text') and callable(getattr(widget, 'text', None)) and widget.text():
                        title_widget = widget
                        break
            if title_widget is None:
                return
            # 空白的标题图标隐藏，避免与导航徽标争抢左侧区域。
            for index in range(layout.count()):
                item = layout.itemAt(index)
                widget = item.widget() if item is not None else None
                if (widget is not None and widget is not title_widget
                        and hasattr(widget, 'text') and callable(getattr(widget, 'text', None))
                        and not widget.text()):
                    widget.hide()
            # 标题移到徽标右侧：目标 titlebar 坐标 x=80（绝对 88 × 徽标右缘 ~62 + 26px 间距）。
            target_x = 80
            for _ in range(2):
                try:
                    layout.activate()
                except Exception:
                    pass
                extra = target_x - title_widget.x()
                if extra <= 0:
                    break
                layout.insertSpacing(layout.indexOf(title_widget), extra)
            title_widget.setStyleSheet('font-size:13px; font-weight:600; background:transparent;')
            title_bar._studio_gap = True
        except Exception:
            pass

    def _studio_toggle_maximized(self):
        """最大化/还原切换：带一次状态校验重试，避免个别环境点击被系统动画吞掉。"""
        target_max = not self.isMaximized()
        self._apply_max_state(target_max)
        QTimer.singleShot(160, lambda target=target_max: self._verify_max_state(target))

    def _apply_max_state(self, maximized: bool):
        if maximized:
            self.showMaximized()
        else:
            self.showNormal()
        self.update()

    def _verify_max_state(self, target_max: bool):
        try:
            if self.isMaximized() != target_max:
                self._apply_max_state(target_max)
        except Exception:
            pass

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        margin = self._window_margins_px()
        radius = self._window_radius if margin else 0
        rect = QRectF(self.rect()).adjusted(margin, margin, -margin, -margin)
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.setClipPath(path)
        paint_background(painter, rect, opaque=True)
        painter.setClipping(False)
        if margin:
            # 1px 高光描边，让圆角边缘在深色桌面上有物理厚度感。
            from .materials import is_light
            painter.setPen(QColor(15, 26, 52, 46) if is_light() else QColor(255, 255, 255, 26))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

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
