"""Standalone video creation page.

The page is deliberately independent from the existing video automation page.
It owns the form, preview state and task history presentation; the protocol and
task persistence are provided by :mod:`core.manual_video`.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QCoreApplication, QEvent, QRect, QSize, Qt, QSignalBlocker, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QDesktopServices, QFont, QIcon, QImage, QPainter, QPixmap
from PyQt5.QtWidgets import (
    QBoxLayout, QCheckBox, QDialog, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPlainTextEdit,
    QProgressBar, QScrollArea, QSizePolicy, QSpinBox, QStackedWidget,
    QToolButton, QVBoxLayout, QWidget, QApplication,
)
from PyQt5.QtCore import QUrl
try:
    from PyQt5.QtMultimedia import (QAbstractVideoBuffer, QAbstractVideoSurface, QMediaContent,
                                    QMediaPlayer, QVideoFrame, QVideoSurfaceFormat)
except ImportError:  # pragma: no cover - optional multimedia backend
    QMediaContent = QMediaPlayer = QAbstractVideoSurface = None
from qfluentwidgets import ComboBox, InfoBar, LineEdit, PrimaryPushButton, PushButton, StrongBodyLabel, TitleLabel, ToolButton, setCustomStyleSheet, FluentIcon as FIF

from core.i18n import register_retranslate, tr
from core.line_routing import resolve_line
from core.model_catalog import builtin_models, classify_model, normalize_model_id, family_for
from .. import materials
from ..components.image_lightbox import ImageLightbox
from ..components.model_selector import catalog_snapshot
from ..responsive import scrollable_body
from ..theme import style_controls, style_page
from ..tokens import px


_ORPHAN_WORKERS = set()
_ORPHAN_WORKERS_LOCK = threading.Lock()
_SHUTDOWN_HOOK_CONNECTED = False


def _color(key):
    try:
        return materials.ecommerce_color(key)
    except (KeyError, AttributeError):
        return materials.text_color(1)


if QAbstractVideoSurface is not None:
    class _VideoFrameSurface(QAbstractVideoSurface):
        frame_ready = pyqtSignal(QImage)

        def supportedPixelFormats(self, handleType=QAbstractVideoBuffer.NoHandle):
            if handleType != QAbstractVideoBuffer.NoHandle:
                return []
            return [QVideoFrame.Format_RGB32, QVideoFrame.Format_ARGB32,
                    QVideoFrame.Format_RGB24, QVideoFrame.Format_RGB565, QVideoFrame.Format_RGB555]

        def present(self, frame):
            mapped = QVideoFrame(frame)
            if not mapped.map(QAbstractVideoBuffer.ReadOnly):
                return False
            try:
                image_format = QVideoFrame.imageFormatFromPixelFormat(mapped.pixelFormat())
                if image_format == QImage.Format_Invalid:
                    return False
                bits = mapped.bits()
                bits.setsize(mapped.mappedBytes())
                image = QImage(bits, mapped.width(), mapped.height(), mapped.bytesPerLine(), image_format)
                # Some Windows decoders leave RGB32's unused alpha byte at zero.
                # Normalize it before painting into the translucent main window.
                image = (image.convertToFormat(QImage.Format_RGB888)
                         if image_format == QImage.Format_RGB32 else image.copy())
                if self.surfaceFormat().scanLineDirection() == QVideoSurfaceFormat.BottomToTop:
                    image = image.mirrored(False, True)
            finally:
                mapped.unmap()
            self.frame_ready.emit(image)
            return True


class _VideoPreviewWidget(QWidget):
    """Paint decoded Qt frames inside the translucent window without a native overlay."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._image = QImage()
        self._aspect_mode = Qt.KeepAspectRatio
        self.surface = _VideoFrameSurface(self)
        self.surface.frame_ready.connect(self._set_frame, Qt.QueuedConnection)

    def _set_frame(self, image):
        self._image = image
        self.update()

    def aspectRatioMode(self):
        return self._aspect_mode

    def setAspectRatioMode(self, mode):
        self._aspect_mode = mode
        self.update()

    def frameRect(self):
        size = self._image.size().scaled(self.size(), self._aspect_mode)
        return QRect((self.width() - size.width()) // 2, (self.height() - size.height()) // 2,
                     size.width(), size.height())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(_color('video_creation_preview_bg')))
        if not self._image.isNull():
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            painter.drawImage(self.frameRect(), self._image)

    def showFullScreen(self):
        self._embedded_flags = self.windowFlags()
        self.setWindowFlag(Qt.Window, True)
        super().showFullScreen()

    def showNormal(self):
        self.setWindowFlags(getattr(self, '_embedded_flags', Qt.Widget))
        super().showNormal()
        if self.parentWidget() is not None and self.parentWidget().layout() is not None:
            self.parentWidget().layout().invalidate()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
            event.accept()
            return
        super().keyPressEvent(event)


def _dispose_worker(worker):
    with _ORPHAN_WORKERS_LOCK:
        _ORPHAN_WORKERS.discard(worker)
    try:
        worker.deleteLater()
    except RuntimeError:
        pass


def _wait_orphan_workers():
    with _ORPHAN_WORKERS_LOCK:
        workers = tuple(_ORPHAN_WORKERS)
    for worker in workers:
        if worker.isRunning():
            worker.wait()
        _dispose_worker(worker)


class _CountStepper(QWidget):
    valueChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('videoGenerationCountStepper')
        self._minimum, self._maximum, self._value = 1, 10, 1
        self.setFixedHeight(px(34))
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        layout = QHBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
        self.minus_button = QToolButton(self); self.minus_button.setObjectName('videoCountMinus')
        self.minus_button.setText('\N{MINUS SIGN}'); self.minus_button.setFixedSize(px(28), px(28))
        self.minus_button.clicked.connect(lambda: self.setValue(self._value - 1))
        self.value_label = QLabel('1', self); self.value_label.setObjectName('videoCountValue')
        self.value_label.setAlignment(Qt.AlignCenter); self.value_label.setFixedSize(px(72), px(34))
        self.plus_button = QToolButton(self); self.plus_button.setObjectName('videoCountPlus')
        self.plus_button.setText('+'); self.plus_button.setFixedSize(px(28), px(28))
        self.plus_button.clicked.connect(lambda: self.setValue(self._value + 1))
        layout.addWidget(self.minus_button, 0, Qt.AlignVCenter); layout.addWidget(self.value_label, 0, Qt.AlignVCenter); layout.addWidget(self.plus_button, 0, Qt.AlignVCenter)
        self._refresh()

    def _refresh(self):
        self.minus_button.setEnabled(self._value > self._minimum)
        self.plus_button.setEnabled(self._value < self._maximum)

    def value(self): return self._value

    def setValue(self, value):
        try: value = int(value)
        except (TypeError, ValueError): return
        value = max(self._minimum, min(self._maximum, value))
        if value == self._value:
            self._refresh(); return
        self._value = value; self.value_label.setText(str(value)); self._refresh(); self.valueChanged.emit(value)

    def setRange(self, minimum, maximum):
        self._minimum, self._maximum = int(minimum), max(int(minimum), int(maximum)); self.setValue(self._value)

    def minimum(self): return self._minimum
    def maximum(self): return self._maximum

    def refresh_interface_scale(self):
        self.setFixedHeight(px(34)); self.minus_button.setFixedSize(px(28), px(28)); self.plus_button.setFixedSize(px(28), px(28)); self.value_label.setFixedSize(px(72), px(34))


class _VideoCard(QFrame):
    def __init__(self, object_name='videoCreationCard', parent=None):
        super().__init__(parent); self.setObjectName(object_name); self.refresh_colors()

    def refresh_colors(self):
        self.setStyleSheet(
            'QFrame#%s {background:%s; border:1px solid %s; border-radius:%dpx;}'
            % (self.objectName(), _color('video_creation_surface'), _color('video_creation_border'), px(16)))


class _ReferenceImageDropZone(QFrame):
    files_dropped = pyqtSignal(object)
    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('videoReferenceImageDropZone')
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(px(8), px(6), px(8), px(6))
        self.label = QLabel(tr('点击或拖入参考图片'), self)
        self.label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setWordWrap(True)
        layout.addWidget(self.label)

    @staticmethod
    def _paths(event):
        allowed = {'.jpg', '.jpeg', '.png', '.webp'}
        return [url.toLocalFile() for url in event.mimeData().urls()
                if url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in allowed]

    def dragEnterEvent(self, event):
        paths = self._paths(event)
        if paths:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = self._paths(event)
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

class _HistoryRow(QFrame):
    clicked = pyqtSignal(object)
    delete_requested = pyqtSignal(object)
    folder_requested = pyqtSignal(object)
    regenerate_requested = pyqtSignal(object)

    def __init__(self, record, parent=None):
        super().__init__(parent); self.record = dict(record or {}); self.setObjectName('videoHistoryRow')
        self.setCursor(Qt.PointingHandCursor); self.setMinimumHeight(px(64))
        layout = QHBoxLayout(self); layout.setContentsMargins(px(8), px(6), px(8), px(6)); layout.setSpacing(px(8))
        self.check = QCheckBox(self); self.check.setObjectName('videoHistoryCheck'); self.check.setFixedWidth(px(22)); self.check.setEnabled(bool(self.record.get('file_path') or self.record.get('video_url'))); layout.addWidget(self.check)
        thumb = QLabel(self); thumb.setObjectName('videoHistoryThumb'); thumb.setFixedSize(px(64), px(40)); thumb.setAlignment(Qt.AlignCenter); thumb.setText(tr('视频')); layout.addWidget(thumb)
        text_box = QVBoxLayout(); text_box.setSpacing(px(2));
        prompt = QLabel(str(self.record.get('prompt_name') or self.record.get('prompt') or tr('未命名视频')), self); prompt.setObjectName('videoHistoryPrompt'); prompt.setWordWrap(True); prompt.setMaximumHeight(px(34)); text_box.addWidget(prompt)
        created = self.record.get('created_at')
        try:
            stamp = datetime.fromtimestamp(float(created)).strftime('%Y-%m-%d %H:%M') if created else ''
        except (TypeError, ValueError, OSError):
            stamp = str(created or '').replace('T', ' ')[:16]
        meta = QLabel(' · '.join(filter(None, [stamp, f"{self.record.get('duration', '')}s" if self.record.get('duration') else '', str(self.record.get('output_size') or ''), str(self.record.get('model') or '')])), self); meta.setObjectName('videoHistoryMeta'); text_box.addWidget(meta)
        layout.addLayout(text_box, 1)
        status_text = {'pending': tr('等待中'), 'running': tr('生成中'), 'processing': tr('生成中'), 'downloading': tr('下载中'), 'succeeded': tr('已完成'), 'completed': tr('已完成'), 'failed': tr('失败'), 'cancelled': tr('已取消')}
        self.status = QLabel(status_text.get(str(self.record.get('status') or '').lower(), tr('等待中')), self); self.status.setObjectName('videoHistoryStatus'); layout.addWidget(self.status)
        actions = (
            (FIF.PLAY, self.regenerate_requested, 'videoHistoryRegenerate', tr('重新生成')),
            (FIF.FOLDER, self.folder_requested, 'videoHistoryFolder', tr('打开文件夹')),
            (FIF.DELETE, self.delete_requested, 'videoHistoryDelete', tr('删除记录')),
        )
        for icon, signal, name, tooltip in actions:
            button = ToolButton(icon, self); button.setObjectName(name); button.setIconSize(QSize(px(16), px(16))); button.setFixedSize(px(30), px(30)); button.setToolTip(tooltip); button.clicked.connect(lambda _=False, s=signal: s.emit(self.record)); layout.addWidget(button)
        self._refresh_style()

    def retranslate_ui(self):
        statuses = {'pending': '等待中', 'running': '生成中', 'processing': '生成中',
                    'downloading': '下载中', 'succeeded': '已完成', 'completed': '已完成',
                    'failed': '失败', 'cancelled': '已取消'}
        self.status.setText(tr(statuses.get(str(self.record.get('status') or '').lower(), '等待中')))
        for name, text in [('videoHistoryRegenerate', '重新生成'),
                           ('videoHistoryFolder', '打开文件夹'), ('videoHistoryDelete', '删除记录')]:
            self.findChild(ToolButton, name).setToolTip(tr(text))

    def _refresh_style(self):
        self.setStyleSheet('QFrame#videoHistoryRow {background:%s; border:1px solid %s; border-radius:%dpx;} QLabel#videoHistoryThumb {background:%s; color:%s; border-radius:%dpx;} QPushButton {padding:0;}' % (_color('video_creation_surface'), _color('video_creation_border'), px(8), _color('video_creation_preview_bg'), _color('video_creation_muted'), px(6)))
        self.findChild(QLabel, 'videoHistoryMeta').setStyleSheet('color:%s;' % _color('video_creation_muted'))
        self.status.setStyleSheet('color:%s;' % materials.text_color(1))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and not any(button.geometry().contains(event.pos()) for button in self.findChildren(ToolButton)):
            self.clicked.emit(self.record)
        super().mouseReleaseEvent(event)


class _SubmitWorker(QThread):
    submitted = pyqtSignal(object)
    task_updated = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, manager, kwargs, parent=None):
        super().__init__(parent); self.manager = manager; self.kwargs = kwargs

    def run(self):
        try:
            if hasattr(self.manager, 'prepare_tasks') and hasattr(self.manager, 'run_tasks'):
                self.manager.on_task = self.task_updated.emit
                try:
                    tasks = self.manager.prepare_tasks(**self.kwargs)
                except TypeError:
                    reduced = dict(self.kwargs); reduced.pop('reference_videos', None); reduced.pop('reference_audios', None)
                    tasks = self.manager.prepare_tasks(**reduced)
                self.submitted.emit(tasks); self.manager.run_tasks(tasks)
            elif callable(self.manager):
                self.submitted.emit(self.manager(**self.kwargs))
            else:
                raise RuntimeError(tr('视频生成服务暂不可用'))
        except Exception as error:  # noqa: BLE001
            self.failed.emit(str(error))


class _DownloadWorker(QThread):
    completed = pyqtSignal(str, object)
    failed = pyqtSignal(str, object)

    def __init__(self, manager, record, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.record = dict(record or {})

    def run(self):
        try:
            if hasattr(self.manager, 'download_task'):
                path = self.manager.download_task(self.record)
            elif hasattr(self.manager, 'download_result'):
                path = self.manager.download_result(self.record)
            else:
                path = self.record.get('file_path') or self.record.get('video_url') or ''
            updated = self._latest_record()
            self.completed.emit(str(path or ''), updated)
        except Exception as error:  # noqa: BLE001
            self.failed.emit(str(error), self._latest_record())

    def _latest_record(self):
        local_id = str(self.record.get('local_id') or '')
        store = getattr(self.manager, 'store', None)
        getter = getattr(store, 'get', None)
        if local_id and callable(getter):
            try:
                updated = getter(local_id)
                if isinstance(updated, dict):
                    return updated
            except Exception:
                pass
        return dict(self.record)


def _video_records(config_manager):
    try: records = catalog_snapshot(config_manager, 'video') or {}
    except Exception: records = builtin_models()
    result = {}
    for model_id, record in (records or {}).items():
        if isinstance(record, dict) and record.get('available', True) is not False and (record.get('kind') == 'video' or classify_model(model_id) == 'video'):
            result[str(model_id)] = dict(record)
    config = getattr(config_manager, 'config', {}) or {}; configured = str((config.get('video_creation') or {}).get('model') or '')
    if configured and configured not in result: result[configured] = {'id': configured, 'display_name': configured, 'kind': 'video', 'available': True}
    return result


class VideoCreationPage(QWidget):
    task_submitted = pyqtSignal(object)
    history_changed = pyqtSignal()
    log_message = pyqtSignal(str, str)
    navigate_workspace = pyqtSignal()
    open_settings_requested = pyqtSignal()

    def __init__(self, config_manager, parent=None):
        super().__init__(parent); self.setObjectName('videoCreationPage'); self.config_manager = config_manager
        self._workers, self._worker, self._manager, self.manager_factory = [], None, None, None
        self._tasks = {}; self._history_records = []; self._history_page = 1; self._history_query = ''
        self._history_dirty = True
        self._build_ui(); self.select_all_history.toggled.connect(self._toggle_select_all_history); self._load_config_defaults(); self._refresh_models(getattr(self, '_configured_model', '')); self._update_form_state()
        style_page(self); style_controls(self); register_retranslate(self)
        materials.register_theme_callback(self.refresh_colors)
        self.refresh_colors()

    @property
    def busy(self): return self.has_running_workers()

    def _build_ui(self):
        self.setMinimumWidth(0); root = scrollable_body(self); self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); self.body_content.setMinimumWidth(0); self.body_content.installEventFilter(self)
        root.setContentsMargins(px(26), px(16), px(26), px(20)); root.setSpacing(px(16))
        header = QHBoxLayout(); title_box = QVBoxLayout(); title_box.setSpacing(px(4)); self.title_label = TitleLabel(tr('视频创作'), self); self.title_label.setProperty('studioTitleSize', 20); self.caption_label = QLabel(tr('把想法写成画面，从这里开始。'), self); self.caption_label.setObjectName('videoCreationCaption'); title_box.addWidget(self.title_label); title_box.addWidget(self.caption_label); header.addLayout(title_box); header.addStretch(1)
        self.workspace_link = PushButton(FIF.DOCUMENT, tr('AI 视频工作台'), self); self.workspace_link.setObjectName('videoWorkspaceLink'); self.workspace_link.clicked.connect(self.navigate_workspace.emit); header.addWidget(self.workspace_link); root.addLayout(header)
        self.body_layout = QBoxLayout(QBoxLayout.LeftToRight); self.body_layout.setSpacing(px(24));
        self.left_column = QWidget(self); self.left_column.setMinimumWidth(0); self.left_column.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred); left = QVBoxLayout(self.left_column); left.setContentsMargins(0, 0, 0, 0); left.setSpacing(px(12)); left.addWidget(self._build_settings_card(), 1); self.body_layout.addWidget(self.left_column, 0)
        self.right_column = QWidget(self); self.right_column.setMinimumWidth(0); self.right_column.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred); right = QVBoxLayout(self.right_column); right.setContentsMargins(0, 0, 0, 0); right.setSpacing(px(12)); right.addWidget(self._build_preview_card(), 0); right.addWidget(self._build_history_card(), 1); self.body_layout.addWidget(self.right_column, 1); root.addLayout(self.body_layout, 1); self._fit_columns()

    def _build_settings_card(self):
        card = _VideoCard('videoCreationSettingsCard', self.left_column); card.setMinimumWidth(0); card.setMaximumWidth(px(372)); card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred); layout = QVBoxLayout(card); layout.setContentsMargins(px(16), px(16), px(16), px(16)); layout.setSpacing(px(12))
        head = QHBoxLayout(); text = QVBoxLayout(); self.settings_title = StrongBodyLabel(tr('🎬 创作设置'), card); self.settings_caption = QLabel(tr('选择模型，描述你的画面'), card); text.addWidget(self.settings_title); text.addWidget(self.settings_caption); head.addLayout(text, 1); self.settings_toggle = QToolButton(card); self.settings_toggle.setText('⌃'); self.settings_toggle.setCheckable(True); self.settings_toggle.setChecked(True); self.settings_toggle.clicked.connect(self._toggle_settings); head.addWidget(self.settings_toggle); layout.addLayout(head)
        self.settings_body = QWidget(card); body = QVBoxLayout(self.settings_body); body.setContentsMargins(0, 0, 0, 0); body.setSpacing(px(12));
        layout.setAlignment(Qt.AlignTop); body.setAlignment(Qt.AlignTop)
        self.model_label = QLabel(tr('🎬 视频模型'), self.settings_body); body.addWidget(self.model_label); self.model_combo = ComboBox(self.settings_body); self.model_combo.setObjectName('videoModelCombo'); self.model_selector = self.model_combo; self.model_combo.currentIndexChanged.connect(self._model_changed); self.model_combo.currentIndexChanged.connect(lambda _index: self._update_form_state()); body.addWidget(self.model_combo)
        api_row = QHBoxLayout(); self.api_label = QLabel(tr('🔗 API 连接'), self.settings_body); api_row.addWidget(self.api_label); api_row.addStretch(1); self.api_status = QLabel(self._api_status_text(), self.settings_body); self.api_status.setObjectName('videoApiStatus'); api_row.addWidget(self.api_status); self._style_api_status(); self.api_button = QToolButton(self.settings_body); self.api_button.setText('›'); self.api_button.clicked.connect(self.open_settings_requested.emit); api_row.addWidget(self.api_button); body.addLayout(api_row)
        self.mode_label = QLabel(tr('⚙ 生成模式'), self.settings_body); body.addWidget(self.mode_label); mode_row = QHBoxLayout(); self.text_mode_button = PushButton(tr('文生视频'), self.settings_body); self.text_mode_button.setCheckable(True); self.text_mode_button.setChecked(True); self.reference_mode_button = PushButton(tr('参考素材'), self.settings_body); self.reference_mode_button.setCheckable(True); mode_row.addWidget(self.text_mode_button); mode_row.addWidget(self.reference_mode_button); self.text_mode_button.clicked.connect(lambda: self._set_mode('text')); self.reference_mode_button.clicked.connect(lambda: self._set_mode('reference')); body.addLayout(mode_row)
        desc_row = QHBoxLayout(); self.prompt_label = QLabel(tr('📝 画面描述'), self.settings_body); desc_row.addWidget(self.prompt_label); desc_row.addStretch(1); self.expand_prompt_button = QToolButton(self.settings_body); self.expand_prompt_button.setText('⤢'); self.expand_prompt_button.setToolTip(tr('放大')); self.copy_prompt_button = QToolButton(self.settings_body); self.copy_prompt_button.setText('⧉'); self.copy_prompt_button.setToolTip(tr('复制')); desc_row.addWidget(self.expand_prompt_button); desc_row.addWidget(self.copy_prompt_button); body.addLayout(desc_row)
        self.prompt_edit = QPlainTextEdit(self.settings_body); self.prompt_edit.setObjectName('videoPromptEdit'); self.description_edit = self.prompt_edit; self.prompt_edit.setPlaceholderText(tr('描述你要生成的视频画面（例如：光束掠过湿润的赛博朋克街道，镜头平稳推进，雨水从黑色风衣滴落...）')); self.prompt_edit.setFixedHeight(px(150)); self.prompt_edit.textChanged.connect(self._prompt_text_changed); body.addWidget(self.prompt_edit); self.expand_prompt_button.clicked.connect(self._expand_prompt); self.copy_prompt_button.clicked.connect(self._copy_prompt)
        self.reference_panel = self._build_reference_panel(self.settings_body); body.addWidget(self.reference_panel)
        self.parameters_panel = self._build_parameters_panel(self.settings_body); body.addWidget(self.parameters_panel)
        self.generate_button = PrimaryPushButton(FIF.PLAY, tr('▶ 生成视频 ×1'), self.settings_body); self.generate_button.setObjectName('videoGenerateButton'); self.generate_button.setFixedHeight(px(44)); self.generate_button.clicked.connect(self.submit_generation); body.addWidget(self.generate_button); body.addStretch(1); layout.addWidget(self.settings_body); self._set_mode('text'); return card

    def _build_reference_panel(self, parent):
        panel = QWidget(parent); layout = QVBoxLayout(panel); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(px(8)); self.reference_images = []; self.reference_videos = []; self.reference_audios = []
        self.reference_images_label = QLabel(tr('参考图片（0/3）'), panel); layout.addWidget(self.reference_images_label)
        self.reference_image_drop_zone = _ReferenceImageDropZone(panel)
        self.reference_image_drop_zone.setFixedHeight(px(96))
        self.reference_image_drop_zone.setStyleSheet(
            'QFrame#videoReferenceImageDropZone { background:%s; border:1px dashed %s; border-radius:%dpx; } '
            'QLabel { color:%s; }'
            % (_color('video_creation_input'), _color('video_creation_border'), px(8), _color('video_creation_muted'))
        )
        self.reference_image_drop_zone.files_dropped.connect(self._accept_reference_images)
        self.reference_image_drop_zone.clicked.connect(self._choose_reference_images)
        layout.addWidget(self.reference_image_drop_zone)
        self.reference_images_grid_host = QWidget(panel)
        self.reference_images_grid = QGridLayout(self.reference_images_grid_host)
        self.reference_images_grid.setContentsMargins(0, 0, 0, 0)
        self.reference_images_grid.setHorizontalSpacing(px(10))
        self.reference_images_grid.setVerticalSpacing(px(10))
        layout.addWidget(self.reference_images_grid_host)
        # Kept as a compatibility handle for callers that used the old picker.
        self.reference_images_button = PushButton(tr('选择图片'), panel)
        self.reference_images_button.clicked.connect(self._choose_reference_images)
        self.reference_images_button.setVisible(False)
        unsupported = tr('当前线路未配置视频/音频上传能力')
        self.reference_videos_label = QLabel(tr('参考视频（0/3）'), panel); layout.addWidget(self.reference_videos_label); self.reference_videos_button = PushButton(tr('选择视频'), panel); self.reference_videos_button.setEnabled(False); self.reference_videos_button.setToolTip(unsupported); layout.addWidget(self.reference_videos_button); self.reference_videos_hint = QLabel(unsupported, panel); self.reference_videos_hint.setObjectName('videoReferenceUploadUnavailable'); self.reference_videos_hint.setWordWrap(True); layout.addWidget(self.reference_videos_hint)
        self.reference_audios_label = QLabel(tr('参考音频（0/3）'), panel); layout.addWidget(self.reference_audios_label); self.reference_audios_button = PushButton(tr('选择音频'), panel); self.reference_audios_button.setEnabled(False); self.reference_audios_button.setToolTip(unsupported); layout.addWidget(self.reference_audios_button); self.reference_audios_hint = QLabel(unsupported, panel); self.reference_audios_hint.setObjectName('videoReferenceUploadUnavailable'); self.reference_audios_hint.setWordWrap(True); layout.addWidget(self.reference_audios_hint)
        self.reference_hint = QLabel(tr('MiniMax-H3 会按 images、reference_videos、reference_audios 字段提交；可将素材写入 Prompt'), panel); self.reference_hint.setWordWrap(True); self.reference_hint.setObjectName('videoReferenceHint'); layout.addWidget(self.reference_hint)
        self._rebuild_reference_images()
        return panel

    def _build_parameters_panel(self, parent):
        panel = QFrame(parent); panel.setObjectName('videoGenerationParameters'); layout = QVBoxLayout(panel); layout.setContentsMargins(px(12), px(10), px(12), px(10)); layout.setSpacing(px(8))
        title_row = QHBoxLayout(); self.parameters_title = StrongBodyLabel(tr('生成参数'), panel); title_row.addWidget(self.parameters_title); title_row.addStretch(1); self.parameters_toggle = QToolButton(panel); self.parameters_toggle.setObjectName('videoParametersToggle'); self.parameters_toggle.setText('⌃'); self.parameters_toggle.setToolTip(tr('收起')); self.parameters_toggle.setCheckable(True); self.parameters_toggle.setChecked(True); self.parameters_toggle.clicked.connect(self._toggle_parameters); title_row.addWidget(self.parameters_toggle); layout.addLayout(title_row)
        self.parameters_content = QWidget(panel); content = QVBoxLayout(self.parameters_content); content.setContentsMargins(0, 0, 0, 0); content.setSpacing(px(8))
        duration_row = QHBoxLayout(); self.duration_label = QLabel(tr('⏱ 渲染时长'), self.parameters_content); duration_row.addWidget(self.duration_label); self.duration_spin = QSpinBox(self.parameters_content); self.duration_spin.setObjectName('videoDurationSpin'); self.duration_spin.setRange(4, 15); self.duration_spin.setValue(5); self.duration_spin.valueChanged.connect(self._duration_changed); duration_row.addWidget(self.duration_spin); duration_row.addWidget(QLabel(tr('秒 (4-15)'), self.parameters_content)); duration_row.addStretch(1); content.addLayout(duration_row)
        duration_shortcuts = QHBoxLayout(); duration_shortcuts.setSpacing(px(8)); self.duration_buttons = []
        for value in (5, 10, 15):
            button = PushButton(tr('{n}秒').format(n=value), self.parameters_content); button.setCheckable(True); button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed); button.setMinimumWidth(max(px(56), button.sizeHint().width())); button.clicked.connect(lambda _=False, v=value: self.duration_spin.setValue(v)); self.duration_buttons.append(button); duration_shortcuts.addWidget(button, 1)
        content.addLayout(duration_shortcuts)
        self.model_parameters_title = StrongBodyLabel(tr('🎨 模型专属参数'), self.parameters_content); self.model_parameters_title.setObjectName('videoModelParametersTitle'); content.addWidget(self.model_parameters_title)
        row = QGridLayout(); row.setHorizontalSpacing(px(8)); self.ratio_label = QLabel(tr('🎨 画面'), self.parameters_content); self.ratio_combo = ComboBox(self.parameters_content); self.ratio_combo.setObjectName('videoRatioCombo'); self.aspect_ratio_combo = self.ratio_combo; self.ratio_combo.addItems(['9:16', '16:9', '1:1']); self.ratio_combo.currentTextChanged.connect(self._ratio_changed); self.output_size_label = QLabel(tr('输出尺寸'), self.parameters_content); self.output_size_combo = ComboBox(self.parameters_content); self.output_size_combo.setObjectName('videoOutputSizeCombo'); row.addWidget(self.ratio_label, 0, 0); row.addWidget(self.output_size_label, 0, 1); row.addWidget(self.ratio_combo, 1, 0); row.addWidget(self.output_size_combo, 1, 1); row.setColumnStretch(0, 1); row.setColumnStretch(1, 1); content.addLayout(row)
        self.parameters_hint = QLabel(tr('模型参数会根据画幅与线路能力自动调整。'), self.parameters_content); self.parameters_hint.setWordWrap(True); self.parameters_hint.setObjectName('videoParametersHint'); content.addWidget(self.parameters_hint)
        count_row = QHBoxLayout(); self.count_label = QLabel(tr('生成次数'), self.parameters_content); count_row.addWidget(self.count_label); count_row.addStretch(1); self.count_spin = _CountStepper(self.parameters_content); self.count_spin.valueChanged.connect(self._count_changed); count_row.addWidget(self.count_spin); content.addLayout(count_row); layout.addWidget(self.parameters_content); return panel

    def _build_preview_card(self):
        card = _VideoCard('videoPreviewCard', self.right_column)
        layout = QVBoxLayout(card); layout.setContentsMargins(px(16), px(16), px(16), px(16)); layout.setSpacing(px(10))
        head = QHBoxLayout(); title_box = QVBoxLayout(); self.preview_title = StrongBodyLabel(tr('📄 作品预览'), card); self.preview_caption = QLabel(tr('播放、查看与下载生成结果'), card); title_box.addWidget(self.preview_title); title_box.addWidget(self.preview_caption); head.addLayout(title_box, 1); self.status_badge = QLabel('', card); self.status_badge.setObjectName('videoStatusBadge'); head.addWidget(self.status_badge); layout.addLayout(head)
        self.preview_area = QFrame(card); self.preview_area.setObjectName('videoPreviewArea'); self.preview_area.setMinimumSize(0, 0); self.preview_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed); self.preview_area.setStyleSheet('QFrame#videoPreviewArea { background: %s; border-radius: %dpx; } QFrame#videoPreviewArea QLabel { color: %s; } QFrame#videoPreviewArea QStackedWidget { background: transparent; }' % (_color('video_creation_preview_bg'), px(8), _color('video_creation_preview_text'))); preview_layout = QVBoxLayout(self.preview_area); preview_layout.setContentsMargins(px(16), px(16), px(16), px(16)); self.preview_stack = QStackedWidget(self.preview_area)
        self.preview_empty_page = QWidget(self.preview_stack)
        empty_layout = QVBoxLayout(self.preview_empty_page)
        empty_layout.setContentsMargins(0, 0, 0, 0)
        empty_layout.setSpacing(px(10))
        self.preview_empty_play = QToolButton(self.preview_empty_page)
        self.preview_empty_play.setObjectName('videoEmptyPlayButton')
        self.preview_empty_play.setIcon(FIF.PLAY.icon())
        self.preview_empty_play.setIconSize(QSize(px(22), px(22)))
        self.preview_empty_play.setToolTip(tr('开始生成'))
        self.preview_empty_play.setFixedSize(px(52), px(52))
        self.preview_empty_play.clicked.connect(self.submit_generation)
        empty_layout.addWidget(self.preview_empty_play, 0, Qt.AlignHCenter)
        self.preview_empty = QLabel(tr('下一帧，由你创作\n选择模型，写下画面描述\n生成的视频将在这里呈现'), self.preview_empty_page)
        self.preview_empty.setAlignment(Qt.AlignCenter)
        self.preview_empty.setWordWrap(True)
        empty_layout.addWidget(self.preview_empty, 0, Qt.AlignHCenter)
        self.preview_empty_page.setStyleSheet(
            'QToolButton#videoEmptyPlayButton { background:%s; color:%s; border:0; border-radius:%dpx; font-size:%dpx; } '
            'QToolButton#videoEmptyPlayButton:hover { background:%s; }'
            % (_color('video_creation_primary'), _color('video_creation_preview_text'), px(26), px(18), _color('video_creation_primary_soft'))
        )
        self.preview_stack.addWidget(self.preview_empty_page)
        self.preview_loading = QLabel(tr('生成中…'), self.preview_stack); self.preview_loading.setAlignment(Qt.AlignCenter); self.preview_stack.addWidget(self.preview_loading)
        self.preview_media_page = QWidget(self.preview_stack); media_layout = QVBoxLayout(self.preview_media_page); media_layout.setContentsMargins(0, 0, 0, 0)
        self.media_player = None
        try:
            if QMediaPlayer is not None and QAbstractVideoSurface is not None:
                self.media_player = QMediaPlayer(self.preview_media_page)
            self.media_available = bool(self.media_player is not None and self.media_player.isAvailable())
        except Exception:
            self.media_available = False
        if self.media_available:
            self.video_widget = _VideoPreviewWidget(self.preview_media_page)
            self.video_widget.setObjectName('videoPreviewWidget')
            self.video_widget.setAspectRatioMode(Qt.KeepAspectRatio)
            self.media_player.setVideoOutput(self.video_widget.surface)
            media_layout.addWidget(self.video_widget, 1)
            self.media_controls = QWidget(self.preview_media_page); controls = QHBoxLayout(self.media_controls); controls.setContentsMargins(0, 0, 0, 0); controls.setSpacing(px(6))
            self.media_play_button = QToolButton(self.media_controls); self.media_play_button.setObjectName('videoPlayPauseButton'); self.media_play_button.setIcon(FIF.PLAY.icon()); self.media_play_button.setToolTip(tr('播放')); self.media_play_button.clicked.connect(self._toggle_media_playback); controls.addWidget(self.media_play_button)
            self.media_time_label = QLabel('00:00 / 00:00', self.media_controls); self.media_time_label.setObjectName('videoMediaTime'); controls.addWidget(self.media_time_label); controls.addStretch(1)
            self.media_mute_button = QToolButton(self.media_controls); self.media_mute_button.setObjectName('videoMuteButton'); self.media_mute_button.setIcon(FIF.VOLUME.icon()); self.media_mute_button.setToolTip(tr('静音')); self.media_mute_button.clicked.connect(self._toggle_media_mute); controls.addWidget(self.media_mute_button)
            self.media_fullscreen_button = QToolButton(self.media_controls); self.media_fullscreen_button.setObjectName('videoFullscreenButton'); self.media_fullscreen_button.setIcon(FIF.FULL_SCREEN.icon()); self.media_fullscreen_button.setToolTip(tr('全屏')); self.media_fullscreen_button.clicked.connect(self._toggle_media_fullscreen); controls.addWidget(self.media_fullscreen_button)
            self.media_player.positionChanged.connect(self._media_position_changed); self.media_player.durationChanged.connect(self._media_duration_changed); self.media_player.stateChanged.connect(self._media_state_changed)
            try: self.media_player.error.connect(self._on_media_error)
            except (AttributeError, TypeError): pass
        else:
            if self.media_player is not None:
                self.media_player.deleteLater()
            self.video_widget = None; self.media_player = None; self.media_controls = None; self.media_play_button = self.media_time_label = self.media_mute_button = self.media_fullscreen_button = None; self.media_available = False
        self.preview_done = QLabel('', self.preview_media_page); self.preview_done.setAlignment(Qt.AlignCenter); self.preview_done.setWordWrap(True); self.preview_done.setVisible(not self.media_available); media_layout.addWidget(self.preview_done, 1); self.open_video_button = PushButton(FIF.FOLDER, tr('打开文件'), self.preview_media_page); self.open_video_button.clicked.connect(self._open_current_file);  self.preview_stack.addWidget(self.preview_media_page)
        failed_page = QWidget(self.preview_stack); failed_layout = QVBoxLayout(failed_page); self.preview_failed = QLabel('', failed_page); self.preview_failed.setAlignment(Qt.AlignCenter); self.preview_failed.setWordWrap(True); failed_layout.addWidget(self.preview_failed); self.retry_button = PushButton(FIF.SYNC, tr('重新生成'), failed_page); self.retry_button.clicked.connect(self._retry_current); failed_layout.addWidget(self.retry_button, 0, Qt.AlignCenter); self.preview_stack.addWidget(failed_page); preview_layout.addWidget(self.preview_stack); layout.addWidget(self.preview_area, 0, Qt.AlignHCenter)
        if self.media_controls is not None:
            layout.addWidget(self.media_controls)
        layout.addWidget(self.open_video_button, 0, Qt.AlignHCenter)
        url_row = QHBoxLayout(); self.url_edit = LineEdit(card); self.url_edit.setObjectName('videoUrlEdit'); self.url_edit.setReadOnly(True); self.url_edit.setPlaceholderText(tr('生成完成后显示视频路径或 URL')); url_row.addWidget(self.url_edit, 1); self.download_button = PushButton(FIF.DOWNLOAD, tr('下载视频'), card); self.download_button.clicked.connect(self._download_current); self.copy_url_button = PushButton(FIF.COPY, tr('复制链接'), card); self.copy_url_button.clicked.connect(self._copy_url); url_row.addWidget(self.download_button); url_row.addWidget(self.copy_url_button); layout.addLayout(url_row)
        self.history_detail_hint = QLabel('', card); self.history_detail_hint.setObjectName('videoHistoryDetailHint'); layout.addWidget(self.history_detail_hint)
        self.info_bar = QLabel(tr('温馨提示：平台不会永久保存数据，生成的视频仅保留 48 小时，请及时下载备份，逾期丢失概不负责。'), card); self.info_bar.setWordWrap(True); self.info_bar.setObjectName('videoInfoBar'); layout.addWidget(self.info_bar)
        progress_row = QHBoxLayout(); self.progress_label = QLabel('', card); self.progress_model_label = QLabel('', card); self.progress_model_label.setObjectName('videoProgressModel'); self.progress_model_label.setStyleSheet('QLabel#videoProgressModel { color:%s; background:%s; border:1px solid %s; border-radius:%dpx; padding:0 %dpx; }' % (_color('video_creation_muted'), _color('video_creation_input'), _color('video_creation_border'), px(9), px(6))); self.progress_model_label.setVisible(False); self.copy_task_id_button = QToolButton(card); self.copy_task_id_button.setObjectName('videoCopyTaskIdButton'); self.copy_task_id_button.setText('⧉'); self.copy_task_id_button.setToolTip(tr('复制')); self.copy_task_id_button.setVisible(False); self.copy_task_id_button.clicked.connect(self._copy_task_id); self.progress_bar = QProgressBar(card); self.progress_bar.setRange(0, 100); self.progress_bar.setValue(0); self.progress_bar.setTextVisible(False); progress_row.addWidget(self.progress_label); progress_row.addWidget(self.progress_model_label); progress_row.addWidget(self.copy_task_id_button); progress_row.addWidget(self.progress_bar, 1); layout.addLayout(progress_row); return card

    def _build_history_card(self):
        card = _VideoCard('videoHistoryCard', self.right_column); layout = QVBoxLayout(card); layout.setContentsMargins(px(16), px(16), px(16), px(16)); layout.setSpacing(px(8)); head = QHBoxLayout(); self.history_title = StrongBodyLabel(tr('历史视频 (0)'), card); head.addWidget(self.history_title); head.addStretch(1); self.clear_history_button = PushButton(tr('清空'), card); self.clear_history_button.clicked.connect(self.clear_history); self.history_toggle = QToolButton(card); self.history_toggle.setText('⌃'); self.history_toggle.setCheckable(True); self.history_toggle.setChecked(True); self.history_toggle.clicked.connect(lambda checked: self.history_list.setVisible(checked)); head.addWidget(self.clear_history_button); head.addWidget(self.history_toggle); layout.addLayout(head); self.history_caption = QLabel(tr('保留最近 20 条，记录只存于当前浏览器。'), card); layout.addWidget(self.history_caption); self.history_search = LineEdit(card); self.history_search.setPlaceholderText(tr('搜索 Prompt、任务 ID 或模型')); self.history_search.textChanged.connect(self._history_search_changed); layout.addWidget(self.history_search); toolbar = QHBoxLayout(); self.select_all_history = QCheckBox(tr('全选本页可下载'), card); toolbar.addWidget(self.select_all_history); toolbar.addStretch(1); self.batch_download_button = PushButton(tr('批量下载'), card); self.batch_download_button.clicked.connect(self._batch_download); toolbar.addWidget(self.batch_download_button); self.history_result_count = QLabel(tr('0 条结果'), card); toolbar.addWidget(self.history_result_count); layout.addLayout(toolbar); self.history_list = QWidget(card); self.history_layout = QVBoxLayout(self.history_list); self.history_layout.setContentsMargins(0, 0, 0, 0); self.history_layout.setSpacing(px(6)); layout.addWidget(self.history_list, 1); page_row = QHBoxLayout(); self.history_page_label = QLabel('', card); self.history_page_label.setAlignment(Qt.AlignCenter); self.history_prev_button = QToolButton(card); self.history_prev_button.setText('‹'); self.history_prev_button.setToolTip(tr('上一页')); self.history_prev_button.clicked.connect(lambda: self._change_history_page(-1)); self.history_next_button = QToolButton(card); self.history_next_button.setText('›'); self.history_next_button.setToolTip(tr('下一页')); self.history_next_button.clicked.connect(lambda: self._change_history_page(1)); page_row.addWidget(self.history_prev_button); page_row.addWidget(self.history_page_label, 1); page_row.addWidget(self.history_next_button); layout.addLayout(page_row); return card

    def resizeEvent(self, event): super().resizeEvent(event); self._fit_columns()
    def eventFilter(self, watched, event):
        if watched is getattr(self, 'body_content', None) and event.type() == QEvent.Resize: self._fit_columns()
        return super().eventFilter(watched, event)

    def _fit_columns(self):
        available = self.body_content.width() if getattr(self, 'body_content', None) else self.width()
        margins = self.body_content.layout().contentsMargins()
        available -= margins.left() + margins.right()
        stacked = available < px(980)
        self.body_layout.setSpacing(px(24))
        direction = QBoxLayout.TopToBottom if stacked else QBoxLayout.LeftToRight
        if self.body_layout.direction() != direction: self.body_layout.setDirection(direction)
        if stacked:
            self.left_column.setMinimumWidth(0); self.left_column.setMaximumWidth(16777215); self.left_column.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            self.right_column.setMinimumWidth(0); self.right_column.setMaximumWidth(16777215); self.right_column.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        else:
            self.left_column.setMinimumWidth(px(372)); self.left_column.setMaximumWidth(px(372)); self.left_column.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
            self.right_column.setMinimumWidth(0); self.right_column.setMaximumWidth(16777215); self.right_column.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.body_layout.invalidate(); self.updateGeometry(); QTimer.singleShot(0, self._update_preview_geometry)

    def _update_preview_geometry(self):
        if not hasattr(self, 'preview_area') or not self.preview_area.parentWidget():
            return
        ratio = {'9:16': 9 / 16, '16:9': 16 / 9, '1:1': 1.0}.get(self.ratio_combo.currentText(), 9 / 16)
        available_width = max(px(1), self.preview_area.parentWidget().contentsRect().width() - px(36))
        max_height = px(420)
        height = min(max_height, int(available_width / ratio))
        width = max(px(1), int(height * ratio))
        self.preview_area.setFixedSize(width, height)

    def _api_status_text(self):
        try: configured = bool(resolve_line(getattr(self.config_manager, 'config', {}), 'video').get('api_key'))
        except Exception: configured = False
        return tr('已配置') if configured else tr('未配置')

    def _style_api_status(self):
        configured = self.api_status.text() == tr('已配置')
        color = _color('video_creation_success') if configured else _color('video_creation_error')
        self.api_status.setStyleSheet('QLabel#videoApiStatus { color:%s; font-weight:600; }' % color)

    def _copy_task_id(self):
        task_id = str(getattr(self, '_current_task_id', '') or '')
        app = QApplication.instance()
        if app is not None and task_id:
            app.clipboard().setText(task_id)

    def _toggle_settings(self, checked): self.settings_body.setVisible(bool(checked)); self.settings_toggle.setText('⌃' if checked else '⌄')
    def _toggle_parameters(self, checked):
        self.parameters_content.setVisible(bool(checked)); self.parameters_toggle.setText('⌃' if checked else '⌄'); self.parameters_toggle.setToolTip(tr('收起') if checked else tr('展开'))
    def _set_mode(self, mode):
        self.mode = 'reference' if mode == 'reference' else 'text'; self.text_mode_button.setChecked(self.mode == 'text'); self.reference_mode_button.setChecked(self.mode == 'reference'); self.reference_panel.setVisible(self.mode == 'reference'); self._update_form_state()
    def _choose_reference_images(self):
        paths, _ = QFileDialog.getOpenFileNames(self, tr('选择参考图片'), '', tr('图片 (*.png *.jpg *.jpeg *.webp)'))
        self._accept_reference_images(paths)

    def _accept_reference_images(self, paths):
        allowed = {'.jpg', '.jpeg', '.png', '.webp'}
        incoming = []
        for path in paths or []:
            value = str(path or '')
            if value and Path(value).suffix.lower() in allowed and value not in self.reference_images:
                incoming.append(value)
        self.reference_images = (list(self.reference_images) + incoming)[:3]
        self._sync_reference_labels()

    def _clear_reference_image_tiles(self):
        while self.reference_images_grid.count():
            item = self.reference_images_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _rebuild_reference_images(self):
        self._clear_reference_image_tiles()
        has_images = bool(self.reference_images)
        self.reference_image_drop_zone.setVisible(not has_images)
        self.reference_images_grid_host.setVisible(has_images)
        if not has_images:
            return
        for index, path in enumerate(self.reference_images):
            tile = QFrame(self.reference_images_grid_host)
            tile.setObjectName('videoReferenceImageTile')
            tile.setFixedSize(px(96), px(96))
            tile.setStyleSheet(
                'QFrame#videoReferenceImageTile { background:%s; border:1px solid %s; border-radius:%dpx; }'
                % (_color('video_creation_surface'), _color('video_creation_border'), px(8))
            )
            tile_layout = QGridLayout(tile)
            tile_layout.setContentsMargins(0, 0, 0, 0)
            tile_layout.setSpacing(0)
            image = QToolButton(tile)
            image.setObjectName('videoReferenceImagePreview')
            image.clicked.connect(lambda _checked=False, item_index=index: self._preview_reference_image(item_index))
            image.setFixedSize(px(94), px(94))
            image.setStyleSheet('border-radius:%dpx;' % px(8))
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                image.setIcon(QIcon(pixmap))
                image.setIconSize(QSize(px(90), px(90)))
            else:
                image.setText(tr('图片'))
                image.setStyleSheet('color:%s; border-radius:%dpx;' % (_color('video_creation_muted'), px(8)))
            tile_layout.addWidget(image, 0, 0, 1, 2)
            remove = QToolButton(tile)
            remove.setText('×')
            remove.setToolTip(tr('删除参考图片'))
            remove.setObjectName('videoReferenceImageRemove')
            remove.setFixedSize(px(22), px(22))
            remove.clicked.connect(lambda _checked=False, item_index=index: self._remove_reference_image(item_index))
            tile_layout.addWidget(remove, 0, 1, Qt.AlignTop | Qt.AlignRight)
            insert = QToolButton(tile)
            insert.setText('⊕')
            insert.setToolTip(tr('插入参考图'))
            insert.setObjectName('videoReferenceImageInsert')
            insert.setFixedSize(px(22), px(22))
            insert.clicked.connect(lambda _checked=False, item_index=index: self._insert_reference_image(item_index))
            tile_layout.addWidget(insert, 0, 0, Qt.AlignBottom | Qt.AlignLeft)
            self.reference_images_grid.addWidget(tile, index // 3, index % 3)
            tile.show()
        if len(self.reference_images) < 3:
            self.reference_image_add_zone = _ReferenceImageDropZone(self.reference_images_grid_host)
            self.reference_image_add_zone.setObjectName('videoReferenceImageAddTile')
            self.reference_image_add_zone.setFixedSize(px(96), px(96))
            self.reference_image_add_zone.label.setText(tr('继续添加参考图片（{n}/3）').format(n=len(self.reference_images)))
            self.reference_image_add_zone.files_dropped.connect(self._accept_reference_images)
            self.reference_image_add_zone.clicked.connect(self._choose_reference_images)
            self.reference_image_add_zone.setStyleSheet(
                'QFrame#videoReferenceImageAddTile { background:%s; border:1px dashed %s; border-radius:%dpx; } '
                'QLabel { color:%s; }'
                % (_color('video_creation_input'), _color('video_creation_border'), px(8), _color('video_creation_muted'))
            )
            add_index = len(self.reference_images)
            self.reference_images_grid.addWidget(self.reference_image_add_zone, add_index // 3, add_index % 3)
            self.reference_image_add_zone.show()

    def _remove_reference_image(self, index):
        if 0 <= int(index) < len(self.reference_images):
            self.reference_images.pop(int(index))
            self._sync_reference_labels()

    def _preview_reference_image(self, index):
        self._lightbox = ImageLightbox.show_images(self, self.reference_images, index)

    def _insert_reference_image(self, index):
        if not 0 <= int(index) < len(self.reference_images):
            return
        current = self.prompt_edit.toPlainText().rstrip()
        token = '@参考图'
        self.prompt_edit.setPlainText((current + (' ' if current else '') + token).strip())
    def _choose_media(self, kind):
        # Reference video/audio upload is not available through the current
        # uploader. Keep these paths out of the request rather than pretending
        # that local files are relay URLs.
        if kind in {'video', 'audio'}:
            self._warning(tr('当前线路未配置视频/音频上传能力'))
            return
        if kind == 'video': paths, _ = QFileDialog.getOpenFileNames(self, tr('选择参考视频'), '', tr('视频 (*.mp4 *.webm *.mov)')); self.reference_videos = [str(path) for path in paths[:3]]
        else: paths, _ = QFileDialog.getOpenFileNames(self, tr('选择参考音频'), '', tr('音频 (*.mp3 *.wav *.m4a *.aac *.ogg)')); self.reference_audios = [str(path) for path in paths[:3]]
        self._sync_reference_labels()
    def _sync_reference_labels(self, rebuild=True):
        self.reference_images_label.setText(tr('参考图片（{n}/3）').format(n=len(self.reference_images))); self.reference_videos_label.setText(tr('参考视频（{n}/3）').format(n=len(self.reference_videos))); self.reference_audios_label.setText(tr('参考音频（{n}/3）').format(n=len(self.reference_audios)))
        if rebuild:
            self._rebuild_reference_images()

    def _refresh_models(self, selected=''):
        self._records = _video_records(self.config_manager); current = selected or self.model_combo.currentData() or ''; blocker = QSignalBlocker(self.model_combo); self.model_combo.clear()
        for model_id, record in self._records.items():
            self.model_combo.addItem(str(record.get('display_name') or record.get('name') or model_id), userData=model_id)
        del blocker; index = self.model_combo.findData(normalize_model_id(current, self._records.keys())); self.model_combo.setCurrentIndex(index if index >= 0 else (0 if self.model_combo.count() else -1)); self._model_changed(self.model_combo.currentIndex())
    def _model_changed(self, index):
        previous_ratio = self.ratio_combo.currentText() or str((getattr(self.config_manager, 'config', {}).get('video_creation') or {}).get('ratio') or '9:16')
        previous_size = self.output_size_combo.currentText()
        record = self._records.get(str(self.model_combo.itemData(index)), {})
        self.model_combo.setToolTip(str(self.model_combo.itemData(index) or ''))
        ratios = record.get('ratios') or ('9:16', '16:9', '1:1')
        supported = [str(item) for item in ratios if str(item) in {'9:16', '16:9', '1:1'}] or ['9:16', '16:9', '1:1']
        blocker = QSignalBlocker(self.ratio_combo); self.ratio_combo.clear(); self.ratio_combo.addItems(supported); self.ratio_combo.setCurrentText(previous_ratio if previous_ratio in supported else supported[0]); del blocker
        durations = record.get('durations') or tuple(range(4, 16))
        try: duration_values = [int(value) for value in durations]
        except (TypeError, ValueError): duration_values = list(range(4, 16))
        duration_values = [value for value in duration_values if 4 <= value <= 15] or list(range(4, 16))
        self.duration_spin.setRange(min(duration_values), max(duration_values))
        self._ratio_changed(self.ratio_combo.currentText())
        if self.output_size_combo.findText(previous_size) >= 0: self.output_size_combo.setCurrentText(previous_size)
        self._update_form_state()
    def _load_config_defaults(self):
        defaults = (getattr(self.config_manager, 'config', {}) or {}).get('video_creation') or {}; self._configured_model = str(defaults.get('model') or ''); self.mode = str(defaults.get('mode') or 'text'); self.ratio_combo.setCurrentText(str(defaults.get('ratio') or '9:16')); self.output_size_combo.setCurrentText(str(defaults.get('output_size') or '')); self.duration_spin.setValue(int(defaults.get('duration') or 5)); self.count_spin.setValue(int(defaults.get('count') or 1))
    def _ratio_changed(self, ratio):
        # Keep the reference dimensions stable when a model advertises several
        # resolutions. Extra model-specific sizes can follow the canonical
        # aspect-ratio size, but must not replace it for another ratio.
        sizes = {'9:16': '1088x1920', '16:9': '1920x1088', '1:1': '1024x1024'}
        record = self._records.get(str(self.model_combo.currentData()), {}) if hasattr(self, '_records') else {}
        configured = [str(item) for item in (record.get('resolutions') or record.get('output_sizes') or ()) if str(item)]
        primary = sizes.get(str(ratio), '1088x1920')
        values = [primary]
        values.extend(item for item in configured if item != primary and item not in values)
        self.output_size_combo.clear(); self.output_size_combo.addItems(values); QTimer.singleShot(0, self._update_preview_geometry)
    def _duration_changed(self, value):
        for button, number in zip(self.duration_buttons, (5, 10, 15)): button.setChecked(int(value) == number)
    def _count_changed(self, value): self.generate_button.setText(tr('▶ 生成视频 ×{n}').format(n=int(value))); self._update_form_state()
    def _prompt_text_changed(self):
        text = self.prompt_edit.toPlainText()
        if len(text) > 5000:
            blocker = QSignalBlocker(self.prompt_edit)
            self.prompt_edit.setPlainText(text[:5000])
            cursor = self.prompt_edit.textCursor()
            cursor.movePosition(cursor.End)
            self.prompt_edit.setTextCursor(cursor)
            del blocker
        self._update_form_state()
    def _update_form_state(self):
        valid = bool(self.prompt_edit.toPlainText().strip()) and len(self.prompt_edit.toPlainText()) <= 5000 and bool(self.model_combo.currentData()); self.generate_button.setEnabled(valid)
    def _expand_prompt(self):
        dialog = QDialog(self); dialog.setWindowTitle(tr('放大画面描述')); dialog.resize(px(640), px(420)); layout = QVBoxLayout(dialog); editor = QPlainTextEdit(dialog); editor.setPlainText(self.prompt_edit.toPlainText()); layout.addWidget(editor); save = PrimaryPushButton(tr('保存'), dialog); save.clicked.connect(lambda: (self.prompt_edit.setPlainText(editor.toPlainText()), dialog.accept())); layout.addWidget(save); dialog.exec_()
    def _copy_prompt(self):
        app = QApplication.instance()
        if app is not None:
            app.clipboard().setText(self.prompt_edit.toPlainText())

    def _warning(self, text):
        try: InfoBar.warning(tr('提示'), text, parent=self)
        except Exception: pass
    def _success(self, text):
        try: InfoBar.success(tr('已提交'), text, parent=self)
        except Exception: pass
    def _manager_instance(self):
        if self._manager is not None: return self._manager
        if callable(self.manager_factory): self._manager = self.manager_factory(); return self._manager
        try:
            from core.manual_video import ManualVideoManager, get_manual_video_store
            store = get_manual_video_store(self.config_manager, log=getattr(self.config_manager, 'log_callback', None)); self._manager = ManualVideoManager(self.config_manager, store)
        except Exception: self._manager = None
        return self._manager
    def submit_generation(self):
        prompt = self.prompt_edit.toPlainText().strip(); model = str(self.model_combo.currentData() or '').strip()
        if not prompt: self._warning(tr('请输入画面描述')); return False
        if len(prompt) > 5000: self._warning(tr('画面描述不能超过5000字')); return False
        if not model: self._warning(tr('请选择视频模型')); return False
        manager = self._manager_instance()
        if manager is None: self._warning(tr('视频生成服务暂不可用')); return False
        kwargs = dict(prompt=prompt, model=model, mode=self.mode, ratio=self.ratio_combo.currentText(), output_size=self.output_size_combo.currentText(), duration=int(self.duration_spin.value()), count=int(self.count_spin.value()), reference_images=list(self.reference_images) if self.mode == 'reference' else [], reference_videos=list(self.reference_videos) if self.mode == 'reference' else [], reference_audios=list(self.reference_audios) if self.mode == 'reference' else [])
        worker = _SubmitWorker(manager, kwargs, self); self._worker = worker; self._workers.append(worker); worker.submitted.connect(self._on_submitted); worker.task_updated.connect(self._on_task); worker.failed.connect(self._on_submit_failed); worker.finished.connect(lambda w=worker: self._worker_finished(w)); worker.start(); return True
    def _worker_finished(self, worker):
        try: self._workers.remove(worker)
        except ValueError: pass
        worker.deleteLater()
    def _on_submitted(self, records):
        for record in records if isinstance(records, (list, tuple)) else []: self._on_task(record)
        self.task_submitted.emit(records); self.history_changed.emit(); self._success(tr('已提交 {n} 个视频生成任务，可在任务历史查看').format(n=len(records) if isinstance(records, (list, tuple)) else self.count_spin.value()))
    def _on_task(self, record):
        if not isinstance(record, dict): return
        local_id = str(record.get('local_id') or record.get('task_id') or '');
        if local_id:
            self._tasks[local_id] = dict(record)
            self._current_record = dict(record)
        self.history_detail_hint.clear()
        status = str(record.get('status') or '').lower(); self._render_task(record); self.refresh_history(); self.history_changed.emit()
        if status in {'succeeded', 'completed'}: self._set_preview_done(record)
    def _render_task(self, record):
        status = str(record.get('status') or 'pending').lower(); progress = record.get('progress'); task_id = str(record.get('task_id') or record.get('local_id') or ''); model = str(record.get('model') or '')
        self.status_badge.setText({'running': tr('生成中'), 'processing': tr('生成中'), 'downloading': tr('下载中'), 'succeeded': tr('已完成'), 'completed': tr('已完成'), 'failed': tr('失败'), 'cancelled': tr('已取消')}.get(status, '')); self.status_badge.setProperty('status', status); self.status_badge.setStyleSheet(self._status_badge_style(status));
        if status == 'downloading':
            progress_text = tr('下载中…')
            if progress is not None:
                progress_text += f' {int(progress)}%'
            if task_id:
                progress_text += f' · ID: {task_id}'
        else:
            progress_text = f"{int(progress)}% · ID: {task_id}" if progress is not None else (f"ID: {task_id}" if task_id else '')
        self.progress_label.setText(progress_text); self.progress_model_label.setText(model); self.progress_model_label.setVisible(bool(model)); self._current_task_id = task_id; self.copy_task_id_button.setVisible(bool(task_id));
        if progress is None and status in {'pending', 'running', 'processing', 'downloading'}:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(max(0, min(100, int(float(progress if progress is not None else (100 if status in {'succeeded', 'completed'} else 0))))))
        self.preview_stack.setCurrentIndex(1 if status in {'pending', 'running', 'processing', 'downloading'} else 3 if status in {'failed', 'error'} else 2 if status in {'succeeded', 'completed'} else 0); self.preview_failed.setText(str(record.get('error') or tr('视频生成失败')))

    def _status_badge_style(self, status):
        color = _color('video_creation_primary') if status in {'pending', 'running', 'processing', 'downloading'} else _color('video_creation_success') if status in {'succeeded', 'completed'} else _color('video_creation_error') if status in {'failed', 'error', 'cancelled'} else _color('video_creation_muted')
        return 'QLabel#videoStatusBadge { color:%s; border:1px solid %s; border-radius:%dpx; padding:0 %dpx; font-weight:600; }' % (color, color, px(10), px(8))

    @staticmethod
    def _media_time(value):
        seconds = max(0, int(float(value or 0) / 1000))
        return f'{seconds // 60:02d}:{seconds % 60:02d}'

    def _media_position_changed(self, position):
        if self.media_player is None or self.media_time_label is None:
            return
        self.media_time_label.setText(f'{self._media_time(position)} / {self._media_time(self.media_player.duration())}')

    def _media_duration_changed(self, duration):
        self._media_position_changed(self.media_player.position() if self.media_player is not None else 0)

    def _media_state_changed(self, state):
        if self.media_play_button is None:
            return
        playing = state == QMediaPlayer.PlayingState
        self.media_play_button.setIcon((FIF.PAUSE if playing else FIF.PLAY).icon())
        self.media_play_button.setToolTip(tr('暂停') if playing else tr('播放'))

    def _toggle_media_playback(self):
        if self.media_player is None:
            return
        try:
            if self.media_player.state() == QMediaPlayer.PlayingState:
                self.media_player.pause()
            else:
                self.media_player.play()
        except Exception:
            self._on_media_error()

    def _toggle_media_mute(self):
        if self.media_player is None:
            return
        try:
            muted = not bool(self.media_player.isMuted())
            self.media_player.setMuted(muted)
            self.media_mute_button.setIcon((FIF.MUTE if muted else FIF.VOLUME).icon())
            self.media_mute_button.setToolTip(tr('取消静音') if muted else tr('静音'))
        except Exception:
            self._on_media_error()

    def _toggle_media_fullscreen(self):
        if self.video_widget is None:
            return
        try:
            if self.video_widget.isFullScreen():
                self.video_widget.showNormal()
            else:
                self.video_widget.showFullScreen()
        except Exception:
            self._on_media_error()

    def _on_media_error(self, _error=None):
        """Keep a usable file fallback when the optional multimedia backend fails."""
        record = getattr(self, '_current_record', None) or {}
        path = str(record.get('file_path') or self.url_edit.text() or '')
        self.media_available = False
        if self.video_widget is not None:
            self.video_widget.setVisible(False)
        if self.media_controls is not None:
            self.media_controls.setVisible(False)
        self.preview_done.setVisible(True)
        self.preview_done.setText(self._preview_done_text(record, path))
        self.open_video_button.setVisible(bool(path and os.path.isfile(path)))

    @staticmethod
    def _download_details(record):
        try:
            size_bytes = int(record.get('size_bytes') or 0)
            throughput = float(record.get('throughput_bps') or 0)
            if throughput <= 0:
                download_ms = float(record.get('download_ms') or 0)
                if download_ms > 0 and size_bytes > 0:
                    throughput = size_bytes * 1000 / download_ms
            if size_bytes <= 0 or throughput <= 0:
                return ''
            return f'{size_bytes / 1024**2:.1f} MB · {throughput / 1024**2:.1f} MB/s'
        except (TypeError, ValueError, OverflowError):
            return ''

    @classmethod
    def _preview_done_text(cls, record, path):
        if path and record.get('file_path'):
            details = cls._download_details(record)
            suffix = f'\n{details}' if details else ''
            return f'{tr("已保存到本地")}\n{path}{suffix}'
        return tr('视频已生成\n{path}').format(path=path or tr('暂无视频文件'))

    def _set_preview_done(self, record, *, from_history=False):
        self._current_record = dict(record or {})
        path = str(record.get('file_path') or record.get('video_url') or '')
        self.url_edit.setText(path); self.preview_done.setText(self._preview_done_text(record, path))
        detail = self._download_details(record)
        if path and record.get('file_path'):
            detail = tr('已保存到本地') + (f' · {detail}' if detail else '')
        if from_history:
            detail = tr('已打开历史视频详情') + (f' · {detail}' if detail else '')
        self.history_detail_hint.setText(detail)
        if self.media_player is not None and path:
            try:
                self.media_available = True
                if self.video_widget is not None:
                    self.video_widget.setVisible(True)
                if self.media_controls is not None:
                    self.media_controls.setVisible(True)
                self.preview_done.setVisible(False)
                source = QUrl.fromLocalFile(path) if os.path.isfile(path) else QUrl(path)
                self.media_player.setMedia(QMediaContent(source)); self.media_player.play()
            except Exception:
                self._on_media_error()
        if not self.media_available:
            self._on_media_error()
    def _on_submit_failed(self, error): self._warning(tr('提交失败：{message}').format(message=error or tr('请稍后重试')))
    def _download_current(self):
        record = getattr(self, '_current_record', None)
        if record:
            existing = str(record.get('file_path') or '')
            if existing and self._is_valid_local_video(existing):
                InfoBar.success(tr('已保存到本地'), existing, parent=self, duration=3500)
                return False
            return self._download_record(record)
        path = self.url_edit.text().strip()
        if path and self._is_valid_local_video(path):
            InfoBar.success(tr('已保存到本地'), path, parent=self, duration=3500)
            return False

    @staticmethod
    def _is_valid_local_video(path):
        try:
            from core.manual_video import ManualVideoManager
            ManualVideoManager._validate_mp4(path)
            return True
        except (OSError, ValueError):
            return False

    def _download_record(self, record):
        manager = self._manager_instance()
        if manager is None: return False
        worker = _DownloadWorker(manager, record, self); self._workers.append(worker); self._worker = worker
        worker.completed.connect(self._on_download_completed); worker.failed.connect(self._on_download_failed); worker.finished.connect(lambda w=worker: self._worker_finished(w)); worker.start(); return True

    def _on_download_completed(self, path, record):
        if path:
            updated = dict(record or {})
            updated.update(file_path=path, status='succeeded')
            self._current_record = updated
            self._render_task(updated)
            self._set_preview_done(updated)
            self.refresh_history()
            self.history_changed.emit()
            if os.path.isfile(path): QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _on_download_failed(self, error, record):
        if isinstance(record, dict):
            self._current_record = dict(record)
            self._render_task(record)
            self.refresh_history()
            self.history_changed.emit()
        self._warning(tr('下载视频失败：{message}').format(message=error or tr('请稍后重试')))
    def _copy_url(self):
        app = QApplication.instance()
        if app is not None:
            app.clipboard().setText(self.url_edit.text())

    def _open_current_file(self):
        """Open the completed local file with the platform default handler."""
        record = getattr(self, '_current_record', None) or {}
        path = str(record.get('file_path') or self.url_edit.text() or '')
        if path and os.path.isfile(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            return True
        return False

    def _retry_current(self):
        """Create a new task using the current record's prompt."""
        record = getattr(self, '_current_record', None) or {}
        prompt = str(record.get('prompt') or '').strip()
        if prompt:
            self.prompt_edit.setPlainText(prompt)
        return self.submit_generation()
    def _history_store(self):
        try:
            from core.manual_video import get_manual_video_store
            return get_manual_video_store(self.config_manager, log=getattr(self.config_manager, 'log_callback', None))
        except Exception: return None
    def refresh_history(self):
        self._history_dirty = False
        store = self._history_store(); records = []
        try: records = store.list(limit=20) if store is not None else []
        except Exception: records = []
        records.extend(self._tasks.values()); unique = {str(item.get('local_id') or item.get('task_id')): item for item in records if isinstance(item, dict)}; self._history_records = list(unique.values()); self._render_history()
    def _history_search_changed(self, text): self._history_query = str(text or '').strip().lower(); self._history_page = 1; self._render_history()
    def _change_history_page(self, delta): self._history_page = max(1, self._history_page + int(delta)); self._render_history()
    def _toggle_select_all_history(self, checked):
        for row in self.history_list.findChildren(_HistoryRow):
            if row.record.get('file_path') or row.record.get('video_url'):
                row.check.setChecked(bool(checked))
    def _render_history(self):
        records = [r for r in self._history_records if not self._history_query or self._history_query in ' '.join(str(r.get(k) or '') for k in ('prompt', 'prompt_name', 'task_id', 'model')).lower()]; page_size = 5; pages = max(1, (len(records) + page_size - 1) // page_size); self._history_page = min(self._history_page, pages); shown = records[(self._history_page - 1) * page_size:self._history_page * page_size]; self.history_title.setText(tr('历史视频 ({n})').format(n=len(self._history_records))); self.history_result_count.setText(tr('{n} 条结果').format(n=len(records))); self.history_page_label.setText(tr('第 {start}-{end} 条，共 {total} 条').format(start=((self._history_page - 1) * page_size + 1) if shown else 0, end=((self._history_page - 1) * page_size + len(shown)) if shown else 0, total=len(records)))
        while self.history_layout.count():
            item = self.history_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide(); widget.setParent(None); widget.deleteLater()
        for record in shown:
            row = _HistoryRow(record, self.history_list); row.clicked.connect(self._load_history_record); row.delete_requested.connect(self._delete_history_record); row.folder_requested.connect(self._open_history_folder); row.regenerate_requested.connect(self._regenerate_record); self.history_layout.addWidget(row)
        self.history_layout.addStretch(1)
        self.history_prev_button.setEnabled(self._history_page > 1); self.history_next_button.setEnabled(self._history_page < pages)
    def _load_history_record(self, record):
        self._current_record = dict(record or {})
        self._render_task(record)
        if str(record.get('status') or '').lower() in {'succeeded', 'completed'}:
            self._set_preview_done(record, from_history=True)
        self.history_detail_hint.setText(tr('已打开历史视频详情'))
    def _delete_history_record(self, record):
        store = self._history_store()
        try:
            if store is not None: store.delete(record.get('local_id'))
        except Exception: pass
        self.refresh_history()
    def _open_history_folder(self, record):
        path = record.get('output_dir') or record.get('file_path') or ''; path = str(Path(path).parent if Path(path).is_file() else path); QDesktopServices.openUrl(QUrl.fromLocalFile(path)) if path else None
    def _regenerate_record(self, record): self.prompt_edit.setPlainText(str(record.get('prompt') or '')); self.submit_generation()
    def clear_history(self):
        store = self._history_store()
        for record in list(self._history_records):
            try:
                if store is not None: store.delete(record.get('local_id'))
            except Exception: pass
        self._tasks.clear()
        self.refresh_history()
    def _batch_download(self):
        for row in self.history_list.findChildren(_HistoryRow):
            if row.check.isChecked(): self._download_record(row.record)

    def has_running_workers(self):
        return any(worker.isRunning() for worker in tuple(self._workers) if worker is not None)
    def shutdown_workers(self, wait_ms=1000):
        global _SHUTDOWN_HOOK_CONNECTED
        for worker in tuple(self._workers):
            manager = getattr(worker, 'manager', None)
            if manager is not None and hasattr(manager, 'cancel'):
                try: manager.cancel()
                except Exception: pass
            if worker.isRunning(): worker.wait(wait_ms)
            if worker.isRunning():
                try: worker.setParent(None)
                except RuntimeError: pass
                with _ORPHAN_WORKERS_LOCK: _ORPHAN_WORKERS.add(worker)
                worker.finished.connect(lambda w=worker: _dispose_worker(w))
            else: worker.deleteLater()
        if _ORPHAN_WORKERS and not _SHUTDOWN_HOOK_CONNECTED and QCoreApplication.instance() is not None:
            QCoreApplication.instance().aboutToQuit.connect(_wait_orphan_workers); _SHUTDOWN_HOOK_CONNECTED = True
        return self.has_running_workers()
    def closeEvent(self, event): self.shutdown_workers(); super().closeEvent(event)
    def showEvent(self, event):
        super().showEvent(event)
        if self._history_dirty:
            self.refresh_history()

    def refresh_interface_scale(self):
        self._fit_columns(); self.body_layout.invalidate(); self.body_layout.activate(); self.body_content.updateGeometry(); self._update_preview_geometry()
    def refresh_colors(self):
        for card in self.findChildren(_VideoCard): card.refresh_colors()
        self.caption_label.setStyleSheet('color:%s;' % _color('video_creation_muted'))
        self.status_badge.setFixedHeight(px(24))
        self.body_content.setStyleSheet(
            'QToolButton {background:%s; color:%s; border:1px solid %s; border-radius:%dpx; padding:%dpx;}'
            'QToolButton:hover {background:%s;}'
            'QToolButton:disabled {color:%s;}'
            'QCheckBox {color:%s; background:transparent;}'
            'QCheckBox::indicator {width:%dpx; height:%dpx; background:%s; border:1px solid %s; border-radius:%dpx;}'
            'QCheckBox::indicator:checked {background:%s;}'
            % (_color('video_creation_input'), materials.text_color(1), _color('video_creation_border'), px(5), px(3),
               _color('video_creation_primary_soft'), _color('video_creation_muted'), materials.text_color(1),
               px(14), px(14), _color('video_creation_input'), _color('video_creation_border'), px(3),
               _color('video_creation_primary')))
        self._style_api_status()
        self.progress_model_label.setStyleSheet('QLabel#videoProgressModel { color:%s; background:%s; border:1px solid %s; border-radius:%dpx; padding:0 %dpx; }' % (_color('video_creation_muted'), _color('video_creation_input'), _color('video_creation_border'), px(9), px(6)))
        self.preview_area.setStyleSheet(
            'QFrame#videoPreviewArea { background: %s; border-radius: %dpx; } '
            'QFrame#videoPreviewArea QLabel { color: %s; } '
            'QFrame#videoPreviewArea QStackedWidget { background: transparent; }'
            % (_color('video_creation_preview_bg'), px(8), _color('video_creation_preview_text'))
        )
        self.preview_empty_play.setStyleSheet(
            'QToolButton#videoEmptyPlayButton { background:%s; color:%s; border:0; border-radius:%dpx; font-size:%dpx; } '
            'QToolButton#videoEmptyPlayButton:hover { background:%s; }'
            % (_color('video_creation_primary'), _color('video_creation_preview_text'), px(26), px(18), _color('video_creation_primary_soft'))
        )
        for button, icon in ((self.open_video_button, FIF.FOLDER), (self.retry_button, FIF.SYNC)):
            # The playback surface stays dark in both themes. Register both
            # variants so Fluent's deferred show polish retains the contrast.
            rules = ('PushButton {background:%s; color:%s; border:1px solid %s; border-radius:%dpx;}'
                     % (_color('video_creation_preview_bg'), _color('video_creation_preview_text'),
                        _color('video_creation_muted'), px(8)))
            setCustomStyleSheet(button, rules, rules)
            button.setIcon(icon.icon(color=QColor(_color('video_creation_preview_text'))))
        self.status_badge.setStyleSheet(self._status_badge_style(getattr(self.status_badge, 'property', lambda _name: '')('status') or ''))
        self.reference_image_drop_zone.setStyleSheet(
            'QFrame#videoReferenceImageDropZone { background:%s; border:1px dashed %s; border-radius:%dpx; } '
            'QLabel { color:%s; }'
            % (_color('video_creation_input'), _color('video_creation_border'), px(8), _color('video_creation_muted'))
        )
        surface = _color('video_creation_input')
        border = _color('video_creation_border')
        text = materials.text_color(1)
        self.prompt_edit.setStyleSheet(
            'QPlainTextEdit#videoPromptEdit {background:%s; color:%s; border:1px solid %s; '
            'border-radius:%dpx; padding:%dpx; font-size:%dpx;}'
            % (surface, text, border, px(8), px(10), px(13)))
        self.duration_spin.setStyleSheet(
            'QSpinBox {background:%s; color:%s; border:1px solid %s; border-radius:%dpx; padding:%dpx;}'
            % (surface, text, border, px(6), px(4)))
        self.parameters_content.parentWidget().setStyleSheet(
            'QFrame#videoGenerationParameters {background:%s; border:1px solid %s; border-radius:%dpx;}'
            % (surface, border, px(8)))
        self.progress_bar.setStyleSheet(
            'QProgressBar {background:%s; border:0; border-radius:%dpx; min-height:%dpx;}'
            'QProgressBar::chunk {background:%s; border-radius:%dpx;}'
            % (surface, px(4), px(8), _color('video_creation_primary'), px(4)))
        self.info_bar.setStyleSheet('background:%s; color:%s; border-radius:%dpx; padding:%dpx;'
                                   % (_color('video_creation_info_bg'), text, px(8), px(10)))
        for row in self.history_list.findChildren(_HistoryRow):
            row._refresh_style()
        for tile in self.reference_images_grid_host.findChildren(QFrame, 'videoReferenceImageTile'):
            tile.setStyleSheet('QFrame#videoReferenceImageTile {background:%s; border:1px solid %s; border-radius:%dpx;}'
                              % (surface, border, px(8)))
        add = getattr(self, 'reference_image_add_zone', None)
        if add is not None:
            add.setStyleSheet('QFrame#videoReferenceImageAddTile {background:%s; border:1px dashed %s; border-radius:%dpx;} QLabel {color:%s;}'
                             % (surface, border, px(8), _color('video_creation_muted')))
    def retranslate_ui(self):
        self.title_label.setText(tr('视频创作')); self.caption_label.setText(tr('把想法写成画面，从这里开始。')); self.workspace_link.setText(tr('AI 视频工作台')); self.settings_title.setText(tr('🎬 创作设置')); self.settings_caption.setText(tr('选择模型，描述你的画面')); self.model_label.setText(tr('🎬 视频模型')); self.api_label.setText(tr('🔗 API 连接')); self.api_status.setText(self._api_status_text()); self._style_api_status(); self.mode_label.setText(tr('⚙ 生成模式')); self.text_mode_button.setText(tr('文生视频')); self.reference_mode_button.setText(tr('参考素材')); self.prompt_label.setText(tr('📝 画面描述')); self.prompt_edit.setPlaceholderText(tr('描述你要生成的视频画面（例如：光束掠过湿润的赛博朋克街道，镜头平稳推进，雨水从黑色风衣滴落...）')); self.parameters_title.setText(tr('生成参数')); self.model_parameters_title.setText(tr('🎨 模型专属参数')); self.parameters_toggle.setToolTip(tr('收起') if self.parameters_toggle.isChecked() else tr('展开')); self.info_bar.setText(tr('温馨提示：平台不会永久保存数据，生成的视频仅保留 48 小时，请及时下载备份，逾期丢失概不负责。')); self.reference_image_drop_zone.label.setText(tr('点击或拖入参考图片')); self._sync_reference_labels(rebuild=False); self._count_changed(self.count_spin.value()); self._update_form_state()
        for row in self.history_list.findChildren(_HistoryRow):
            row.retranslate_ui()
        if self.media_play_button is not None: self.media_play_button.setToolTip(tr('暂停') if self.media_player is not None and self.media_player.state() == QMediaPlayer.PlayingState else tr('播放'))
        if self.media_mute_button is not None: self.media_mute_button.setToolTip(tr('取消静音') if self.media_player is not None and self.media_player.isMuted() else tr('静音'))
        if self.media_fullscreen_button is not None: self.media_fullscreen_button.setToolTip(tr('退出全屏') if self.video_widget is not None and self.video_widget.isFullScreen() else tr('全屏'))
