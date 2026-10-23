"""Fluent setting cards backed by the application's JSON config."""

from PyQt5.QtCore import Qt, QPoint, QSize, QEvent, pyqtSignal, QTime, QSignalBlocker, QTimer
from PyQt5.QtWidgets import QFileDialog, QHBoxLayout, QBoxLayout, QLayout, QProgressBar, QScrollArea, QVBoxLayout, QWidget, QPushButton, QSizePolicy, QStackedWidget
from ..responsive import CoalescedCall, ContentHeight, WrapLayout
from ..tokens import px
from qfluentwidgets import (
    CheckBox, ComboBox, ComboBoxSettingCard, FluentIcon as FIF, LineEdit,
    OptionsConfigItem, OptionsValidator, PushButton, SettingCard, SettingCardGroup,
    SegmentedWidget, SpinBox, SwitchSettingCard, CaptionLabel, TitleLabel,
    TransparentToolButton, ScrollArea, TimePicker, InfoBar, InfoBarIcon, InfoBarPosition,
)
from ..components.custom_widgets import make_card
from ..widgets.workspace_surface import label
from ..widgets.status_dot import Dot, dot_icon
from ..theme import SettingSurface, style_controls
from core.background import BackgroundJobs
from core.api_client import ApiClient
from core.image_uploader import ImageUploader
from core.model_parameters import MODELS
from core.repository_sync import RepositorySync, repository_secrets, default_repository_root, validate_repository_root
from core.version import APP_NAME, APP_VERSION
from core.licensing import license_status
from core.i18n import tr
from core.api_errors import classify_connection_error, classify_http_error
from core.line_routing import normalize_base_url, resolve_line
from core.line_probe import probe_line, probe_upload, looks_like_upload_token, valid_http_url
from core.model_catalog import classify_model, normalize_model_id
from ..components.model_options import apply_model_options
from ..components.model_selector import ModelComboBox, ModelPickComboBox, catalog_snapshot, usable
from datetime import datetime
import copy
import json
import math
import re
import sys
import time
from urllib.parse import urlsplit


class ResponsiveSettingCard:
    def _prepare_responsive_card(self):
        self._card_minimum_height = max(64, self.minimumHeight())
        heading = QWidget(self)
        heading_layout = QHBoxLayout(heading)
        heading_layout.setContentsMargins(0, 0, 0, 0)
        for _index in range(3):
            item = self.hBoxLayout.takeAt(0)
            if item.widget() is not None:
                heading_layout.addWidget(item.widget(), 0, item.alignment())
            elif item.layout() is not None:
                heading_layout.addLayout(item.layout())
            else:
                heading_layout.addItem(item)
        self._heading = heading
        self._card_label_texts = {}
        self._card_label_rendered = {}
        for text_label in (self.titleLabel, self.contentLabel):
            text_label.setWordWrap(False)
            text_label.setMinimumWidth(0)
            text_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self.vBoxLayout.setAlignment(text_label, Qt.Alignment())
            text_label.installEventFilter(self)
        heading.setMinimumWidth(0)
        heading.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        heading_layout.setStretchFactor(self.vBoxLayout, 1)
        heading.installEventFilter(self)
        controls = QWidget(self)
        controls_layout = WrapLayout(controls)
        while self.hBoxLayout.count():
            item = self.hBoxLayout.takeAt(0)
            if item.widget() is not None:
                controls_layout.addWidget(item.widget(), alignment=item.alignment())
            elif item.layout() is not None:
                controls_layout.addLayout(item.layout())
        self._responsive_controls = controls
        self.hBoxLayout.addWidget(heading, 1)
        self.hBoxLayout.addWidget(controls, 1)
        self._card_relayout = CoalescedCall(self, self._fit_responsive_card, 0)
        self._fit_responsive_card()

    def _elide_card_labels(self):
        for text_label in (self.titleLabel, self.contentLabel):
            text = text_label.text()
            if text != self._card_label_rendered.get(text_label):
                self._card_label_texts[text_label] = text
            full_text = self._card_label_texts.get(text_label, text)
            text_label.setToolTip(full_text)
            width = max(0, text_label.contentsRect().width() - px(2))
            rendered = text_label.fontMetrics().elidedText(full_text, Qt.ElideRight, width)
            self._card_label_rendered[text_label] = rendered
            if text != rendered:
                text_label.setText(rendered)

    def _fit_responsive_card(self):
        # Retain source strings even after setText() has rendered an ellipsis.
        for text_label in (self.titleLabel, self.contentLabel):
            if text_label.text() != self._card_label_rendered.get(text_label):
                self._card_label_texts[text_label] = text_label.text()
        empty_heading = not any(text.strip() for text in self._card_label_texts.values())
        self._heading.setVisible(not empty_heading)
        if empty_heading:
            self._heading.setFixedWidth(0)
        else:
            self._heading.setMaximumWidth(16777215)
        self.hBoxLayout.setStretchFactor(self._heading, 0 if empty_heading else 1)
        self.hBoxLayout.setStretchFactor(self._responsive_controls, 1)
        compact = self.width() < px(680)
        self.hBoxLayout.setDirection(QBoxLayout.TopToBottom if compact else QBoxLayout.LeftToRight)
        self.hBoxLayout.setContentsMargins(px(16), px(12), px(16), px(12))
        self.hBoxLayout.setSpacing(px(10))
        width = max(1, self.width() - px(32))
        controls_width = width if compact or empty_heading else max(1, (width - px(10)) // 2)
        heading_height = 0
        if not empty_heading:
            labels = [text_label for text_label in (self.titleLabel, self.contentLabel)
                      if not text_label.isHidden()]
            for text_label in labels:
                line_height = text_label.fontMetrics().lineSpacing() + 2 * text_label.margin()
                if text_label.minimumHeight() != line_height:
                    text_label.setMinimumHeight(line_height)
            heading_height = sum(text_label.minimumHeight() for text_label in labels)
            heading_height += max(0, len(labels) - 1) * self.vBoxLayout.spacing()
            if not self.iconLabel.isHidden():
                heading_height = max(heading_height, self.iconLabel.height())
        controls_height = self._responsive_controls.layout().heightForWidth(controls_width)
        body_height = (heading_height + controls_height + (px(10) if heading_height else 0)
                       if compact else max(heading_height, controls_height))
        height = max(px(self._card_minimum_height), body_height + px(24) + px(10))
        if self.height() != height or self.minimumHeight() != height or self.maximumHeight() != height:
            self.setFixedHeight(height)
        self.hBoxLayout.invalidate()
        self.hBoxLayout.activate()
        self._heading.layout().activate()
        self.vBoxLayout.activate()
        self._elide_card_labels()

    def eventFilter(self, watched, event):
        if hasattr(self, '_card_relayout') and event.type() in (QEvent.LayoutRequest, QEvent.FontChange):
            self._card_relayout.schedule()
        return super().eventFilter(watched, event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, '_card_relayout') and event.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._card_relayout.schedule()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, '_card_relayout'):
            self._card_relayout.schedule()

    def setTitle(self, title):
        super().setTitle(title)
        if hasattr(self, '_card_relayout'):
            self._card_label_texts[self.titleLabel] = title
            self._card_label_rendered.pop(self.titleLabel, None)
            self._card_relayout.schedule()

    def setContent(self, content):
        super().setContent(content)
        if hasattr(self, '_card_relayout'):
            self._card_label_texts[self.contentLabel] = content
            self._card_label_rendered.pop(self.contentLabel, None)
            self._card_relayout.schedule()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_card_relayout') and event.size().width() != event.oldSize().width():
            self._card_relayout.schedule()

    def refresh_interface_scale(self):
        self._fit_responsive_card()


class JsonComboBoxSettingCard(ResponsiveSettingCard, SettingSurface, ComboBoxSettingCard):
    """Use Fluent's combo card without its unrelated config/config.json writes."""
    def _onCurrentIndexChanged(self, index):
        self.configItem.value = self.comboBox.itemData(index)

    def setValue(self, value):
        if value in self.optionToText:
            self.comboBox.setCurrentText(self.optionToText[value])


class StudioSwitchSettingCard(ResponsiveSettingCard, SettingSurface, SwitchSettingCard):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._prepare_responsive_card()


class CustomSettingCard(ResponsiveSettingCard, SettingSurface, SettingCard):
    # 1.11.3 has no LineEditSettingCard or SpinBoxSettingCard: compose its
    # SettingCard with its LineEdit/SpinBox to retain the Fluent design.
    def __init__(self, title, control, icon=FIF.SETTING, parent=None):
        super().__init__(icon, title, parent=parent)
        control.setMinimumWidth(0)
        control.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.hBoxLayout.addWidget(control, 1, Qt.AlignVCenter)
        self.hBoxLayout.addSpacing(20)
        self.setMinimumHeight(64)
        self._prepare_responsive_card()


class RepositorySyncSettingCard(SettingSurface, SettingCard):
    def __init__(self, controls):
        super().__init__(FIF.SYNC, tr('GitHub 私人仓库同步'))
        self.controls = controls
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(0)
        self._fit_timer.timeout.connect(self.fit_content)
        self.vBoxLayout.addWidget(controls)
        self.vBoxLayout.setContentsMargins(0, 12, 20, 12)
        for index in range(self.hBoxLayout.count()):
            if self.hBoxLayout.itemAt(index).spacerItem() is not None:
                self.hBoxLayout.setStretch(index, 0)
        self.hBoxLayout.setStretchFactor(self.vBoxLayout, 1)
        self.fit_content()

    def fit_content(self):
        self.controls.setMinimumHeight(max(0, self.controls.heightForWidth(self.controls.width())))
        self.hBoxLayout.invalidate()
        self.setFixedHeight(max(160, self.hBoxLayout.totalHeightForWidth(self.width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width() and not self._fit_timer.isActive():
            self._fit_timer.start()


class _ResultInfoBar(InfoBar):
    """Wrap probe details within the visible window, including after resizing."""
    def _adjustText(self):
        parent = self.parentWidget()
        width = min(px(680), max(1, parent.width() - 48)) if parent else px(680)
        self.hBoxLayout.setSizeConstraint(QLayout.SetNoConstraint)
        self.textLayout.setSizeConstraint(QLayout.SetNoConstraint)
        self.titleLabel.setWordWrap(False)
        self.titleLabel.setText(self.title)
        self.contentLabel.setTextFormat(Qt.PlainText)
        self.contentLabel.setWordWrap(True)
        self.contentLabel.setMinimumWidth(0)
        self.contentLabel.setText(self.content)
        margins = self.hBoxLayout.contentsMargins()
        text_margins = self.textLayout.contentsMargins()
        reserved = (margins.left() + margins.right() + self.iconWidget.sizeHint().width()
                    + self.closeButton.width() + 12 + text_margins.left() + text_margins.right())
        text_width = max(1, width - reserved)
        self.titleLabel.setFixedWidth(text_width)
        self.contentLabel.setFixedWidth(text_width)
        self.setFixedWidth(width)
        self.hBoxLayout.invalidate()
        height = self.hBoxLayout.totalHeightForWidth(width)
        self.setFixedHeight(max(height, self.hBoxLayout.minimumSize().height()))
        self.hBoxLayout.activate()


class SettingsPage(QWidget):
    debug_mode_changed = pyqtSignal(bool)
    api_credentials_changed = pyqtSignal()
    schedule_changed = pyqtSignal()
    task_settings_changed = pyqtSignal()
    detection_changed = pyqtSignal()
    appearance_changed = pyqtSignal()
    generation_defaults_changed = pyqtSignal()

    class _SettingsScrollProxy:
        """滚动代理：保持旧接口（ensureWidgetVisible / verticalScrollBar / widget），
        并在目标控件不在当前页时自动切换到它所在的页。"""
        def __init__(self, page):
            self._page = page

        def ensureWidgetVisible(self, widget, x=0, y=0):
            key = self._page._page_key_for_widget(widget)
            if key is None:
                return
            try:
                self._page.page_stack.setCurrentIndex(self._page._page_order.index(key))
            except Exception:
                return
            area = self._page._pages[key][0]
            try:
                area.ensureWidgetVisible(widget, x, y)
            except Exception:
                pass

        def verticalScrollBar(self):
            try:
                key = self._page._current_page_key()
                return self._page._pages[key][0].verticalScrollBar()
            except Exception:
                return None

        def widget(self):
            try:
                key = self._page._current_page_key()
                return self._page._pages[key][1]
            except Exception:
                return None

    def __init__(self, config_manager, log_callback, parent=None):
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self.config_manager = config_manager
        self.log_callback = log_callback
        self._sanitize_upload_fields_once()
        self.jobs = BackgroundJobs(self)
        self.jobs.log_message.connect(self.log_callback)
        self.model_rows = []
        self.pool_rows = {'video': self.model_rows, 'chat': [], 'image': []}
        self.groups = []
        self._pages = {}
        self._page_order = []
        self._rail_buttons = {}
        self._page_buttons = {}
        self._station_nav_buttons = {}
        self._station_page_key_fields = {}
        self.page_stack = QStackedWidget()
        # —— 服务线路页 ——
        self.root = self._make_page('stations')
        self._page_title(self.root, '中转站', '每个中转站独立成一页；左侧导航可直达；「设为当前」后任务走该线路')
        self._build_stations()
        self.root.addStretch(1)
        # —— 新增中转站页（共用编辑器） ——
        self.root = self._make_page('station-new')
        self._page_title(self.root, '新增中转站', '填写后保存即可；也可从「中转站管理」一键保存当前 API 配置')
        self._build_station_editor_page()
        self.root.addStretch(1)
        # —— 设置各分类页 ——
        self.root = self._make_page('api')
        self._page_title(self.root, '当前线路', '任务提交使用此线路；切换中转站后自动同步')
        self._build_api(); self.root.addStretch(1)
        self.root = self._make_page('pool')
        self._page_title(self.root, '模型池', '多模型调度、故障转移与健康状态')
        self._build_pool(); self.root.addStretch(1)
        self.root = self._make_page('task')
        self._page_title(self.root, '任务策略', '重试、并发、未匹配策略与命名规则')
        self._build_task(); self.root.addStretch(1)
        self.root = self._make_page('defaults')
        self._page_title(self.root, '默认参数', '新任务的默认模型与生成参数')
        self._build_defaults(); self.root.addStretch(1)
        self.root = self._make_page('schedule')
        self._page_title(self.root, '定时执行', '按时间自动开始任务队列')
        self._build_schedule(); self.root.addStretch(1)
        self.root = self._make_page('appearance')
        self._page_title(self.root, '外观与语言', '主题、玻璃质感、背景与界面语言')
        self._build_appearance(); self.root.addStretch(1)
        self.root = self._make_page('license')
        self._page_title(self.root, '账号与许可', '激活状态、许可管理与版本信息')
        self._build_license_section(); self.root.addStretch(1)
        self.root = self._make_page('sync')
        self._page_title(self.root, 'GitHub 同步', '把代码改动同步到你的私人仓库')
        self._build_sync(); self.root.addStretch(1)
        self.scroll = self._SettingsScrollProxy(self)
        self._rail_buttons = {}
        self.rail = self._build_rail()
        shell = QHBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        self.rail_scroll = ScrollArea()
        self.rail_scroll.setWidgetResizable(True)
        self.rail_scroll.setFrameShape(QScrollArea.NoFrame)
        self.rail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.rail_scroll.setFixedWidth(196)
        self.rail_scroll.setWidget(self.rail)
        shell.addWidget(self.rail_scroll)
        shell.addWidget(self.page_stack, 1)
        self._switch_page('stations', highlight=True)
        self.pool_timer = QTimer(self)
        self.pool_timer.setInterval(1000)
        self.pool_timer.timeout.connect(self.refresh_pool_state)
        self.pool_timer.start()
        self.refresh_pool_state()

    # ------------------------------------------------------------------
    # 分页基建
    # ------------------------------------------------------------------
    def _make_page(self, key):
        content = QWidget(); content.setObjectName('settingsContent')
        layout = QVBoxLayout(content); layout.setContentsMargins(34, 26, 34, 42); layout.setSpacing(20)
        area = ScrollArea(); area.setWidgetResizable(True); area.setFrameShape(QScrollArea.NoFrame)
        area.setWidget(content)
        try:
            area.verticalScrollBar().valueChanged.connect(self._sync_rail_on_scroll)
        except Exception:
            pass
        self.page_stack.addWidget(area)
        self._pages[key] = (area, content)
        if key not in self._page_order:
            self._page_order.append(key)
        return layout

    @staticmethod
    def _page_title(layout, title, subtitle):
        layout.addWidget(TitleLabel(title))
        caption = CaptionLabel(subtitle)
        caption.setWordWrap(True)
        layout.addWidget(caption)

    def _current_page_key(self):
        index = self.page_stack.currentIndex()
        if 0 <= index < len(self._page_order):
            return self._page_order[index]
        return 'stations'

    def _switch_page(self, key, highlight=True):
        if key not in self._page_order:
            return
        self.page_stack.setCurrentIndex(self._page_order.index(key))
        if highlight:
            self._highlight_rail(key)

    def _highlight_rail(self, key):
        button = self._page_buttons.get(key)
        candidates = list(getattr(self, '_rail_buttons', {}).values())
        candidates += list(getattr(self, '_station_nav_buttons', {}).values())
        if hasattr(self, '_station_nav_add'):
            candidates.append(self._station_nav_add)
        for candidate in candidates:
            try:
                active = candidate is button
                candidate.setProperty('railActive', active)
                candidate.style().unpolish(candidate); candidate.style().polish(candidate)
            except Exception:
                pass

    def _page_key_for_widget(self, widget):
        w = widget
        while w is not None:
            for key, (_area, content) in self._pages.items():
                if w is content:
                    return key
            w = w.parentWidget()
        return None

    def _group(self, title):
        group = SettingCardGroup(title)
        group.cardLayout.setSpacing(24)
        self.root.addWidget(group)
        self.groups.append(group)
        return group

    # ------------------------------------------------------------------
    # 左侧分类导航（滚动锚点）
    # ------------------------------------------------------------------
    def _build_rail(self):
        rail = QWidget(); rail.setObjectName('settingsRail')
        rail.setStyleSheet(
            '#settingsRail {background:transparent; border-right:1px solid rgba(255,255,255,0.07);}'
            '#settingsRail QPushButton {border:0; border-radius:8px; text-align:left; padding:10px 14px;'
            ' color:#9CA3AF; font-size:13px; background:transparent;}'
            '#settingsRail QPushButton:hover {background:rgba(255,255,255,0.06); color:#E5E7EB;}'
            '#settingsRail QPushButton[railActive="true"] {background:rgba(91,141,239,0.14); color:#FFFFFF; font-weight:600;}')
        layout = QVBoxLayout(rail); layout.setContentsMargins(18, 24, 16, 28); layout.setSpacing(5)
        self._rail_buttons = {}
        self._page_buttons = {}

        def add_entry(text, key):
            button = QPushButton(text)
            button.setCursor(Qt.PointingHandCursor)
            button.setMinimumHeight(32)
            button.clicked.connect(lambda checked=False, k=key, b=button: self._jump_to_group(k, b))
            layout.addWidget(button)
            self._rail_buttons[text] = button
            self._page_buttons[key] = button
            return button

        def add_head(text):
            head = CaptionLabel(text)
            head.setStyleSheet('color:rgba(237,237,240,0.55); font-size:11px; letter-spacing:0.08em; padding-left:6px;')
            layout.addWidget(head); layout.addSpacing(4)

        # —— 账号与许可 ——
        add_head('账号与许可')
        add_entry('许可与激活', 'license')
        layout.addSpacing(16)
        # —— 外观 ——
        add_head('外观')
        add_entry('外观与语言', 'appearance')
        layout.addSpacing(16)
        # —— 模型与生成 ——
        add_head('模型与生成')
        add_entry('模型池', 'pool')
        add_entry('默认参数', 'defaults')
        layout.addSpacing(16)
        # —— 网络与数据：中转站管理 + 每站一条 + 当前线路 + GitHub 同步 ——
        add_head('网络与数据')
        add_entry('中转站管理', 'stations')
        self._station_nav_layout = QVBoxLayout(); self._station_nav_layout.setSpacing(4)
        layout.addLayout(self._station_nav_layout)
        self._station_nav_add = QPushButton('+ 新增中转站')
        self._station_nav_add.setObjectName('stationNavButton')
        self._station_nav_add.setCursor(Qt.PointingHandCursor)
        self._station_nav_add.setMinimumHeight(32)
        self._station_nav_add.clicked.connect(lambda: self._open_station_editor(None))
        layout.addWidget(self._station_nav_add)
        self._page_buttons['station-new'] = self._station_nav_add
        self._station_nav_buttons = {}
        self._sync_station_nav()
        add_entry('当前线路', 'api')
        add_entry('GitHub 同步', 'sync')
        layout.addSpacing(16)
        # —— 高级 ——
        add_head('高级')
        add_entry('任务策略', 'task')
        add_entry('定时执行', 'schedule')
        layout.addStretch(1)
        return rail

    def _jump_to_group(self, target, button=None):
        key = target if isinstance(target, str) else self._page_key_for_widget(target)
        if key is None or key not in self._page_order:
            return
        self.page_stack.setCurrentIndex(self._page_order.index(key))
        active_button = button or self._page_buttons.get(key)
        candidates = list(getattr(self, '_rail_buttons', {}).values())
        candidates += list(getattr(self, '_station_nav_buttons', {}).values())
        if hasattr(self, '_station_nav_add'):
            candidates.append(self._station_nav_add)
        for candidate in candidates:
            try:
                active = candidate is active_button
                candidate.setProperty('railActive', active)
                candidate.style().unpolish(candidate); candidate.style().polish(candidate)
            except Exception:
                pass

    def _sync_station_nav(self):
        layout = getattr(self, '_station_nav_layout', None)
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._station_nav_buttons = {}
        for key in [k for k in list(getattr(self, '_page_buttons', {})) if k.startswith('station:')]:
            self._page_buttons.pop(key, None)
        active = self.config_manager.config.get('stations_active', '')
        stations = self._stations()
        if not stations:
            hint = CaptionLabel('（暂无 · 点「+ 新增中转站」创建）')
            hint.setWordWrap(True)
            hint.setStyleSheet('color:rgba(237,237,240,0.30); font-size:11px; padding-left:8px;')
            layout.addWidget(hint)
            return
        for station in stations:
            sid = station.get('id')
            is_active = sid == active and bool(active)
            button = QPushButton(station.get('name') or '未命名')
            button.setIcon(dot_icon('#5B8DEF' if is_active else '#6B7280', 10, filled=is_active))
            button.setIconSize(QSize(10, 10))
            button.setObjectName('stationNavButton')
            button.setCursor(Qt.PointingHandCursor)
            button.setMinimumHeight(32)
            button.clicked.connect(lambda checked=False, s=sid, b=button: self._focus_station(s, b))
            layout.addWidget(button)
            self._station_nav_buttons[sid] = button
            self._page_buttons[f'station:{sid}'] = button

    def _focus_station(self, station_id, button=None):
        key = f'station:{station_id}'
        if key in self._page_order:
            self.page_stack.setCurrentIndex(self._page_order.index(key))
        active_button = button or self._page_buttons.get(key)
        candidates = list(getattr(self, '_rail_buttons', {}).values())
        candidates += list(getattr(self, '_station_nav_buttons', {}).values())
        if hasattr(self, '_station_nav_add'):
            candidates.append(self._station_nav_add)
        for candidate in candidates:
            try:
                active = candidate is active_button
                candidate.setProperty('railActive', active)
                candidate.style().unpolish(candidate); candidate.style().polish(candidate)
            except Exception:
                pass

    def _sync_rail_on_scroll(self, *_):
        try:
            key = self._current_page_key()
            button = self._page_buttons.get(key)
            if button is not None and not button.property('railActive'):
                self._highlight_rail(key)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 中转站分页（每站一页 + 新增页 + 总览卡）
    # ------------------------------------------------------------------
    def _render_station_pages(self):
        previous_root = getattr(self, 'root', None)
        current = self._current_page_key()
        self._station_page_key_fields = {}
        for key in [k for k in list(self._pages) if k.startswith('station:')]:
            area, content = self._pages.pop(key)
            if key in self._page_order:
                self._page_order.remove(key)
            self.page_stack.removeWidget(area)
            area.deleteLater()
            self._page_buttons.pop(key, None)
        for station in self._stations():
            key = f"station:{station.get('id')}"
            self.root = self._make_page(key)
            self._build_station_page(station)
            self.root.addStretch(1)
            try:
                style_controls(self._pages[key][1])
            except Exception:
                pass
        self.root = previous_root
        target = current if current in self._page_order else 'stations'
        self._switch_page(target, highlight=True); 

    def _build_station_page(self, station):
        sid = station.get('id')
        self.root.addWidget(TitleLabel(station.get('name') or '未命名中转站'))
        caption = CaptionLabel('中转站 ID：' + str(sid) + ' · 修改后点击「保存修改」')
        caption.setWordWrap(True)
        self.root.addWidget(caption)
        card = make_card()
        layout = QVBoxLayout(card); layout.setContentsMargins(24, 20, 24, 20); layout.setSpacing(12)
        name = LineEdit(); name.setText(station.get('name', '')); name.setPlaceholderText('名称，例如：主线路 · 国内')
        base = LineEdit(); base.setText(station.get('base_url', '')); base.setPlaceholderText('API Base URL')
        key_edit = LineEdit(); key_edit.setText(station.get('api_key', '')); key_edit.setEchoMode(LineEdit.Password); key_edit.setPlaceholderText('API Key')
        self._station_page_key_fields[sid] = key_edit
        upload = LineEdit(); upload.setText(station.get('upload_url', '')); upload.setPlaceholderText('上传接口 URL（可留空）')
        upload_key = LineEdit(); upload_key.setText(station.get('upload_api_key', '')); upload_key.setEchoMode(LineEdit.Password); upload_key.setPlaceholderText('上传 Token（可留空）')
        for caption_text, field in (('名称', name), ('API Base URL', base), ('API Key', key_edit),
                                    ('上传接口 URL', upload), ('上传 Token', upload_key)):
            row = WrapLayout(spacing=10)
            lbl = CaptionLabel(caption_text); lbl.setMinimumWidth(110)
            field.setMinimumWidth(180)
            field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            field.setFixedHeight(32)
            row.addWidget(lbl, 0, Qt.AlignVCenter); row.addWidget(field, 1)
            layout.addLayout(row)
            if field is upload:
                self._attach_upload_url_hint(upload, upload_key, layout)
        caps = WrapLayout(spacing=14)
        caps_label = CaptionLabel('能力标签'); caps_label.setMinimumWidth(110)
        caps.addWidget(caps_label, 0, Qt.AlignVCenter)
        boxes = {}
        for key2, text in (('video', tr('视频')), ('llm', tr('文本（AI分析）')), ('image', tr('生图'))):
            box = CheckBox(text); box.setChecked(key2 in (station.get('capabilities') or []))
            boxes[key2] = box; caps.addWidget(box)
        caps.addStretch(1); layout.addLayout(caps)
        buttons = WrapLayout(spacing=8)
        save = PushButton(tr('保存修改')); use = PushButton(tr('设为当前')); remove = PushButton(tr('删除该中转站'))
        test = PushButton(FIF.CONNECT, tr('测试'))
        save.setFixedHeight(32); use.setFixedHeight(32); remove.setFixedHeight(32)
        # 破坏性操作按危险语义标色（当前设置中仅此一处破坏性操作）。
        remove.setStyleSheet('QPushButton {color:#E5484D; border:1px solid rgba(229,72,77,0.45); border-radius:8px; background:transparent; padding:0 12px;}'
                             'QPushButton:hover {background:rgba(229,72,77,0.10); border-color:rgba(229,72,77,0.70);}')
        buttons.addWidget(save); buttons.addWidget(test); buttons.addWidget(use); buttons.addStretch(1); buttons.addWidget(remove)
        layout.addLayout(buttons)
        self.root.addWidget(card)
        save.clicked.connect(lambda checked=False, s=sid, n=name, b=base, k=key_edit, u=upload, uk=upload_key, bx=boxes:
                             self._save_station_page(s, n, b, k, u, uk, bx))
        test.clicked.connect(lambda checked=False, b=base, k=key_edit, u=upload, uk=upload_key, bx=boxes, button=test:
                             self._test_station_fields(b, k, u, uk, bx, button))
        use.clicked.connect(lambda checked=False, s=sid: self._set_current_station(s))
        remove.clicked.connect(lambda checked=False, s=sid: self._delete_station(s))

    def _save_station_page(self, sid, name, base, key_edit, upload, upload_key, boxes):
        name_text = name.text().strip()
        if not name_text:
            self.log_callback('中转站名称不能为空', 'warning')
            return
        if not self._repair_upload_fields(upload, upload_key):
            return
        stations = self._stations()
        for station in stations:
            if station.get('id') == sid:
                station.update(name=name_text, base_url=normalize_base_url(base.text()), api_key=key_edit.text().strip(),
                               upload_url=upload.text().strip(), upload_api_key=upload_key.text().strip(),
                               capabilities=[k for k, b in boxes.items() if b.isChecked()])
        self._persist_stations(stations)
        if sid == self.config_manager.config.get('stations_active'):
            self._apply_station_api(next(station for station in stations if station.get('id') == sid))
        self._render_stations()
        self._switch_page(f'station:{sid}', highlight=True)
        self.log_callback(f'中转站已更新：{name_text}', 'success')

    # ------------------------------------------------------------------
    # 中转站：每个中转站一个分类（命名 / 配置 / 测试 / 切换）
    # ------------------------------------------------------------------
    def _stations(self):
        return list(self.config_manager.config.get('stations', []) or [])

    def _persist_stations(self, stations, active=None):
        self.config_manager.update(('stations',), stations)
        if active is not None:
            self.config_manager.update(('stations_active',), active)
        self._refresh_routing_options()
        self._reload_model_lines()

    def _build_stations(self):
        group = self._group('中转站')
        self.stations_group = group
        card = make_card(); self.stations_card = card
        layout = QVBoxLayout(card); layout.setContentsMargins(24, 20, 24, 20); layout.setSpacing(12)
        hint = CaptionLabel(tr('每个中转站独立成一项（左侧导航可直达）；「设为当前」后任务走该线路；可标记视频 / 文本（AI分析）/ 生图能力。'))
        hint.setWordWrap(True); layout.addWidget(hint)
        self.station_list = QVBoxLayout(); self.station_list.setSpacing(8)
        holder = QWidget(); holder.setLayout(self.station_list); layout.addWidget(holder)
        buttons = WrapLayout(spacing=8)
        self.add_station_button = PushButton(FIF.ADD, '新增中转站')
        self.add_station_button.clicked.connect(lambda: self._open_station_editor(None))
        self.add_deepseek_button = PushButton(FIF.ADD, tr('添加官方 DeepSeek（模板）'))
        self.add_deepseek_button.setToolTip(tr('创建官方 DeepSeek 文本线路，API Key 留空后填写'))
        self.add_deepseek_button.clicked.connect(self._add_official_deepseek_template)
        self.capture_station_button = PushButton(FIF.SAVE, '把当前 API 配置保存为中转站')
        self.capture_station_button.clicked.connect(self._capture_current_station)
        buttons.addWidget(self.add_station_button); buttons.addWidget(self.add_deepseek_button)
        buttons.addWidget(self.capture_station_button); buttons.addStretch(1)
        layout.addLayout(buttons)
        group.addSettingCard(card)
        card._content_height = ContentHeight(card)
        self.station_rows = []
        self._editing_station_id = ''
        self._render_stations()

    def _build_station_editor_page(self):
        """新增中转站页：保留共用编辑器字段（与旧版完全一致）。"""
        card = make_card()
        layout = QVBoxLayout(card); layout.setContentsMargins(24, 20, 24, 20); layout.setSpacing(12)
        self.station_editor_host = QWidget()
        editor = QVBoxLayout(self.station_editor_host); editor.setContentsMargins(0, 0, 0, 0); editor.setSpacing(8)
        self.station_editor_title = CaptionLabel('新增中转站'); editor.addWidget(self.station_editor_title)
        self.station_name = LineEdit(); self.station_name.setPlaceholderText('名称，例如：主线路 · 国内'); editor.addWidget(self.station_name)
        self.station_base = LineEdit(); self.station_base.setPlaceholderText('API Base URL'); editor.addWidget(self.station_base)
        self.station_key = LineEdit(); self.station_key.setPlaceholderText('API Key')
        self.station_key.setEchoMode(LineEdit.Password); editor.addWidget(self.station_key)
        self.station_upload = LineEdit(); self.station_upload.setPlaceholderText('上传接口 URL（可留空沿用默认）'); editor.addWidget(self.station_upload)
        self.station_upload_key = LineEdit(); self.station_upload_key.setPlaceholderText('上传 Token（可留空）')
        self.station_upload_key.setEchoMode(LineEdit.Password); editor.addWidget(self.station_upload_key)
        self._attach_upload_url_hint(self.station_upload, self.station_upload_key, editor)
        caps = WrapLayout(spacing=14)
        self.station_caps = {}
        for key, text in (('video', tr('视频')), ('llm', tr('文本（AI分析）')), ('image', tr('生图'))):
            box = CheckBox(text); self.station_caps[key] = box; caps.addWidget(box)
        caps.addStretch(1); editor.addLayout(caps)
        editor_buttons = WrapLayout(spacing=8)
        save = PushButton(tr('保存')); save.clicked.connect(self._save_station_editor)
        test = PushButton(FIF.CONNECT, tr('测试'))
        test.clicked.connect(lambda checked=False, button=test: self._test_station_fields(
            self.station_base, self.station_key, self.station_upload, self.station_upload_key,
            self.station_caps, button))
        cancel = PushButton(tr('取消')); cancel.clicked.connect(self._close_station_editor)
        editor_buttons.addWidget(save); editor_buttons.addWidget(test); editor_buttons.addWidget(cancel); editor_buttons.addStretch(1)
        editor.addLayout(editor_buttons)
        layout.addWidget(self.station_editor_host)
        self.root.addWidget(card)

    def _fit_stations_card(self):
        try:
            self.stations_card.setFixedHeight(self.stations_card.layout().sizeHint().height() + 4)
        except Exception:
            pass

    @staticmethod
    def _station_host_text(station):
        return str(station.get('base_url') or '未填写地址')

    def _render_stations(self):
        while self.station_list.count():
            item = self.station_list.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.station_rows = []
        stations = self._stations()
        active = self.config_manager.config.get('stations_active', '')
        if not stations:
            empty = CaptionLabel('还没有中转站 · 点击「把当前 API 配置保存为中转站」一键创建，或「新增中转站」手动填写')
            empty.setWordWrap(True); self.station_list.addWidget(empty)
            self._fit_stations_card()
            self._sync_station_nav()
            self._refresh_api_group_title()
            self._render_station_pages()
            return
        cap_texts = {'video': tr('视频'), 'llm': tr('文本（AI分析）'), 'image': tr('生图')}
        for station in stations:
            row = make_card()
            row_layout = QVBoxLayout(row); row_layout.setContentsMargins(14, 10, 14, 10); row_layout.setSpacing(6)
            is_active = station.get('id') == active and bool(active)
            line1 = WrapLayout(spacing=8)
            dot = Dot(8, '#5B8DEF' if is_active else '#454B5A', filled=is_active)
            line1.addWidget(dot)
            name = label(station.get('name') or '未命名中转站', 13, '#F4F5F7', True)
            name.setToolTip(station.get('name') or '')
            setattr(row, '_studio_name_label', name)
            line1.addWidget(name)
            if is_active:
                current = label('当前使用中', 10, '#7CC79A')
                current.setProperty('statusTone', True)
                line1.addWidget(current)
            line1.addStretch(1)
            if not is_active:
                use = PushButton('设为当前'); use.setFixedHeight(28); use.setCursor(Qt.PointingHandCursor)
                use.clicked.connect(lambda checked=False, sid=station.get('id'): self._set_current_station(sid))
                line1.addWidget(use)
            edit = PushButton('编辑'); edit.setFixedHeight(28); edit.setCursor(Qt.PointingHandCursor)
            edit.clicked.connect(lambda checked=False, sid=station.get('id'): self._open_station_editor(sid))
            line1.addWidget(edit)
            remove = TransparentToolButton(FIF.DELETE); remove.setToolTip('删除该中转站')
            remove.clicked.connect(lambda checked=False, sid=station.get('id'): self._delete_station(sid))
            line1.addWidget(remove)
            line2 = WrapLayout(spacing=10)
            host = label(self._station_host_text(station), 11, '#6B7280', mono=True)
            host.setToolTip(station.get('base_url', ''))
            host.setMinimumWidth(0)
            host.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            line2.addWidget(host, 1)
            for cap in station.get('capabilities', []):
                line2.addWidget(label(cap_texts.get(cap, cap), 10, '#8B93A3'))
            row_layout.addLayout(line1); row_layout.addLayout(line2)
            self.station_list.addWidget(row)
            self.station_rows.append((row, station))
        self._fit_stations_card()
        self._sync_station_nav()
        self._refresh_api_group_title()
        self._render_station_pages()

    def _open_station_editor(self, station_id):
        self._editing_station_id = ''
        if station_id and station_id in {s.get('id') for s in self._stations()}:
            self._switch_page(f'station:{station_id}', highlight=True)
            field = self._station_page_key_fields.get(station_id)
            if field is not None:
                QTimer.singleShot(0, field.setFocus)
            return
        self.station_editor_title.setText('新增中转站')
        self.station_name.setText('')
        self.station_base.setText('')
        self.station_key.setText('')
        self.station_upload.setText('')
        self.station_upload_key.setText('')
        for key, box in self.station_caps.items():
            box.setChecked(key == 'video')
        self._switch_page('station-new', highlight=True)

    def _add_official_deepseek_template(self):
        """Create or focus the one official DeepSeek station without a key."""
        target_host = 'api.deepseek.com'
        existing = None
        for station in self._stations():
            try:
                host = (urlsplit(str(station.get('base_url') or '')).hostname or '').lower()
            except ValueError:
                host = ''
            if host == target_host:
                existing = station
                break
        if existing is None:
            existing = {
                'id': f'st-{int(time.time() * 1000)}',
                'name': '官方 DeepSeek',
                'base_url': normalize_base_url('https://api.deepseek.com'),
                'api_key': '',
                'upload_url': '',
                'upload_api_key': '',
                'capabilities': ['llm'],
                'enabled': True,
            }
            stations = self._stations()
            stations.append(existing)
            self._persist_stations(stations)
            self._render_stations()
        self._open_station_editor(existing.get('id'))
        self.log_callback(tr('官方 DeepSeek 模板已准备，请填写 API Key'), 'info')

    def _close_station_editor(self):
        self._editing_station_id = ''
        self._switch_page('stations', highlight=True)

    def _save_station_editor(self):
        name = self.station_name.text().strip()
        if not name:
            self.log_callback('中转站名称不能为空', 'warning')
            return
        if not self._repair_upload_fields(self.station_upload, self.station_upload_key):
            return
        capabilities = [key for key, box in self.station_caps.items() if box.isChecked()]
        payload = dict(name=name, base_url=normalize_base_url(self.station_base.text()), api_key=self.station_key.text().strip(),
                       upload_url=self.station_upload.text().strip(), upload_api_key=self.station_upload_key.text().strip(),
                       capabilities=capabilities, enabled=True)
        stations = self._stations()
        if self._editing_station_id:
            for station in stations:
                if station.get('id') == self._editing_station_id:
                    station.update(payload)
        else:
            payload['id'] = f'st-{int(time.time()*1000)}'
            stations.append(payload)
        self._persist_stations(stations)
        self._editing_station_id = ''
        self._render_stations()
        self._switch_page('stations', highlight=True)
        self.log_callback(f'中转站已保存：{name}', 'success')

    def _set_current_station(self, station_id):
        target = next((s for s in self._stations() if s.get('id') == station_id), None)
        if target is None:
            return
        self.config_manager.update(('stations_active',), station_id, save=False)
        self._apply_station_api(target)
        self._render_stations()
        self.log_callback(f'已切换到中转站：{target.get("name", "")}', 'success')

    def _apply_station_api(self, target):
        api = dict(self.config_manager.config.get('api', {}))
        for key, edit in (('base_url', self.base_url), ('api_key', self.api_key),
                          ('upload_url', self.upload_url), ('upload_api_key', self.upload_key)):
            value = target.get(key, '') or ''
            if key == 'base_url':
                value = normalize_base_url(value)
            blocker = QSignalBlocker(edit)
            edit.setText(value)
            del blocker
            api[key] = value
        self.config_manager.update(('api',), api)
        # Publish only after all credentials belong to the same station.
        self.api_credentials_changed.emit()

    def _delete_station(self, station_id):
        stations = [s for s in self._stations() if s.get('id') != station_id]
        active = self.config_manager.config.get('stations_active', '')
        self._persist_stations(stations, '' if active == station_id else None)
        self._render_stations()
        self._switch_page('stations', highlight=True)

    def _capture_current_station(self):
        api = dict(self.config_manager.config.get('api', {}))
        stations = self._stations()
        host = str(api.get('base_url', '')).split('//')[-1].split('/')[0].strip()
        name = host or f'中转站 {len(stations) + 1}'
        taken = {s.get('name') for s in stations}
        candidate, index = name, 2
        while candidate in taken:
            candidate = f'{name} ·{index}'; index += 1
        station = dict(id=f'st-{int(time.time()*1000)}', name=candidate,
                       base_url=normalize_base_url(api.get('base_url', '')),
                       api_key=api.get('api_key', ''), upload_url=api.get('upload_url', ''),
                       upload_api_key=api.get('upload_api_key', ''), capabilities=['video'], enabled=True)
        stations.append(station)
        active = self.config_manager.config.get('stations_active', '') or station['id']
        self._persist_stations(stations, active)
        self._render_stations()
        self.log_callback(f'已保存中转站：{candidate}', 'success')

    def _mirror_api_to_station(self, *_args):
        active = self.config_manager.config.get('stations_active', '')
        if not active:
            return
        stations = self._stations(); changed = False
        api = self.config_manager.config.get('api', {})
        for station in stations:
            if station.get('id') != active:
                continue
            for key in ('base_url', 'api_key', 'upload_url', 'upload_api_key'):
                value = api.get(key, '')
                if station.get(key, '') != value:
                    station[key] = value; changed = True
        if changed:
            self.config_manager.update(('stations',), stations)

    def _refresh_api_group_title(self):
        try:
            group = getattr(self, 'api_group', None)
            if group is None:
                return
            active = self.config_manager.config.get('stations_active', '')
            name = next((s.get('name') for s in self._stations() if s.get('id') == active and active), '')
            group.titleLabel.setText(f'当前线路 · {name}' if name else '当前线路')
            group.titleLabel.setToolTip('此处编辑的是当前使用的中转站连接参数；修改会自动回写该中转站')
        except Exception:
            pass

    def _value(self, path):
        return self.config_manager.config[path[0]][path[1]]

    def _line(self, group, title, path, secret=False):
        edit = LineEdit()
        edit.setText(self._value(path))
        edit.setMinimumWidth(240)
        if path == ('api', 'base_url'):
            edit.editingFinished.connect(self._save_normalized_base_url)
        else:
            edit.textChanged.connect(lambda v: self.config_manager.update(path, v))
        host = QWidget()
        if path == ('api', 'upload_url'):
            column = QVBoxLayout(host)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(4)
            row_host = QWidget(host)
            row = WrapLayout(row_host)
            column.addWidget(row_host)
        else:
            column = None
            row = WrapLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        if secret:
            edit.setEchoMode(LineEdit.Password)
            reveal = TransparentToolButton(FIF.VIEW)
            reveal.setToolTip("显示或隐藏密钥")
            reveal.clicked.connect(lambda: self._toggle_secret(edit, reveal))
            row.addWidget(reveal)
        if path == ("api", "api_key"):
            self.test_button = PushButton(FIF.CONNECT, tr('测试连接'))
            self.test_button.setToolTip(tr('分别检查视频、文本和图片 API；上传 Token 未配置时跳过上传测试'))
            self.test_button.clicked.connect(self.test_connection)
            row.addWidget(self.test_button)
        if column is not None:
            hint = CaptionLabel('')
            hint.setWordWrap(True)
            hint.setStyleSheet('color:#D98282;')
            hint.hide()
            column.addWidget(hint)
            self._api_upload_url_hint = hint
            self._api_upload_url_hint_layout = column
        card = CustomSettingCard(title, host, FIF.LINK)
        group.addSettingCard(card)
        if column is not None:
            self._upload_url_hint_cards = getattr(self, '_upload_url_hint_cards', {})
            self._upload_url_hint_cards[edit] = card
        return edit

    def _combo(self, group, title, path, choices, texts=None):
        item = OptionsConfigItem(path[0], path[1], self._value(path), OptionsValidator(choices))
        card = JsonComboBoxSettingCard(item, FIF.SETTING, title, texts=texts or choices)
        card.comboBox.setMinimumWidth(200)
        card._prepare_responsive_card()
        # Model-dependent choices are rebuilt without signals. Persist the actual
        # selected item even when the setting card's cached ConfigItem is unchanged.
        card.comboBox.currentIndexChanged.connect(
            lambda index: self.config_manager.update(path, card.comboBox.itemData(index)) if index >= 0 else None)
        group.addSettingCard(card)
        return card.comboBox

    def _spin(self, group, title, path, low, high):
        control = SpinBox()
        control.setRange(low, high)
        control.setValue(self._value(path))
        control.setMinimumWidth(120)
        control.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        control.valueChanged.connect(lambda v: self.config_manager.update(path, v))
        group.addSettingCard(CustomSettingCard(title, control))
        return control

    def _model_combo(self, group, title, path, automatic=False):
        combo = ModelComboBox()
        combo.setMinimumWidth(0)
        combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        combo.set_models(catalog_snapshot(self.config_manager), self._value(path), automatic=automatic)
        combo.currentTextChanged.connect(lambda model: self.config_manager.update(path, model))
        group.addSettingCard(CustomSettingCard(title, combo))
        return combo

    def _switch(self, group, title, path, icon=FIF.SETTING):
        card = StudioSwitchSettingCard(icon, title)
        card.setChecked(self._value(path))
        card.checkedChanged.connect(lambda v: self.config_manager.update(path, bool(v)))
        group.addSettingCard(card)
        return card

    def _build_api(self):
        group = self._group("API 连接")
        self.api_group = group
        self._build_routing_cards(group)
        self.base_url = self._line(group, "API Base URL", ("api", "base_url"))
        self.base_url.setPlaceholderText(tr('可只填域名，如 https://api.kkone.vip/ ，自动补 /v1；官方 DeepSeek 填 https://api.deepseek.com'))
        self.api_key = self._line(group, "API Key", ("api", "api_key"), True)
        self.upload_url = self._line(group, "图床上传 URL", ("api", "upload_url"))
        self.upload_key = self._line(group, '图床 API Key（上传 Token）', ('api', 'upload_api_key'), True)
        self._register_upload_url_hint(self.upload_url, self.upload_key, self._api_upload_url_hint,
                                       self._api_upload_url_hint_layout)
        self.upload_key.setPlaceholderText('服务商签发的素材上传 Token')
        self.upload_key.setToolTip('默认图床使用 X-Upload-Token 和 Bearer 头；视频 API Key 不保证具有上传权限')
        self.base_url.textChanged.connect(self._persist_base_url_input)
        for field in (self.api_key, self.upload_url, self.upload_key):
            field.textChanged.connect(self._mirror_api_to_station)
        self.upload_url.editingFinished.connect(lambda: self._repair_upload_fields(self.upload_url, self.upload_key))
        self._refresh_api_group_title()
        self.auto_upload = self._switch(group, "自动上传缺失图片", ("api", "auto_upload_missing"), FIF.CLOUD)
        self.debug_mode = self._switch(group, '调试模式', ('diagnostics', 'debug_mode'), FIF.INFO)
        self.debug_mode.setToolTip('记录参考图、提示词和脱敏请求体；额外下载上传后的图片检查尺寸和格式')
        self.debug_mode.checkedChanged.connect(self.debug_mode_changed)
        host = QWidget()
        row = WrapLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        self.last_model_sync = CaptionLabel('尚未同步')
        self.sync_models_button = PushButton(FIF.SYNC, '同步上游模型')
        row.addWidget(self.last_model_sync, 1)
        row.addWidget(self.sync_models_button)
        group.addSettingCard(CustomSettingCard('上游模型目录', host, FIF.SYNC))
        diag_host = QWidget()
        diag_row = WrapLayout(diag_host)
        diag_row.setContentsMargins(0, 0, 0, 0)
        self.diagnostics_hint = CaptionLabel(tr('将日志、脱敏配置与环境信息打包为 zip，便于排查问题'))
        self.diagnostics_hint.setWordWrap(True)
        self.diagnostics_button = PushButton(FIF.SAVE, tr('导出诊断包'))
        self.diagnostics_button.clicked.connect(self._export_diagnostics)
        diag_row.addWidget(self.diagnostics_hint, 1)
        diag_row.addWidget(self.diagnostics_button)
        group.addSettingCard(CustomSettingCard(tr('诊断包'), diag_host, FIF.SAVE))

    def _build_routing_cards(self, group):
        self.routing_combos = {}
        self.routing_test_buttons = {}
        routing_options = (
            ('video', tr('视频线路')),
            ('chat', tr('AI分析（文本）线路')),
            ('image', tr('图片生成线路')),
        )
        for capability, title in routing_options:
            combo = ComboBox()
            combo.setMinimumWidth(200)
            combo.currentIndexChanged.connect(
                lambda index, kind=capability, control=combo: self._routing_changed(kind, control, index))
            self.routing_combos[capability] = combo
            host = QWidget()
            row = WrapLayout(host, spacing=8)
            row.setContentsMargins(0, 0, 0, 0)
            test = PushButton(FIF.CONNECT, tr('测试'))
            test.setFixedHeight(32)
            test.clicked.connect(lambda checked=False, kind=capability, button=test:
                                 self._test_routing_line(kind, button))
            row.addWidget(combo, 1)
            row.addWidget(test)
            self.routing_test_buttons[capability] = test
            group.addSettingCard(CustomSettingCard(title, host, FIF.LINK))
        self._refresh_routing_options()

    def _test_routing_line(self, capability, button):
        config = copy.deepcopy(self.config_manager.config)
        line = resolve_line(config, capability)
        models = catalog_snapshot(self.config_manager, capability)
        button.setEnabled(False)
        button.setText(tr('测试中'))

        def work():
            return probe_line(line.get('base_url', ''), line.get('api_key', ''), capability, models=models)

        def done(result):
            button.setEnabled(True)
            button.setText(tr('测试'))
            title = {'video': tr('视频 API'), 'chat': tr('AI分析（文本）API'),
                     'image': tr('图片 API')}[capability]
            message = self._format_probe_line(title, result, capability)
            raw_message = self._format_probe_detail(title, result) + self._raw_probe_metadata(result)
            tone = 'success' if result.get('ok') else 'error'
            self.log_callback(f"{tr('测试')} {capability}: {raw_message}", tone)
            self._notice(message, bool(result.get('ok')), tooltip=raw_message)

        def failed(message):
            done(dict(ok=False, message=self._redact_probe_error(message), counts={}))

        try:
            self.jobs.start(work, done, failed)
        except Exception as error:
            failed(error)

    def _attach_upload_url_hint(self, url_field, token_field, layout):
        hint = CaptionLabel('')
        hint.setWordWrap(True)
        hint.setStyleSheet('color:#D98282;')
        hint.hide()
        layout.addWidget(hint)
        self._register_upload_url_hint(url_field, token_field, hint, layout)

    def _register_upload_url_hint(self, url_field, token_field, hint, layout=None):
        hints = getattr(self, '_upload_url_hints', None)
        if hints is None:
            hints = self._upload_url_hints = {}
        hints[url_field] = hint
        layouts = getattr(self, '_upload_url_hint_layouts', None)
        if layouts is None:
            layouts = self._upload_url_hint_layouts = {}
        if layout is not None:
            layouts[url_field] = layout
        url_field.editingFinished.connect(
            lambda url=url_field, token=token_field: self._repair_upload_fields(url, token))

    @staticmethod
    def _correct_upload_pair(upload_url, upload_token):
        url = str(upload_url or '').strip()
        token = str(upload_token or '').strip()
        if not url or not looks_like_upload_token(url):
            return url, token, False, False
        overwritten = bool(token and token != url)
        return '', url, True, overwritten

    def _sanitize_upload_fields_once(self):
        if getattr(self, '_upload_fields_sanitized', False):
            return 0
        self._upload_fields_sanitized = True
        config = getattr(self.config_manager, 'config', {})
        if not isinstance(config, dict):
            return 0

        corrected = 0
        api = dict(config.get('api') or {})
        url, token, changed, _overwritten = self._correct_upload_pair(
            api.get('upload_url', ''), api.get('upload_api_key', ''))
        if changed:
            api['upload_url'] = url
            api['upload_api_key'] = token
            self._write_config_value(('api',), api)
            corrected += 1

        source_stations = config.get('stations', [])
        stations = [dict(station) for station in source_stations if isinstance(station, dict)] \
            if isinstance(source_stations, list) else []
        stations_changed = False
        for station in stations:
            url, token, changed, _overwritten = self._correct_upload_pair(
                station.get('upload_url', ''), station.get('upload_api_key', ''))
            if not changed:
                continue
            station['upload_url'] = url
            station['upload_api_key'] = token
            stations_changed = True
            corrected += 1
        if stations_changed:
            self._write_config_value(('stations',), stations)

        if corrected:
            self.log_callback(tr('启动时已自动纠正 {count} 条上传 URL 中的 Token').format(count=corrected), 'info')
        return corrected

    def _write_config_value(self, path, value):
        update = getattr(self.config_manager, 'update', None)
        if callable(update):
            update(path, value)
            return
        target = self.config_manager.config
        for key in path[:-1]:
            target = target.setdefault(key, {})
        target[path[-1]] = value

    def _fit_upload_url_hint(self, url_field):
        layout = getattr(self, '_upload_url_hint_layouts', {}).get(url_field)
        if layout is None:
            return
        layout.invalidate()
        owner = layout.parentWidget()
        if owner is not None:
            owner.updateGeometry()

        setting_card = getattr(self, '_upload_url_hint_cards', {}).get(url_field)
        if setting_card is None and owner is not None:
            candidate = owner
            while candidate is not None:
                if hasattr(candidate, '_card_relayout'):
                    setting_card = candidate
                    break
                if hasattr(candidate, 'setBorderRadius'):
                    break
                candidate = candidate.parentWidget()
        if setting_card is not None and hasattr(setting_card, '_card_relayout'):
            setting_card._card_relayout.schedule()
            return

        candidate = owner
        while candidate is not None and not hasattr(candidate, 'setBorderRadius'):
            candidate = candidate.parentWidget()
        if candidate is None or candidate.layout() is None:
            return

        def fit_card(card=candidate):
            card.layout().invalidate()
            card.layout().activate()
            card.setFixedHeight(max(1, card.layout().sizeHint().height()))
            card.updateGeometry()

        QTimer.singleShot(0, fit_card)

    def _redact_probe_error(self, message):
        text = str(message)
        config = getattr(self.config_manager, 'config', {})
        secrets = []
        api = config.get('api', {}) if isinstance(config, dict) else {}
        if isinstance(api, dict):
            secrets.extend((api.get('api_key'), api.get('upload_api_key')))
        stations = config.get('stations', []) if isinstance(config, dict) else []
        for station in stations if isinstance(stations, list) else ():
            if isinstance(station, dict):
                secrets.extend((station.get('api_key'), station.get('upload_api_key')))
        for secret in secrets:
            if secret:
                text = text.replace(str(secret), '[REDACTED]')
        return tr(text)

    @staticmethod
    def _dedupe_probe_lines(text):
        lines = []
        seen = set()
        statuses = '|'.join(re.escape(tr(status)) for status in ('成功', '失败', '跳过'))
        for line in str(text or '').splitlines():
            line = line.strip()
            if line and line not in seen:
                seen.add(line)
                match = re.match(r'^.+?：(?:' + statuses + r') · (.*)$', line)
                if match:
                    lines.append(line)
                    detail = match.group(1).strip()
                    if detail:
                        seen.add(detail)
                    continue
                lines.append(line)
        return '\n'.join(lines)

    def _friendly_probe_detail(self, result):
        raw = self._redact_probe_error(result.get('message') or tr('测试未完成'))
        lowered = raw.casefold()
        if any(marker in lowered for marker in (
                'httpsconnectionpool', 'connectionpool', 'proxyerror', 'connectionerror',
                'sslerror', 'unexpected_eof', 'ssl_c_syscall', 'handshake', 'max retries',
                'readtimeout', 'connecttimeout', 'timed out', 'timeout')):
            return tr(classify_connection_error(raw)['message'])

        status = result.get('status_code')
        if isinstance(status, str) and status.isdecimal():
            status = int(status)
        if not isinstance(status, int) or isinstance(status, bool):
            status = None
        if status is None:
            match = re.search(r'\bHTTP\s*(\d{3})\b', raw, flags=re.IGNORECASE)
            status = int(match.group(1)) if match else None
        if status is None and raw.lstrip().startswith(('{', '[')):
            try:
                payload = json.loads(raw)
                error = payload.get('error', payload) if isinstance(payload, dict) else {}
                candidate = error.get('code') if isinstance(error, dict) else None
                if isinstance(candidate, str) and candidate.isdecimal():
                    candidate = int(candidate)
                status = candidate if isinstance(candidate, int) and not isinstance(candidate, bool) else None
            except (TypeError, ValueError):
                status = None
            if status is None:
                status = 400
        if isinstance(status, int) and 200 <= status < 300:
            if result.get('ok'):
                return raw
            if raw.lstrip().startswith(('{', '[')):
                return tr('请求失败，请检查线路与参数')
            return raw
        if status is not None:
            try:
                classified = classify_http_error(status, raw)
                if classified['category'] == 'http':
                    return tr('HTTP {code}：{message}').format(
                        code=status, message=tr('请求失败，请检查线路与参数'))
                return tr(classified['message'])
            except (TypeError, ValueError):
                pass
        if raw.lstrip().startswith(('{', '[')):
            return tr('请求失败，请检查线路与参数')
        return raw

    def _format_probe_line(self, title, result, capability):
        if result.get('level') == 'warning':
            status = tr('跳过')
        else:
            status = tr('成功') if result.get('ok') else tr('失败')
        detail = '；'.join(self._dedupe_probe_lines(self._friendly_probe_detail(result)).splitlines())
        suffix = ''
        if capability in ('video', 'chat', 'image'):
            counts = result.get('counts') or {}
            count_text = tr('视频 {video} / 文本 {chat} / 图片 {image}').format(
                video=counts.get('video', 0), chat=counts.get('chat', 0),
                image=counts.get('image', 0))
            if result.get('ok') and capability == 'chat' and result.get('model'):
                detail = tr('文本调用成功')
                suffix = tr('命中 {model}；{counts}').format(
                    model=result['model'], counts=count_text)
            elif counts:
                suffix = count_text
        if suffix:
            detail += f'（{suffix}）'
        return f'{title}：{status} · {detail}'

    def _format_probe_detail(self, title, result):
        if result.get('level') == 'warning':
            status = tr('跳过')
        else:
            status = tr('成功') if result.get('ok') else tr('失败')
        detail = self._redact_probe_error(result.get('message') or tr('测试未完成'))
        return f'{title}：{status} · {detail}'

    @staticmethod
    def _raw_probe_metadata(result):
        lines = []
        counts = result.get('counts') or {}
        if counts:
            lines.append(tr('视频 {video} / 文本 {chat} / 图片 {image}').format(
                video=counts.get('video', 0), chat=counts.get('chat', 0),
                image=counts.get('image', 0)))
        if result.get('model'):
            lines.append(tr('文本实际命中模型：{model}').format(model=result['model']))
        return '\n' + '\n'.join(lines) if lines else ''

    def _repair_upload_fields(self, url_field, token_field, *, notify=True, log_invalid=True):
        raw_url, token, changed, overwritten = self._correct_upload_pair(
            url_field.text(), token_field.text())
        if changed:
            if token_field.text().strip() != token:
                token_field.setText(token)
            if url_field.text().strip():
                url_field.clear()
            if notify:
                message = (tr('已把误填到 URL 的上传 Token 移入 Token 字段（覆盖旧值）')
                           if overwritten else tr('已将上传 Token 移入 Token 字段，并清空上传 URL。'))
                self._notice(message, True)
        valid = valid_http_url(raw_url)
        hint = getattr(self, '_upload_url_hints', {}).get(url_field)
        if hint is not None:
            if valid:
                hint.clear()
                hint.hide()
            else:
                hint.setText(tr('上传 URL 必须以 http:// 或 https:// 开头，或留空使用默认地址。'))
                hint.show()
            self._fit_upload_url_hint(url_field)
        if not valid and log_invalid:
            self.log_callback(tr('上传 URL 格式无效，请修正后再保存或测试。'), 'warning')
        return valid

    def _test_station_fields(self, base_field, key_field, upload_field, upload_key_field,
                             capability_boxes, button):
        upload_valid = self._repair_upload_fields(
            upload_field, upload_key_field, notify=False, log_invalid=False)
        selected = [(key, {'llm': 'chat'}.get(key, key))
                    for key in ('video', 'llm', 'image')
                    if key in capability_boxes and capability_boxes[key].isChecked()]
        if not selected:
            self._notice(tr('请至少勾选一项能力后再测试。'), False, level='warning')
            return
        base_url = normalize_base_url(base_field.text())
        api_key = key_field.text().strip()
        button.setEnabled(False)
        button.setText(tr('测试中'))

        def work():
            results = {}
            for key, capability in selected:
                try:
                    results[key] = probe_line(base_url, api_key, capability)
                except Exception as error:
                    results[key] = dict(ok=False, level='error',
                                       message=self._redact_probe_error(error), counts={})
            return results

        def finish(results):
            button.setEnabled(True)
            button.setText(tr('测试'))
            titles = {'video': tr('视频 API'), 'llm': tr('AI分析（文本）API'),
                      'image': tr('图片 API')}
            messages = []
            raw_details = []
            all_ok = True
            for key, _capability in selected:
                result = results.get(key, {})
                ok = bool(result.get('ok'))
                all_ok = all_ok and ok
                capability = {'video': 'video', 'llm': 'chat', 'image': 'image'}[key]
                messages.append(self._format_probe_line(titles[key], result, capability))
                raw = self._format_probe_detail(titles[key], result) + self._raw_probe_metadata(result)
                raw_details.append(raw)
                self.log_callback(raw, 'success' if ok else 'error')
            level = 'error' if not all_ok else 'success'
            if not upload_valid:
                warning = tr('上传 URL 未配置或格式无效，本次仅测试所选 API 线路')
                messages.append(self._format_probe_line(tr('上传 API'), dict(
                    ok=True, level='warning', message=warning), 'upload'))
                raw_details.append(warning)
                self.log_callback(warning, 'warning')
                if all_ok:
                    level = 'warning'
            self._notice('\n'.join(messages), all_ok, level=level,
                         tooltip=self._dedupe_probe_lines('\n'.join(raw_details)))

        def failed(message):
            finish({key: dict(ok=False, message=self._redact_probe_error(message))
                    for key, _capability in selected})

        try:
            self.jobs.start(work, finish, failed)
        except Exception as error:
            failed(error)

    def _refresh_routing_options(self):
        combos = getattr(self, 'routing_combos', {})
        if not combos:
            return
        stations = [station for station in self._stations() if station.get('enabled')]
        routing = self.config_manager.config.get('line_routing', {})
        restored_route = False
        for capability, combo in combos.items():
            previous = combo.currentData() or 'active'
            saved = (routing.get(capability, 'active') if isinstance(routing, dict) else 'active') or 'active'
            blocker = QSignalBlocker(combo)
            combo.clear()
            combo.addItem(tr('当前线路'), userData='active')
            for station in stations:
                combo.addItem(str(station.get('name') or station.get('id') or ''), userData=station.get('id'))
            index = combo.findData(saved)
            combo.setCurrentIndex(index if index >= 0 else 0)
            restored_route |= (combo.currentData() or 'active') != previous
            del blocker
        # Rebuilding the options can restore a persisted station without a
        # currentIndexChanged signal (signals are blocked above).  Notify the
        # existing connection chain so route labels and capability catalogs
        # reflect that restored selection as well.
        if restored_route:
            self.api_credentials_changed.emit()

    def _routing_changed(self, capability, combo, index):
        if index < 0:
            return
        station_id = combo.itemData(index) or 'active'
        self.config_manager.update(('line_routing', capability), station_id)
        controller = getattr(self.config_manager, 'model_catalog_controller', None)
        if controller is not None:
            controller.reload_lines()
        self.api_credentials_changed.emit()

    def _reload_model_lines(self):
        controller = getattr(self.config_manager, 'model_catalog_controller', None)
        if controller is not None:
            controller.reload_lines()

    def _save_normalized_base_url(self):
        value = normalize_base_url(self.base_url.text())
        blocker = QSignalBlocker(self.base_url)
        self.base_url.setText(value)
        del blocker
        self.config_manager.update(('api', 'base_url'), value)
        self._mirror_api_to_station()
        self.api_credentials_changed.emit()

    def _persist_base_url_input(self, value):
        self.config_manager.update(('api', 'base_url'), normalize_base_url(value))
        self._mirror_api_to_station()

    def _build_pool(self):
        self.pool_group = self._group('模型池')
        host = QWidget()
        host_layout = QVBoxLayout(host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setSpacing(12)
        self.pool_category_title = CaptionLabel(tr('模型池类别'))
        self.pool_category_title.setAlignment(Qt.AlignCenter)
        self.pool_category_title.setStyleSheet('font-weight:600;')
        host_layout.addWidget(self.pool_category_title)
        self.pool_tabs = SegmentedWidget()
        self.pool_tabs.addItem('video', tr('视频模型'))
        self.pool_tabs.addItem('chat', tr('AI分析模型'))
        self.pool_tabs.addItem('image', tr('图片模型'))
        self.pool_stack = QStackedWidget()
        self.pool_sections = {}
        self.pool_rows = {'video': self.model_rows, 'chat': [], 'image': []}
        self.pool_models_cards = {}
        self.pool_model_layouts = {}
        self.add_model_buttons = {}
        self.pool_controls = {}

        for kind, config_key, title in (
                ('video', 'model_pool', '视频模型'),
                ('chat', 'chat_model_pool', 'AI分析模型'),
                ('image', 'image_model_pool', '图片模型')):
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(0, 0, 0, 0)
            section = SettingCardGroup(tr(title), page)
            self.pool_sections[kind] = section
            enabled = self._switch(section, '启用模型池' if kind == 'video' else tr('启用模型池'),
                                   (config_key, 'enabled'), FIF.SYNC)
            strategy = self._combo(section, '调用策略' if kind == 'video' else tr('调用策略'),
                                   (config_key, 'strategy'), ['轮询', '随机', '优先级'])
            strategy.setMinimumWidth(0)
            strategy.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            failover = self._switch(section, '自动故障转移' if kind == 'video' else tr('自动故障转移'),
                                    (config_key, 'auto_failover'), FIF.SYNC)
            cooldown = self._spin(section, '冷却时间（秒）' if kind == 'video' else tr('冷却时间（秒）'),
                                  (config_key, 'cooldown'), 1, 3600)
            self.pool_controls[kind] = (enabled, strategy, failover, cooldown)
            if kind == 'video':
                self.pool_enabled, self.strategy, self.failover, self.cooldown = enabled, strategy, failover, cooldown
                self.pool_enabled.setToolTip(tr('关闭时使用工作台所选单模型。运行期间修改设置将在下一批任务生效。'))

            card = make_card()
            layout = QVBoxLayout(card)
            layout.setContentsMargins(20, 20, 20, 20)
            layout.setSpacing(12)
            hint_text = ('模型列表 · 列表顺序即优先级；修改在下一批生效'
                         if kind == 'video' else tr('模型列表 · 列表顺序即优先级；修改在下一批生效'))
            hint = CaptionLabel(hint_text)
            hint.setWordWrap(True)
            layout.addWidget(hint)
            model_layout = QVBoxLayout()
            model_layout.setSpacing(8)
            layout.addLayout(model_layout)
            self.pool_models_cards[kind] = card
            self.pool_model_layouts[kind] = model_layout
            for model in self._value((config_key, 'models')):
                self._add_model_row(model.get('name', ''), model.get('enabled', True),
                                    model.get('status', '健康'), persist=False, kind=kind)
            add_model = PushButton(FIF.ADD, '添加模型' if kind == 'video' else tr('添加模型'))
            add_model.clicked.connect(lambda checked=False, model_kind=kind:
                                      self._add_model_row('video-v3' if model_kind == 'video' else '',
                                                          True, '健康', kind=model_kind))
            self.add_model_buttons[kind] = add_model
            layout.addWidget(add_model, 0, Qt.AlignLeft)
            card.setFixedHeight(layout.sizeHint().height())
            card._content_height = ContentHeight(card)
            section.addSettingCard(card)
            page_layout.addWidget(section)
            page_layout.addStretch(1)
            self.pool_stack.addWidget(page)

        self.models_card = self.pool_models_cards['video']
        self.model_layout = self.pool_model_layouts['video']
        self.add_model = self.add_model_buttons['video']
        self.pool_tabs.currentItemChanged.connect(self._pool_tab_changed)
        host_layout.addWidget(self.pool_tabs)
        host_layout.addWidget(self.pool_stack)
        self.pool_category_card = CustomSettingCard('', host, FIF.LIBRARY)
        self.pool_category_card.iconLabel.hide()
        self.pool_category_card.iconLabel.setFixedSize(0, 0)
        self.pool_group.addSettingCard(self.pool_category_card)
        self.pool_tabs.setCurrentItem('video')
        self._pool_tab_changed('video')

    def _pool_tab_changed(self, kind):
        if kind not in self.pool_sections:
            return
        self.pool_stack.setCurrentIndex(('video', 'chat', 'image').index(kind))
        self._fit_pool_models_card(kind)

    def _fit_pool_models_card(self, kind):
        card = self.pool_models_cards.get(kind)
        if card is not None:
            card.setFixedHeight(card.layout().sizeHint().height())
            card.updateGeometry()

    def _build_task(self):
        group = self._group("任务策略")
        self.task_group = group
        self.auto_detect = self._switch(group, '自动识别模型', ('prompt_detection', 'enabled'))
        self.fallback_model = self._model_combo(group, '无法识别时使用', ('prompt_detection', 'fallback_model'), automatic=True)
        self.auto_detect.checkedChanged.connect(lambda *_: self.detection_changed.emit())
        self.fallback_model.currentIndexChanged.connect(lambda *_: self.detection_changed.emit())
        self.prevent_duplicates = self._switch(group, '防重复提交', ('task_strategy', 'prevent_duplicates'), FIF.SETTING)
        self.auto_convert_prompt = self._switch(group, '自动转换提示词格式', ('prompt_conversion', 'enabled'), FIF.SYNC)
        self.preserve_original_prompt = self._switch(group, '转换后保留原文', ('prompt_conversion', 'preserve_original'), FIF.DOCUMENT)
        self.preserve_original_prompt.setToolTip('保留转换前的额外原文快照；关闭后仍保存实际提交文本，便于核对任务。原始 TXT 文件不改动。')
        self.prefer_same_format = self._switch(group, '故障转移优先同格式模型', ('prompt_conversion', 'prefer_same_format'), FIF.SYNC)
        self.max_concurrency = self._spin(group, '最大并发数', ('task_strategy', 'max_concurrency'), 1, 5)
        self.max_concurrency.setToolTip('全队列最多同时处理的任务数（默认 5）；所有产品共享该并发，任务完成立即补位')
        self.auto_retry = self._switch(group, "失败自动重试", ("task_strategy", "auto_retry"), FIF.SYNC)
        self.max_retry = self._spin(group, "最大重试次数", ("task_strategy", "max_retries"), 0, 20)
        self.max_retry.setToolTip('首次尝试之外的重试次数（默认 3）；超过后任务标记失败并保存现场，队列继续下一个')
        self.retry_interval = self._spin(group, "重试间隔（秒）", ("task_strategy", "retry_interval"), 1, 60)
        self.fail_threshold = self._spin(group, "连续失败阈值", ("task_strategy", "failure_skip_threshold"), 1, 100)
        self.watch_interval = self._spin(group, '无人值守监听（秒）', ('task_strategy', 'watch_interval'), 0, 86400)
        self.watch_interval.setToolTip('队列结束后按此间隔自动重扫目录；发现新提示词会自动开始下一批，适合 7×24 无人值守。0 = 关闭')
        self.disk_cleanup_days = self._spin(group, '临时缓存保留（天）', ('task_strategy', 'disk_cleanup_days'), 0, 90)
        self.disk_cleanup_days.setToolTip('仅清理输出目录内超过保留期的下载临时文件（含联接/符号链接防护，绝不越出输出目录）；0 表示停止清理，仅检查剩余空间')
        self.disk_min_free_gb = self._spin(group, '磁盘剩余预警（GB）', ('task_strategy', 'disk_min_free_gb'), 1, 200)
        self.disk_min_free_gb.setToolTip('输出盘剩余空间低于该值时写入预警日志并加强清理')
        self.max_retry.valueChanged.connect(lambda *_: self.task_settings_changed.emit())
        self.fail_threshold.valueChanged.connect(lambda *_: self.task_settings_changed.emit())
        self.unmatched = self._combo(group, "未匹配参考图时", ("task_strategy", "unmatched_prompt"),
                                     ["跳过并警告", "仍提交文生视频", "暂停任务"],
                                     ["跳过该任务", "仍提交为文生视频", "暂停任务"])
        self.naming = self._line(group, "下载命名规则", ("task_strategy", "naming_rule"))
        self.open_folder = self._switch(group, "下载完成自动打开", ("task_strategy", "open_folder_after_download"), FIF.FOLDER)

    def _build_defaults(self):
        group = self._group("默认参数")
        self.defaults_group = group
        self.default_model = self._model_combo(group, "默认模型", ("defaults", "model"))
        self.default_ratio = self._combo(group, "默认比例", ("defaults", "aspect_ratio"), ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "2:3", "3:2"])
        self.default_resolution = self._combo(group, "默认分辨率", ("defaults", "resolution"), ["480p", "720p", "768p", "1080p", "2K", "4K"])
        self.default_duration = self._spin(group, "默认时长（秒）", ("defaults", "duration"), 1, 30)
        self.default_poll = self._spin(group, "默认轮询间隔（秒）", ("defaults", "poll_interval"), 3, 60)
        self.default_model.currentTextChanged.connect(self._refresh_default_options)
        self.default_duration.valueChanged.connect(self._refresh_default_options)
        for control in (self.default_model, self.default_ratio, self.default_resolution):
            control.currentTextChanged.connect(self._emit_generation_defaults)
        self.default_duration.valueChanged.connect(self._emit_generation_defaults)
        self.default_poll.valueChanged.connect(self._emit_generation_defaults)
        self._refresh_default_options()

    def _refresh_default_options(self, *_):
        values = apply_model_options(self.default_model.currentText(), self.default_ratio, self.default_resolution, self.default_duration,
                                     catalog=catalog_snapshot(self.config_manager))
        for key, value in values.items():
            if self.config_manager.config['defaults'][key] != value:
                self.config_manager.update(('defaults', key), value)

    def _emit_generation_defaults(self, *_args):
        if not getattr(self, '_applying_saved', False):
            self.generation_defaults_changed.emit()

    def apply_saved_generation_defaults(self):
        if getattr(self, '_applying_saved', False):
            return
        self._applying_saved = True
        try:
            defaults = self.config_manager.config['workspace']
            blocked = [QSignalBlocker(self.default_model), QSignalBlocker(self.default_ratio),
                       QSignalBlocker(self.default_resolution), QSignalBlocker(self.default_duration),
                       QSignalBlocker(self.default_poll)]
            self.default_model.set_models(catalog_snapshot(self.config_manager), defaults.get('model', ''))
            apply_model_options(self.default_model.currentText(), self.default_ratio, self.default_resolution,
                                self.default_duration, catalog=catalog_snapshot(self.config_manager))
            self._select_saved_choice(self.default_ratio, defaults.get('aspect_ratio', ''))
            self._select_saved_choice(self.default_resolution, defaults.get('resolution', ''))
            duration = int(defaults.get('duration') or self.default_duration.value())
            if self.default_duration.minimum() <= duration <= self.default_duration.maximum():
                self.default_duration.setValue(duration)
            self.default_poll.setValue(int(defaults.get('poll_interval') or 5))
            del blocked
            self._refresh_default_options()
        finally:
            self._applying_saved = False
    def _select_saved_choice(self, control, value):
        text = str(value or '')
        choices = [control.itemText(index) for index in range(control.count())]
        if text in choices:
            control.setCurrentText(text)
    def _build_appearance(self):
        group = self._group("外观")
        self.appearance_group = group
        from ..palettes import combo_choices
        theme_values, theme_texts = combo_choices()
        self.theme = self._combo(group, "主题", ("appearance", "theme"), theme_values, theme_texts)
        self.theme.setToolTip("9 套主题 + 跟随系统：深色（深空 / 午夜 / 石墨 / 星云 / 深海 / 暮色），浅色（白玉 / 晨雾 / 暖沙）；切换立即生效")
        from ..scaling import SCALE_CHOICES
        host = QWidget()
        row = WrapLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self.ui_scale = ComboBox()
        for value, text in zip(SCALE_CHOICES, (tr('自动'), '100%', '110%', '125%', '130%', '140%', '150%', '160%')):
            self.ui_scale.addItem(text, userData=value)
        saved = str(self.config_manager.config.get('appearance', {}).get('ui_scale', 'auto'))
        self.ui_scale.setCurrentIndex(SCALE_CHOICES.index(saved) if saved in SCALE_CHOICES else 0)
        self.ui_scale.setMinimumWidth(140)
        self.ui_scale.setToolTip(tr('仅调整额外界面倍率，不重复叠加系统DPI；字体与主布局立即生效'))
        self.ui_scale_status = CaptionLabel(tr('正在读取显示器比例...'))
        self.ui_scale_status.setWordWrap(True)
        row.addWidget(self.ui_scale)
        row.addWidget(self.ui_scale_status, 1)
        self.ui_scale_card = CustomSettingCard(tr('界面缩放'), host)
        group.addSettingCard(self.ui_scale_card)
        self.ui_scale.currentIndexChanged.connect(self._ui_scale_changed)
        self.blur = self._switch(group, "高斯模糊玻璃", ("appearance", "blur"), FIF.CLOUD)
        self.blur.setToolTip("开启后窗口叠加系统亚克力模糊（由系统合成器提供，部分环境可能不可用）")
        self.language = self._combo(group, "语言", ("appearance", "language"), ["简体中文", "English"])
        self.reduce_motion = self._switch(group, '减弱动效', ('appearance', 'reduce_motion'), FIF.INFO)
        self.reduce_motion.setToolTip('停止呼吸点与流光等循环动画，并让页面过渡即时完成（无障碍）')
        self.theme.currentIndexChanged.connect(lambda *_: self.appearance_changed.emit())
        self.blur.checkedChanged.connect(lambda *_: self.appearance_changed.emit())
        self.language.currentIndexChanged.connect(lambda *_: self.appearance_changed.emit())
        self.reduce_motion.checkedChanged.connect(lambda *_: self.appearance_changed.emit())

    def _ui_scale_changed(self, index):
        if index < 0:
            return
        saved = self.config_manager.update(('appearance', 'ui_scale'), self.ui_scale.itemData(index))
        self.appearance_changed.emit()
        if not saved:
            from qfluentwidgets import InfoBar
            InfoBar.error(tr('界面缩放保存失败'), tr('本次已生效；请检查配置目录写入权限后重新选择'), parent=self)

    def update_interface_scale_status(self, factors):
        self.ui_scale_status.setText(
            tr('当前总比例 {total:.0%}（系统DPI {dpi:.0%} × 界面 {z:.0%}）').format(**factors))
        self.ui_scale_status.setToolTip(tr('总比例包含系统DPI；字体与主布局立即生效'))

    def _build_license_section(self):
        """账号与许可：状态徽标 + 详情 + 试用进度（到期进度条式视觉）。"""
        card = make_card()
        layout = QVBoxLayout(card); layout.setContentsMargins(20, 16, 20, 16); layout.setSpacing(10)
        top = WrapLayout(spacing=8)
        self.license_dot = Dot(8, '#8B93A3')
        top.addWidget(self.license_dot, 0, Qt.AlignVCenter)
        self.license_badge = label('未激活', 12, '#8B93A3', True)
        self.license_badge.setProperty('statusTone', True)
        top.addWidget(self.license_badge)
        top.addStretch(1)
        activate_button = PushButton(tr('激活'))
        activate_button.clicked.connect(self._activate_license)
        top.addWidget(activate_button)
        layout.addLayout(top)
        self.license_status_label = CaptionLabel(self._license_text())
        self.license_status_label.setWordWrap(True)
        layout.addWidget(self.license_status_label)
        # 试用进度：非试用期自动隐藏。
        self.license_trial_bar = QProgressBar()
        self.license_trial_bar.setTextVisible(False)
        self.license_trial_bar.setFixedHeight(6)
        layout.addWidget(self.license_trial_bar)
        self.license_trial_caption = CaptionLabel('')
        self.license_trial_caption.setProperty('statusTone', True)
        layout.addWidget(self.license_trial_caption)
        self.root.addWidget(card)
        self.root.addWidget(CaptionLabel(f'{APP_NAME} v{APP_VERSION} · ' + tr('多模型并发、产品批处理、定时执行与GitHub同步')))
        self._refresh_license_badge()

    def _refresh_license_badge(self):
        """按当前许可状态刷新徽标与试用进度（激活成功 / 状态变化后调用）。"""
        status = license_status(self.config_manager.config)
        tones = {'active': '#22C55E', 'trial': '#5B8DEF', 'expired': '#E5B94E', 'invalid': '#F56C6C'}
        texts = {'active': '已激活', 'trial': '试用中', 'expired': '试用已结束', 'invalid': '许可无效'}
        tone = tones.get(status['state'], '#8B93A3')
        if hasattr(self, 'license_dot'):
            self.license_dot.set_color(tone)
        if hasattr(self, 'license_badge'):
            self.license_badge.setText(texts.get(status['state'], '未激活'))
            self.license_badge.setTextColor(tone, tone)
        trial = status['state'] == 'trial' and status.get('days_left') is not None
        for widget in (self.license_trial_bar, self.license_trial_caption):
            widget.setVisible(trial)
        if trial:
            days = int(self.config_manager.config.get('license', {}).get('trial_days') or 14)
            left = max(0, int(status['days_left']))
            self.license_trial_bar.setRange(0, max(1, days))
            self.license_trial_bar.setValue(left)
            from .. import materials
            accent = materials.palette()['accent']
            self.license_trial_bar.setStyleSheet(
                'QProgressBar {background:rgba(128,128,128,0.18); border:0; border-radius:3px;}'
                f'QProgressBar::chunk {{background:{accent}; border-radius:3px;}}')
            self.license_trial_caption.setText(f'试用剩余 {left} / {days} 天')
            self.license_trial_caption.setTextColor('#8B93A3', '#8B93A3')

    def _license_text(self):
        status = license_status(self.config_manager.config)
        return tr('许可状态') + '：' + status['detail']

    def _activate_license(self):
        from core.licensing import parse_key
        from qfluentwidgets import Dialog, InfoBar
        dialog = Dialog(tr('许可与激活'), '', self.window())
        line = LineEdit()
        line.setPlaceholderText(tr('输入激活码'))
        line.setClearButtonEnabled(True)
        # QFluentWidgets Dialog uses textLayout (viewLayout belongs to
        # MessageBoxBase). An invalid layout silently broke the activate click.
        dialog.textLayout.addWidget(line)
        dialog.yesButton.setText(tr('激活'))
        if not dialog.exec():
            return
        key = line.text().strip()
        try:
            parse_key(key)
        except ValueError as error:
            InfoBar.error(tr('激活失败'), str(error), parent=self.window(), duration=6500)
            return
        except Exception as error:  # 兜底：解析层任何异常都必须给用户可见反馈，不能静默无响应
            InfoBar.error(tr('激活失败'), str(error).strip() or tr('激活码无法识别，请检查后重试'),
                          parent=self.window(), duration=6500)
            return
        self.config_manager.update(('license', 'key'), key)
        if hasattr(self, 'license_status_label'):
            self.license_status_label.setText(self._license_text())
        self._refresh_license_badge()
        InfoBar.success(tr('激活成功'), tr('已激活'), parent=self.window(), duration=4500)

    def _export_diagnostics(self):
        from PyQt5.QtWidgets import QFileDialog
        from qfluentwidgets import InfoBar
        from core.diagnostics_pack import build_diagnostics_pack
        default = 'Yanlin-diagnostics-' + datetime.now().strftime('%Y%m%d-%H%M') + '.zip'
        path, _ = QFileDialog.getSaveFileName(self, tr('导出诊断包'), default, 'Zip (*.zip)')
        if not path:
            return
        try:
            target = build_diagnostics_pack(self.config_manager, path)
        except Exception as error:
            InfoBar.error(tr('导出失败'), str(error), parent=self.window(), duration=6500)
            return
        self.log_callback(f'诊断包已导出：{target}', 'success')
        InfoBar.success(tr('已导出诊断包'), str(target), parent=self.window(), duration=6000)

    def _build_schedule(self):
        group = self._group('定时执行')
        self.schedule_group = group
        self.schedule_enabled = self._switch(group, '启用定时执行', ('schedule', 'enabled'), FIF.PLAY)
        self.schedule_time = TimePicker(showSeconds=False)
        self.schedule_time.hBoxLayout.setSizeConstraint(QLayout.SetMinimumSize)
        for column in self.schedule_time.columns:
            column.setMinimumWidth(72)
            column.setMaximumWidth(16777215)
            column.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.schedule_time.setTime(QTime.fromString(self._value(('schedule', 'time')), 'HH:mm'))
        group.addSettingCard(CustomSettingCard('定时开始时间', self.schedule_time, FIF.HISTORY))
        self.schedule_mode = self._combo(group, '执行模式', ('schedule', 'mode'), ['once', 'daily'], ['仅一次', '每天重复'])
        self.schedule_after = self._combo(group, '定时队列全部结束后', ('schedule', 'after_finish'), ['keep', 'close'], ['保持运行', '自动关闭软件'])
        self.current_time = CaptionLabel('当前时间：正在读取')
        self.current_time.setWordWrap(True)
        group.addSettingCard(CustomSettingCard('当前时间 / 网络校时', self.current_time, FIF.HISTORY))
        self.schedule_enabled.checkedChanged.connect(lambda *_: self.schedule_changed.emit())
        self.schedule_time.timeChanged.connect(self._schedule_time_changed)
        self.schedule_mode.currentIndexChanged.connect(lambda *_: self.schedule_changed.emit())
        # Completion behavior does not re-arm an already consumed occurrence.

    def _schedule_time_changed(self, value):
        self.config_manager.update(('schedule', 'time'), value.toString('HH:mm'))
        self.schedule_changed.emit()

    def update_schedule_state(self, now, clock_status):
        self.current_time.setText(now.strftime('%Y-%m-%d %H:%M:%S') + '\n' + clock_status)
        enabled = bool(self.config_manager.config['schedule']['enabled'])
        if self.schedule_enabled.isChecked() != enabled:
            blocker = QSignalBlocker(self.schedule_enabled)
            self.schedule_enabled.setChecked(enabled)
            del blocker

    def _build_sync(self):
        group = self._group('GitHub代码同步')
        self.sync_group = group
        self._repository_busy = False
        controls = QWidget()
        layout = QVBoxLayout(controls)
        layout.setContentsMargins(0, 12, 0, 12)
        path_row = WrapLayout()
        self.repo_path_layout = path_row
        self.repo_root_edit = LineEdit()
        self.repo_root_edit.setMinimumWidth(140)
        self.repo_root_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.repo_root_edit.setReadOnly(True)
        self.repo_root_edit.setPlaceholderText(tr('请先选择本地项目Git根目录'))
        self.repo_root_edit.setText(self._sync_repo_root())
        self.repo_root_edit.setToolTip(tr('本地项目Git根目录；只同步main到项目授权origin'))
        self.repo_select_button = PushButton(tr('选择项目文件夹'))
        self.repo_select_button.setToolTip(tr('选择Git根目录，随后自动校验分支和origin；不会修改远端'))
        self.repo_select_button.clicked.connect(self.select_repository_root)
        path_row.addWidget(self.repo_root_edit, 1)
        path_row.addWidget(self.repo_select_button)
        layout.addLayout(path_row)
        self.repo_status_label = CaptionLabel()
        self.repo_status_label.setTextFormat(Qt.PlainText)
        self.repo_status_label.setWordWrap(True)
        layout.addWidget(self.repo_status_label)
        self.sync_button = PushButton(FIF.SYNC, tr('同步到GitHub'))
        self.sync_button.setToolTip(tr('提交已修改的代码并推送main；配置、密钥、视频及日志不会上传'))
        self.sync_button.clicked.connect(self.sync_github)
        layout.addWidget(self.sync_button, 0, Qt.AlignRight)
        self.repo_sync_card = RepositorySyncSettingCard(controls)
        group.addSettingCard(self.repo_sync_card)
        self._repository_status(
            tr('目录尚未校验；选择文件夹后或同步前自动校验main分支与origin。')
            if self._sync_repo_root() else tr('请先在设置→GitHub同步选择本地项目Git根目录'))

    def _sync_repo_root(self):
        configured = self.config_manager.config.get('sync', {}).get('repo_root', '').strip()
        if configured:
            return configured
        return '' if getattr(sys, 'frozen', False) else str(default_repository_root())

    def _repository_status(self, text, valid=None):
        from .. import materials
        self.repo_status_label.setText(text)
        palette = materials.palette()
        color = palette['success'] if valid else palette['error'] if valid is False else palette['text3']
        self.repo_status_label.setTextColor(color, color)
        self.repo_sync_card.fit_content()

    def _show_repository_validation(self, report):
        text = (tr('目录已就绪：{root} · main · origin 已匹配').format(root=report['root'])
                if report['valid'] else '\n'.join(report['errors']))
        self._repository_status(text, report['valid'])

    def _repository_busy_state(self, busy, message=''):
        self._repository_busy = busy
        self.sync_button.setEnabled(not busy)
        self.repo_select_button.setEnabled(not busy)
        self.sync_button.setText(message if busy else tr('同步到GitHub'))
        self.sync_button.setToolTip(
            tr('正在校验目录或同步，完成后恢复') if busy else
            tr('提交已修改的代码并推送main；配置、密钥、视频及日志不会上传'))
        self.repo_select_button.setToolTip(
            tr('正在校验目录或同步，完成后可重新选择') if busy else
            tr('选择Git根目录，随后自动校验分支和origin；不会修改远端'))

    def select_repository_root(self):
        from qfluentwidgets import InfoBar
        if self._repository_busy:
            InfoBar.warning(tr('GitHub同步'), tr('正在校验目录或同步，请等待完成再操作'), parent=self)
            return
        self._repository_busy_state(True, tr('正在选择目录...'))
        try:
            selected = QFileDialog.getExistingDirectory(self, tr('选择本地项目Git根目录'), self._sync_repo_root())
        finally:
            self._repository_busy_state(False)
        if selected:
            saved = self.config_manager.update(('sync', 'repo_root'), selected)
            self.repo_root_edit.setText(selected)
            if not saved:
                InfoBar.error(tr('项目目录保存失败'), tr('本次可校验所选目录，但配置未能保存；请检查配置目录写入权限后重新选择'),
                              parent=self, duration=7000)
            self._start_repository_job(sync=False)

    def sync_github(self):
        self._start_repository_job(sync=True)

    def _start_repository_job(self, *, sync):
        from qfluentwidgets import InfoBar
        if self._repository_busy:
            InfoBar.warning(tr('GitHub同步'), tr('正在校验目录或同步，请等待完成再操作'), parent=self)
            return
        root = self._sync_repo_root()
        if not root:
            message = tr('请先在设置→GitHub同步选择本地项目Git根目录')
            self._repository_status(message, False)
            InfoBar.warning(tr('GitHub同步'), message, parent=self, duration=7000)
            return
        self._repository_busy_state(True, tr('正在同步...') if sync else tr('正在校验目录...'))
        self._repository_status(tr('正在后台校验项目Git根目录、main分支与origin...'))
        config = copy.deepcopy(self.config_manager.config)
        def work():
            secrets = repository_secrets(config)
            report = validate_repository_root(root, secrets=secrets)
            outcome = dict(validation=report)
            if sync and report['valid']:
                try:
                    outcome['result'] = RepositorySync(
                        root=report['root'], secrets=secrets, log=self.jobs.log_message.emit).sync()
                except Exception as error:
                    outcome['error'] = str(error)
            return outcome
        def failed(message):
            try:
                self.log_callback(message, 'error')
                InfoBar.error(tr('GitHub同步失败'), message, parent=self, duration=7000)
            finally:
                self._repository_busy_state(False)
        def done(outcome):
            try:
                report = outcome['validation']
                self._show_repository_validation(report)
                if not report['valid']:
                    if sync:
                        failed('\n'.join(report['errors']))
                elif 'error' in outcome:
                    failed(outcome['error'])
                elif 'result' in outcome:
                    InfoBar.success(tr('GitHub同步成功'), tr('已同步到GitHub，最新commit: {commit}').format(commit=outcome['result']['commit']),
                                    parent=self, duration=5000)
            finally:
                self._repository_busy_state(False)
        try:
            self.jobs.start(work, done, failed)
        except Exception as error:
            failed(str(error))

    def _model_pool_config_key(self, kind):
        return {'video': 'model_pool', 'chat': 'chat_model_pool', 'image': 'image_model_pool'}[kind]

    def _pool_model_value(self, combo, kind):
        value = combo.currentText() if kind == 'video' else combo.value()
        known_ids = None if kind == 'video' else catalog_snapshot(
            self.config_manager, kind).keys()
        return normalize_model_id(value, known_ids)

    def _add_model_row(self, name, enabled, status, persist=True, kind='video'):
        row = make_card()
        layout = WrapLayout(row)
        layout.setContentsMargins(20, 10, 20, 10)
        layout.setSpacing(8)
        check = CheckBox("启用" if kind == 'video' else tr("启用"))
        check.setChecked(enabled)
        combo = ModelComboBox() if kind == 'video' else ModelPickComboBox()
        combo.setMinimumWidth(160 if kind == 'video' else 140)
        combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        if kind == 'video':
            combo.set_models(catalog_snapshot(self.config_manager), name, keep_missing=True)
        else:
            combo.set_models(catalog_snapshot(self.config_manager, kind), name, kind=kind)
        state = CaptionLabel(status if kind == 'video' else tr(status))
        state.setProperty('statusTone', True)
        color = "#67c23a" if status == "健康" else "#e6a23c"
        state.setTextColor(color, color)
        dot = Dot(7, color)
        state.dot = dot
        remove = TransparentToolButton(FIF.DELETE)
        remove.setToolTip("删除模型" if kind == 'video' else tr("删除模型"))
        for widget in (check, combo, dot, state, remove):
            layout.addWidget(widget, 1 if widget is combo else 0)
        self.pool_model_layouts[kind].addWidget(row)
        self.pool_rows[kind].append((row, check, combo, state))
        check.stateChanged.connect(lambda *_args, model_kind=kind: self._persist_models(kind=model_kind))
        combo.currentTextChanged.connect(lambda *_args, model_kind=kind: self._persist_models(kind=model_kind))
        remove.clicked.connect(lambda checked=False, model_row=row, model_kind=kind:
                               self._remove_model_row(model_row, model_kind))
        if persist:
            style_controls(row)
            self._persist_models(kind=kind)
            self._fit_pool_models_card(kind)

    def _remove_model_row(self, row, kind='video'):
        # Drop Python references before deleteLater; future edits must not touch deleted Qt objects.
        self.pool_rows[kind][:] = [entry for entry in self.pool_rows[kind] if entry[0] is not row]
        self.pool_model_layouts[kind].removeWidget(row)
        row.deleteLater()
        self._persist_models(kind=kind)
        self._fit_pool_models_card(kind)

    def _persist_models(self, *_args, kind='video'):
        config_key = self._model_pool_config_key(kind)
        previous = {}
        for model in self._value((config_key, 'models')):
            if not isinstance(model, dict):
                continue
            name = normalize_model_id(model.get('name'))
            if not name:
                continue
            if name not in previous:
                previous[name] = dict(model, name=name)
            else:
                previous[name]['enabled'] = bool(previous[name].get('enabled')) or bool(model.get('enabled'))
        catalog = catalog_snapshot(self.config_manager, kind) if kind != 'video' else None
        merged = {}
        models = []
        for _, check, combo, _ in self.pool_rows[kind]:
            name = self._pool_model_value(combo, kind)
            if not name:
                continue
            displayed = combo.text().strip() if hasattr(combo, 'text') else combo.currentText().strip()
            if kind != 'video' and displayed != name:
                blocker = QSignalBlocker(combo)
                combo.set_value(name)
                del blocker
            enabled = check.isChecked()
            if kind != 'video':
                record = catalog.get(name)
                enabled = enabled and bool(record and record.get('available', True)
                                           and record.get('kind') == kind)
            if name in merged:
                merged[name]['enabled'] = bool(merged[name].get('enabled')) or enabled
                continue
            models.append(dict(previous.get(name, {'status': '健康'}), name=name,
                               enabled=enabled))
            merged[name] = models[-1]
        self.config_manager.update((config_key, 'models'), models)
        self.refresh_pool_state()

    def update_pool_state(self, snapshot):
        """Merge runtime health only; user model selection belongs to the next batch."""
        updates = {item['name']: item for item in snapshot}
        models = copy.deepcopy(self._value(('model_pool', 'models')))
        changed = False
        for model in models:
            state = updates.get(model['name'])
            if state is None:
                continue
            for key in ('status', 'cooldown_until', 'consecutive_failures'):
                if key in state and model.get(key) != state[key]:
                    model[key] = state[key]; changed = True
        if changed:
            self.config_manager.update(('model_pool', 'models'), models)
        self.refresh_pool_state()

    def stop_timers(self):
        """窗口关闭时停止页面内的周期刷新，避免退出流程中仍在轮询模型池状态。"""
        try:
            self.pool_timer.stop()
        except Exception:
            pass

    def refresh_pool_state(self):
        now = time.time()
        models = copy.deepcopy(self._value(('model_pool', 'models')))
        changed = False
        for model in models:
            if model.get('status') == '冷却中':
                if not model.get('cooldown_until'):
                    model['cooldown_until'] = now + self._value(('model_pool', 'cooldown'))
                    changed = True
                elif model['cooldown_until'] <= now:
                    model.update(status='健康', cooldown_until=0, consecutive_failures=0)
                    changed = True
        if changed:
            self.config_manager.update(('model_pool', 'models'), models)
        states = {model['name']: model for model in models}
        self._refresh_pool_kind('video', catalog_snapshot(self.config_manager), states, now)
        for kind in ('chat', 'image'):
            config_key = self._model_pool_config_key(kind)
            catalog = catalog_snapshot(self.config_manager, kind)
            other_models = copy.deepcopy(self._value((config_key, 'models')))
            changed = False
            for model in other_models:
                record = catalog.get(model.get('name'))
                valid = bool(record and record.get('available', True)
                             and record.get('kind') == kind)
                if not valid and model.get('enabled'):
                    model['enabled'] = False
                    changed = True
            if changed:
                self.config_manager.update((config_key, 'models'), other_models)
            other_states = {model['name']: model for model in other_models}
            self._refresh_pool_kind(kind, catalog, other_states, now)

    def _refresh_pool_kind(self, kind, catalog, states, now):
        for _, check, combo, status_label in self.pool_rows[kind]:
            name = self._pool_model_value(combo, kind)
            model = states.get(name, {})
            record = catalog.get(name)
            if kind == 'video':
                if record is None or not usable(record):
                    status = '已下架' if record is None else '非视频模型' if record.get('kind') != 'video' else '协议待确认'
                    self._set_pool_row_state(check, status_label, status, '#92929b', False)
                    check.setToolTip(tr('该模型当前不可用：已下架、不是视频模型或协议待确认，请同步模型或选择其他模型'))
                    continue
                cooling = model.get('status') == '冷却中'
                remaining = max(0, math.ceil(model.get('cooldown_until', 0) - now))
                status = f'冷却中 {remaining}秒' if cooling else '健康'
                self._set_pool_row_state(check, status_label, status,
                                         '#e6a23c' if cooling else '#67c23a', True)
                check.setToolTip(tr('启用或停用此模型'))
                continue

            if record is None or not record.get('available', True):
                status = '已下架'
                valid = False
            elif record.get('kind') != kind:
                status = '非文本模型' if kind == 'chat' else '非图片模型'
                valid = False
            else:
                status, valid = '健康', True
            if not valid and check.isChecked():
                blocker = QSignalBlocker(check)
                check.setChecked(False)
                del blocker
            self._set_pool_row_state(check, status_label, status,
                                     '#67c23a' if valid else '#92929b', valid, translate=True)
            check.setToolTip(tr('启用或停用此模型') if valid else tr('请同步模型或选择对应类型的模型'))

    @staticmethod
    def _set_pool_row_state(check, status_label, status, color, enabled, translate=False):
        status_label.setText(tr(status) if translate else status)
        status_label.setTextColor(color, color)
        dot = getattr(status_label, 'dot', None)
        if dot is not None:
            dot.set_color(color)
        check.setEnabled(enabled)

    def refresh_catalog(self, *_):
        catalog = catalog_snapshot(self.config_manager)
        self.default_model.set_models(catalog, self._value(('defaults', 'model')))
        if self.default_model.currentText():
            self.config_manager.update(('defaults', 'model'), self.default_model.currentText())
        self.fallback_model.set_models(catalog, self._value(('prompt_detection', 'fallback_model')), automatic=True)
        self.config_manager.update(('prompt_detection', 'fallback_model'), self.fallback_model.currentText())
        renamed = False
        for _, _, combo, _ in self.model_rows:
            old_name = combo.currentText()
            combo.set_models(catalog, combo.currentText(), keep_missing=True)
            renamed = renamed or old_name != combo.currentText()
        if renamed:
            self._persist_models()
        for kind in ('chat', 'image'):
            records = catalog_snapshot(self.config_manager, kind)
            for _, _, combo, _ in self.pool_rows[kind]:
                combo.set_models(records, self._pool_model_value(combo, kind), kind=kind)
        self._refresh_default_options()
        self.refresh_pool_state()
        self.refresh_sync_state()

    def refresh_sync_state(self):
        controller = getattr(self.config_manager, 'model_catalog_controller', None)
        if controller is None:
            return
        self.sync_models_button.setEnabled(not controller.syncing)
        self.sync_models_button.setText(tr('正在同步...') if controller.syncing else tr('同步上游模型'))
        self.sync_models_button.setToolTip(
            tr('模型同步进行中，完成后恢复') if controller.syncing else
            tr('从上游刷新可用模型列表'))
        fetched = controller.catalog.fetched_at
        text = datetime.fromtimestamp(fetched).strftime('%Y-%m-%d %H:%M:%S') if fetched else '尚未同步'
        self.last_model_sync.setText('上次同步：' + text)

    def model_sync_finished(self, success, count, message):
        from qfluentwidgets import InfoBar
        (InfoBar.success if success else InfoBar.warning)('模型同步', message, parent=self, duration=4500)
        self.refresh_sync_state()

    def refresh_task_settings(self, *_):
        for widget, key in ((self.max_retry, 'max_retries'), (self.fail_threshold, 'failure_skip_threshold')):
            blocker = QSignalBlocker(widget)
            widget.setValue(self._value(('task_strategy', key)))
            del blocker

    def test_connection(self):
        self._repair_upload_fields(self.upload_url, self.upload_key)
        self._save_normalized_base_url()
        config = copy.deepcopy(self.config_manager.config)
        config.setdefault('api', {})['upload_url'] = self.upload_url.text().strip()
        config['api']['upload_api_key'] = self.upload_key.text().strip()
        self.test_button.setEnabled(False)
        self.test_button.setText(tr('测试中'))
        self.test_button.setToolTip(tr('正在测试连接，完成后恢复'))

        def check():
            results = {}
            for capability in ('video', 'chat', 'image'):
                line = resolve_line(config, capability)
                try:
                    results[capability] = probe_line(
                        line.get('base_url', ''), line.get('api_key', ''), capability,
                        models=catalog_snapshot(self.config_manager, capability))
                except Exception as error:
                    results[capability] = dict(ok=False, level='error',
                                               message=self._redact_probe_error(error), counts={})
            try:
                results['upload'] = probe_upload(config.get('api', {}))
            except Exception as error:
                results['upload'] = dict(ok=False, level='error',
                                         message=self._redact_probe_error(error))
            results['ok'] = all(results[key].get('ok') for key in ('video', 'chat', 'image'))
            return results

        def finish(result):
            self.test_button.setEnabled(True)
            self.test_button.setText(tr('测试连接'))
            self.test_button.setToolTip(tr('分别检查视频、文本和图片 API；上传 Token 未配置时跳过上传测试'))
            self.last_connection_result = result
            messages = []
            raw_details = []
            for name, title in (('video', tr('视频 API')), ('chat', tr('AI分析（文本）API')),
                                ('image', tr('图片 API')), ('upload', tr('上传 API'))):
                item = result.get(name, {})
                capability = name if name in ('video', 'chat', 'image') else 'upload'
                message = self._format_probe_line(title, item, capability)
                raw = self._format_probe_detail(title, item) + self._raw_probe_metadata(item)
                if item.get('level') == 'warning':
                    level = 'warning'
                else:
                    level = 'success' if item.get('ok') else 'error'
                self.log_callback(raw, level)
                messages.append(message)
                raw_details.append(raw)
            self._notice('\n'.join(messages), bool(result.get('ok')),
                         level='warning' if any(item.get('level') == 'warning'
                                                for item in result.values() if isinstance(item, dict))
                         and result.get('ok') else None,
                         tooltip=self._dedupe_probe_lines('\n'.join(raw_details)))

        def failed(message):
            clean = self._redact_probe_error(message)
            result = dict(ok=False,
                          video=dict(ok=False, message=clean),
                          chat=dict(ok=False, message=tr('测试未完成')),
                          image=dict(ok=False, message=tr('测试未完成')),
                          upload=dict(ok=True, level='warning', message=tr('测试未完成')))
            finish(result)
        try:
            self.jobs.start(check, finish, failed)
        except Exception as error:
            failed(error)

    def _notice(self, message, success=True, level=None, tooltip=None):
        tone = level or ('success' if success else 'error')
        self.log_callback(message, tone)
        return self._show_result_infobar(tr('测试连接'), message, tone, tooltip=tooltip)

    def _show_result_infobar(self, title, message, tone, tooltip=None):
        icon = {'success': InfoBarIcon.SUCCESS, 'warning': InfoBarIcon.WARNING,
                'error': InfoBarIcon.ERROR}.get(tone, InfoBarIcon.ERROR)
        duration = {'success': 5000, 'warning': 7000, 'error': 9000}.get(tone, 9000)
        bar = _ResultInfoBar(icon, title, self._redact_probe_error(message), orient=Qt.Vertical,
                             parent=self.window(), duration=duration, position=InfoBarPosition.TOP)
        full_detail = self._redact_probe_error(tooltip if tooltip is not None else message)
        bar.titleLabel.setToolTip(full_detail)
        bar.contentLabel.setToolTip(full_detail)
        bar.show()
        return bar

    @staticmethod
    def _toggle_secret(field, button):
        visible = field.echoMode() == LineEdit.Password
        field.setEchoMode(LineEdit.Normal if visible else LineEdit.Password)
        button.setIcon(FIF.HIDE if visible else FIF.VIEW)
