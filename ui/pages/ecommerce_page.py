"""Ecommerce form and queued Qt presentation of the existing Python pipeline."""
from pathlib import Path
from copy import deepcopy
import threading
from urllib.parse import urlsplit

from PyQt5.QtCore import QEvent, Qt, QThread, QSignalBlocker, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QColor, QTextCharFormat, QTextCursor
from PyQt5.QtWidgets import (
    QFileDialog, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    CardWidget, CaptionLabel, CheckBox, ComboBox, InfoBar, LineEdit,
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
from ..components.ecommerce_history_panel import EcommerceHistoryPanel, RESUMABLE_STATUSES
from ..components.image_lightbox import ImageLightbox
from ..components.image_pickers import (
    GalleryPickerDialog, UsedImagePickerDialog,
)
from ..components.image_upload_grid import ImageUploadGrid
from ..components.model_selector import ModelPickComboBox, catalog_snapshot
from core.model_catalog import normalize_model_id
from ..components.studio_dialog import StudioDialog, safe_delete_dialog
from ..file_actions import open_local
from ..responsive import WrapLayout, scrollable_body
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
        self._close_when_idle = False
        self._manager_overrides = {}
        self.product_image_paths = []
        self.style_image_paths = []
        self._form_cards = []
        self._model_probe_workers = {}
        self._model_probe_revisions = {'chat': 0, 'image': 0}
        self._model_probe_fingerprints = {}
        self._probe_closing = False
        self._lightbox = None
        self._platform_selection_order = []
        self._picker_buttons = []
        defaults = config_manager.config.get('ecommerce', {})

        root = scrollable_body(self)
        root.setContentsMargins(px(26), px(16), px(26), px(20))
        root.setSpacing(px(16))
        root.addWidget(TitleLabel(tr('电商图制作')))
        hint = CaptionLabel(tr('选择平台与产品资料 · AI分析 → 图片生成 → 完成后打开输出目录'))
        hint.setWordWrap(True)
        root.addWidget(hint)

        platform_layout = self._card(root, tr('选择平台（最多 {count} 个）').format(count=MAX_PLATFORMS))
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
        self.product_name = LineEdit()
        self.product_name.setPlaceholderText(tr('产品名称（必填）'))
        self.product_info = PlainTextEdit()
        self.product_info.setPlaceholderText(tr('产品信息（必填）：材质、尺寸、功能、卖点、适用场景等'))
        self.product_info.setMinimumHeight(px(112))
        product_layout.addWidget(self.product_name)
        product_layout.addWidget(self.product_info)

        options = self._card(root, tr('图片配置'))
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

        self._upload_card(root)

        models = self._card(root, tr('模型设置'))
        self.model_grid = QGridLayout()
        self.model_grid.setContentsMargins(0, 0, 0, 0)
        self.model_grid.setHorizontalSpacing(px(10))
        self.model_grid.setVerticalSpacing(px(8))
        self.model_grid.setColumnStretch(0, 1)
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
            '开启后，DeepSeek 官方线路的 deepseek-chat 自动切换为 deepseek-v4-pro；'
            'Claude 思考模型加 thinking 参数；其他模型不受影响'))
        self.model_grid.addWidget(CaptionLabel(tr('深度思考')), 4, 0)
        self.model_grid.addWidget(self.thinking_switch, 4, 1, Qt.AlignLeft | Qt.AlignVCenter)
        models.addLayout(self.model_grid)
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

        actions = WrapLayout()
        self.start_button = PrimaryPushButton(tr('开始生成'))
        self.cancel_button = PushButton(tr('取消'))
        self.cancel_button.setEnabled(False)
        self.start_button.clicked.connect(self.start_generation)
        self.cancel_button.clicked.connect(self.cancel_generation)
        actions.addWidget(self.start_button)
        actions.addWidget(self.cancel_button)
        root.addLayout(actions)
        progress = self._card(root, tr('生成进度'), form=False)
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
        root.addWidget(self.result_panel)
        self.history_panel = EcommerceHistoryPanel(_stage_text, self)
        self.history_panel.refresh_requested.connect(self._refresh_history)
        self.history_panel.view_requested.connect(self._view_history_group)
        self.history_panel.resume_requested.connect(self._run_resume)
        self.history_panel.open_requested.connect(self._open_history_folder)
        self.history_panel.delete_requested.connect(self._delete_history_group)
        root.addWidget(self.history_panel)
        root.addStretch(1)
        style_controls(self)
        style_page(self)
        materials.register_theme_callback(self._refresh_colors)
        self._refresh_colors()
        self._refresh_conflicts()
        self._refresh_history()
        self.body_scroll.viewport().installEventFilter(self)

    @property
    def busy(self):
        return self._worker is not None

    def _card(self, root, title, form=True):
        card = CardWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(px(18), px(16), px(18), px(16))
        layout.setSpacing(px(12))
        layout.addWidget(StrongBodyLabel(tr(title)))
        root.addWidget(card)
        if form:
            self._form_cards.append(card)
        return layout

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
        card = CardWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(px(18), px(16), px(18), px(16))
        layout.setSpacing(px(10))
        self._form_cards.append(card)

        layout.addLayout(self._upload_header(
            tr('产品图'), target='product', required=True,
            hint=tr('上传产品白底图，最多 6 张')))
        self.product_grid = ImageUploadGrid('product', 6, self)
        self.product_grid.changed.connect(self._on_product_images_changed)
        self.product_grid.preview_requested.connect(self._preview_product_image)
        self.product_grid.add_requested.connect(self._choose_products)
        layout.addWidget(self.product_grid)

        layout.addSpacing(px(8))
        layout.addLayout(self._upload_header(
            tr('风格参考图'), target='style', optional=True,
            hint=tr('可选，上传参考图来指定风格')))
        self.style_grid = ImageUploadGrid('style', 1, self)
        self.style_grid.changed.connect(self._on_style_images_changed)
        self.style_grid.preview_requested.connect(self._preview_style_image)
        self.style_grid.add_requested.connect(self._choose_style)
        layout.addWidget(self.style_grid)
        root.addWidget(card)
        return card

    def _upload_header(self, title, *, target, required=False, optional=False, hint=''):
        header = QHBoxLayout()
        header.setSpacing(px(8))
        header.addWidget(StrongBodyLabel(title))
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
        header.addWidget(hint_label)
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
        return header

    def _picker_button(self, text, tooltip):
        button = PushButton(text)
        button.setFixedHeight(px(24))
        button.setToolTip(tooltip)
        button.setCursor(Qt.PointingHandCursor)
        self._picker_buttons.append(button)
        return button

    def _refresh_picker_button_styles(self):
        colors = materials.palette()
        accent = QColor(colors['accent'])
        hover = accent.darker(112).name()
        disabled = QColor(colors['text3']).name()
        for button in self._picker_buttons:
            try:
                button.setStyleSheet(
                    'QPushButton {background: %s; color: %s; border: 0;'
                    ' border-radius: %dpx; padding: 2px %dpx;'
                    ' font-size: %dpx; font-weight: 500;}'
                    ' QPushButton:hover {background: %s;}'
                    ' QPushButton:disabled {background: %s; color: %s;}'
                    % (accent.name(), materials.ECOMMERCE['white'], px(6),
                       px(8), px(12), hover, materials.rgba(colors['border']),
                       disabled))
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
        available = (self.body_scroll.viewport().width() - outer.left() - outer.right()
                     - inner.left() - inner.right())
        spacing = max(0, self.model_grid.horizontalSpacing()) * 4
        reserved = sum(max(first.minimumSizeHint().width(), second.minimumSizeHint().width(),
                           first.minimumWidth(), second.minimumWidth())
                       for first, second in ((self.analysis_line_label, self.image_line_label),
                                             (self.analysis_status_dot, self.image_status_dot),
                                             (self.sync_analysis_models_button,
                                              self.sync_image_models_button)))
        combo_width = min(px(200), max(px(120), available - spacing - reserved - px(260)))
        picker_width = min(px(260), max(px(140), available - spacing - reserved - combo_width))
        for picker in (self.analysis_model_picker, self.image_model_picker):
            picker.setMinimumWidth(picker_width)
        for combo in (self.analysis_line_combo, self.image_line_combo):
            combo.setMinimumWidth(combo_width)
            combo.setMaximumWidth(px(200))

    def refresh_interface_scale(self):
        self._fit_model_columns()
        for attribute in ('product_grid', 'style_grid'):
            grid = getattr(self, attribute, None)
            if grid is not None:
                grid.refresh_interface_scale()

    def eventFilter(self, watched, event):
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
        if self._model_probe_fingerprints.get(capability) != identity:
            self._model_probe_fingerprints[capability] = identity
            self._model_probe_revisions[capability] += 1
            _picker, dot, _button = self._model_probe_controls(capability)
            dot.set_color('#92929b')
            dot.setToolTip(tr('未测试'))

    def _test_selected_model(self, capability):
        if self.busy or self._probe_closing or capability in self._model_probe_workers:
            return
        picker, dot, button = self._model_probe_controls(capability)
        self._refresh_model_probe_identity(capability)
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
        _picker, dot, _button = self._model_probe_controls(capability)
        ok = bool(result.get('ok'))
        message = self._clean(result.get('message') or tr('模型测试失败，请检查线路与模型'))
        dot.set_color('#67c23a' if ok else '#f56c6c')
        dot.setToolTip(tr('模型测试通过：{message}').format(message=message) if ok else message)
        if not ok:
            title = tr('文本模型测试失败') if capability == 'chat' else tr('图片模型测试失败')
            InfoBar.error(tr('电商图'), title + ' · ' + message, parent=self, duration=5000)
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
        self.background_conflict.setStyleSheet('color: %s;' % colors['error'])
        self.ratio_conflict.setStyleSheet('color: %s;' % colors['warning'])
        for label in (self.stage_label, self.summary_label, *self.item_labels.values()):
            label.setStyleSheet('color: %s;' % colors['text1'])
        self.product_grid.refresh_colors()
        self.style_grid.refresh_colors()
        self._refresh_picker_button_styles()
        self.result_panel.refresh_colors()
        self.history_panel.refresh_colors()
        if self._lightbox is not None:
            try:
                self._lightbox._apply_theme()
            except RuntimeError:
                self._lightbox = None
        self.log_box.clear()
        for message, level in self._logs:
            self._insert_log(message, level)

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
        self.stage_label.setText(tr('AI分析中'))
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

    def _refresh_history(self):
        store = self.config_manager.ecommerce_store()
        groups = store.list_groups(limit=50)
        for group in groups:
            if group['status'] in ('analyzing', 'generating'):
                counts = store.item_counts(group['group_id'])
                group['totals'] = dict(counts, total=sum(counts.values()))
        self.history_panel.update_groups(redact_structure(groups, self._clean), store.persistence_available)
        self.history_records_changed.emit()

    def _view_history_group(self, group_id):
        if self.busy:
            return
        if not self.config_manager.ecommerce_store().get_group(group_id):
            self._notice(tr('当前生成组不存在'))
            self._refresh_history()
            return
        self._clear_results()
        self._refresh_results(group_id)

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

    def _confirm_delete(self, group):
        dialog = StudioDialog(self, tr('删除记录'))
        dialog.resize(px(440), px(220))
        dialog.body_layout.addWidget(self._text_label(
            self._clean(group.get('product_name') or group['group_id'][:8])))
        dialog.body_layout.addWidget(self._text_label(tr('仅删除记录，不删除已生成文件')))
        actions = WrapLayout()
        yes, no = PrimaryPushButton(tr('是')), PushButton(tr('否'))
        yes.setObjectName('confirmDeleteRecord')
        no.setObjectName('keepRecord')
        yes.clicked.connect(dialog.accept)
        no.clicked.connect(dialog.reject)
        actions.addWidget(yes)
        actions.addWidget(no)
        dialog.body_layout.addLayout(actions)
        style_controls(dialog)
        try:
            return dialog.exec_() == dialog.Accepted
        finally:
            safe_delete_dialog(dialog)

    def _delete_history_group(self, group_id):
        if self.busy:
            return
        store = self.config_manager.ecommerce_store()
        group = store.get_group(group_id)
        if group and self._confirm_delete(group) and not self.busy:
            store.delete_group(group_id)
            if self._group and self._group['group_id'] == group_id:
                self._clear_results()
        self._refresh_history()

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
        self.history_panel.set_busy(True)
        self.cancel_button.setEnabled(True)
        worker.start()

    def cancel_generation(self):
        if self.busy:
            self.cancel_event.set()
            self.cancel_button.setEnabled(False)
            self._append_log(tr('已请求取消；未开始的图片将跳过，等待已发出的请求结束。'), 'warning')

    @pyqtSlot(object)
    def _on_group(self, record):
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
        self._refresh_history()

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
        self._refresh_history()

    def _refresh_results(self, group_id=None):
        group_id = group_id or (self._group or {}).get('group_id')
        if group_id:
            store = self.config_manager.ecommerce_store()
            group = store.get_group(group_id)
            if group:
                self._on_group(group)
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
        self._refresh_results(group_id)
        self._refresh_history()
        if error:
            self.stage_label.setText(tr('失败'))
            group_id = group_id or (self._group or {}).get('group_id')
            if self._logged_group_errors.get(group_id) != error:
                self._logged_group_errors[group_id] = error
                self._append_log(error, 'error')
            self._notice(error)
        elif self._group:
            self._append_log(self.stage_label.text() + ' · ' + self.summary_label.text(), 'info')

    @pyqtSlot()
    def _thread_stopped(self):
        # Pool callbacks may be queued from different threads. Read the final
        # database snapshot once more before enabling regeneration actions.
        self._refresh_results()
        self._worker = None
        self.cancel_event = threading.Event()
        for card in self._form_cards:
            card.setEnabled(True)
        for capability in ('chat', 'image'):
            _picker, _dot, button = self._model_probe_controls(capability)
            button.setEnabled(capability not in self._model_probe_workers)
        self.cancel_button.setEnabled(False)
        self.result_panel.set_busy(False)
        self.history_panel.set_busy(False)
        self._refresh_conflicts()
        self.idle.emit()
        if self._close_when_idle:
            self.close()

    def open_output(self):
        if self._group and self._group.get('output_dir'):
            self._open_folder(self._group['output_dir'])

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_model_columns()
        self._probe_closing = False
        for capability in ('chat', 'image'):
            _picker, _dot, button = self._model_probe_controls(capability)
            button.setEnabled(not self.busy and capability not in self._model_probe_workers)
        self._refresh_history()

    def closeEvent(self, event):
        if self.busy:
            self._close_when_idle = True
            self.cancel_generation()
            event.ignore()
            return
        if self._lightbox is not None:
            try:
                self._lightbox.close()
            except RuntimeError:
                pass
            self._lightbox = None
        self._probe_closing = True
        super().closeEvent(event)
