"""Brand chrome for the 64px navigation rail (deep-space liquid glass)."""
from PyQt5.QtCore import Qt, QRectF
from PyQt5.QtGui import QColor, QLinearGradient, QPainter, QPen, QRadialGradient
from qfluentwidgets import NavigationWidget


class _RailWidget(NavigationWidget):
    """Static rail widget: fixed compact footprint, clickable like nav items."""

    def __init__(self, width=64, height=64, parent=None):
        super().__init__(isSelectable=False, parent=parent)
        self._rail_size = (width, height)
        self.setFixedSize(width, height)
        self.setCursor(Qt.PointingHandCursor)

    def setCompacted(self, isCompacted: bool):
        self.setFixedSize(*self._rail_size)


class BrandLogo(_RailWidget):
    """Rounded-10px blue/purple gradient tile with a white Y monogram."""

    def __init__(self, parent=None):
        super().__init__(64, 68, parent)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        size = 34.0
        # 磁贴锚定在组件顶部，与标题栏文字垂直居中对齐。
        rect = QRectF((self.width()-size)/2, 1.0, size, size)
        center = rect.center()
        halo = QRadialGradient(center, size*0.95)
        halo.setColorAt(0, QColor(124, 108, 240, 66))
        halo.setColorAt(1, QColor(124, 108, 240, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(QRectF(center.x()-size*0.95, center.y()-size*0.95, size*1.9, size*1.9))
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0, QColor('#5B8DEF'))
        gradient.setColorAt(1, QColor('#7C6CF0'))
        painter.setBrush(gradient)
        painter.drawRoundedRect(rect, 10, 10)
        painter.setPen(QColor('#FFFFFF'))
        font = self.font(); font.setPixelSize(17); font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, 'Y')


class UserAvatar(_RailWidget):
    """Bottom user avatar rendered as a subtle gradient circle."""

    def __init__(self, parent=None):
        super().__init__(64, 56, parent)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        size = 30.0
        rect = QRectF((self.width()-size)/2, (self.height()-size)/2, size, size)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0, QColor('#3A416E'))
        gradient.setColorAt(1, QColor('#232945'))
        painter.setPen(QPen(QColor(255, 255, 255, 38), 1))
        painter.setBrush(gradient)
        painter.drawEllipse(rect)
        painter.setPen(QColor('#D6DBFF'))
        font = self.font(); font.setPixelSize(13); font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, 'Y')
