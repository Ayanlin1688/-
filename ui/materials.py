"""Cached Fluent materials with one in-memory glass transition per native drag."""
from functools import lru_cache
import math
import weakref

from PyQt5.QtCore import Qt, QRectF, QPointF, QPoint, QSize
from PyQt5.QtGui import QColor, QFont, QFontDatabase, QPainter, QPainterPath, QPixmap, QLinearGradient, QRadialGradient, QPen
from PyQt5.QtWidgets import QApplication, QGraphicsEffect

from .palettes import THEMES, resolve as resolve_theme
from .tokens import RADIUS, px

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
_UI_FONT_FAMILY = None


def ui_font_family():
    """缓存界面字体族；缺少中文字体时回退到系统默认字体。"""
    global _UI_FONT_FAMILY
    if _UI_FONT_FAMILY is not None:
        return _UI_FONT_FAMILY
    app = QApplication.instance()
    # QFontDatabase 必须在 QApplication 创建后访问；早期导入不缓存结果。
    if app is None:
        return QFont().family() or 'Sans Serif'
    fallback = app.font().family() or QFont().family() or 'Sans Serif'
    try:
        available = set(QFontDatabase().families())
        _UI_FONT_FAMILY = next((family for family in (
            'Microsoft YaHei UI', 'Microsoft YaHei', 'Segoe UI'
        ) if family in available), fallback)
    except (RuntimeError, OSError):
        _UI_FONT_FAMILY = fallback
    return _UI_FONT_FAMILY


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

# 深色主题下把偏灰的辅助文字整体提亮（2026-09 P1 对比度审计：再抬一档，低亮度环境可读）。
DARK_TEXT_MAP = {
    '#9ca3af': '#B0B8C8', '#9CA3AF': '#B0B8C8',
    '#8B93A3': '#A8B0C0', '#8b93a3': '#A8B0C0',
    '#6B7280': '#8F98AB', '#7A8294': '#99A2B4',
    '#92929b': '#A6ABB6', '#9AA6B8': '#AEB8C8',
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
    alive = []
    for target_ref, func in list(_THEME_CALLBACKS):
        try:
            if target_ref is None:
                func()
            else:
                target = target_ref()
                if target is not None:
                    func(target)
                else:
                    continue  # 宿主已销毁：剔除死引用（注册表只增不删会让主题切换线性变慢）
        except RuntimeError:
            continue  # 宿主已析构：剔除
        except Exception:
            pass
        alive.append((target_ref, func))
    _THEME_CALLBACKS[:] = alive


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
    alive = []
    for ref, color in list(_LABEL_REGISTRY):
        widget = ref()
        if widget is None:
            continue  # 已销毁的弱引用：剔除
        try:
            mapped = map_text_color(color)
            name = mapped.name() if isinstance(mapped, QColor) else str(mapped)
            widget.setTextColor(QColor(name), QColor(name))
            widget.setStyleSheet(f'background:transparent; color:{name};')
        except (RuntimeError, AttributeError):
            continue  # 控件已析构或类型不符：剔除
        alive.append((ref, color))
    _LABEL_REGISTRY[:] = alive


def restyle_theme_buttons():
    from .widgets.workspace_surface import restyle_buttons
    restyle_buttons()


def background_brush(rect):
    gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    gradient.setColorAt(0, QColor(THEME['bg1']))
    if THEME.get('bgm'):
        gradient.setColorAt(0.5, QColor(THEME['bgm']))
    gradient.setColorAt(1, QColor(THEME['bg2']))
    return gradient


def paint_background(painter, rect, opaque=True):
    """主题基底（纵向渐变）叠加环境径向光（glow1/2 必需，glow3/4 可选）。"""
    if LIGHT_MODE or opaque:
        painter.fillRect(rect, background_brush(rect))
    else:
        tint = QColor(THEME['bg1']); tint.setAlpha(200)
        painter.fillRect(rect, tint)
    span = max(rect.width(), rect.height())
    scale = float(THEME.get('glow_scale', 1.3))
    anchors = (('glow1', rect.topLeft()), ('glow3', rect.topRight()),
               ('glow2', rect.bottomRight()), ('glow4', rect.bottomLeft()))
    for key, anchor in anchors:
        glow = THEME.get(key)
        if not glow:
            continue
        radial = QRadialGradient(anchor, span * scale)
        radial.setColorAt(0, QColor(glow[0], glow[1], glow[2], glow[3]))
        radial.setColorAt(1, QColor(glow[0], glow[1], glow[2], 0))
        painter.fillRect(rect, radial)


def paint_window_tint(painter, rect):
    """Constant-cost glass fallback during native interactive move/resize."""
    tint = acrylic_tint()
    color = QColor(*[int(tint[index:index + 2], 16) for index in (0, 2, 4, 6)])
    painter.save()
    painter.setCompositionMode(QPainter.CompositionMode_Source)
    painter.fillRect(rect, color)
    painter.restore()


def legacy_background_snapshot(window):
    """Keep a coarse, unreadable color field of the composited glass only in RAM."""
    screen = window.screen()
    if screen is None or not window.isVisible():
        return None
    margin = window._window_margins_px()
    size = window.size() - QSize(margin * 2, margin * 2)
    origin = window.mapToGlobal(QPoint(margin, margin)) - screen.geometry().topLeft()
    try:
        captured = screen.grabWindow(0, origin.x(), origin.y(), size.width(), size.height())
        if captured.isNull():
            return None
        coarse = captured.toImage().scaled(16, 10, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        captured = QPixmap.fromImage(coarse)
        return captured
    except (RuntimeError, OSError):
        return None


def window_background_snapshot(size, ratio):
    """Freeze only the decorative background, never text or interactive controls."""
    pixmap = QPixmap(max(1, math.ceil(size.width() * ratio)), max(1, math.ceil(size.height() * ratio)))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    try:
        paint_background(painter, QRectF(0, 0, size.width(), size.height()), opaque=True)
    finally:
        painter.end()
    return pixmap


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
    radius = RADIUS['container']
    path = QPainterPath(); path.addRoundedRect(rect, radius, radius)
    painter.save(); painter.setClipPath(path)
    painter.fillPath(path, surface_fill(hover))
    if not LIGHT_MODE:
        painter.drawPixmap(rect, frost_texture(), QRectF(0, 0, 256, 256))
    if elevated:
        # Qt QSS has no inset box-shadow. Paint a soft 20px inset explicitly.
        _ar = THEME['accent_rgb']
        for inset in range(px(20), 0, -1):
            alpha = round(26 * math.exp(-inset/max(1, px(5))))
            color = (QColor(*THEME['ink'], round(alpha * 1.4)) if LIGHT_MODE
                     else QColor(_ar[0], _ar[1], _ar[2], alpha))
            painter.setPen(QPen(color, 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(inset, inset, -inset, -inset), radius, radius)
    painter.restore()
    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(surface_border(hover, elevated), 1))
    painter.drawRoundedRect(rect, radius, radius)
    # A fine upper highlight gives the translucent surface a lit edge.
    highlight = QLinearGradient(rect.topLeft(), rect.topRight())
    highlight.setColorAt(0, QColor(255, 255, 255, 0))
    highlight.setColorAt(.45, QColor(255, 255, 255, THEME['highlight']))
    highlight.setColorAt(1, QColor(255, 255, 255, 0))
    painter.setPen(QPen(highlight, 1))
    painter.drawLine(QPointF(px(14), 1), QPointF(widget.width()-px(14), 1))


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
    extent = px(24)
    radius = RADIUS['container']
    target = rect.adjusted(-extent, -extent, extent, extent)
    # Translucent cards cannot hide the opaque centre of a shadow as an opaque
    # surface would. Cut out the whole rounded surface, not the nine-slice
    # centre alone, or the remaining 12px strips appear as a hard inner frame.
    outside = QPainterPath(); outside.addRect(target)
    surface = QPainterPath(); surface.addRoundedRect(rect.adjusted(1, 1, -1, -1), radius, radius)
    painter.save(); painter.setClipPath(outside.subtracted(surface))
    xs = [target.left(), rect.left()+radius, rect.right()-radius, target.right()]
    ys = [target.top(), rect.top()+radius, rect.bottom()-radius, target.bottom()]
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
        return rect.adjusted(-px(16), -px(14), px(16), px(20))

    def draw(self, painter):
        pixmap, offset = self.sourcePixmap(Qt.LogicalCoordinates, QGraphicsEffect.NoPad)
        if pixmap.isNull():
            return
        dpr = pixmap.devicePixelRatioF() or 1.0  # 防御 DPR=0（回归日志多次实抓），避免除零中断绘制
        rect = QRectF(offset.x(), offset.y(), pixmap.width()/dpr, pixmap.height()/dpr)
        draw_shadow(painter, rect, self.hover, self.primary)
        painter.drawPixmap(offset, pixmap)


# 电商图上传区 / 灯箱 / 任务列表的固定配色。参考软件为浅色设计，这里做
# 主题无关的语义色集中登记；主题相关的底色/文字仍从 palette() 取。
# 测试护栏：ui/ 其余文件不得再散落十六进制颜色字面量。
ECOMMERCE = {
    'white': '#FFFFFF',
    'danger': '#EF4444',            # 删除按钮 / 错误红
    'danger_hover': '#DC2626',
    'badge_bg': (0, 0, 0, 191),     # 图片序号底 #000000bf
    'badge_text': '#FFFFFF',
    'mask': (0, 0, 0, 217),         # 灯箱遮罩 #000000d9
    'nav_bg': (15, 23, 42, 184),    # 灯箱左右箭头底 #0f172ab8
    'nav_hover': (59, 130, 246, 230),
    'close_bg': (239, 68, 68, 179),
    'close_hover': '#DC2626',
    'lightbox_light_header': '#F5F5F7',
    'lightbox_light_body': '#F9FAFB',
    'lightbox_dark_header': '#1E1E2E',
    'lightbox_dark_body': '#14141A',
    'upload_tint': (0, 122, 255, 10),   # hover 背景 #007aff0a
    'upload_border_hover': (0, 122, 255, 255),
    'brand_blue': '#007AFF',            # 参考软件的强调蓝（平台预览/灯箱 tab）
    'brand_blue_soft': (0, 122, 255, 26),
    'chip_bg': (255, 255, 255, 20),
    'success': '#22C55E',
    'warning': '#F59E0B',
}


def rgba(rgb, alpha=None):
    """(r, g, b[, a]) → Qt 样式表可用的 rgba 字符串。"""
    red, green, blue = rgb[:3]
    if alpha is None:
        alpha = rgb[3] if len(rgb) > 3 else 255
    return 'rgba(%d,%d,%d,%s)' % (red, green, blue, alpha)


# 任务监看带 / 状态胶囊配色（浅色, 深色）双色对：颜色单一来源集中于此，
# 组件只引用键名（测试护栏：ui/ 其余文件的颜色字面量需保持收敛）。
MONITOR = {
    'live': ('#3E63C8', '#9CC2F5'),
    'title': ('#17191C', '#F4F5F7'),
    'title_error': ('#B3261E', '#F56C6C'),
    'dim': ('#5D6570', '#A9B1C1'),
    'state_failed': ('#B3261E', '#F1A0A0'),
    'state_completed': ('#2E8B57', '#7EDC9F'),
    'state_active': ('#3E63C8', '#9CC2F5'),
    'state_wait': ('#B7791F', '#EFC078'),
    'state_idle': ('#5D6570', '#A9B1C1'),
}

# 电商图任务列表 / 平台预览专用色（浅色, 深色）双色对。
# 任务列表沿用主题玻璃质感，只补少量中性底色；平台预览要模拟真实平台的
# 公开商品页，固定使用浅色页面 token（不随应用主题反转）。组件一律经
# ecommerce_color() 取用，避免在组件里散落颜色字面量。
ECOMMERCE_UI = {
    # 单个制作页：浅色参考布局与深色主题共用语义键。
    'single_card': ('#FFFFFF', '#181C28'),
    'single_card_border': ('#E9EDF5', '#303545'),
    'single_button_border': ('#E5E8EF', '#41485B'),
    'single_button_text': ('#1677FF', '#8DBBFF'),
    'single_button_hover': ('#F0F6FF', '#25364F'),
    'single_empty_icon': ('#94A3B8', '#9BAAC3'),
    'single_empty_text': ('#7F8A9C', '#BBC4D4'),
    'single_empty_secondary': ('#98A1B2', '#A6AFC0'),
    'thumb_bg': ('#EEF1F7', '#1B1E2B'),
    'chip_bg': ('#F1F3F8', '#202330'),
    'index_bg': ('#F1F3F8', '#202330'),
    'tag_bg': ('#EAF2FF', '#17233D'),
    'preview_page': '#F5F5F7',
    'preview_card': '#FFFFFF',
    'preview_text': '#1F2329',
    'preview_muted': '#8A8F99',
    'preview_line': '#E8EAF0',
    'preview_input': '#F2F3F5',
    'preview_white': '#FFFFFF',
}


def ecommerce_color(key):
    """取电商图组件专用色；双色对按当前主题（浅/深）选择。"""
    value = ECOMMERCE_UI[key]
    if isinstance(value, tuple):
        return value[0] if LIGHT_MODE else value[1]
    return value
