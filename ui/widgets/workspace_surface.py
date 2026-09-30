"""Workspace-only materials and image interactions; other pages keep their theme."""
import math
import time
from functools import lru_cache
from pathlib import Path

from PyQt5.QtCore import Qt, QRectF, QSize, QPoint, QPointF, QMimeData, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap, QImageReader, QDrag, QLinearGradient, QRadialGradient
from PyQt5.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QSizePolicy
from ..components.studio_dialog import StudioDialog
from core.i18n import tr
from qfluentwidgets import (CardWidget, CaptionLabel, StrongBodyLabel, ImageLabel,
                            PushButton, TransparentToolButton, ScrollArea, FluentIcon as FIF, Theme)
from ..motion import StatusDot, WidgetMotion

BLUE = '#3b82f6'
GREEN = '#22c55e'
RED = '#ef4444'
YELLOW = '#f59e0b'
MUTED = '#9ca3af'


def label(text='', size=12, color=MUTED, bold=False, mono=False):
    from ..materials import map_text_color, register_label
    widget = StrongBodyLabel(text) if bold else CaptionLabel(text)
    widget.setProperty('studioStyled', True)
    font = widget.font(); font.setPixelSize(size); font.setBold(bold)
    if mono:
        font.setFamily('Cascadia Mono')
    widget.setFont(font)
    display = map_text_color(color)
    name = display.name() if isinstance(display, QColor) else str(display)
    widget.setTextColor(QColor(name), QColor(name))
    widget.setStyleSheet(f'background:transparent; color:{name};')
    register_label(widget, color)
    return widget


def _apply_button_style(button, primary=False):
    from ..materials import is_light, palette
    light = is_light()
    th = palette()
    ar, ag, ab = th['accent_rgb']
    fill = (th['accent']
            if primary else ('rgba(15,26,52,0.06)' if light else 'rgba(255,255,255,0.06)'))
    border = ('rgba(%d,%d,%d,0.6)' % (ar, ag, ab) if primary else ('rgba(15,26,52,0.16)' if light else 'rgba(255,255,255,0.12)'))
    hover = ('rgba(%d,%d,%d,0.85)' % (ar, ag, ab) if primary else ('rgba(15,26,52,0.28)' if light else 'rgba(255,255,255,0.22)'))
    has_icon = not button.icon().isNull()
    if has_icon and button.text():
        padding = '0 12px 0 32px'
    elif button.text():
        padding = '0 12px'
    else:
        padding = '0'
    text_color = '#16233B' if light else '#f5f5f5'
    disabled_color = 'rgba(15,26,52,0.35)' if light else 'rgba(255,255,255,0.42)'
    disabled_bg = 'rgba(15,26,52,0.04)' if light else 'rgba(255,255,255,0.03)'
    disabled_border = 'rgba(15,26,52,0.08)' if light else 'rgba(255,255,255,0.06)'
    button.setStyleSheet(f'''
        QPushButton, QToolButton {{background:{fill}; color:{text_color}; border:1px solid {border};
            border-radius:8px; padding:{padding}; font-size:13px;}}
        QPushButton:hover, QToolButton:hover {{border-color:{hover};}}
        QPushButton:disabled, QToolButton:disabled {{color:{disabled_color}; background:{disabled_bg}; border-color:{disabled_border};}}
    ''')


def style_button(button, primary=False):
    from ..materials import register_button
    button.setProperty('studioStyled', True)
    register_button(button, primary)
    _apply_button_style(button, primary)
    previous = getattr(button, '_studio_motion', None)
    if previous is not None:
        try:
            previous.detach()  # 重复样式化时先解绑旧实例，避免悬空引用刷日志
        except Exception:
            pass
    button._studio_motion = WidgetMotion(button, primary=primary)
    return button


def restyle_buttons():
    from ..materials import _BUTTON_REGISTRY
    alive = []
    for ref, primary in list(_BUTTON_REGISTRY):
        button = ref()
        if button is None:
            continue  # 已销毁的弱引用：剔除
        try:
            _apply_button_style(button, primary)
        except RuntimeError:
            continue  # 控件已析构：剔除
        alive.append((ref, primary))
    _BUTTON_REGISTRY[:] = alive


class ElidedLabel(CaptionLabel):
    """Keep the full path for copying/tooltips without widening the layout."""
    def __init__(self, text='', parent=None):
        super().__init__(parent)
        self.full_text = str(text)
        self._suffix = ''
        self.setProperty('studioStyled', True)
        font = self.font(); font.setPixelSize(12); self.setFont(font)
        self.setTextColor(MUTED, MUTED)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._relayout()

    def setFullText(self, text, suffix=''):
        self.full_text = str(text)
        self._suffix = str(suffix or '')
        self.setToolTip(self.full_text + self._suffix)
        self._relayout()

    def _relayout(self):
        # 优先按路径分隔符省略：绝不把单个词或目录名切成两半。
        metrics = self.fontMetrics()
        width = max(20, self.width())
        base, suffix = self.full_text, self._suffix
        if metrics.horizontalAdvance(base + suffix) <= width:
            self.setText(base + suffix)
            return
        head = ''
        rest = base
        if len(rest) > 2 and rest[1:3] in ('\\', ':/'):
            head, rest = rest[:3], rest[3:]
        parts = [part for part in rest.split('\\') if part]
        if len(parts) >= 2:
            for keep in range(1, len(parts) + 1):
                candidate = head + '…\\' + '\\'.join(parts[-keep:])
                if metrics.horizontalAdvance(candidate + suffix) <= width:
                    self.setText(candidate + suffix)
                    return
        self.setText(metrics.elidedText(base, Qt.ElideMiddle, width) + suffix)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()


class WorkspaceCard(CardWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty('studioStyled', True)
        self.setBorderRadius(12)
        self._hover = False
        self._tint = None

    def set_tint(self, color):
        """可选底色微染（统计卡等局部用法）；None 表示无染色。"""
        self._tint = color
        self.update()

    def enterEvent(self, event):
        self._hover = True; self.update(); super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False; self.update(); super().leaveEvent(event)

    def paintEvent(self, event):
        from ..materials import ink, surface_fill, is_light
        painter = QPainter(self); painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        painter.setPen(QPen(ink(58 if self._hover else 30), 1))
        painter.setBrush(surface_fill(.6 if self._hover else 0))
        painter.drawRoundedRect(rect, 12, 12)
        # 玻璃顶部内发光：只在上缘画 1px 低透明度亮线。
        glow = QLinearGradient(rect.topLeft(), rect.topRight())
        glow.setColorAt(0, QColor(255, 255, 255, 0))
        glow.setColorAt(.5, QColor(255, 255, 255, 175 if is_light() else 48))
        glow.setColorAt(1, QColor(255, 255, 255, 0))
        painter.setPen(QPen(glow, 1)); painter.drawLine(QPointF(rect.left()+14, rect.top()+1), QPointF(rect.right()-14, rect.top()+1))
        sheen = QLinearGradient(rect.topLeft(), rect.bottomRight())
        sheen.setColorAt(0, QColor(150, 174, 218, 8)); sheen.setColorAt(1, QColor(150, 174, 218, 0))
        painter.setBrush(sheen); painter.setPen(Qt.NoPen); painter.drawRoundedRect(rect, 12, 12)
        if self._tint is not None:
            painter.setBrush(self._tint); painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect, 12, 12)


class BreathingDot(StatusDot):
    def paintEvent(self, event):
        from ..motion import clock_for
        if self.active and not self.property('pulseRunning') and not self.visibleRegion().isEmpty():
            clock_for(self).add(self)
        painter = QPainter(self); painter.setRenderHint(QPainter.Antialiasing); painter.setPen(Qt.NoPen)
        opacity = .7 + .3*math.cos(time.monotonic()*math.pi) if self.active else 1
        if self.active:
            # 4px 模糊光晕：径向渐变模拟柔光扩散。
            glow = QRadialGradient(QPointF(7, 9), 8.5)
            glow.setColorAt(0, QColor(self.color.red(), self.color.green(), self.color.blue(), round(90*opacity)))
            glow.setColorAt(.55, QColor(self.color.red(), self.color.green(), self.color.blue(), round(35*opacity)))
            glow.setColorAt(1, QColor(self.color.red(), self.color.green(), self.color.blue(), 0))
            painter.setOpacity(1)
            painter.setBrush(glow); painter.drawEllipse(QRectF(-1.5, .5, 17, 17))
        painter.setOpacity(opacity)
        painter.setBrush(self.color); painter.drawEllipse(QRectF(3, 5, 8, 8))


@lru_cache(maxsize=256)
def _read_thumbnail(path, modified, size):
    reader = QImageReader(path); reader.setAutoTransform(True)
    original_size = reader.size()
    if original_size.isValid():
        reader.setScaledSize(original_size.scaled(300, 200, Qt.KeepAspectRatio))
    pixmap = QPixmap.fromImage(reader.read())
    if pixmap.isNull():
        pixmap = QPixmap(180, 120); pixmap.fill(QColor('#242d45'))
        painter = QPainter(pixmap)
        painter.drawPixmap(70, 38, FIF.PHOTO.icon(Theme.DARK).pixmap(40, 40)); painter.end()
    return pixmap


def thumbnail(path):
    try:
        stat = Path(path).stat()
        return _read_thumbnail(str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return _read_thumbnail(str(path), 0, 0)


class ReferenceThumbnail(ImageLabel):
    clicked_path = pyqtSignal(str)
    moved = pyqtSignal(int, int)
    MIME = 'application/x-storyboard-picture-order'

    def __init__(self, path, index, owner, large=True, parent=None):
        super().__init__(parent)
        self.path, self.index, self.owner, self.large = path, index, owner, large
        self._press = QPoint()
        self.editable = False
        self.setProperty('studioStyled', True)
        self.setImage(thumbnail(path)); self.setFixedSize(120, 80) if large else self.setFixedSize(26, 26)
        self.setBorderRadius(6, 6, 6, 6)
        self.setAcceptDrops(large)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(f'Picture {index+1} · {Path(path).name}\n' + tr('点击放大；空闲时拖动调整顺序'))
        self.setFocusPolicy(Qt.StrongFocus if large else Qt.NoFocus)
        self.setAccessibleName(tr('查看') + f' Picture {index+1}：{Path(path).name}')

    def paintEvent(self, event):
        painter = QPainter(self); painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()); clip = QPainterPath(); clip.addRoundedRect(rect, 6, 6)
        painter.setClipPath(clip); painter.fillRect(self.rect(), QColor('#20232b'))
        source = self.pixmap()
        if source and not source.isNull():
            # Fit the full product; thumbnails never conceal edges with a crop.
            scaled = source.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            painter.drawPixmap((self.width()-scaled.width())//2, (self.height()-scaled.height())//2, scaled)
        if self.large:
            painter.fillRect(0, 61, self.width(), 19, QColor(0, 0, 0, 155))
            painter.setPen(QColor('#f5f5f5')); font = painter.font(); font.setPixelSize(10); painter.setFont(font)
            painter.drawText(8, 74, f'Picture {self.index+1}')
            if self.editable:
                # 拖动把手：3 × 2 距离点阵（替代 ⠿ 文字符号）。
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(245, 245, 245, 205))
                for row_index in range(2):
                    for column_index in range(3):
                        painter.drawEllipse(QRectF(103 + column_index * 4.5, 69 + row_index * 5, 2.2, 2.2))

    def mousePressEvent(self, event):
        self._press = event.pos(); super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.large and self.editable and event.buttons() & Qt.LeftButton and (event.pos()-self._press).manhattanLength() >= 10:
            drag = QDrag(self); mime = QMimeData()
            mime.setData(self.MIME, f'{self.owner}:{self.index}'.encode())
            drag.setMimeData(mime); drag.setPixmap(self.grab()); drag.setHotSpot(event.pos())
            self._press = QPoint(-1000, -1000); drag.exec_(Qt.MoveAction)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and (event.pos()-self._press).manhattanLength() < 10:
            self.clicked_path.emit(self.path)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Space):
            self.clicked_path.emit(self.path)
        else:
            super().keyPressEvent(event)

    def dragEnterEvent(self, event):
        data = bytes(event.mimeData().data(self.MIME)).decode(errors='replace')
        if self.editable and data.startswith(f'{self.owner}:'):
            event.acceptProposedAction()

    def dropEvent(self, event):
        data = bytes(event.mimeData().data(self.MIME)).decode(errors='replace')
        if self.editable and data.startswith(f'{self.owner}:'):
            try:
                self.moved.emit(int(data.rsplit(':', 1)[1]), self.index)
                event.acceptProposedAction()
            except ValueError:
                event.ignore()


class ReferenceStrip(ScrollArea):
    reordered = pyqtSignal(object)
    preview_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.paths = []; self.thumbnails = []; self.editable = True
        self.setWidgetResizable(True); self.setFixedHeight(88)
        self.setStyleSheet('QScrollArea {border:0; background:transparent;}')
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.content = QWidget(); self.content.setAutoFillBackground(False)
        self.row = QHBoxLayout(self.content); self.row.setContentsMargins(0, 0, 0, 4); self.row.setSpacing(8)
        self.setWidget(self.content); self.content.setAutoFillBackground(False)

    def set_paths(self, paths):
        paths = list(paths or [])
        if paths == self.paths:
            return
        self.paths = paths
        while self.row.count():
            item = self.row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.thumbnails = []
        for index, path in enumerate(paths):
            widget = ReferenceThumbnail(path, index, id(self), parent=self.content)
            widget.editable = self.editable
            widget.clicked_path.connect(self.preview_requested)
            widget.moved.connect(self.move_reference)
            self.thumbnails.append(widget); self.row.addWidget(widget)
        if not paths:
            self.row.addWidget(label(tr('暂无参考图 · 请在匹配详情中绑定'), 12))
        self.row.addStretch(1)

    def move_reference(self, source, target):
        if self.editable and 0 <= source < len(self.paths) and 0 <= target < len(self.paths) and source != target:
            paths = list(self.paths); paths.insert(target, paths.pop(source))
            # The workspace validates/persists before accepting the new order.
            self.reordered.emit(paths)

    def set_editable(self, editable):
        self.editable = editable
        for widget in self.thumbnails:
            widget.editable = editable; widget.update()


class ImagePreview(StudioDialog):
    def __init__(self, path, parent=None):
        super().__init__(parent, tr('参考图查看器'))
        self.path = path
        reader = QImageReader(path); reader.setAutoTransform(True)
        self.original = QPixmap.fromImage(reader.read())
        self.setObjectName('workspaceImagePreview')
        self.resize(980, 720)
        layout = self.body_layout
        heading = QHBoxLayout(); heading.addStretch(1)
        self.size_label = label(f'{self.original.width()} × {self.original.height()}  ·  ' + tr('原始图片'), 12)
        heading.addWidget(self.size_label); layout.addLayout(heading)
        self.area = ScrollArea(); self.area.setWidgetResizable(False)
        self.area.setStyleSheet('QScrollArea {background:#0c0e14; border:1px solid rgba(255,255,255,0.08); border-radius:12px;}')
        self.area.setAlignment(Qt.AlignCenter)
        self.image = ImageLabel(); self.image.setBorderRadius(8, 8, 8, 8)
        self.area.setWidget(self.image); layout.addWidget(self.area, 1)
        self.scale = 1.
        self.image.setImage(self.original if not self.original.isNull() else thumbnail(path))
        footer = QHBoxLayout(); name = ElidedLabel(str(Path(path).name)); footer.addWidget(name, 1)
        zoom_out = style_button(PushButton('', None, FIF.ZOOM_OUT)); zoom_out.setFixedSize(30, 30)
        zoom_out.setToolTip(tr('缩小')); zoom_out.clicked.connect(lambda: self.zoom(.8)); footer.addWidget(zoom_out)
        fit_button = style_button(PushButton(tr('适应窗口'))); fit_button.setFixedHeight(30)
        fit_button.clicked.connect(self.fit); footer.addWidget(fit_button)
        zoom_in = style_button(PushButton('', None, FIF.ZOOM_IN)); zoom_in.setFixedSize(30, 30)
        zoom_in.setToolTip(tr('放大')); zoom_in.clicked.connect(lambda: self.zoom(1.25)); footer.addWidget(zoom_in)
        close_button = style_button(PushButton(tr('关闭'))); close_button.setFixedHeight(30)
        close_button.clicked.connect(self.accept); footer.addWidget(close_button)
        layout.addLayout(footer)
        if self.original.isNull():
            self.size_label.setText(tr('图片不可读或文件已移动'))

    def showEvent(self, event):
        super().showEvent(event); self.fit()

    def fit(self):
        size = self.original.size() if not self.original.isNull() else QSize(180, 120)
        self.scale = min((self.area.viewport().width()-20)/max(1, size.width()),
                         (self.area.viewport().height()-20)/max(1, size.height()), 1.)
        self._resize_image()

    def zoom(self, factor):
        self.scale = max(.05, min(5, self.scale*factor)); self._resize_image()

    def _resize_image(self):
        pixmap = self.original if not self.original.isNull() else thumbnail(self.path)
        self.image.setFixedSize(max(1, round(pixmap.width()*self.scale)), max(1, round(pixmap.height()*self.scale)))
