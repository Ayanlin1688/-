"""全屏图片预览灯箱（参考软件同款）：深色遮罩、顶部标题、左右箭头、滚轮缩放与拖拽平移。

参考软件原版只有翻页，用户明确要求「必须支持滚轮放大看细节、拖拽平移」，因此
图片区使用 QGraphicsView：
- 滚轮以鼠标位置为锚点缩放（0.8×适配 ~ 10×适配，可双击/按钮复位）；
- 拖拽平移（ScrollHandDrag）；
- 键盘 ←/→ 切换图片、Esc 关闭、+/- 缩放、0 复位。
灯箱为无边框全屏对话框，遮罩 #000000d9、白色/深色圆角面板（主题自适应）。
"""
from pathlib import Path

from PyQt5.QtCore import Qt, QRectF, QSize, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QDialog, QFrame, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView,
    QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from core.i18n import tr
from .. import materials
from ..tokens import px


class _ZoomView(QGraphicsView):
    """图片视图：适配窗口 + 滚轮缩放 + 拖拽平移 + 键盘缩放。"""

    zoom_changed = pyqtSignal(float)
    navigation_requested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setCursor(Qt.OpenHandCursor)
        self._item = None
        self._fit_scale = 1.0
        self._path = ''

    # —— 图片 ——
    def current_path(self):
        return self._path

    def set_image(self, path):
        self._path = str(path or '')
        self.scene().clear()
        self._item = None
        pixmap = QPixmap()
        try:
            if self._path and Path(self._path).is_file():
                pixmap.loadFromData(Path(self._path).read_bytes())
        except (OSError, ValueError):
            pixmap = QPixmap()
        if pixmap.isNull():
            item = self.scene().addText(tr('无法预览该图片'))
            self._item = None
            font = item.font()
            font.setPixelSize(px(15))
            item.setFont(font)
            item.setDefaultTextColor(QColor(materials.palette()['text2']))
        else:
            self._item = QGraphicsPixmapItem(pixmap)
            self.scene().addItem(self._item)
        # QGraphicsScene's implicit scene rect only grows. Reset it when a
        # portrait follows a wider image so both side margins stay equal.
        self.scene().setSceneRect(self.scene().itemsBoundingRect())
        self._fit_image()

    def _fit_image(self):
        self.resetTransform()
        rect = self.scene().itemsBoundingRect()
        if rect.isNull() or rect.width() <= 0 or rect.height() <= 0:
            self._fit_scale = 1.0
            return
        margin = px(24)
        target = QRectF(rect)
        self.fitInView(target.adjusted(-margin, -margin, margin, margin),
                       Qt.KeepAspectRatio)
        self._fit_scale = max(1e-6, self.transform().m11())
        self.zoom_changed.emit(self.zoom_ratio())

    # —— 缩放 ——
    def zoom_ratio(self):
        return self.transform().m11() / self._fit_scale if self._fit_scale else 1.0

    def zoom_by(self, factor):
        if self._item is None:
            return
        current = self.transform().m11()
        target = current * factor
        lower = self._fit_scale * 0.8
        upper = max(self._fit_scale * 10.0, self._fit_scale)
        if target < lower:
            factor = lower / current
        elif target > upper:
            factor = upper / current
        if abs(factor - 1.0) < 1e-6:
            return
        self.scale(factor, factor)
        self.zoom_changed.emit(self.zoom_ratio())

    def reset_zoom(self):
        self._fit_image()

    def wheelEvent(self, event):
        if self._item is None:
            return super().wheelEvent(event)
        delta = event.angleDelta().y()
        if not delta:
            return super().wheelEvent(event)
        self.zoom_by(1.25 if delta > 0 else 0.8)
        event.accept()

    def mouseDoubleClickEvent(self, event):
        self.reset_zoom()
        event.accept()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Left, Qt.Key_Right):
            # QGraphicsView otherwise consumes arrows to scroll the zoomed image.
            self.navigation_requested.emit(-1 if event.key() == Qt.Key_Left else 1)
            event.accept()
            return
        if event.key() in (Qt.Key_Plus, Qt.Key_Equal):
            self.zoom_by(1.25)
            event.accept()
            return
        if event.key() == Qt.Key_Minus:
            self.zoom_by(0.8)
            event.accept()
            return
        if event.key() == Qt.Key_0:
            self.reset_zoom()
            event.accept()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._item is not None and self.zoom_ratio() <= 1.001:
            self._fit_image()


class _RoundButton(QWidget):
    """灯箱圆形按钮（红色关闭 / 深色左右箭头），自绘不受全局按钮样式影响。"""

    clicked = pyqtSignal()

    def __init__(self, glyph, *, size=40, background=None, hover=None,
                 foreground='#FFFFFF', font_size=20, parent=None):
        super().__init__(parent)
        self._glyph = str(glyph)
        self._size = int(size)
        self._background = background or materials.ECOMMERCE['nav_bg']
        self._hover_background = hover or materials.ECOMMERCE['nav_hover']
        self._foreground = foreground
        self._hover = False
        self._font_size = font_size
        self.setFixedSize(self._size, self._size)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)

    def keyPressEvent(self, event):
        if self.isEnabled() and event.key() in (Qt.Key_Space, Qt.Key_Return):
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def set_glyph(self, glyph):
        self._glyph = str(glyph)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setOpacity(1.0 if self.isEnabled() else 0.35)
        painter.setPen(Qt.NoPen)
        color = self._hover_background if self._hover else self._background
        painter.setBrush(color if isinstance(color, QColor) else QColor(*color))
        painter.drawEllipse(self.rect())
        font = painter.font()
        font.setPixelSize(self._font_size)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor(self._foreground)))
        painter.drawText(self.rect(), Qt.AlignCenter, self._glyph)
        painter.end()
        super().paintEvent(event)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.isEnabled() and event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class ImageLightbox(QDialog):
    """全屏图片灯箱。show_images() 是页面层入口。"""

    closed = pyqtSignal()

    def __init__(self, parent=None, paths=None, index=0):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self._paths = []
        self._index = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addStretch(1)
        self.panel = QWidget(self)
        self.panel.setObjectName('lightboxPanel')
        self.panel.setMinimumSize(QSize(px(420), px(320)))
        self.panel.setMaximumWidth(px(1200))
        self.panel.setSizePolicy(self.panel.sizePolicy().horizontalPolicy(),
                                 self.panel.sizePolicy().verticalPolicy())
        outer.addWidget(self.panel, 0, Qt.AlignCenter)
        outer.addStretch(1)

        panel_layout = QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(0)

        self.header = QWidget(self.panel)
        self.header.setObjectName('lightboxHeader')
        self.header.setFixedHeight(px(56))
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(px(20), 0, px(16), 0)
        header_layout.setSpacing(px(10))
        self.accent_square = QLabel(self.header)
        self.accent_square.setFixedSize(px(12), px(12))
        header_layout.addWidget(self.accent_square)
        title_box = QWidget(self.header)
        title_layout = QVBoxLayout(title_box)
        title_layout.setContentsMargins(0, px(2), 0, 0)
        title_layout.setSpacing(px(2))
        self.title_label = QLabel(tr('图片预览'), title_box)
        title_layout.addWidget(self.title_label)
        self.tab_underline = QLabel(title_box)
        self.tab_underline.setFixedHeight(px(2))
        title_layout.addWidget(self.tab_underline)
        header_layout.addWidget(title_box)
        header_layout.addStretch(1)
        self.hint_label = QLabel(tr('滚轮缩放 · 拖拽平移 · ←/→ 切换 · Esc 关闭'), self.header)
        header_layout.addWidget(self.hint_label)
        self.metadata_label = QLabel('', self.header)
        header_layout.addWidget(self.metadata_label)
        self.counter_label = QLabel('', self.header)
        header_layout.addWidget(self.counter_label)
        self.close_button = _RoundButton(
            '×', size=px(32), background=materials.ECOMMERCE['close_bg'],
            hover=QColor(materials.ECOMMERCE['close_hover']).getRgb()[:3],
            font_size=px(16), parent=self.header)
        self.close_button.setToolTip(tr('关闭（Esc）'))
        self.close_button.clicked.connect(self._close)
        header_layout.addWidget(self.close_button)
        panel_layout.addWidget(self.header)

        self.body = QWidget(self.panel)
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(px(72), px(12), px(72), px(12))
        body_layout.setSpacing(0)
        self.view = _ZoomView(self.body)
        self.view.setObjectName('lightboxView')
        self.view.navigation_requested.connect(
            lambda step: self.previous_image() if step < 0 else self.next_image())
        body_layout.addWidget(self.view, 1)
        panel_layout.addWidget(self.body, 1)
        self.prev_button = _RoundButton('‹', size=px(40),
                                        background=materials.ECOMMERCE['nav_bg'],
                                        hover=materials.ECOMMERCE['nav_hover'],
                                        parent=self.body)
        self.next_button = _RoundButton('›', size=px(40),
                                        background=materials.ECOMMERCE['nav_bg'],
                                        hover=materials.ECOMMERCE['nav_hover'],
                                        parent=self.body)
        self.prev_button.setToolTip(tr('上一张（←）'))
        self.next_button.setToolTip(tr('下一张（→）'))
        self.prev_button.clicked.connect(self.previous_image)
        self.next_button.clicked.connect(self.next_image)
        self._apply_theme()
        for button in (self.close_button, self.prev_button, self.next_button):
            button.raise_()
        try:
            from core.i18n import register_retranslate
            register_retranslate(self)
        except Exception:
            pass
        if paths:
            self.set_images(paths, index)

    def retranslate_ui(self):
        """语言切换：标题/提示/按钮 tooltip 重翻（幂等）。"""
        self.title_label.setText(tr('图片预览'))
        self.hint_label.setText(tr('滚轮缩放 · 拖拽平移 · ←/→ 切换 · Esc 关闭'))
        self.close_button.setToolTip(tr('关闭（Esc）'))
        self.prev_button.setToolTip(tr('上一张（←）'))
        self.next_button.setToolTip(tr('下一张（→）'))
        self.counter_label.setToolTip(tr('当前第 {index} 张').format(
            index=self._index + 1))
        self._show_current()

    # —— 主题 ——
    def _apply_theme(self):
        colors = materials.palette()
        light = bool(colors.get('light'))
        header_bg = (materials.ECOMMERCE['lightbox_light_header'] if light
                     else materials.ECOMMERCE['lightbox_dark_header'])
        body_bg = (materials.ECOMMERCE['lightbox_light_body'] if light
                   else materials.ECOMMERCE['lightbox_dark_body'])
        text1 = colors['text1']
        text2 = colors['text2']
        accent = colors['accent']
        self.panel.setStyleSheet(
            'QWidget#lightboxPanel {background: %s; border-radius: %dpx;}'
            % (body_bg, px(16)))
        self.header.setStyleSheet(
            'QWidget#lightboxHeader {background: %s; border-top-left-radius: %dpx;'
            ' border-top-right-radius: %dpx; border-bottom: 1px solid %s;}'
            % (header_bg, px(16), px(16), materials.rgba(colors['border'])))
        self.accent_square.setStyleSheet(
            'background: %s; border-radius: %dpx;' % (accent, px(3)))
        self.title_label.setStyleSheet(
            'color: %s; background: transparent; font-size: %dpx; font-weight: 600;'
            % (text1, px(14)))
        self.tab_underline.setStyleSheet('background: %s; border-radius: 1px;' % accent)
        self.hint_label.setStyleSheet(
            'color: %s; background: transparent; font-size: %dpx;' % (text2, px(11)))
        self.metadata_label.setStyleSheet(
            'color:%s;background:transparent;font-size:%dpx;' % (text2, px(12)))
        self.counter_label.setStyleSheet(
            'color: %s; background: transparent; font-size: %dpx;' % (text2, px(12)))
        self.view.setStyleSheet(
            'QGraphicsView#lightboxView {background: %s; border: 0;}'
            'QScrollBar:vertical, QScrollBar:horizontal {width: 0px; height: 0px;}'
            % body_bg)

    # —— 数据 ——
    def set_images(self, paths, index=0):
        self._paths = [str(path) for path in (paths or [])]
        self._index = max(0, min(int(index or 0), len(self._paths) - 1)) if self._paths else 0
        self._show_current()

    def images(self):
        return list(self._paths)

    def current_index(self):
        return self._index

    def current_path(self):
        if 0 <= self._index < len(self._paths):
            return self._paths[self._index]
        return ''

    def _show_current(self):
        self.counter_label.setText(
            '%d / %d' % (self._index + 1, len(self._paths)) if self._paths else '0 / 0')
        if not self._paths:
            self.view.set_image('')
            self.prev_button.setEnabled(False)
            self.next_button.setEnabled(False)
            return
        self.view.set_image(self._paths[self._index])
        pixmap = self.view._item.pixmap() if self.view._item is not None else QPixmap()
        try:
            size = Path(self.current_path()).stat().st_size
            size_text = ('%.1f MB' % (size / 1048576) if size >= 1048576
                         else '%.1f KB' % (size / 1024))
        except (OSError, ValueError):
            size_text = '—'
        resolution = '%d × %d' % (pixmap.width(), pixmap.height()) if not pixmap.isNull() else '—'
        self.metadata_label.setText(resolution + ' · ' + size_text)
        multiple = len(self._paths) > 1
        self.prev_button.show()
        self.next_button.show()
        self.prev_button.setEnabled(self._index > 0)
        self.next_button.setEnabled(self._index < len(self._paths) - 1)
        self._place_navs()

    def next_image(self):
        if self._index < len(self._paths) - 1:
            self._index += 1
            self._show_current()

    def previous_image(self):
        if self._index > 0:
            self._index -= 1
            self._show_current()

    # —— 布局 ——
    def _place_navs(self):
        bottom = px(40)
        self.prev_button.move(px(24), max(0, (self.body.height() - bottom) // 2))
        self.next_button.move(max(0, self.body.width() - bottom - px(24)),
                              max(0, (self.body.height() - bottom) // 2))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'panel'):
            self.panel.setFixedSize(max(1, self.width() - px(32)), max(1, self.height() - px(32)))
            self.hint_label.setVisible(self.width() >= px(1000))
            self.panel.layout().activate()
        self._place_navs()

    def refresh_colors(self):
        self._apply_theme()

    def refresh_interface_scale(self):
        self.header.setFixedHeight(px(56))
        for button, size in ((self.prev_button, 40), (self.next_button, 40), (self.close_button, 32)):
            button.setFixedSize(px(size), px(size))
        self.body.layout().setContentsMargins(px(72), px(12), px(72), px(12))
        self._apply_theme()
        self._place_navs()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            self._close()
            event.accept()
            return
        if key == Qt.Key_Left:
            self.previous_image()
            event.accept()
            return
        if key == Qt.Key_Right:
            self.next_image()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        # 点击遮罩（面板外）关闭。
        if event.button() == Qt.LeftButton and not self.panel.geometry().contains(event.pos()):
            self._close()
            event.accept()
            return
        super().mousePressEvent(event)

    def _close(self):
        self.closed.emit()
        self.reject()

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)

    # —— 入口 ——
    @classmethod
    def show_images(cls, parent, paths, index=0):
        paths = [str(path) for path in (paths or []) if str(path or '').strip()]
        if not paths:
            return None
        dialog = cls(parent)
        dialog.set_images(paths, index)
        geometry = parent.window().frameGeometry() if parent is not None else None
        if geometry is None:
            app = QApplication.instance()
            geometry = app.primaryScreen().availableGeometry() if app else None
        if geometry is not None:
            dialog.setGeometry(geometry)
            dialog.panel.setMinimumSize(0, 0)
            dialog.panel.setMaximumSize(16777215, 16777215)
            dialog.panel.setFixedSize(max(1, geometry.width() - px(32)),
                                      max(1, geometry.height() - px(32)))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog
