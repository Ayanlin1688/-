"""Fluent main window using the package's native navigation and title bar."""

from __future__ import annotations

import sys

from PyQt5.QtGui import QColor, QLinearGradient, QPainter, QPainterPath
from PyQt5.QtCore import QMargins, Qt, QTimer, QRectF, QEvent
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import CaptionLabel, Dialog, FluentIcon as FIF, FluentWindow, InfoBar, NavigationItemPosition, Theme, setTheme, setThemeColor

from core.config_manager import ConfigManager
from core.crash_reporter import set_notify
from core.i18n import set_language, tr
from core.version import APP_NAME, APP_VERSION
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
        self.config_manager = config_manager or ConfigManager(migrate=True)
        self.config_manager.load_config()
        # 语言须在界面构建前生效：否则构建期 tr() 拿不到配置语言（重启后英文也显示中文）。
        set_language(self.config_manager.config.get('appearance', {}).get('language', '简体中文'))
        self.model_catalog = ModelCatalogController(
            self.config_manager, lambda *args: self.workspace_page.append_log(*args), self,
            auto_sync=network_time if model_sync is None else model_sync)
        self.setWindowTitle("Yanlin Smart-Creation Matrix")
        self.resize(1400, 900)
        self.setMinimumSize(1100, 750)
        self._setup_pages()
        self.stackedWidget.setAnimationEnabled(False)
        self.page_transition = PageTransition(self.stackedWidget)
        # 两态导航：折叠 64px 图标栏 ↔ 展开 240px 文字导航（左上菜单按钮手动切换）。
        from . import tokens as design
        _nav_panel = self.navigationInterface.panel
        self.navigationInterface.setMinimumExpandWidth(900)
        _nav_panel.setExpandWidth(design.NAV_EXPAND_WIDTH)
        _nav_panel.setMenuButtonVisible(True)
        _nav_panel.collapse()
        from qfluentwidgets.components.widgets.acrylic_label import isAcrylicAvailable
        self.navigationInterface.panel.setAcrylicEnabled(isAcrylicAvailable)
        self._rail_indicator_setters = []
        self._apply_nav_style()
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
        self.settings_page.api_credentials_changed.connect(self.model_catalog.credentials_changed)
        self.settings_page.detection_changed.connect(self.workspace_page.scan_sources)
        self.settings_page.appearance_changed.connect(self.apply_appearance)
        QTimer.singleShot(0, self.model_catalog.start)
        QTimer.singleShot(0, self._sync_window_frame)
        QTimer.singleShot(80, self.apply_appearance)
        QTimer.singleShot(5000, self._check_updates)
        set_notify(self._uncaught_exception)
        self._announce_license()

    def _uncaught_exception(self, exc_type, exc, path):
        """未捕获异常：写入口志；主线程内同时提示 UI（子线程只落盘）。"""
        message = f'未捕获异常已拦截：{exc_type.__name__}: {exc}；现场文件：{path or "logs 目录"}'
        try:
            from PyQt5.QtCore import QThread
            page = getattr(self, 'workspace_page', None)
            if page is None:
                return
            app = QApplication.instance()
            if app is not None and QThread.currentThread() is app.thread():
                page.append_log(message, 'error')
            else:
                page.run_log.write(message, 'error')
        except Exception:
            pass

    def _check_updates(self):
        """启动时可选更新检查：读取配置的版本清单地址，发现新版本提示下载。"""
        updates = self.config_manager.config.get('updates') or {}
        url = str(updates.get('manifest_url') or '').strip()
        if not url or not updates.get('check_on_start', True):
            return
        from core.update_check import check_for_update

        def done(result):
            if not result or self._closing:
                return
            self.workspace_page.append_log(f"发现新版本 v{result['version']}（当前 v{APP_VERSION}），可前往下载更新", 'info')
            InfoBar.info('发现新版本', f"v{result['version']} 已发布（当前 v{APP_VERSION}）", parent=self, duration=8000)

        self.workspace_page.jobs.start(lambda: check_for_update(url), done, lambda message: None)

    def _retranslate_chrome(self):
        """语言切换即时生效范围：导航 / 关于入口 / 关于对话框（其余界面重启后完整生效）。"""
        for item, text in zip(getattr(self, '_nav_items', []), [tr('工作台'), tr('任务历史'), tr('设置')]):
            try:
                item.setText(text)
            except Exception:
                pass
        about_item = getattr(self, '_about_item', None)
        if about_item is not None:
            try:
                about_item.setText(tr('关于'))
            except Exception:
                pass

    def _announce_license(self):
        """启动公告许可状态；首次运行写入试用起点。"""
        from core.licensing import license_status
        status = license_status(self.config_manager.config)
        try:
            self.config_manager.save_config()
        except Exception:
            pass
        self.workspace_page.append_log('许可状态：' + status['detail'], 'info')
        if status['state'] in {'expired', 'invalid'}:
            InfoBar.warning(tr('许可提示'), status['detail'], parent=self, duration=8000)

    def apply_appearance(self):
        """应用外观设置（主题 / 高斯模糊 / 语言），切换立即生效；主题未变化时不重复全量刷新。"""
        config = self.settings_page.config_manager.config.get('appearance', {})
        set_language(config.get('language', '简体中文'))
        self._retranslate_chrome()
        from .motion import set_reduced_motion
        set_reduced_motion(bool(config.get('reduce_motion', False)))
        try:
            from . import materials
            from .palettes import resolve
            desired = resolve(config.get('theme', 'dark'))
            if desired != materials.THEME_ID:
                from .theme import apply_ui_mode
                apply_ui_mode(config.get('theme', 'dark'))
        except Exception:
            pass
        # 主题相关的窗口级外观：导航栏底色、自绘背景色、轨道指示条。
        try:
            from . import materials
            bg = materials.palette()['bg1']
            self.setCustomBackgroundColor(bg, bg)
            self._apply_nav_style()
            for setter in self._rail_indicator_setters:
                try:
                    th = materials.palette()
                    setter(th['accent'], th['accent'])
                except Exception:
                    pass
        except Exception:
            pass
        self._apply_blur_setting(config)

    def _apply_nav_style(self):
        """导航栏底色跟随当前主题。"""
        from qfluentwidgets import setCustomStyleSheet
        from . import materials
        style = 'NavigationPanel {background:%s; border:0;}' % materials.nav_background()
        setCustomStyleSheet(self.navigationInterface.panel, style, style)

    def _apply_blur_setting(self, config=None):
        config = config if config is not None else self.settings_page.config_manager.config.get('appearance', {})
        enabled = bool(config.get('blur', False))
        try:
            hwnd = int(self.winId())
            if enabled:
                from . import materials
                self.windowEffect.setAcrylicEffect(hwnd, materials.acrylic_tint(), True)
            else:
                self.windowEffect.removeBackgroundEffect(hwnd)
        except Exception:
            pass
        self.update()

    def _setup_navigation_chrome(self) -> None:
        """两态导航：折叠为 64px 图标栏（品牌徽标居顶、用户头像贴底），展开为 240px 文字导航。"""
        from .components.brand_widgets import BrandLogo, UserAvatar
        from . import tokens as design
        panel = self.navigationInterface.panel
        panel.setFixedWidth(design.NAV_COLLAPSED_WIDTH)
        # 菜单按钮保留为两态切换入口：折叠态即左上角汉堡按钮，展开态为收起入口。
        panel.menuButton.setVisible(True)
        try:
            panel.menuButton.clicked.disconnect()
        except Exception:
            pass
        panel.menuButton.clicked.connect(self._toggle_navigation)
        try:
            self.navigationInterface.displayModeChanged.connect(self._on_nav_display_mode_changed)
        except Exception:
            pass
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
        self._nav_rail_items = []
        for route in routes:
            try:
                item = self.navigationInterface.widget(route)
            except Exception:
                continue
            item.setFixedHeight(44)
            inner = getattr(item, 'itemWidget', item)
            original = inner._margins
            self._nav_rail_items.append((item, inner, original))
            inner._margins = lambda original=original: self._rail_margins(original())
            setter = getattr(inner, 'setIndicatorColor', None)
            if setter:
                from . import materials
                th = materials.palette()
                setter(th['accent'], th['accent'])
                self._rail_indicator_setters.append(setter)
            self._decorate_rail_item(inner)
        # 分组：在「设置」前插入分隔符（创作 / 系统 两组）。
        try:
            settings_item = self.navigationInterface.widget(self.settings_page.objectName())
            index = panel.topLayout.indexOf(settings_item)
            if index > 0:
                panel.insertSeparator(index)
        except Exception:
            pass
        # 应用初始折叠样式。
        self._sync_nav_pane(True)
        # 恢复上次的展开状态（记忆化）。
        try:
            if self.config_manager.config.get('appearance', {}).get('nav_expanded'):
                QTimer.singleShot(120, self._toggle_navigation)
        except Exception:
            pass
        # 图标项之间的呼吸感：加大顶部布局间距并统一底部留白。
        panel = self.navigationInterface.panel
        for layout_name, spacing in (('topLayout', design.SPACE['sm']),):
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
        # 标题栏拖拽误触发加固：点击手抖不再演变为“窗口漂移”。
        self._harden_titlebar_drag()

    def _toggle_navigation(self) -> None:
        """菜单按钮：两态切换（展开前先释放折叠态的固定宽度，保证过渡动画）。"""
        panel = self.navigationInterface.panel
        expanding = panel.isCollapsed()
        try:
            if expanding:
                self._sync_nav_pane(False)
                panel.expand()
            else:
                panel.collapse()
        except Exception:
            pass
        try:
            self.config_manager.update(('appearance', 'nav_expanded'), bool(expanding))
        except Exception:
            pass
        # 动画结束后再落一次样式，避免被原生 setCompacted 的固定尺寸覆盖。
        QTimer.singleShot(320, self._settle_nav_pane)

    def _settle_nav_pane(self) -> None:
        try:
            self._sync_nav_pane(self.navigationInterface.panel.isCollapsed())
        except Exception:
            pass

    def _on_nav_display_mode_changed(self, mode=None) -> None:
        # 原生在发出信号后还会执行 setCompacted（会重置尺寸），延后一拍再同步。
        QTimer.singleShot(0, self._settle_nav_pane)

    def _sync_nav_pane(self, collapsed: bool) -> None:
        """按折叠/展开状态同步导航尺寸：折叠 64px 固定栏；展开释放宽度并还原内边距。"""
        from . import tokens as design
        panel = self.navigationInterface.panel
        try:
            if collapsed:
                panel.setFixedWidth(design.NAV_COLLAPSED_WIDTH)
            else:
                panel.setMinimumWidth(0)
                panel.setMaximumWidth(16777215)
        except Exception:
            pass
        for item, inner, original in getattr(self, '_nav_rail_items', []):
            try:
                if collapsed:
                    item.setMinimumSize(0, 44); item.setMaximumSize(56, 44)
                    inner.setMinimumSize(0, 44); inner.setMaximumSize(56, 44)
                    inner._margins = lambda original=original: self._rail_margins(original())
                else:
                    item.setMinimumSize(0, 44); item.setMaximumSize(16777215, 44)
                    inner.setMinimumSize(0, 44); inner.setMaximumSize(16777215, 44)
                    inner._margins = original
                inner.updateGeometry()
            except Exception:
                pass
        try:
            panel.menuButton.setToolTip(tr('展开 / 收起导航'))
        except Exception:
            pass

    @staticmethod
    def _decorate_rail_item(inner) -> None:
        """Selected chip background + the 3px accent indicator bar."""
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
                from . import materials
                painter.setBrush(QColor(materials.palette()['accent']))
                painter.drawRoundedRect(QRectF(8, bar_y, 3, 16), 1.5, 1.5)
                painter.end()

        inner.paintEvent = paint

    def _harden_titlebar_drag(self) -> None:
        """标题栏拖拽加固：7px 位移阈值，杜绝点击手抖被误判为“拖动窗口”。

        库原实现会在每个鼠标移动事件上触发 startSystemMove（SC_MOVE），
        点击最大化按钮时的手指细微抖动都可能让窗口开始跟随鼠标漂移。
        这里加阈值：位移不足 7px 的移动一律不启动窗口移动；超过阈值后
        维持库原有的“每次移动调用一次”行为，拖动体验不变。
        """
        try:
            bar = self.titleBar
            state = {'press': None, 'seen': False}
            original_press = bar.mousePressEvent
            original_move = bar.mouseMoveEvent
            original_release = getattr(bar, 'mouseReleaseEvent', None)

            def guarded_press(event):
                if event.button() == Qt.LeftButton:
                    state['press'] = event.pos()
                    state['seen'] = True
                return original_press(event)

            def guarded_move(event):
                try:
                    if event.buttons() & Qt.LeftButton:
                        if not state['seen'] or state['press'] is None:
                            # 未收到按下（例如被其它控件吞掉）：先记录参考点，不触发移动。
                            state['press'] = event.pos()
                            state['seen'] = True
                            return None
                        delta = event.pos() - state['press']
                        if delta.manhattanLength() < 7 or not bar.canDrag(event.pos()):
                            return None
                    return original_move(event)
                except Exception:
                    return original_move(event)

            def guarded_release(event):
                state['press'] = None
                state['seen'] = False
                if original_release is not None:
                    return original_release(event)
                return None

            bar.mousePressEvent = guarded_press
            bar.mouseMoveEvent = guarded_move
            bar.mouseReleaseEvent = guarded_release
        except Exception:
            pass

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
            try:
                self.titleBar.maxBtn.setMaxState(self.isMaximized())
            except Exception:
                pass

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
        """最大化/还原切换：带状态校验重试、代数防竞态、Win32 兜底与鼠标释放卫生。"""
        target_max = not self.isMaximized()
        self._max_toggle_generation = getattr(self, '_max_toggle_generation', 0) + 1
        generation = self._max_toggle_generation
        self._apply_max_state(target_max)
        self._release_titlebar_mouse()
        QTimer.singleShot(160, lambda: self._verify_max_state(target_max, generation))

    def _apply_max_state(self, maximized: bool):
        if maximized:
            self.showMaximized()
        else:
            self.showNormal()
        try:
            self.titleBar.maxBtn.setMaxState(self.isMaximized())
        except Exception:
            pass
        self.update()

    def _verify_max_state(self, target_max: bool, generation=None):
        try:
            if generation is not None and generation != getattr(self, '_max_toggle_generation', 0):
                # 后续又发生了新的切换，旧校验不得推翻新的意图（快速连点竞态）。
                return
            if self.isMaximized() != target_max:
                self._apply_max_state(target_max)
                if self.isMaximized() != target_max:
                    self._force_max_state_win32(target_max)
        except Exception:
            pass

    def _release_titlebar_mouse(self):
        """点击最大化后主动释放左键状态（对齐 qframelesswindow 原处理，防后续点击被吞）。"""
        try:
            if sys.platform == 'win32':
                from qframelesswindow.utils.win32_utils import releaseMouseLeftButton
                releaseMouseLeftButton(self.winId())
        except Exception:
            pass

    def _force_max_state_win32(self, maximized: bool):
        """Win32 兜底：绕过 Qt 状态直接用系统命令最大化/还原。"""
        try:
            if sys.platform != 'win32':
                return
            import ctypes
            sw = 3 if maximized else 9  # SW_MAXIMIZE / SW_RESTORE
            ctypes.windll.user32.ShowWindow(int(self.winId()), sw)
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
            from .materials import frame_line_color
            painter.setPen(frame_line_color())
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
        self.history_page.create_requested.connect(lambda: self.switchTo(self.workspace_page))
        self._nav_items = [
            self.addSubInterface(self.workspace_page, FIF.HOME, tr('工作台')),
            self.addSubInterface(self.history_page, FIF.HISTORY, tr('任务历史')),
            self.addSubInterface(self.settings_page, FIF.SETTING, tr('设置')),
        ]
        self.workspace_page.navigation_requested.connect(lambda route: self.switchTo({
            'workspace': self.workspace_page, 'history': self.history_page, 'settings': self.settings_page}[route]))
        self._about_item = self.navigationInterface.addItem(
            routeKey="about",
            icon=FIF.INFO,
            text=tr('关于'),
            onClick=self.show_about,
            selectable=False,
            position=NavigationItemPosition.BOTTOM,
        )

    def show_about(self) -> None:
        from PyQt5.QtWidgets import QLabel
        from .components.brand_widgets import logo_pixmap
        dialog = Dialog(tr('关于') + ' ' + APP_NAME, f'版本 v{APP_VERSION}\n' + tr('产品批处理 · 定时执行 · GitHub同步') + '\n' + tr('多模型调度 · 自动重试 · 并发生成 · 自动下载'), self)
        badge = QLabel()
        badge.setPixmap(logo_pixmap(56))
        badge.setFixedSize(56, 56)
        badge.setAlignment(Qt.AlignCenter)
        try:
            dialog.textLayout.insertWidget(0, badge, 0, Qt.AlignHCenter)
        except Exception:
            pass
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
