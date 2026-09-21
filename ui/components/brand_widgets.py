"""Brand chrome for the 64px navigation rail (deep-space liquid glass)."""
from PyQt5.QtCore import Qt, QRectF
from PyQt5.QtGui import QColor, QLinearGradient, QPainter, QPen, QPixmap, QRadialGradient
from qfluentwidgets import NavigationWidget


def logo_pixmap(size=64):
    """品牌磁贴的静态渲染（蓝紫渐变 + 白色 Y 字标）：
    用于应用图标 / 启动画面 / 关于页；与 BrandLogo 的在线绘制保持同一视觉。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    rect = QRectF(0, 0, size, size)
    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0, QColor('#5B8DEF'))
    gradient.setColorAt(1, QColor('#7C6CF0'))
    painter.setPen(Qt.NoPen)
    painter.setBrush(gradient)
    radius = max(3.0, size * 0.28)
    painter.drawRoundedRect(rect, radius, radius)
    painter.setPen(QColor('#FFFFFF'))
    font = painter.font()
    font.setPixelSize(max(8, round(size * 0.5)))
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(rect, Qt.AlignCenter, 'Y')
    painter.end()
    return pixmap


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
        self._expanded = False

    def set_expanded(self, expanded):
        """展开态：加宽为品牌区块（磁贴 + 中文名 + 英文小字）。"""
        self._expanded = bool(expanded)
        self._apply_size()

    def setCompacted(self, isCompacted: bool):
        self._apply_size()

    def _apply_size(self):
        self.setFixedSize(224 if self._expanded else 64, 68)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        size = 34.0
        # 磁贴锚定在组件顶部，与标题栏文字垂直居中对齐。
        left = 15.0
        rect = QRectF(left, 1.0, size, size)
        center = rect.center()
        from ..materials import palette
        th = palette()
        a2 = th['accent2_rgb']
        halo = QRadialGradient(center, size*0.95)
        halo.setColorAt(0, QColor(a2[0], a2[1], a2[2], 66))
        halo.setColorAt(1, QColor(a2[0], a2[1], a2[2], 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(QRectF(center.x()-size*0.95, center.y()-size*0.95, size*1.9, size*1.9))
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0, QColor(th['accent']))
        gradient.setColorAt(1, QColor(th['accent2']))
        painter.setBrush(gradient)
        painter.drawRoundedRect(rect, 10, 10)
        painter.setPen(QColor('#FFFFFF'))
        font = self.font(); font.setPixelSize(17); font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, 'Y')
        if self._expanded and self.width() > 140:
            from core.version import APP_NAME, APP_NAME_CN
            from ..materials import map_text_color
            text_left = left + size + 10
            text_width = max(20.0, self.width() - text_left - 10)
            title_color = map_text_color('#f5f5f5')
            painter.setPen(title_color if isinstance(title_color, QColor) else QColor(str(title_color)))
            font = self.font(); font.setPixelSize(15); font.setBold(True)
            painter.setFont(font)
            painter.drawText(QRectF(text_left, 6, text_width, 22), Qt.AlignLeft | Qt.AlignVCenter, APP_NAME_CN)
            sub_color = map_text_color('#9AA6B8')
            painter.setPen(sub_color if isinstance(sub_color, QColor) else QColor(str(sub_color)))
            font = self.font(); font.setPixelSize(10); font.setBold(False)
            painter.setFont(font)
            painter.drawText(QRectF(text_left, 28, text_width, 16), Qt.AlignLeft | Qt.AlignVCenter, APP_NAME)


class UserAvatar(_RailWidget):
    """Bottom user avatar rendered as a subtle gradient circle."""

    def __init__(self, parent=None):
        super().__init__(64, 56, parent)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        size = 30.0
        rect = QRectF((self.width()-size)/2, (self.height()-size)/2, size, size)
        from ..materials import palette
        th = palette()
        a1 = th['accent_rgb']; a2 = th['accent2_rgb']
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        c1 = QColor(a1[0], a1[1], a1[2]); c2 = QColor(a2[0], a2[1], a2[2])
        if not th['light']:
            c1 = c1.darker(175); c2 = c2.darker(200)
        gradient.setColorAt(0, c1)
        gradient.setColorAt(1, c2)
        painter.setPen(QPen(QColor(255, 255, 255, 38), 1))
        painter.setBrush(gradient)
        painter.drawEllipse(rect)
        painter.setPen(QColor('#FFFFFF') if not th['light'] else QColor(a1[0], a1[1], a1[2]).darker(180))
        font = self.font(); font.setPixelSize(13); font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, 'Y')
