"""Fluent main window using the package's native navigation and title bar."""

from __future__ import annotations

import sys
import os
import threading
import time
import math

from PyQt5.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath
from PyQt5.QtCore import QAbstractEventDispatcher, QMargins, Qt, QTimer, QRect, QRectF, QEvent, QPointF, QSizeF
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import CaptionLabel, Dialog, FluentIcon as FIF, FluentWindow, InfoBar, NavigationItemPosition, Theme, setTheme, setThemeColor

from core.config_manager import ConfigManager
from core.crash_reporter import set_notify
from core.i18n import current_language, register_retranslate, set_language, translate_text, tr
from core.version import APP_NAME, APP_VERSION
from core.network_clock import NetworkClock
from core.scheduler import ScheduleEngine
from .components.custom_widgets import ensure_ui_font
from .assets import asset_path
from .components.studio_dialog import safe_delete_dialog
from .pages.history_page import HistoryPage
from .pages.ecommerce_page import EcommercePage
from .pages.image_generation_page import ImageGenerationPage
from .pages.settings_page import SettingsPage
from .pages.workspace_page import WorkspacePage
from .theme import style_controls, style_page, apply_palette
from .materials import ACCENT, paint_background, paint_window_tint, window_background_snapshot, legacy_background_snapshot, ui_font_family
from .motion import PageTransition
from .model_catalog_controller import ModelCatalogController
from .scaling import screen_factors, apply_interface_scale
from .tokens import px
from .responsive import CoalescedCall
from .window_resize import resize_hit_test
from .window_backdrop import WindowsBackdropApi, WindowBackdrop, WM_ENTERSIZEMOVE, WM_EXITSIZEMOVE


class MainWindow(FluentWindow):
    def __init__(self, config_manager: ConfigManager | None = None, network_time=True, model_sync=None) -> None:
        app = QApplication.instance()
        if app is not None and not getattr(app, '_yanlin_ui_font_initialized', False):
            ensure_ui_font()
            app.setFont(QFont(ui_font_family(), 9))
            app._yanlin_ui_font_initialized = True
        apply_palette(app)
        setTheme(Theme.DARK, save=False)
        setThemeColor(QColor(ACCENT), save=False)
        super().__init__()
        self._dpi_window_handle = None
        self._dpi_screen = None
        self._geometry_ready = False
        self._restoring_geometry = False
        self._restore_maximized = False
        # 自绘圆角窗口：无边框 + 透明外圈，在 Win10 上也能获得一致的 12px 圆角与高光描边。
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._window_margin = 8
        self._window_radius = 12
        self._frame_margin = None
        self._fullscreen_restore_geometry = None
        self._fullscreen_restore_maximized = False
        self._glass_material = 'gradient'
        self._drag_background = None
        self.setMicaEffectEnabled(False)
        self.setCustomBackgroundColor("#0A0B12", "#0A0B12")
        self._backdrop = WindowBackdrop(int(self.winId()), WindowsBackdropApi(
            available=QApplication.platformName() == 'windows'))
        self.config_manager = config_manager or ConfigManager(migrate=True)
        self.config_manager.load_config()
        # 语言须在界面构建前生效：否则构建期 tr() 拿不到配置语言（重启后英文也显示中文）。
        set_language(self.config_manager.config.get('appearance', {}).get('language', '简体中文'))
        # 记录最近一次已完整重翻的语言，避免 apply_appearance 与语言总线重复重翻。
        self._last_retranslated_language = None
        self.model_catalog = ModelCatalogController(
            self.config_manager, lambda *args: self.workspace_page.append_log(*args), self,
            auto_sync=network_time if model_sync is None else model_sync)
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(asset_path('app.ico'))))
        self.resize(1400, 900)
        self.setMinimumSize(960, 600)
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
        if getattr(self.config_manager, 'load_error', ''):
            self.workspace_page.append_log(self.config_manager.load_error, 'warning')
        self._closing = False
        self._responsive_resize = CoalescedCall(self, self._apply_responsive_window, 40)
        self._material_repaint = CoalescedCall(self, self._repaint_material, 32)
        self._geometry_save = CoalescedCall(self, self._save_window_geometry, 500)
        self._move_resize_dispatcher = QAbstractEventDispatcher.instance(self.thread())
        if self._move_resize_dispatcher is not None:
            self._move_resize_dispatcher.aboutToBlock.connect(self._recover_drag_on_idle)
        self._close_timer = QTimer(self)
        self._close_timer.setInterval(100)
        self._close_timer.timeout.connect(self._finish_close)
        self._setup_schedule(network_time)
        for page in (self.workspace_page, self.ecommerce_page, self.history_page, self.settings_page):
            style_page(page)
        self._updateStackedBackground()
        style_controls(self)
        self.model_catalog.changed.connect(self.workspace_page.refresh_catalog)
        self.model_catalog.changed.connect(self.settings_page.refresh_catalog)
        self.model_catalog.changed.connect(self.ecommerce_page.refresh_model_pickers)
        self.model_catalog.state_changed.connect(self.settings_page.refresh_sync_state)
        self.model_catalog.sync_finished.connect(self.settings_page.model_sync_finished)
        self.model_catalog.sync_finished.connect(lambda ok, count, message: self.workspace_page.set_api_connection(ok, message))
        self.model_catalog.state_changed.connect(self.workspace_page.refresh_api_state)
        self.settings_page.sync_models_button.clicked.connect(self.model_catalog.refresh)
        self.settings_page.base_url.textChanged.connect(self.model_catalog.credentials_changed)
        self.settings_page.api_key.textChanged.connect(self.model_catalog.credentials_changed)
        self.settings_page.api_credentials_changed.connect(self.model_catalog.credentials_changed)
        self.settings_page.api_credentials_changed.connect(self.ecommerce_page.refresh_effective_lines)
        self.settings_page.api_credentials_changed.connect(self.ecommerce_page.refresh_line_routes)
        self.settings_page.api_credentials_changed.connect(self.ecommerce_page.refresh_model_pickers)
        self.settings_page.base_url.textChanged.connect(self.ecommerce_page.refresh_effective_lines)
        self.settings_page.api_key.textChanged.connect(self.ecommerce_page.refresh_effective_lines)
        self.ecommerce_page.line_changed.connect(self.settings_page._refresh_routing_options)
        self.settings_page.detection_changed.connect(self.workspace_page.scan_sources)
        self.settings_page.appearance_changed.connect(self.apply_appearance)
        self.settings_page.generation_defaults_changed.connect(self._settings_generation_changed)
        self.workspace_page.params_card.values_changed.connect(self._workspace_generation_changed)
        QTimer.singleShot(0, self.model_catalog.start)
        QTimer.singleShot(0, self._sync_window_frame)
        QTimer.singleShot(5000, self._check_updates)
        set_notify(self._uncaught_exception)
        self._announce_license()
        # Apply the saved scale before choosing the first visible geometry.
        self.apply_appearance()
        self._restore_window_geometry()
        # 登记到语言总线：设置里切语言（set_language 广播）时整窗重翻。
        register_retranslate(self)

    @staticmethod
    def _bounded_geometry(rect, available, minimum):
        """将窗口约束在 Qt 提供的可用工作区内，兼容负坐标多屏。"""
        width = max(minimum.width(), min(available.width(), rect.width()))
        height = max(minimum.height(), min(available.height(), rect.height()))
        # 小于最低布局尺寸的远程桌面仍须保留最小尺寸，并将溢出均分到两侧。
        x = (available.x() + (available.width() - width) // 2 if width > available.width()
             else max(available.left(), min(rect.x(), available.right() - width + 1)))
        y = (available.y() + (available.height() - height) // 2 if height > available.height()
             else max(available.top(), min(rect.y(), available.bottom() - height + 1)))
        return QRect(x, y, width, height)

    def _restore_window_geometry(self):
        """首帧前恢复正常窗口位置；副屏已拔除时回到主屏中央。"""
        app = QApplication.instance()
        screens = app.screens()
        primary = app.primaryScreen()
        if primary is None:
            return
        available = primary.availableGeometry()
        saved = self.config_manager.config.get('appearance', {}).get('window_geometry')
        self._restoring_geometry = True
        try:
            rect = None
            if isinstance(saved, dict):
                try:
                    values = [saved[key] for key in ('x', 'y', 'w', 'h')]
                    if not all(isinstance(value, int) and not isinstance(value, bool) for value in values):
                        raise ValueError('invalid geometry')
                    candidate = QRect(*values)
                    if not candidate.isValid():
                        raise ValueError('invalid geometry')
                    candidate.setWidth(max(960, candidate.width()))
                    candidate.setHeight(max(600, candidate.height()))
                    matches = [(screen, candidate.intersected(screen.availableGeometry())) for screen in screens]
                    matches = [(screen, intersection) for screen, intersection in matches
                               if intersection.width() >= 120 and intersection.height() >= 80]
                    if matches:
                        screen, _intersection = max(matches, key=lambda pair: pair[1].width() * pair[1].height())
                        available = screen.availableGeometry()
                        rect = self._bounded_geometry(candidate, available, self.minimumSize())
                        self._restore_maximized = saved.get('maximized') == 1
                except (KeyError, TypeError, ValueError, OverflowError):
                    pass
            if rect is None:
                rect = QRect(0, 0, min(1400, available.width()), min(900, available.height()))
                rect.moveCenter(available.center())
                self._restore_maximized = False
            self.setGeometry(self._bounded_geometry(rect, available, self.minimumSize()))
        finally:
            self._restoring_geometry = False
            self._geometry_ready = True

    def _save_window_geometry(self):
        """仅保存正常几何与最大化标记，不记录最小化时的坐标。"""
        if not self._geometry_ready or self._restoring_geometry or self.isMinimized():
            return
        try:
            maximized = self.isMaximized()
            rect = self.normalGeometry() if maximized else self.geometry()
            if not rect.isValid():
                rect = self.geometry()
            value = dict(x=int(rect.x()), y=int(rect.y()), w=int(rect.width()), h=int(rect.height()),
                         maximized=int(maximized))
            if self.config_manager.config.get('appearance', {}).get('window_geometry') != value:
                self.config_manager.update(('appearance', 'window_geometry'), value)
        except Exception:
            # Geometry persistence must never prevent a normal or forced shutdown.
            pass

    def _schedule_geometry_save(self):
        save = getattr(self, '_geometry_save', None)
        if (save is not None and self._geometry_ready and not self._restoring_geometry
                and self.isVisible() and not getattr(self, '_closing', False)):
            save.timer.start()

    def on_interface_scale_committed(self, old_scale, new_scale):
        """在冻结更新期间围绕窗口中心调整大小，再约束到当前屏幕。"""
        self._sync_nav_pane(self.navigationInterface.panel.isCollapsed())
        if (not self.isVisible() or self.isMaximized() or self.isFullScreen()
                or not all(isinstance(value, (int, float)) and math.isfinite(value) and value > 0
                           for value in (old_scale, new_scale)) or old_scale == new_scale):
            return
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        rect = self.geometry()
        ratio = new_scale / old_scale
        scaled = QRect(0, 0, round(rect.width() * ratio), round(rect.height() * ratio))
        scaled.moveCenter(rect.center())
        self.setGeometry(self._bounded_geometry(scaled, available, self.minimumSize()))

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
            InfoBar.info(tr('发现新版本'), tr('v{version} 已发布（当前 v{current}）').format(
                version=result['version'], current=APP_VERSION), parent=self, duration=8000)

        self.workspace_page.jobs.start(lambda: check_for_update(url), done, lambda message: None)

    def _retranslate_chrome(self):
        """语言切换即时生效：导航项 / 关于入口 / 菜单按钮提示。"""
        for item, text in zip(getattr(self, '_nav_items', []), [tr('工作台'), tr('图片生成'), tr('电商图'), tr('任务历史'), tr('设置')]):
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
        try:
            self.navigationInterface.panel.menuButton.setToolTip(tr('展开 / 收起导航'))
        except Exception:
            pass
        try:
            avatar = getattr(self, 'user_avatar', None)
            if avatar is not None:
                avatar.setToolTip(tr('当前用户 · 版本信息'))
        except Exception:
            pass

    def _retranslate_pages(self):
        """遍历各页面与关于入口，调用其 retranslate_ui()（有则调，异常兜底）。"""
        for name in ('workspace_page', 'image_generation_page', 'ecommerce_page', 'history_page', 'settings_page'):
            page = getattr(self, name, None)
            handler = getattr(page, 'retranslate_ui', None)
            if not callable(handler):
                continue
            try:
                handler()
            except Exception:
                # 单个页面重翻失败不得影响其它页面的语言切换。
                pass

    def _retranslate_dynamic_chrome(self):
        """重翻由主窗口持有的动态文案：定时状态、网络校时、下一个定时提示。"""
        try:
            engine = getattr(self, 'schedule_engine', None)
            label = getattr(self, 'next_schedule_label', None)
            if engine is not None and label is not None:
                label.setText(translate_text(engine.next_text()))
        except Exception:
            pass
        try:
            settings = getattr(self, 'settings_page', None)
            clock = getattr(self, 'clock', None)
            if settings is not None and clock is not None:
                settings.update_schedule_state(clock.now(), clock.status)
        except Exception:
            pass
        try:
            from core.version import APP_NAME as _name
            title_label = getattr(self, 'titleLabel', None)
            if title_label is not None:
                title_label.setText(_name)
            self.setWindowTitle(_name)
        except Exception:
            pass

    def _retranslate_all(self):
        """语言切换全量应用：先外壳，再各页面，最后刷新窗口标题与动态状态。"""
        if getattr(self, '_retranslating_ui', False):
            return
        self._retranslating_ui = True
        try:
            self._retranslate_chrome()
            self._retranslate_pages()
            self._retranslate_dynamic_chrome()
            self._last_retranslated_language = current_language()
        finally:
            self._retranslating_ui = False

    def retranslate_ui(self):
        """语言总线回调：整窗（导航 / 页面 / 标题）按当前语言重翻，幂等。"""
        self._retranslate_all()

    def _announce_license(self):
        """启动公告许可状态；首次运行写入试用起点。"""
        from core.licensing import license_status
        status = license_status(self.config_manager.config)
        try:
            self.config_manager.save_config()
        except Exception:
            pass
        detail = translate_text(status['detail'])
        self.workspace_page.append_log('许可状态：' + detail, 'info')
        if status['state'] in {'expired', 'invalid'}:
            InfoBar.warning(tr('许可提示'), detail, parent=self, duration=8000)

    def _settings_generation_changed(self):
        if getattr(self.settings_page, '_applying_saved', False):
            return
        page = self.workspace_page
        previous = page.params_card.model.currentText()
        page.params_card.apply_saved_workspace()
        page._refresh_row_parameters()
        if page.params_card.model.currentText() != previous:
            page.scan_sources()

    def _workspace_generation_changed(self, key, _value):
        if getattr(self.workspace_page.params_card, '_applying_saved', False):
            return
        if key in ('model', 'aspect_ratio', 'resolution', 'duration', 'poll_interval'):
            self.settings_page.apply_saved_generation_defaults()

    def apply_appearance(self):
        """应用外观设置（主题 / 高斯模糊 / 语言），切换立即生效；主题未变化时不重复全量刷新。"""
        config = self.settings_page.config_manager.config.get('appearance', {})
        set_language(config.get('language', '简体中文'))
        # 语言实际变化时由语言总线（MainWindow 已登记）同步重翻；若本次未变化
        # （首次构建 / 仅主题刷新）则在这里补一次幂等重翻，保证界面文案与配置一致。
        if getattr(self, '_last_retranslated_language', None) != current_language():
            self._retranslate_all()
        from .motion import set_reduced_motion
        set_reduced_motion(bool(config.get('reduce_motion', False)))
        from .tokens import MOTION
        from PyQt5.QtCore import QEasingCurve
        animation = self.navigationInterface.panel.expandAni
        animation.setDuration(0 if config.get('reduce_motion', False) else MOTION['base'])
        animation.setEasingCurve(QEasingCurve.OutCubic)
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
        self._apply_interface_scale()
        self._apply_blur_setting(config)

    def _apply_interface_scale(self):
        if getattr(self, '_closing', False):
            return
        app = QApplication.instance()
        screen = self._dpi_screen or app.primaryScreen()
        factors = apply_interface_scale(app, screen, self.config_manager.config.get('appearance', {}).get('ui_scale', 'auto'))
        self._interface_factors = factors
        self.settings_page.update_interface_scale_status(factors)
        panel = self.navigationInterface.panel
        self._sync_nav_pane(panel.isCollapsed())
        self._frame_margin = None
        self._sync_window_frame()
        responsive = getattr(self, '_responsive_resize', None)
        if responsive is not None:
            responsive.schedule()

    def _apply_nav_style(self):
        """导航栏底色跟随当前主题。"""
        from qfluentwidgets import setCustomStyleSheet
        from . import materials
        style = 'NavigationPanel {background:%s; border:0;}' % materials.nav_background()
        setCustomStyleSheet(self.navigationInterface.panel, style, style)

    def _apply_blur_setting(self, config=None):
        if getattr(self, '_closing', False):
            return
        config = config if config is not None else self.settings_page.config_manager.config.get('appearance', {})
        enabled = bool(config.get('blur', False))
        from . import materials
        self._backdrop.bind(int(self.winId()))
        previous = self._backdrop.backend
        if self._backdrop.configure(enabled, materials.acrylic_tint(), not materials.is_light()):
            self._glass_material = self._backdrop.backend
            if not self._backdrop.dragging:
                self._present_backdrop()
            if previous != self._backdrop.backend:
                self.workspace_page.append_log(f'窗口玻璃后端：{self._backdrop.backend}（Windows build {self._backdrop.api.build}）', 'info')

    def _interactive_move_resize(self, entering):
        backdrop = getattr(self, '_backdrop', None)
        if backdrop is None or getattr(self, '_closing', False):
            return
        previous = backdrop._applied
        changed = backdrop.begin_drag(self._prepare_drag_background) if entering else backdrop.end_drag()
        if changed:
            self._glass_material = backdrop.backend
            if not entering:
                self._drag_background = None
                if backdrop.backend == 'dwm' and backdrop.paint_mode == 'clear' and previous == backdrop._applied:
                    self._material_repaint.schedule()
                else:
                    self._present_backdrop()

    def _present_backdrop(self):
        self._material_repaint.timer.stop()
        if self.isVisible():
            self.repaint()
            self._backdrop.api.redraw(self._backdrop.hwnd)
        else:
            self.update()

    def _prepare_drag_background(self):
        self._material_repaint.timer.stop()
        if self._backdrop.backend == 'legacy':
            self._drag_background = legacy_background_snapshot(self)
        self.repaint()

    def _recover_drag_on_idle(self):
        if not self._closing and self._backdrop.dragging:
            if self._backdrop.api.is_move_resize_active(self._backdrop.hwnd) is False:
                self._interactive_move_resize(False)

    def _repaint_material(self):
        if not self._closing:
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
        routes = [self.workspace_page.objectName(), self.ecommerce_page.objectName(), self.history_page.objectName(),
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
            # 指示条只由原生 setIndicatorColor 绘制；这里只校正其几何：
            # 44px 导航行内垂直居中，并随界面缩放（原生硬编码 10/16 不随缩放）。
            for target in (item, inner):
                target.indicatorRect = lambda target=target: self._rail_indicator_rect(target)
            self._guard_leaf_nav_item(item, inner)
            # 点击后延迟一拍校正尺寸：吸收原生在两态切换边缘的任何小动作（幂等）。
            item.clicked.connect(lambda *args, _self=self: QTimer.singleShot(300, _self._settle_nav_pane))
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
                def _restore_expanded_pane():
                    try:
                        panel = self.navigationInterface.panel
                        if panel.isCollapsed():
                            self._toggle_navigation()
                        else:
                            self._sync_nav_pane(False)
                    except Exception:
                        pass
                QTimer.singleShot(120, _restore_expanded_pane)
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
            self.titleBar.setDoubleClickEnabled(False)
            self.titleBar.mouseDoubleClickEvent = self._studio_titlebar_double_click
        except Exception:
            pass
        # 关于入口由底部头像承担，导航栏保持纯图标三入口。
        try:
            self.navigationInterface.widget('about').hide()
        except Exception:
            pass
        # 标题栏拖拽误触发加固：点击手抖不再演变为“窗口漂移”。
        self._harden_titlebar_drag()
        # 真实屏幕自愈：窗口在后台/被遮挡期间绘制可能被系统节流，导航区
        # 只在屏幕上残留空白（PrintWindow 自检看不到这类问题）。守卫保证
        # 激活/恢复/切页/低频都会补一次重绘，导航内容任何情况下自动回来。
        self._install_repaint_guard()

    def _install_repaint_guard(self) -> None:
        guard = QTimer(self)
        guard.setInterval(6000)
        guard.timeout.connect(self._integrity_repaint)
        guard.start()
        self._repaint_guard = guard
        try:
            self.stackedWidget.currentChanged.connect(lambda *_: QTimer.singleShot(160, self._integrity_repaint))
        except Exception:
            pass

    def _integrity_repaint(self) -> None:
        try:
            if not self.isVisible() or self.isMinimized():
                return
            panel = self.navigationInterface.panel
            panel.update()
            for item, inner, _original in getattr(self, '_nav_rail_items', []):
                try:
                    inner.update()
                except Exception:
                    pass
            # 页面栈 / 当前页 / 标题栏同样可能被系统节流留下残影：一并补绘。
            try:
                page = self.stackedWidget.currentWidget()
                if page is not None:
                    page.update()
            except Exception:
                pass
            try:
                self.titleBar.update()
            except Exception:
                pass
            try:
                from PyQt5.QtWidgets import QApplication
                modal = QApplication.activeModalWidget()
                if modal is not None:
                    modal.update()
            except Exception:
                pass
            self.update()
        except Exception:
            pass

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
        self._integrity_repaint()

    def _on_nav_display_mode_changed(self, mode=None) -> None:
        # 原生在发出信号后还会执行 setCompacted（会重置尺寸），延后一拍再同步。
        QTimer.singleShot(0, self._settle_nav_pane)

    def _sync_nav_pane(self, collapsed: bool) -> None:
        """按折叠/展开状态同步导航尺寸：折叠 64px 固定栏；展开释放宽度并还原内边距。"""
        from . import tokens as design
        panel = self.navigationInterface.panel
        nav_height = self.navigationInterface.height()
        if nav_height > 0 and panel.height() != nav_height:
            # InterfaceScaler restores captured min/max sizes; the navigation
            # panel is parent-sized by QFluentWidgets and must track its rail.
            panel.setFixedHeight(nav_height)
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
                    item.setMinimumSize(0, px(44)); item.setMaximumSize(px(56), px(44))
                    inner.setMinimumSize(0, px(44)); inner.setMaximumSize(px(56), px(44))
                    inner._margins = lambda original=original: self._rail_margins(original())
                else:
                    item.setMinimumSize(0, px(44)); item.setMaximumSize(16777215, px(44))
                    inner.setMinimumSize(0, px(44)); inner.setMaximumSize(16777215, px(44))
                    inner._margins = original
                inner.updateGeometry()
            except Exception:
                pass
        try:
            panel.menuButton.setToolTip(tr('展开 / 收起导航'))
        except Exception:
            pass
        try:
            brand = getattr(self, 'brand_logo', None)
            if brand is not None and hasattr(brand, 'set_expanded'):
                brand.set_expanded(not collapsed)
                expanded_width = max(design.NAV_EXPAND_WIDTH, brand.expanded_width() + px(16))
                if panel.expandWidth != expanded_width:
                    panel.setExpandWidth(expanded_width)
                    if not collapsed and not panel.isCollapsed():
                        panel.expandAni.stop()
                        panel.expand(useAni=False)
        except Exception:
            pass
        try:
            title_bar = getattr(self, 'titleBar', None)
            layout = getattr(title_bar, 'hBoxLayout', None) or (title_bar.layout() if title_bar is not None else None)
            if layout is not None:
                want = self.windowTitle()
                for index in range(layout.count()):
                    item = layout.itemAt(index)
                    widget = item.widget() if item is not None else None
                    if widget is not None and hasattr(widget, 'text') and widget.text() == want:
                        widget.setVisible(bool(collapsed))
                        break
        except Exception:
            pass

    @staticmethod
    def _decorate_rail_item(inner) -> None:
        """Selected / hover chip background only.

        The accent indicator bar is drawn natively from ``setIndicatorColor``
        (see ``_rail_indicator_rect``); painting it here as well produced the
        duplicated double-bar the user reported after switching pages.
        """
        original = inner.paintEvent

        def paint(event, inner=inner, original=original):
            if inner.isSelected or inner.isEnter:
                painter = QPainter(inner)
                painter.setRenderHint(QPainter.Antialiasing)
                painter.setPen(Qt.NoPen)
                from .materials import ink
                painter.setBrush(ink(12))
                painter.drawRoundedRect(QRectF(inner.rect()), px(8), px(8))
                painter.end()
            original(event)

        inner.paintEvent = paint

    @staticmethod
    def _rail_indicator_rect(widget) -> QRectF:
        """Native indicator geometry: scaled with the interface and centred in the 44px row."""
        margins = widget._margins()
        height = max(2, px(16))
        width = max(1, px(3))
        return QRectF(float(margins.left()), (widget.height() - height) / 2.0,
                      float(width), float(height))

    @staticmethod
    def _guard_leaf_nav_item(item, inner) -> None:
        """叶子导航项防御：禁用 QFluentWidgets 的展开/收起尺寸动画。

        本主题下树节点 sizeHint 宽度为 0；展开态点击会触发 setExpanded 的几何动画，
        将节点收缩到 0 宽——被点击的导航项随即从界面消失（实测复现“点击后就消失了”）。
        三个导航项均为叶子节点、没有可展开的子列表：仅保留展开状态与箭头动画。
        """
        original = item.setExpanded

        def safe_set_expanded(is_expanded, ani=False, _item=item, _inner=inner, _orig=original):
            if _item.isLeaf():
                _item.isExpanded = bool(is_expanded)
                try:
                    _inner.setExpanded(bool(is_expanded))
                except Exception:
                    pass
                return
            return _orig(is_expanded, ani)

        item.setExpanded = safe_set_expanded

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
        return QMargins(margins.left() + px(8), margins.top(), margins.right(), margins.bottom())

    def _apply_windows_material(self) -> str:
        """Apply the configured native backdrop without opaque material overdraw."""
        self._apply_blur_setting()
        return self._glass_material

    def _window_margins_px(self):
        state = self.windowState()
        if state & Qt.WindowFullScreen:
            return 0
        if state & Qt.WindowMaximized:
            return 0
        return px(self._window_margin)

    def _native_window_is_maximized(self) -> bool:
        if sys.platform != 'win32':
            return False
        try:
            from qframelesswindow.utils.win32_utils import isMaximized
            return bool(isMaximized(self.winId()))
        except Exception:
            return False

    def _refresh_native_window_frame(self) -> None:
        """让无边框窗口在状态切换后重新计算 Win32 非客户区。"""
        if sys.platform != 'win32':
            return
        try:
            import ctypes
            import win32con
            import win32gui

            hwnd = int(self.winId())
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            if getattr(self, '_isResizeEnabled', True) and not style & win32con.WS_THICKFRAME:
                win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style | win32con.WS_THICKFRAME)
            flags = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, flags)
        except Exception:
            pass

    def _resize_hit_test(self, point, size=None):
        if (getattr(self, '_closing', False) or not getattr(self, '_isResizeEnabled', True)
                or self.windowState() & (Qt.WindowMaximized | Qt.WindowFullScreen | Qt.WindowMinimized)):
            return 0
        return resize_hit_test(point, self.size() if size is None else size)

    def nativeEvent(self, event_type, message):
        if sys.platform == 'win32':
            from ctypes import c_short
            from ctypes.wintypes import MSG
            import win32con
            import win32gui

            native_message = MSG.from_address(int(message))
            if native_message.hWnd and native_message.message in (WM_ENTERSIZEMOVE, WM_EXITSIZEMOVE):
                self._interactive_move_resize(native_message.message == WM_ENTERSIZEMOVE)
            if native_message.hWnd and native_message.message == win32con.WM_NCHITTEST:
                screen_point = (
                    c_short(native_message.lParam & 0xffff).value,
                    c_short((native_message.lParam >> 16) & 0xffff).value,
                )
                client_point = win32gui.ScreenToClient(native_message.hWnd, screen_point)
                client_rect = win32gui.GetClientRect(native_message.hWnd)
                ratio = self.devicePixelRatioF()
                point = QPointF(client_point[0] / ratio, client_point[1] / ratio)
                size = QSizeF((client_rect[2] - client_rect[0]) / ratio,
                             (client_rect[3] - client_rect[1]) / ratio)
                hit = self._resize_hit_test(point, size)
                if hit:
                    return True, hit
        result = super().nativeEvent(event_type, message)
        if (sys.platform == 'win32' and native_message.message == win32con.WM_NCCALCSIZE
                and result == (True, win32con.WVR_REDRAW)):
            return True, 0
        return result

    def _sync_window_state_controls(self) -> None:
        try:
            state = self.windowState()
            maximized = (
                bool(state & Qt.WindowFullScreen)
                or bool(state & Qt.WindowMaximized)
            )
            self.titleBar.maxBtn.setMaxState(maximized)
        except Exception:
            pass

    def _sync_window_frame(self, native=True) -> None:
        """把标题栏与内容同步到圆角内缩区，并在最大化时放大到整屏。"""
        margin = self._window_margins_px()
        if self._frame_margin != margin:
            self._frame_margin = margin
            try:
                self.hBoxLayout.setContentsMargins(margin, margin, margin, margin)
            except Exception:
                pass
        try:
            geometry = (margin, margin, max(0, self.width() - 2 * margin), self.titleBar.height())
            if self.titleBar.geometry().getRect() != geometry:
                self.titleBar.setGeometry(*geometry)
        except Exception:
            pass
        backdrop = getattr(self, '_backdrop', None)
        if native and not (backdrop is not None and backdrop.dragging):
            self._refresh_native_window_frame()
        if backdrop is not None and backdrop.enabled and backdrop.dragging:
            self._material_repaint.schedule()
        else:
            self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_window_frame(native=False)
        responsive = getattr(self, '_responsive_resize', None)
        if responsive is not None:
            responsive.schedule()
        self._schedule_geometry_save()

    def moveEvent(self, event):
        super().moveEvent(event)
        self._schedule_geometry_save()

    def _apply_responsive_window(self):
        """Keep the full navigation reachable without stealing narrow content width."""
        if getattr(self, '_closing', False):
            return
        try:
            narrow = self.width() < px(1160)
            panel = self.navigationInterface.panel
            if narrow and not panel.isCollapsed():
                panel.collapse()
                QTimer.singleShot(320, self._settle_nav_pane)
            self._sync_window_frame(native=False)
        except (AttributeError, RuntimeError):
            pass

    def showEvent(self, event):
        super().showEvent(event)
        if self._restore_maximized:
            self._restore_maximized = False
            self.showMaximized()
        self._connect_screen_signals()
        if hasattr(self, '_material_repaint'):
            self._apply_blur_setting()

    def event(self, event):
        if event.type() == QEvent.WinIdChange and hasattr(self, '_material_repaint'):
            QTimer.singleShot(0, self._apply_blur_setting)
        return super().event(event)

    def _connect_screen_signals(self):
        if getattr(self, '_closing', False):
            return
        handle = self.windowHandle()
        if handle is None or handle is self._dpi_window_handle:
            return
        self._disconnect_screen_signals()
        self._dpi_window_handle = handle
        handle.screenChanged.connect(self._on_screen_changed)
        self._on_screen_changed(handle.screen())

    def _disconnect_screen_signals(self):
        if self._dpi_screen is not None:
            try:
                self._dpi_screen.logicalDotsPerInchChanged.disconnect(self._on_screen_dpi_changed)
            except (TypeError, RuntimeError):
                pass
        if self._dpi_window_handle is not None:
            try:
                self._dpi_window_handle.screenChanged.disconnect(self._on_screen_changed)
            except (TypeError, RuntimeError):
                pass
        self._dpi_screen = None
        self._dpi_window_handle = None

    def _on_screen_changed(self, screen):
        if getattr(self, '_closing', False):
            return
        if screen is not self._dpi_screen:
            if self._dpi_screen is not None:
                try:
                    self._dpi_screen.logicalDotsPerInchChanged.disconnect(self._on_screen_dpi_changed)
                except (TypeError, RuntimeError):
                    pass
            self._dpi_screen = screen
            if screen is not None:
                screen.logicalDotsPerInchChanged.connect(self._on_screen_dpi_changed)
        self._log_screen_factors('screen-changed')
        self._apply_interface_scale()

    def _on_screen_dpi_changed(self, _dpi):
        self._log_screen_factors('logical-dpi-changed')
        self._apply_interface_scale()

    def _log_screen_factors(self, reason):
        """Record logical screen metrics independently of the extra interface scale."""
        if self._dpi_screen is None or getattr(self, '_closing', False):
            return
        factors = screen_factors(self._dpi_screen)
        self.workspace_page.append_log(
            f"Screen metrics ({reason}): {self._dpi_screen.name()} · dpi={factors['dpi']:.3f} · "
            f"logical={factors['logical_width']}x{factors['logical_height']} · "
            f"available={factors['available_width']}x{factors['available_height']}", 'info')

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            QTimer.singleShot(0, self._sync_window_frame)
            QTimer.singleShot(0, self._sync_window_state_controls)
            self._schedule_geometry_save()
        if event.type() in (QEvent.ActivationChange, QEvent.WindowStateChange, QEvent.Show):
            # 激活/恢复/显示后补一次重绘：修复“后台期间绘制节流”造成的屏幕残留空白。
            QTimer.singleShot(120, self._integrity_repaint)

    def _apply_titlebar_tweak(self):
        """Keep the title at its usual position with the brand's regular 14px font."""
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
            title_widget.setStyleSheet('font-size:14px; font-weight:400; background:transparent;')
            title_bar._studio_gap = True
        except Exception:
            pass

    def _studio_titlebar_double_click(self, event):
        if event.button() == Qt.LeftButton and self.titleBar.canDrag(event.pos()):
            self._studio_toggle_maximized()
            event.accept()
            return
        event.ignore()

    def _studio_toggle_maximized(self):
        """标题栏按钮切换全屏，并可靠恢复到进入全屏前的窗口状态。"""
        target_fullscreen = not self.isFullScreen()
        self._max_toggle_generation = getattr(self, '_max_toggle_generation', 0) + 1
        generation = self._max_toggle_generation
        self._apply_fullscreen_state(target_fullscreen)
        self._release_titlebar_mouse()
        QTimer.singleShot(160, lambda: self._verify_fullscreen_state(target_fullscreen, generation))

    def _apply_max_state(self, maximized: bool):
        """保留给外部调用方的 Qt 最大化兼容入口。"""
        if self.isFullScreen():
            self.showNormal()
        if maximized:
            self.showMaximized()
        else:
            self.showNormal()
        self._sync_window_state_controls()
        QTimer.singleShot(0, self._sync_window_frame)
        QTimer.singleShot(40, self._sync_window_frame)
        self.update()

    def _screen_geometry(self, fullscreen: bool):
        screen = self.windowHandle().screen() if self.windowHandle() is not None else QApplication.primaryScreen()
        if screen is None:
            return None
        return screen.geometry() if fullscreen else screen.availableGeometry()

    def _window_geometry_matches(self, fullscreen: bool) -> bool:
        target = self._screen_geometry(fullscreen)
        if target is None:
            return True
        actual = self.geometry()
        return (
            abs(actual.x() - target.x()) <= 2
            and abs(actual.y() - target.y()) <= 2
            and abs(actual.width() - target.width()) <= 2
            and abs(actual.height() - target.height()) <= 2
        )

    def _fullscreen_state_matches(self, target_fullscreen: bool) -> bool:
        if self.isFullScreen() != target_fullscreen:
            return False
        if target_fullscreen:
            return self._window_geometry_matches(True)
        return self.isMaximized() == bool(self._fullscreen_restore_maximized)

    def _apply_fullscreen_state(self, fullscreen: bool):
        if fullscreen:
            if not self.isFullScreen():
                self._fullscreen_restore_maximized = self.isMaximized()
                normal_geometry = self.normalGeometry()
                if normal_geometry.isValid():
                    self._fullscreen_restore_geometry = normal_geometry
                elif not self.isMaximized() and self.geometry().isValid():
                    self._fullscreen_restore_geometry = self.geometry()
            if self.isMaximized():
                self.showNormal()
            self.showFullScreen()
        else:
            restore_maximized = bool(self._fullscreen_restore_maximized)
            restore_geometry = self._fullscreen_restore_geometry
            self.showNormal()
            if restore_maximized:
                self.showMaximized()
            elif restore_geometry is not None and restore_geometry.isValid():
                self.setGeometry(restore_geometry)
        self._sync_window_state_controls()
        QTimer.singleShot(0, self._sync_window_frame)
        QTimer.singleShot(40, self._sync_window_frame)
        self.update()

    def _verify_fullscreen_state(self, target_fullscreen: bool, generation=None, attempt=0):
        try:
            if generation is not None and generation != getattr(self, '_max_toggle_generation', 0):
                return
            if self._fullscreen_state_matches(target_fullscreen):
                self._sync_window_state_controls()
                self._sync_window_frame()
                if not target_fullscreen:
                    self._fullscreen_restore_geometry = None
                    self._fullscreen_restore_maximized = False
                return
            if attempt < 2:
                self._apply_fullscreen_state(target_fullscreen)
                QTimer.singleShot(
                    100,
                    lambda: self._verify_fullscreen_state(target_fullscreen, generation, attempt + 1),
                )
                return
            if attempt >= 3:
                self._sync_window_state_controls()
                self._sync_window_frame()
                return
            if not target_fullscreen:
                self._force_max_state_win32(bool(self._fullscreen_restore_maximized))
            self._apply_fullscreen_state(target_fullscreen)
            QTimer.singleShot(
                100,
                lambda: self._verify_fullscreen_state(target_fullscreen, generation, attempt + 1),
            )
        except Exception:
            pass

    def _verify_max_state(self, target_max: bool, generation=None):
        try:
            if generation is not None and generation != getattr(self, '_max_toggle_generation', 0):
                # 后续又发生了新的切换，旧校验不得推翻新的意图（快速连点竞态）。
                return
            state_matches = (
                self.isMaximized() == target_max
                and not self.isFullScreen()
                and (not target_max or self._window_geometry_matches(False))
            )
            if not state_matches:
                self._apply_max_state(target_max)
                state_matches = (
                    self.isMaximized() == target_max
                    and not self.isFullScreen()
                    and (not target_max or self._window_geometry_matches(False))
                )
                if not state_matches:
                    self._force_max_state_win32(target_max)
                    QTimer.singleShot(0, self._sync_window_frame)
                    QTimer.singleShot(40, self._sync_window_frame)
                    QTimer.singleShot(120, lambda: self._verify_max_state(target_max, generation))
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
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        if margin:
            painter.fillRect(self.rect(), QColor(0, 0, 0, 1))
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        radius = px(self._window_radius) if margin else 0
        rect = QRectF(self.rect()).adjusted(margin, margin, -margin, -margin)
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.setClipPath(path)
        backdrop = getattr(self, '_backdrop', None)
        mode = backdrop.paint_mode if backdrop is not None else 'gradient'
        if mode == 'clear':
            painter.setCompositionMode(QPainter.CompositionMode_Source)
            painter.fillRect(rect, QColor(0, 0, 0, 1))
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        elif mode == 'tint':
            if self._drag_background is not None:
                painter.setRenderHint(QPainter.SmoothPixmapTransform)
                painter.setCompositionMode(QPainter.CompositionMode_Source)
                painter.drawPixmap(rect, self._drag_background, QRectF(self._drag_background.rect()))
                painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            else:
                paint_window_tint(painter, rect)
        elif backdrop is not None and backdrop.dragging:
            if self._drag_background is None:
                self._drag_background = window_background_snapshot(rect.size(), self.devicePixelRatioF())
            painter.drawPixmap(rect, self._drag_background, QRectF(self._drag_background.rect()))
        else:
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
        self.image_generation_page = ImageGenerationPage(self.config_manager)
        self.ecommerce_page = EcommercePage(self.config_manager)
        self.history_page = HistoryPage(self.workspace_page.append_log, self.config_manager)
        self.settings_page = SettingsPage(self.config_manager, self.workspace_page.append_log)
        self.settings_page.debug_mode_changed.connect(self.workspace_page.set_debug_mode)
        self.workspace_page.task_manager.pool_updated.connect(self.settings_page.update_pool_state)
        self.settings_page.task_settings_changed.connect(self.workspace_page.params_card.refresh_task_settings)
        self.workspace_page.params_card.values_changed.connect(self.settings_page.refresh_task_settings)
        self.settings_page.pool_enabled.checkedChanged.connect(self.workspace_page.params_card.refresh_pool_hint)
        self.settings_page.strategy.currentTextChanged.connect(self.workspace_page.params_card.refresh_pool_hint)
        self.workspace_page.history_changed.connect(self.history_page.update_history)
        self.image_generation_page.history_changed.connect(self.history_page.refresh_records)
        self.ecommerce_page.history_records_changed.connect(self.history_page.refresh_records)
        self.history_page.redownload_requested.connect(self.workspace_page.redownload)
        self.history_page.resolve_requested.connect(self.workspace_page.resolve_submission)
        self.history_page.batch_resolve_requested.connect(self.workspace_page.resolve_submissions)
        self.history_page.regenerate_requested.connect(self.workspace_page.regenerate)
        self.history_page.create_requested.connect(lambda: self.switchTo(self.workspace_page))
        self.history_page.delete_requested.connect(self.workspace_page.delete_history_records)
        self.history_page.view_ecommerce_group.connect(self._view_ecommerce_history_group)
        self.history_page.ecommerce_delete_requested.connect(self._delete_ecommerce_history_groups)
        self.history_page.clean_cache_requested.connect(self.workspace_page.clean_cache_now)
        self._nav_items = [
            self.addSubInterface(self.workspace_page, FIF.HOME, tr('工作台')),
            self.addSubInterface(self.image_generation_page, FIF.IMAGE_EXPORT, tr('图片生成')),
            self.addSubInterface(self.ecommerce_page, FIF.PHOTO, tr('电商图')),
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

    def _view_ecommerce_history_group(self, group_id):
        self.switchTo(self.ecommerce_page)
        self.ecommerce_page._view_history_group(group_id)

    def _delete_ecommerce_history_groups(self, group_ids):
        store = self.config_manager.ecommerce_store()
        for group_id in group_ids:
            store.delete_group(group_id)
        self.history_page.refresh_records()
        self.ecommerce_page._queue_history_refresh()

    def show_about(self) -> None:
        from PyQt5.QtWidgets import QLabel
        from .components.brand_widgets import logo_pixmap
        detail = (tr('版本 v{version}').format(version=APP_VERSION) + '\n'
                  + tr('产品批处理 · 定时执行 · GitHub同步') + '\n'
                  + tr('多模型调度 · 自动重试 · 并发生成 · 自动下载'))
        dialog = Dialog(tr('关于') + ' ' + APP_NAME, detail, self)
        dialog.setWindowIcon(self.windowIcon())
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
        self.next_schedule_label = CaptionLabel(translate_text(self.schedule_engine.next_text()))
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
            self.clock.status = tr('系统时间（未启用网络校时）')
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
        self.next_schedule_label.setText(translate_text(self.schedule_engine.next_text()))
        self.settings_page.update_schedule_state(self.clock.now(), self.clock.status)

    def _scheduled_finished(self, *_):
        scheduled, self._scheduled_batch = self._scheduled_batch, False
        tasks = self.workspace_page.task_manager.tasks
        if scheduled and tasks and self.config_manager.config['schedule']['after_finish'] == 'close' and not any(t.get('status') == 'cancelled' for t in tasks):
            self.workspace_page.append_log('定时队列全部结束，自动关闭软件', 'info')
            QTimer.singleShot(100, self.close)

    def _background_busy(self):
        return (self.workspace_page.task_manager.is_running or self.workspace_page.jobs.busy
                or self.workspace_page.history_jobs.busy or self.workspace_page.log_drawer.jobs.busy
                or self.history_page.jobs.busy
                or bool(self.workspace_page._history_pending)
                or self.settings_page.jobs.busy or self.model_catalog.jobs.busy
                or self.image_generation_page.has_running_workers())

    def _close_child_dialogs(self):
        """Reject all child dialogs so no modal loop can hold the app open."""
        workspace = getattr(self, 'workspace_page', None)
        if workspace is not None:
            workspace._close_child_dialogs()
        try:
            from PyQt5.QtWidgets import QDialog
            for widget in QApplication.topLevelWidgets():
                if isinstance(widget, QDialog) and widget is not self:
                    try:
                        widget.done(0)
                        widget.close()
                        safe_delete_dialog(widget)
                    except RuntimeError:
                        pass
        except Exception:
            pass

    def closeEvent(self, event):
        self._save_window_geometry()
        self._geometry_save.timer.stop()
        # Images already submitted may be billable: wait for their result/timeout.
        # Keep this guard separate from the existing workspace shutdown sequence.
        if self.ecommerce_page.busy:
            event.ignore()
            self.ecommerce_page.cancel_generation()
            if not getattr(self, '_ecommerce_close_pending', False):
                self._ecommerce_close_pending = True
                self.ecommerce_page.idle.connect(self.close)
            return
        self._disconnect_screen_signals()
        if self._move_resize_dispatcher is not None:
            try:
                self._move_resize_dispatcher.aboutToBlock.disconnect(self._recover_drag_on_idle)
            except (TypeError, RuntimeError):
                pass
            self._move_resize_dispatcher = None
        self._material_repaint.timer.stop()
        self._backdrop.close()
        if self._closing:
            self._close_child_dialogs()
            # 接受第二次关闭事件即可让主窗口正常退出。这里不能提前调用
            # QApplication.quit()，否则共享进程中的后续 QDialog.exec_()
            # 会立即返回；真实应用会在最后一个可见顶层窗口关闭后自动退出。
            event.accept()
            return
        self._close_child_dialogs()
        self.schedule_timer.stop()
        # Cancel image-generation workers before the stacked pages are torn down.
        self.image_generation_page.shutdown_workers()
        self.history_page.jobs.request_stop()
        self.settings_page.stop_timers()
        self.model_catalog.shutdown()
        if self._background_busy():
            event.ignore()
            if not self._closing:
                self._closing = True
                self.workspace_page.shutdown()
                self.settings_page.setEnabled(False)
                self.settings_page.setToolTip(tr('正在安全退出，等待后台任务完成后关闭'))
                self.workspace_page.append_log('正在安全退出，等待后台请求返回或超时...', 'warning')
                self._close_deadline = time.monotonic() + 3.0
                self._close_timer.start()
            return
        self.workspace_page.shutdown()
        super().closeEvent(event)

    def _finish_close(self):
        if not self._background_busy():
            self._close_timer.stop()
            self.close()
        elif time.monotonic() >= getattr(self, '_close_deadline', float('inf')):
            self._save_window_geometry()
            self._close_timer.stop()
            self.workspace_page.append_log('后台任务超过安全退出时限，强制退出应用', 'error')
            self.workspace_page.task_manager.force_stop()
            for jobs in (self.workspace_page.jobs, self.workspace_page.history_jobs,
                         self.history_page.jobs,
                         self.workspace_page.log_drawer.jobs, self.settings_page.jobs,
                         self.model_catalog.jobs):
                jobs.force_stop()
            QApplication.quit()
            _force_timer = threading.Timer(0.5, lambda: os._exit(0))
            _force_timer.daemon = True
            _force_timer.start()
