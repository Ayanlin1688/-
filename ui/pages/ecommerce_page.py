"""Ecommerce form and queued Qt presentation of the existing Python pipeline."""
from pathlib import Path
from copy import deepcopy
import os
import threading
from urllib.parse import urlsplit

from PyQt5.QtCore import QEvent, Qt, QThread, QSignalBlocker, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap, QTextCharFormat, QTextCursor
from PyQt5.QtWidgets import (
    QBoxLayout, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout,
    QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    CaptionLabel, CheckBox, ComboBox, InfoBar, LineEdit,
    PlainTextEdit, PrimaryPushButton, PushButton, SpinBox, StrongBodyLabel,
    SwitchButton, TextEdit, TitleLabel, TransparentToolButton, FluentIcon as FIF,
)

from core.ecommerce.platforms import (
    domestic, overseas, LANGUAGE_TEXT, RATIOS, MAX_PLATFORMS, detect_conflicts, get_platform,
)
from core.ecommerce.image_client import TIER_SHORT_EDGE
from core.ecommerce.manager import EcommerceImageManager
from core.api_errors import classify_connection_error
from core.i18n import tr
from core.line_routing import resolve_line
from core.line_probe import probe_line
from core.log_redaction import redact_structure, redact_text
from .. import materials
from ..components.ecommerce_result_panel import EcommerceResultCard, EcommerceResultPanel
from ..components.ecommerce_task_list import EcommerceTaskList, RESUMABLE_STATUSES
from ..components.image_lightbox import ImageLightbox
from ..components.image_pickers import (
    GalleryPickerDialog, UsedImagePickerDialog,
)
from ..components.image_upload_grid import ImageUploadGrid
from ..components.model_selector import ModelPickComboBox, catalog_snapshot
from core.model_catalog import normalize_model_id
from ..components.platform_preview_dialog import PlatformPreviewDialog
from ..components.studio_dialog import StudioDialog, safe_delete_dialog
from .ecommerce_batch_page import EcommerceBatchPage
from ..file_actions import open_local
from ..responsive import WrapLayout
from ..theme import style_controls, style_page
from ..tokens import px
from ..widgets.status_dot import Dot


# Parentless QThreads survive even if a containing widget is deleted externally.
# Release only on Qt's native finished signal, after run() has actually returned.
_LIVE_WORKERS = set()
def _image_filter():
    return tr('图片 (*.png *.jpg *.jpeg *.webp *.bmp)')


def _stage_text(status):
    return {
        'analyzing': tr('AI分析中'), 'generating': tr('图片生成中'), 'succeeded': tr('已完成'),
        'partial': tr('部分成功'), 'failed': tr('失败'), 'cancelled': tr('已取消'),
    }.get(status, status)


def _item_text(status):
    return {
        'pending': tr('等待'), 'running': tr('生成中'), 'succeeded': tr('成功'),
        'failed': tr('失败'), 'skipped': tr('跳过'),
    }.get(status, status)


class EcommerceWorker(QThread):
    group_changed = pyqtSignal(object)
    item_changed = pyqtSignal(object)
    log_message = pyqtSignal(str, str)
    finished = pyqtSignal(str, str)

    def __init__(self, manager_factory, arguments, cancel_event, secrets=(), *, operation='start'):
        super().__init__()
        self.manager_factory = manager_factory
        self.arguments = arguments
        self.cancel_event = cancel_event
        self.secrets = tuple(secrets)
        self.operation = operation

    def _clean(self, value):
        return redact_text(value, self.secrets)

    def run(self):
        group_id = self.arguments.get('group_id', '')

        def on_group(record):
            nonlocal group_id
            group_id = record['group_id']
            self.group_changed.emit(redact_structure(record, self._clean))

        try:
            manager = self.manager_factory()
            self.secrets += (manager.api_key,)
            manager.on_group = on_group
            manager.on_item = lambda rec: self.item_changed.emit(redact_structure(rec, self._clean))
            manager.on_log = lambda msg, level: self.log_message.emit(self._clean(msg), level)
            runners = {
                'start': manager.start_group,
                'regenerate': manager.regenerate_items,
                'resume': manager.resume_group,
            }
            if self.operation == 'batch':
                groups = [dict(item) for item in (self.arguments.get('groups') or ())
                          if isinstance(item, dict)]
                last_error = ''
                for position, group_arguments in enumerate(groups):
                    if self.cancel_event.is_set():
                        break
                    group_arguments.pop('row_index', None)
                    try:
                        group_arguments.pop('group_id', None)
                        runners['start'](**group_arguments, cancel_event=self.cancel_event)
                    except Exception as error:  # noqa: BLE001 - 单行失败不阻断后续行
                        last_error = self._clean(error)
                        self.log_message.emit(
                            tr('批量第 {index} 行失败：{message}').format(
                                index=position + 1, message=last_error), 'error')
                self.finished.emit(group_id, last_error)
                return
            group_id = runners[self.operation](**self.arguments, cancel_event=self.cancel_event)
            self.finished.emit(group_id, '')
        except Exception as error:
            self.finished.emit(group_id, self._clean(error))
        finally:
            self.manager_factory = None


class EcommerceModelProbeWorker(QThread):
    """仅在用户点击后，使用独立线程测试已选模型与线路快照。"""
    result = pyqtSignal(str, int, object)

    def __init__(self, config, capability, models, preferred_model, revision):
        super().__init__()
        self.config = config
        self.capability = capability
        self.models = models
        self.preferred_model = preferred_model
        self.revision = revision

    def run(self):
        line = resolve_line(self.config, self.capability)
        key = str(line.get('api_key') or '').strip()
        try:
            if not str(line.get('base_url') or '').strip() or not key:
                result = dict(ok=False, message=tr('未配置该能力线路'))
            else:
                result = probe_line(
                    line['base_url'], key, self.capability, models=self.models,
                    preferred_model=self.preferred_model)
            result = redact_structure(result, lambda value: redact_text(value, (key,)))
        except Exception as error:
            result = dict(ok=False, message=tr(classify_connection_error(error)['message']))
        self.result.emit(self.capability, self.revision, result)


class EcommercePage(QWidget):
    idle = pyqtSignal()
    history_records_changed = pyqtSignal()
    line_changed = pyqtSignal()

    def __init__(self, config_manager, parent=None):
        super().__init__(parent)
        self.setObjectName('ecommercePage')
        self.config_manager = config_manager
        self._worker = None
        self.cancel_event = threading.Event()
        self._secrets = ()
        self._group = None
        self._items = {}
        self.item_labels = {}
        self._logs = []
        self._logged_group_errors = {}
        self._history_timer = QTimer(self)
        self._history_timer.setSingleShot(True)
        self._history_timer.setInterval(160)
        self._history_timer.timeout.connect(self._flush_history_refresh)
        self._history_dirty = False
        self._history_signature = None
        self._history_persistence_available = None
        self._enrich_cache = {}
        self._history_first_show = True
        self._close_when_idle = False
        self._manager_overrides = {}
        self.product_image_paths = []
        self.style_image_paths = []
        self._form_cards = []
        self._model_probe_workers = {}
        self._model_probe_revisions = {'chat': 0, 'image': 0}
        self._model_probe_fingerprints = {}
        self._probe_closing = False
        # 探针缓存：同模型/线路指纹的结果直接回显（绿/红立即出现），
        # 未命中则 350ms 防抖后台自动探测，避免每次切换都点按钮。
        self._model_probe_cache = {}
        self._model_probe_cache_limit = 48
        self._model_probe_timers = {}
        self._probe_autostart = False
        for capability in ('chat', 'image'):
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(350)
            timer.timeout.connect(
                lambda capability=capability: self._auto_probe_model(capability))
            self._model_probe_timers[capability] = timer
        self._lightbox = None
        self._platform_selection_order = []
        self._picker_buttons = []
        self._single_cards = []
        self._single_card_titles = []
        defaults = config_manager.config.get('ecommerce', {})

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        self.stack = QStackedWidget(self)
        self.stack.setObjectName('ecommerceStack')
        shell.addWidget(self.stack)

        # 视图 0：任务列表（参考软件「电商图任务」页，进入电商图默认页）
        self.task_view = QWidget()
        self.task_view.setObjectName('ecommerceTaskView')
        task_layout = QVBoxLayout(self.task_view)
        task_layout.setContentsMargins(px(26), px(16), px(26), px(20))
        task_layout.setSpacing(px(12))
        self.task_list = EcommerceTaskList(_stage_text, self.task_view)
        self.task_list.refresh_requested.connect(self._queue_history_refresh)
        self.task_list.view_requested.connect(self._view_history_group)
        self.task_list.resume_requested.connect(self._run_resume)
        self.task_list.open_requested.connect(self._open_history_folder)
        self.task_list.delete_requested.connect(self._delete_history_groups)
        self.task_list.batch_create_requested.connect(self._show_batch_view)
        self.task_list.new_create_requested.connect(self._start_new_creation)
        self.task_list.preview_requested.connect(self._show_platform_preview)
        task_layout.addWidget(self.task_list, 1)
        self.stack.addWidget(self.task_view)

        # 视图 1：固定页头 + 两个独立滚动区；窄窗只改变列方向。
        self.single_view = QWidget()
        self.single_view.setObjectName('ecommerceSingleView')
        self.stack.addWidget(self.single_view)
        root, result_root = self._setup_single_columns()

        # 视图 2：批量制作（共享默认 + 30 行表格 + 草稿持久化）
        self.batch_page = EcommerceBatchPage(self.config_manager, self)
        self.batch_page.submit_requested.connect(self._start_batch)
        self.batch_page.history_requested.connect(self.show_task_list)
        self.batch_page.single_requested.connect(self.show_single_creation)
        self.batch_view = QWidget()
        self.batch_view.setObjectName('ecommerceBatchView')
        batch_layout = QVBoxLayout(self.batch_view)
        batch_layout.setContentsMargins(0, 0, 0, 0)
        batch_layout.setSpacing(0)
        batch_layout.addWidget(self.batch_page)
        self.stack.addWidget(self.batch_view)
        self.stack.setCurrentIndex(0)
        for view in (self.task_view, self.single_view, self.batch_view):
            view.setAutoFillBackground(False)
        self.stack.setStyleSheet(
            'QStackedWidget#ecommerceStack {background: transparent;}'
            'QWidget#ecommerceTaskView, QWidget#ecommerceSingleView,'
            ' QWidget#ecommerceBatchView {background: transparent;}')

        self._upload_card(root)
        platform_layout = self._card(root, tr('电商平台*'))
        self.platform_card = platform_layout.parentWidget()
        self.platform_checkboxes = {}
        self.platform_groups = {}
        for label, records in ((tr('国内'), domestic()), (tr('海外'), overseas())):
            section = QWidget()
            layout = QVBoxLayout(section)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(StrongBodyLabel(tr(label)))
            choices = WrapLayout(spacing=px(12))
            for record in records:
                checkbox = CheckBox(record['name'])
                checkbox.setObjectName('platform_' + record['id'])
                self.platform_checkboxes[record['id']] = checkbox
                checkbox.toggled.connect(lambda checked, key=record['id']: self._platform_changed(key, checked))
                choices.addWidget(checkbox)
            layout.addLayout(choices)
            self.platform_groups[label] = section
            platform_layout.addWidget(section)
        self.background_conflict = self._text_label()
        self.ratio_conflict = self._text_label()
        platform_layout.addWidget(self.background_conflict)
        platform_layout.addWidget(self.ratio_conflict)

        product_layout = self._card(root, tr('产品资料'))
        self.product_card = product_layout.parentWidget()
        self.product_name = LineEdit()
        self.product_name.setPlaceholderText(tr('产品名称（必填）'))
        self.product_info = PlainTextEdit()
        self.product_info.setPlaceholderText(tr('产品信息（必填）：材质、尺寸、功能、卖点、适用场景等'))
        self.product_info.setMinimumHeight(px(112))
        product_layout.addWidget(self.product_name)
        product_layout.addWidget(self.product_info)

        options = self._card(root, tr('图片配置'))
        self.options_card = options.parentWidget()
        row = WrapLayout(spacing=px(16))
        self.language = ComboBox()
        for key, value in LANGUAGE_TEXT.items():
            self.language.addItem(tr(value), userData=key)
        self._select_data(self.language, defaults.get('language', 'zh'))
        row.addLayout(self._field(tr('图片文字语言'), self.language))
        self.tier = ComboBox()
        self.tier.addItems(list(TIER_SHORT_EDGE))
        self.tier.setCurrentText(defaults.get('default_tier', next(iter(TIER_SHORT_EDGE))))
        row.addLayout(self._field(tr('分辨率档位'), self.tier))
        options.addLayout(row)
        counts = WrapLayout(spacing=px(16))
        # 参考软件范围：轮播图 1-6 张、详情图 1-15 张。
        self.carousel_count = SpinBox()
        self.carousel_count.setRange(1, 6)
        self.carousel_count.setValue(
            self._clamp_count(defaults.get('default_carousel_count', 3), 1, 6))
        self.detail_count = SpinBox()
        self.detail_count.setRange(1, 15)
        self.detail_count.setValue(
            self._clamp_count(defaults.get('default_detail_count', 4), 1, 15))
        self.carousel_ratio, self.detail_ratio = ComboBox(), ComboBox()
        for combo, key in ((self.carousel_ratio, 'default_carousel_ratio'),
                           (self.detail_ratio, 'default_detail_ratio')):
            combo.addItems(RATIOS)
            combo.setCurrentText(defaults.get(key, RATIOS[0]))
        for title, widget in ((tr('轮播图张数'), self.carousel_count), (tr('轮播图比例'), self.carousel_ratio),
                              (tr('详情图张数'), self.detail_count), (tr('详情图比例'), self.detail_ratio)):
            counts.addLayout(self._field(title, widget))
        options.addLayout(counts)

        models = self._card(root, tr('模型设置'))
        self.models_card = models.parentWidget()
        self.model_grid = QGridLayout()
        self.model_grid.setContentsMargins(0, 0, 0, 0)
        self.model_grid.setHorizontalSpacing(px(10))
        self.model_grid.setVerticalSpacing(px(8))
        self.model_grid.setColumnStretch(0, 1)
        # 模型设置卡居中限宽：宽窗不再把控件拉满整行（需求5：卡片居中、不要过宽）。
        self.model_card_container = QWidget()
        self.model_card_container.setMaximumWidth(px(980))
        self.model_card_container.setLayout(self.model_grid)
        centered_models = QHBoxLayout()
        centered_models.setContentsMargins(0, 0, 0, 0)
        centered_models.addStretch(1)
        centered_models.addWidget(self.model_card_container, 10)
        centered_models.addStretch(1)
        self.model_centered_layout = centered_models
        self.analysis_model_picker = ModelPickComboBox()
        self.analysis_model_picker.setObjectName('ecommerceAnalysisModel')
        self.analysis_model_picker.setMinimumWidth(px(260))
        self.analysis_model_picker.set_models(
            catalog_snapshot(config_manager, 'chat'),
            defaults.get('analysis_model', 'gemini-3.8-flash'), kind='chat')
        self.analysis_line_label = CaptionLabel()
        self.analysis_line_combo = ComboBox()
        self.analysis_line_combo.setObjectName('ecommerceAnalysisLine')
        self.analysis_line_combo.setMinimumWidth(px(200))
        self.analysis_line_combo.setMaximumWidth(px(200))
        self.sync_analysis_models_button = TransparentToolButton(FIF.SYNC)
        self.sync_analysis_models_button.setObjectName('syncAnalysisModels')
        self.sync_analysis_models_button.setToolTip(tr('同步模型'))
        self.analysis_status_dot = Dot(size=8, color='#92929b')
        self.analysis_status_dot.setObjectName('analysisStatusDot')
        self.image_model_picker = ModelPickComboBox()
        self.image_model_picker.setObjectName('ecommerceImageModel')
        self.image_model_picker.setMinimumWidth(px(260))
        self.image_model_picker.set_models(
            catalog_snapshot(config_manager, 'image'),
            defaults.get('image_model', 'gpt-image-2.5-sunburst'), kind='image')
        self.image_line_label = CaptionLabel()
        self.image_line_combo = ComboBox()
        self.image_line_combo.setObjectName('ecommerceImageLine')
        self.image_line_combo.setMinimumWidth(px(200))
        self.image_line_combo.setMaximumWidth(px(200))
        self.sync_image_models_button = TransparentToolButton(FIF.SYNC)
        self.sync_image_models_button.setObjectName('syncImageModels')
        self.sync_image_models_button.setToolTip(tr('同步模型'))
        self.image_status_dot = Dot(size=8, color='#92929b')
        self.image_status_dot.setObjectName('imageStatusDot')
        for row, title, picker, combo, label, dot, button in (
                (0, tr('AI 分析模型'), self.analysis_model_picker, self.analysis_line_combo,
                 self.analysis_line_label, self.analysis_status_dot, self.sync_analysis_models_button),
                (2, tr('图片模型'), self.image_model_picker, self.image_line_combo,
                 self.image_line_label, self.image_status_dot, self.sync_image_models_button)):
            self.model_grid.addWidget(CaptionLabel(title), row, 0)
            self.model_grid.addWidget(CaptionLabel(tr('线路')), row, 1)
            self.model_grid.addWidget(picker, row + 1, 0)
            self.model_grid.addWidget(combo, row + 1, 1, Qt.AlignLeft | Qt.AlignVCenter)
            self.model_grid.addWidget(label, row + 1, 2)
            self.model_grid.addWidget(dot, row + 1, 3, Qt.AlignCenter)
            self.model_grid.addWidget(button, row + 1, 4)
            # These status dots own useful hover details, unlike passive dots.
            dot.setAttribute(Qt.WA_TransparentForMouseEvents, False)
            dot.setToolTip(tr('未测试'))

        self.thinking_switch = SwitchButton()
        self.thinking_switch.setObjectName('ecommerceThinking')
        self.thinking_switch.setChecked(bool(defaults.get('thinking', False)))
        self.thinking_switch.setToolTip(tr(
            '开启后按模型族自动下发思考参数：DeepSeek 线路的 deepseek-chat/flash 自动切换 '
            'deepseek-v4-pro 并下发 reasoning_effort=high；Gemini 下发 thinkingConfig；'
            '其他推理模型（R1 / QwQ / o1 / o3 / o4 / Thinking 等）下发 enable_thinking；'
            'Claude 走 thinking budget；非推理模型不会盲发参数'))
        self.model_grid.addWidget(CaptionLabel(tr('深度思考')), 4, 0)
        self.model_grid.addWidget(self.thinking_switch, 4, 1, Qt.AlignLeft | Qt.AlignVCenter)
        models.addLayout(self.model_centered_layout)
        self.sync_analysis_models_button.clicked.connect(lambda: self._test_selected_model('chat'))
        self.sync_image_models_button.clicked.connect(lambda: self._test_selected_model('image'))
        self.analysis_model_picker.currentTextChanged.connect(
            lambda _text: self._persist_model('analysis_model', self.analysis_model_picker.value()))
        self.image_model_picker.currentTextChanged.connect(
            lambda _text: self._persist_model('image_model', self.image_model_picker.value()))
        self.analysis_model_picker.textEdited.connect(
            lambda _text: self._persist_model('analysis_model', self.analysis_model_picker.value()))
        self.image_model_picker.textEdited.connect(
            lambda _text: self._persist_model('image_model', self.image_model_picker.value()))
        self.thinking_switch.checkedChanged.connect(
            lambda value: self._persist_model('thinking', bool(value)))
        self.analysis_line_combo.currentIndexChanged.connect(
            lambda index: self._route_changed('chat', self.analysis_line_combo, index))
        self.image_line_combo.currentIndexChanged.connect(
            lambda index: self._route_changed('image', self.image_line_combo, index))
        self._refresh_line_routes()
        self.refresh_effective_lines()
        self.refresh_model_pickers()

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(12)
        self.start_button = PrimaryPushButton(tr('开始生成'))
        self.cancel_button = PushButton(tr('取消'))
        self.cancel_button.setEnabled(False)
        self.start_button.clicked.connect(self.start_generation)
        self.cancel_button.clicked.connect(self.cancel_generation)
        for button in (self.start_button, self.cancel_button):
            button.setFixedHeight(40)
        actions.addStretch(1)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.start_button)
        root.addLayout(actions)
        root.addStretch(1)
        progress = self._card(result_root, tr('生成进度'), form=False)
        self.progress_card = progress.parentWidget()
        self.stage_label = self._text_label(tr('等待开始'))
        self.summary_label = self._text_label()
        progress.addWidget(self.stage_label)
        progress.addWidget(self.summary_label)
        self.items_layout = QVBoxLayout()
        progress.addLayout(self.items_layout)
        self.open_output_button = PushButton(FIF.FOLDER, tr('打开输出文件夹'))
        self.open_output_button.setEnabled(False)
        self.open_output_button.clicked.connect(self.open_output)
        progress.addWidget(self.open_output_button)
        self.log_box = TextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(px(160))
        self.log_box.document().setMaximumBlockCount(1000)
        progress.addWidget(self.log_box)
        self.result_panel = EcommerceResultPanel(self, config_manager=config_manager)
        self.result_panel.regenerate_requested.connect(lambda key: self._run_regenerate([key]))
        self.result_panel.regenerate_failed_requested.connect(self._run_regenerate)
        self.result_panel.regenerate_selected_requested.connect(self._run_regenerate)
        self.result_panel.log_message.connect(self._append_log)
        self.result_panel.preview_requested.connect(self._open_image_lightbox)
        self.regenerate_failed_button = self.result_panel.regenerate_failed_button
        result_root.addWidget(self.result_panel)
        result_root.addStretch(1)
        style_controls(self)
        style_page(self)
        materials.register_theme_callback(self._refresh_colors)
        self._refresh_colors()
        self._refresh_conflicts()
        self._sync_result_columns()
        self._schedule_history_refresh()
        self.body_scroll.viewport().installEventFilter(self)
        self.single_view.installEventFilter(self)

    # —— 视图切换（QStackedWidget：任务列表 / 单个制作 / 批量制作） ——
    TASK_VIEW, SINGLE_VIEW, BATCH_VIEW = 0, 1, 2

    def current_view_index(self):
        return self.stack.currentIndex()

    def show_task_list(self):
        self.stack.setCurrentIndex(self.TASK_VIEW)
        self._schedule_history_refresh()

    def show_single_creation(self):
        self.stack.setCurrentIndex(self.SINGLE_VIEW)
        self._fit_single_columns()
        self._fit_model_columns()
        self._sync_result_columns()
        QTimer.singleShot(0, self._fit_model_columns)

    def _show_batch_view(self):
        self.stack.setCurrentIndex(self.BATCH_VIEW)

    # —— 批量制作（顺序队列：每行独立 group_id） ——
    def _start_batch(self, groups):
        if self.busy:
            return
        groups = [dict(item) for item in (groups or ()) if isinstance(item, dict)]
        if not groups:
            self._notice(tr('没有可提交的行'))
            return
        config = self.config_manager.config
        lines = (resolve_line(config, 'chat'), resolve_line(config, 'image'))
        if any(not str(line.get('base_url') or '').strip()
               or not str(line.get('api_key') or '').strip() for line in lines):
            self._notice(tr('请先到“设置”页填写 API 地址与密钥；AI 分析需要含文本模型权限的密钥'))
            return
        # 批量页的模型选择已写入 config，管理器按 config 构建；不使用单制作页覆盖值。
        self._manager_overrides = {}
        self._secrets = tuple(resolve_line(config, capability).get('api_key', '')
                              for capability in ('chat', 'image'))
        self._logs.clear()
        self.log_box.clear()
        self._append_log(tr('批量制作：共 {count} 行，按顺序逐行提交').format(
            count=len(groups)), 'info')
        self.show_task_list()
        self._launch_worker(dict(groups=groups), operation='batch')

    def _start_new_creation(self):
        """「+ 新建制作」：切到单个制作视图并清空当前制作内容。"""
        self.show_single_creation()
        self.task_list.set_manage_mode(False)
        self.product_name.clear()
        self.product_info.clear()
        self.product_image_paths = []
        self.style_image_paths = []
        self._refresh_references()
        self._clear_results()
        self.product_name.setFocus()
        self._queue_history_refresh()

    def _show_platform_preview(self, group_id):
        store = self.config_manager.ecommerce_store()
        group = store.get_group(group_id)
        if not group:
            self._notice(tr('当前生成组不存在'))
            self._queue_history_refresh()
            return
        try:
            manifest = group.get('manifest')
            if not (isinstance(manifest, dict) and manifest.get('items')):
                items = store.list_items(group_id)
                manifest = dict(manifest or {}, items=[
                    dict(kind=item.get('kind'), seq=item.get('seq'),
                         title=item.get('title'), file=item.get('file_path'),
                         status=item.get('status'), size=item.get('size'),
                         model=item.get('model'), error=item.get('error'))
                    for item in items])
                group = dict(group, manifest=manifest)
            dialog = PlatformPreviewDialog(self, redact_structure(group, self._clean))
            try:
                from core.i18n import register_retranslate
                register_retranslate(dialog)
            except Exception:
                pass
            try:
                dialog.exec_()
            finally:
                safe_delete_dialog(dialog)
        except Exception:
            self._notice(tr('平台预览打开失败'))

    @property
    def busy(self):
        return self._worker is not None

    def _setup_single_columns(self):
        shell = QVBoxLayout(self.single_view)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        self.single_header = QWidget()
        self.single_header.setObjectName('ecommerceSingleHeader')
        self.single_header.setMinimumHeight(76)
        header = QHBoxLayout(self.single_header)
        header.setContentsMargins(48, 12, 48, 12)
        header.setSpacing(24)
        heading = QVBoxLayout()
        heading.setSpacing(4)
        self.single_title_label = TitleLabel(tr('电商图制作'))
        self.single_subtitle_label = CaptionLabel(
            tr('上传产品图，AI 自动生成电商轮播图与详情图'))
        self.single_subtitle_label.setWordWrap(True)
        self.single_subtitle_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        heading.addWidget(self.single_title_label)
        heading.addWidget(self.single_subtitle_label)
        header.addLayout(heading, 1)
        self.history_tasks_button = PushButton(tr('历史任务'))
        self.history_tasks_button.setObjectName('ecommerceHistoryTasksButton')
        self.batch_creation_button = PushButton(tr('批量制作'))
        self.batch_creation_button.setObjectName('ecommerceBatchCreationButton')
        self.history_tasks_button.clicked.connect(lambda: self.show_task_list())
        self.batch_creation_button.clicked.connect(lambda: self._show_batch_view())
        navigation = QHBoxLayout()
        navigation.setSpacing(12)
        for button in (self.history_tasks_button, self.batch_creation_button):
            button.setFixedHeight(36)
            navigation.addWidget(button)
        header.addLayout(navigation)
        shell.addWidget(self.single_header)

        self.single_columns_layout = QBoxLayout(QBoxLayout.LeftToRight)
        self.single_columns_layout.setContentsMargins(48, 0, 48, 24)
        self.single_columns_layout.setSpacing(24)
        shell.addLayout(self.single_columns_layout, 1)
        columns = []
        for name in ('ecommerceFormScroll', 'ecommerceResultScroll'):
            area = QScrollArea()
            area.setObjectName(name)
            area.setFrameShape(QFrame.NoFrame)
            area.setWidgetResizable(True)
            area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            area.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
            content = QWidget()
            content.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            layout = QVBoxLayout(content)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(16)
            # 控件的内容最小宽度不能反向撑大整个页面。
            layout.setSizeConstraint(QLayout.SetNoConstraint)
            area.setWidget(content)
            self.single_columns_layout.addWidget(area, 1)
            columns.append((area, content, layout))
        self.body_scroll, self.body_content, form_root = columns[0]
        self.result_scroll, self.result_content, result_root = columns[1]

        self.right_header_host = QWidget()
        self.right_header_host.setObjectName('ecommerceRightHeaderHost')
        self.right_header_host.setFixedHeight(0)
        result_root.addWidget(self.right_header_host)
        self.empty_state = self._single_card('ecommerceEmptyState')
        self.empty_state.setMinimumHeight(420)
        empty = QVBoxLayout(self.empty_state)
        empty.setContentsMargins(28, 28, 28, 28)
        empty.setSpacing(16)
        empty.addStretch(1)
        self.empty_icon = QLabel()
        self.empty_icon.setFixedSize(40, 40)
        empty.addWidget(self.empty_icon, 0, Qt.AlignHCenter)
        self.empty_title_label = QLabel(tr('填写完左侧信息后点击「开始制作」'))
        self.empty_subtitle_label = QLabel(tr('AI 将自动分析产品并生成图片'))
        for label in (self.empty_title_label, self.empty_subtitle_label):
            label.setTextFormat(Qt.PlainText)
            label.setWordWrap(True)
            label.setAlignment(Qt.AlignCenter)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            empty.addWidget(label)
        empty.addStretch(2)
        result_root.addWidget(self.empty_state)
        return form_root, result_root

    def _single_card(self, name):
        # 普通 QFrame 保持不透明直显，不挂载卡片入场/透明度动效。
        card = QFrame()
        card.setObjectName(name)
        card.setAttribute(Qt.WA_StyledBackground, True)
        self._single_cards.append(card)
        return card

    def _card(self, root, title, form=True):
        card = self._single_card('ecommerceSingleCard' + str(len(self._single_cards)))
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(16)
        label = StrongBodyLabel(title)
        self._single_card_titles.append(label)
        layout.addWidget(label)
        root.addWidget(card)
        if form:
            self._form_cards.append(card)
        return layout

    def _fit_single_columns(self):
        if not hasattr(self, 'single_columns_layout'):
            return
        wide = self.single_view.width() >= px(1240)
        direction = QBoxLayout.LeftToRight if wide else QBoxLayout.TopToBottom
        if self.single_columns_layout.direction() != direction:
            self.single_columns_layout.setDirection(direction)
        self.single_columns_layout.setSpacing(px(24 if wide else 16))

    def _sync_result_columns(self):
        """右栏只由当前任务/结果状态决定，校验失败不改变空态。"""
        has_results = bool(self.result_panel.cards)
        active = self.busy or self._group is not None or has_results
        self.empty_state.setVisible(not active)
        self.progress_card.setVisible(active)
        self.result_panel.setVisible(has_results or self._group is not None)

    @staticmethod
    def _field(title, widget):
        layout = QVBoxLayout()
        layout.addWidget(CaptionLabel(tr(title)))
        layout.addWidget(widget)
        return layout

    @staticmethod
    def _text_label(text=''):
        label = QLabel(text)
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(True)
        return label

    @staticmethod
    def _select_data(combo, value):
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return

    @staticmethod
    def _clamp_count(value, low, high):
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = low
        return max(low, min(high, number))

    # —— 上传区（参考软件同款：产品图 6 格 + 风格图 1 格 + 两个选择器） ——
    def _upload_card(self, root):
        self.product_upload_card = self._single_card('ecommerceProductUploadCard')
        product = QVBoxLayout(self.product_upload_card)
        product.setContentsMargins(20, 18, 20, 18)
        product.setSpacing(16)
        product.addLayout(self._upload_header(
            tr('产品图'), target='product', required=True,
            hint=tr('上传产品白底图，最多 6 张')))
        self.product_grid = ImageUploadGrid('product', 6, self)
        self.product_grid.changed.connect(self._on_product_images_changed)
        self.product_grid.preview_requested.connect(self._preview_product_image)
        self.product_grid.add_requested.connect(self._choose_products)
        product.addWidget(self.product_grid)
        root.addWidget(self.product_upload_card)
        self.style_upload_card = self._single_card('ecommerceStyleUploadCard')
        style = QVBoxLayout(self.style_upload_card)
        style.setContentsMargins(20, 18, 20, 18)
        style.setSpacing(16)
        style.addLayout(self._upload_header(
            tr('风格参考图'), target='style', optional=True,
            hint=tr('可选，上传参考图来指定风格')))
        self.style_grid = ImageUploadGrid('style', 1, self)
        self.style_grid.changed.connect(self._on_style_images_changed)
        self.style_grid.preview_requested.connect(self._preview_style_image)
        self.style_grid.add_requested.connect(self._choose_style)
        style.addWidget(self.style_grid)
        root.addWidget(self.style_upload_card)
        self._form_cards.extend((self.product_upload_card, self.style_upload_card))
        self._refresh_references()

    def _upload_header(self, title, *, target, required=False, optional=False, hint=''):
        layout = QVBoxLayout()
        layout.setSpacing(4)
        header = QHBoxLayout()
        header.setSpacing(8)
        label = StrongBodyLabel(title)
        self._single_card_titles.append(label)
        header.addWidget(label)
        if required:
            star = QLabel('*')
            star.setStyleSheet('color: %s;' % materials.ERROR)
            header.addWidget(star)
        if optional:
            badge = CaptionLabel(tr('可选'))
            badge.setStyleSheet('color: %s;' % materials.palette()['text2'])
            header.addWidget(badge)
        hint_label = CaptionLabel(hint)
        hint_label.setWordWrap(True)
        hint_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        header.addStretch(1)
        used = self._picker_button(tr('已用图片'), tr('从已完成任务选择图片'))
        gallery = self._picker_button(tr('画廊选图'), tr('从本地画廊目录选择图片'))
        used.clicked.connect(
            lambda _checked=False, target=target: self._pick_used_images(target))
        gallery.clicked.connect(
            lambda _checked=False, target=target: self._pick_gallery_images(target))
        used.setObjectName('usedImagesButton_' + target)
        gallery.setObjectName('galleryButton_' + target)
        header.addWidget(used)
        header.addWidget(gallery)
        layout.addLayout(header)
        layout.addWidget(hint_label)
        return layout

    def _picker_button(self, text, tooltip):
        button = PushButton(text)
        button.setFixedHeight(28)
        button.setToolTip(tooltip)
        button.setCursor(Qt.PointingHandCursor)
        self._picker_buttons.append(button)
        return button

    def _refresh_picker_button_styles(self):
        colors = materials.palette()
        for button in self._picker_buttons:
            try:
                button.setStyleSheet(
                    'QPushButton {background: transparent; color: %s; border: 1px solid %s;'
                    ' border-radius: 6px; padding: 2px 8px;'
                    ' font-size: 12px; font-weight: 500;}'
                    ' QPushButton:hover {background: %s;}'
                    ' QPushButton:disabled {color: %s; border-color: %s;}'
                    % (materials.ecommerce_color('single_button_text'), colors['accent'],
                       materials.ecommerce_color('single_button_hover'),
                       colors['text3'], materials.rgba(colors['border'])))
            except RuntimeError:
                continue

    def _on_product_images_changed(self, paths):
        self.product_image_paths = [str(path) for path in (paths or [])][:6]

    def _on_style_images_changed(self, paths):
        self.style_image_paths = [str(path) for path in (paths or [])][:1]

    def _pick_used_images(self, target):
        try:
            dialog = UsedImagePickerDialog(
                self, self.config_manager.ecommerce_store(),
                max_count=6 if target == 'product' else 1)
            if dialog.exec_() == dialog.Accepted:
                self._merge_images(target, dialog.result_paths)
        finally:
            safe_delete_dialog(dialog)

    def _pick_gallery_images(self, target):
        try:
            dialog = GalleryPickerDialog(
                self, self.config_manager, max_count=6 if target == 'product' else 1)
            if dialog.exec_() == dialog.Accepted:
                self._merge_images(target, dialog.result_paths)
        finally:
            safe_delete_dialog(dialog)

    def _merge_images(self, target, paths):
        paths = [str(path) for path in (paths or []) if str(path or '').strip()]
        if not paths:
            return
        if target == 'product':
            merged = list(dict.fromkeys(self.product_image_paths + paths))
            if len(merged) > 6:
                self._notice(tr('产品图最多 6 张，请移除后再添加'))
                merged = merged[:6]
            self.product_image_paths = merged
        else:
            if len(paths) > 1:
                self._notice(tr('风格图最多 1 张，已使用第一张'))
            self.style_image_paths = [paths[0]]
        self._refresh_references()

    def _preview_product_image(self, index):
        self._open_image_lightbox(self.product_image_paths, index)

    def _preview_style_image(self, index):
        self._open_image_lightbox(self.style_image_paths, index)

    def _open_image_lightbox(self, paths, index=0):
        paths = [str(path) for path in (paths or []) if str(path or '').strip()]
        if not paths:
            return None
        if self._lightbox is not None:
            try:
                self._lightbox.close()
            except RuntimeError:
                pass
            self._lightbox = None
        try:
            dialog = ImageLightbox.show_images(self, paths, index)
        except Exception:
            self._notice(tr('图片预览打开失败'))
            return None
        if dialog is not None:
            self._lightbox = dialog
            dialog.closed.connect(self._on_lightbox_closed)
        return dialog

    def _on_lightbox_closed(self):
        self._lightbox = None

    def _persist_model(self, key, value):
        kind = 'chat' if key == 'analysis_model' else 'image' if key == 'image_model' else None
        if kind:
            value = normalize_model_id(value, catalog_snapshot(self.config_manager, kind).keys())
        self.config_manager.update(('ecommerce', key), value)
        if kind:
            self._refresh_model_probe_identity(kind)

    def model_overrides(self):
        values = {
            'analysis_model': normalize_model_id(
                self.analysis_model_picker.value(),
                catalog_snapshot(self.config_manager, 'chat').keys()),
            'image_model': normalize_model_id(
                self.image_model_picker.value(),
                catalog_snapshot(self.config_manager, 'image').keys()),
        }
        return {key: value for key, value in values.items() if value}

    def refresh_model_pickers(self, *_args):
        analysis = self.analysis_model_picker.value()
        image = self.image_model_picker.value()
        self.analysis_model_picker.set_models(
            catalog_snapshot(self.config_manager, 'chat'), analysis, kind='chat')
        self.image_model_picker.set_models(
            catalog_snapshot(self.config_manager, 'image'), image, kind='image')

    @staticmethod
    def _line_host(line):
        return (urlsplit(str(line.get('base_url') or '').strip()).hostname or '').lower() or tr('未配置')

    def refresh_effective_lines(self, *_args):
        config = self.config_manager.config
        colors = materials.palette()
        for capability, label in (('chat', self.analysis_line_label), ('image', self.image_line_label)):
            host = self._line_host(resolve_line(config, capability))
            configured = host != tr('未配置')
            label.setText(tr('已生效') if configured else tr('未配置'))
            label.setToolTip(tr('{capability}线路：{host}').format(
                capability=tr('AI 分析') if capability == 'chat' else tr('图片生成'), host=host))
            label.setStyleSheet('color: %s;' % (colors['text1'] if configured else colors['warning']))
            self._refresh_model_probe_identity(capability)
        self._fit_model_columns()

    def _fit_model_columns(self):
        """宽窗保留完整模型宽度，窄窗按同一列预算收缩，避免横向滚动。"""
        if not self.isVisible():
            return
        outer = self.body_content.layout().contentsMargins()
        inner = self.analysis_model_picker.parentWidget().layout().contentsMargins()
        viewport = self.body_scroll.viewport().width()
        # 模型卡居中限宽：列宽预算最多按 980px 卡宽计算，宽窗不把控件拉满整行；
        # 同时扣掉卡片自身左右内边距，避免窄窗下卡片最小宽度撑出横向滚动。
        card_width = min(px(980), max(0, viewport - outer.left() - outer.right()))
        available = max(px(120), card_width - px(40) - inner.left() - inner.right())
        spacing = max(0, self.model_grid.horizontalSpacing()) * 4
        reserved = sum(max(first.minimumSizeHint().width(), second.minimumSizeHint().width(),
                           first.minimumWidth(), second.minimumWidth())
                       for first, second in ((self.analysis_line_label, self.image_line_label),
                                             (self.analysis_status_dot, self.image_status_dot),
                                             (self.sync_analysis_models_button,
                                              self.sync_image_models_button)))
        picker_width = min(px(260), max(px(110), available - spacing - reserved - px(120)))
        combo_width = min(px(200), max(px(90), available - spacing - reserved - picker_width))
        total = picker_width + combo_width + reserved + spacing
        if total > available:
            picker_width = max(px(80), picker_width - (total - available))
            total = picker_width + combo_width + reserved + spacing
        if total > available:
            combo_width = max(px(80), combo_width - (total - available))
        for picker in (self.analysis_model_picker, self.image_model_picker):
            picker.setMinimumWidth(picker_width)
        for combo in (self.analysis_line_combo, self.image_line_combo):
            combo.setMinimumWidth(combo_width)
            combo.setMaximumWidth(px(200))
        for label in (self.analysis_line_label, self.image_line_label):
            label.setMaximumWidth(px(96))

    def refresh_interface_scale(self):
        self._fit_single_columns()
        self._fit_model_columns()
        self._refresh_empty_icon()
        for attribute in ('product_grid', 'style_grid'):
            grid = getattr(self, attribute, None)
            if grid is not None:
                grid.refresh_interface_scale()
        try:
            self.task_list.refresh_interface_scale()
        except RuntimeError:
            pass
        try:
            self.batch_page.refresh_interface_scale()
        except RuntimeError:
            pass

    def eventFilter(self, watched, event):
        if watched is self.single_view and event.type() == QEvent.Resize:
            self._fit_single_columns()
        if (watched is self.body_scroll.viewport()
                and event.type() == QEvent.Resize):
            self._fit_model_columns()
        return super().eventFilter(watched, event)

    def _refresh_line_routes(self, *_args):
        stations = [station for station in self.config_manager.config.get('stations', [])
                    if isinstance(station, dict) and station.get('enabled')]
        routing = self.config_manager.config.get('line_routing', {})
        for capability, combo in (('chat', self.analysis_line_combo), ('image', self.image_line_combo)):
            saved = (routing.get(capability, 'active') if isinstance(routing, dict) else 'active') or 'active'
            blocker = QSignalBlocker(combo)
            combo.clear()
            combo.addItem(tr('当前线路'), userData='active')
            for station in stations:
                combo.addItem(str(station.get('name') or station.get('id') or ''),
                              userData=station.get('id'))
            index = combo.findData(saved)
            combo.setCurrentIndex(index if index >= 0 else 0)
            del blocker

    refresh_line_routes = _refresh_line_routes

    def _route_changed(self, capability, combo, index):
        if index < 0:
            return
        station_id = combo.itemData(index) or 'active'
        self.config_manager.update(('line_routing', capability), station_id)
        controller = getattr(self.config_manager, 'model_catalog_controller', None)
        if controller is not None:
            controller.reload_lines()
        self.refresh_model_pickers()
        self.refresh_effective_lines()
        self.line_changed.emit()

    def _sync_catalog(self, _capability):
        controller = getattr(self.config_manager, 'model_catalog_controller', None)
        if controller is not None:
            controller.refresh()

    def _model_probe_controls(self, capability):
        if capability == 'chat':
            return (self.analysis_model_picker, self.analysis_status_dot,
                    self.sync_analysis_models_button)
        return self.image_model_picker, self.image_status_dot, self.sync_image_models_button

    def _model_probe_fingerprint(self, capability):
        picker, _dot, _button = self._model_probe_controls(capability)
        line = resolve_line(self.config_manager.config, capability)
        return (picker.value(), line.get('source'), line.get('base_url'), line.get('api_key'))

    def _refresh_model_probe_identity(self, capability):
        identity = self._model_probe_fingerprint(capability)
        if self._model_probe_fingerprints.get(capability) == identity:
            return
        self._model_probe_fingerprints[capability] = identity
        self._model_probe_revisions[capability] += 1
        cached = self._model_probe_cache.get(identity)
        if cached is not None:
            self._apply_probe_result(capability, cached, cached_mode=True)
        else:
            _picker, dot, _button = self._model_probe_controls(capability)
            dot.set_color('#92929b')
            dot.setToolTip(tr('未测试'))
        self._schedule_auto_probe(capability)

    def _schedule_auto_probe(self, capability):
        if not self._probe_autostart or self._probe_closing or self.busy:
            return
        picker, _dot, _button = self._model_probe_controls(capability)
        if not str(picker.value() or '').strip():
            return
        timer = self._model_probe_timers.get(capability)
        if timer is not None:
            timer.start()

    def _auto_probe_model(self, capability):
        if self.busy or self._probe_closing or capability in self._model_probe_workers:
            return
        if not self._probe_autostart:
            return
        self._test_selected_model(capability, automatic=True)

    def _apply_probe_result(self, capability, result, *, cached_mode=False, automatic=False):
        _picker, dot, _button = self._model_probe_controls(capability)
        ok = bool(result.get('ok'))
        message = self._clean(result.get('message') or tr('模型测试失败，请检查线路与模型'))
        dot.set_color('#67c23a' if ok else '#f56c6c')
        dot.setToolTip(tr('模型测试通过：{message}').format(message=message) if ok else message)
        if not ok and not cached_mode and not automatic:
            title = tr('文本模型测试失败') if capability == 'chat' else tr('图片模型测试失败')
            InfoBar.error(tr('电商图'), title + ' · ' + message, parent=self, duration=5000)

    def _remember_probe_result(self, capability, result):
        identity = self._model_probe_fingerprints.get(capability)
        if identity is None:
            return
        self._model_probe_cache[identity] = dict(result)
        while len(self._model_probe_cache) > self._model_probe_cache_limit:
            self._model_probe_cache.pop(next(iter(self._model_probe_cache)))

    def _test_selected_model(self, capability, automatic=False):
        if self.busy or self._probe_closing or capability in self._model_probe_workers:
            return
        picker, dot, button = self._model_probe_controls(capability)
        self._refresh_model_probe_identity(capability)
        if not automatic:
            dot.set_color('#92929b')
            dot.setToolTip(tr('未测试'))
        button.setEnabled(False)
        revision = self._model_probe_revisions[capability]
        snapshot = catalog_snapshot(self.config_manager, capability)
        # All mutable UI/configuration state is snapshotted before entering QThread.
        model_rows = [dict(record, id=model_id) if isinstance(record, dict)
                      else {'id': model_id} for model_id, record in snapshot.items()]
        worker = EcommerceModelProbeWorker(
            deepcopy(self.config_manager.config), capability, model_rows, picker.value(), revision)
        worker.automatic = bool(automatic)
        self._model_probe_workers[capability] = worker
        _LIVE_WORKERS.add(worker)
        worker.result.connect(self._on_model_probe_result, Qt.QueuedConnection)
        worker.finished.connect(self._model_probe_thread_stopped, Qt.QueuedConnection)
        worker.finished.connect(lambda: _LIVE_WORKERS.discard(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    @pyqtSlot(str, int, object)
    def _on_model_probe_result(self, capability, revision, result):
        self._refresh_model_probe_identity(capability)
        if self._probe_closing or revision != self._model_probe_revisions[capability]:
            return
        worker = self.sender()
        automatic = bool(getattr(worker, 'automatic', False))
        self._remember_probe_result(capability, result)
        self._apply_probe_result(capability, result, automatic=automatic)
        self._sync_catalog(capability)

    @pyqtSlot()
    def _model_probe_thread_stopped(self):
        worker = self.sender()
        capability = worker.capability
        if self._model_probe_workers.get(capability) is worker:
            self._model_probe_workers.pop(capability, None)
            _picker, _dot, button = self._model_probe_controls(capability)
            button.setEnabled(not self.busy and not self._probe_closing)

    def selected_platforms(self):
        return [key for key, checkbox in self.platform_checkboxes.items() if checkbox.isChecked()]

    def _notice(self, message, warning=False):
        method = InfoBar.warning if warning else InfoBar.error
        method(tr('电商图'), self._clean(message), parent=self, duration=5000)

    def _platform_changed(self, key, checked):
        if checked:
            if key not in self._platform_selection_order:
                self._platform_selection_order.append(key)
            if len(self.selected_platforms()) > MAX_PLATFORMS:
                checkbox = self.platform_checkboxes[key]
                checkbox.blockSignals(True)
                checkbox.setChecked(False)
                checkbox.blockSignals(False)
                try:
                    self._platform_selection_order.remove(key)
                except ValueError:
                    pass
                self._notice(tr('最多选择 {count} 个平台').format(count=MAX_PLATFORMS),
                             warning=True)
                self._refresh_conflicts()
                return
        else:
            try:
                self._platform_selection_order.remove(key)
            except ValueError:
                pass
        self._refresh_conflicts()
        # 参考软件行为：已选平台数组变化且表单空闲时，套用第一个选中平台的默认值。
        self._apply_first_platform_defaults()

    def _apply_first_platform_defaults(self):
        """勾选平台后自动套用第一个选中平台的轮播图 / 详情图比例与张数。

        与参考软件一致：仅在表单空闲（未生成中）且已选平台非空时套用；
        取用户选中顺序中的第一个平台，而不是目录顺序。
        """
        if self.busy:
            return
        selected = self.selected_platforms()
        if not selected:
            return
        first = next((key for key in self._platform_selection_order if key in selected),
                     selected[0])
        try:
            record = get_platform(first)
        except (KeyError, TypeError):
            return
        try:
            if record.get('defaultCarouselRatio') in RATIOS:
                self.carousel_ratio.setCurrentText(record['defaultCarouselRatio'])
            if record.get('defaultDetailRatio') in RATIOS:
                self.detail_ratio.setCurrentText(record['defaultDetailRatio'])
            self.carousel_count.setValue(
                self._clamp_count(record.get('defaultCarouselCount'), 1, 6))
            self.detail_count.setValue(
                self._clamp_count(record.get('defaultDetailCount'), 1, 15))
        except (RuntimeError, TypeError, ValueError):
            pass

    def _refresh_conflicts(self):
        conflicts = detect_conflicts(self.selected_platforms())
        self.background_conflict.setText('\n'.join(c['tip'] for c in conflicts if c['type'] == 'background'))
        self.ratio_conflict.setText('\n'.join(c['tip'] for c in conflicts if c['type'].startswith('ratio_')))
        self.background_conflict.setVisible(bool(self.background_conflict.text()))
        self.ratio_conflict.setVisible(bool(self.ratio_conflict.text()))
        self.start_button.setEnabled(not self.busy and not self.background_conflict.text())

    def _refresh_colors(self):
        colors = materials.palette()
        self._refresh_single_styles()
        self.background_conflict.setStyleSheet('color: %s;' % colors['error'])
        self.ratio_conflict.setStyleSheet('color: %s;' % colors['warning'])
        for label in (self.stage_label, self.summary_label, *self.item_labels.values()):
            label.setStyleSheet('color: %s;' % colors['text1'])
        self.product_grid.refresh_colors()
        self.style_grid.refresh_colors()
        self._refresh_picker_button_styles()
        self.result_panel.refresh_colors()
        self.task_list.refresh_colors()
        if self._lightbox is not None:
            try:
                self._lightbox._apply_theme()
            except RuntimeError:
                self._lightbox = None
        self.log_box.clear()
        for message, level in self._logs:
            self._insert_log(message, level)

    def _refresh_empty_icon(self):
        # 安装版 QFluentWidgets 未必带灯泡枚举，使用 Qt 自绘保持跨版本一致。
        size = px(40)
        dpr = self.empty_icon.devicePixelRatioF()
        pixmap = QPixmap(round(size * dpr), round(size * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.scale(size / 40, size / 40)
        painter.setPen(QPen(QColor(materials.ecommerce_color('single_empty_icon')),
                            2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        bulb = QPainterPath()
        bulb.moveTo(15, 28)
        bulb.cubicTo(15, 24, 9, 22, 9, 15)
        bulb.cubicTo(9, 1, 31, 1, 31, 15)
        bulb.cubicTo(31, 22, 25, 24, 25, 28)
        bulb.closeSubpath()
        painter.drawPath(bulb)
        painter.drawLine(16, 32, 24, 32)
        painter.drawLine(18, 36, 22, 36)
        painter.end()
        self.empty_icon.setPixmap(pixmap)

    def _refresh_single_styles(self):
        # 样式保留 100% 基线，由 InterfaceScaler 统一缩放，避免叠乘。
        for card in self._single_cards:
            card.setStyleSheet(
                'QFrame#%s {background: %s; border: 1px solid %s; border-radius: 12px;}'
                % (card.objectName(), materials.ecommerce_color('single_card'),
                   materials.ecommerce_color('single_card_border')))
        self.single_title_label.setStyleSheet(
            'font-size: 20px; font-weight: 700; color: %s;' % materials.palette()['text1'])
        self.single_subtitle_label.setStyleSheet(
            'font-size: 12px; color: %s;' % materials.palette()['text2'])
        for label in self._single_card_titles:
            label.setStyleSheet('font-size: 13px; font-weight: 600; color: %s;'
                               % materials.palette()['text1'])
        for button in (self.history_tasks_button, self.batch_creation_button):
            button.setStyleSheet(
                'QPushButton#%s {background: transparent; color: %s; border: 1px solid %s;'
                ' border-radius: 8px; padding: 0 16px; font-size: 13px;}'
                ' QPushButton#%s:hover {background: %s;}'
                % (button.objectName(), materials.ecommerce_color('single_button_text'),
                   materials.ecommerce_color('single_button_border'), button.objectName(),
                   materials.ecommerce_color('single_button_hover')))
        self.start_button.setStyleSheet('border-radius: 8px;')
        self.cancel_button.setStyleSheet('border-radius: 8px;')
        self.empty_title_label.setStyleSheet('font-size: 14px; color: %s;'
                                            % materials.ecommerce_color('single_empty_text'))
        self.empty_subtitle_label.setStyleSheet('font-size: 12px; color: %s;'
                                               % materials.ecommerce_color('single_empty_secondary'))
        self._refresh_empty_icon()

    def _clean(self, value):
        current_key = self.config_manager.config.get('api', {}).get('api_key', '')
        return redact_text(value, self._secrets + (current_key,))

    def _choose_products(self):
        paths, _ = QFileDialog.getOpenFileNames(self, tr('选择产品图'), '', _image_filter())
        paths = [str(Path(path).resolve()) for path in paths]
        if not paths:
            return
        merged = list(dict.fromkeys(self.product_image_paths + paths))
        if len(merged) > 6:
            self._notice(tr('产品图最多 6 张，请移除后再添加'))
            merged = merged[:6]
        self.product_image_paths = merged
        self._refresh_references()

    def _clear_products(self):
        self.product_image_paths = []
        self._refresh_references()

    def _choose_style(self):
        path, _ = QFileDialog.getOpenFileName(self, tr('选择风格图'), '', _image_filter())
        if path:
            self.style_image_paths = [str(Path(path).resolve())]
            self._refresh_references()

    def _clear_style(self):
        self.style_image_paths = []
        self._refresh_references()

    def _refresh_references(self):
        self.product_image_paths = [str(path) for path in self.product_image_paths
                                    if str(path or '').strip()][:6]
        self.style_image_paths = [str(path) for path in self.style_image_paths
                                  if str(path or '').strip()][:1]
        self.product_grid.set_paths(self.product_image_paths, emit=False)
        self.style_grid.set_paths(self.style_image_paths, emit=False)

    def _remove_product_path(self, path):
        try:
            self.product_image_paths.remove(path)
        except ValueError:
            return
        self._refresh_references()

    def _arguments(self):
        selected = self.selected_platforms()
        if not 1 <= len(selected) <= MAX_PLATFORMS:
            raise ValueError(tr('请选择 1 至 {count} 个平台').format(count=MAX_PLATFORMS))
        for conflict in detect_conflicts(selected):
            if conflict['type'] == 'background':
                raise ValueError(conflict['tip'])
        name, info = self.product_name.text().strip(), self.product_info.toPlainText().strip()
        if not name or not info:
            raise ValueError(tr('产品名称与产品信息均为必填'))
        d, g = self.carousel_count.value(), self.detail_count.value()
        if d + g < 1:
            raise ValueError(tr('至少生成 1 张图'))
        if len(self.product_image_paths) > 6 or len(self.style_image_paths) > 1:
            raise ValueError(tr('产品图最多 6 张，风格图最多 1 张'))
        config = self.config_manager.config
        lines = (resolve_line(config, 'chat'), resolve_line(config, 'image'))
        if any(not str(line.get('base_url') or '').strip()
               or not str(line.get('api_key') or '').strip() for line in lines):
            raise ValueError(tr('请先到“设置”页填写 API 地址与密钥；AI 分析需要含文本模型权限的密钥'))
        return dict(platforms=selected, product_name=name, product_info=info,
                    language=self.language.currentData(), carousel_count=d, detail_count=g,
                    carousel_ratio=self.carousel_ratio.currentText(), detail_ratio=self.detail_ratio.currentText(),
                    tier=self.tier.currentText(),
                    product_image_paths=[str(Path(p).resolve()) for p in self.product_image_paths],
                    style_image_path=str(Path(self.style_image_paths[0]).resolve()) if self.style_image_paths else None)

    def _make_manager(self):
        # Model values are snapshotted on the UI thread before the worker starts.
        return EcommerceImageManager.build_from_config(
            self.config_manager, overrides=dict(self._manager_overrides))

    def start_generation(self):
        if self.busy:
            return
        try:
            arguments = self._arguments()
        except ValueError as error:
            self._notice(str(error))
            return
        config = self.config_manager.config
        self._manager_overrides = self.model_overrides()
        arguments['thinking'] = bool(self.thinking_switch.isChecked())
        self._secrets = tuple(resolve_line(config, capability).get('api_key', '')
                               for capability in ('chat', 'image'))
        self._clear_results()
        self._logs.clear()
        self.log_box.clear()
        self.stage_label.setText(tr('等待开始'))
        self._append_log(tr('AI 分析需要含文本模型权限的密钥；图片按所选配置生成。'), 'info')
        self._launch_worker(arguments)

    def _clear_results(self):
        self._group, self._items = None, {}
        self._logged_group_errors.clear()
        for label in self.item_labels.values():
            self.items_layout.removeWidget(label)
            label.deleteLater()
        self.item_labels.clear()
        self.result_panel.clear()
        self.summary_label.clear()
        self.open_output_button.setEnabled(False)
        self.stage_label.setText(tr('等待开始'))
        self._sync_result_columns()

    def _schedule_history_refresh(self):
        """合并逐图进度；隐藏页只记脏状态，进入页面后再读取历史。"""
        self._history_dirty = True
        if self.isVisible():
            self._history_timer.start()

    def _queue_history_refresh(self):
        """按钮回调先返回事件循环，再提交一次最终历史快照。"""
        self._schedule_history_refresh()
        QTimer.singleShot(0, self._flush_history_refresh)

    def _flush_history_refresh(self):
        self._history_timer.stop()
        try:
            if not self._history_dirty:
                return
            self._history_dirty = False
            self._refresh_history()
        except Exception:
            # 失败的快照没有真正提交，保留脏状态让下一次入口可重试。
            self._history_dirty = True
            self._history_signature = None
            self._history_persistence_available = None
            raise
        finally:
            # 首帧隐藏中间态，即使数据库读取失败也不能冻结列表。
            if not self.task_list.updatesEnabled():
                self.task_list.setUpdatesEnabled(True)
                self.task_list.update()

    def _refresh_history(self):
        """只 enrich/绘制发生变化的历史快照，保留列表筛选和选中状态。"""
        store = self.config_manager.ecommerce_store()
        groups = store.list_groups(limit=50)
        signature = tuple((group.get('group_id'), group.get('updated_at'),
                           group.get('status')) for group in groups)
        available = bool(store.persistence_available)
        if (signature == self._history_signature
                and available == self._history_persistence_available):
            return
        self._history_signature = signature
        self._history_persistence_available = available
        group_ids = {group.get('group_id', '') for group in groups}
        for group_id in set(self._enrich_cache) - group_ids:
            self._enrich_cache.pop(group_id, None)
        for group in groups:
            self._enrich_history_group(store, group)
        self.task_list.update_groups(redact_structure(groups, self._clean),
                                     available)
        self.history_records_changed.emit()

    def _enrich_history_group(self, store, group):
        group_id = group.get('group_id', '')
        cached = self._enrich_cache.get(group_id)
        if cached is not None and cached['updated_at'] == group.get('updated_at'):
            group['thumbs'] = list(cached['thumbs'])
            group['image_model'] = cached['image_model']
            return
        manifest = group.get('manifest')
        items = []
        if isinstance(manifest, dict) and isinstance(manifest.get('items'), list):
            items = [item for item in manifest['items'] if isinstance(item, dict)]
        else:
            try:
                items = store.list_items(group_id)
            except Exception:  # noqa: BLE001 - 历史列表不应因数据库异常崩溃
                items = []
        if group.get('status') in ('analyzing', 'generating'):
            try:
                counts = store.item_counts(group_id)
                group['totals'] = dict(counts, total=sum(counts.values()))
            except Exception:  # noqa: BLE001
                pass
        thumbs = []
        ordered = sorted(
            items, key=lambda item: (0 if item.get('kind') == 'carousel' else 1,
                                    item.get('seq') if isinstance(item.get('seq'), int) else 0))
        checked = 0
        for item in ordered:
            if item.get('status') != 'succeeded':
                continue
            path = item.get('file') or item.get('file_path') or ''
            if not path:
                continue
            checked += 1
            try:
                if os.path.isfile(path):
                    thumbs.append(str(path))
            except (OSError, ValueError):
                pass
            if checked >= 3:
                break
        group['thumbs'] = thumbs
        config = group.get('config') if isinstance(group.get('config'), dict) else {}
        manifest = manifest if isinstance(manifest, dict) else {}
        group['image_model'] = (config.get('image_model')
                                or manifest.get('image_model') or '')
        self._enrich_cache[group_id] = dict(
            updated_at=group.get('updated_at'), thumbs=list(thumbs),
            image_model=group['image_model'])

    def _view_history_group(self, group_id):
        if self.busy:
            return
        if not self.config_manager.ecommerce_store().get_group(group_id):
            self._notice(tr('当前生成组不存在'))
            self._queue_history_refresh()
            return
        self.show_single_creation()
        self._clear_results()
        self._refresh_results(group_id, refresh_history=False)
        self._queue_history_refresh()

    def _run_resume(self, group_id):
        if self.busy:
            return
        group = self.config_manager.ecommerce_store().get_group(group_id)
        if not group:
            self._notice(tr('当前生成组不存在'))
            return
        if group['status'] not in RESUMABLE_STATUSES:
            self._notice(tr('当前组无需断点恢复'))
            return
        self._view_history_group(group_id)
        self._secrets = (self.config_manager.config.get('api', {}).get('api_key', ''),)
        self.summary_label.clear()
        self.stage_label.setText(tr('图片生成中'))
        self._append_log(tr('断点恢复'), 'info')
        self._launch_worker(dict(group_id=group_id), operation='resume')

    def _open_folder(self, output_dir):
        try:
            exists = bool(output_dir) and Path(output_dir).is_dir()
        except (OSError, ValueError):
            exists = False
        if not exists:
            self._notice(tr('输出文件夹不存在'))
            return
        open_local(output_dir, self._append_log, folder=True)

    def _open_history_folder(self, group_id):
        group = self.config_manager.ecommerce_store().get_group(group_id)
        if group:
            self._open_folder(group.get('output_dir'))
        else:
            self._notice(tr('当前生成组不存在'))

    def _delete_history_groups(self, group_ids):
        """任务列表确认后批量删除记录（只删记录，不删已生成文件）。"""
        if self.busy:
            return
        store = self.config_manager.ecommerce_store()
        removed_current = False
        for group_id in list(group_ids or ()):
            group_id = str(group_id)
            if store.get_group(group_id) and store.delete_group(group_id):
                self._enrich_cache.pop(group_id, None)
                if self._group and self._group['group_id'] == group_id:
                    removed_current = True
        if removed_current:
            self._clear_results()
        self._queue_history_refresh()

    def _run_regenerate(self, item_keys=None):
        if self.busy:
            return
        try:
            store = self.config_manager.ecommerce_store()
            group = store.get_group(self._group['group_id']) if self._group else None
            if not group:
                raise ValueError(tr('当前生成组不存在'))
            items = store.list_items(group['group_id'])
            by_id = {item['item_id']: item for item in items}
            if item_keys is None:
                keys = [item['item_id'] for item in items if item['status'] == 'failed']
                if not keys:
                    raise ValueError(tr('没有需要重生成的失败项'))
            else:
                keys = []
                for key in item_keys:
                    if isinstance(key, tuple) and len(key) == 2:
                        kind, seq = key
                        if kind not in ('carousel', 'detail') or type(seq) is not int:
                            raise ValueError(tr('请选择当前组中的图片'))
                        key = f'{group["group_id"]}:{kind}:{seq}'
                    if not isinstance(key, str) or key not in by_id:
                        raise ValueError(tr('请选择当前组中的图片'))
                    if key not in keys:
                        keys.append(key)
                if not keys:
                    raise ValueError(tr('请选择当前组中的图片'))
        except (ValueError, TypeError) as error:
            self._notice(str(error))
            return
        self._secrets = (self.config_manager.config.get('api', {}).get('api_key', ''),)
        self.summary_label.clear()
        self.stage_label.setText(tr('图片生成中'))
        self._launch_worker(dict(group_id=group['group_id'], item_keys=keys), operation='regenerate')

    def _launch_worker(self, arguments, *, operation='start'):
        # Each worker run is a new reporting window, including regeneration of
        # an existing group.  Do not suppress an identical error from the new run.
        self._logged_group_errors.clear()
        worker = EcommerceWorker(self._make_manager, arguments, self.cancel_event, self._secrets,
                                 operation=operation)
        self._worker = worker
        _LIVE_WORKERS.add(worker)
        worker.group_changed.connect(self._on_group, Qt.QueuedConnection)
        worker.item_changed.connect(self._on_item, Qt.QueuedConnection)
        worker.log_message.connect(self._append_log, Qt.QueuedConnection)
        worker.finished.connect(self._on_result, Qt.QueuedConnection)
        native_finished = QThread.finished.__get__(worker, QThread)
        native_finished.connect(self._thread_stopped, Qt.QueuedConnection)
        native_finished.connect(lambda: _LIVE_WORKERS.discard(worker))
        native_finished.connect(worker.deleteLater)
        for card in self._form_cards:
            card.setEnabled(False)
        self.start_button.setEnabled(False)
        self.result_panel.set_busy(True)
        self.task_list.set_busy(True)
        self.batch_page.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self._sync_result_columns()
        worker.start()

    def cancel_generation(self):
        if self.busy:
            self.cancel_event.set()
            self.cancel_button.setEnabled(False)
            self._append_log(tr('已请求取消；未开始的图片将跳过，等待已发出的请求结束。'), 'warning')

    @pyqtSlot(object)
    def _on_group(self, record, *, refresh_history=True):
        if (self._group and self._group['group_id'] == record['group_id']
                and self._group.get('updated_at', 0) > record.get('updated_at', 0)):
            return
        self._group = redact_structure(record, self._clean)
        status = self._group['status']
        self.stage_label.setText(_stage_text(status))
        if status not in ('analyzing', 'generating'):
            totals = self._group.get('totals', {})
            self.summary_label.setText(tr('成功 {success} / 失败 {failed} / 跳过 {skipped}').format(
                success=totals.get('succeeded', 0), failed=totals.get('failed', 0), skipped=totals.get('skipped', 0)))
            self.open_output_button.setEnabled(bool(self._group.get('output_dir')))
            for item in self.config_manager.ecommerce_store().list_items(self._group['group_id']):
                self._on_item(item)
        error = self._group.get('error')
        group_id = self._group.get('group_id')
        if error and self._logged_group_errors.get(group_id) != error:
            self._logged_group_errors[group_id] = error
            self._append_log(error, 'error')
        if refresh_history:
            self._schedule_history_refresh()
            if status in ('succeeded', 'partial', 'failed', 'cancelled'):
                self._flush_history_refresh()
        self._sync_result_columns()

    @pyqtSlot(object)
    def _on_item(self, record):
        key = (record['kind'], record['seq'])
        previous = self._items.get(key)
        if (previous and previous['item_id'] == record['item_id']
                and previous.get('updated_at', 0) > record.get('updated_at', 0)):
            return
        record = redact_structure(record, self._clean)
        record['output_dir'] = (self._group or {}).get('output_dir', '')
        self._items[key] = record
        if key not in self.item_labels:
            label = self._text_label()
            self.item_labels[key] = label
            self.items_layout.insertWidget(sorted(self.item_labels).index(key), label)
            label.setStyleSheet('color: %s;' % materials.palette()['text1'])
        name = tr('轮') if record['kind'] == 'carousel' else tr('详')
        parts = [f'{name}{record["seq"]}：{_item_text(record["status"])}']
        if record.get('model'):
            parts.append(tr('模型：{model}').format(model=record['model']))
        if record.get('size'):
            parts.append(record['size'])
        parts.extend(record.get('warnings', []))
        error_text, error_tooltip = EcommerceResultCard._display_error(record.get('error'), record)
        if error_text:
            parts.append(error_text)
        self.item_labels[key].setText(' · '.join(parts))
        self.item_labels[key].setToolTip(error_tooltip)
        self.result_panel.update_item(record)
        card = self.result_panel.cards.get(record.get('item_id'))
        if card is not None and record.get('model'):
            card.status_label.setText('{} · {}'.format(
                card.status_label.text(), tr('模型：{model}').format(model=record['model'])))
        self._schedule_history_refresh()
        self._sync_result_columns()

    def _refresh_results(self, group_id=None, *, refresh_history=True):
        group_id = group_id or (self._group or {}).get('group_id')
        if group_id:
            store = self.config_manager.ecommerce_store()
            group = store.get_group(group_id)
            if group:
                self._on_group(group, refresh_history=refresh_history)
                if group['status'] in ('analyzing', 'generating'):
                    for item in store.list_items(group_id):
                        self._on_item(item)

    @pyqtSlot(str, str)
    def _append_log(self, message, level='info'):
        message = self._clean(message)
        level = level if level in ('info', 'warning', 'error') else 'info'
        self._logs.append((message, level))
        self._logs = self._logs[-1000:]
        self._insert_log(message, level)

    def _insert_log(self, message, level):
        cursor = self.log_box.textCursor()
        cursor.movePosition(QTextCursor.End)
        format_ = QTextCharFormat()
        format_.setForeground(QColor(materials.palette()[{'info': 'text1', 'warning': 'warning', 'error': 'error'}[level]]))
        cursor.insertText(f'[{level}] {message}\n', format_)
        self.log_box.setTextCursor(cursor)
        self.log_box.ensureCursorVisible()

    @pyqtSlot(str, str)
    def _on_result(self, group_id, error):
        operation = getattr(self._worker, 'operation', 'start') if self._worker else 'start'
        self._refresh_results(group_id, refresh_history=False)
        if operation == 'batch':
            if error:
                self._append_log(tr('批量制作异常中断：{message}').format(message=error), 'error')
                self._notice(error)
            else:
                self._append_log(tr('批量制作结束，可在任务列表查看各任务组'), 'info')
            self.show_task_list()
            self._flush_history_refresh()
            return
        if error:
            self.stage_label.setText(tr('失败'))
            group_id = group_id or (self._group or {}).get('group_id')
            if self._logged_group_errors.get(group_id) != error:
                self._logged_group_errors[group_id] = error
                self._append_log(error, 'error')
            self._notice(error)
        elif self._group:
            self._append_log(self.stage_label.text() + ' · ' + self.summary_label.text(), 'info')
        self._schedule_history_refresh()
        self._flush_history_refresh()

    @pyqtSlot()
    def _thread_stopped(self):
        # Pool callbacks may be queued from different threads. Read the final
        # database snapshot once more before enabling regeneration actions.
        self._refresh_results(refresh_history=False)
        self._worker = None
        self.cancel_event = threading.Event()
        for card in self._form_cards:
            card.setEnabled(True)
        for capability in ('chat', 'image'):
            _picker, _dot, button = self._model_probe_controls(capability)
            button.setEnabled(capability not in self._model_probe_workers)
        self.cancel_button.setEnabled(False)
        self.result_panel.set_busy(False)
        self.task_list.set_busy(False)
        try:
            self.batch_page.setEnabled(True)
        except RuntimeError:
            pass
        self._refresh_conflicts()
        self._sync_result_columns()
        self._schedule_history_refresh()
        self._flush_history_refresh()
        self.idle.emit()
        if self._close_when_idle:
            self.close()

    def open_output(self):
        if self._group and self._group.get('output_dir'):
            self._open_folder(self._group['output_dir'])

    def retranslate_ui(self):
        """语言切换：重翻本页静态控件与子组件/动态状态（幂等，异常兜底）。"""
        for label, source in (
                (self.single_title_label, '电商图制作'),
                (self.single_subtitle_label, '上传产品图，AI 自动生成电商轮播图与详情图'),
                (self.history_tasks_button, '历史任务'),
                (self.batch_creation_button, '批量制作'),
                (self.empty_title_label, '填写完左侧信息后点击「开始制作」'),
                (self.empty_subtitle_label, 'AI 将自动分析产品并生成图片')):
            label.setText(tr(source))
        try:
            self.task_list.retranslate_ui()
        except Exception:
            pass
        try:
            self.batch_page.retranslate_ui()
        except Exception:
            pass
        try:
            self.result_panel.retranslate_ui()
        except Exception:
            pass
        for grid in (getattr(self, 'product_grid', None), getattr(self, 'style_grid', None)):
            try:
                grid.retranslate_ui()
            except Exception:
                pass
        try:
            from ..i18n_helpers import retranslate_tree
            retranslate_tree(self)
        except Exception:
            pass
        try:
            self.refresh_effective_lines()
            self.refresh_model_pickers()
            if self._group:
                status = self._group.get('status')
                self.stage_label.setText(_stage_text(status))
                totals = self._group.get('totals', {})
                self.summary_label.setText(
                    tr('成功 {success} / 失败 {failed} / 跳过 {skipped}').format(
                        success=totals.get('succeeded', 0),
                        failed=totals.get('failed', 0),
                        skipped=totals.get('skipped', 0)))
                for item in self.config_manager.ecommerce_store().list_items(
                        self._group['group_id']):
                    self._on_item(item)
            elif not self.busy:
                self.stage_label.setText(tr('等待开始'))
        except Exception:
            pass
        for capability in ('chat', 'image'):
            try:
                _picker, dot, _button = self._model_probe_controls(capability)
                identity = self._model_probe_fingerprints.get(capability)
                cached = self._model_probe_cache.get(identity) if identity else None
                if cached is not None:
                    self._apply_probe_result(capability, cached, cached_mode=True)
                else:
                    dot.setToolTip(tr('未测试'))
            except Exception:
                pass

    def showEvent(self, event):
        super().showEvent(event)
        if self._history_first_show:
            self._history_first_show = False
            self.task_list.setUpdatesEnabled(False)
        self._fit_model_columns()
        self._fit_single_columns()
        self._probe_closing = False
        self._probe_autostart = True
        for capability in ('chat', 'image'):
            identity = self._model_probe_fingerprint(capability)
            cached = self._model_probe_cache.get(identity)
            if cached is not None:
                self._apply_probe_result(capability, cached, cached_mode=True)
            self._schedule_auto_probe(capability)
            _picker, _dot, button = self._model_probe_controls(capability)
            button.setEnabled(not self.busy and capability not in self._model_probe_workers)
        self._history_dirty = True
        QTimer.singleShot(0, self._flush_history_refresh)

    def hideEvent(self, event):
        self._history_timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event):
        if self.busy:
            self._close_when_idle = True
            self.cancel_generation()
            event.ignore()
            return
        self._probe_autostart = False
        self._history_timer.stop()
        for timer in getattr(self, '_model_probe_timers', {}).values():
            try:
                timer.stop()
            except RuntimeError:
                pass
        if self._lightbox is not None:
            try:
                self._lightbox.close()
            except RuntimeError:
                pass
            self._lightbox = None
        self._probe_closing = True
        super().closeEvent(event)

