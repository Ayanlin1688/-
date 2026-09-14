"""Workspace-only materials and image interactions; other pages keep their theme."""
import math
import time
from functools import lru_cache
from pathlib import Path

from PyQt5.QtCore import Qt, QRectF, QSize, QPoint, QMimeData, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap, QImageReader, QDrag, QLinearGradient
from PyQt5.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QSizePolicy
from qframelesswindow import FramelessDialog
from qfluentwidgets import (CardWidget, CaptionLabel, StrongBodyLabel, ImageLabel,
                            PushButton, TransparentToolButton, ScrollArea, FluentIcon as FIF, Theme)
from ..motion import StatusDot, WidgetMotion

BLUE = '#3b82f6'
GREEN = '#22c55e'
RED = '#ef4444'
YELLOW = '#eab308'
MUTED = '#71717a'


def label(text='', size=12, color=MUTED, bold=False, mono=False):
    widget = StrongBodyLabel(text) if bold else CaptionLabel(text)
    widget.setProperty('studioStyled', True)
    font = widget.font(); font.setPixelSize(size); font.setBold(bold)
    if mono:
        font.setFamily('Cascadia Mono')
    widget.setFont(font); widget.setTextColor(color, color)
    widget.setStyleSheet(f'background:transparent; color:{color};')
    return widget


def style_button(button, primary=False):
    button.setProperty('studioStyled', True)
    fill = ('qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #2563eb,stop:1 #60a5fa)'
            if primary else 'rgba(255,255,255,0.025)')
    has_icon = not button.icon().isNull()
    padding = '5px 12px 5px 34px' if has_icon and button.text() else '5px 12px' if button.text() else '0px'
    button.setStyleSheet(f'''
        QPushButton, QToolButton {{background:{fill}; color:#f5f5f5; border:1px solid rgba(255,255,255,0.08);
            border-radius:8px; padding:{padding}; font-size:12px;}}
        QPushButton:hover, QToolButton:hover {{border-color:rgba(255,255,255,0.2);}}
        QPushButton:disabled, QToolButton:disabled {{color:#55555f; background:rgba(255,255,255,0.015);}}
    ''')
    button._studio_motion = WidgetMotion(button)
    return button


class ElidedLabel(CaptionLabel):
    """Keep the full path for copying/tooltips without widening the layout."""
    def __init__(self, text='', parent=None):
        super().__init__(parent)
        self.full_text = text
        self.setProperty('studioStyled', True)
        font = self.font(); font.setPixelSize(12); self.setFont(font)
        self.setTextColor(MUTED, MUTED)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setFullText(text)

    def setFullText(self, text):
        self.full_text = str(text)
        self.setToolTip(self.full_text)
        self.setText(self.fontMetrics().elidedText(self.full_text, Qt.ElideMiddle, max(20, self.width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setFullText(self.full_text)


class WorkspaceCard(CardWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty('studioStyled', True)
        self.setBorderRadius(16)
        self._hover = False

    def enterEvent(self, event):
        self._hover = True; self.update(); super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False; self.update(); super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self); painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        painter.setPen(QPen(QColor(255, 255, 255, 42 if self._hover else 20), 1))
        painter.setBrush(QColor(255, 255, 255, 10)); painter.drawRoundedRect(rect, 16, 16)
        sheen = QLinearGradient(rect.topLeft(), rect.bottomRight())
        sheen.setColorAt(0, QColor(150, 174, 218, 7)); sheen.setColorAt(1, QColor(150, 174, 218, 0))
        painter.setBrush(sheen); painter.setPen(Qt.NoPen); painter.drawRoundedRect(rect, 16, 16)


class BreathingDot(StatusDot):
    def paintEvent(self, event):
        from ..motion import clock_for
        if self.active and not self.property('pulseRunning') and not self.visibleRegion().isEmpty():
            clock_for(self).add(self)
        painter = QPainter(self); painter.setRenderHint(QPainter.Antialiasing); painter.setPen(Qt.NoPen)
        painter.setOpacity(.7 + .3*math.cos(time.monotonic()*math.pi) if self.active else 1)
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
        self.setBorderRadius(8, 8, 8, 8)
        self.setAcceptDrops(large)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(f'Picture {index+1} · {Path(path).name}\n点击放大；空闲时拖动调整顺序')
        self.setFocusPolicy(Qt.StrongFocus if large else Qt.NoFocus)
        self.setAccessibleName(f'查看 Picture {index+1}：{Path(path).name}')

    def paintEvent(self, event):
        painter = QPainter(self); painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()); clip = QPainterPath(); clip.addRoundedRect(rect, 8 if self.large else 6, 8 if self.large else 6)
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
            painter.drawText(102, 74, '⠿' if self.editable else '')

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
        if list(paths) == self.paths and self.row.count():
            return
        self.paths = list(paths)
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
            self.row.addWidget(label('暂无参考图 · 请在匹配详情中绑定', 12))
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


class ImagePreview(FramelessDialog):
    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path
        reader = QImageReader(path); reader.setAutoTransform(True)
        self.original = QPixmap.fromImage(reader.read())
        self.setObjectName('workspaceImagePreview'); self.setWindowTitle('参考图查看器')
        self.resize(980, 720)
        self.setStyleSheet('#workspaceImagePreview {background:#111116;}')
        layout = QVBoxLayout(self); layout.setContentsMargins(24, 42, 24, 20); layout.setSpacing(16)
        heading = QHBoxLayout(); heading.addWidget(label('参考图查看器', 18, '#f5f5f5', True)); heading.addStretch(1)
        self.size_label = label(f'{self.original.width()} × {self.original.height()}  ·  原始图片', 12)
        heading.addWidget(self.size_label); layout.addLayout(heading)
        self.area = ScrollArea(); self.area.setWidgetResizable(False)
        self.area.setStyleSheet('QScrollArea {background:#0d0d10; border:1px solid rgba(255,255,255,0.08); border-radius:12px;}')
        self.area.setAlignment(Qt.AlignCenter)
        self.image = ImageLabel(); self.image.setBorderRadius(8, 8, 8, 8)
        self.area.setWidget(self.image); layout.addWidget(self.area, 1)
        self.scale = 1.
        self.image.setImage(self.original if not self.original.isNull() else thumbnail(path))
        footer = QHBoxLayout(); name = ElidedLabel(str(Path(path).name)); footer.addWidget(name, 1)
        for text, callback in [('−', lambda: self.zoom(.8)), ('适应窗口', self.fit), ('+', lambda: self.zoom(1.25)), ('关闭', self.accept)]:
            button = style_button(PushButton(text)); button.clicked.connect(callback); footer.addWidget(button)
        layout.addLayout(footer)
        if self.original.isNull():
            self.size_label.setText('图片不可读或文件已移动')
        self.titleBar.closeBtn.setNormalColor(QColor('#a1a1aa'))

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
