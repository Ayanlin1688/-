"""电商图任务列表：卡片 / 筛选 tab / 管理模式批量删除 / 分页。

1:1 复刻参考软件「电商图任务」页（§5.1 第 10、11 条 + §5.2 历史列表规格），
用 PyQt5 + QFluentWidgets 重写：不联网、不读数据库，groups 由页面 enrich 后
传入；所有缺失键按缺省值兜底，页面可以只传最小字段。

对外契约（页面接线用）：
    signals: refresh_requested / view_requested / resume_requested /
             open_requested / delete_requested(list) / batch_create_requested /
             new_create_requested / preview_requested
    methods: update_groups(groups, persistence_available=True) / set_busy(bool) /
             refresh_colors() / selected_ids() / set_manage_mode(bool) /
             retranslate_ui()
    attrs:   cards(当前可见 group_id -> EcommerceTaskCard) / current_tab
"""
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime
import re

from PyQt5.QtCore import QEvent, QRectF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPixmap
from PyQt5.QtWidgets import (
    QBoxLayout, QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    Action, CardWidget, CheckBox, FluentIcon as FIF, PrimaryPushButton,
    PushButton, RoundMenu, StrongBodyLabel,
)

from core.i18n import tr
from .. import materials
from ..responsive import WrapLayout
from ..theme import style_controls
from ..tokens import px
from . import image_async
from .studio_dialog import StudioDialog, safe_delete_dialog

MAX_GROUPS = 50
RESUMABLE_STATUSES = frozenset(('partial', 'failed', 'analyzing', 'generating'))
TAB_KEYS = ('all', 'generating', 'done', 'issue')
TAB_STATUSES = {
    'all': None,
    'generating': ('analyzing', 'generating'),
    'done': ('succeeded',),
    'issue': ('partial', 'failed', 'cancelled'),
}


def _base_px(value):
    """Return an immutable 100% baseline for newly constructed widgets.

    InterfaceScaler applies the active scale to widget bounds after
    construction.  Keeping this value unscaled makes cards rebuilt at 125%
    match cards that were present before the scale change.
    """
    return round(float(value))

# ---------------------------------------------------------------------------
# 小工具

def _rgba(color, alpha):
    """把主题色转成 rgba() 样式串（alpha 0-255）。"""
    value = color if isinstance(color, QColor) else QColor(color)
    return 'rgba(%d,%d,%d,%d)' % (value.red(), value.green(), value.blue(), alpha)


def _image(key):
    """只读取有界 QImage 缓存；界面刷新路径不读盘、不解码。"""
    return image_async.cached_image(key)


def _label(size=13, bold=False, wrap=False, text=''):
    widget = QLabel(text)
    widget.setTextFormat(Qt.PlainText)
    widget.setWordWrap(wrap)
    font = widget.font()
    font.setFamily(materials.ui_font_family())
    # 声明尺寸使用 100% 基线；InterfaceScaler 负责控件与样式缩放。
    font.setPixelSize(size)
    font.setBold(bold)
    widget.setFont(font)
    return widget


def _platform_name(platform):
    if isinstance(platform, dict):
        return str(platform.get('name') or '')
    return str(platform or '')


def _status_tone(status):
    colors = materials.palette()
    if status in ('analyzing', 'generating'):
        return colors['accent']
    if status == 'partial':
        return colors['warning']
    if status == 'succeeded':
        return colors['success']
    if status == 'failed':
        return colors['error']
    return colors['text2']


def _as_count(value):
    return value if type(value) is int and value >= 0 else None


def _group_thumbs(group):
    """缩略图优先取页面给的 thumbs；缺失时从 manifest 成功项按轮播→详情取前 3。"""
    thumbs = []
    for value in group.get('thumbs') or ():
        if value:
            thumbs.append(str(value))
        if len(thumbs) >= 3:
            return thumbs
    if thumbs:
        return thumbs
    manifest = group.get('manifest')
    items = manifest.get('items') if isinstance(manifest, dict) else ()
    succeeded = [item for item in items or () if isinstance(item, dict)
                 and item.get('status') == 'succeeded' and item.get('file')]
    succeeded.sort(key=lambda item: (0 if item.get('kind') == 'carousel' else 1,
                                     item.get('seq') if type(item.get('seq')) is int else 0))
    return [str(item['file']) for item in succeeded[:3]]


def _product_info(group):
    config = group.get('config') if isinstance(group.get('config'), dict) else {}
    manifest = group.get('manifest') if isinstance(group.get('manifest'), dict) else {}
    info = str(config.get('product_info') or manifest.get('product_info') or '').strip()
    return info


def _display_description(group):
    """列表仅展示简短产品说明，避免把系统提示词整段铺进任务卡。"""
    original = _product_info(group)
    if not original:
        return tr('（未填写产品信息）')
    text = re.sub(r'\s+', ' ', original.replace('\r\n', '\n').replace('\t', ' ')).strip()
    folded = text.lower()
    chinese = len(re.findall(r'[\u4e00-\u9fff]', text))
    prompt_like = (text.startswith('[')
                   or any(marker in folded for marker in
                          ('system role', 'specification', 'you are ', 'ground truth'))
                   or (len(text) > 300 and chinese / len(text) < 0.2))

    def clipped(value, limit):
        return value[:limit] + '…' if len(value) > limit else value

    if not prompt_like:
        return clipped(text, 200)
    manifest = group.get('manifest') if isinstance(group.get('manifest'), dict) else {}
    for item in manifest.get('items') or ():
        if not isinstance(item, dict) or item.get('status') != 'succeeded':
            continue
        description = re.sub(r'\s+', ' ', str(item.get('desc') or item.get('title') or '')).strip()
        if re.search(r'[\u4e00-\u9fff]', description):
            return clipped(description, 140)
    carousel, detail = _group_output_counts(group)
    if carousel is not None or detail is not None:
        return tr('提示词型产品信息 · 轮播 {carousel} 张 / 详情 {detail} 张').format(
            carousel=carousel or 0, detail=detail or 0)
    return tr('已提交提示词型任务')


def _group_model(group):
    config = group.get('config') if isinstance(group.get('config'), dict) else {}
    manifest = group.get('manifest') if isinstance(group.get('manifest'), dict) else {}
    return str(group.get('image_model') or config.get('image_model')
               or manifest.get('image_model') or '')


def _group_output_counts(group):
    """轮播 / 详情张数：优先 config，其次 manifest，最后按清单项计数。"""
    config = group.get('config') if isinstance(group.get('config'), dict) else {}
    manifest = group.get('manifest') if isinstance(group.get('manifest'), dict) else {}
    items = [item for item in manifest.get('items') or () if isinstance(item, dict)]

    def resolve(key, kind):
        for source in (config, manifest):
            value = _as_count(source.get(key))
            if value is not None:
                return value
        return sum(1 for item in items if item.get('kind') == kind) if items else None

    return resolve('carousel_count', 'carousel'), resolve('detail_count', 'detail')


def _created_text(group):
    value = group.get('created_at')
    try:
        return datetime.fromtimestamp(float(value)).strftime('%Y-%m-%d %H:%M')
    except (TypeError, ValueError, OSError, OverflowError):
        return ''


# ---------------------------------------------------------------------------
# 缩略图 / 序号盒

class _Thumbnail(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.PlainText)
        self.setAlignment(Qt.AlignCenter)
        self.setFixedSize(_base_px(88), _base_px(88))
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.has_image = False
        self.path = ''
        self._rendered_path = None
        self._rendered_theme = None
        self._rendered_key = None
        self._rendered_size = None
        self._requested_key = None
        font = self.font()
        font.setFamily(materials.ui_font_family())
        font.setPixelSize(12)
        self.setFont(font)
        self.set_source('')

    def set_source(self, path):
        """先显示占位或缓存；缺失缓存交给后台线程读取与解码。"""
        path = str(path) if path else ''
        theme = materials.THEME_ID
        key = image_async.image_cache_key(path) if path else None
        size = (px(88), float(self.devicePixelRatioF() or 1.0))
        if (path == self._rendered_path and theme == self._rendered_theme
                and key == self._rendered_key and size == self._rendered_size):
            return
        self.path = path
        self._rendered_path, self._rendered_theme = path, theme
        self._rendered_key, self._rendered_size = key, size
        self.setToolTip(path)
        self.has_image = False
        if not path:
            self._requested_key = None
            self._paint_thumbnail(QImage())
            return
        image = _image(key)
        if image is not None:
            self._requested_key = None
            self._paint_thumbnail(image, missing=image.isNull())
            return
        self._paint_thumbnail(QImage())
        if key != self._requested_key:
            self._requested_key = key
            image_async.load_image_async(path, (240, 240), self._decoded)

    def _decoded(self, key, path, image):
        if path != self.path:
            return
        current_key = image_async.image_cache_key(path)
        if current_key != key:
            if current_key != self._rendered_key:
                self.set_source(path)
            return
        self._rendered_key = key
        self._requested_key = None
        self._paint_thumbnail(image, missing=image.isNull())

    def _paint_thumbnail(self, image, missing=False):
        size = px(88)
        ratio = max(1.0, float(self.devicePixelRatioF() or 1.0))
        self.has_image = not image.isNull()
        self.setProperty('thumbLoaded', self.has_image)
        self.setStyleSheet(
            'color:%s; background:%s; border-radius:%dpx;'
            % (materials.text_color(3), materials.ecommerce_color('thumb_bg'), 6))
        if missing:
            self.setPixmap(QPixmap())
            self.setText(tr('缺失'))
            return
        self.setText('')
        canvas = QPixmap(round(size * ratio), round(size * ratio))
        canvas.setDevicePixelRatio(ratio)
        canvas.fill(Qt.transparent)
        painter = QPainter(canvas)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            path_ = QPainterPath()
            path_.addRoundedRect(QRectF(0, 0, size, size), px(6), px(6))
            painter.setClipPath(path_)
            painter.fillRect(QRectF(0, 0, size, size),
                             QColor(materials.ecommerce_color('thumb_bg')))
            if not image.isNull():
                physical = round(size * ratio)
                scaled = image.scaled(physical, physical, Qt.KeepAspectRatioByExpanding,
                                      Qt.SmoothTransformation)
                source = QRectF((scaled.width() - physical) / 2,
                                (scaled.height() - physical) / 2,
                                physical, physical)
                painter.drawImage(QRectF(0, 0, size, size), scaled, source)
            else:
                icon_size = px(18)
                painter.setOpacity(0.45)
                painter.drawPixmap((size - icon_size) // 2, (size - icon_size) // 2,
                                   FIF.PHOTO.icon().pixmap(icon_size, icon_size))
        finally:
            painter.end()
        self.setPixmap(canvas)

    def refresh(self):
        # 主题/语言刷新需要重绘占位；解码结果仍直接复用缓存。
        self._rendered_theme = None
        self.set_source(self.path)


class _IndexBox(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setFixedSize(_base_px(24), _base_px(24))
        font = self.font()
        font.setFamily(materials.ui_font_family())
        font.setPixelSize(12)
        self.setFont(font)
        self.setTextFormat(Qt.PlainText)

    def refresh_colors(self):
        colors = materials.palette()
        self.setStyleSheet('color:%s; background:transparent; border:0; padding:0;'
                           % colors['text2'])


# ---------------------------------------------------------------------------
# 单个任务卡

class EcommerceTaskCard(CardWidget):
    """单条任务卡；所有操作转发到所属面板的信号。"""

    def __init__(self, group, panel, index=0):
        super().__init__(panel)
        self.panel = panel
        self.group = dict(group or {})
        self.group_id = str(self.group.get('group_id') or '')
        self.index = int(index)
        self._selected = False
        self.setProperty('selected', False)
        self._hover = False
        self._styled = False
        self._theme_id = None
        self._chip_names = []
        self._data_initialized = False
        self._narrow_actions = None
        self._description_timer = QTimer(self)
        self._description_timer.setSingleShot(True)
        self._description_timer.timeout.connect(self._sync_description_height)

        self.setObjectName('ecommerceTaskCard')
        self.setBorderRadius(12)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumHeight(_base_px(170))
        root = QHBoxLayout(self)
        root.setContentsMargins(_base_px(18), _base_px(16), _base_px(18), _base_px(16))
        root.setSpacing(_base_px(12))

        self.checkbox = CheckBox(self)
        self.checkbox.setFixedSize(_base_px(18), _base_px(18))
        self.checkbox.setToolTip(tr('选择该记录'))
        self.checkbox.toggled.connect(self._on_checkbox)
        self.checkbox.setVisible(False)
        root.addWidget(self.checkbox, 0, Qt.AlignVCenter)

        self.index_label = _IndexBox(self)
        root.addWidget(self.index_label, 0, Qt.AlignVCenter)

        self.thumb_host = QWidget(self)
        thumb_layout = QHBoxLayout(self.thumb_host)
        thumb_layout.setContentsMargins(0, 0, 0, 0)
        thumb_layout.setSpacing(_base_px(6))
        self.thumb_labels = [_Thumbnail(self.thumb_host) for _ in range(3)]
        for thumb in self.thumb_labels:
            thumb_layout.addWidget(thumb)
        root.addWidget(self.thumb_host, 0, Qt.AlignVCenter)

        self.content_host = QWidget(self)
        self.content_layout = QBoxLayout(QBoxLayout.LeftToRight, self.content_host)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(_base_px(12))
        info = QVBoxLayout()
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(_base_px(4))
        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(_base_px(8))
        self.time_label = _label(12)
        self.time_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.status_badge = QLabel(self)
        self.status_badge.setTextFormat(Qt.PlainText)
        self.status_badge.setAlignment(Qt.AlignCenter)
        top_row.addWidget(self.time_label, 1)
        top_row.addWidget(self.status_badge, 0)
        info.addLayout(top_row)
        self.name_label = _label(15, bold=True)
        self.name_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        info.addWidget(self.name_label)
        self.desc_label = _label(12, wrap=True)
        self.desc_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.desc_label.installEventFilter(self)
        metrics = self.desc_label.fontMetrics()
        self.desc_label.setMaximumHeight(metrics.lineSpacing() * 2 + 2)
        info.addWidget(self.desc_label)
        chips_host = QWidget(self)
        chips = WrapLayout(chips_host, spacing=4)
        self.chip_host = chips_host
        self.chip_layout = chips
        self.platform_chips = []
        info.addWidget(chips_host)
        output_row = QHBoxLayout()
        output_row.setContentsMargins(0, 0, 0, 0)
        output_row.setSpacing(_base_px(8))
        self.counts_label = _label(12)
        self.counts_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.model_label = QLabel(self)
        self.model_label.setTextFormat(Qt.PlainText)
        self.model_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.model_label.setMinimumWidth(0)
        output_row.addWidget(self.counts_label, 1)
        output_row.addWidget(self.model_label, 0)
        info.addLayout(output_row)
        self.content_layout.addLayout(info, 1)

        self.actions_host = QWidget(self.content_host)
        self.actions_layout = QBoxLayout(QBoxLayout.TopToBottom, self.actions_host)
        self.actions_layout.setContentsMargins(0, 0, 0, 0)
        self.actions_layout.setSpacing(_base_px(6))
        self.view_button = PushButton(tr('查看结果'), self)
        self.preview_button = PushButton(tr('平台预览'), self)
        self.delete_button = PushButton(tr('删除'), self)
        for button in (self.view_button, self.preview_button, self.delete_button):
            button.setFixedSize(_base_px(88), _base_px(30))
            self.actions_layout.addWidget(button)
        self.actions_host.setFixedSize(_base_px(88), self.actions_host.sizeHint().height())
        self.content_layout.addWidget(self.actions_host, 0, Qt.AlignRight)
        root.addWidget(self.content_host, 1)

        self.context_menu = RoundMenu(parent=self)
        self.resume_action = Action(tr('继续生成（断点恢复）'), self.context_menu)
        self.open_action = Action(tr('打开文件夹'), self.context_menu)
        self.context_menu.addAction(self.resume_action)
        self.context_menu.addAction(self.open_action)
        self.resume_action.triggered.connect(
            lambda _checked=False: self.panel.resume_requested.emit(self.group_id))
        self.open_action.triggered.connect(
            lambda _checked=False: self.panel.open_requested.emit(self.group_id))

        self.view_button.clicked.connect(
            lambda: self.panel.view_requested.emit(self.group_id))
        self.preview_button.clicked.connect(
            lambda: self.panel.preview_requested.emit(self.group_id))
        self.delete_button.clicked.connect(
            lambda: self.panel.request_delete([self.group_id]))
        self.setContextMenuPolicy(Qt.DefaultContextMenu)

        self.update_group(group, index=index)
        # 新卡片会先被全局缩放器处理，再按最终字体度量校正两行上限。
        self._description_timer.start(0)

    def eventFilter(self, watched, event):
        if (watched is getattr(self, 'desc_label', None)
                and event.type() in (QEvent.FontChange, QEvent.StyleChange, QEvent.Polish)):
            self._description_timer.start(0)
        return super().eventFilter(watched, event)

    def _sync_description_height(self):
        maximum = self.desc_label.fontMetrics().lineSpacing() * 2 + px(2)
        if self.desc_label.maximumHeight() != maximum:
            self.desc_label.setMaximumHeight(maximum)

    # -- 数据 ---------------------------------------------------------
    def update_group(self, group, index=None):
        if index is not None:
            self.index = int(index)
        index_text = str(self.index + 1)
        if self.index_label.text() != index_text:
            self.index_label.setText(index_text)
        incoming = dict(group or {})
        if self._data_initialized and incoming == self.group:
            return
        self.group = deepcopy(incoming)
        self._data_initialized = True
        self.group_id = str(self.group.get('group_id') or self.group_id)
        self.time_label.setText(_created_text(self.group))
        self.time_label.setToolTip(self.time_label.text())

        name = str(self.group.get('product_name') or '').strip()
        if not name:
            name = self.group_id[:8]
        self.name_label.setText(name)
        self.name_label.setToolTip(name)

        self.desc_label.setText(_display_description(self.group))
        self.desc_label.setToolTip(_product_info(self.group)[:300])

        names = [_platform_name(item) for item in (self.group.get('platforms') or ())]
        names = [name for name in names if name]
        if not names:
            names = [tr('未选平台')]
        if getattr(self, '_chip_names', None) != names:
            for chip in self.platform_chips:
                self.chip_layout.removeWidget(chip)
                chip.deleteLater()
            self.platform_chips = []
            for name in names:
                chip = QLabel(name, self.chip_host)
                chip.setTextFormat(Qt.PlainText)
                font = chip.font()
                font.setFamily(materials.ui_font_family())
                font.setPixelSize(11)
                chip.setFont(font)
                _style_chip(chip)
                self.chip_layout.addWidget(chip)
                self.platform_chips.append(chip)
            self._chip_names = list(names)

        carousel, detail = _group_output_counts(self.group)
        parts = []
        if carousel is not None:
            parts.append(tr('轮播图 {count} 张').format(count=carousel))
        if detail is not None:
            parts.append(tr('详情图 {count} 张').format(count=detail))
        self.counts_label.setText('  '.join(parts))
        self.counts_label.setVisible(bool(parts))

        model = _group_model(self.group)
        self.model_label.setText(model or tr('未选模型'))
        self.model_label.setToolTip(model)

        thumbs = _group_thumbs(self.group)
        for slot, thumb in enumerate(self.thumb_labels):
            thumb.set_source(thumbs[slot] if slot < len(thumbs) else '')

        self._refresh_status()
        self._description_timer.start(0)
        self.set_busy(self.panel.busy)
        theme = materials.THEME_ID
        if not self._styled or theme != self._theme_id:
            self.refresh_colors()
        self._styled = True
        self._theme_id = theme

    def retranslate(self):
        """语言切换时刷新卡片内的固定文案（数据文案由 update_group 刷新）。"""
        self.view_button.setText(tr('查看结果'))
        self.preview_button.setText(tr('平台预览'))
        self.delete_button.setText(tr('删除'))
        self.checkbox.setToolTip(tr('选择该记录'))
        self.resume_action.setText(tr('继续生成（断点恢复）'))
        self.open_action.setText(tr('打开文件夹'))
        self._data_initialized = False
        self.update_group(self.group)
        for thumb in self.thumb_labels:
            thumb.refresh()

    def _refresh_status(self):
        status = self.group.get('status')
        error = str(self.group.get('error') or '')
        interrupted = tr('上次运行未正常结束（程序中断）')
        if error == interrupted and status in ('failed', 'analyzing', 'generating'):
            text = interrupted
        else:
            text = {
                'succeeded': tr('已完成'),
                'generating': tr('生成中'),
                'analyzing': tr('分析中'),
                'partial': tr('部分·失败'),
                'failed': tr('出错'),
                'cancelled': tr('已取消'),
            }.get(status, str(status or ''))
        self.status_label = self.status_badge
        self.status_badge.setText(str(text))
        self._tone = _status_tone(status)
        background = {
            'succeeded': materials.ecommerce_color('task_done_bg'),
            'generating': materials.ecommerce_color('task_active_bg'),
            'analyzing': materials.ecommerce_color('task_active_bg'),
            'partial': materials.ecommerce_color('task_issue_bg'),
            'failed': materials.ecommerce_color('task_failed_bg'),
            'cancelled': materials.ecommerce_color('task_cancelled_bg'),
        }.get(status, materials.ecommerce_color('task_cancelled_bg'))
        self.status_badge.setStyleSheet(
            'color:%s; background:%s; border-radius:%dpx; padding:%dpx %dpx;'
            ' font-size:%dpx; font-weight:600;'
            % (self._tone, background, px(6), px(3), px(8), px(11)))

    # -- 状态 ---------------------------------------------------------
    @property
    def thumb_count(self):
        return sum(1 for thumb in self.thumb_labels if thumb.has_image)

    @property
    def thumbs(self):
        return [thumb.path for thumb in self.thumb_labels if thumb.has_image]

    @property
    def selected(self):
        return self._selected

    def set_selected(self, selected):
        selected = bool(selected)
        if selected == self._selected:
            return
        self._selected = selected
        self.setProperty('selected', selected)
        self.checkbox.blockSignals(True)
        self.checkbox.setChecked(selected)
        self.checkbox.blockSignals(False)
        self.update()

    def set_busy(self, busy):
        busy = bool(busy)
        for button in (self.view_button, self.preview_button, self.delete_button):
            button.setEnabled(not busy)
        self.checkbox.setEnabled(not busy)
        self.resume_action.setEnabled(
            not busy and self.group.get('status') in RESUMABLE_STATUSES)
        self.open_action.setEnabled(not busy)

    def _on_checkbox(self, checked):
        self.set_selected(bool(checked))
        self.panel._set_group_selected(self.group_id, bool(checked))

    def context_actions(self):
        return [tr('继续生成（断点恢复）'), tr('打开文件夹')]

    def trigger_context_action(self, name):
        name = str(name or '')
        if name == tr('继续生成（断点恢复）') and self.resume_action.isEnabled():
            self.resume_action.trigger()
            return True
        if name == tr('打开文件夹') and self.open_action.isEnabled():
            self.open_action.trigger()
            return True
        return False

    def contextMenuEvent(self, event):
        self.context_menu.exec_(event.globalPos())

    def resizeEvent(self, event):
        host = self.panel.window()
        width = host.width() if host is not None else self.panel.width()
        narrow = width < px(1080)
        if narrow != self._narrow_actions:
            self._narrow_actions = narrow
            if narrow:
                self.content_layout.setDirection(QBoxLayout.TopToBottom)
                self.actions_layout.setDirection(QBoxLayout.LeftToRight)
                self.actions_host.setMinimumWidth(0)
                self.actions_host.setMaximumWidth(16777215)
                self.actions_host.setFixedHeight(px(30))
                self.content_layout.setAlignment(self.actions_host, Qt.AlignRight)
            else:
                self.content_layout.setDirection(QBoxLayout.LeftToRight)
                self.actions_layout.setDirection(QBoxLayout.TopToBottom)
                self.actions_host.setFixedSize(px(88), self.actions_host.sizeHint().height())
            self.content_layout.invalidate()
        super().resizeEvent(event)

    # -- 外观 ---------------------------------------------------------
    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            radius = px(12)
            rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
            path = QPainterPath()
            path.addRoundedRect(rect, radius, radius)
            accent = QColor(*materials.accent_rgb())
            if self._selected:
                fill = QColor(accent)
                fill.setAlpha(20)
                border = accent
            else:
                fill = materials.surface_fill(1 if self._hover else 0)
                border = materials.surface_border(1 if self._hover else 0)
            if materials.is_light():
                fill = QColor(fill)
                fill.setAlpha(max(fill.alpha(), 210))
            painter.fillPath(path, fill)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(border, 1))
            painter.drawRoundedRect(rect, radius, radius)
        finally:
            painter.end()

    def refresh_colors(self):
        colors = materials.palette()
        for label in (self.time_label, self.counts_label):
            label.setStyleSheet('color:%s; background:transparent;' % colors['text2'])
        self.name_label.setStyleSheet('color:%s; background:transparent;' % colors['text1'])
        self.desc_label.setStyleSheet('color:%s; background:transparent;' % colors['text2'])
        self.index_label.refresh_colors()
        for thumb in self.thumb_labels:
            thumb.refresh()
        for chip in self.platform_chips:
            _style_chip(chip)
        self.model_label.setStyleSheet(
            'color:%s; background:%s; border-radius:%dpx; padding:%dpx %dpx;'
            % (colors['accent'], materials.ecommerce_color('tag_bg'), 8, 1, 7))
        _style_outline(self.view_button, colors['accent'])
        _style_outline(self.preview_button, colors['text2'])
        _style_outline(self.delete_button, colors['error'])
        self._tone = _status_tone(self.group.get('status'))
        self.status_badge.setStyleSheet(
            'color:%s; background:%s; border-radius:%dpx; padding:%dpx %dpx;'
            ' font-size:%dpx; font-weight:600;'
            % (self._tone,
               {
                   'succeeded': materials.ecommerce_color('task_done_bg'),
                   'generating': materials.ecommerce_color('task_active_bg'),
                   'analyzing': materials.ecommerce_color('task_active_bg'),
                   'partial': materials.ecommerce_color('task_issue_bg'),
                   'failed': materials.ecommerce_color('task_failed_bg'),
                   'cancelled': materials.ecommerce_color('task_cancelled_bg'),
               }.get(self.group.get('status'), materials.ecommerce_color('task_cancelled_bg')),
               px(6), px(3), px(8), px(11)))
        self._styled = True
        self._theme_id = materials.THEME_ID
        self.update()


# ---------------------------------------------------------------------------
# 按钮 / 底栏 / tab 的样式

def _style_chip(chip):
    colors = materials.palette()
    chip.setStyleSheet(
        'color:%s; background:%s; border-radius:%dpx; padding:%dpx %dpx;'
        % (colors['text2'], materials.ecommerce_color('chip_bg'),
           10, 1, 7))

def _style_outline(button, color):
    """描边按钮：蓝=查看、灰=预览、红=删除；颜色全部来自主题。"""
    value = QColor(color)
    hover = _rgba(value, 28)
    disabled = _rgba(value, 90)
    button.setStyleSheet(
        'QPushButton {border:1px solid %s; border-radius:%dpx; background:transparent;'
        ' color:%s; padding:0 %dpx; font-size:%dpx;}'
        'QPushButton:hover {background:%s;}'
        'QPushButton:disabled {color:%s; border-color:%s;}'
        % (value.name(), 8, value.name(), 10, 12, hover,
           disabled, _rgba(value, 60)))


def _style_danger_primary(button):
    colors = materials.palette()
    error = QColor(colors['error'])
    hover = error.lighter(112)
    button.setStyleSheet(
        'QPushButton {border:1px solid %s; border-radius:%dpx; background:%s;'
        ' color:%s; padding:0 %dpx; font-size:%dpx; font-weight:600;}'
        'QPushButton:hover {background:%s;}'
        'QPushButton:disabled {background:%s; border-color:%s; color:%s;}'
        % (error.name(), 8, error.name(), materials.ecommerce_color('preview_white'),
           14, 13, hover.name(), _rgba(error, 60),
           _rgba(error, 80), _rgba(error, 120)))


class _TabButton(QWidget):
    """筛选 tab：名称 + 计数；active 时主题色浅底。"""

    clicked = pyqtSignal(str)

    def __init__(self, key, parent=None):
        super().__init__(parent)
        self.key = key
        self.active = False
        self.setObjectName('ecommerceTabButton')
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(px(14), 0, px(14), 0)
        layout.setSpacing(px(8))
        self.icon = _TabIcon(key, self)
        layout.addWidget(self.icon)
        self.name_label = _label(13, bold=False)
        self.count_label = _label(13, bold=True)
        layout.addWidget(self.name_label)
        layout.addWidget(self.count_label)
        self.set_active(False)

    def set_text(self, name, count):
        self.name_label.setText(str(name))
        self.count_label.setText(str(count))

    def set_active(self, active, force=False):
        active = bool(active)
        if active == self.active and not force:
            return
        self.active = active
        self.setProperty('active', self.active)
        self.style().unpolish(self)
        self.style().polish(self)
        colors = materials.palette()
        accent = QColor(colors['accent'])
        if self.active:
            self.setStyleSheet(
                '#ecommerceTabButton {background:%s; border:%dpx solid %s; border-radius:%dpx;}'
                % (_rgba(accent, 20), px(1), accent.name(), px(8)))
            for label in (self.name_label, self.count_label):
                label.setStyleSheet('color:%s; background:transparent;' % accent.name())
        else:
            self.setStyleSheet(
                '#ecommerceTabButton {background:%s; border:%dpx solid %s; border-radius:%dpx;}'
                % (materials.ecommerce_color('single_card'), px(1),
                   materials.ecommerce_color('task_border'), px(8)))
            self.name_label.setStyleSheet('color:%s; background:transparent;'
                                          % colors['text2'])
            self.count_label.setStyleSheet('color:%s; background:transparent;'
                                           % accent.name())
        self.icon.set_active(self.active)

    def resizeEvent(self, event):
        self.setFixedHeight(px(40))
        super().resizeEvent(event)

    def click(self):
        self.clicked.emit(self.key)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit(self.key)
        super().mouseReleaseEvent(event)


class _TabIcon(QWidget):
    """Painter icons avoid platform-dependent emoji glyph rendering."""

    def __init__(self, key, parent=None):
        super().__init__(parent)
        self.key = key
        self.setProperty('tabIcon', key)
        self.setFixedSize(px(16), px(16))
        self.active = False

    def set_active(self, active):
        self.active = bool(active)
        self.setProperty('active', self.active)
        self.update()

    def paintEvent(self, event):
        colors = materials.palette()
        color = QColor(colors['accent'] if self.active else colors['text2'])
        if not self.active and self.key == 'done':
            color = QColor(materials.ecommerce_color('task_done'))
        elif not self.active and self.key == 'issue':
            color = QColor(materials.ecommerce_color('task_issue'))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(color, max(1, px(1.5)), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush)
        bounds = QRectF(px(2), px(1), px(12), px(14))
        if self.key == 'all':
            painter.drawRoundedRect(bounds, px(2), px(2))
            painter.drawLine(px(5), px(5), px(11), px(5))
            painter.drawLine(px(5), px(8), px(11), px(8))
            painter.drawLine(px(5), px(11), px(9), px(11))
        elif self.key == 'generating':
            painter.drawArc(QRectF(px(3), px(2), px(10), px(12)), 0, 180 * 16)
            painter.drawArc(QRectF(px(3), px(2), px(10), px(12)), 180 * 16, 180 * 16)
            painter.drawLine(px(5), px(4), px(11), px(4))
            painter.drawLine(px(5), px(12), px(11), px(12))
        elif self.key == 'done':
            painter.drawRect(bounds)
            painter.drawLine(px(4), px(8), px(7), px(11))
            painter.drawLine(px(7), px(11), px(12), px(5))
        else:
            triangle = QPainterPath()
            triangle.moveTo(px(8), px(1))
            triangle.lineTo(px(15), px(14))
            triangle.lineTo(px(1), px(14))
            triangle.closeSubpath()
            painter.drawPath(triangle)
            painter.drawLine(px(8), px(5), px(8), px(9))
            painter.drawPoint(px(8), px(12))
        painter.end()


class _GlassBar(CardWidget):
    """底部管理操作条：主题玻璃表面 + 顶部分隔线。"""

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            materials.paint_surface(self, painter)
            line = QColor(materials.palette()['text1'])
            line.setAlpha(24)
            painter.setPen(QPen(line, 1))
            painter.drawLine(px(12), 1, self.width() - px(12), 1)
        finally:
            painter.end()


# ---------------------------------------------------------------------------
# 任务列表主组件

class EcommerceTaskList(QWidget):
    refresh_requested = pyqtSignal()
    view_requested = pyqtSignal(str)
    resume_requested = pyqtSignal(str)
    open_requested = pyqtSignal(str)
    delete_requested = pyqtSignal(object)
    batch_create_requested = pyqtSignal()
    new_create_requested = pyqtSignal()
    preview_requested = pyqtSignal(str)

    def __init__(self, stage_text, parent=None):
        super().__init__(parent)
        self.stage_text = stage_text if callable(stage_text) else (lambda status: str(status or ''))
        self._groups = []
        self._selected = set()
        self._tab = 'all'
        self._manage_mode = False
        self._busy = False
        self._persistence_available = True
        self._groups_signature = ()
        self._chrome_signature = None
        self.cards = OrderedDict()
        self._row_widgets = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(px(12))

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(px(12))
        titles = QVBoxLayout()
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(px(2))
        self.title_label = StrongBodyLabel(tr('电商图任务'), self)
        self.subtitle_label = _label(13)
        titles.addWidget(self.title_label)
        titles.addWidget(self.subtitle_label)
        header.addLayout(titles, 1)
        self.manage_button = PushButton(tr('管理'), self)
        self.cancel_button = PushButton(tr('取消'), self)
        self.batch_create_button = PushButton(tr('批量制作'), self)
        self.new_create_button = PrimaryPushButton(tr('+ 新建制作'), self)
        self.cancel_button.setVisible(False)
        self.manage_button.clicked.connect(lambda: self.set_manage_mode(True))
        self.cancel_button.clicked.connect(lambda: self.set_manage_mode(False))
        self.batch_create_button.clicked.connect(self.batch_create_requested)
        self.new_create_button.clicked.connect(self.new_create_requested)
        for button in (self.manage_button, self.cancel_button, self.batch_create_button,
                       self.new_create_button):
            button.setFixedHeight(px(36))
            header.addWidget(button, 0, Qt.AlignVCenter)
        root.addLayout(header)

        self.notice_label = _label(12, wrap=True)
        self.notice_label.setText(tr('数据库不可用，仅展示本次会话结果'))
        self.notice_label.setVisible(False)
        root.addWidget(self.notice_label)

        tabs = QHBoxLayout()
        tabs.setContentsMargins(0, 0, 0, 0)
        tabs.setSpacing(px(12))
        self.tab_buttons = OrderedDict()
        for key in TAB_KEYS:
            tab = _TabButton(key, self)
            tab.clicked.connect(self.set_tab)
            self.tab_buttons[key] = tab
            tabs.addWidget(tab)
        tabs.addStretch(1)
        root.addLayout(tabs)

        self.rows_host = QWidget(self)
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(px(12))
        # 少量卡片也保持自然高度，不把视口剩余高度平均摊进各行。
        self.rows_layout.setAlignment(Qt.AlignTop)
        self.empty_label = _label(13, wrap=True)
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setContentsMargins(0, 28, 0, 28)
        self.rows_layout.addWidget(self.empty_label)
        # 卡片区独立滚动：底部批量操作条固定不随列表滚动。
        self.rows_scroll = QScrollArea(self)
        self.rows_scroll.setWidgetResizable(True)
        self.rows_scroll.setFrameShape(QFrame.NoFrame)
        self.rows_scroll.setStyleSheet('QScrollArea{background:transparent;border:0;}')
        self.rows_scroll.setWidget(self.rows_host)
        self.rows_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        root.addWidget(self.rows_scroll, 1)

        self.bottom_bar = _GlassBar(self)
        bar_layout = QHBoxLayout(self.bottom_bar)
        bar_layout.setContentsMargins(px(16), px(10), px(16), px(10))
        bar_layout.setSpacing(px(10))
        self.bottom_bar.setFixedHeight(px(52))
        self.selected_label = _label(12)
        self.select_page_button = PushButton(tr('全选本页'), self.bottom_bar)
        self.clear_selection_button = PushButton(tr('清空'), self.bottom_bar)
        self.delete_selected_button = PrimaryPushButton(tr('删除 {count} 个').format(count=0),
                                                        self.bottom_bar)
        self.select_page_button.clicked.connect(self._select_current_page)
        self.clear_selection_button.clicked.connect(self._clear_selection)
        self.delete_selected_button.clicked.connect(self._delete_selected)
        bar_layout.addWidget(self.selected_label)
        bar_layout.addStretch(1)
        bar_layout.addWidget(self.select_page_button)
        bar_layout.addWidget(self.clear_selection_button)
        bar_layout.addWidget(self.delete_selected_button)
        self.bottom_bar.setVisible(False)
        root.addWidget(self.bottom_bar)

        style_controls(self)
        _style_danger_primary(self.delete_selected_button)
        self._retranslate_tabs()
        self._render()

    # -- 只读状态 -----------------------------------------------------
    @property
    def busy(self):
        return self._busy

    @property
    def manage_mode(self):
        return self._manage_mode

    @property
    def current_tab(self):
        return self._tab

    @current_tab.setter
    def current_tab(self, key):
        self.set_tab(key)

    @property
    def selection_total(self):
        return len(self._groups)

    def filtered_groups(self):
        statuses = TAB_STATUSES.get(self._tab)
        if statuses is None:
            return list(self._groups)
        return [group for group in self._groups if group.get('status') in statuses]

    def page_groups(self):
        return self.filtered_groups()[:MAX_GROUPS]

    def groups(self):
        return list(self._groups)

    def selected_ids(self):
        known = [str(group['group_id']) for group in self._groups]
        return [group_id for group_id in known if group_id in self._selected]

    def card_for(self, group_id):
        return self.cards.get(str(group_id))

    # -- 对外操作 -----------------------------------------------------
    def update_groups(self, groups, persistence_available=True):
        normalized = []
        for group in groups or ():
            if isinstance(group, dict) and group.get('group_id'):
                normalized.append(dict(group))
        normalized = normalized[:MAX_GROUPS]
        # Detect late edits that reuse an updated_at value while retaining
        # the existing card widgets and their asynchronous thumbnail state.
        signature = tuple((str(group.get('group_id')), repr(group))
                          for group in normalized)
        self._persistence_available = bool(persistence_available)
        self.notice_label.setVisible(not self._persistence_available)
        if signature == self._groups_signature:
            return
        self._groups = normalized
        valid = {str(group['group_id']) for group in normalized}
        self._selected &= valid
        self._render()
        self._groups_signature = signature

    def set_busy(self, busy):
        self._busy = bool(busy)
        for card in self.cards.values():
            card.set_busy(self._busy)
        self._sync_bottom_bar()

    def set_manage_mode(self, enabled):
        enabled = bool(enabled)
        if enabled == self._manage_mode:
            return
        self._manage_mode = enabled
        if not enabled:
            self._selected.clear()
        self.manage_button.setVisible(not enabled)
        self.cancel_button.setVisible(enabled)
        self.bottom_bar.setVisible(enabled)
        for card in self.cards.values():
            card.checkbox.setVisible(enabled)
            card.set_selected(card.group_id in self._selected)
        self._sync_bottom_bar()
        self.updateGeometry()

    def set_tab(self, key):
        key = str(key or '')
        if key not in TAB_STATUSES or key == self._tab:
            return
        self._tab = key
        self._render()

    def refresh_colors(self):
        """主题切换后整体刷新：面板 chrome + 可见卡片 + tab 强制重刷。"""
        self._refresh_chrome()
        for tab in self.tab_buttons.values():
            tab.set_active(tab.key == self._tab, force=True)
        for card in self.cards.values():
            card.refresh_colors()

    def _refresh_chrome(self):
        """面板自身（非卡片）的颜色/样式；高频刷新时只走这条轻量路径。"""
        colors = materials.palette()
        self.title_label.setStyleSheet('color:%s; background:transparent;' % colors['text1'])
        self.subtitle_label.setStyleSheet('color:%s; background:transparent;' % colors['text2'])
        self.notice_label.setStyleSheet('color:%s; background:transparent;' % colors['warning'])
        self.empty_label.setStyleSheet('color:%s; background:transparent;' % colors['text2'])
        self.selected_label.setStyleSheet('color:%s; background:transparent;' % colors['text1'])
        _style_danger_primary(self.delete_selected_button)
        self.bottom_bar.update()
        self._chrome_signature = self._chrome_key()

    def _chrome_key(self):
        return (materials.THEME_ID, tuple(materials.palette().items()),
                px(8), px(12), px(13), px(14))

    def retranslate_ui(self):
        """原地更新可见文案，保留卡片与异步缩略图。"""
        try:
            self.title_label.setText(tr('电商图任务'))
            self.manage_button.setText(tr('管理'))
            self.cancel_button.setText(tr('取消'))
            self.batch_create_button.setText(tr('批量制作'))
            self.new_create_button.setText(tr('+ 新建制作'))
            self.notice_label.setText(tr('数据库不可用，仅展示本次会话结果'))
            self.select_page_button.setText(tr('全选本页'))
            self.clear_selection_button.setText(tr('清空'))
            self.empty_label.setText(self._empty_text())
            self._retranslate_tabs()
            for card in self.cards.values():
                card.retranslate()
            self.subtitle_label.setText(tr('{count} 个制作任务').format(count=len(self._groups)))
            self._sync_bottom_bar()
        except RuntimeError:
            return

    def refresh_interface_scale(self):
        """复用已缩放的卡片，只更新依赖最终字体度量和像素尺寸的内容。"""
        try:
            # InterfaceScaler 已调整现有控件的字体和布局。此时重建会让
            # px() 产生的新尺寸再次被捕获、缩放，且丢失原有卡片引用。
            for card in self.cards.values():
                card._sync_description_height()
                for thumb in card.thumb_labels:
                    thumb.refresh()
            self.rows_layout.invalidate()
        except RuntimeError:
            return

    # -- 内部：渲染 ---------------------------------------------------
    def _empty_text(self):
        if not self._groups:
            return tr('暂无任务，点「+ 新建制作」开始')
        return tr('当前筛选下暂无任务')

    def _tab_name(self, key):
        return {
            'all': tr('全部'),
            'generating': tr('生成中'),
            'done': tr('已完成'),
            'issue': tr('部分/失败'),
        }.get(key, key)

    def _retranslate_tabs(self):
        for key, tab in self.tab_buttons.items():
            tab.name_label.setText(self._tab_name(key))

    def _tab_counts(self):
        counts = {key: 0 for key in TAB_KEYS}
        for group in self._groups:
            status = group.get('status')
            counts['all'] += 1
            for key in ('generating', 'done', 'issue'):
                if status in (TAB_STATUSES[key] or ()):
                    counts[key] += 1
        return counts

    def _render(self):
        subtitle = tr('{count} 个制作任务').format(count=len(self._groups))
        if self.subtitle_label.text() != subtitle:
            self.subtitle_label.setText(subtitle)
        counts = self._tab_counts()
        for key, tab in self.tab_buttons.items():
            tab.set_text(tab.name_label.text(), counts[key])
            tab.set_active(key == self._tab)

        notice = not self._persistence_available
        self.notice_label.setVisible(notice)
        if notice:
            self.notice_label.setText(tr('数据库不可用，仅展示本次会话结果'))

        groups = self.page_groups()
        ids = tuple(str(group['group_id']) for group in groups)
        structure = (self._tab, ids)
        if (structure != getattr(self, '_structure', None)
                or len(self.cards) != len(ids)
                or any(card_id not in self.cards for card_id in ids)):
            self._structure = structure
            self._rebuild_cards(groups)
        else:
            self._update_cards(groups)

        empty_text = self._empty_text()
        if self.empty_label.text() != empty_text:
            self.empty_label.setText(empty_text)
        self.empty_label.setVisible(not groups)

        self._sync_bottom_bar()
        if self._chrome_signature != self._chrome_key():
            self._refresh_chrome()

    def _rebuild_cards(self, groups):
        updates_enabled = self.updatesEnabled()
        self.setUpdatesEnabled(False)
        try:
            while self._row_widgets:
                widget = self._row_widgets.pop()
                self.rows_layout.removeWidget(widget)
                widget.hide()
                widget.deleteLater()
            self.cards = OrderedDict()
            for offset, group in enumerate(groups):
                card = EcommerceTaskCard(group, self, index=offset)
                card.set_busy(self._busy)
                card.checkbox.setVisible(self._manage_mode)
                card.set_selected(card.group_id in self._selected)
                self.rows_layout.addWidget(card)
                self._row_widgets.append(card)
                self.cards[card.group_id] = card
        finally:
            self.setUpdatesEnabled(updates_enabled)
            self.update()

    def _update_cards(self, groups):
        """可见卡片集合不变：原地更新内容，保留卡片 / 缩略图 / 引用。"""
        updates_enabled = self.updatesEnabled()
        self.setUpdatesEnabled(False)
        try:
            for offset, group in enumerate(groups):
                group_id = str(group['group_id'])
                card = self.cards.get(group_id)
                if card is None:
                    continue
                card.update_group(dict(group), index=offset)
                card.checkbox.setVisible(self._manage_mode)
                card.set_selected(group_id in self._selected)
        finally:
            self.setUpdatesEnabled(updates_enabled)
            self.update()

    def resizeEvent(self, event):
        # Cards may be constructed before the top-level window is shown; a
        # panel resize gives them the final width needed for the narrow layout.
        for card in self.cards.values():
            card.resize(card.size())
        super().resizeEvent(event)

    def _sync_bottom_bar(self):
        selected = len(self.selected_ids())
        self.selected_label.setText(
            tr('已选 {selected} / {total}').format(selected=selected,
                                                   total=self.selection_total))
        self.delete_selected_button.setText(
            tr('删除 {count} 个').format(count=selected))
        self.delete_selected_button.setEnabled(not self._busy and selected > 0)
        self.select_page_button.setEnabled(not self._busy)
        self.clear_selection_button.setEnabled(not self._busy and selected > 0)

    # -- 内部：选择 ---------------------------------------------------
    def _set_group_selected(self, group_id, checked):
        group_id = str(group_id)
        if checked:
            if any(str(group['group_id']) == group_id for group in self._groups):
                self._selected.add(group_id)
        else:
            self._selected.discard(group_id)
        card = self.cards.get(group_id)
        if card is not None and card.selected != bool(checked):
            card.set_selected(checked)
        self._sync_bottom_bar()

    def _select_current_page(self):
        if self._busy:
            return
        for group in self.page_groups():
            group_id = str(group['group_id'])
            self._selected.add(group_id)
            card = self.cards.get(group_id)
            if card is not None:
                card.set_selected(True)
        self._sync_bottom_bar()

    def _clear_selection(self):
        if self._busy:
            return
        self._selected.clear()
        for card in self.cards.values():
            card.set_selected(False)
        self._sync_bottom_bar()

    # -- 内部：删除 ---------------------------------------------------
    def _delete_selected(self):
        if self._busy:
            return
        self.request_delete(self.selected_ids())

    def request_delete(self, group_ids):
        """统一删除入口：先确认，再一次 emit；测试可 patch confirm_delete。"""
        ids = [str(group_id) for group_id in (group_ids or ()) if group_id]
        if self._busy or not ids:
            return False
        self._pending_delete_name = ''
        if len(ids) == 1:
            group = next((item for item in self._groups
                          if str(item.get('group_id')) == ids[0]), None) or {}
            self._pending_delete_name = (str(group.get('product_name') or '').strip()
                                         or ids[0][:8])
        if not self.confirm_delete(len(ids)):
            return False
        self._selected -= set(ids)
        for card in self.cards.values():
            if card.group_id in set(ids):
                card.set_selected(False)
        self._sync_bottom_bar()
        self.delete_requested.emit(list(ids))
        return True

    def _build_delete_dialog(self, count, name=''):
        """构建确认弹窗（不 exec）；测试可直接检查文案（与参考软件逐字一致）。"""
        count = max(1, int(count))
        dialog = StudioDialog(self, tr('删除记录'))
        dialog.resize(px(420), px(210))
        colors = materials.palette()
        if count > 1:
            message = tr('确认删除选中的 {count} 个任务？').format(count=count)
        elif str(name or '').strip():
            message = tr('确认删除任务「{name}」？').format(name=str(name).strip())
        else:
            message = tr('删除该记录？')
        first = _label(15, bold=True, wrap=True, text=message)
        second = _label(12, wrap=True,
                        text=tr('此操作不可恢复，生成完成的图片不受影响'))
        dialog.body_layout.addWidget(first)
        dialog.body_layout.addWidget(second)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        actions.addStretch(1)
        cancel_button = PushButton(tr('取消'), dialog)
        confirm_button = PrimaryPushButton(tr('删除'), dialog)
        cancel_button.clicked.connect(dialog.reject)
        actions.addWidget(cancel_button)
        actions.addWidget(confirm_button)
        dialog.body_layout.addLayout(actions)
        style_controls(dialog)
        _style_danger_primary(confirm_button)
        first.setStyleSheet('color:%s; background:transparent;' % colors['text1'])
        second.setStyleSheet('color:%s; background:transparent;' % colors['text2'])
        dialog.delete_message_labels = (first, second)
        dialog.delete_confirm_button = confirm_button
        dialog.delete_cancel_button = cancel_button
        return dialog, confirm_button, cancel_button

    def confirm_delete(self, count):
        """删除确认；默认弹 StudioDialog，测试可整体 patch 成本方法。"""
        dialog, confirm_button, _cancel = self._build_delete_dialog(
            count, getattr(self, '_pending_delete_name', ''))
        confirm_button.clicked.connect(dialog.accept)
        try:
            return dialog.exec_() == dialog.Accepted
        finally:
            safe_delete_dialog(dialog)
