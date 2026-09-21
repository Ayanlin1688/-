"""Fluent setting cards backed by the application's JSON config."""

from PyQt5.QtCore import Qt, QPoint, QSize, pyqtSignal, QTime, QSignalBlocker, QTimer
from PyQt5.QtWidgets import QHBoxLayout, QProgressBar, QScrollArea, QVBoxLayout, QWidget, QPushButton, QSizePolicy, QStackedWidget
from qfluentwidgets import (
    CheckBox, ComboBox, ComboBoxSettingCard, FluentIcon as FIF, LineEdit,
    OptionsConfigItem, OptionsValidator, PushButton, SettingCard, SettingCardGroup,
    SpinBox, SwitchSettingCard, CaptionLabel, TitleLabel, TransparentToolButton, ScrollArea, TimePicker,
)
from ..components.custom_widgets import make_card
from ..widgets.workspace_surface import label
from ..widgets.status_dot import Dot, dot_icon
from ..theme import SettingSurface, style_controls
from core.background import BackgroundJobs
from core.api_client import ApiClient
from core.image_uploader import ImageUploader
from core.model_parameters import MODELS
from core.repository_sync import RepositorySync, GITHUB_REPOSITORY, repository_secrets
from core.version import APP_NAME, APP_VERSION
from core.licensing import license_status
from core.i18n import tr
from ..components.model_options import apply_model_options
from ..components.model_selector import ModelComboBox, catalog_snapshot, usable
from datetime import datetime
import copy
import math
import time


class JsonComboBoxSettingCard(SettingSurface, ComboBoxSettingCard):
    """Use Fluent's combo card without its unrelated config/config.json writes."""
    def _onCurrentIndexChanged(self, index):
        self.configItem.value = self.comboBox.itemData(index)

    def setValue(self, value):
        if value in self.optionToText:
            self.comboBox.setCurrentText(self.optionToText[value])


class StudioSwitchSettingCard(SettingSurface, SwitchSettingCard):
    pass


class CustomSettingCard(SettingSurface, SettingCard):
    # 1.11.3 has no LineEditSettingCard or SpinBoxSettingCard: compose its
    # SettingCard with its LineEdit/SpinBox to retain the Fluent design.
    def __init__(self, title, control, icon=FIF.SETTING, parent=None):
        super().__init__(icon, title, parent=parent)
        self.hBoxLayout.addWidget(control, 1, Qt.AlignVCenter)
        self.hBoxLayout.addSpacing(20)
        self.setMinimumHeight(64)


class SettingsPage(QWidget):
    debug_mode_changed = pyqtSignal(bool)
    api_credentials_changed = pyqtSignal()
    schedule_changed = pyqtSignal()
    task_settings_changed = pyqtSignal()
    detection_changed = pyqtSignal()
    appearance_changed = pyqtSignal()

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
        self.jobs = BackgroundJobs(self)
        self.jobs.log_message.connect(self.log_callback)
        self.model_rows = []
        self.groups = []
        self._pages = {}
        self._page_order = []
        self._rail_buttons = {}
        self._page_buttons = {}
        self._station_nav_buttons = {}
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
        shell.addWidget(self.rail)
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
        layout = QVBoxLayout(content); layout.setContentsMargins(28, 22, 28, 30); layout.setSpacing(16)
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
        group.cardLayout.setSpacing(16)
        self.root.addWidget(group)
        self.groups.append(group)
        return group

    # ------------------------------------------------------------------
    # 左侧分类导航（滚动锚点）
    # ------------------------------------------------------------------
    def _build_rail(self):
        rail = QWidget(); rail.setObjectName('settingsRail'); rail.setFixedWidth(184)
        rail.setStyleSheet(
            '#settingsRail {background:transparent; border-right:1px solid rgba(255,255,255,0.07);}'
            '#settingsRail QPushButton {border:0; border-radius:8px; text-align:left; padding:7px 12px;'
            ' color:#9CA3AF; font-size:13px; background:transparent;}'
            '#settingsRail QPushButton:hover {background:rgba(255,255,255,0.06); color:#E5E7EB;}'
            '#settingsRail QPushButton[railActive="true"] {background:rgba(91,141,239,0.14); color:#FFFFFF; font-weight:600;}')
        layout = QVBoxLayout(rail); layout.setContentsMargins(16, 22, 14, 26); layout.setSpacing(4)
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
            head.setStyleSheet('color:rgba(237,237,240,0.38); font-size:11px; padding-left:6px;')
            layout.addWidget(head); layout.addSpacing(2)

        # —— 账号与许可 ——
        add_head('账号与许可')
        add_entry('许可与激活', 'license')
        layout.addSpacing(10)
        # —— 外观 ——
        add_head('外观')
        add_entry('外观与语言', 'appearance')
        layout.addSpacing(10)
        # —— 模型与生成 ——
        add_head('模型与生成')
        add_entry('模型池', 'pool')
        add_entry('默认参数', 'defaults')
        layout.addSpacing(10)
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
        layout.addSpacing(10)
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
        layout = QVBoxLayout(card); layout.setContentsMargins(20, 18, 20, 18); layout.setSpacing(10)
        name = LineEdit(); name.setText(station.get('name', '')); name.setPlaceholderText('名称，例如：主线路 · 国内')
        base = LineEdit(); base.setText(station.get('base_url', '')); base.setPlaceholderText('API Base URL')
        key_edit = LineEdit(); key_edit.setText(station.get('api_key', '')); key_edit.setEchoMode(LineEdit.Password); key_edit.setPlaceholderText('API Key')
        upload = LineEdit(); upload.setText(station.get('upload_url', '')); upload.setPlaceholderText('上传接口 URL（可留空）')
        upload_key = LineEdit(); upload_key.setText(station.get('upload_api_key', '')); upload_key.setEchoMode(LineEdit.Password); upload_key.setPlaceholderText('上传 Token（可留空）')
        for caption_text, field in (('名称', name), ('API Base URL', base), ('API Key', key_edit),
                                    ('上传接口 URL', upload), ('上传 Token', upload_key)):
            row = QHBoxLayout(); row.setSpacing(10)
            lbl = CaptionLabel(caption_text); lbl.setFixedWidth(110)
            field.setFixedHeight(32)
            row.addWidget(lbl, 0, Qt.AlignVCenter); row.addWidget(field, 1)
            layout.addLayout(row)
        caps = QHBoxLayout(); caps.setSpacing(14)
        caps_label = CaptionLabel('能力标签'); caps_label.setFixedWidth(110)
        caps.addWidget(caps_label, 0, Qt.AlignVCenter)
        boxes = {}
        for key2, text in (('video', '视频'), ('llm', '语言'), ('image', '生图')):
            box = CheckBox(text); box.setChecked(key2 in (station.get('capabilities') or []))
            boxes[key2] = box; caps.addWidget(box)
        caps.addStretch(1); layout.addLayout(caps)
        buttons = QHBoxLayout(); buttons.setSpacing(8)
        save = PushButton('保存修改'); use = PushButton('设为当前'); remove = PushButton('删除该中转站')
        save.setFixedHeight(32); use.setFixedHeight(32); remove.setFixedHeight(32)
        # 破坏性操作按危险语义标色（当前设置中仅此一处破坏性操作）。
        remove.setStyleSheet('QPushButton {color:#E5484D; border:1px solid rgba(229,72,77,0.45); border-radius:8px; background:transparent; padding:0 12px;}'
                             'QPushButton:hover {background:rgba(229,72,77,0.10); border-color:rgba(229,72,77,0.70);}')
        buttons.addWidget(save); buttons.addWidget(use); buttons.addStretch(1); buttons.addWidget(remove)
        layout.addLayout(buttons)
        self.root.addWidget(card)
        save.clicked.connect(lambda checked=False, s=sid, n=name, b=base, k=key_edit, u=upload, uk=upload_key, bx=boxes:
                             self._save_station_page(s, n, b, k, u, uk, bx))
        use.clicked.connect(lambda checked=False, s=sid: self._set_current_station(s))
        remove.clicked.connect(lambda checked=False, s=sid: self._delete_station(s))

    def _save_station_page(self, sid, name, base, key_edit, upload, upload_key, boxes):
        name_text = name.text().strip()
        if not name_text:
            self.log_callback('中转站名称不能为空', 'warning')
            return
        stations = self._stations()
        for station in stations:
            if station.get('id') == sid:
                station.update(name=name_text, base_url=base.text().strip(), api_key=key_edit.text().strip(),
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

    def _build_stations(self):
        group = self._group('中转站')
        self.stations_group = group
        card = make_card(); self.stations_card = card
        layout = QVBoxLayout(card); layout.setContentsMargins(20, 18, 20, 18); layout.setSpacing(10)
        hint = CaptionLabel('每个中转站独立成一项（左侧导航可直达）；「设为当前」后任务走该线路；可标记视频 / 语言 / 生图能力。')
        hint.setWordWrap(True); layout.addWidget(hint)
        self.station_list = QVBoxLayout(); self.station_list.setSpacing(8)
        holder = QWidget(); holder.setLayout(self.station_list); layout.addWidget(holder)
        buttons = QHBoxLayout(); buttons.setSpacing(8)
        self.add_station_button = PushButton(FIF.ADD, '新增中转站')
        self.add_station_button.clicked.connect(lambda: self._open_station_editor(None))
        self.capture_station_button = PushButton(FIF.SAVE, '把当前 API 配置保存为中转站')
        self.capture_station_button.clicked.connect(self._capture_current_station)
        buttons.addWidget(self.add_station_button); buttons.addWidget(self.capture_station_button); buttons.addStretch(1)
        layout.addLayout(buttons)
        group.addSettingCard(card)
        self.station_rows = []
        self._editing_station_id = ''
        self._render_stations()

    def _build_station_editor_page(self):
        """新增中转站页：保留共用编辑器字段（与旧版完全一致）。"""
        card = make_card()
        layout = QVBoxLayout(card); layout.setContentsMargins(20, 18, 20, 18); layout.setSpacing(10)
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
        caps = QHBoxLayout(); caps.setSpacing(14)
        self.station_caps = {}
        for key, text in (('video', '视频'), ('llm', '语言'), ('image', '生图')):
            box = CheckBox(text); self.station_caps[key] = box; caps.addWidget(box)
        caps.addStretch(1); editor.addLayout(caps)
        editor_buttons = QHBoxLayout(); editor_buttons.setSpacing(8)
        save = PushButton('保存'); save.clicked.connect(self._save_station_editor)
        cancel = PushButton('取消'); cancel.clicked.connect(self._close_station_editor)
        editor_buttons.addWidget(save); editor_buttons.addWidget(cancel); editor_buttons.addStretch(1)
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
        cap_texts = {'video': '视频', 'llm': '语言', 'image': '生图'}
        for station in stations:
            row = make_card()
            row_layout = QVBoxLayout(row); row_layout.setContentsMargins(14, 10, 14, 10); row_layout.setSpacing(6)
            is_active = station.get('id') == active and bool(active)
            line1 = QHBoxLayout(); line1.setSpacing(8)
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
            line2 = QHBoxLayout(); line2.setSpacing(10)
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

    def _close_station_editor(self):
        self._editing_station_id = ''
        self._switch_page('stations', highlight=True)

    def _save_station_editor(self):
        name = self.station_name.text().strip()
        if not name:
            self.log_callback('中转站名称不能为空', 'warning')
            return
        capabilities = [key for key, box in self.station_caps.items() if box.isChecked()]
        payload = dict(name=name, base_url=self.station_base.text().strip(), api_key=self.station_key.text().strip(),
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
        station = dict(id=f'st-{int(time.time()*1000)}', name=candidate, base_url=api.get('base_url', ''),
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
        edit.textChanged.connect(lambda v: self.config_manager.update(path, v))
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        if secret:
            edit.setEchoMode(LineEdit.Password)
            reveal = TransparentToolButton(FIF.VIEW)
            reveal.setToolTip("显示或隐藏密钥")
            reveal.clicked.connect(lambda: self._toggle_secret(edit, reveal))
            row.addWidget(reveal)
        if path == ("api", "api_key"):
            self.test_button = PushButton(FIF.CONNECT, "测试连接")
            self.test_button.setToolTip('分别检查视频 API 和上传 API；上传一张微型测试图片，不创建视频任务')
            self.test_button.clicked.connect(self.test_connection)
            row.addWidget(self.test_button)
        group.addSettingCard(CustomSettingCard(title, host, FIF.LINK))
        return edit

    def _combo(self, group, title, path, choices, texts=None):
        item = OptionsConfigItem(path[0], path[1], self._value(path), OptionsValidator(choices))
        card = JsonComboBoxSettingCard(item, FIF.SETTING, title, texts=texts or choices)
        card.comboBox.setMinimumWidth(200)
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
        control.setFixedWidth(200)
        control.valueChanged.connect(lambda v: self.config_manager.update(path, v))
        group.addSettingCard(CustomSettingCard(title, control))
        return control

    def _model_combo(self, group, title, path, automatic=False):
        combo = ModelComboBox()
        combo.setMinimumWidth(240)
        combo.setMaximumWidth(560)
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
        self.base_url = self._line(group, "API Base URL", ("api", "base_url"))
        self.api_key = self._line(group, "API Key", ("api", "api_key"), True)
        self.upload_url = self._line(group, "图床上传 URL", ("api", "upload_url"))
        self.upload_key = self._line(group, '图床 API Key（上传 Token）', ('api', 'upload_api_key'), True)
        self.upload_key.setPlaceholderText('服务商签发的素材上传 Token')
        self.upload_key.setToolTip('默认图床使用 X-Upload-Token 和 Bearer 头；视频 API Key 不保证具有上传权限')
        for field in (self.base_url, self.api_key, self.upload_url, self.upload_key):
            field.textChanged.connect(self._mirror_api_to_station)
        self._refresh_api_group_title()
        self.auto_upload = self._switch(group, "自动上传缺失图片", ("api", "auto_upload_missing"), FIF.CLOUD)
        self.debug_mode = self._switch(group, '调试模式', ('diagnostics', 'debug_mode'), FIF.INFO)
        self.debug_mode.setToolTip('记录参考图、提示词和脱敏请求体；额外下载上传后的图片检查尺寸和格式')
        self.debug_mode.checkedChanged.connect(self.debug_mode_changed)
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        self.last_model_sync = CaptionLabel('尚未同步')
        self.sync_models_button = PushButton(FIF.SYNC, '同步上游模型')
        row.addWidget(self.last_model_sync, 1)
        row.addWidget(self.sync_models_button)
        group.addSettingCard(CustomSettingCard('上游模型目录', host, FIF.SYNC))
        diag_host = QWidget()
        diag_row = QHBoxLayout(diag_host)
        diag_row.setContentsMargins(0, 0, 0, 0)
        self.diagnostics_hint = CaptionLabel(tr('将日志、脱敏配置与环境信息打包为 zip，便于排查问题'))
        self.diagnostics_hint.setWordWrap(True)
        self.diagnostics_button = PushButton(FIF.SAVE, tr('导出诊断包'))
        self.diagnostics_button.clicked.connect(self._export_diagnostics)
        diag_row.addWidget(self.diagnostics_hint, 1)
        diag_row.addWidget(self.diagnostics_button)
        group.addSettingCard(CustomSettingCard(tr('诊断包'), diag_host, FIF.SAVE))

    def _build_pool(self):
        group = self._group("模型池")
        self.pool_group = group
        self.pool_enabled = self._switch(group, '启用模型池', ('model_pool', 'enabled'), FIF.SYNC)
        self.pool_enabled.setToolTip('关闭时使用工作台所选单模型。运行期间修改设置将在下一批任务生效。')
        self.strategy = self._combo(group, "调用策略", ("model_pool", "strategy"), ["轮询", "随机", "优先级"])
        self.failover = self._switch(group, "自动故障转移", ("model_pool", "auto_failover"), FIF.SYNC)
        self.cooldown = self._spin(group, "冷却时间（秒）", ("model_pool", "cooldown"), 1, 3600)
        card = make_card()
        self.models_card = card
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        hint = CaptionLabel('模型列表 · 列表顺序即优先级；修改在下一批生效')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.model_layout = QVBoxLayout()
        self.model_layout.setSpacing(8)
        layout.addLayout(self.model_layout)
        for model in self._value(("model_pool", "models")):
            self._add_model_row(model["name"], model["enabled"], model["status"], persist=False)
        self.add_model = PushButton(FIF.ADD, "添加模型")
        self.add_model.clicked.connect(lambda: self._add_model_row("video-v3", True, "健康"))
        layout.addWidget(self.add_model, 0, Qt.AlignLeft)
        card.setFixedHeight(layout.sizeHint().height())
        group.addSettingCard(card)

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
        self._refresh_default_options()

    def _refresh_default_options(self, *_):
        values = apply_model_options(self.default_model.currentText(), self.default_ratio, self.default_resolution, self.default_duration,
                                     catalog=catalog_snapshot(self.config_manager))
        for key, value in values.items():
            if self.config_manager.config['defaults'][key] != value:
                self.config_manager.update(('defaults', key), value)

    def _build_appearance(self):
        group = self._group("外观")
        self.appearance_group = group
        from ..palettes import combo_choices
        theme_values, theme_texts = combo_choices()
        self.theme = self._combo(group, "主题", ("appearance", "theme"), theme_values, theme_texts)
        self.theme.setToolTip("9 套主题 + 跟随系统：深色（深空 / 午夜 / 石墨 / 星云 / 深海 / 暮色），浅色（白玉 / 晨雾 / 暖沙）；切换立即生效")
        self.blur = self._switch(group, "高斯模糊玻璃", ("appearance", "blur"), FIF.CLOUD)
        self.blur.setToolTip("开启后窗口叠加系统亚克力模糊（由系统合成器提供，部分环境可能不可用）")
        self.language = self._combo(group, "语言", ("appearance", "language"), ["简体中文", "English"])
        self.reduce_motion = self._switch(group, '减弱动效', ('appearance', 'reduce_motion'), FIF.INFO)
        self.reduce_motion.setToolTip('停止呼吸点与流光等循环动画，并让页面过渡即时完成（无障碍）')
        self.theme.currentIndexChanged.connect(lambda *_: self.appearance_changed.emit())
        self.blur.checkedChanged.connect(lambda *_: self.appearance_changed.emit())
        self.language.currentIndexChanged.connect(lambda *_: self.appearance_changed.emit())
        self.reduce_motion.checkedChanged.connect(lambda *_: self.appearance_changed.emit())

    def _build_license_section(self):
        """账号与许可：状态徽标 + 详情 + 试用进度（到期进度条式视觉）。"""
        card = make_card()
        layout = QVBoxLayout(card); layout.setContentsMargins(20, 16, 20, 16); layout.setSpacing(10)
        top = QHBoxLayout(); top.setSpacing(8)
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
        self.sync_button = PushButton(FIF.SYNC, '同步到GitHub')
        self.sync_button.setToolTip('提交已修改的代码并推送main；配置、密钥、视频及日志不会上传')
        self.sync_button.clicked.connect(self.sync_github)
        group.addSettingCard(CustomSettingCard('GitHub 私人仓库同步', self.sync_button, FIF.SYNC))
    def sync_github(self):
        self.sync_button.setEnabled(False)
        self.sync_button.setText('正在同步...')
        config = copy.deepcopy(self.config_manager.config)
        def work():
            secrets = repository_secrets(config)
            return RepositorySync(secrets=secrets, log=self.jobs.log_message.emit).sync()
        def reset():
            self.sync_button.setEnabled(True); self.sync_button.setText('同步到GitHub')
        def done(result):
            reset()
            from qfluentwidgets import InfoBar
            InfoBar.success('GitHub同步成功', '已同步到GitHub，最新commit: ' + result['commit'][:12], parent=self, duration=5000)
        def failed(message):
            reset()
            self.log_callback(message, 'error')
            from qfluentwidgets import InfoBar
            InfoBar.error('GitHub同步失败', message, parent=self, duration=7000)
        self.jobs.start(work, done, failed)

    def _add_model_row(self, name, enabled, status, persist=True):
        row = make_card()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(20, 10, 20, 10)
        check = CheckBox("启用")
        check.setChecked(enabled)
        combo = ModelComboBox()
        combo.set_models(catalog_snapshot(self.config_manager), name, keep_missing=True)
        state = CaptionLabel(status)
        state.setProperty('statusTone', True)
        color = "#67c23a" if status == "健康" else "#e6a23c"
        state.setTextColor(color, color)
        dot = Dot(7, color)
        state.dot = dot
        remove = TransparentToolButton(FIF.DELETE)
        remove.setToolTip("删除模型")
        for widget in (check, combo, dot, state, remove):
            layout.addWidget(widget, 1 if widget is combo else 0)
        self.model_layout.addWidget(row)
        self.model_rows.append((row, check, combo, state))
        check.stateChanged.connect(self._persist_models)
        combo.currentTextChanged.connect(self._persist_models)
        remove.clicked.connect(lambda: self._remove_model_row(row))
        if persist:
            style_controls(row)
            self._persist_models()
            self.models_card.setFixedHeight(self.models_card.layout().sizeHint().height())

    def _remove_model_row(self, row):
        # Drop Python references before deleteLater; future edits must not touch deleted Qt objects.
        self.model_rows = [entry for entry in self.model_rows if entry[0] is not row]
        self.model_layout.removeWidget(row)
        row.deleteLater()
        self._persist_models()
        self.models_card.setFixedHeight(self.models_card.layout().sizeHint().height())

    def _persist_models(self, *_):
        previous = {model['name']: model for model in self._value(('model_pool', 'models'))}
        self.config_manager.update(("model_pool", "models"), [
            dict(previous.get(combo.currentText(), {'status': '健康'}), name=combo.currentText(), enabled=check.isChecked())
            for _, check, combo, _ in self.model_rows
        ])
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
        catalog = catalog_snapshot(self.config_manager)
        for _, check, combo, label in self.model_rows:
            model = states.get(combo.currentText(), {})
            record = catalog.get(combo.currentText())
            if record is None or not usable(record):
                status = '已下架' if record is None else '非视频模型' if record.get('kind') != 'video' else '协议待确认'
                label.setText(status)
                label.setTextColor('#92929b', '#92929b')
                dot = getattr(label, 'dot', None)
                if dot is not None:
                    dot.set_color('#92929b')
                check.setEnabled(False)
                continue
            check.setEnabled(True)
            cooling = model.get('status') == '冷却中'
            remaining = max(0, math.ceil(model.get('cooldown_until', 0)-now))
            label.setText(f'冷却中 {remaining}秒' if cooling else '健康')
            color = '#e6a23c' if cooling else '#67c23a'
            label.setTextColor(color, color)
            dot = getattr(label, 'dot', None)
            if dot is not None:
                dot.set_color(color)

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
        self._refresh_default_options()
        self.refresh_pool_state()
        self.refresh_sync_state()

    def refresh_sync_state(self):
        controller = getattr(self.config_manager, 'model_catalog_controller', None)
        if controller is None:
            return
        self.sync_models_button.setEnabled(not controller.syncing)
        self.sync_models_button.setText('正在同步...' if controller.syncing else '同步上游模型')
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
        config = copy.deepcopy(self.config_manager.config)
        self.test_button.setEnabled(False)
        self.test_button.setText('正在测试...')
        def check():
            client = ApiClient(config['api']['base_url'], config['api']['api_key'], log=self.jobs.log_message.emit)
            uploader = ImageUploader.from_config(config, log=self.jobs.log_message.emit)
            try:
                return client.test_connection(uploader)
            finally:
                client.close()
                uploader.close()
        def finish(result):
            self.test_button.setEnabled(True)
            self.test_button.setText('测试连接')
            self.last_connection_result = result
            messages = []
            for name, title in (('video', '视频 API'), ('upload', '上传 API')):
                item = result[name]
                message = f"{title}：{'成功' if item['ok'] else '失败'} · {item['message']}"
                self.log_callback(message, 'success' if item['ok'] else 'error')
                messages.append(message)
            self._notice('\n'.join(messages), result['ok'])
        def failed(message):
            result = dict(ok=False, video=dict(ok=False, message=message), upload=dict(ok=False, message='测试未完成'))
            finish(result)
        self.jobs.start(check, finish, failed)

    def _notice(self, message, success=True):
        from qfluentwidgets import InfoBar
        self.log_callback(message, 'success' if success else 'error')
        (InfoBar.success if success else InfoBar.error)('测试连接', message, parent=self, duration=4500)

    @staticmethod
    def _toggle_secret(field, button):
        visible = field.echoMode() == LineEdit.Password
        field.setEchoMode(LineEdit.Normal if visible else LineEdit.Password)
        button.setIcon(FIF.HIDE if visible else FIF.VIEW)
