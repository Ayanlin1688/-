from pathlib import Path

from PyQt5.QtCore import Qt, QRectF, pyqtSignal
from PyQt5.QtGui import QPainter, QPainterPath, QPixmap
from PyQt5.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import IconWidget, TransparentToolButton, FluentIcon as FIF

from core.i18n import tr
from .. import materials
from ..tokens import px


class ReferenceThumbnail(QWidget):
    """Small selectable reference preview with a safe missing-file state."""

    clicked = pyqtSignal()
    removed = pyqtSignal()

    def __init__(self, path, *, size=84, selectable=False, parent=None):
        super().__init__(parent)
        self.path = str(path)
        self.selectable = bool(selectable)
        self.selected = False
        self.setFixedWidth(px(size))
        self.setToolTip(self.path)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.preview = QLabel()
        self.preview.setFixedSize(px(size), px(size))
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.preview.setToolTip(self.path)
        self.preview.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.preview.setTextFormat(Qt.PlainText)
        self.photo_icon = IconWidget(FIF.PHOTO, self.preview)
        self.photo_icon.setFixedSize(px(24), px(24))
        self.photo_icon.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.photo_icon.move((px(size) - px(24)) // 2, px(4))
        self.photo_icon.hide()
        layout.addWidget(self.preview)
        self.name_label = QLabel()
        self.name_label.setAlignment(Qt.AlignCenter)
        self.name_label.setText(self._display_name())
        self.name_label.setToolTip(self.path)
        layout.addWidget(self.name_label)
        self.close_button = TransparentToolButton(FIF.CLOSE, self)
        self.close_button.setFixedSize(px(22), px(22))
        self.close_button.setToolTip(tr('删除'))
        self.close_button.clicked.connect(self.removed)
        self.close_button.raise_()
        self._load()
        self._refresh_style()

    def _display_name(self):
        name = Path(self.path).name or self.path
        return self.fontMetrics().elidedText(name, Qt.ElideMiddle, px(84))

    def _load(self):
        image = QPixmap()
        try:
            if Path(self.path).is_file():
                image.loadFromData(Path(self.path).read_bytes())
        except (OSError, ValueError):
            pass
        if image.isNull():
            self.preview.setPixmap(QPixmap())
            self.preview.setText(tr('无法预览'))
            self.photo_icon.show()
            return
        size = self.preview.size()
        scaled = image.scaled(size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        left = max(0, (scaled.width() - size.width()) // 2)
        top = max(0, (scaled.height() - size.height()) // 2)
        canvas = QPixmap(size)
        canvas.fill(Qt.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, size.width(), size.height()), px(8), px(8))
        painter.setClipPath(clip)
        painter.drawPixmap(0, 0, scaled, left, top, size.width(), size.height())
        painter.end()
        self.preview.setText('')
        self.preview.setPixmap(canvas)
        self.photo_icon.hide()

    def set_selected(self, selected):
        self.selected = bool(selected)
        self._refresh_style()

    def _refresh_style(self):
        colors = materials.palette()
        border = colors.get('theme', colors.get('accent', '#5B8DEF')) if self.selected else colors.get('outline', colors.get('text2', '#6B7280'))
        self.preview.setStyleSheet('border: 2px solid %s; border-radius: 8px;' % border)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.close_button.move(max(0, self.width() - self.close_button.width()), 0)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.selectable and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class ReferenceGrid(QWidget):
    """Wrap container for reference thumbnails with ListWidget-like selection helpers."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected_index = -1
        from ..responsive import WrapLayout
        self._layout = WrapLayout(spacing=px(8))
        self.setLayout(self._layout)

    def clear(self):
        self.selected_index = -1
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def add_thumbnail(self, thumbnail):
        index = self._layout.count()
        thumbnail.clicked.connect(lambda index=index: self.setCurrentRow(index))
        self._layout.addWidget(thumbnail)

    def count(self):
        return self._layout.count()

    def item(self, index):
        widget = self._layout.itemAt(index).widget() if self._layout.itemAt(index) else None
        if widget is None:
            return None
        return _ReferenceItem(widget)

    def currentRow(self):
        return self.selected_index

    def setCurrentRow(self, index):
        self.selected_index = index if 0 <= index < self.count() else -1
        for row in range(self.count()):
            widget = self._layout.itemAt(row).widget()
            if widget is not None:
                widget.set_selected(row == self.selected_index)


class _ReferenceItem:
    def __init__(self, widget):
        self.widget = widget

    def text(self):
        return Path(self.widget.path).name or self.widget.path

    def setToolTip(self, value):
        self.widget.setToolTip(value)

    def toolTip(self):
        return self.widget.toolTip()
