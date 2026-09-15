"""Cached Fluent material painting. No live desktop capture or per-frame blur."""
from functools import lru_cache
import math

from PyQt5.QtCore import Qt, QRectF, QPointF
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPixmap, QLinearGradient, QRadialGradient, QPen
from PyQt5.QtWidgets import QGraphicsEffect

ACCENT = '#5B8DEF'
BRAND_START = '#5B8DEF'
BRAND_END = '#7C6CF0'
SUCCESS = '#22c55e'
WARNING = '#f59e0b'
ERROR = '#f56c6c'
SECONDARY = QColor('#9CA3AF')
TERTIARY = QColor('#6B7280')
TEXT_PRIMARY = '#F4F5F7'
BG_BASE = '#0A0B12'


def background_brush(rect):
    gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    gradient.setColorAt(0, QColor(BG_BASE))
    gradient.setColorAt(1, QColor('#10111C'))
    return gradient


def paint_background(painter, rect, opaque=True):
    """Deep-space base with the two ambient radial washes."""
    if opaque:
        painter.fillRect(rect, QColor(BG_BASE))
    else:
        # Native Mica/Acrylic shows through the tinted glass layer.
        tint = QColor(BG_BASE); tint.setAlpha(200)
        painter.fillRect(rect, tint)
    span = max(rect.width(), rect.height())
    blue = QRadialGradient(rect.topLeft(), span * 1.3)
    blue.setColorAt(0, QColor(91, 141, 239, 16))
    blue.setColorAt(1, QColor(91, 141, 239, 0))
    painter.fillRect(rect, blue)
    purple = QRadialGradient(rect.bottomRight(), span * 1.3)
    purple.setColorAt(0, QColor(124, 108, 240, 14))
    purple.setColorAt(1, QColor(124, 108, 240, 0))
    painter.fillRect(rect, purple)


@lru_cache(maxsize=1)
def frost_texture():
    """Pre-softened illumination: cheap Acrylic fallback on all Windows versions."""
    pixmap = QPixmap(256, 256); pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    light = QRadialGradient(QPointF(30, 0), 270)
    light.setColorAt(0, QColor(129, 181, 240, 9))
    light.setColorAt(1, QColor(129, 181, 240, 0))
    painter.fillRect(pixmap.rect(), light)
    painter.setPen(QColor(255, 255, 255, 2))
    for y in range(0, 256, 3):
        for x in range(y % 7, 256, 7):
            painter.drawPoint(x, y)
    painter.end()
    return pixmap


def paint_surface(widget, painter, elevated=False, hover=0):
    painter.setRenderHint(QPainter.Antialiasing)
    rect = QRectF(widget.rect()).adjusted(1, 1, -1, -1)
    path = QPainterPath(); path.addRoundedRect(rect, 14, 14)
    painter.save(); painter.setClipPath(path)
    painter.fillPath(path, QColor(255, 255, 255, 13 + round(8*hover)))
    painter.drawPixmap(rect, frost_texture(), QRectF(0, 0, 256, 256))
    if elevated:
        # Qt QSS has no inset box-shadow. Paint a soft 20px inset explicitly.
        for inset in range(20, 0, -1):
            alpha = round(26 * math.exp(-inset/5))
            painter.setPen(QPen(QColor(91, 141, 239, alpha), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(inset, inset, -inset, -inset), 14, 14)
    painter.restore()
    painter.setBrush(Qt.NoBrush)
    border = QColor(150, 168, 255, 128) if elevated else QColor(255, 255, 255, round(26 + 20*hover))
    painter.setPen(QPen(border, 1))
    painter.drawRoundedRect(rect, 14, 14)
    # A fine upper highlight gives the translucent surface a lit edge.
    highlight = QLinearGradient(rect.topLeft(), rect.topRight())
    highlight.setColorAt(0, QColor(255, 255, 255, 0))
    highlight.setColorAt(.45, QColor(255, 255, 255, 22))
    highlight.setColorAt(1, QColor(255, 255, 255, 0))
    painter.setPen(QPen(highlight, 1))
    painter.drawLine(QPointF(14, 1), QPointF(widget.width()-14, 1))


@lru_cache(maxsize=4)
def shadow_tile(hover=False, primary=False):
    """Gaussian falloff, prepainted once and stretched with nine-slice rendering."""
    pixmap = QPixmap(96, 96); pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap); painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    # Two independent lobes: 0/4/12/.3 and 0/1/2/.2. Only the small cached
    # tile is rasterized; no blur ever runs on text or entire widget trees.
    for offset, radius, alpha in ((4, 12, 77), (1, 2, 51)):
        for spread in range(radius, -1, -1):
            weight = math.exp(-.5 * (spread / max(1, radius/2))**2)
            color = QColor(0, 0, 0, round(alpha * weight / max(1, radius/2)))
            if primary:
                color = QColor(91, 141, 239, round(18*weight / max(1, radius/3)))
            if hover:
                color.setAlpha(min(255, round(color.alpha()*1.7)))
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(24-spread, 24+offset-spread, 48+spread*2, 48+spread*2), 12+spread, 12+spread)
    painter.end()
    return pixmap


def draw_shadow(painter, rect, hover=0, primary=False):
    target = rect.adjusted(-24, -24, 24, 24)
    # Translucent cards cannot hide the opaque centre of a shadow as an opaque
    # surface would. Cut out the whole rounded surface, not the nine-slice
    # centre alone, or the remaining 12px strips appear as a hard inner frame.
    outside = QPainterPath(); outside.addRect(target)
    surface = QPainterPath(); surface.addRoundedRect(rect.adjusted(1, 1, -1, -1), 12, 12)
    painter.save(); painter.setClipPath(outside.subtracted(surface))
    xs = [target.left(), rect.left()+12, rect.right()-12, target.right()]
    ys = [target.top(), rect.top()+12, rect.bottom()-12, target.bottom()]
    source = [0, 36, 60, 96]
    for active, opacity in ((False, 1-hover), (True, hover)):
        if opacity <= 0:
            continue
        painter.save(); painter.setOpacity(opacity)
        tile = shadow_tile(active, primary)
        for y in range(3):
            for x in range(3):
                if x == y == 1:
                    continue
                painter.drawPixmap(QRectF(xs[x], ys[y], xs[x+1]-xs[x], ys[y+1]-ys[y]), tile,
                    QRectF(source[x], source[y], source[x+1]-source[x], source[y+1]-source[y]))
        painter.restore()
    painter.restore()


class SurfaceShadow(QGraphicsEffect):
    """One cached, two-layer effect per card; no nested button/page effects."""
    def __init__(self, parent, primary=False):
        super().__init__(parent)
        self.hover = 0
        self.primary = primary

    def boundingRectFor(self, rect):
        return rect.adjusted(-16, -14, 16, 20)

    def draw(self, painter):
        pixmap, offset = self.sourcePixmap(Qt.LogicalCoordinates, QGraphicsEffect.NoPad)
        if pixmap.isNull():
            return
        rect = QRectF(offset.x(), offset.y(), pixmap.width()/pixmap.devicePixelRatioF(), pixmap.height()/pixmap.devicePixelRatioF())
        draw_shadow(painter, rect, self.hover, self.primary)
        painter.drawPixmap(offset, pixmap)
