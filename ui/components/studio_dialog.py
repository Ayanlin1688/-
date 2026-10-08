"""统一弹窗外壳：12px 自绘圆角 + 自绘标题头（标题 + 关闭），全应用共用。

QFramelessWidgets 自带的对话框标题栏没有标题文字且在 Windows 10 上是直角，
这里统一接管：隐藏系统标题栏、绘制圆角窗口、提供可拖拽的标题行。
"""

import math

from PyQt5 import sip
from PyQt5.QtCore import Qt, QRectF, QObject
from PyQt5.QtGui import QColor, QLinearGradient, QPainter, QPainterPath
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    CaptionLabel, FluentIcon as FIF, StrongBodyLabel, TransparentToolButton,
)
from core.i18n import tr

WINDOW_RADIUS = 12
WINDOW_MARGIN = 8


def is_dialog_alive(dialog):
    return dialog is not None and (not isinstance(dialog, QObject) or not sip.isdeleted(dialog))


def safe_delete_dialog(dialog):
    """由所有者安排一次销毁，兼容已失效或已安排销毁的弹窗。"""
    try:
        if not is_dialog_alive(dialog) or getattr(dialog, '_studio_delete_pending', False) is True:
            return False
        dialog.deleteLater()
        dialog._studio_delete_pending = True
        return True
    except RuntimeError:
        return False


def dialog_label(text='', size=13, color='#F4F5F7', bold=False):
    """轻量标签（弹窗内使用，避免与 workspace_surface 形成循环依赖）。"""
    from ..materials import map_text_color, register_label
    widget = StrongBodyLabel(text) if bold else CaptionLabel(text)
    font = widget.font()
    font.setPixelSize(size)
    font.setBold(bold)
    widget.setFont(font)
    display = map_text_color(color)
    name = display.name() if isinstance(display, QColor) else str(display)
    widget.setTextColor(QColor(name), QColor(name))
    widget.setStyleSheet(f'background:transparent; color:{name};')
    register_label(widget, color)
    return widget


class DialogHeader(QWidget):
    """弹窗标题行：标题 + 关闭按钮，整行可拖拽窗口。"""

    def __init__(self, dialog, title='', parent=None):
        super().__init__(dialog if parent is None else parent)
        self.dialog = dialog
        self._drag_offset = None
        self.setFixedHeight(42)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 8, 0)
        layout.setSpacing(10)
        self.title_label = dialog_label(title, 15, '#F4F5F7', True)
        layout.addWidget(self.title_label)
        layout.addStretch(1)
        self.close_button = TransparentToolButton(FIF.CLOSE)
        self.close_button.setFixedSize(32, 32)
        self.close_button.setToolTip(tr('关闭（Esc）'))
        self.close_button.clicked.connect(self.dialog.reject)
        layout.addWidget(self.close_button)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPos() - self.dialog.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.dialog.move(event.globalPos() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class StudioDialogBase:
    """Mixin：给 qframelesswindow.FramelessDialog 注入统一的圆角外壳与标题头。

    使用方式：
        from qframelesswindow import FramelessDialog
        class MyDialog(StudioDialogBase, FramelessDialog): ...
    构造完成后调用 self._build_studio_shell(title)。
    """

    def _build_studio_shell(self, title=''):
        from PyQt5.QtWidgets import QDialog
        if not isinstance(self, QDialog):
            return
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle(title)
        self._window_margin = WINDOW_MARGIN
        self._window_radius = WINDOW_RADIUS
        stock = getattr(self, 'titleBar', None)
        if stock is not None:
            stock.hide()
            stock.setFixedHeight(0)
        shell = QVBoxLayout(self)
        shell.setContentsMargins(WINDOW_MARGIN, WINDOW_MARGIN, WINDOW_MARGIN, WINDOW_MARGIN)
        shell.setSpacing(0)
        self.header = DialogHeader(self, title)
        shell.addWidget(self.header)
        self.body = QWidget()
        self.body.setObjectName('studioDialogBody')
        shell.addWidget(self.body, 1)
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(16, 2, 16, 14)
        self.body_layout.setSpacing(14)

    def set_title(self, text):
        self.header.title_label.setText(text)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(WINDOW_MARGIN, WINDOW_MARGIN, -WINDOW_MARGIN, -WINDOW_MARGIN)
        path = QPainterPath()
        path.addRoundedRect(rect, WINDOW_RADIUS, WINDOW_RADIUS)
        painter.setClipPath(path)
        from ..materials import frame_line_color, palette
        th = palette()
        # 弹窗底色取主题基底并微微提亮，顶部叠一层主题光晕。
        base = QColor(th['bg1']).lighter(104 if th['light'] else 118)
        painter.fillRect(rect, base)
        wash = QLinearGradient(rect.topLeft(), rect.bottomRight())
        ar, ag, ab = th['accent_rgb']
        br, bv, bb = th['accent2_rgb']
        wash.setColorAt(0, QColor(ar, ag, ab, 26 if th['light'] else 16))
        wash.setColorAt(1, QColor(br, bv, bb, 0))
        painter.fillRect(rect, wash)
        painter.setClipping(False)
        painter.setPen(frame_line_color())
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), WINDOW_RADIUS, WINDOW_RADIUS)


def make_studio_dialog_class():
    """返回可实例化的统一弹窗基类（组合 qframelesswindow.FramelessDialog）。"""
    from qframelesswindow import FramelessDialog

    class StudioDialog(StudioDialogBase, FramelessDialog):
        def __init__(self, parent=None, title=''):
            super().__init__(parent)
            self._build_studio_shell(title)

    return StudioDialog


StudioDialog = make_studio_dialog_class()
