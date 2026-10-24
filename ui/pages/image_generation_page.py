"""Standalone AI image generation form.

The page intentionally owns presentation and submission orchestration only.  The
network protocol and task persistence live in :mod:`core.manual_image`; this
module can therefore be rendered and exercised entirely offline.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

from PyQt5.QtCore import QCoreApplication, QEvent, Qt, QSignalBlocker, QThread, pyqtSignal, QRectF
from PyQt5.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPixmap
from PyQt5.QtWidgets import (
    QBoxLayout, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPlainTextEdit, QScrollArea, QSizePolicy, QSpinBox, QVBoxLayout,
    QToolButton, QWidget,
)
from qfluentwidgets import (
    ComboBox, InfoBar, LineEdit, PrimaryPushButton, PushButton, StrongBodyLabel,
    TitleLabel, FluentIcon as FIF,
)

from core.i18n import register_retranslate, tr
from core.model_catalog import builtin_models, classify_model, normalize_model_id
from .. import materials
from ..components.image_lightbox import ImageLightbox
from ..components.image_pickers import GalleryPickerDialog, UsedImagePickerDialog
from ..components.image_upload_grid import ImageUploadGrid
from ..components.model_selector import catalog_snapshot
from ..responsive import scrollable_body
from ..theme import style_controls, style_page
from ..tokens import px

_ORPHAN_WORKERS = set()
_ORPHAN_WORKERS_LOCK = threading.Lock()
_SHUTDOWN_HOOK_CONNECTED = False


def _dispose_orphan_worker(worker):
    with _ORPHAN_WORKERS_LOCK:
        _ORPHAN_WORKERS.discard(worker)
    try:
        worker.deleteLater()
    except RuntimeError:
        pass


def _wait_for_orphan_workers():
    with _ORPHAN_WORKERS_LOCK:
        workers = tuple(_ORPHAN_WORKERS)
    for worker in workers:
        if worker.isRunning():
            worker.wait()
        _dispose_orphan_worker(worker)


class _QuickPromptCard(QFrame):
    clicked = pyqtSignal()

    def __init__(self, index, text, parent=None):
        super().__init__(parent)
        self.setObjectName('quickPromptItem')
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(text)
        self.setMinimumHeight(px(54))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(px(12), px(8), px(12), px(8))
        layout.setSpacing(px(12))
        badge = QLabel(str(index), self)
        badge.setAlignment(Qt.AlignCenter)
        badge.setFixedSize(px(28), px(28))
        badge.setObjectName('quickPromptIndex')
        label = QLabel(text, self)
        label.setWordWrap(True)
        label.setObjectName('quickPromptText')
        layout.addWidget(badge, 0, Qt.AlignTop)
        layout.addWidget(label, 1)
        self.refresh_colors()

    def refresh_colors(self):
        border = materials.ecommerce_color('image_generation_border')
        surface = materials.ecommerce_color('image_generation_surface')
        primary = materials.ecommerce_color('image_generation_primary')
        selected = materials.ecommerce_color('image_generation_primary_soft')
        self.setStyleSheet(
            'QFrame#quickPromptItem {background:%s; border:1px solid %s; border-radius:%dpx;}'
            'QFrame#quickPromptItem:hover, QFrame#quickPromptItem[selectedPrompt="true"] '
            '{background:%s; border:1px solid %s;}'
            'QLabel#quickPromptIndex {background:%s; color:white; border-radius:%dpx; font-weight:600;}'
            'QLabel#quickPromptText {background:transparent; border:0;}'
            % (surface, border, px(8), selected, primary, primary, px(6)))

    def click(self):
        self.clicked.emit()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


DEFAULT_QUICK_PROMPTS = (
    '美丽的日出风景，金色的阳光洒在宁静的湖面上，远处是连绵的山脉',
    '未来主义城市景观，高耸的摩天大楼，霓虹灯闪烁，飞行汽车穿梭其间',
    '一只可爱的小猫，毛茸茸的，大眼睛，正在玩毛线球',
    '色彩鲜艳的抽象艺术作品，充满动感和创意',
    '春天的樱花树下，粉色的花瓣随风飘落，阳光透过树枝洒下',
    '宇宙飞船在浩瀚的星空中航行，远处是美丽的星云和行星',
)
RATIOS = ('9:16', '1:1', '16:9', '3:2', '2:3', '4:3', '3:4', '5:4')
TIERS = ('1K', '2K', '4K')


class _CountStepper(QWidget):
    """Compact minus/value/plus control used for generation count.

    The small compatibility surface mirrors the QSpinBox methods the page and
    existing callers use, while keeping the visual control deterministic at
    each interface scale.
    """

    valueChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('imageGenerationCountStepper')
        self._minimum = 1
        self._maximum = 10
        self._value = 1
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setFixedHeight(px(34))
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        self.minus_button = QToolButton(self)
        self.minus_button.setObjectName('imageGenerationCountMinus')
        self.minus_button.setText('\N{MINUS SIGN}')
        self.minus_button.setToolTip(tr('减少生成次数'))
        self.minus_button.setFixedSize(px(28), px(28))
        self.minus_button.clicked.connect(lambda: self.setValue(self._value - 1))

        self.value_label = QLabel(str(self._value), self)
        self.value_label.setObjectName('imageGenerationCountValue')
        self.value_label.setAlignment(Qt.AlignCenter)
        self.value_label.setFixedSize(px(72), px(34))

        self.plus_button = QToolButton(self)
        self.plus_button.setObjectName('imageGenerationCountPlus')
        self.plus_button.setText('+')
        self.plus_button.setToolTip(tr('增加生成次数'))
        self.plus_button.setFixedSize(px(28), px(28))
        self.plus_button.clicked.connect(lambda: self.setValue(self._value + 1))

        row.addWidget(self.minus_button, 0, Qt.AlignVCenter)
        row.addWidget(self.value_label, 0, Qt.AlignVCenter)
        row.addWidget(self.plus_button, 0, Qt.AlignVCenter)
        self.refresh_colors()
        self._refresh_buttons()

    def refresh_colors(self):
        border = materials.ecommerce_color('image_generation_border')
        surface = materials.ecommerce_color('image_generation_surface')
        primary = materials.ecommerce_color('image_generation_primary')
        hint = materials.ecommerce_color('image_generation_hint')
        self.setStyleSheet(
            'QToolButton {background:%s; color:%s; border:1px solid %s; border-radius:%dpx; '
            'font-size:%dpx; font-weight:600;}'
            'QToolButton:hover:enabled {background:%s; color:%s;}'
            'QToolButton:disabled {background:%s; color:%s;}'
            'QLabel#imageGenerationCountValue {background:%s; color:%s; border:1px solid %s; '
            'border-radius:%dpx; font-size:%dpx;}'
            % (surface, primary, border, px(6), px(14),
               materials.ecommerce_color('image_generation_primary_soft'), primary,
               surface, hint, surface, primary, border, px(6), px(14)))

    def refresh_interface_scale(self):
        self.setFixedHeight(px(34))
        self.minus_button.setFixedSize(px(28), px(28))
        self.plus_button.setFixedSize(px(28), px(28))
        self.value_label.setFixedSize(px(72), px(34))
        self.refresh_colors()

    def retranslate_ui(self):
        self.minus_button.setToolTip(tr('减少生成次数'))
        self.plus_button.setToolTip(tr('增加生成次数'))

    def _refresh_buttons(self):
        self.minus_button.setEnabled(self._value > self._minimum)
        self.plus_button.setEnabled(self._value < self._maximum)

    def value(self):
        return self._value

    def setValue(self, value):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
        value = max(self._minimum, min(self._maximum, value))
        if value == self._value:
            self._refresh_buttons()
            return
        self._value = value
        self.value_label.setText(str(value))
        self._refresh_buttons()
        self.valueChanged.emit(value)

    def setRange(self, minimum, maximum):
        self._minimum = int(minimum)
        self._maximum = max(self._minimum, int(maximum))
        self.setValue(self._value)
        self._refresh_buttons()

    def setMinimum(self, minimum):
        self.setRange(minimum, self._maximum)

    def setMaximum(self, maximum):
        self.setRange(self._minimum, maximum)

    def minimum(self):
        return self._minimum

    def maximum(self):
        return self._maximum


def _records_for_image(config_manager):
    """Return current image records with a safe builtin fallback."""
    try:
        records = catalog_snapshot(config_manager, 'image') or {}
    except Exception:  # noqa: BLE001 - catalog is optional at startup
        records = builtin_models()
    result = {}
    for model_id, record in (records or {}).items():
        if not isinstance(record, dict):
            continue
        if record.get('available', True) is False:
            continue
        if record.get('kind') == 'image' or classify_model(model_id) == 'image':
            result[str(model_id)] = dict(record)
    config = getattr(config_manager, 'config', {}) or {}
    configured = str((config.get('ecommerce') or {}).get('image_model') or '').strip()
    if configured and configured not in result:
        result[configured] = {'id': configured, 'display_name': configured,
                              'kind': 'image', 'available': True}
    return result


class _SubmitWorker(QThread):
    """Run a manager submission without blocking the form."""

    submitted = pyqtSignal(object)
    task_updated = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, manager, kwargs, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.kwargs = kwargs

    def run(self):
        try:
            manager = self.manager
            if hasattr(manager, 'prepare_tasks') and hasattr(manager, 'run_tasks'):
                manager.on_task = self.task_updated.emit
                tasks = manager.prepare_tasks(**self.kwargs)
                self.submitted.emit(tasks)
                manager.run_tasks(tasks)
            elif callable(manager):
                self.submitted.emit(manager(**self.kwargs))
            else:
                raise RuntimeError('图片生成服务暂不可用')
        except Exception as error:  # noqa: BLE001 - UI displays a short message
            self.failed.emit(str(error))


class _Card(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('imageGenerationCard')

    def refresh_colors(self):
        color = materials.ecommerce_color('image_generation_surface')
        border = materials.ecommerce_color('image_generation_border')
        self.setStyleSheet(
            'QFrame#imageGenerationCard {background:%s; border:1px solid %s; border-radius:%dpx;}'
            % (color, border, px(12)))


class _CtaCard(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('imageGenerationCta')
        self.setMinimumHeight(px(150))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), px(16), px(16))
        gradient = QLinearGradient(self.rect().topLeft(), self.rect().bottomRight())
        gradient.setColorAt(0, QColor(materials.ecommerce_color('image_generation_cta_start')))
        gradient.setColorAt(1, QColor(materials.ecommerce_color('image_generation_cta_end')))
        painter.fillPath(path, gradient)
        painter.end()
        super().paintEvent(event)


class _ReferenceDropStrip(QFrame):
    """Full-width empty state for the standalone image reference picker."""

    clicked = pyqtSignal()
    files_dropped = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('imageReferenceDropStrip')
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(px(96))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(px(16), px(12), px(16), px(12))
        layout.setSpacing(px(8))
        self.icon_label = QLabel('⬆', self)
        self.icon_label.setObjectName('imageReferenceDropIcon')
        self.icon_label.setAlignment(Qt.AlignCenter)
        self.icon_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.icon_label.setFixedWidth(px(22))
        self.text_label = QLabel(self)
        self.text_label.setObjectName('imageReferenceDropText')
        self.text_label.setAlignment(Qt.AlignCenter)
        self.text_label.setWordWrap(True)
        self.text_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout.addStretch(1)
        layout.addWidget(self.icon_label, 0, Qt.AlignVCenter)
        layout.addWidget(self.text_label, 0, Qt.AlignVCenter)
        layout.addStretch(1)
        self.retranslate_ui()
        self.refresh_colors()

    def refresh_colors(self):
        border = materials.ecommerce_color('image_generation_border')
        hint = materials.ecommerce_color('image_generation_hint')
        surface = materials.ecommerce_color('image_generation_input')
        primary = materials.ecommerce_color('image_generation_primary')
        self.setStyleSheet(
            'QFrame#imageReferenceDropStrip {background:%s; border:1px dashed %s;'
            ' border-radius:%dpx;}'
            'QLabel#imageReferenceDropIcon {background:transparent; color:%s;'
            ' font-size:%dpx;}'
            'QLabel#imageReferenceDropText {background:transparent; color:%s;'
            ' font-size:%dpx;}'
            % (surface, border, px(8), primary, px(20), hint, px(12)))

    def refresh_interface_scale(self):
        self.setFixedHeight(px(96))
        self.refresh_colors()

    def retranslate_ui(self):
        self.text_label.setText(tr(
            '⬆ 点击上传图片 (支持 JPG、PNG、WEBP 格式，最多只能上传8张图片)'))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = []
        for url in event.mimeData().urls():
            if url.isLocalFile():
                paths.append(url.toLocalFile())
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()


class _CustomPromptDialog(QDialog):
    def __init__(self, values, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr('自定义提示词'))
        self.resize(px(520), px(440))
        self.values = list(values or [])[:20]
        root = QVBoxLayout(self)
        root.setContentsMargins(px(20), px(20), px(20), px(20))
        root.setSpacing(px(10))
        self.list_widget = QListWidget(self)
        for value in self.values:
            item = QListWidgetItem(str(value))
            self.list_widget.addItem(item)
        root.addWidget(self.list_widget, 1)
        self.editor = LineEdit(self)
        self.editor.setPlaceholderText(tr('输入自定义提示词'))
        root.addWidget(self.editor)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.delete_button = PushButton(tr('删除所选'), self)
        self.save_button = PrimaryPushButton(tr('保存'), self)
        self.cancel_button = PushButton(tr('取消'), self)
        self.delete_button.clicked.connect(self._delete_selected)
        self.save_button.clicked.connect(self._save)
        self.cancel_button.clicked.connect(self.reject)
        actions.addWidget(self.delete_button)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.save_button)
        root.addLayout(actions)
        style_controls(self)
        register_retranslate(self)

    def _delete_selected(self):
        row = self.list_widget.currentRow()
        if row >= 0:
            self.list_widget.takeItem(row)

    def _save(self):
        text = self.editor.text().strip()
        if text and self.list_widget.count() < 20:
            self.list_widget.addItem(text)
            self.editor.clear()
        self.values = [self.list_widget.item(i).text() for i in range(self.list_widget.count())][:20]
        self.accept()

    def retranslate_ui(self):
        self.setWindowTitle(tr('自定义提示词'))
        self.editor.setPlaceholderText(tr('输入自定义提示词'))
        self.delete_button.setText(tr('删除所选'))
        self.save_button.setText(tr('保存'))
        self.cancel_button.setText(tr('取消'))


class ImageGenerationPage(QWidget):
    """The standalone image generation form used by the main navigation."""

    task_submitted = pyqtSignal(object)
    history_changed = pyqtSignal()
    log_message = pyqtSignal(str, str)

    def __init__(self, config_manager, parent=None):
        super().__init__(parent)
        self.setObjectName('imageGenerationPage')
        self.config_manager = config_manager
        self._worker = None
        self._workers = []
        self._lightbox = None
        self._selected_prompt = None
        self.reference_paths = []
        self._records = _records_for_image(config_manager)
        self._manager = None
        self.manager_factory = None
        self._build_ui()
        self._load_config_defaults()
        self._refresh_models()
        self._update_prompt_state()
        style_page(self)
        style_controls(self)
        register_retranslate(self)

    @property
    def busy(self):
        """Whether a submission worker is still executing."""
        return self.has_running_workers()

    # —— build ——
    def _build_ui(self):
        # The form contains long prompt examples whose size hints are wider than
        # the narrow layout breakpoint.  Keep the page/columns shrinkable so Qt
        # can actually enter the stacked layout below 1180px.
        self.setMinimumWidth(0)
        root = scrollable_body(self)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body_content.setMinimumWidth(0)
        self.body_content.installEventFilter(self)
        root.setContentsMargins(px(26), px(16), px(26), px(20))
        root.setSpacing(px(16))
        header = QHBoxLayout()
        title_box = QVBoxLayout(); title_box.setSpacing(px(4))
        self.title_label = TitleLabel(tr('图片生成'), self)
        self.caption_label = QLabel(tr('使用 AI 生成高质量图片内容'), self)
        self.caption_label.setObjectName('imageGenerationCaption')
        title_box.addWidget(self.title_label); title_box.addWidget(self.caption_label)
        header.addLayout(title_box); header.addStretch(1)
        root.addLayout(header)

        body = QBoxLayout(QBoxLayout.LeftToRight); body.setSpacing(px(20))
        self.body_layout = body
        self.left_column = QWidget(self)
        self.left_column.setMinimumWidth(0)
        self.left_column.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        left = QVBoxLayout(self.left_column); left.setContentsMargins(0, 0, 0, 0); left.setSpacing(px(16))
        self.form_card = _Card(self.left_column)
        form = QVBoxLayout(self.form_card); form.setContentsMargins(px(24), px(24), px(24), px(24)); form.setSpacing(px(28))
        form.addWidget(self._build_prompt_section())
        form.addWidget(self._build_reference_section())
        form.addWidget(self._build_model_section())
        left.addWidget(self.form_card, 1)
        submit_row = QHBoxLayout(); submit_row.addStretch(1)
        self.submit_button = PrimaryPushButton(FIF.UP, tr('开始生成'), self.left_column)
        self.submit_button.setObjectName('imageGenerationSubmit')
        self.submit_button.setMinimumWidth(px(140)); self.submit_button.setFixedHeight(px(44))
        self.submit_button.clicked.connect(self.submit_generation)
        submit_row.addWidget(self.submit_button); left.addLayout(submit_row)
        body.addWidget(self.left_column, 1)

        self.right_column = QWidget(self)
        self.right_column.setObjectName('imageGenerationRightColumn')
        self.right_column.setMinimumWidth(0)
        self.right_column.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.right_column.setMaximumWidth(px(480))
        right = QVBoxLayout(self.right_column); right.setContentsMargins(0, 0, 0, 0); right.setSpacing(px(16))
        self.cta_card = _CtaCard(self.right_column)
        cta = QVBoxLayout(self.cta_card); cta.setContentsMargins(px(24), px(20), px(24), px(18)); cta.setSpacing(px(8))
        self.cta_button = PrimaryPushButton(FIF.UP, tr('开始生成'), self.cta_card)
        self.cta_button.setObjectName('imageGenerationCtaButton'); self.cta_button.setFixedHeight(px(44))
        self.cta_button.clicked.connect(self.submit_generation)
        self.cta_hint = QLabel(tr('填写提示词并选择模型后即可生成'), self.cta_card)
        self.cta_hint.setAlignment(Qt.AlignCenter); self.cta_hint.setStyleSheet('background:transparent;')
        cta.addWidget(self.cta_button); cta.addWidget(self.cta_hint)
        right.addWidget(self.cta_card)
        right.addWidget(self._build_quick_prompt_card(), 1)
        body.addWidget(self.right_column)
        root.addLayout(body, 1)
        self._fit_columns()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_columns()

    def eventFilter(self, watched, event):
        if watched is getattr(self, 'body_content', None) and event.type() == QEvent.Resize:
            self._fit_columns()
        return super().eventFilter(watched, event)

    def _fit_columns(self):
        available_width = self.body_content.width() if getattr(self, 'body_content', None) else 0
        if available_width <= 0:
            available_width = self.width()
        stacked = available_width < px(1180)
        direction = QBoxLayout.TopToBottom if stacked else QBoxLayout.LeftToRight
        if self.body_layout.direction() != direction:
            self.body_layout.setDirection(direction)
        if stacked:
            self.body_layout.setSpacing(px(16))
            self.right_column.setMaximumWidth(16777215)
            self.right_column.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        else:
            self.body_layout.setSpacing(px(20))
            self.right_column.setMaximumWidth(px(480))
            self.right_column.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.body_layout.invalidate()
        self.updateGeometry()

    def _label_row(self, title, count_label=None, clear=None):
        row = QHBoxLayout(); row.setSpacing(px(8))
        label = StrongBodyLabel(title, self)
        row.addWidget(label); row.addStretch(1)
        if count_label is not None: row.addWidget(count_label)
        if clear is not None: row.addWidget(clear)
        return row

    def _build_prompt_section(self):
        box = QWidget(self.form_card); layout = QVBoxLayout(box); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(px(10))
        self.prompt_count_label = QLabel('0 / 10000', box)
        self.clear_prompt_button = PushButton(tr('清除'), box); self.clear_prompt_button.setVisible(False)
        self.clear_prompt_button.clicked.connect(lambda: self.prompt_edit.clear())
        layout.addLayout(self._label_row(tr('✏ 提示词 *'), self.prompt_count_label, self.clear_prompt_button))
        self.prompt_edit = QPlainTextEdit(box); self.prompt_edit.setObjectName('imagePromptEdit')
        self.prompt_input = self.prompt_edit
        self.prompt_edit.setPlaceholderText(tr('请描述你想生成的图片内容...')); self.prompt_edit.setFixedHeight(px(140))
        self._refresh_prompt_style()
        self.prompt_edit.textChanged.connect(self._update_prompt_state)
        layout.addWidget(self.prompt_edit)
        return box

    def _build_reference_section(self):
        box = QWidget(self.form_card); layout = QVBoxLayout(box); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(px(10))
        title = QLabel(tr('🖼 参考图'), box); title.setStyleSheet('font-weight:600;')
        note = QLabel(tr('（图片生成需要上传参考图，文生图不需要）'), box)
        note.setStyleSheet('color:%s;' % materials.ecommerce_color('image_generation_hint'))
        heading = QHBoxLayout(); heading.addWidget(title); heading.addWidget(note); heading.addStretch(1)
        self.used_images_button = PushButton(tr('已用图片'), box)
        self.gallery_button = PushButton(tr('画廊选图'), box)
        self.clear_images_button = PushButton(tr('清空'), box); self.clear_images_button.setVisible(False)
        self.used_images_button.clicked.connect(self._choose_used_images)
        self.gallery_button.clicked.connect(self._choose_gallery_images)
        self.clear_images_button.clicked.connect(self.clear_references)
        heading.addWidget(self.used_images_button); heading.addWidget(self.gallery_button); heading.addWidget(self.clear_images_button)
        layout.addLayout(heading)
        self.reference_drop_strip = _ReferenceDropStrip(box)
        self.reference_drop_strip.clicked.connect(self.choose_reference_files)
        self.reference_drop_strip.files_dropped.connect(self._accept_dropped_references)
        layout.addWidget(self.reference_drop_strip)
        self.reference_grid = ImageUploadGrid(
            'product', max_count=8, parent=box,
            add_label_provider=self._reference_add_label)
        self.image_grid = self.reference_grid
        self.reference_grid.setObjectName('imageReferenceGrid')
        self.reference_grid.preview_requested.connect(self._preview_reference)
        self.reference_grid.add_requested.connect(self.choose_reference_files)
        self.reference_grid.changed.connect(self._references_changed)
        self.reference_grid.set_paths([], emit=False)
        self.reference_grid.setVisible(False)
        self._sync_reference_add_label()
        layout.addWidget(self.reference_grid)
        return box

    def _build_model_section(self):
        box = QFrame(self.form_card)
        box.setObjectName('imageModelSubcard')
        layout = QVBoxLayout(box)
        layout.setContentsMargins(px(16), px(14), px(16), px(16))
        layout.setSpacing(px(14))
        self.model_section_title = QLabel(tr('🐱 模型'), box)
        self.model_section_title.setObjectName('imageModelSectionTitle')
        layout.addWidget(self.model_section_title)
        self._refresh_model_subcard_style()

        row1 = QGridLayout(); row1.setHorizontalSpacing(px(12)); row1.setVerticalSpacing(px(8))
        self.model_label = QLabel(tr('模型 *'), box); self.version_label = QLabel(tr('模型版本 *'), box)
        self.model_combo = ComboBox(box); self.model_combo.setObjectName('imageModelCombo')
        self.model_selector = self.model_combo
        self.version_combo = ComboBox(box); self.version_combo.setObjectName('imageModelVersionCombo')
        self.model_combo.currentIndexChanged.connect(self._model_changed)
        row1.addWidget(self.model_label, 0, 0); row1.addWidget(self.version_label, 0, 1)
        row1.addWidget(self.model_combo, 1, 0); row1.addWidget(self.version_combo, 1, 1)
        row1.setColumnStretch(0, 1); row1.setColumnStretch(1, 1); layout.addLayout(row1)
        row2 = QGridLayout(); row2.setHorizontalSpacing(px(12));
        self.ratio_label = QLabel(tr('宽高比'), box); self.tier_label = QLabel(tr('分辨率'), box); self.count_label = QLabel(tr('生成次数'), box)
        self.ratio_combo = ComboBox(box); self.ratio_combo.addItems(list(RATIOS)); self.ratio_combo.setCurrentText('9:16')
        self.aspect_ratio_combo = self.ratio_combo
        self.tier_combo = ComboBox(box); self.tier_combo.addItems(list(TIERS)); self.tier_combo.setCurrentText('1K')
        self.resolution_combo = self.tier_combo
        self.count_spin = _CountStepper(box)
        self.count_spin.valueChanged.connect(self._update_count_hint)
        row2.addWidget(self.ratio_label, 0, 0); row2.addWidget(self.tier_label, 0, 1); row2.addWidget(self.count_label, 0, 2)
        row2.addWidget(self.ratio_combo, 1, 0); row2.addWidget(self.tier_combo, 1, 1); row2.addWidget(self.count_spin, 1, 2)
        row2.setColumnStretch(0, 1); row2.setColumnStretch(1, 1); row2.setColumnStretch(2, 1); layout.addLayout(row2)
        self.count_hint = QLabel('', box); self.count_hint.setStyleSheet('color:%s;' % materials.ecommerce_color('image_generation_primary'))
        layout.addWidget(self.count_hint)
        return box

    def _refresh_model_subcard_style(self):
        if not hasattr(self, 'model_section_title'):
            return
        self.model_section_title.setStyleSheet(
            'font-size:%dpx; font-weight:600; background:transparent;' % px(13))
        card = self.model_section_title.parentWidget()
        if card is not None:
            card.setStyleSheet(
                'QFrame#imageModelSubcard {background:%s; border:1px solid %s; border-radius:%dpx;}'
                % (materials.ecommerce_color('image_generation_surface'),
                   materials.ecommerce_color('image_generation_border'), px(10)))

    def _build_quick_prompt_card(self):
        card = _Card(self.right_column)
        layout = QVBoxLayout(card); layout.setContentsMargins(px(20), px(20), px(20), px(20)); layout.setSpacing(px(10))
        heading = QHBoxLayout()
        self.quick_title = StrongBodyLabel(tr('💡 快速提示词'), card)
        self.custom_prompt_button = PushButton(tr('+ 自定义提示词'), card); self.custom_prompt_button.clicked.connect(self.open_custom_prompts)
        heading.addWidget(self.quick_title); heading.addStretch(1); heading.addWidget(self.custom_prompt_button); layout.addLayout(heading)
        self.quick_caption = QLabel(tr('点击快速应用提示词'), card); self.quick_caption.setStyleSheet('color:%s;' % materials.ecommerce_color('image_generation_hint')); layout.addWidget(self.quick_caption)
        self.quick_list = QWidget(card); self.quick_layout = QVBoxLayout(self.quick_list); self.quick_layout.setContentsMargins(0, 0, 0, 0); self.quick_layout.setSpacing(px(10)); layout.addWidget(self.quick_list, 1)
        self.quick_prompt_buttons = []
        self._refresh_quick_prompts()
        return card

    # —— references ——
    def choose_reference_files(self):
        remaining = max(0, 8 - len(self.reference_paths))
        if not remaining:
            self._warning(tr('最多只能上传8张图片'))
            return
        paths, _ = QFileDialog.getOpenFileNames(self, tr('选择参考图'), '', tr('图片 (*.png *.jpg *.jpeg *.webp)'))
        if paths:
            self.set_references(self.reference_paths + paths[:remaining])
            if len(paths) > remaining:
                self._warning(tr('最多只能上传8张图片'))

    def _accept_dropped_references(self, paths):
        remaining = max(0, 8 - len(self.reference_paths))
        if not remaining:
            self._warning(tr('最多只能上传8张图片'))
            return
        paths = [str(path) for path in (paths or []) if str(path).strip()]
        if paths:
            self.set_references(self.reference_paths + paths[:remaining])
            if len(paths) > remaining:
                self._warning(tr('最多只能上传8张图片'))

    def _choose_used_images(self):
        store = getattr(self.config_manager, 'ecommerce_store', None)
        if store is None:
            try:
                from core.ecommerce.persistence import EcommerceImageStore
                store = EcommerceImageStore(getattr(self.config_manager, 'path', None))
            except Exception:
                return
        dialog = UsedImagePickerDialog(self, store, max_count=max(1, 8 - len(self.reference_paths)))
        if dialog.exec_() == QDialog.Accepted:
            self.set_references(self.reference_paths + dialog.result_paths)

    def _choose_gallery_images(self):
        dialog = GalleryPickerDialog(self, self.config_manager, max_count=max(1, 8 - len(self.reference_paths)))
        if dialog.exec_() == QDialog.Accepted:
            self.set_references(self.reference_paths + dialog.result_paths)

    def set_references(self, paths):
        self.reference_paths = [str(path) for path in (paths or []) if str(path).strip()][:8]
        self.reference_grid.set_paths(self.reference_paths, emit=False)
        self._references_changed(self.reference_paths)

    def clear_references(self):
        self.set_references([])

    def _references_changed(self, paths):
        self.reference_paths = list(paths or [])[:8]
        self.clear_images_button.setVisible(bool(self.reference_paths))
        self.reference_drop_strip.setVisible(not bool(self.reference_paths))
        self.reference_grid.setVisible(bool(self.reference_paths))
        self._sync_reference_add_label()

    def _reference_add_label(self, count, _max_count=8):
        return tr('＋ 继续添加 ({n}/8)').format(n=int(count))

    def _sync_reference_add_label(self):
        tile = self.reference_grid.add_tile() if hasattr(self, 'reference_grid') else None
        if tile is None:
            return
        if self.reference_paths:
            label = self._reference_add_label(len(self.reference_paths), 8)
        else:
            label = tr('⬆ 点击上传图片 (支持 JPG、PNG、WEBP 格式，最多只能上传8张图片)')
        tile.set_label(label)

    def _preview_reference(self, index):
        if not self.reference_paths:
            return
        self._lightbox = ImageLightbox.show_images(
            self, self.reference_paths,
            max(0, min(int(index), len(self.reference_paths) - 1)))

    # —— models and state ——
    def _refresh_models(self, selected=''):
        self._records = _records_for_image(self.config_manager)
        current = selected or self.model_combo.currentData() or ''
        blocker = QSignalBlocker(self.model_combo); self.model_combo.clear()
        for model_id, record in self._records.items():
            display = str(record.get('display_name') or record.get('name') or model_id)
            self.model_combo.addItem(display, userData=model_id)
        del blocker
        index = self.model_combo.findData(normalize_model_id(current, self._records.keys()))
        if index < 0 and self.model_combo.count(): index = 0
        self.model_combo.setCurrentIndex(index)
        self._model_changed(index)

    def _model_changed(self, index):
        model_id = self.model_combo.itemData(index) if index >= 0 else ''
        record = self._records.get(str(model_id), {})
        versions = record.get('versions') or record.get('model_versions') or record.get('version') or []
        if isinstance(versions, str): versions = [versions]
        if not versions: versions = [str(model_id)] if model_id else []
        self.version_combo.clear(); self.version_combo.addItems([str(item) for item in versions])
        self._update_prompt_state()

    def _load_config_defaults(self):
        defaults = (getattr(self.config_manager, 'config', {}) or {}).get('image_generation') or {}
        self.ratio_combo.setCurrentText(str(defaults.get('ratio') or '9:16'))
        self.tier_combo.setCurrentText(str(defaults.get('tier') or '1K'))
        try: self.count_spin.setValue(max(1, min(10, int(defaults.get('count', 1)))))
        except (TypeError, ValueError): pass

    def _update_prompt_state(self):
        value = self.prompt_edit.toPlainText()
        if len(value) > 10000:
            cursor = self.prompt_edit.textCursor(); pos = cursor.position()
            blocker = QSignalBlocker(self.prompt_edit); self.prompt_edit.setPlainText(value[:10000]); cursor.setPosition(min(pos, 10000)); self.prompt_edit.setTextCursor(cursor); del blocker
            value = value[:10000]
        self.prompt_count_label.setText(tr('{n} / 10000').format(n=len(value)))
        self.clear_prompt_button.setVisible(bool(value))
        too_long = len(value) > 10000
        self.prompt_count_label.setStyleSheet('color:%s;' % materials.ecommerce_color('image_generation_error') if too_long else '')
        self.submit_button.setEnabled(bool(value.strip()) and bool(self.model_combo.currentData()) and not too_long)
        self.cta_button.setEnabled(self.submit_button.isEnabled())

    def _update_count_hint(self, value):
        self.count_hint.setText(tr('将循环创建 {n} 个相同任务').format(n=value) if int(value) > 1 else '')

    def _refresh_prompt_style(self):
        bg = QColor(str(materials.palette()['bg2'])).name()
        border = materials.ecommerce_color('image_generation_border')
        self.prompt_edit.setStyleSheet(
            'QPlainTextEdit#imagePromptEdit {background:%s; border:1px solid %s; border-radius:%dpx;}'
            % (bg, border, px(8)))

    # —— submission ——
    def _warning(self, text):
        try: InfoBar.warning(tr('提示'), text, parent=self)
        except Exception: pass

    def _success(self, text):
        try: InfoBar.success(tr('已提交'), text, parent=self)
        except Exception: pass

    def _manager_instance(self):
        if self._manager is not None: return self._manager
        if callable(self.manager_factory):
            self._manager = self.manager_factory()
            return self._manager
        try:
            from core.manual_image import ManualImageManager, get_manual_image_store
            store = get_manual_image_store(self.config_manager, log=getattr(self.config_manager, 'log_callback', None))
            self._manager = ManualImageManager(self.config_manager, store)
        except Exception:
            self._manager = None
        return self._manager

    def submit_generation(self):
        prompt = self.prompt_edit.toPlainText().strip()
        model = str(self.model_combo.currentData() or '').strip()
        if not prompt:
            self._warning(tr('请输入提示词')); return False
        if len(prompt) > 10000:
            self._warning(tr('提示词不能超过10000字')); return False
        if not model:
            self._warning(tr('请选择模型')); return False
        count = int(self.count_spin.value())
        kwargs = dict(prompt=prompt, model=model,
                      model_version=str(self.version_combo.currentText() or model),
                      ratio=self.ratio_combo.currentText(), tier=self.tier_combo.currentText(),
                      reference_paths=list(self.reference_paths), count=count)
        manager = self._manager_instance()
        if manager is None:
            self._warning(tr('图片生成服务暂不可用')); return False
        self._worker = _SubmitWorker(manager, kwargs, self)
        self._worker.submitted.connect(self._on_submitted); self._worker.failed.connect(self._on_submit_failed)
        self._worker.task_updated.connect(lambda _record: self.history_changed.emit())
        self._workers.append(self._worker)
        self._worker.finished.connect(lambda worker=self._worker: self._worker_finished(worker))
        self._worker.start()
        return True

    def _worker_finished(self, worker):
        try:
            self._workers.remove(worker)
        except ValueError:
            pass
        worker.deleteLater()

    def has_running_workers(self):
        """Return whether an image-generation worker is still running."""
        for worker in tuple(self._workers):
            try:
                if worker.isRunning():
                    return True
            except RuntimeError:
                # A finished orphan may already have been deleted by Qt.
                continue
        return False

    def shutdown_workers(self, wait_ms=1000):
        """Cancel workers and detach any request that outlives the page."""
        global _SHUTDOWN_HOOK_CONNECTED
        workers = tuple(self._workers)
        for worker in workers:
            manager = getattr(worker, 'manager', None)
            if manager is not None and hasattr(manager, 'cancel'):
                manager.cancel()
            if worker.isRunning():
                worker.wait(wait_ms)
            if worker.isRunning():
                # Do not destroy a QWidget-owned QThread while a bounded HTTP
                # request is still unwinding; keep it alive until it finishes.
                try:
                    worker.submitted.disconnect()
                    worker.task_updated.disconnect()
                    worker.failed.disconnect()
                    worker.finished.disconnect()
                except (TypeError, RuntimeError):
                    pass
                try:
                    worker.setParent(None)
                except RuntimeError:
                    pass
                with _ORPHAN_WORKERS_LOCK:
                    _ORPHAN_WORKERS.add(worker)
                worker.finished.connect(lambda worker=worker: self._orphan_worker_finished(worker))
            else:
                try:
                    self._workers.remove(worker)
                except ValueError:
                    pass
                worker.deleteLater()
        if _ORPHAN_WORKERS and not _SHUTDOWN_HOOK_CONNECTED:
            app = QCoreApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(_wait_for_orphan_workers)
                _SHUTDOWN_HOOK_CONNECTED = True

        return self.has_running_workers()

    def _orphan_worker_finished(self, worker):
        try:
            self._workers.remove(worker)
        except ValueError:
            pass
        _dispose_orphan_worker(worker)

    def closeEvent(self, event):
        self.shutdown_workers()
        super().closeEvent(event)

    def _on_submitted(self, result):
        self.task_submitted.emit(result)
        self.history_changed.emit()
        self._success(tr('已提交 {n} 个图片生成任务，可在任务历史查看').format(n=len(result) if isinstance(result, (list, tuple)) else self.count_spin.value()))

    def _on_submit_failed(self, error):
        self._warning(tr('提交失败：{message}').format(message=error or tr('请稍后重试')))

    # —— quick prompts/config ——
    def _custom_prompts(self):
        config = getattr(self.config_manager, 'config', {}) or {}
        values = (config.get('image_generation') or {}).get('custom_prompts', [])
        return [str(value) for value in values if str(value).strip()][:20]

    def _refresh_quick_prompts(self):
        while self.quick_layout.count():
            item = self.quick_layout.takeAt(0); widget = item.widget()
            if widget is not None: widget.deleteLater()
        self.quick_prompt_buttons = []
        prompts = [(tr(value), True) for value in DEFAULT_QUICK_PROMPTS]
        prompts.extend((value, False) for value in self._custom_prompts())
        for index, (value, _is_default) in enumerate(prompts, 1):
            button = _QuickPromptCard(index, value, self.quick_list)
            button.clicked.connect(lambda text=value, btn=button: self.apply_quick_prompt(text, btn))
            self.quick_layout.addWidget(button); self.quick_prompt_buttons.append(button)
        self.quick_layout.addStretch(1)

    def apply_quick_prompt(self, text, button=None):
        self.prompt_edit.setPlainText(str(text)); self._selected_prompt = str(text)
        for item in self.quick_prompt_buttons:
            item.setProperty('selectedPrompt', item is button)
            item.style().unpolish(item); item.style().polish(item); item.update()

    def open_custom_prompts(self):
        dialog = _CustomPromptDialog(self._custom_prompts(), self)
        if dialog.exec_() != QDialog.Accepted: return
        try: self.config_manager.update(('image_generation', 'custom_prompts'), dialog.values)
        except Exception:
            config = getattr(self.config_manager, 'config', {})
            config.setdefault('image_generation', {})['custom_prompts'] = dialog.values
        self._refresh_quick_prompts()

    # —— host integration ——
    def refresh_interface_scale(self):
        self._refresh_quick_prompts(); self.reference_drop_strip.refresh_interface_scale(); self.reference_grid.refresh_interface_scale(); self.count_spin.refresh_interface_scale(); self._refresh_model_subcard_style(); self._sync_reference_add_label()
        self._fit_columns()
        self.body_layout.invalidate()
        self.body_layout.activate()
        content_layout = self.body_content.layout()
        if content_layout is not None:
            content_layout.invalidate()
            content_layout.activate()
        self.body_content.updateGeometry()

    def refresh_colors(self):
        self.form_card.refresh_colors()
        for card in (self.cta_card, self.right_column.findChild(QFrame, 'imageGenerationCard')):
            if card is not None and hasattr(card, 'refresh_colors'): card.refresh_colors()
        self._refresh_prompt_style(); self.count_spin.refresh_colors(); self._refresh_model_subcard_style(); self._update_prompt_state()

    def retranslate_ui(self):
        self.title_label.setText(tr('图片生成')); self.caption_label.setText(tr('使用 AI 生成高质量图片内容'))
        self.prompt_edit.setPlaceholderText(tr('请描述你想生成的图片内容...'))
        self.clear_prompt_button.setText(tr('清除')); self.used_images_button.setText(tr('已用图片')); self.gallery_button.setText(tr('画廊选图')); self.clear_images_button.setText(tr('清空'))
        self.model_section_title.setText(tr('🐱 模型')); self.model_label.setText(tr('模型 *')); self.version_label.setText(tr('模型版本 *')); self.ratio_label.setText(tr('宽高比')); self.tier_label.setText(tr('分辨率')); self.count_label.setText(tr('生成次数'))
        self.submit_button.setText(tr('开始生成')); self.cta_button.setText(tr('开始生成')); self.cta_hint.setText(tr('填写提示词并选择模型后即可生成'))
        self.quick_title.setText(tr('💡 快速提示词')); self.quick_caption.setText(tr('点击快速应用提示词')); self.custom_prompt_button.setText(tr('+ 自定义提示词'))
        self.reference_drop_strip.retranslate_ui(); self.reference_drop_strip.refresh_colors(); self.count_spin.retranslate_ui()
        self._refresh_quick_prompts(); self._update_count_hint(self.count_spin.value()); self._update_prompt_state()
