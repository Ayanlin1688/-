"""Raster LIN brand chrome and the navigation rail's user avatar."""
from PyQt5.QtCore import Qt, QRectF
from PyQt5.QtGui import QColor, QFontMetrics, QLinearGradient, QPainter, QPen, QPixmap
from qfluentwidgets import NavigationWidget
from core.version import APP_NAME
from ..assets import asset_path
from ..tokens import px


def logo_pixmap(size=64, compact=False):
    """Load the transparent LIN tile at four device pixels per logical pixel."""
    name = 'app-small.png' if compact else ('app.png' if size <= 64 else 'app-512.png')
    pixmap = QPixmap(str(asset_path(name)))
    pixmap = pixmap.scaled(size * 4, size * 4, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    pixmap.setDevicePixelRatio(4)
    return pixmap


class _RailWidget(NavigationWidget):
    """Static rail widget: fixed compact footprint, clickable like nav items."""

    def __init__(self, width=64, height=64, parent=None):
        super().__init__(isSelectable=False, parent=parent)
        self._rail_size = (width, height)
        self.setFixedSize(width, height)
        self.setCursor(Qt.PointingHandCursor)

    def setCompacted(self, isCompacted: bool):
        self.setFixedSize(*[px(value) for value in self._rail_size])


class BrandLogo(_RailWidget):
    """Green LIN tile with the full single-line English title when expanded."""

    def __init__(self, parent=None):
        super().__init__(64, 68, parent)
        self._expanded = False
        self.title_text = APP_NAME
        self.setAccessibleName(APP_NAME)
        self.setToolTip(APP_NAME)

    def set_expanded(self, expanded):
        """Show the single English brand title beside the tile."""
        self._expanded = bool(expanded)
        self._apply_size()
        self.update()

    def setCompacted(self, isCompacted: bool):
        self._apply_size()

    def _title_font(self):
        font = self.font()
        font.setPixelSize(px(14))
        font.setBold(False)
        return font

    def expanded_width(self):
        text_width = QFontMetrics(self._title_font()).horizontalAdvance(self.title_text)
        return max(px(224), px(15) + px(34) + px(10) + text_width + px(10))

    def _apply_size(self):
        self.setFixedSize(self.expanded_width() if self._expanded else px(64), px(68))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        size = px(34)
        left = px(15)
        rect = QRectF(left, px(1), size, size)
        painter.drawPixmap(rect.toRect(), logo_pixmap(size, compact=not self._expanded))
        if self._expanded and self.width() > px(140):
            from ..materials import map_text_color
            text_left = left + size + px(10)
            text_width = max(px(20), self.width() - text_left - px(10))
            title_color = map_text_color('#f5f5f5')
            painter.setPen(title_color if isinstance(title_color, QColor) else QColor(str(title_color)))
            font = self._title_font()
            painter.setFont(font)
            painter.drawText(QRectF(text_left, rect.top(), text_width, size), Qt.AlignLeft | Qt.AlignVCenter, self.title_text)


class UserAvatar(_RailWidget):
    """Bottom user avatar rendered as a subtle gradient circle."""

    def __init__(self, parent=None):
        super().__init__(64, 56, parent)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        size = px(30)
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
        font = self.font(); font.setPixelSize(px(13)); font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, 'Y')
