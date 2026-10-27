"""电商图批量制作页：共享默认 + 每行一个商品 + 粘贴/拖拽 + 草稿持久化。

页面 1:1 复刻参考软件「电商图批量制作」页（Electron 版 ProductImageBatch）：
- 页头标题 / 副标题 + 右上「历史任务」「单个制作」切换；
- 共享默认卡：25 平台多选（国内/海外，各两列）、图片文字语言、轮播图/详情图
  启用开关 + 张数 + 9 个比例 chip、AI 分析模型 / 图片生成模型、分辨率、风格参考图；
- 工具栏：新增 1 行 / 新增 5 行 / 全选 / 复制选中（N）/ 删除选中 / 清空 + 统计；
- 每行：复选框 + 序号 + 复制/删除 · 图片格（首图 + 计数 + 画廊选图/已用图片/输入）·
  产品名称 + 产品信息 · 就绪/待完善徽章；最多 30 行；不完整行淡红底；
- 图片格支持拖拽文件、图片格/名称/信息框支持 Ctrl+V 粘贴截图；
- 共享默认 + 所有行实时（500ms 防抖、原子写）保存到 config 目录
  `ecommerce_batch_draft.json`，构造时自动恢复，损坏 JSON 静默忽略并中文提示。

本文件只通过信号把提交任务交给宿主页（`submit_requested`），不调导演台 / 不联网；
所有用户可见中文均经 `tr()`，颜色取 `ui/materials.py` 常量，尺寸取 `ui/tokens.px()`。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from PyQt5.QtCore import (
    QEvent, QSize, QSignalBlocker, Qt, QTimer, pyqtSignal,
)
from PyQt5.QtGui import (
    QColor, QImage, QPainter, QPainterPath, QPen, QPixmap,
)
from PyQt5.QtWidgets import (
    QApplication, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    CaptionLabel, CheckBox, ComboBox, FluentIcon as FIF, LineEdit,
    PlainTextEdit, PrimaryPushButton, PushButton, SpinBox, StrongBodyLabel,
    TitleLabel, TransparentToolButton,
)

from core.ecommerce.image_client import TIER_SHORT_EDGE
from core.ecommerce.platforms import (
    LANGUAGE_TEXT, MAX_PLATFORMS, RATIOS, domestic, get_platform, overseas,
)
from core.i18n import tr
from core.model_catalog import normalize_model_id
from .. import materials
from ..components.image_pickers import (
    GalleryPickerDialog, UrlImageDialog, UsedImagePickerDialog,
    image_cache_dir, is_image_file, resolve_input_entries,
)
from ..components.image_upload_grid import ImageUploadGrid
from ..components.image_lightbox import ImageLightbox
from ..components.platform_multi_select import PlatformMultiSelect
from ..components.model_selector import ModelPickComboBox, catalog_snapshot
from ..components.studio_dialog import safe_delete_dialog
from ..responsive import WrapLayout
from ..theme import style_controls
from ..tokens import px

__all__ = ['EcommerceBatchPage', 'EcommerceBatchRow', 'RatioChipGroup', 'RowImageCell']

MAX_ROWS = 30
MAX_PRODUCT_IMAGES = 6
DRAFT_FILENAME = 'ecommerce_batch_draft.json'
DRAFT_VERSION = 1
DRAFT_DEBOUNCE_MS = 500
DEFAULT_IMAGE_MODEL = 'gpt-image-2.5-sunburst'
DEFAULT_ANALYSIS_MODEL = 'gemini-3.8-flash'
FALLBACK_TIER = '2K'


# ---------------------------------------------------------------------------
# 纯函数 / 颜色（颜色只从 materials 取，本文件不出现十六进制字面量）
# ---------------------------------------------------------------------------
def _image_filter():
    return tr('图片 (*.png *.jpg *.jpeg *.webp *.bmp)')


def _danger_rgb():
    return QColor(materials.ECOMMERCE['danger']).getRgb()[:3]


def _invalid_border():
    return materials.rgba(_danger_rgb(), 90)


def _invalid_background():
    return materials.rgba(_danger_rgb(), 16)


def _badge_style(color):
    rgb = QColor(color).getRgb()[:3]
    return ('background: %s; color: %s; border-radius: %dpx;'
            ' padding: %dpx %dpx; font-size: %dpx; font-weight: 600;'
            % (materials.rgba(rgb, 38), color, px(9), px(1), px(8), px(11)))


def _clipboard_image(clipboard):
    """从剪贴板取 QImage；任何异常都退化为空图（不阻塞粘贴）。"""
    try:
        image = clipboard.image() if clipboard is not None else QImage()
    except Exception:  # noqa: BLE001 - 剪贴板实现因平台而异
        return QImage()
    return image if isinstance(image, QImage) else QImage()


def _mime_image_paths(mime):
    """从拖拽 mime 数据里挑出本地图片文件路径（统一为原生路径分隔符）。"""
    paths = []
    try:
        if mime is None or not mime.hasUrls():
            return paths
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            raw = url.toLocalFile()
            if not raw or not is_image_file(raw):
                continue
            try:
                path = str(Path(raw))
            except (OSError, ValueError):
                path = raw
            if path not in paths:
                paths.append(path)
    except (RuntimeError, TypeError, ValueError):
        return paths
    return paths


def _clamp_int(value, low, high, fallback=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = low if fallback is None else int(fallback)
    return max(low, min(high, number))


def _as_str(value):
    return '' if value is None else str(value).strip()


def _string_list(value, limit):
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for item in value:
        text = _as_str(item)
        if text and text not in result:
            result.append(text)
    return result[:max(0, int(limit))]


def _resolved(path):
    """展示 / 提交统一用绝对路径；解析失败保留原值（草稿可含已移动文件）。"""
    text = _as_str(path)
    if not text:
        return ''
    try:
        return str(Path(text).expanduser().resolve())
    except (OSError, ValueError):
        return text


# ---------------------------------------------------------------------------
# 可粘贴的输入控件
# ---------------------------------------------------------------------------
class _PasteLineEdit(LineEdit):
    """名称输入框：Ctrl+V 时先让页面尝试粘贴图片，粘贴到图片则消费按键。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.paste_handler = None

    def keyPressEvent(self, event):
        if self._is_paste(event) and self._run_paste():
            event.accept()
            return
        super().keyPressEvent(event)

    @staticmethod
    def _is_paste(event):
        return (event.key() == Qt.Key_V
                and bool(event.modifiers() & Qt.ControlModifier))

    def _run_paste(self):
        handler = self.paste_handler
        if handler is None:
            return False
        try:
            return bool(handler())
        except Exception:  # noqa: BLE001 - 粘贴失败不应影响输入框
            return False


class _PastePlainTextEdit(PlainTextEdit):
    """产品信息输入框：与名称框相同的 Ctrl+V 图片粘贴行为。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.paste_handler = None

    def keyPressEvent(self, event):
        if (event.key() == Qt.Key_V
                and bool(event.modifiers() & Qt.ControlModifier)):
            handler = self.paste_handler
            if handler is not None:
                try:
                    if handler():
                        event.accept()
                        return
                except Exception:  # noqa: BLE001
                    pass
        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# 比例 chip
# ---------------------------------------------------------------------------
class RatioChipGroup(QWidget):
    """9 个比例 chip（WrapLayout 换行），单选，选中态用品牌蓝。"""

    changed = pyqtSignal(str)

    def __init__(self, ratios=None, parent=None):
        super().__init__(parent)
        self._ratios = list(ratios or RATIOS)
        self._buttons = {}
        self._current = self._ratios[0] if self._ratios else ''
        layout = WrapLayout(spacing=px(6))
        self._layout = layout
        self.setLayout(layout)
        for ratio in self._ratios:
            button = QPushButton(ratio, self)
            button.setObjectName('batchRatioChip')
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setFixedHeight(px(24))
            button.setMinimumWidth(px(46))
            button.clicked.connect(
                lambda _checked=False, value=ratio: self.select(value))
            layout.addWidget(button)
            self._buttons[ratio] = button
        self.select(self._current, emit=False)

    # —— 公共 API ——
    def ratios(self):
        return list(self._ratios)

    def current_ratio(self):
        return self._current

    def chip_for(self, ratio):
        return self._buttons.get(str(ratio or ''))

    def buttons(self):
        return dict(self._buttons)

    def select(self, ratio, emit=True):
        ratio = str(ratio or '')
        if ratio not in self._buttons:
            return False
        changed = ratio != self._current
        self._current = ratio
        for key, button in self._buttons.items():
            blocker = QSignalBlocker(button)
            button.setChecked(key == ratio)
            del blocker
        self.refresh_colors()
        if emit and changed:
            self.changed.emit(ratio)
        return True

    def set_chips_enabled(self, enabled):
        for button in self._buttons.values():
            button.setEnabled(bool(enabled))

    def apply_scale(self):
        for button in self._buttons.values():
            button.setFixedHeight(px(24))
            button.setMinimumWidth(px(46))
        self._layout.setSpacing(px(6))
        self.refresh_colors()

    def refresh_colors(self):
        colors = materials.palette()
        accent = materials.ECOMMERCE['brand_blue']
        white = materials.ECOMMERCE['white']
        border = materials.rgba(colors['border'])
        text = colors['text2']
        for ratio, button in self._buttons.items():
            if ratio == self._current:
                button.setStyleSheet(
                    'QPushButton#batchRatioChip {background: %s; color: %s;'
                    ' border: 1px solid %s; border-radius: %dpx;'
                    ' font-size: %dpx; font-weight: 600; padding: 0 %dpx;}'
                    % (accent, white, accent, px(6), px(11), px(4)))
            else:
                button.setStyleSheet(
                    'QPushButton#batchRatioChip {background: transparent;'
                    ' color: %s; border: 1px solid %s; border-radius: %dpx;'
                    ' font-size: %dpx; padding: 0 %dpx;}'
                    'QPushButton#batchRatioChip:hover {color: %s;'
                    ' border-color: %s;}'
                    'QPushButton#batchRatioChip:disabled {color: %s;'
                    ' border-color: %s;}'
                    % (text, border, px(6), px(11), px(4), accent, accent,
                       materials.palette()['text3'],
                       materials.rgba(colors['border'])))
        self.updateGeometry()


# ---------------------------------------------------------------------------
# 行内图片格
# ---------------------------------------------------------------------------
class _ThumbWidget(QWidget):
    """首图缩略图 / 虚线「+」格：点击请求选图，接收拖拽。"""

    clicked = pyqtSignal()
    files_dropped = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = QPixmap()
        self._hover = False
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tr('添加产品图'))
        self.apply_scale()

    def apply_scale(self):
        size = px(84)
        self.setFixedSize(size, size)

    def set_path(self, path):
        image = QPixmap()
        try:
            if path and Path(str(path)).is_file():
                image.loadFromData(Path(str(path)).read_bytes())
        except (OSError, ValueError):
            image = QPixmap()
        self._pixmap = image
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        colors = materials.palette()
        radius = px(8)
        rect = self.rect().adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(float(rect.x()), float(rect.y()),
                            float(rect.width()), float(rect.height()),
                            radius, radius)
        if self._pixmap.isNull():
            painter.fillPath(path, QColor(colors['bg2']))
            painter.setClipping(False)
            pen = QPen(QColor(materials.ECOMMERCE['brand_blue']) if self._hover
                       else QColor(*colors['border']))
            pen.setWidthF(1.5)
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect, radius, radius)
            font = painter.font()
            font.setPixelSize(px(22))
            painter.setFont(font)
            painter.setPen(QColor(materials.ECOMMERCE['brand_blue'])
                           if self._hover else QColor(colors['text3']))
            painter.drawText(self.rect(), Qt.AlignCenter, '+')
        else:
            painter.setClipPath(path)
            scaled = self._pixmap.scaled(
                self.size(), Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation)
            left = max(0, (scaled.width() - self.width()) // 2)
            top = max(0, (scaled.height() - self.height()) // 2)
            painter.drawPixmap(0, 0, scaled, left, top,
                               self.width(), self.height())
            painter.setClipping(False)
            painter.setPen(QPen(QColor(materials.ECOMMERCE['brand_blue'])
                                if self._hover else QColor(*colors['border']),
                                1.5))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect, radius, radius)
        painter.end()
        super().paintEvent(event)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def dragEnterEvent(self, event):
        if _mime_image_paths(event.mimeData()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event):
        paths = _mime_image_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        self.files_dropped.emit(paths)
        event.acceptProposedAction()


class RowImageCell(QWidget):
    """行内图片格：首图 / 「+」+ 产品图计数 + 画廊选图 / 已用图片 / 输入。"""

    browse_requested = pyqtSignal()
    gallery_requested = pyqtSignal()
    used_requested = pyqtSignal()
    url_requested = pyqtSignal()
    files_dropped = pyqtSignal(object)
    paste_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('batchRowImages')
        self._paths = []
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumWidth(px(140))
        self.setMaximumWidth(px(280))
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

        self.thumb = _ThumbWidget(self)
        self.thumb.clicked.connect(self._on_thumb_clicked)
        self.thumb.files_dropped.connect(self.files_dropped.emit)
        self.thumb.installEventFilter(self)
        self.count_label = CaptionLabel(
            tr('产品图 {count}/6').format(count=0), self)
        self.count_label.setWordWrap(True)

        links = WrapLayout(spacing=px(4))
        self._links = links
        self.gallery_button = self._link_button(tr('画廊选图'))
        self.used_button = self._link_button(tr('已用图片'))
        self.url_button = self._link_button(tr('输入'))
        self.gallery_button.clicked.connect(
            lambda _checked=False: self.gallery_requested.emit())
        self.used_button.clicked.connect(
            lambda _checked=False: self.used_requested.emit())
        self.url_button.clicked.connect(
            lambda _checked=False: self.url_requested.emit())
        for button in (self.gallery_button, self.used_button, self.url_button):
            links.addWidget(button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(4))
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(0)
        top.addWidget(self.thumb)
        top.addStretch(1)
        layout.addLayout(top)
        layout.addWidget(self.count_label)
        layout.addLayout(links)
        layout.addStretch(1)
        self.refresh_colors()

    # —— 公共 API ——
    def sizeHint(self):
        hint = super().sizeHint()
        return QSize(px(220), max(px(120), hint.height()))

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        return QSize(px(140), max(px(120), hint.height()))

    def paths(self):
        return list(self._paths)

    def count(self):
        return len(self._paths)

    def set_paths(self, paths):
        cleaned = [str(path) for path in (paths or []) if _as_str(path)]
        self._paths = list(dict.fromkeys(cleaned))[:MAX_PRODUCT_IMAGES]
        self.thumb.set_path(self._paths[0] if self._paths else '')
        self.count_label.setText(
            tr('产品图 {count}/6').format(count=len(self._paths)))
        self.updateGeometry()

    def add_paths(self, paths):
        merged = list(dict.fromkeys(self._paths + [
            str(path) for path in (paths or []) if _as_str(path)]))
        added = [path for path in merged if path not in self._paths]
        self.set_paths(merged[:MAX_PRODUCT_IMAGES])
        return added

    def apply_scale(self):
        self.thumb.apply_scale()
        self.setMinimumWidth(px(140))
        self.setMaximumWidth(px(280))
        self._links.setSpacing(px(4))
        self.refresh_colors()

    def refresh_colors(self):
        colors = materials.palette()
        accent = materials.ECOMMERCE['brand_blue']
        for button in (self.gallery_button, self.used_button, self.url_button):
            button.setStyleSheet(
                'QPushButton#batchLinkButton {background: transparent;'
                ' border: 0; color: %s; font-size: %dpx; padding: 0;'
                ' text-align: left;}'
                'QPushButton#batchLinkButton:hover {text-decoration: underline;}'
                % (accent, px(12)))
        self.count_label.setStyleSheet('color: %s;' % colors['text3'])
        self.thumb.update()

    def dragEnterEvent(self, event):
        if _mime_image_paths(event.mimeData()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event):
        paths = _mime_image_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        self.files_dropped.emit(paths)
        event.acceptProposedAction()

    def keyPressEvent(self, event):
        if (event.key() == Qt.Key_V
                and bool(event.modifiers() & Qt.ControlModifier)):
            self.paste_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def eventFilter(self, watched, event):
        if (watched is self.thumb and event.type() == QEvent.KeyPress
                and event.key() == Qt.Key_V
                and bool(event.modifiers() & Qt.ControlModifier)):
            self.paste_requested.emit()
            event.accept()
            return True
        return super().eventFilter(watched, event)

    # —— 内部 ——
    def _link_button(self, text):
        button = QPushButton(text, self)
        button.setObjectName('batchLinkButton')
        button.setFlat(True)
        button.setCursor(Qt.PointingHandCursor)
        button.setStyleSheet('border: 0; background: transparent;')
        return button

    def _on_thumb_clicked(self):
        self.setFocus(Qt.MouseFocusReason)
        self.browse_requested.emit()


# ---------------------------------------------------------------------------
# 行
# ---------------------------------------------------------------------------
class EcommerceBatchRow(QFrame):
    """一行商品：选择/序号/复制/删除 · 图片格 · 名称+信息 · 状态徽章。"""

    changed = pyqtSignal()
    selection_changed = pyqtSignal()
    copy_requested = pyqtSignal(object)
    delete_requested = pyqtSignal(object)
    browse_requested = pyqtSignal(object)
    gallery_requested = pyqtSignal(object)
    used_requested = pyqtSignal(object)
    url_requested = pyqtSignal(object)
    files_dropped = pyqtSignal(object, object)
    paste_requested = pyqtSignal(object)

    def __init__(self, page, *, image_paths=None, product_name='',
                 product_info='', parent=None):
        super().__init__(page if parent is None else parent)
        self.page = page
        self.setObjectName('ecommerceBatchRow')
        self.setFrameShape(QFrame.NoFrame)
        self._style_key = ''
        self._badge_key = ''

        self.checkbox = CheckBox(self)
        self.checkbox.setObjectName('batchRowSelect')
        self.checkbox.setToolTip(tr('选择此行'))
        self.index_label = CaptionLabel('#1', self)
        self.index_label.setAlignment(Qt.AlignCenter)
        self.copy_button = TransparentToolButton(FIF.COPY, self)
        self.copy_button.setObjectName('batchRowCopy')
        self.copy_button.setToolTip(tr('复制行'))
        self.copy_button.clicked.connect(
            lambda: self.copy_requested.emit(self))
        self.delete_button = TransparentToolButton(FIF.DELETE, self)
        self.delete_button.setObjectName('batchRowDelete')
        self.delete_button.setToolTip(tr('删除行'))
        self.delete_button.clicked.connect(
            lambda: self.delete_requested.emit(self))

        controls = QVBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(px(6))
        controls.addWidget(self.checkbox, 0, Qt.AlignHCenter)
        controls.addWidget(self.index_label, 0, Qt.AlignHCenter)
        controls.addWidget(self.copy_button, 0, Qt.AlignHCenter)
        controls.addWidget(self.delete_button, 0, Qt.AlignHCenter)
        controls.addStretch(1)

        self.image_cell = RowImageCell(self)
        self.image_cell.browse_requested.connect(
            lambda: self.browse_requested.emit(self))
        self.image_cell.gallery_requested.connect(
            lambda: self.gallery_requested.emit(self))
        self.image_cell.used_requested.connect(
            lambda: self.used_requested.emit(self))
        self.image_cell.url_requested.connect(
            lambda: self.url_requested.emit(self))
        self.image_cell.files_dropped.connect(
            lambda paths: self.files_dropped.emit(self, paths))
        self.image_cell.paste_requested.connect(
            lambda: self.paste_requested.emit(self))

        self.name_edit = _PasteLineEdit(self)
        self.name_edit.setObjectName('batchRowName')
        self.name_edit.setPlaceholderText(
            tr('产品名称（建议填写，用作任务标签）'))
        self.info_edit = _PastePlainTextEdit(self)
        self.info_edit.setObjectName('batchRowInfo')
        self.info_edit.setPlaceholderText(tr('产品信息（必填）'))
        self.name_edit.textChanged.connect(self.changed.emit)
        self.info_edit.textChanged.connect(self.changed.emit)
        self.name_edit.paste_handler = self._paste_images
        self.info_edit.paste_handler = self._paste_images

        fields = QVBoxLayout()
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(px(8))
        fields.addWidget(self.name_edit)
        fields.addWidget(self.info_edit)
        fields.addStretch(1)

        self.status_badge = CaptionLabel(tr('待完善'), self)
        self.status_badge.setObjectName('batchRowStatus')
        self.status_badge.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        status_layout = QVBoxLayout()
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(px(4))
        status_layout.addWidget(self.status_badge, 0, Qt.AlignLeft)
        status_layout.addStretch(1)

        self._grid = QGridLayout(self)
        self._grid.addLayout(controls, 0, 0)
        self._grid.addWidget(self.image_cell, 0, 1)
        self._grid.addLayout(fields, 0, 2)
        self._grid.addLayout(status_layout, 0, 3)
        self._grid.setColumnMinimumWidth(0, px(56))
        self._grid.setColumnMinimumWidth(3, px(140))
        self._grid.setColumnStretch(2, 1)

        self.checkbox.toggled.connect(
            lambda _checked: self.selection_changed.emit())

        self.image_cell.set_paths(image_paths or [])
        if product_name:
            self.name_edit.setText(str(product_name))
        if product_info:
            self.info_edit.setPlainText(str(product_info))
        style_controls(self)
        self.apply_scale()
        self.refresh_state()

    # —— 公共 API ——
    @property
    def product_image_paths(self):
        return self.image_cell.paths()

    @property
    def product_name(self):
        """产品名称文本（与参考软件行模型一致；输入框本身见 name_edit）。"""
        return self.name_text()

    @property
    def product_info(self):
        """产品信息文本（输入框本身见 info_edit）。"""
        return self.info_text()

    @property
    def row_index(self):
        """1 起始的行号（与界面「#n」一致）。"""
        text = self.index_label.text().lstrip('#')
        try:
            return int(text)
        except (TypeError, ValueError):
            return 0

    @property
    def ready(self):
        return bool(self.page.row_ready(self))

    @property
    def invalid(self):
        return not self.ready

    @property
    def selected(self):
        return bool(self.checkbox.isChecked())

    def set_selected(self, selected):
        blocker = QSignalBlocker(self.checkbox)
        self.checkbox.setChecked(bool(selected))
        del blocker
        self.selection_changed.emit()

    def set_index(self, index):
        self.index_label.setText('#%d' % max(1, int(index)))

    def set_images(self, paths):
        self.image_cell.set_paths(paths)
        self.changed.emit()

    def add_images(self, paths):
        added = self.image_cell.add_paths(paths)
        if added:
            self.changed.emit()
        return added

    def name_text(self):
        return self.name_edit.text().strip()

    def info_text(self):
        return self.info_edit.toPlainText().strip()

    def set_name(self, text):
        self.name_edit.setText('' if text is None else str(text))

    def set_info(self, text):
        self.info_edit.setPlainText('' if text is None else str(text))

    def status_text(self):
        return tr('就绪') if self.ready else tr('待完善')

    def apply_scale(self):
        self._grid.setContentsMargins(px(12), px(12), px(12), px(12))
        self._grid.setHorizontalSpacing(px(12))
        self._grid.setVerticalSpacing(px(8))
        self._grid.setColumnMinimumWidth(0, px(56))
        self._grid.setColumnMinimumWidth(3, px(140))
        for button in (self.copy_button, self.delete_button):
            button.setFixedSize(px(28), px(28))
        self.name_edit.setFixedHeight(px(34))
        self.info_edit.setFixedHeight(px(68))
        self.image_cell.apply_scale()
        self.refresh_state()

    def refresh_state(self):
        ready = self.ready
        self.status_badge.setText(tr('就绪') if ready else tr('待完善'))
        self._apply_badge(ready)
        self._apply_row_style(ready)
        self.status_badge.setToolTip('' if ready else self.failure_reason)

    def refresh_colors(self):
        colors = materials.palette()
        self.index_label.setStyleSheet(
            'color: %s; font-size: %dpx;' % (colors['text2'], px(11)))
        self.image_cell.refresh_colors()
        self.refresh_state()

    def retranslate_ui(self):
        self.checkbox.setToolTip(tr('选择此行'))
        self.copy_button.setToolTip(tr('复制行'))
        self.delete_button.setToolTip(tr('删除行'))
        self.name_edit.setPlaceholderText(
            tr('产品名称（建议填写，用作任务标签）'))
        self.info_edit.setPlaceholderText(tr('产品信息（必填）'))
        self.image_cell.thumb.setToolTip(tr('添加产品图'))
        self.image_cell.count_label.setText(
            tr('产品图 {count}/6').format(
                count=len(self.product_image_paths)))
        self.image_cell.gallery_button.setText(tr('画廊选图'))
        self.image_cell.used_button.setText(tr('已用图片'))
        self.image_cell.url_button.setText(tr('输入'))
        self.refresh_state()

    def validation_failure(self):
        return self.page.validate_row(self)

    @property
    def failure_key(self):
        return self.validation_failure()[0]

    @property
    def failure_reason(self):
        return self.validation_failure()[1]

    # —— 内部 ——
    def _paste_images(self):
        return bool(self.page.paste_into_row(self))

    def _apply_badge(self, ready):
        color = (materials.ECOMMERCE['success'] if ready
                 else materials.ECOMMERCE['warning'])
        style = _badge_style(color)
        if style != self._badge_key:
            self._badge_key = style
            self.status_badge.setStyleSheet(style)

    def _apply_row_style(self, ready):
        colors = materials.palette()
        if ready:
            background = materials.rgba(colors['surface'])
            border = materials.rgba(colors['border'])
        else:
            background = _invalid_background()
            border = _invalid_border()
        style = ('QFrame#ecommerceBatchRow {background: %s;'
                 ' border: 1px solid %s; border-radius: %dpx;}'
                 % (background, border, px(10)))
        if style != self._style_key:
            self._style_key = style
            self.setStyleSheet(style)
        self.status_badge.setToolTip('' if ready else self.failure_reason)


# ---------------------------------------------------------------------------
# 批量制作页
# ---------------------------------------------------------------------------
class EcommerceBatchPage(QWidget):
    """电商图批量制作页（宿主页负责把信号接到 worker / 视图切换）。"""

    submit_requested = pyqtSignal(object)
    history_requested = pyqtSignal()
    single_requested = pyqtSignal()

    def __init__(self, config_manager, parent=None):
        super().__init__(parent)
        self.setObjectName('ecommerceBatchPage')
        self.config_manager = config_manager
        self.rows = []
        self._loading = True
        self._panels = []
        self._platform_selection_order = []
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.setInterval(4000)
        self._notice_timer.timeout.connect(self._clear_notice)
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(DRAFT_DEBOUNCE_MS)
        self._draft_timer.timeout.connect(self.save_draft)
        self.last_notice = ''
        self.draft_save_error = ''
        self.draft_load_error = ''
        defaults = config_manager.config.get('ecommerce', {}) or {}
        self.thinking_enabled = bool(defaults.get('thinking', False))
        self._build_ui(defaults)
        materials.register_theme_callback(self.refresh_colors)
        style_controls(self)
        self._refresh_colors()
        self._update_input_states()
        self._apply_metrics()
        self._loading = False
        self.load_draft()
        if not self.rows:
            self.add_row()
        self._refresh_all_rows()
        self._refresh_summary()
        self._schedule_draft_save()

    # ------------------------------------------------------------------
    # 构建界面
    # ------------------------------------------------------------------
    def _build_ui(self, defaults):
        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        self.header = QWidget(self)
        self.header.setObjectName('batchPageHeader')
        self._header_layout = QHBoxLayout(self.header)
        self._header_layout.setContentsMargins(px(24), px(14), px(24), px(6))
        self._header_layout.setSpacing(px(12))
        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(px(2))
        self.title_label = TitleLabel(tr('电商图批量制作'), self.header)
        self.title_label.setWordWrap(True)
        self.subtitle_label = CaptionLabel(
            tr('每行一个商品，提交后在历史任务中各自显示为独立任务组'),
            self.header)
        self.subtitle_label.setWordWrap(True)
        title_box.addWidget(self.title_label)
        title_box.addWidget(self.subtitle_label)
        self._header_layout.addLayout(title_box, 1)
        self.history_button = PushButton(tr('历史任务'), self.header)
        self.history_button.setObjectName('batchHistoryButton')
        self.single_button = PushButton(tr('单个制作'), self.header)
        self.single_button.setObjectName('batchSingleButton')
        self.history_button.clicked.connect(
            lambda _checked=False: self.history_requested.emit())
        self.single_button.clicked.connect(
            lambda _checked=False: self.single_requested.emit())
        self._header_layout.addWidget(self.history_button, 0, Qt.AlignTop)
        self._header_layout.addWidget(self.single_button, 0, Qt.AlignTop)
        shell.addWidget(self.header)

        self.body_scroll = QScrollArea(self)
        self.body_scroll.setObjectName('batchBodyScroll')
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setFrameShape(QScrollArea.NoFrame)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.body_content = QWidget()
        self.body_content.setObjectName('batchBodyContent')
        self.body_content.setAutoFillBackground(False)
        self.body_scroll.setWidget(self.body_content)
        self._body_layout = QVBoxLayout(self.body_content)
        self._body_layout.setContentsMargins(px(24), px(8), px(24), px(8))
        self._body_layout.setSpacing(px(14))
        self.body_scroll.viewport().setAutoFillBackground(False)
        self.body_scroll.viewport().installEventFilter(self)

        self._build_shared_panel(defaults)
        self._build_table_panel()
        self._body_layout.addStretch(1)
        shell.addWidget(self.body_scroll, 1)

        self.footer = QWidget(self)
        self.footer.setObjectName('batchPageFooter')
        self._footer_layout = QHBoxLayout(self.footer)
        self._footer_layout.setContentsMargins(px(24), px(8), px(24), px(14))
        self._footer_layout.setSpacing(px(12))
        self.footer_hint = CaptionLabel(
            tr('把图片拖进图片格，或在图片格 / 本行名称与信息框里按 Ctrl+V 粘贴截图'),
            self.footer)
        self.footer_hint.setWordWrap(True)
        self.submit_button = PrimaryPushButton(tr('开始批量制作（0）'), self.footer)
        self.submit_button.setObjectName('batchSubmitButton')
        self.submit_button.setEnabled(False)
        self.submit_button.setToolTip(tr('没有可提交的行'))
        self.submit_button.clicked.connect(self.submit)
        self._footer_layout.addWidget(self.footer_hint, 1)
        self._footer_layout.addWidget(self.submit_button, 0, Qt.AlignVCenter)
        shell.addWidget(self.footer)

        self.setStyleSheet(
            'QWidget#ecommerceBatchPage {background: transparent;}'
            'QWidget#batchBodyContent, QWidget#batchPageFooter,'
            ' QWidget#batchPageHeader {background: transparent;}'
            'QScrollArea#batchBodyScroll {background: transparent; border: 0; }')

    def _make_panel(self):
        panel = QFrame(self.body_content)
        panel.setObjectName('ecommerceBatchPanel')
        panel.setFrameShape(QFrame.NoFrame)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(px(16), px(16), px(16), px(16))
        layout.setSpacing(px(12))
        self._panels.append(panel)
        return panel, layout

    def _required_caption(self, caption, parent):
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(px(2))
        label = CaptionLabel(str(caption), parent)
        label.setWordWrap(True)
        star = QLabel('*', parent)
        star.setStyleSheet('color: %s;' % materials.ERROR)
        row.addWidget(label)
        row.addWidget(star)
        row.addStretch(1)
        return row, label

    def _field(self, caption, widget, parent):
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(4))
        label = CaptionLabel(str(caption), parent)
        label.setWordWrap(True)
        layout.addWidget(label)
        layout.addWidget(widget)
        return layout, label

    def _build_shared_panel(self, defaults):
        panel, layout = self._make_panel()
        self.shared_layout = layout
        self.shared_title = StrongBodyLabel(
            tr('共享默认（新建行继承，提交时使用）'), panel)
        layout.addWidget(self.shared_title)

        # —— 电商平台（25 个，国内/海外两组，各两列） ——
        platform_caption_row, self.platform_caption = self._required_caption(
            tr('电商平台'), panel)
        layout.addLayout(platform_caption_row)
        hint = CaptionLabel(
            tr('建议只选一个平台，多平台可能存在规则冲突（最多5个）'), panel)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.platform_hint_label = hint
        self.platform_selector = PlatformMultiSelect(panel)
        self.platform_selector.changed.connect(self._platform_changed)
        self.platform_selector.limit_reached.connect(
            lambda count: self._notice(tr('最多选择 {count} 个平台').format(count=count), warning=True))
        layout.addWidget(self.platform_selector)

        # —— 图片文字语言 ——
        self.language_combo = ComboBox(panel)
        self.language_combo.setObjectName('batchLanguage')
        for key, value in LANGUAGE_TEXT.items():
            self.language_combo.addItem(tr(value), userData=key)
        self._select_combo_data(self.language_combo,
                                defaults.get('language', 'zh') or 'zh')
        language_wrap = QVBoxLayout()
        self.language_combo.setMinimumWidth(px(220))
        self.language_combo.setFixedHeight(px(40))
        language_field, self.language_caption = self._field(
            tr('图片文字语言'), self.language_combo, panel)
        language_wrap.addLayout(language_field)
        layout.addLayout(language_wrap)

        # —— 轮播图：启用 + 张数 + 9 比例 chip ——
        self.carousel_check = CheckBox(tr('轮播图'), panel)
        self.carousel_check.setObjectName('batchCarouselEnable')
        self.carousel_check.setChecked(True)
        self.carousel_spin = SpinBox(panel)
        self.carousel_spin.setObjectName('batchCarouselCount')
        self.carousel_spin.setRange(1, 6)
        self.carousel_spin.setValue(_clamp_int(
            defaults.get('default_carousel_count', 3), 1, 6, 3))
        self.carousel_spin.setFixedWidth(px(110))
        self.carousel_unit_label = CaptionLabel(tr('张'), panel)
        carousel_row = QHBoxLayout()
        carousel_row.setContentsMargins(0, 0, 0, 0)
        carousel_row.setSpacing(px(8))
        carousel_row.addWidget(self.carousel_check)
        carousel_row.addWidget(self.carousel_spin)
        carousel_row.addWidget(self.carousel_unit_label)
        carousel_row.addStretch(1)
        layout.addLayout(carousel_row)
        self.carousel_ratios = RatioChipGroup(parent=panel)
        self._select_ratio(self.carousel_ratios,
                           defaults.get('default_carousel_ratio'))
        self.carousel_ratios.changed.connect(
            lambda _ratio: self._shared_changed())
        layout.addWidget(self.carousel_ratios)
        self.carousel_check.toggled.connect(
            lambda _checked: self._shared_changed())
        self.carousel_spin.valueChanged.connect(
            lambda _value: self._shared_changed())

        # —— 详情图：启用 + 张数 + 独立一行 9 比例 chip ——
        self.detail_check = CheckBox(tr('详情图'), panel)
        self.detail_check.setObjectName('batchDetailEnable')
        self.detail_check.setChecked(True)
        self.detail_spin = SpinBox(panel)
        self.detail_spin.setObjectName('batchDetailCount')
        self.detail_spin.setRange(1, 15)
        self.detail_spin.setValue(_clamp_int(
            defaults.get('default_detail_count', 4), 1, 15, 4))
        self.detail_spin.setFixedWidth(px(110))
        self.detail_unit_label = CaptionLabel(tr('张'), panel)
        detail_row = QHBoxLayout()
        detail_row.setContentsMargins(0, 0, 0, 0)
        detail_row.setSpacing(px(8))
        detail_row.addWidget(self.detail_check)
        detail_row.addWidget(self.detail_spin)
        detail_row.addWidget(self.detail_unit_label)
        detail_row.addStretch(1)
        layout.addLayout(detail_row)
        self.detail_ratios = RatioChipGroup(parent=panel)
        self._select_ratio(self.detail_ratios,
                           defaults.get('default_detail_ratio'))
        self.detail_ratios.changed.connect(
            lambda _ratio: self._shared_changed())
        layout.addWidget(self.detail_ratios)
        self.detail_check.toggled.connect(
            lambda _checked: self._shared_changed())
        self.detail_spin.valueChanged.connect(
            lambda _value: self._shared_changed())

        # —— AI 分析模型 / 图片生成模型 / 分辨率 ——
        self.analysis_model_picker = ModelPickComboBox(panel)
        self.analysis_model_picker.setObjectName('batchAnalysisModel')
        self.analysis_model_picker.set_models(
            catalog_snapshot(self.config_manager, 'chat'),
            defaults.get('analysis_model', DEFAULT_ANALYSIS_MODEL),
            kind='chat')
        self.image_model_picker = ModelPickComboBox(panel)
        self.image_model_picker.setObjectName('batchImageModel')
        self.image_model_picker.set_models(
            catalog_snapshot(self.config_manager, 'image'),
            defaults.get('image_model', DEFAULT_IMAGE_MODEL),
            kind='image')
        self.tier_combo = ComboBox(panel)
        self.tier_combo.setObjectName('batchTier')
        self.tier_combo.addItems(list(TIER_SHORT_EDGE))
        tier = defaults.get('default_tier', FALLBACK_TIER)
        if tier not in TIER_SHORT_EDGE:
            tier = FALLBACK_TIER
        self.tier_combo.setCurrentText(tier)
        models_wrap = QGridLayout()
        models_wrap.setHorizontalSpacing(px(16))
        self.models_grid = models_wrap
        self._model_fields = []
        self._model_columns = 3
        analysis_field, self.analysis_caption = self._field(
            tr('AI 分析模型'), self.analysis_model_picker, panel)
        image_field, self.image_caption = self._field(
            tr('图片生成模型'), self.image_model_picker, panel)
        tier_field, self.tier_caption = self._field(
            tr('分辨率'), self.tier_combo, panel)
        for column, (field, control) in enumerate(((analysis_field, self.analysis_model_picker),
                                                   (image_field, self.image_model_picker),
                                                   (tier_field, self.tier_combo))):
            control.setMinimumWidth(px(220))
            control.setFixedHeight(px(40))
            control.setMaximumWidth(16777215)
            control.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            control.setToolTip(control.currentText())
            control.currentTextChanged.connect(control.setToolTip)
            models_wrap.setColumnMinimumWidth(column, px(220))
            models_wrap.setColumnStretch(column, 1)
            models_wrap.addLayout(field, 0, column)
            self._model_fields.append(field)
        layout.addLayout(models_wrap)
        self.analysis_model_picker.currentTextChanged.connect(
            lambda _text: self._persist_model(
                'analysis_model', self.analysis_model_picker.value()))
        self.analysis_model_picker.textEdited.connect(
            lambda _text: self._persist_model(
                'analysis_model', self.analysis_model_picker.value()))
        self.image_model_picker.currentTextChanged.connect(
            lambda _text: self._persist_model(
                'image_model', self.image_model_picker.value()))
        self.image_model_picker.textEdited.connect(
            lambda _text: self._persist_model(
                'image_model', self.image_model_picker.value()))
        self.tier_combo.currentTextChanged.connect(
            lambda _text: self._shared_changed())

        # —— 风格参考图（可选，整批共用，最多 3 张） ——
        style_header = QHBoxLayout()
        style_header.setContentsMargins(0, 0, 0, 0)
        style_header.setSpacing(px(8))
        self.style_label = StrongBodyLabel(
            tr('风格参考图（可选，最多3张，整批共用）'), panel)
        style_header.addWidget(self.style_label)
        style_header.addStretch(1)
        self.style_used_button = PushButton(tr('已用图片'), panel)
        self.style_used_button.setObjectName('batchStyleUsedButton')
        self.style_gallery_button = PushButton(tr('画廊选图'), panel)
        self.style_gallery_button.setObjectName('batchStyleGalleryButton')
        for button in (self.style_used_button, self.style_gallery_button):
            button.setFixedHeight(px(26))
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(
                'QPushButton {background: %s; color: %s; border: 0;'
                ' border-radius: %dpx; padding: 1px %dpx; font-size: %dpx;}'
                % (materials.ECOMMERCE['brand_blue'],
                   materials.ECOMMERCE['white'], px(6), px(8), px(12)))
        self.style_used_button.clicked.connect(self.pick_style_used)
        self.style_gallery_button.clicked.connect(self.pick_style_gallery)
        style_header.addWidget(self.style_used_button)
        style_header.addWidget(self.style_gallery_button)
        layout.addLayout(style_header)
        self.style_grid = ImageUploadGrid('style', 3, panel, columns=4, uniform=True,
                                          add_label_provider=lambda *_: tr('上传参考图'))
        self.style_grid.setObjectName('batchStyleGrid')
        self.style_grid.changed.connect(self._on_style_changed)
        self.style_grid.add_requested.connect(self.pick_style_file)
        self.style_grid.preview_requested.connect(self._preview_style_image)
        layout.addWidget(self.style_grid)

        self._body_layout.addWidget(panel)

    def _build_table_panel(self):
        panel, layout = self._make_panel()
        self.table_layout = layout
        self.toolbar = WrapLayout(spacing=px(8))
        self.add_one_button = PushButton(tr('新增 1 行'), panel)
        self.add_one_button.setObjectName('batchAddOne')
        self.add_five_button = PushButton(tr('新增 5 行'), panel)
        self.add_five_button.setObjectName('batchAddFive')
        self.select_all_button = PushButton(tr('全选'), panel)
        self.select_all_button.setObjectName('batchSelectAll')
        self.copy_button = PushButton(tr('复制选中（0）'), panel)
        self.copy_button.setObjectName('batchCopySelected')
        self.copy_button.setEnabled(False)
        self.delete_button = PushButton(tr('删除选中'), panel)
        self.delete_button.setObjectName('batchDeleteSelected')
        self.delete_button.setEnabled(False)
        self.clear_button = PushButton(tr('清空'), panel)
        self.clear_button.setObjectName('batchClearRows')
        self.stats_label = CaptionLabel(
            tr('共 {total} 行 · 就绪 {ready}').format(total=0, ready=0), panel)
        self.stats_label.setObjectName('batchStats')
        self.add_one_button.clicked.connect(lambda: self.add_rows(1))
        self.add_five_button.clicked.connect(lambda: self.add_rows(5))
        self.select_all_button.clicked.connect(self.toggle_select_all)
        self.copy_button.clicked.connect(self.copy_selected)
        self.delete_button.clicked.connect(self.delete_selected)
        self.clear_button.clicked.connect(self.clear_rows)
        for widget in (self.add_one_button, self.add_five_button,
                       self.select_all_button, self.copy_button,
                       self.delete_button, self.clear_button,
                       self.stats_label):
            self.toolbar.addWidget(widget)
        self.toolbar.addStretch(1)
        self.toolbar.addWidget(self.stats_label)
        layout.addLayout(self.toolbar)

        self.notice_label = CaptionLabel('', panel)
        self.notice_label.setObjectName('batchNotice')
        self.notice_label.setWordWrap(True)
        self.notice_label.setVisible(False)
        layout.addWidget(self.notice_label)

        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(px(10))
        layout.addLayout(self._rows_layout)
        self._body_layout.addWidget(panel)

    # ------------------------------------------------------------------
    # 共享默认读写
    # ------------------------------------------------------------------
    @staticmethod
    def _select_combo_data(combo, value):
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return True
        return False

    @staticmethod
    def _select_ratio(chips, ratio):
        ratio = str(ratio or '')
        if ratio in RATIOS:
            chips.select(ratio, emit=False)

    def language_key(self):
        data = self.language_combo.currentData()
        return str(data or 'zh')

    def analysis_model_value(self):
        return normalize_model_id(
            self.analysis_model_picker.value(),
            catalog_snapshot(self.config_manager, 'chat').keys())

    def image_model_value(self):
        return normalize_model_id(
            self.image_model_picker.value(),
            catalog_snapshot(self.config_manager, 'image').keys())

    def tier_value(self):
        text = self.tier_combo.currentText()
        return text if text in TIER_SHORT_EDGE else FALLBACK_TIER

    def selected_platforms(self):
        return self.platform_selector.selected_platforms()

    def shared_defaults(self):
        return {
            'platforms': self.selected_platforms(),
            'language': self.language_key(),
            'enable_carousel': bool(self.carousel_check.isChecked()),
            'carousel_count': int(self.carousel_spin.value()),
            'carousel_ratio': self.carousel_ratios.current_ratio(),
            'enable_detail': bool(self.detail_check.isChecked()),
            'detail_count': int(self.detail_spin.value()),
            'detail_ratio': self.detail_ratios.current_ratio(),
            'analysis_model': self.analysis_model_value(),
            'image_model': self.image_model_value(),
            'tier': self.tier_value(),
            'style_image_paths': self.style_grid.paths(),
            'thinking': bool(self.thinking_enabled),
        }

    def set_shared_defaults(self, **kwargs):
        was_loading = self._loading
        self._loading = True
        try:
            for name, value in kwargs.items():
                target = self._DEFAULT_ALIASES.get(name)
                if target is None:
                    continue
                self._apply_shared_value(target, value)
        finally:
            self._loading = was_loading
        if not was_loading:
            self._update_input_states()
            self._refresh_all_rows()
            self._refresh_summary()
            self._schedule_draft_save()

    _DEFAULT_ALIASES = {
        'platforms': 'platforms',
        'platform_ids': 'platforms',
        'language': 'language',
        'image_language': 'language',
        'enable_carousel': 'enable_carousel',
        'carousel_enabled': 'enable_carousel',
        'carousel_count': 'carousel_count',
        'carousel_ratio': 'carousel_ratio',
        'enable_detail': 'enable_detail',
        'detail_enabled': 'enable_detail',
        'detail_count': 'detail_count',
        'detail_ratio': 'detail_ratio',
        'analysis_model': 'analysis_model',
        'image_model': 'image_model',
        'tier': 'tier',
        'style_image_path': 'style_image_path',
        'style_image_paths': 'style_image_paths',
        'thinking': 'thinking',
    }

    def _apply_shared_value(self, target, value):
        if target == 'platforms':
            self.select_platform(value, apply_defaults=False)
        elif target == 'language':
            self._select_combo_data(self.language_combo, str(value or 'zh'))
        elif target == 'enable_carousel':
            self.carousel_check.setChecked(bool(value))
        elif target == 'carousel_count':
            self.carousel_spin.setValue(_clamp_int(value, 1, 6, 3))
        elif target == 'carousel_ratio':
            self._select_ratio(self.carousel_ratios, value)
        elif target == 'enable_detail':
            self.detail_check.setChecked(bool(value))
        elif target == 'detail_count':
            self.detail_spin.setValue(_clamp_int(value, 1, 15, 4))
        elif target == 'detail_ratio':
            self._select_ratio(self.detail_ratios, value)
        elif target == 'analysis_model':
            self.analysis_model_picker.set_value(
                normalize_model_id(
                    value, catalog_snapshot(self.config_manager, 'chat').keys()))
        elif target == 'image_model':
            self.image_model_picker.set_value(
                normalize_model_id(
                    value, catalog_snapshot(self.config_manager, 'image').keys()))
        elif target == 'tier':
            text = str(value or '')
            if text in TIER_SHORT_EDGE:
                self.tier_combo.setCurrentText(text)
        elif target in ('style_image_path', 'style_image_paths'):
            paths = _string_list(
                value if isinstance(value, (list, tuple)) else [value], 3)
            self.style_grid.set_paths(paths, emit=False)
        elif target == 'thinking':
            self.thinking_enabled = bool(value)

    def select_platform(self, ids, *, apply_defaults=True):
        """勾选指定平台（最多 5 个），默认套用第一个平台的轮播/详情默认值。"""
        if isinstance(ids, str):
            ids = [ids]
        wanted = []
        for platform_id in ids or []:
            key = _as_str(platform_id)
            if key in self.platform_selector.records and key not in wanted:
                wanted.append(key)
        if len(wanted) > MAX_PLATFORMS:
            self._notice(tr('最多选择 {count} 个平台').format(
                count=MAX_PLATFORMS), warning=True)
            wanted = wanted[:MAX_PLATFORMS]
        was_loading = self._loading
        self._loading = True
        try:
            blocker = QSignalBlocker(self.platform_selector)
            self.platform_selector.set_platforms(wanted)
            del blocker
            self._platform_selection_order = list(wanted)
            if apply_defaults and wanted:
                self._apply_first_platform_defaults()
        finally:
            self._loading = was_loading
        if not was_loading:
            self._refresh_all_rows()
            self._refresh_summary()
            self._schedule_draft_save()
        return list(wanted)

    def _apply_first_platform_defaults(self):
        selected = self.selected_platforms()
        if not selected:
            return
        first = next(
            (key for key in self._platform_selection_order
             if key in selected), selected[0])
        try:
            record = get_platform(first)
        except (KeyError, TypeError):
            return
        try:
            if record.get('defaultCarouselRatio') in RATIOS:
                self.carousel_ratios.select(
                    record['defaultCarouselRatio'], emit=False)
            if record.get('defaultDetailRatio') in RATIOS:
                self.detail_ratios.select(
                    record['defaultDetailRatio'], emit=False)
            self.carousel_spin.setValue(_clamp_int(
                record.get('defaultCarouselCount'), 1, 6, 3))
            self.detail_spin.setValue(_clamp_int(
                record.get('defaultDetailCount'), 1, 15, 4))
        except (RuntimeError, TypeError, ValueError):
            pass

    def _platform_changed(self, selected):
        if self._loading:
            return
        self._platform_selection_order = list(selected)
        self._apply_first_platform_defaults()
        self._shared_changed()

    def _persist_model(self, key, value):
        kind = 'chat' if key == 'analysis_model' else 'image'
        clean = normalize_model_id(
            value, catalog_snapshot(self.config_manager, kind).keys())
        try:
            self.config_manager.update(('ecommerce', key), clean)
        except (OSError, ValueError, TypeError):
            pass
        self._shared_changed()

    # ------------------------------------------------------------------
    # 行管理
    # ------------------------------------------------------------------
    def _create_row(self, *, image_paths=None, product_name='',
                    product_info=''):
        row = EcommerceBatchRow(
            self, image_paths=image_paths, product_name=product_name,
            product_info=product_info)
        row.changed.connect(self._row_changed)
        row.selection_changed.connect(self._selection_changed)
        row.copy_requested.connect(self.copy_row)
        row.delete_requested.connect(self.delete_row)
        row.browse_requested.connect(self.browse_row_images)
        row.gallery_requested.connect(self.pick_gallery_for_row)
        row.used_requested.connect(self.pick_used_for_row)
        row.url_requested.connect(self.pick_url_for_row)
        row.files_dropped.connect(self._on_row_files_dropped)
        row.paste_requested.connect(self._on_row_paste_requested)
        self.rows.append(row)
        self._rows_layout.addWidget(row)
        row.set_index(len(self.rows))
        return row

    def add_row(self):
        added = self.add_rows(1)
        return added[0] if added else None

    def add_rows(self, count):
        try:
            requested = int(count)
        except (TypeError, ValueError):
            return []
        if requested <= 0:
            return []
        capacity = MAX_ROWS - len(self.rows)
        if capacity <= 0:
            self._notice(tr('最多 {count} 行，无法继续新增').format(
                count=MAX_ROWS), warning=True)
            return []
        count = min(requested, capacity)
        created = [self._create_row() for _ in range(count)]
        if count < requested:
            self._notice(tr('最多 {count} 行，无法继续新增').format(
                count=MAX_ROWS), warning=True)
        self._renumber_rows()
        self._refresh_all_rows()
        self._refresh_summary()
        self._schedule_draft_save()
        return created

    def copy_row(self, row):
        """行内「复制行」：复制该行的图片路径 / 名称 / 信息。"""
        if len(self.rows) >= MAX_ROWS:
            self._notice(tr('最多 {count} 行，无法继续新增').format(
                count=MAX_ROWS), warning=True)
            return None
        new_row = self._create_row(
            image_paths=list(row.product_image_paths),
            product_name=row.name_edit.text(),
            product_info=row.info_edit.toPlainText())
        new_row.set_index(len(self.rows))
        self._renumber_rows()
        self._refresh_all_rows()
        self._refresh_summary()
        self._schedule_draft_save()
        self._notice(tr('已复制 1 行'), warning=False)
        return new_row

    def copy_selected(self):
        sources = self.selected_rows()
        if not sources:
            self._notice(tr('请先选择要复制的行'), warning=True)
            return []
        capacity = MAX_ROWS - len(self.rows)
        if capacity <= 0:
            self._notice(tr('最多 {count} 行，无法继续新增').format(
                count=MAX_ROWS), warning=True)
            return []
        copies = []
        for source in sources[:capacity]:
            copies.append(self._create_row(
                image_paths=list(source.product_image_paths),
                product_name=source.name_edit.text(),
                product_info=source.info_edit.toPlainText()))
        self._renumber_rows()
        self._refresh_all_rows()
        self._refresh_summary()
        self._schedule_draft_save()
        if len(copies) < len(sources):
            self._notice(tr('最多 {count} 行，仅复制了 {copied} 行').format(
                count=MAX_ROWS, copied=len(copies)), warning=True)
        else:
            self._notice(tr('已复制 {count} 行').format(count=len(copies)))
        return copies

    def delete_row(self, row):
        if row not in self.rows:
            return False
        self._remove_row(row)
        if not self.rows:
            self._create_row()
        self._renumber_rows()
        self._refresh_all_rows()
        self._refresh_summary()
        self._schedule_draft_save()
        return True

    def delete_selected(self):
        sources = self.selected_rows()
        if not sources:
            self._notice(tr('请先选择要删除的行'), warning=True)
            return 0
        for row in sources:
            self._remove_row(row)
        if not self.rows:
            self._create_row()
        self._renumber_rows()
        self._refresh_all_rows()
        self._refresh_summary()
        self._schedule_draft_save()
        self._notice(tr('已删除 {count} 行').format(count=len(sources)))
        return len(sources)

    def clear_rows(self):
        removed = len(self.rows)
        for row in list(self.rows):
            self._remove_row(row)
        self._create_row()
        self._renumber_rows()
        self._refresh_all_rows()
        self._refresh_summary()
        self._schedule_draft_save()
        self._notice(tr('已清空 {count} 行').format(count=removed))
        return removed

    def select_all(self):
        for row in self.rows:
            row.set_selected(True)
        self._refresh_summary()

    def deselect_all(self):
        for row in self.rows:
            row.set_selected(False)
        self._refresh_summary()

    def toggle_select_all(self):
        total = len(self.rows)
        selected = len(self.selected_rows())
        if total and selected == total:
            self.deselect_all()
        else:
            self.select_all()

    def selected_rows(self):
        return [row for row in self.rows if row.checkbox.isChecked()]

    def selected_count(self):
        return len(self.selected_rows())

    def _remove_row(self, row):
        try:
            self.rows.remove(row)
        except ValueError:
            return
        self._rows_layout.removeWidget(row)
        row.hide()
        row.setParent(None)
        row.deleteLater()

    def _renumber_rows(self):
        for index, row in enumerate(self.rows, start=1):
            row.set_index(index)

    # ------------------------------------------------------------------
    # 校验 / 提交
    # ------------------------------------------------------------------
    def validate_row(self, row):
        """按参考软件顺序校验：产品图 → 信息 → 平台 → 输出类型 → AI 模型 → 图片模型。"""
        if not row.product_image_paths:
            return ('images', tr('请至少上传 1 张产品图'))
        if not row.info_text():
            return ('product_info', tr('请填写产品信息'))
        if not self.selected_platforms():
            return ('platform', tr('请选择至少一个平台'))
        if not (self.carousel_check.isChecked()
                or self.detail_check.isChecked()):
            return ('output', tr('请选择至少一种输出类型（轮播图或详情图）'))
        if not self.analysis_model_value():
            return ('analysis_model', tr('请选择 AI 分析模型'))
        if not self.image_model_value():
            return ('image_model', tr('请选择图片生成模型'))
        return ('', '')

    def row_ready(self, row):
        return self.validate_row(row)[0] == ''

    def ready_count(self):
        return sum(1 for row in self.rows if row.ready)

    def build_submissions(self):
        shared = self.shared_defaults()
        if len(shared['style_image_paths']) > 3:
            raise ValueError(tr('风格图最多3张'))
        submissions = []
        for index, row in enumerate(self.rows, start=1):
            if not row.ready:
                continue
            submissions.append({
                'platforms': list(shared['platforms']),
                'product_name': row.name_text(),
                'product_info': row.info_text(),
                'language': shared['language'],
                'carousel_count': (shared['carousel_count']
                                   if shared['enable_carousel'] else 0),
                'detail_count': (shared['detail_count']
                                 if shared['enable_detail'] else 0),
                'carousel_ratio': shared['carousel_ratio'],
                'detail_ratio': shared['detail_ratio'],
                'tier': shared['tier'],
                'product_image_paths': [_resolved(path)
                                        for path in row.product_image_paths],
                'style_image_paths': [_resolved(p) for p in shared['style_image_paths']],
                'thinking': bool(shared['thinking']),
                'row_index': index,
            })
        return submissions

    def submit(self):
        try:
            payload = self.build_submissions()
        except ValueError as error:
            self._notice(str(error), warning=True)
            return False
        if not payload:
            self._notice(tr('没有可提交的行'), warning=True)
            return False
        self.submit_requested.emit(payload)
        return True

    # ------------------------------------------------------------------
    # 图片选择 / 粘贴 / 拖拽
    # ------------------------------------------------------------------
    def add_product_images(self, row, paths):
        cleaned = []
        for path in paths or []:
            text = _as_str(path)
            if text and is_image_file(text):
                cleaned.append(text)
        if not cleaned:
            return []
        original = list(row.product_image_paths)
        merged = list(dict.fromkeys(original + cleaned))
        if len(merged) > MAX_PRODUCT_IMAGES:
            self._notice(tr('产品图最多 6 张，请移除后再添加'), warning=True)
            merged = merged[:MAX_PRODUCT_IMAGES]
        row.set_images(merged)
        return [path for path in merged if path not in original]

    def browse_row_images(self, row):
        paths, _filter = QFileDialog.getOpenFileNames(
            self, tr('选择产品图'), '', _image_filter())
        if paths:
            self.add_product_images(
                row, [str(Path(path).resolve()) for path in paths])

    def pick_gallery_for_row(self, row):
        dialog = None
        try:
            dialog = GalleryPickerDialog(
                self, self.config_manager, max_count=MAX_PRODUCT_IMAGES)
            if dialog.exec_() == dialog.Accepted:
                self.add_product_images(row, dialog.result_paths)
        finally:
            safe_delete_dialog(dialog)

    def pick_used_for_row(self, row):
        dialog = None
        try:
            dialog = UsedImagePickerDialog(
                self, self.config_manager.ecommerce_store(),
                max_count=MAX_PRODUCT_IMAGES)
            if dialog.exec_() == dialog.Accepted:
                self.add_product_images(row, dialog.result_paths)
        finally:
            safe_delete_dialog(dialog)

    def pick_url_for_row(self, row):
        dialog = None
        try:
            dialog = UrlImageDialog(
                self, image_cache_dir(self.config_manager),
                max_count=MAX_PRODUCT_IMAGES)
            if dialog.exec_() == dialog.Accepted:
                self.add_product_images(row, dialog.result_paths)
        finally:
            safe_delete_dialog(dialog)

    def _merge_style_images(self, paths):
        merged = list(dict.fromkeys(self.style_grid.paths() + list(paths)))
        if len(merged) > 3:
            self._notice(tr('风格图最多3张，请移除后再添加'), warning=True)
        self.style_grid.set_paths(merged[:3])

    def _preview_style_image(self, index):
        paths = self.style_grid.paths()
        if paths:
            self._lightbox = ImageLightbox.show_images(self, paths, index)

    def pick_style_file(self):
        paths, _filter = QFileDialog.getOpenFileNames(
            self, tr('选择风格图'), '', _image_filter())
        if paths:
            self._merge_style_images([str(Path(p).resolve()) for p in paths])

    def pick_style_used(self):
        dialog = None
        try:
            dialog = UsedImagePickerDialog(
                self, self.config_manager.ecommerce_store(), max_count=3)
            if dialog.exec_() == dialog.Accepted:
                self._merge_style_images(dialog.result_paths)
        finally:
            safe_delete_dialog(dialog)

    def pick_style_gallery(self):
        dialog = None
        try:
            dialog = GalleryPickerDialog(
                self, self.config_manager, max_count=3)
            if dialog.exec_() == dialog.Accepted:
                self._merge_style_images(dialog.result_paths)
        finally:
            safe_delete_dialog(dialog)

    def _clipboard(self):
        return QApplication.clipboard()

    def _save_clipboard_image(self, image):
        if not isinstance(image, QImage) or image.isNull():
            return ''
        cache = image_cache_dir(self.config_manager)
        target = cache / ('pasted_%d_%s.png'
                          % (int(time.time() * 1000), uuid.uuid4().hex[:8]))
        try:
            ok = image.save(str(target), 'PNG')
        except (OSError, RuntimeError):
            ok = False
        if not ok:
            self._notice(tr('粘贴的图片保存失败，请重试'), warning=True)
            return ''
        return _resolved(target)

    def paste_image_to_row(self, row, image):
        path = self._save_clipboard_image(image)
        if not path:
            return ''
        self.add_product_images(row, [path])
        return path

    def paste_paths_to_row(self, row, entries):
        cleaned = [_as_str(entry) for entry in entries or []]
        cleaned = [entry for entry in cleaned if entry]
        if not cleaned:
            return []
        remaining = max(0, MAX_PRODUCT_IMAGES - len(row.product_image_paths))
        if remaining <= 0:
            self._notice(tr('产品图最多 6 张，请移除后再添加'), warning=True)
            return []
        paths, errors = resolve_input_entries(
            cleaned, image_cache_dir(self.config_manager), max_count=remaining)
        if errors:
            self._notice('；'.join(errors), warning=True)
        if not paths:
            return []
        return self.add_product_images(row, paths)

    def paste_into_row(self, row):
        """Ctrl+V 统一入口：优先剪贴板图片，其次本地路径 / 链接文本。"""
        added = []
        clipboard = self._clipboard()
        image = _clipboard_image(clipboard)
        if not image.isNull():
            path = self.paste_image_to_row(row, image)
            if path:
                added.append(path)
            return added
        mime = None
        try:
            mime = clipboard.mimeData()
        except Exception:  # noqa: BLE001
            mime = None
        entries = []
        if mime is not None:
            try:
                if mime.hasUrls():
                    entries = [url.toLocalFile() for url in mime.urls()
                               if url.isLocalFile()]
                elif mime.hasText():
                    entries = [line.strip()
                               for line in mime.text().splitlines()
                               if line.strip()]
            except (RuntimeError, TypeError, ValueError):
                entries = []
        if entries:
            added.extend(self.paste_paths_to_row(row, entries))
        return added

    def _on_row_files_dropped(self, row, paths):
        self.add_product_images(row, paths)

    def _on_row_paste_requested(self, row):
        self.paste_into_row(row)

    def _on_style_changed(self, _paths):
        if self._loading:
            return
        self._refresh_all_rows()
        self._refresh_summary()
        self._schedule_draft_save()

    # ------------------------------------------------------------------
    # 状态刷新
    # ------------------------------------------------------------------
    def _update_input_states(self):
        carousel = self.carousel_check.isChecked()
        detail = self.detail_check.isChecked()
        self.carousel_spin.setEnabled(carousel)
        self.carousel_ratios.set_chips_enabled(carousel)
        self.detail_spin.setEnabled(detail)
        self.detail_ratios.set_chips_enabled(detail)

    def _row_changed(self, *_args):
        if self._loading:
            return
        self._refresh_all_rows()
        self._refresh_summary()
        self._schedule_draft_save()

    def _selection_changed(self, *_args):
        if self._loading:
            return
        self._refresh_summary()

    def _shared_changed(self, *_args):
        if self._loading:
            return
        self._update_input_states()
        self._refresh_all_rows()
        self._refresh_summary()
        self._schedule_draft_save()

    def _refresh_all_rows(self):
        for row in self.rows:
            row.refresh_state()

    def _refresh_summary(self):
        total = len(self.rows)
        ready = self.ready_count()
        selected = self.selected_count()
        self.stats_label.setText(tr('共 {total} 行 · 就绪 {ready}').format(
            total=total, ready=ready))
        self.copy_button.setText(tr('复制选中（{count}）').format(
            count=selected))
        self.copy_button.setEnabled(selected > 0)
        self.delete_button.setEnabled(selected > 0)
        all_selected = total > 0 and selected == total
        self.select_all_button.setText(
            tr('取消全选') if all_selected else tr('全选'))
        self.submit_button.setText(tr('开始批量制作（{count}）').format(
            count=ready))
        self.submit_button.setEnabled(ready > 0)
        self.submit_button.setToolTip('' if ready else tr('没有可提交的行'))

    # ------------------------------------------------------------------
    # 提示 / 主题 / 缩放 / 语言
    # ------------------------------------------------------------------
    def _notice(self, message, warning=False):
        self.last_notice = str(message or '')
        try:
            self.notice_label.setText(self.last_notice)
            self.notice_label.setVisible(bool(self.last_notice))
            self._refresh_notice_color(warning)
            if self.last_notice:
                self._notice_timer.start()
        except RuntimeError:
            pass

    def _refresh_notice_color(self, warning=False):
        try:
            color = (materials.palette()['error'] if warning
                     else materials.palette()['text2'])
            self.notice_label.setStyleSheet('color: %s;' % color)
        except RuntimeError:
            pass

    def _clear_notice(self):
        try:
            self.notice_label.clear()
            self.notice_label.setVisible(False)
        except RuntimeError:
            pass

    def eventFilter(self, watched, event):
        if (getattr(self, 'body_scroll', None) is not None
                and watched is self.body_scroll.viewport()
                and event.type() == QEvent.Resize
                and hasattr(self, '_model_fields')):
            self._fit_model_grid()
        return super().eventFilter(watched, event)

    def _fit_model_grid(self):
        available = self.body_scroll.viewport().width() - px(80)
        columns = max(1, min(3, (available + px(16)) // px(236)))
        if columns == self._model_columns:
            return
        self._model_columns = columns
        fields = [self.models_grid.takeAt(0) for _ in self._model_fields]
        for column in range(3):
            self.models_grid.setColumnMinimumWidth(column, px(220) if column < columns else 0)
            self.models_grid.setColumnStretch(column, 1 if column < columns else 0)
        for index, field in enumerate(fields):
            self.models_grid.addItem(field, index // columns, index % columns)

    def _apply_metrics(self):
        self._header_layout.setContentsMargins(px(24), px(14), px(24), px(6))
        self._header_layout.setSpacing(px(12))
        self._body_layout.setContentsMargins(px(24), px(8), px(24), px(8))
        self._body_layout.setSpacing(px(14))
        self._footer_layout.setContentsMargins(px(24), px(8), px(24), px(14))
        self._footer_layout.setSpacing(px(12))
        for layout in (self.shared_layout, self.table_layout):
            layout.setContentsMargins(px(16), px(16), px(16), px(16))
            layout.setSpacing(px(12))
        self._rows_layout.setSpacing(px(10))
        self._model_columns = 0
        self._fit_model_grid()
        for combo in (self.analysis_model_picker, self.image_model_picker, self.tier_combo):
            combo.setMinimumWidth(px(220))
            combo.setFixedHeight(px(40))
        self.language_combo.setMinimumWidth(px(220))
        self.language_combo.setFixedHeight(px(40))
        self.carousel_spin.setFixedWidth(px(110))
        self.detail_spin.setFixedWidth(px(110))
        for button in (self.style_used_button, self.style_gallery_button):
            button.setFixedHeight(px(26))
            button.setStyleSheet(
                'QPushButton {background: %s; color: %s; border: 0;'
                ' border-radius: %dpx; padding: 1px %dpx; font-size: %dpx;}'
                % (materials.ECOMMERCE['brand_blue'],
                   materials.ECOMMERCE['white'], px(6), px(8), px(12)))
        self.carousel_ratios.apply_scale()
        self.detail_ratios.apply_scale()
        self.platform_selector.refresh_interface_scale()
        self.style_grid.refresh_interface_scale()
        for row in self.rows:
            row.apply_scale()

    def _refresh_colors(self):
        colors = materials.palette()
        panel_style = ('QFrame#ecommerceBatchPanel {background: %s;'
                       ' border: 1px solid %s; border-radius: %dpx;}'
                       % (materials.rgba(colors['surface']),
                          materials.rgba(colors['border']), px(12)))
        for panel in self._panels:
            try:
                panel.setStyleSheet(panel_style)
            except RuntimeError:
                continue
        self.stats_label.setStyleSheet('color: %s;' % colors['text2'])
        self.footer_hint.setStyleSheet('color: %s;' % colors['text3'])
        self._refresh_notice_color()
        self.carousel_ratios.refresh_colors()
        self.detail_ratios.refresh_colors()
        for row in self.rows:
            try:
                row.refresh_colors()
            except RuntimeError:
                continue

    def refresh_colors(self):
        self.platform_selector.refresh_colors()
        self._refresh_colors()
        self._refresh_summary()
        self.update()

    def closeEvent(self, event):
        # 关闭前落盘一次，避免 500ms 防抖窗口内的最后编辑丢失；失败保持静默。
        try:
            if not self._loading:
                self.save_draft()
        except (RuntimeError, OSError):
            pass
        super().closeEvent(event)

    def refresh_interface_scale(self):
        self._apply_metrics()
        self._refresh_colors()
        self._refresh_summary()

    def retranslate_ui(self):
        self.platform_selector.retranslate_ui()
        self.title_label.setText(tr('电商图批量制作'))
        self.subtitle_label.setText(
            tr('每行一个商品，提交后在历史任务中各自显示为独立任务组'))
        self.history_button.setText(tr('历史任务'))
        self.single_button.setText(tr('单个制作'))
        self.shared_title.setText(tr('共享默认（新建行继承，提交时使用）'))
        self.platform_hint_label.setText(
            tr('建议只选一个平台，多平台可能存在规则冲突（最多5个）'))
        self.carousel_check.setText(tr('轮播图'))
        self.detail_check.setText(tr('详情图'))
        self.style_label.setText(tr('风格参考图（可选，最多3张，整批共用）'))
        self.style_used_button.setText(tr('已用图片'))
        self.style_gallery_button.setText(tr('画廊选图'))
        self.add_one_button.setText(tr('新增 1 行'))
        self.add_five_button.setText(tr('新增 5 行'))
        self.delete_button.setText(tr('删除选中'))
        self.clear_button.setText(tr('清空'))
        self.footer_hint.setText(
            tr('把图片拖进图片格，或在图片格 / 本行名称与信息框里按 Ctrl+V 粘贴截图'))
        current_language = self.language_key()
        blocker = QSignalBlocker(self.language_combo)
        self.language_combo.clear()
        for key, value in LANGUAGE_TEXT.items():
            self.language_combo.addItem(tr(value), userData=key)
        self._select_combo_data(self.language_combo, current_language)
        del blocker
        self.analysis_model_picker.set_models(
            catalog_snapshot(self.config_manager, 'chat'),
            self.analysis_model_value(), kind='chat')
        self.image_model_picker.set_models(
            catalog_snapshot(self.config_manager, 'image'),
            self.image_model_value(), kind='image')
        self.platform_caption.setText(tr('电商平台'))
        self.language_caption.setText(tr('图片文字语言'))
        self.analysis_caption.setText(tr('AI 分析模型'))
        self.image_caption.setText(tr('图片生成模型'))
        self.tier_caption.setText(tr('分辨率'))
        self.carousel_unit_label.setText(tr('张'))
        self.detail_unit_label.setText(tr('张'))
        for row in self.rows:
            row.retranslate_ui()
        self._refresh_all_rows()
        self._refresh_summary()
        self._refresh_notice_color()

    # ------------------------------------------------------------------
    # 草稿持久化
    # ------------------------------------------------------------------
    def draft_path(self):
        base = getattr(self.config_manager, 'path', None)
        if base is None:
            base = Path.home() / '.yanlin' / 'config.json'
        return Path(base).parent / DRAFT_FILENAME

    def _draft_payload(self):
        return {
            'version': DRAFT_VERSION,
            'saved_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'shared': self.shared_defaults(),
            'rows': [
                {
                    'product_image_paths': list(row.product_image_paths),
                    'product_name': row.name_edit.text(),
                    'product_info': row.info_edit.toPlainText(),
                    'selected': bool(row.checkbox.isChecked()),
                }
                for row in self.rows
            ],
        }

    def _schedule_draft_save(self):
        if self._loading:
            return
        try:
            self._draft_timer.start()
        except RuntimeError:
            pass

    def save_draft(self):
        try:
            self._draft_timer.stop()
        except RuntimeError:
            pass
        path = self.draft_path()
        payload = self._draft_payload()
        temporary = path.with_name(path.name + '.tmp')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(temporary, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self.draft_save_error = ''
            return True
        except (OSError, TypeError, ValueError):
            self.draft_save_error = tr('草稿保存失败，已保留内存中的内容')
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass
            return False

    def load_draft(self):
        path = self.draft_path()
        try:
            if not path.is_file():
                return False
            raw = path.read_text(encoding='utf-8')
            data = json.loads(raw)
        except (OSError, ValueError, TypeError):
            self.draft_load_error = tr('批量草稿读取失败，已忽略（不影响新建）')
            self._notice(self.draft_load_error, warning=True)
            return False
        if not isinstance(data, dict):
            self.draft_load_error = tr('批量草稿读取失败，已忽略（不影响新建）')
            self._notice(self.draft_load_error, warning=True)
            return False
        was_loading = self._loading
        self._loading = True
        try:
            shared = data.get('shared')
            if isinstance(shared, dict):
                self.set_shared_defaults(**{
                    key: value for key, value in shared.items()
                    if key in self._DEFAULT_ALIASES})
            self._restore_rows(data.get('rows'))
        finally:
            self._loading = was_loading
        self._update_input_states()
        self._refresh_all_rows()
        self._refresh_summary()
        return True

    def _restore_rows(self, rows):
        for row in list(self.rows):
            self._remove_row(row)
        restored = 0
        if isinstance(rows, list):
            for item in rows[:MAX_ROWS]:
                if not isinstance(item, dict):
                    continue
                paths = _string_list(
                    item.get('product_image_paths'), MAX_PRODUCT_IMAGES)
                name = _as_str(item.get('product_name'))
                info = _as_str(item.get('product_info'))
                row = self._create_row(
                    image_paths=paths, product_name=name, product_info=info)
                if bool(item.get('selected')):
                    blocker = QSignalBlocker(row.checkbox)
                    row.checkbox.setChecked(True)
                    del blocker
                restored += 1
        self._renumber_rows()
        return restored
