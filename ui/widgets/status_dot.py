"""规范状态点：以绘制的圆点 / 圆环替代 ✓ ⚠ ● ○ 等文字符号。

交互语义上「点」只承担状态与选中提示，不参与布局拉伸；
按钮 / 菜单需要图示时用 dot_icon() 返回的 QIcon（双倍分辨率保持锐利）。
"""
from PyQt5.QtCore import Qt, QRectF
from PyQt5.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QWidget


class Dot(QWidget):
    """Static 状态点：filled=实心（激活 / 成功），否则为圆环（未激活 / 次要）。"""

    def __init__(self, size=8, color='#22C55E', filled=True, parent=None):
        super().__init__(parent)
        self._size = max(4, int(size))
        self._filled = bool(filled)
        self._color = QColor(color)
        self.setFixedSize(self._size + 4, self._size + 4)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)

    def set_color(self, color):
        self._color = QColor(color)
        self.update()

    def set_filled(self, filled):
        self._filled = bool(filled)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        offset = (self.width() - self._size) / 2
        rect = QRectF(offset, offset, self._size, self._size)
        if self._filled:
            painter.setPen(Qt.NoPen)
            painter.setBrush(self._color)
            painter.drawEllipse(rect)
        else:
            painter.setPen(QPen(self._color, max(1.2, self._size / 7)))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(rect.adjusted(0.8, 0.8, -0.8, -0.8))


def dot_icon(color, size=10, filled=True):
    """按钮 / 菜单用圆点图标（2x 绘制，逻辑尺寸保持 size）。"""
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    color = QColor(color)
    rect = QRectF(size * 0.15, size * 0.15, size * 0.7, size * 0.7)
    if filled:
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(rect)
    else:
        painter.setPen(QPen(color, max(1.1, size / 9)))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(rect.adjusted(0.6, 0.6, -0.6, -0.6))
    painter.end()
    return QIcon(pixmap)
