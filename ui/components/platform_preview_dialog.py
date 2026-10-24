"""平台预览通用模板对话框。

参考软件有 25 套平台定制页面；本仓库按段 2 约定只做两套通用模板：
    海外平台 → 手机壳（9:41 状态栏 + 品牌顶栏 + 图区轮播 + 详情堆叠 + 底部操作条）
    国内平台 → 桌面壳（平台名顶栏 + 搜索框 + 主图 + 详情堆叠）
主题色取平台记录里的 primaryColor / accentColor；多平台时顶部下拉切换。
底部免责文案为产品合规文案，一字不改。
"""
from pathlib import Path

from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QRegion
from PyQt5.QtWidgets import (
    QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)
from qfluentwidgets import ComboBox, LineEdit, PushButton

from core.i18n import tr
from .. import materials
from ..theme import style_controls
from ..tokens import px
from .studio_dialog import StudioDialog

_PREVIEW_CACHE = {}
_PREVIEW_CACHE_LIMIT = 32


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
    font.setPixelSize(px(size))
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
              '与相关平台无隶属关系，仅用于图片效果自查')


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

    def __init__(self, path='', height=220, parent=None, *, edge=0):
        super().__init__(parent)
        self.image_path = str(path) if path else ''
        self.has_image = False
        self._edge = int(edge)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setFixedHeight(px(int(height)))
        self.setMinimumWidth(px(80))
        self.set_image(self.image_path)

    def set_image(self, path):
        self.image_path = str(path) if path else ''
        self._image = _load_image(self.image_path)
        self.has_image = not self._image.isNull()
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
        finally:
            painter.end()


class PreviewPlaceholder(QLabel):
    """无生成图时的占位。"""

    def __init__(self, height=220, parent=None, text=None):
        super().__init__(parent)
        self.setTextFormat(Qt.PlainText)
        self.setAlignment(Qt.AlignCenter)
        self.setWordWrap(True)
        self.setFixedHeight(px(int(height)))
        self.setText(text if text is not None else tr('暂无预览图片'))
        self.setStyleSheet('color:%s; background:%s; border-radius:%dpx;'
                           % (materials.ecommerce_color('preview_muted'),
                              materials.ecommerce_color('preview_input'), px(8)))


class _CarouselDots(QWidget):
    def __init__(self, count, parent=None, color=None):
        super().__init__(parent)
        self.count = max(0, int(count))
        self.index = 0
        self.color = QColor(color or materials.palette()['accent'])
        self.setFixedHeight(px(14))

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
        self.setFixedSize(px(64), px(16))

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
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


class _StatusBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(px(26))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(px(14), 0, px(14), 0)
        layout.setSpacing(px(6))
        self.time_label = _label(11, bold=True, text='9:41')
        self.time_label.setStyleSheet(
            'color:%s; background:transparent;' % materials.ecommerce_color('preview_text'))
        self.icons = _SignalIcons(materials.ecommerce_color('preview_text'), self)
        layout.addWidget(self.time_label)
        layout.addStretch(1)
        layout.addWidget(self.icons)


# ---------------------------------------------------------------------------
# 两套外壳

class _PhoneShell(QWidget):
    """海外平台：375×812 手机壳的通用版。"""

    kind = 'mobile'

    def __init__(self, platform, product_name, carousel, detail, parent=None):
        super().__init__(parent)
        self.platform = dict(platform or {})
        self.product_name = product_name
        self.carousel_paths = list(carousel)
        self.detail_paths = list(detail)
        self.primary = _platform_color(self.platform, 'primaryColor')
        self.accent = _platform_color(self.platform, 'accentColor')
        self.carousel_index = 0
        self.setFixedWidth(px(360))
        self.setFixedHeight(px(660))
        self.setObjectName('previewPhoneShell')
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet('#previewPhoneShell {background:%s; border-radius:%dpx;}'
                           % (materials.ecommerce_color('preview_card'), px(20)))
        root = QVBoxLayout(self)
        root.setContentsMargins(px(1), px(1), px(1), px(1))
        root.setSpacing(0)

        self.status_bar = _StatusBar(self)
        root.addWidget(self.status_bar)

        self.nav_bar = QWidget(self)
        self.nav_bar.setObjectName('previewPhoneNav')
        self.nav_bar.setFixedHeight(px(44))
        self.nav_bar.setStyleSheet('#previewPhoneNav {background:%s;}'
                                   % self.primary.name())
        nav = QHBoxLayout(self.nav_bar)
        nav.setContentsMargins(px(12), 0, px(12), 0)
        nav.setSpacing(px(8))
        self.back_label = _label(16, bold=True, text='\u2039')
        self.name_label = _label(15, bold=True, text=_platform_name(self.platform))
        for label in (self.back_label, self.name_label):
            label.setStyleSheet(
                'color:%s; background:transparent;'
                % materials.ecommerce_color('preview_white'))
        self.cart_label = _label(13, text='\u22ef')
        self.cart_label.setStyleSheet(
            'color:%s; background:transparent;'
            % materials.ecommerce_color('preview_white'))
        nav.addWidget(self.back_label)
        nav.addWidget(self.name_label, 1)
        nav.addWidget(self.cart_label)
        root.addWidget(self.nav_bar)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setStyleSheet('QScrollArea{background:transparent; border:0;}')
        scroll.viewport().setStyleSheet('background:transparent;')
        self.scroll = scroll
        content = QWidget()
        content.setObjectName('previewPhoneBody')
        content.setStyleSheet('#previewPhoneBody {background:%s;}'
                              % materials.ecommerce_color('preview_card'))
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self.image_area = QWidget(content)
        image_layout = QVBoxLayout(self.image_area)
        image_layout.setContentsMargins(0, 0, 0, 0)
        image_layout.setSpacing(0)
        if self.carousel_paths:
            self.main_image = PreviewImage(self.carousel_paths[0], height=300, edge=0)
            image_layout.addWidget(self.main_image)
        else:
            self.main_image = None
            self.placeholder = PreviewPlaceholder(height=300,
                                                  text=tr('暂无预览图片'))
            image_layout.addWidget(self.placeholder)
        self.dots = _CarouselDots(len(self.carousel_paths), self, self.accent)
        image_layout.addWidget(self.dots)
        meta = QWidget(content)
        meta_layout = QHBoxLayout(meta)
        meta_layout.setContentsMargins(px(14), px(4), px(14), 0)
        self.counter_label = _label(11, text=self._counter_text())
        self.counter_label.setStyleSheet(
            'color:%s; background:transparent;'
            % materials.ecommerce_color('preview_muted'))
        meta_layout.addWidget(self.counter_label)
        meta_layout.addStretch(1)
        image_layout.addWidget(meta)
        body.addWidget(self.image_area)

        info = QWidget(content)
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(px(14), px(8), px(14), px(12))
        info_layout.setSpacing(px(6))
        self.product_label = _label(15, bold=True, wrap=True, text=self.product_name)
        self.product_label.setStyleSheet(
            'color:%s; background:transparent;'
            % materials.ecommerce_color('preview_text'))
        self.price_label = _label(18, bold=True, text=self._price_text())
        self.price_label.setStyleSheet('color:%s; background:transparent;'
                                       % self.accent.name())
        self.ship_label = _label(11, text=tr('包邮 · 7 天无理由退换'))
        self.ship_label.setStyleSheet(
            'color:%s; background:transparent;'
            % materials.ecommerce_color('preview_muted'))
        info_layout.addWidget(self.product_label)
        info_layout.addWidget(self.price_label)
        info_layout.addWidget(self.ship_label)
        body.addWidget(info)

        detail_title = _label(13, bold=True, text=tr('商品详情'))
        detail_title.setStyleSheet(
            'color:%s; background:%s; padding:%dpx %dpx;'
            % (materials.ecommerce_color('preview_text'),
               materials.ecommerce_color('preview_input'), px(8), px(14)))
        body.addWidget(detail_title)
        self.detail_images = []
        if self.detail_paths:
            for path in self.detail_paths:
                image = PreviewImage(path, height=240, edge=0)
                self.detail_images.append(image)
                body.addWidget(image)
        else:
            self.detail_placeholder = PreviewPlaceholder(height=160,
                                                         text=tr('暂无预览图片'))
            self.detail_images = [self.detail_placeholder]
            body.addWidget(self.detail_placeholder)
        body.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        self.action_bar = QWidget(self)
        self.action_bar.setObjectName('previewPhoneActions')
        self.action_bar.setFixedHeight(px(52))
        self.action_bar.setStyleSheet(
            '#previewPhoneActions {background:%s; border-top:1px solid %s;}'
            % (materials.ecommerce_color('preview_card'),
               materials.ecommerce_color('preview_line')))
        actions = QHBoxLayout(self.action_bar)
        actions.setContentsMargins(px(12), px(8), px(12), px(8))
        actions.setSpacing(px(8))
        self.cart_button = PushButton(tr('加入购物车'), self.action_bar)
        self.buy_button = PushButton(tr('立即购买'), self.action_bar)
        self.cart_button.setStyleSheet(
            'QPushButton {border:1px solid %s; border-radius:%dpx; background:transparent;'
            ' color:%s; font-size:%dpx; font-weight:600;}'
            % (self.primary.name(), px(6), self.primary.name(), px(13)))
        self.buy_button.setStyleSheet(
            'QPushButton {border:0; border-radius:%dpx; background:%s; color:%s;'
            ' font-size:%dpx; font-weight:600;}'
            % (px(6), self.accent.name(), materials.ecommerce_color('preview_white'),
               px(13)))
        actions.addWidget(self.cart_button, 1)
        actions.addWidget(self.buy_button, 1)
        root.addWidget(self.action_bar)
        _round_mask(self, px(20))

    def _counter_text(self):
        return '%d/%d' % (self.carousel_index + 1, max(1, len(self.carousel_paths)))

    def _price_text(self):
        return '¥ 199.00'

    def set_carousel_index(self, index):
        if not self.carousel_paths:
            return
        self.carousel_index = max(0, min(index, len(self.carousel_paths) - 1))
        if self.main_image is not None:
            self.main_image.set_image(self.carousel_paths[self.carousel_index])
        self.dots.set_index(self.carousel_index)
        self.counter_label.setText(self._counter_text())

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            path = QPainterPath()
            path.addRoundedRect(rect, px(20), px(20))
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(materials.ecommerce_color('preview_line')), 1))
            painter.drawRoundedRect(rect, px(20), px(20))
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
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setAttribute(Qt.WA_StyledBackground, True)
        root = QVBoxLayout(self)
        root.setContentsMargins(px(1), px(1), px(1), px(1))
        root.setSpacing(0)

        self.nav_bar = QWidget(self)
        self.nav_bar.setObjectName('previewDesktopNav')
        self.nav_bar.setFixedHeight(px(56))
        self.nav_bar.setStyleSheet(
            '#previewDesktopNav {background:%s; border-bottom:2px solid %s;}'
            % (materials.ecommerce_color('preview_card'), self.primary.name()))
        nav = QHBoxLayout(self.nav_bar)
        nav.setContentsMargins(px(18), 0, px(18), 0)
        nav.setSpacing(px(14))
        self.name_label = _label(19, bold=True, text=_platform_name(self.platform))
        self.name_label.setStyleSheet('color:%s; background:transparent;'
                                      % self.primary.name())
        self.search_edit = LineEdit(self.nav_bar)
        self.search_edit.setPlaceholderText(self.product_name)
        self.search_edit.setReadOnly(True)
        self.search_edit.setMinimumHeight(px(32))
        self.search_edit.setStyleSheet(
            'LineEdit {border:2px solid %s; border-radius:%dpx; background:%s;'
            ' color:%s; padding:0 %dpx; font-size:%dpx;}'
            % (self.primary.name(), px(8), materials.ecommerce_color('preview_input'),
               materials.ecommerce_color('preview_text'), px(10), px(12)))
        self.search_button = PushButton(tr('搜索'), self.nav_bar)
        self.search_button.setMinimumHeight(px(32))
        self.search_button.setStyleSheet(
            'QPushButton {border:0; border-radius:%dpx; background:%s; color:%s;'
            ' padding:0 %dpx; font-size:%dpx; font-weight:600;}'
            % (px(8), self.primary.name(), materials.ecommerce_color('preview_white'),
               px(18), px(13)))
        nav.addWidget(self.name_label)
        nav.addWidget(self.search_edit, 1)
        nav.addWidget(self.search_button)
        root.addWidget(self.nav_bar)

        main = QWidget(self)
        main.setObjectName('previewDesktopMain')
        main.setStyleSheet('#previewDesktopMain {background:%s;}'
                           % materials.ecommerce_color('preview_card'))
        main_layout = QHBoxLayout(main)
        main_layout.setContentsMargins(px(18), px(18), px(18), px(18))
        main_layout.setSpacing(px(18))
        gallery = QVBoxLayout()
        gallery.setContentsMargins(0, 0, 0, 0)
        gallery.setSpacing(px(6))
        if self.carousel_paths:
            self.main_image = PreviewImage(self.carousel_paths[0], height=320, edge=6)
            gallery.addWidget(self.main_image)
        else:
            self.main_image = None
            self.placeholder = PreviewPlaceholder(height=320, text=tr('暂无预览图片'))
            gallery.addWidget(self.placeholder)
        self.small_images = []
        for path in self.carousel_paths[:4]:
            small = PreviewImage(path, height=64, edge=4)
            small.setFixedWidth(px(64))
            self.small_images.append(small)
        thumbs = QHBoxLayout()
        thumbs.setContentsMargins(0, 0, 0, 0)
        thumbs.setSpacing(px(6))
        for small in self.small_images:
            thumbs.addWidget(small)
        thumbs.addStretch(1)
        gallery.addLayout(thumbs)
        main_layout.addLayout(gallery, 3)
        info = QVBoxLayout()
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(px(8))
        self.product_label = _label(17, bold=True, wrap=True, text=self.product_name)
        self.product_label.setStyleSheet(
            'color:%s; background:transparent;'
            % materials.ecommerce_color('preview_text'))
        self.price_label = _label(26, bold=True, text='¥ 199.00')
        self.price_label.setStyleSheet('color:%s; background:transparent;'
                                       % self.accent.name())
        self.meta_label = _label(11, text=tr('已售 1000+ · 好评率 98%'))
        self.meta_label.setStyleSheet(
            'color:%s; background:transparent;'
            % materials.ecommerce_color('preview_muted'))
        info.addWidget(self.product_label)
        info.addWidget(self.price_label)
        info.addWidget(self.meta_label)
        for text, filled in ((tr('加入购物车'), False), (tr('立即购买'), True)):
            button = PushButton(text, main)
            button.setMinimumHeight(px(34))
            if filled:
                button.setStyleSheet(
                    'QPushButton {border:0; border-radius:%dpx; background:%s; color:%s;'
                    ' font-size:%dpx; font-weight:600;}'
                    % (px(6), self.accent.name(),
                       materials.ecommerce_color('preview_white'), px(13)))
            else:
                button.setStyleSheet(
                    'QPushButton {border:2px solid %s; border-radius:%dpx;'
                    ' background:transparent; color:%s; font-size:%dpx; font-weight:600;}'
                    % (self.primary.name(), px(6), self.primary.name(), px(13)))
            info.addWidget(button)
        info.addStretch(1)
        main_layout.addLayout(info, 2)
        root.addWidget(main)

        self.detail_title = _label(14, bold=True, text=tr('商品详情'))
        self.detail_title.setStyleSheet(
            'color:%s; background:%s; padding:%dpx %dpx; border-top:1px solid %s;'
            % (materials.ecommerce_color('preview_text'),
               materials.ecommerce_color('preview_page'), px(10), px(18),
               materials.ecommerce_color('preview_line')))
        root.addWidget(self.detail_title)
        self.detail_images = []
        if self.detail_paths:
            for path in self.detail_paths:
                image = PreviewImage(path, height=300, edge=0)
                self.detail_images.append(image)
                root.addWidget(image)
        else:
            self.detail_placeholder = PreviewPlaceholder(height=180,
                                                         text=tr('暂无预览图片'))
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
        _round_mask(self, px(12))
        self.update()


# ---------------------------------------------------------------------------
# 对话框

class PlatformPreviewDialog(StudioDialog):
    """平台效果预览：顶部下拉切换平台，正文按国内/海外切换桌面壳/手机壳。"""

    def __init__(self, parent, group, stage_text=None):
        super().__init__(parent, tr('平台预览'))
        self.group = dict(group or {}) if isinstance(group, dict) else {}
        self.stage_text = stage_text if callable(stage_text) else None
        self.platforms = [platform for platform in (self.group.get('platforms') or ())
                          if isinstance(platform, dict)]
        self.shell = None
        self.shell_kind = ''
        self.carousel, self.detail = _group_images(self.group)
        self.resize(px(760), px(720))
        self.setMinimumSize(px(560), px(520))

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(px(10))
        self.platform_label = _label(12, text=tr('电商平台'))
        self.platform_label.setStyleSheet('color:%s; background:transparent;'
                                          % materials.palette()['text2'])
        self.platform_combo = ComboBox(self)
        self.platform_combo.setMinimumWidth(px(180))
        for platform in self.platforms:
            self.platform_combo.addItem(_platform_name(platform) or tr('未选平台'))
        if not self.platforms:
            self.platform_combo.addItem(tr('未选平台'))
        self.product_label = _label(13, bold=True, text=_product_name(self.group))
        self.product_label.setStyleSheet('color:%s; background:transparent;'
                                         % materials.palette()['text1'])
        header.addWidget(self.platform_label)
        header.addWidget(self.platform_combo)
        header.addWidget(self.product_label, 1)
        self.body_layout.addLayout(header)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setStyleSheet('QScrollArea{background:transparent; border:0;}')
        self.scroll.viewport().setStyleSheet(
            'background:%s; border-radius:%dpx;'
            % (materials.ecommerce_color('preview_page'), px(10)))
        self.shell_host = QWidget()
        self.shell_host.setObjectName('previewShellHost')
        self.shell_host.setStyleSheet('#previewShellHost {background:transparent;}')
        self.shell_layout = QVBoxLayout(self.shell_host)
        self.shell_layout.setContentsMargins(px(12), px(12), px(12), px(12))
        self.shell_layout.setSpacing(px(12))
        self.scroll.setWidget(self.shell_host)
        self.body_layout.addWidget(self.scroll, 1)

        self.disclaimer_label = QLabel(_disclaimer_text(), self)
        self.disclaimer_label.setTextFormat(Qt.PlainText)
        self.disclaimer_label.setWordWrap(True)
        self.disclaimer_label.setAlignment(Qt.AlignCenter)
        font = self.disclaimer_label.font()
        font.setPixelSize(px(11))
        self.disclaimer_label.setFont(font)
        self.disclaimer_label.setStyleSheet(
            'color:%s; background:transparent;' % materials.palette()['text3'])
        self.body_layout.addWidget(self.disclaimer_label)

        style_controls(self)
        self._apply_header_styles()
        self.platform_combo.currentIndexChanged.connect(self._platform_changed)
        self._build_shell()

    # -- 平台 / 图片 ---------------------------------------------------
    @property
    def current_index(self):
        return max(0, int(self.platform_combo.currentIndex()))

    @property
    def current_platform(self):
        index = self.current_index
        if 0 <= index < len(self.platforms):
            return self.platforms[index]
        return {}

    @property
    def primary_color(self):
        return _platform_color(self.current_platform, 'primaryColor')

    @property
    def accent_color(self):
        return _platform_color(self.current_platform, 'accentColor')

    @property
    def is_mobile(self):
        return self.shell_kind == 'mobile'

    def _apply_header_styles(self):
        colors = materials.palette()
        self.platform_label.setStyleSheet('color:%s; background:transparent;'
                                          % colors['text2'])
        self.product_label.setStyleSheet('color:%s; background:transparent;'
                                         % colors['text1'])
        self.disclaimer_label.setStyleSheet('color:%s; background:transparent;'
                                            % colors['text3'])

    def _clear_shell(self):
        if self.shell is not None:
            self.shell_layout.removeWidget(self.shell)
            self.shell.hide()
            self.shell.deleteLater()
            self.shell = None
        while self.shell_layout.count():
            item = self.shell_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.deleteLater()

    def _build_shell(self):
        self._clear_shell()
        platform = self.current_platform
        product = _product_name(self.group)
        if _platform_group(platform) == 'overseas':
            shell = _PhoneShell(platform, product, self.carousel, self.detail,
                                self.shell_host)
            self.shell_kind = 'mobile'
            self.shell_layout.addWidget(shell, 0, Qt.AlignHCenter)
        else:
            shell = _DesktopShell(platform, product, self.carousel, self.detail,
                                  self.shell_host)
            self.shell_kind = 'desktop'
            self.shell_layout.addWidget(shell, 0)
        self.shell_layout.addStretch(1)
        self.shell = shell
        self.scroll.verticalScrollBar().setValue(0)

    def _platform_changed(self, _index):
        self._build_shell()

    def set_platform_index(self, index):
        self.platform_combo.setCurrentIndex(int(index))

    # -- 受控刷新 -----------------------------------------------------
    def refresh_colors(self):
        self._apply_header_styles()
        if self.shell is not None:
            self.shell.update()

    def retranslate_ui(self):
        try:
            self.set_title(tr('平台预览'))
            self.platform_label.setText(tr('电商平台'))
            self.disclaimer_label.setText(_disclaimer_text())
            self._build_shell()
        except RuntimeError:
            return

    def refresh_interface_scale(self):
        try:
            self._build_shell()
        except RuntimeError:
            return
