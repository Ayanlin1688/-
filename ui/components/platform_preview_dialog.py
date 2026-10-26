"""覆盖主窗口的离线平台预览；选择和排序只在当前预览会话内生效。"""
from pathlib import Path

from PyQt5.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QRegion
from PyQt5.QtWidgets import (
    QBoxLayout, QHBoxLayout, QLabel, QLayout, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)
from qfluentwidgets import Action, LineEdit, PushButton, RoundMenu
from qframelesswindow import FramelessDialog

from core.i18n import register_retranslate, tr
from .. import materials
from ..tokens import px
from .preview_selection_panel import PreviewSelectionPanel

_PREVIEW_CACHE = {}
_PREVIEW_CACHE_LIMIT = 32


def _preview_style(widget, rules):
    """Keep themed shell rules so a theme switch refreshes the existing shell."""
    widget._preview_style_rules = rules
    widget.setStyleSheet(rules())


def _preview_text(widget, source, prefix=''):
    """Retain source text so language changes preserve images and scroll state."""
    widget._preview_text_source = (source, prefix)
    widget.setText(prefix + tr(source))
    return widget


# ---------------------------------------------------------------------------
# 图片加载 / 通用小标签

def _load_image(value):
    if not value:
        return QImage()
    try:
        path = Path(str(value))
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        cached = _PREVIEW_CACHE.get(key)
        if cached is not None:
            return cached
        image = QImage.fromData(path.read_bytes())
        if image.isNull():
            return image
        if max(image.width(), image.height()) > 1280:
            image = image.scaled(1280, 1280, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        if len(_PREVIEW_CACHE) >= _PREVIEW_CACHE_LIMIT:
            _PREVIEW_CACHE.pop(next(iter(_PREVIEW_CACHE)))
        _PREVIEW_CACHE[key] = image
        return image
    except (OSError, ValueError, TypeError):
        return QImage()


def _label(size=13, bold=False, wrap=False, text='', parent=None):
    widget = QLabel(parent)
    widget.setTextFormat(Qt.PlainText)
    widget.setWordWrap(wrap)
    font = widget.font()
    # InterfaceScaler captures unscaled construction values, including dialogs
    # created after a scale change. Painting and dynamic geometry use px().
    font.setFamily(materials.ui_font_family())
    font.setPixelSize(size)
    font.setBold(bold)
    widget.setFont(font)
    if text:
        widget.setText(text)
    return widget


def _color(value, fallback):
    candidate = QColor(str(value or ''))
    return candidate if candidate.isValid() else QColor(fallback)


def _platform_color(platform, key):
    value = platform.get(key) if isinstance(platform, dict) else None
    return _color(value, materials.palette()['accent'])


def _platform_name(platform):
    if isinstance(platform, dict):
        return str(platform.get('name') or '')
    return str(platform or '')


def _platform_group(platform):
    if isinstance(platform, dict) and str(platform.get('group') or '') == 'overseas':
        return 'overseas'
    return 'domestic'


def _group_images(group):
    """从 manifest 取全部已生成图；quota 之外的 thumbs 作为轮播兜底。"""
    manifest = group.get('manifest') if isinstance(group.get('manifest'), dict) else {}
    items = [item for item in manifest.get('items') or () if isinstance(item, dict)]

    def pick(kind):
        selected = [item for item in items
                    if item.get('kind') == kind and item.get('status') == 'succeeded'
                    and item.get('file')]
        selected.sort(key=lambda item: item.get('seq') if type(item.get('seq')) is int else 0)
        return [str(item['file']) for item in selected]

    carousel, detail = pick('carousel'), pick('detail')
    if not carousel:
        carousel = [str(value) for value in group.get('thumbs') or () if value][:3]
    return carousel, detail


def _product_name(group):
    name = str(group.get('product_name') or '').strip()
    config = group.get('config') if isinstance(group.get('config'), dict) else {}
    if not name:
        name = str(config.get('product_name') or '').strip()
    if not name:
        name = str(group.get('group_id') or '')[:8]
    return name


def _disclaimer_text():
    return tr('仅供图片效果预览，界面布局模拟自各平台公开商品页，'
              '与相关平台无隶属关系，不代表任何平台官方立场。')


def _round_mask(widget, radius):
    """给外壳加圆角裁剪，让子控件也收进圆角里（固定/变化尺寸都适用）。"""
    if widget.width() <= 0 or widget.height() <= 0:
        return
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, widget.width(), widget.height()),
                        radius, radius)
    widget.setMask(QRegion(path.toFillPolygon().toPolygon()))


# ---------------------------------------------------------------------------
# 图片 / 占位

class PreviewImage(QWidget):
    """按 contain 绘制的真实图片；无图时由外壳换用占位标签。"""

    clicked = pyqtSignal()

    def __init__(self, path='', height=220, parent=None, *, edge=0):
        super().__init__(parent)
        self.image_path = str(path) if path else ''
        self.has_image = False
        self._edge = int(edge)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setFixedHeight(int(height))
        self.setMinimumWidth(0)
        self.setCursor(Qt.PointingHandCursor)
        self.set_image(self.image_path)

    def set_image(self, path):
        self.image_path = str(path) if path else ''
        self._image = _load_image(self.image_path)
        self.has_image = not self._image.isNull()
        self.setToolTip(self.image_path)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            rect = QRectF(self.rect())
            radius = px(self._edge) if self._edge else 0
            if radius:
                clip = QPainterPath()
                clip.addRoundedRect(rect, radius, radius)
                painter.setClipPath(clip)
            painter.fillRect(rect, QColor(materials.ecommerce_color('preview_input')))
            if not getattr(self, '_image', QImage()).isNull():
                source = self._image
                scale = min(rect.width() / max(1, source.width()),
                            rect.height() / max(1, source.height()))
                width = max(1, round(source.width() * scale))
                height = max(1, round(source.height() * scale))
                target = QRectF((rect.width() - width) / 2,
                                (rect.height() - height) / 2, width, height)
                painter.drawImage(target, source, QRectF(source.rect()))
            elif self.image_path:
                painter.setPen(QColor(materials.ecommerce_color('preview_muted')))
                painter.drawText(self.rect(), Qt.AlignCenter, tr('缺失'))
        finally:
            painter.end()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class PreviewPlaceholder(QLabel):
    """无生成图时的占位。"""

    def __init__(self, height=220, parent=None, text=None):
        super().__init__(parent)
        self.setTextFormat(Qt.PlainText)
        self.setAlignment(Qt.AlignCenter)
        self.setWordWrap(True)
        self.setFixedHeight(int(height))
        if text is None:
            _preview_text(self, '暂无生成图片')
        else:
            self.setText(text)
        _preview_style(self, lambda: 'color:%s; background:%s; border-radius:%dpx;'
                           % (materials.ecommerce_color('preview_muted'),
                              materials.ecommerce_color('preview_input'), 8))


class _CarouselDots(QWidget):
    def __init__(self, count, parent=None, color=None):
        super().__init__(parent)
        self.count = max(0, int(count))
        self.index = 0
        self.color = QColor(color or materials.palette()['accent'])
        self.setFixedHeight(14)

    def set_index(self, index):
        self.index = max(0, min(int(index), max(0, self.count - 1)))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(Qt.NoPen)
            dot, gap = px(5), px(4)
            total = self.count * dot + max(0, self.count - 1) * gap
            left = (self.width() - total) / 2
            for position in range(self.count):
                active = position == self.index
                color = QColor(self.color) if active else QColor(materials.ecommerce_color('preview_muted'))
                color.setAlpha(255 if active else 90)
                width = px(12) if active else dot
                painter.setBrush(color)
                painter.drawRoundedRect(
                    QRectF(left, (self.height() - dot) / 2, width, dot),
                    dot / 2, dot / 2)
                left += width + gap
        finally:
            painter.end()


class _SignalIcons(QWidget):
    """状态栏右侧的信号 / WiFi / 电池图形（纯自绘，不依赖字体图标）。"""

    def __init__(self, color, parent=None):
        super().__init__(parent)
        self.color = QColor(color)
        self.setFixedSize(64, 16)

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            self.color = QColor(materials.ecommerce_color('preview_text'))
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.color)
            unit = px(2)
            base = self.height() - px(2)
            for step, height in enumerate((3, 5, 7, 9)):
                bar_height = px(height)
                painter.drawRoundedRect(
                    QRectF(px(1) + step * (unit + px(1)), base - bar_height,
                           unit, bar_height), unit / 2, unit / 2)
            wifi_left = px(26)
            wifi = QPainterPath()
            wifi.moveTo(wifi_left + px(1), self.height() - px(3))
            wifi.arcTo(QRectF(wifi_left, px(1), px(13), px(13)), 40, 100)
            wifi.moveTo(wifi_left + px(2), self.height() - px(6))
            wifi.arcTo(QRectF(wifi_left + px(2), px(3), px(11), px(11)), 40, 100)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(self.color, max(1, px(1))))
            painter.drawPath(wifi)
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.color)
            painter.drawEllipse(QRectF(wifi_left + px(4), self.height() - px(5),
                                       px(3), px(3)))
            battery_left = px(44)
            battery = QRectF(battery_left, px(3), px(17), px(10))
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(self.color, max(1, px(1))))
            painter.drawRoundedRect(battery, px(2), px(2))
            painter.setBrush(self.color)
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(QRectF(battery_left + px(2), px(5), px(11), px(6)),
                                    px(1), px(1))
            painter.drawRoundedRect(QRectF(battery_left + px(18), px(6), px(2), px(4)),
                                    px(1), px(1))
        finally:
            painter.end()


class _PlatformPill(QPushButton):
    """用图形绘制菜单箭头，避免平台字体缺少三角形字形。"""

    has_menu = False

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.has_menu:
            painter = QPainter(self)
            try:
                painter.setRenderHint(QPainter.Antialiasing)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(materials.ecommerce_color('preview_overlay_text')))
                x, y = self.width() - px(15), self.height() / 2
                arrow = QPainterPath()
                arrow.moveTo(x - px(3), y - px(2))
                arrow.lineTo(x + px(3), y - px(2))
                arrow.lineTo(x, y + px(2))
                arrow.closeSubpath()
                painter.drawPath(arrow)
            finally:
                painter.end()


class _StatusBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(26)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 14, 0)
        layout.setSpacing(6)
        self.time_label = _label(11, bold=True, text='9:41')
        _preview_style(self.time_label, lambda: 'color:%s; background:transparent;' % materials.ecommerce_color('preview_text'))
        self.icons = _SignalIcons(materials.ecommerce_color('preview_text'), self)
        layout.addWidget(self.time_label)
        layout.addStretch(1)
        layout.addWidget(self.icons)


# ---------------------------------------------------------------------------
# 两套外壳

class _PhoneShell(QWidget):
    """海外商品页；内部内容滚动，底部操作条留在手机壳内。"""

    kind = 'mobile'

    def __init__(self, platform, product_name, carousel, detail, parent=None):
        super().__init__(parent)
        self.platform = dict(platform or {})
        self.product_name = product_name
        self.carousel_paths, self.detail_paths = list(carousel), list(detail)
        self.primary = _platform_color(self.platform, 'primaryColor')
        self.accent = _platform_color(self.platform, 'accentColor')
        self.carousel_index = 0
        self._available_width = None
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setMinimumWidth(0)
        self.setFixedHeight(730)
        self.resize(400, 730)
        self.setObjectName('previewPhoneShell')
        self.setAttribute(Qt.WA_StyledBackground, True)
        _preview_style(self, lambda: '#previewPhoneShell {background:%s; border-radius:20px;}'
                          % materials.ecommerce_color('preview_card'))
        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)

        self.status_bar = _StatusBar(self)
        self.status_bar.setObjectName('previewPhoneStatus')
        _preview_style(self.status_bar, lambda: '#previewPhoneStatus {background:%s;}'
                                     % materials.ecommerce_color('preview_pill_bg'))
        root.addWidget(self.status_bar)
        self.nav_bar = QWidget(self)
        self.nav_bar.setObjectName('previewPhoneNav')
        self.nav_bar.setFixedHeight(44)
        _preview_style(self.nav_bar, lambda: '#previewPhoneNav {background:%s;}'
                                  % materials.ecommerce_color('preview_pill_bg'))
        nav = QHBoxLayout(self.nav_bar)
        nav.setContentsMargins(14, 0, 14, 0)
        nav.setSpacing(8)
        self.back_label = _label(22, text='‹')
        self.name_label = _preview_text(_label(14, bold=True), 'Product Details')
        self.name_label.setAlignment(Qt.AlignCenter)
        self.cart_label = _label(20, text='⋯')
        for label in (self.back_label, self.name_label, self.cart_label):
            _preview_style(label, lambda: 'color:%s; background:transparent;'
                               % materials.ecommerce_color('preview_text'))
        nav.addWidget(self.back_label)
        nav.addWidget(self.name_label, 1)
        nav.addWidget(self.cart_label)
        root.addWidget(self.nav_bar)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName('previewPhoneScroll')
        self.scroll.setMinimumWidth(0)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet('QScrollArea {background:transparent; border:0;}')
        content = QWidget()
        content.setMinimumWidth(0)
        content.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        content.setObjectName('previewPhoneBody')
        _preview_style(content, lambda: '#previewPhoneBody {background:%s;}'
                             % materials.ecommerce_color('preview_card'))
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.image_area = QWidget(content)
        image_layout = QVBoxLayout(self.image_area)
        image_layout.setContentsMargins(0, 0, 0, 0)
        image_layout.setSpacing(0)
        if self.carousel_paths:
            self.main_image = PreviewImage(self.carousel_paths[0], height=340)
            self.main_image.clicked.connect(self._next_image)
            image_layout.addWidget(self.main_image)
        else:
            self.main_image = None
            self.placeholder = PreviewPlaceholder(height=340)
            image_layout.addWidget(self.placeholder)
            self.selection_hint = _preview_text(_label(12), '请勾选轮播图')
            self.selection_hint.setAlignment(Qt.AlignCenter)
            _preview_style(self.selection_hint, lambda: 'color:%s; background:transparent;'
                                             % materials.ecommerce_color('preview_muted'))
            image_layout.addWidget(self.selection_hint)
        self.dots = _CarouselDots(len(self.carousel_paths), self.image_area,
                                  materials.ecommerce_color('preview_sale'))
        self.dots.setVisible(len(self.carousel_paths) > 1)
        image_layout.addWidget(self.dots)
        self.counter_label = _label(11, text=self._counter_text(), parent=self.image_area)
        self.counter_label.setAlignment(Qt.AlignCenter)
        self.counter_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        _preview_style(self.counter_label, lambda: 'color:%s; background:%s; border-radius:10px; padding:2px 7px;'
                                        % (materials.ecommerce_color('preview_white'),
                                           materials.ecommerce_color('preview_badge_bg')))
        self.counter_label.setVisible(bool(self.carousel_paths))
        self.image_area.installEventFilter(self)
        body.addWidget(self.image_area)

        info = QWidget(content)
        info.setObjectName('previewPhoneInfo')
        info.setAttribute(Qt.WA_StyledBackground, True)
        _preview_style(info, lambda: '#previewPhoneInfo {background:%s;}'
                          % materials.ecommerce_color('preview_card'))
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(16, 10, 16, 14)
        info_layout.setSpacing(7)
        self.sale_label = _preview_text(_label(10, bold=True), 'SALE')
        self.sale_label.setAlignment(Qt.AlignCenter)
        _preview_style(self.sale_label, lambda: 'color:%s; background:%s; border-radius:9px; padding:3px 6px;'
                                     % (materials.ecommerce_color('preview_white'),
                                        materials.ecommerce_color('preview_sale')))
        prices = QHBoxLayout()
        prices.setSpacing(8)
        prices.addWidget(self.sale_label, 0, Qt.AlignVCenter)
        self.price_label = _label(25, bold=True, text=self._price_text())
        _preview_style(self.price_label, lambda: 'color:%s; background:transparent;'
                                      % materials.ecommerce_color('preview_sale'))
        self.old_price_label = _label(12, text='$29.98')
        font = self.old_price_label.font()
        font.setStrikeOut(True)
        self.old_price_label.setFont(font)
        _preview_style(self.old_price_label, lambda: 'color:%s; background:transparent;'
                                          % materials.ecommerce_color('preview_muted'))
        prices.addWidget(self.price_label)
        prices.addWidget(self.old_price_label)
        prices.addStretch(1)
        info_layout.addLayout(prices)
        self.sold_label = _preview_text(_label(11), '12.8K sold')
        _preview_style(self.sold_label, lambda: 'color:%s; background:transparent;'
                                     % materials.ecommerce_color('preview_muted'))
        info_layout.addWidget(self.sold_label)
        self.product_label = _label(14, bold=True, wrap=True, text=self.product_name)
        self.product_label.setMinimumWidth(0)
        self.product_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        _preview_style(self.product_label, lambda: 'color:%s; background:transparent;'
                                        % materials.ecommerce_color('preview_text'))
        info_layout.addWidget(self.product_label)
        services = QHBoxLayout()
        services.setSpacing(6)
        for source in ('Free Shipping', '30-day Returns', 'Fast Delivery'):
            badge = _preview_text(_label(9), source)
            badge.setAlignment(Qt.AlignCenter)
            badge.setMinimumWidth(0)
            badge.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            _preview_style(badge, lambda: 'color:%s; background:%s; border-radius:9px; padding:4px 5px;'
                               % (materials.ecommerce_color('preview_text'),
                                  materials.ecommerce_color('preview_input')))
            services.addWidget(badge, 1)
        info_layout.addLayout(services)
        body.addWidget(info)
        self.detail_title = _preview_text(_label(13, bold=True), '商品详情')
        _preview_style(self.detail_title, lambda: 'color:%s; background:%s; padding:8px 14px;'
                                       % (materials.ecommerce_color('preview_text'),
                                          materials.ecommerce_color('preview_input')))
        body.addWidget(self.detail_title)
        self.detail_images = []
        for path in self.detail_paths:
            image = PreviewImage(path, height=300)
            self.detail_images.append(image)
            body.addWidget(image)
        if not self.detail_images:
            self.detail_placeholder = PreviewPlaceholder(height=180)
            self.detail_images.append(self.detail_placeholder)
            body.addWidget(self.detail_placeholder)
        body.addStretch(1)
        self.scroll.setWidget(content)
        root.addWidget(self.scroll, 1)

        self.bottom_bar = QWidget(self)
        self.bottom_bar.setFixedHeight(60)
        _preview_style(self.bottom_bar, lambda: 'background:%s;'
                                     % materials.ecommerce_color('preview_card'))
        bottom = QHBoxLayout(self.bottom_bar)
        bottom.setContentsMargins(10, 8, 10, 8)
        bottom.setSpacing(6)
        for symbol, source in (('⌂', 'Home'), ('♧', 'Chat'), ('♡', 'Save')):
            label = _preview_text(_label(9), source, symbol + '\n')
            label.setAlignment(Qt.AlignCenter)
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            _preview_style(label, lambda: 'color:%s; background:transparent;'
                               % materials.ecommerce_color('preview_text'))
            bottom.addWidget(label, 1)
        self.cart_button = _preview_text(QPushButton(self.bottom_bar), 'Add to Cart')
        self.buy_button = _preview_text(QPushButton(self.bottom_bar), 'Buy Now')
        for button, filled in ((self.cart_button, False), (self.buy_button, True)):
            button.setMinimumWidth(0)
            button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
            _preview_style(button, lambda filled=filled: 'QPushButton {border:1px solid %s; border-radius:6px; '
                                 'color:%s; background:%s; font-size:11px; font-weight:600;}'
                                 % (materials.ecommerce_color('preview_sale'),
                                    materials.ecommerce_color('preview_white' if filled else 'preview_sale'),
                                    materials.ecommerce_color('preview_sale' if filled else 'preview_card')))
            bottom.addWidget(button, 3)
        root.addWidget(self.bottom_bar)

    def sizeHint(self):
        width = px(400)
        if self._available_width is not None:
            width = min(width, self._available_width)
        return QSize(max(1, width), px(730))

    def minimumSizeHint(self):
        return QSize(0, 0)

    def fit_width(self, available):
        self._available_width = max(1, available)
        self.updateGeometry()

    def _counter_text(self):
        return '%d/%d' % (self.carousel_index + 1, len(self.carousel_paths)) if self.carousel_paths else ''

    def _price_text(self):
        return '$19.99'

    def _next_image(self):
        self.set_carousel_index((self.carousel_index + 1) % max(1, len(self.carousel_paths)))

    def set_carousel_index(self, index):
        if not self.carousel_paths:
            return
        self.carousel_index = max(0, min(int(index), len(self.carousel_paths) - 1))
        self.main_image.set_image(self.carousel_paths[self.carousel_index])
        self.dots.set_index(self.carousel_index)
        self.counter_label.setText(self._counter_text())
        self._place_counter()

    def _place_counter(self):
        if self.carousel_paths:
            self.counter_label.adjustSize()
            self.counter_label.move(max(0, self.image_area.width() - self.counter_label.width() - px(12)),
                                    max(0, self.main_image.height() - self.counter_label.height() - px(10)))
            self.counter_label.raise_()

    def eventFilter(self, watched, event):
        if watched is self.image_area and event.type() == QEvent.Resize:
            self._place_counter()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        _round_mask(self, px(20))
        self._place_counter()

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(materials.ecommerce_color('preview_line')), 1))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), px(20), px(20))
        finally:
            painter.end()


class _DesktopShell(QWidget):
    """国内平台：平台名顶栏 + 搜索框 + 主图 + 详情堆叠的通用桌面壳。"""

    kind = 'desktop'

    def __init__(self, platform, product_name, carousel, detail, parent=None):
        super().__init__(parent)
        self.platform = dict(platform or {})
        self.product_name = product_name
        self.carousel_paths = list(carousel)
        self.detail_paths = list(detail)
        self.primary = _platform_color(self.platform, 'primaryColor')
        self.accent = _platform_color(self.platform, 'accentColor')
        self.carousel_index = 0
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setAttribute(Qt.WA_StyledBackground, True)
        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)

        self.nav_bar = QWidget(self)
        self.nav_bar.setObjectName('previewDesktopNav')
        self.nav_bar.setFixedHeight(56)
        _preview_style(self.nav_bar, lambda: '#previewDesktopNav {background:%s; border-bottom:2px solid %s;}'
            % (materials.ecommerce_color('preview_card'), self.primary.name()))
        nav = QHBoxLayout(self.nav_bar)
        nav.setContentsMargins(18, 0, 18, 0)
        nav.setSpacing(14)
        self.name_label = _label(19, bold=True, text=_platform_name(self.platform))
        self.name_label.setStyleSheet('color:%s; background:transparent;'
                                      % self.primary.name())
        self.search_edit = LineEdit(self.nav_bar)
        self.search_edit.setPlaceholderText(self.product_name)
        self.search_edit.setReadOnly(True)
        self.search_edit.setMinimumWidth(0)
        self.search_edit.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.search_edit.setMinimumHeight(32)
        _preview_style(self.search_edit, lambda: 'LineEdit {border:2px solid %s; border-radius:%dpx; background:%s;'
            ' color:%s; padding:0 %dpx; font-size:%dpx;}'
            % (self.primary.name(), 8, materials.ecommerce_color('preview_input'),
               materials.ecommerce_color('preview_text'), 10, 12))
        self.search_button = _preview_text(PushButton(self.nav_bar), '搜索')
        self.search_button.setMinimumHeight(32)
        _preview_style(self.search_button, lambda: 'QPushButton {border:0; border-radius:%dpx; background:%s; color:%s;'
            ' padding:0 %dpx; font-size:%dpx; font-weight:600;}'
            % (8, self.primary.name(), materials.ecommerce_color('preview_white'),
               18, 13))
        nav.addWidget(self.name_label)
        nav.addWidget(self.search_edit, 1)
        nav.addWidget(self.search_button)
        root.addWidget(self.nav_bar)

        main = QWidget(self)
        main.setObjectName('previewDesktopMain')
        _preview_style(main, lambda: '#previewDesktopMain {background:%s;}'
                           % materials.ecommerce_color('preview_card'))
        main_layout = QHBoxLayout(main)
        self.main_layout = main_layout
        main_layout.setContentsMargins(18, 18, 18, 18)
        main_layout.setSpacing(18)
        gallery = QVBoxLayout()
        gallery.setContentsMargins(0, 0, 0, 0)
        gallery.setSpacing(6)
        if self.carousel_paths:
            self.main_image = PreviewImage(self.carousel_paths[0], height=320, edge=6)
            self.main_image.clicked.connect(self._next_image)
            gallery.addWidget(self.main_image)
        else:
            self.main_image = None
            self.placeholder = PreviewPlaceholder(height=320)
            gallery.addWidget(self.placeholder)
        self.small_images = []
        for path in self.carousel_paths[:4]:
            small = PreviewImage(path, height=64, edge=4)
            small.setFixedWidth(64)
            self.small_images.append(small)
        thumbs = QHBoxLayout()
        thumbs.setContentsMargins(0, 0, 0, 0)
        thumbs.setSpacing(6)
        for small in self.small_images:
            thumbs.addWidget(small)
        thumbs.addStretch(1)
        gallery.addLayout(thumbs)
        main_layout.addLayout(gallery, 3)
        info = QVBoxLayout()
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(8)
        self.product_label = _label(17, bold=True, wrap=True, text=self.product_name)
        _preview_style(self.product_label, lambda: 'color:%s; background:transparent;'
            % materials.ecommerce_color('preview_text'))
        self.price_label = _label(26, bold=True, text='¥ 199.00')
        self.price_label.setStyleSheet('color:%s; background:transparent;'
                                       % self.accent.name())
        self.meta_label = _preview_text(_label(11), '已售 1000+ · 好评率 98%')
        _preview_style(self.meta_label, lambda: 'color:%s; background:transparent;'
            % materials.ecommerce_color('preview_muted'))
        self.product_label.setMinimumWidth(0)
        self.product_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        info.addWidget(self.product_label)
        info.addWidget(self.price_label)
        info.addWidget(self.meta_label)
        for source, filled in (('加入购物车', False), ('立即购买', True)):
            button = _preview_text(PushButton(main), source)
            button.setMinimumHeight(34)
            if filled:
                _preview_style(button, lambda: 'QPushButton {border:0; border-radius:%dpx; background:%s; color:%s;'
                    ' font-size:%dpx; font-weight:600;}'
                    % (6, self.accent.name(),
                       materials.ecommerce_color('preview_white'), 13))
            else:
                button.setStyleSheet(
                    'QPushButton {border:2px solid %s; border-radius:%dpx;'
                    ' background:transparent; color:%s; font-size:%dpx; font-weight:600;}'
                    % (self.primary.name(), 6, self.primary.name(), 13))
            info.addWidget(button)
        info.addStretch(1)
        main_layout.addLayout(info, 2)
        root.addWidget(main)

        self.detail_title = _preview_text(_label(14, bold=True), '商品详情')
        _preview_style(self.detail_title, lambda: 'color:%s; background:%s; padding:%dpx %dpx; border-top:1px solid %s;'
            % (materials.ecommerce_color('preview_text'),
               materials.ecommerce_color('preview_page'), 10, 18,
               materials.ecommerce_color('preview_line')))
        root.addWidget(self.detail_title)
        self.detail_images = []
        if self.detail_paths:
            for path in self.detail_paths:
                image = PreviewImage(path, height=300, edge=0)
                self.detail_images.append(image)
                root.addWidget(image)
        else:
            self.detail_placeholder = PreviewPlaceholder(height=180)
            self.detail_images = [self.detail_placeholder]
            root.addWidget(self.detail_placeholder)
        root.addStretch(1)

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            painter.setBrush(QColor(materials.ecommerce_color('preview_card')))
            painter.setPen(QPen(QColor(materials.ecommerce_color('preview_line')), 1))
            painter.drawRoundedRect(rect, px(12), px(12))
        finally:
            painter.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.main_layout.setDirection(QBoxLayout.TopToBottom if self.width() < px(700)
                                      else QBoxLayout.LeftToRight)
        _round_mask(self, px(12))
        self.update()


# ---------------------------------------------------------------------------
    def set_carousel_index(self, index):
        if not self.carousel_paths:
            return
        self.carousel_index = max(0, min(int(index), len(self.carousel_paths) - 1))
        self.main_image.set_image(self.carousel_paths[self.carousel_index])

    def _next_image(self):
        self.set_carousel_index((self.carousel_index + 1) % max(1, len(self.carousel_paths)))


# 对话框

class PlatformPreviewDialog(FramelessDialog):
    """全窗口预览。平台切换和受控刷新保留当前勾选及排序。"""

    def __init__(self, parent, group, stage_text=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setWindowModality(Qt.WindowModal)
        self.setModal(True)
        self.titleBar.hide()
        self.titleBar.setFixedHeight(0)
        self.setResizeEnabled(False)
        self.setObjectName('platformPreviewOverlay')
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.group = dict(group or {}) if isinstance(group, dict) else {}
        self.stage_text = stage_text if callable(stage_text) else None
        self.platforms = [platform for platform in (self.group.get('platforms') or ())
                          if isinstance(platform, dict)]
        self._platform_index = 0
        self._layout_ready = False
        self.shell = None
        self.shell_kind = ''
        self.carousel, self.detail = _group_images(self.group)
        self._cover_parent = parent.window() if parent is not None else None
        self._cover_parent_geometry()
        root = QVBoxLayout(self)
        root.setSizeConstraint(QLayout.SetNoConstraint)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.overlay_header = QWidget(self)
        self.overlay_header.setObjectName('previewOverlayHeader')
        self.overlay_header.setFixedHeight(48)
        header = QHBoxLayout(self.overlay_header)
        header.setContentsMargins(20, 0, 20, 0)
        header.setSpacing(12)
        self.platform_button = _PlatformPill(self.overlay_header)
        self.platform_button.setFixedHeight(32)
        self.platform_button.setCursor(Qt.PointingHandCursor)
        self.platform_menu = RoundMenu(parent=self)
        self.platform_button.clicked.connect(self._show_platform_menu)
        header.addWidget(self.platform_button)
        header.addStretch(1)
        self.header_separator = QWidget(self.overlay_header)
        self.header_separator.setFixedSize(1, 20)
        header.addWidget(self.header_separator)
        self.close_button = QPushButton('×', self.overlay_header)
        self.close_button.setFixedSize(32, 32)
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.clicked.connect(self.close)
        header.addWidget(self.close_button)
        root.addWidget(self.overlay_header)

        self.disclaimer_bar = QWidget(self)
        self.disclaimer_bar.setObjectName('previewDisclaimerBar')
        self.disclaimer_bar.setFixedHeight(28)
        disclaimer = QHBoxLayout(self.disclaimer_bar)
        disclaimer.setContentsMargins(20, 0, 20, 0)
        disclaimer.setSpacing(6)
        self.disclaimer_icon = _label(12, text='ⓘ', parent=self.disclaimer_bar)
        self.disclaimer_label = _label(11, wrap=True, parent=self.disclaimer_bar)
        self.disclaimer_label.setMinimumWidth(0)
        self.disclaimer_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        disclaimer.addWidget(self.disclaimer_icon)
        disclaimer.addWidget(self.disclaimer_label, 1)
        root.addWidget(self.disclaimer_bar)

        self.canvas = QWidget(self)
        self.canvas.setObjectName('previewCanvas')
        body = QHBoxLayout(self.canvas)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.scroll = QScrollArea(self.canvas)
        self.scroll.setObjectName('previewShellScroll')
        self.scroll.setMinimumWidth(0)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.viewport().setObjectName('previewShellViewport')
        self.scroll.viewport().installEventFilter(self)
        self.shell_host = QWidget()
        self.shell_host.setObjectName('previewShellHost')
        self.shell_host.setMinimumWidth(0)
        self.shell_host.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.shell_layout = QVBoxLayout(self.shell_host)
        self.shell_layout.setContentsMargins(12, 12, 12, 12)
        self.shell_layout.setSpacing(0)
        self.scroll.setWidget(self.shell_host)
        body.addWidget(self.scroll, 1)
        self.selection_panel = PreviewSelectionPanel(self._image_records('carousel'),
                                                     self._image_records('detail'), self.canvas)
        self.selection_panel.setFixedWidth(260)
        self.selection_panel.selection_changed.connect(self._build_shell)
        body.addWidget(self.selection_panel)
        root.addWidget(self.canvas, 1)
        self.retranslate_ui()
        self.refresh_colors()
        register_retranslate(self)
        materials.register_theme_callback(self.refresh_colors)
        # Let InterfaceScaler capture the unscaled construction constraints
        # before applying the responsive current-scale widths.
        QTimer.singleShot(0, self._finish_layout)

    def _image_records(self, kind):
        manifest = self.group.get('manifest')
        items = (manifest.get('items') or ()) if isinstance(manifest, dict) else ()
        records = [item for item in items if isinstance(item, dict)
                   and item.get('kind') == kind and item.get('status') == 'succeeded'
                   and item.get('file')]
        records.sort(key=lambda item: item.get('seq') if type(item.get('seq')) is int else 0)
        return records or (self.carousel if kind == 'carousel' else self.detail)

    def _cover_parent_geometry(self):
        if self._cover_parent is not None:
            self.setGeometry(QRect(self._cover_parent.mapToGlobal(QPoint()), self._cover_parent.size()))
        else:
            self.resize(1600, 900)

    @property
    def current_index(self):
        return self._platform_index

    @property
    def current_platform(self):
        return self.platforms[self.current_index] if self.platforms else {}

    @property
    def primary_color(self):
        return _platform_color(self.current_platform, 'primaryColor')

    @property
    def accent_color(self):
        return _platform_color(self.current_platform, 'accentColor')

    @property
    def is_mobile(self):
        return self.shell_kind == 'mobile'

    def selected_images(self, kind):
        return self.selection_panel.selected_images(kind)

    def selected_count(self, kind):
        return self.selection_panel.selected_count(kind)

    def toggle_image(self, kind, index):
        self.selection_panel.toggle_image(kind, index)

    def clear_selection(self, kind):
        self.selection_panel.clear_selection(kind)

    def move_image(self, kind, from_index, to_index):
        self.selection_panel.move_image(kind, from_index, to_index)

    def set_platform_index(self, index):
        if type(index) is not int or not 0 <= index < len(self.platforms):
            return
        if index != self._platform_index:
            self._platform_index = index
            self._update_platform_button()
            self._build_shell()

    def _update_platform_button(self):
        name = _platform_name(self.current_platform) or tr('未选平台')
        self.platform_button.has_menu = len(self.platforms) > 1
        self.platform_button.setText('●  ' + name + ('     ' if self.platform_button.has_menu else ''))
        self.platform_button.setToolTip(tr('切换预览平台'))
        self.platform_button.setAccessibleName(name)

    def _show_platform_menu(self):
        if len(self.platforms) > 1:
            self.platform_menu.exec(self.platform_button.mapToGlobal(QPoint(0, self.platform_button.height())))

    def _clear_shell(self):
        while self.shell_layout.count():
            item = self.shell_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        self.shell = None

    def _build_shell(self):
        self._clear_shell()
        shell_type = _PhoneShell if _platform_group(self.current_platform) == 'overseas' else _DesktopShell
        self.shell = shell_type(self.current_platform, _product_name(self.group),
                                self.selected_images('carousel'), self.selected_images('detail'),
                                self.shell_host)
        self.shell_kind = self.shell.kind
        self.shell_layout.addWidget(self.shell, 0, Qt.AlignHCenter if self.is_mobile else Qt.Alignment())
        self.shell_layout.addStretch(1)
        self.scroll.verticalScrollBar().setValue(0)
        if self._layout_ready:
            # Newly created shell constraints must be captured before fitting.
            QTimer.singleShot(0, self._fit_shell_width)

    def _finish_layout(self):
        self._layout_ready = True
        self._fit_columns()

    def _fit_columns(self):
        if not self._layout_ready:
            return
        compact = self.width() < px(1100)
        self.selection_panel.setFixedWidth(px(232 if compact else 260))
        self.selection_panel.set_columns(2 if compact else 3)
        self._fit_shell_width()

    def _fit_shell_width(self):
        if self.shell is not None and self.is_mobile:
            margins = self.shell_layout.contentsMargins()
            self.shell.fit_width(max(1, self.scroll.viewport().width() - margins.left() - margins.right()))

    def eventFilter(self, watched, event):
        scroll = getattr(self, 'scroll', None)
        if scroll is not None and watched is scroll.viewport() and event.type() == QEvent.Resize:
            if self._layout_ready:
                self._fit_shell_width()
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        super().showEvent(event)
        if self._cover_parent is not None:
            self._cover_parent_geometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if getattr(self, '_layout_ready', False):
            self._fit_columns()

    def retranslate_ui(self):
        self.setWindowTitle(tr('平台预览'))
        self.disclaimer_label.setText(_disclaimer_text())
        self.close_button.setToolTip(tr('关闭平台预览'))
        self.close_button.setAccessibleName(tr('关闭平台预览'))
        self._update_platform_button()
        self.platform_menu.clear()
        for index, platform in enumerate(self.platforms or [{}]):
            action = Action(_platform_name(platform) or tr('未选平台'), self)
            action.triggered.connect(lambda checked=False, value=index: self.set_platform_index(value))
            self.platform_menu.addAction(action)
        self.selection_panel.retranslate_ui()
        if self.shell is None:
            self._build_shell()
        else:
            for widget in [self.shell, *self.shell.findChildren(QWidget)]:
                source = getattr(widget, '_preview_text_source', None)
                if source is not None:
                    widget.setText(source[1] + tr(source[0]))
                widget.update()

    def refresh_colors(self):
        color = materials.ecommerce_color
        self.setStyleSheet('QWidget#platformPreviewOverlay {background:%s;}' % color('preview_canvas'))
        self.overlay_header.setStyleSheet('QWidget#previewOverlayHeader {background:%s;}'
                                         % color('preview_overlay_header'))
        self.platform_button.setStyleSheet('QPushButton {background:%s; color:%s; border:1px solid %s; '
                                           'border-radius:16px; padding:0 12px; font-size:12px;}'
                                           % (color('preview_pill_bg'), color('preview_overlay_text'),
                                              color('preview_overlay_border')))
        self.header_separator.setStyleSheet('background:%s;' % color('preview_overlay_border'))
        self.close_button.setStyleSheet('QPushButton {background:%s; color:%s; border:0; '
                                        'border-radius:16px; font-size:24px;} QPushButton:hover {background:%s;}'
                                        % (color('preview_close_bg'), color('preview_white'),
                                           color('preview_close_hover')))
        self.disclaimer_bar.setStyleSheet('QWidget#previewDisclaimerBar {background:%s;}'
                                         % color('preview_overlay_disclaimer'))
        self.disclaimer_icon.setStyleSheet('color:%s; background:transparent;' % color('preview_muted'))
        self.disclaimer_label.setStyleSheet('color:%s; background:transparent;' % color('preview_disclaimer_text'))
        self.canvas.setStyleSheet('QWidget#previewCanvas {background:%s;}' % color('preview_canvas'))
        self.scroll.setStyleSheet('QScrollArea {background:%s; border:0;}' % color('preview_canvas'))
        self.scroll.viewport().setStyleSheet('QWidget#previewShellViewport {background:%s;}'
                                             % color('preview_canvas'))
        self.shell_host.setStyleSheet('QWidget#previewShellHost {background:%s;}'
                                      % color('preview_canvas'))
        self.selection_panel.refresh_colors()
        if self.shell is not None:
            for widget in [self.shell, *self.shell.findChildren(QWidget)]:
                rules = getattr(widget, '_preview_style_rules', None)
                if rules is not None:
                    widget.setStyleSheet(rules())
                widget.update()

    def refresh_interface_scale(self):
        self.overlay_header.setFixedHeight(px(48))
        self.disclaimer_bar.setFixedHeight(px(28))
        self.platform_button.setFixedHeight(px(32))
        self.close_button.setFixedSize(px(32), px(32))
        self.header_separator.setFixedSize(px(1), px(20))
        self._fit_columns()
        self.updateGeometry()
