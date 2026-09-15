"""Cached Fluent material painting. No live desktop capture or per-frame blur."""
from functools import lru_cache
import math
import weakref

from PyQt5.QtCore import Qt, QRectF, QPointF
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPixmap, QLinearGradient, QRadialGradient, QPen
from PyQt5.QtWidgets import QGraphicsEffect

from .palettes import THEMES, resolve as resolve_theme

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

# ---------------------------------------------------------------------------
# 多主题：所有面板 / 文字 / 描边 / 光晕颜色都从当前主题调色板取，
# 切换时调用 apply_theme_tokens()，再整体刷新注册过的控件。
LIGHT_MODE = False
THEME_ID = 'dark'
THEME = THEMES['dark']
_LABEL_REGISTRY = []
_BUTTON_REGISTRY = []
_THEME_CALLBACKS = []


def apply_theme_tokens(theme_id):
    """激活主题调色板；返回实际生效的主题 id。"""
    global THEME_ID, THEME, LIGHT_MODE
    THEME_ID = resolve_theme(theme_id)
    THEME = THEMES[THEME_ID]
    LIGHT_MODE = bool(THEME.get('light'))
    return THEME_ID


def palette():
    return THEME


def accent_color():
    return THEME['accent']


def accent2_color():
    return THEME['accent2']


def accent_rgb():
    return THEME['accent_rgb']


def text_color(level=1):
    return THEME.get(f'text{max(1, min(3, int(level)))}', THEME['text1'])


def frame_line_color():
    """窗口圆角描边（1px 高光/墨线）。"""
    return QColor(*THEME['frame'])


def acrylic_tint():
    """高斯模糊模式的窗口着色（RRGGBBAA，供系统亚克力 API 使用）。"""
    return THEME['acrylic']


def nav_background():
    """左侧导航栏底色（rgba 字符串）。"""
    return THEME['nav']

TEXT_COLOR_MAP = {
    '#f5f5f5': '#1A1D24', '#F5F5F7': '#1A1D24', '#F4F5F7': '#1A1D24', '#EDEDF0': '#1A1D24',
    '#ffffff': '#111420', '#FFFFFF': '#111420', '#E5E7EB': '#2A2F3A', '#C7CCD6': '#3A4150',
    '#f0f0f5': '#1A1D24', '#9AA6B8': '#4A5468',
    '#9ca3af': '#4F586A', '#9CA3AF': '#4F586A', '#8B93A3': '#4A5468', '#8b93a3': '#4A5468',
    '#6B7280': '#515B6C', '#7A8294': '#515B6C', '#92929b': '#5E6878',
    '#454B5A': '#9AA3B2', '#4B5563': '#8A93A2', '#D6DBFF': '#3A416E',
}

# 深色主题下把偏灰的辅助文字整体提亮一档，保证低亮度环境下的可读性。
DARK_TEXT_MAP = {
    '#9ca3af': '#A9B1C1', '#9CA3AF': '#A9B1C1',
    '#8B93A3': '#9AA3B5', '#8b93a3': '#9AA3B5',
    '#6B7280': '#818A9C', '#7A8294': '#8A93A5',
    '#92929b': '#9CA1AC', '#9AA6B8': '#AEB8C8',
}


def set_light_mode(enabled):
    global LIGHT_MODE
    LIGHT_MODE = bool(enabled)


def is_light():
    return LIGHT_MODE


def map_text_color(color):
    """把基础文字色映射到当前主题：浅色主题映射为深色文字，深色主题按需提亮。"""
    mapping = TEXT_COLOR_MAP if LIGHT_MODE else DARK_TEXT_MAP
    if isinstance(color, QColor):
        return QColor(mapping.get(color.name(), color.name()))
    return mapping.get(str(color), str(color))


def register_label(widget, color):
    _LABEL_REGISTRY.append((weakref.ref(widget), color))


def register_button(button, primary=False):
    _BUTTON_REGISTRY.append((weakref.ref(button), primary))


def register_theme_callback(callback):
    target = getattr(callback, '__self__', None)
    func = getattr(callback, '__func__', callback)
    if target is not None:
        _THEME_CALLBACKS.append((weakref.ref(target), func))
    else:
        _THEME_CALLBACKS.append((None, callback))


def run_theme_callbacks():
    for target_ref, func in list(_THEME_CALLBACKS):
        try:
            if target_ref is None:
                func()
            else:
                target = target_ref()
                if target is not None:
                    func(target)
        except Exception:
            pass


def ink(alpha):
    """描边 / 悬停等叠加色：深色主题用白、浅色主题用墨色。"""
    return QColor(*THEME['ink'], alpha)


def surface_fill(hover=0):
    r, g, b, base = THEME['surface']
    return QColor(r, g, b, min(255, base + round(THEME['surface_add'] * hover)))


def surface_border(hover=0, elevated=False):
    if elevated and not LIGHT_MODE:
        return QColor(*THEME['border_elev'])
    r, g, b, base = THEME['border']
    return QColor(r, g, b, min(255, base + round(THEME['border_add'] * hover)))


def apply_theme_to_labels():
    for ref, color in list(_LABEL_REGISTRY):
        widget = ref()
        if widget is None:
            continue
        try:
            mapped = map_text_color(color)
            name = mapped.name() if isinstance(mapped, QColor) else str(mapped)
            widget.setTextColor(QColor(name), QColor(name))
            widget.setStyleSheet(f'background:transparent; color:{name};')
        except (RuntimeError, AttributeError):
            continue


def restyle_theme_buttons():
    from .widgets.workspace_surface import restyle_buttons
    restyle_buttons()


def background_brush(rect):
    gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    gradient.setColorAt(0, QColor(THEME['bg1']))
    gradient.setColorAt(1, QColor(THEME['bg2']))
    return gradient


def paint_background(painter, rect, opaque=True):
    """主题基底（纵向渐变）叠加两束环境径向光。"""
    if LIGHT_MODE or opaque:
        painter.fillRect(rect, background_brush(rect))
    else:
        tint = QColor(THEME['bg1']); tint.setAlpha(200)
        painter.fillRect(rect, tint)
    span = max(rect.width(), rect.height())
    g1 = THEME['glow1']; g2 = THEME['glow2']
    blue = QRadialGradient(rect.topLeft(), span * 1.3)
    blue.setColorAt(0, QColor(g1[0], g1[1], g1[2], g1[3]))
    blue.setColorAt(1, QColor(g1[0], g1[1], g1[2], 0))
    painter.fillRect(rect, blue)
    purple = QRadialGradient(rect.bottomRight(), span * 1.3)
    purple.setColorAt(0, QColor(g2[0], g2[1], g2[2], g2[3]))
    purple.setColorAt(1, QColor(g2[0], g2[1], g2[2], 0))
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
    painter.fillPath(path, surface_fill(hover))
    if not LIGHT_MODE:
        painter.drawPixmap(rect, frost_texture(), QRectF(0, 0, 256, 256))
    if elevated:
        # Qt QSS has no inset box-shadow. Paint a soft 20px inset explicitly.
        _ar = THEME['accent_rgb']
        for inset in range(20, 0, -1):
            alpha = round(26 * math.exp(-inset/5))
            color = (QColor(*THEME['ink'], round(alpha * 1.4)) if LIGHT_MODE
                     else QColor(_ar[0], _ar[1], _ar[2], alpha))
            painter.setPen(QPen(color, 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(inset, inset, -inset, -inset), 14, 14)
    painter.restore()
    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(surface_border(hover, elevated), 1))
    painter.drawRoundedRect(rect, 14, 14)
    # A fine upper highlight gives the translucent surface a lit edge.
    highlight = QLinearGradient(rect.topLeft(), rect.topRight())
    highlight.setColorAt(0, QColor(255, 255, 255, 0))
    highlight.setColorAt(.45, QColor(255, 255, 255, THEME['highlight']))
    highlight.setColorAt(1, QColor(255, 255, 255, 0))
    painter.setPen(QPen(highlight, 1))
    painter.drawLine(QPointF(14, 1), QPointF(widget.width()-14, 1))


@lru_cache(maxsize=24)
def shadow_tile(hover=False, primary=False, accent=(91, 141, 239)):
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
                color = QColor(accent[0], accent[1], accent[2], round(18*weight / max(1, radius/3)))
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
        tile = shadow_tile(active, primary, tuple(THEME['accent_rgb']))
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
