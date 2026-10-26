"""Small vector icons matching the navigation's 16px drawing grid."""
from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from qfluentwidgets import FluentIconBase, Theme, isDarkTheme


class ShoppingBagIcon(FluentIconBase):
    def icon(self, theme=Theme.AUTO, color=None):
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        self.render(painter, QRectF(0, 0, 32, 32), theme, fill=color)
        painter.end()
        return QIcon(pixmap)

    def render(self, painter, rect, theme=Theme.AUTO, indexes=None, **attributes):
        dark = isDarkTheme() if theme == Theme.AUTO else theme == Theme.DARK
        color = attributes.get('fill') or ('#FFFFFF' if dark else '#000000')
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.translate(rect.x(), rect.y())
        painter.scale(rect.width() / 16, rect.height() / 16)
        painter.setPen(QPen(QColor(color), 1.3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush)
        bag = QPainterPath()
        bag.moveTo(3.5, 5.5)
        bag.lineTo(12.5, 5.5)
        bag.lineTo(13.5, 14)
        bag.lineTo(2.5, 14)
        bag.closeSubpath()
        painter.drawPath(bag)
        handle = QPainterPath()
        handle.moveTo(5.5, 7)
        handle.lineTo(5.5, 4)
        handle.cubicTo(5.5, 0.8, 10.5, 0.8, 10.5, 4)
        handle.lineTo(10.5, 7)
        painter.drawPath(handle)
        painter.restore()
