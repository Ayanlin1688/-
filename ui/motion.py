"""Bounded visual animation; task execution and configuration never enter here."""
import math
import time
import weakref
from types import MethodType

from PyQt5 import sip
from PyQt5.QtCore import Qt, QObject, QEvent, QRect, QRectF, QPoint, QTimer, QPropertyAnimation, QEasingCurve, pyqtProperty
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QLinearGradient, QPixmap
from PyQt5.QtWidgets import QWidget, QApplication
from qfluentwidgets import BodyLabel

from .materials import SurfaceShadow, background_brush, paint_background
from .tokens import MOTION

# 无障碍「减弱动效」：由配置 appearance.reduce_motion 驱动的全局开关。
_reduced_motion = False
_clocks = weakref.WeakSet()


def set_reduced_motion(enabled: bool) -> None:
    """切换减弱动效：停止呼吸点 / 流光等循环动画，过渡即时完成；
    开启 / 关闭后唤醒所有视觉时钟，让可见动画立即进入正确状态。"""
    global _reduced_motion
    _reduced_motion = bool(enabled)
    for clock in list(_clocks):
        try:
            clock.wake()
        except (RuntimeError, ReferenceError):
            _clocks.discard(clock)


def reduced_motion() -> bool:
    return _reduced_motion


class HoverWash(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.amount = 0
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.NoFocus)
        self.show()

    def paintEvent(self, event):
        painter = QPainter(self); painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen); painter.setBrush(QColor(255, 255, 255, round(18*self.amount)))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 8, 8)


class WidgetMotion(QObject):
    """Animate from the layout's last geometry, never from last frame's geometry."""
    def __init__(self, widget, card=False, primary=False):
        super().__init__(widget)
        self.widget = widget; self.card = card
        self._value = 0.; self._press = 0.; self._internal = False
        self._base = QRect(widget.geometry())
        self.hovered = False
        self.animation = QPropertyAnimation(self, b'amount', self)
        self.press_animation = QPropertyAnimation(self, b'press', self)
        for animation in (self.animation, self.press_animation):
            animation.setDuration(MOTION['fast']); animation.setEasingCurve(QEasingCurve.OutCubic)
        self.wash = None if card else HoverWash(widget)
        if not card:
            original_hit = widget.hitButton
            def stable_hit(button, point):
                # Qt retains its normal mouse grab and click emission. Keep the
                # resting hit area alive while the painted button shrinks so a
                # stationary press near an edge still releases as one click.
                return original_hit(point) or self._base.contains(button.mapToParent(point))
            widget.hitButton = MethodType(stable_hit, widget)
        # Nested effects trigger Qt backing-store recursion; cards only have
        # an effect when no parent already supplies a card shadow.
        ancestor = widget.parentWidget()
        while ancestor and ancestor.graphicsEffect() is None:
            ancestor = ancestor.parentWidget()
        self.shadow = SurfaceShadow(widget, primary) if (card or primary) and ancestor is None else None
        if self.shadow:
            widget.setGraphicsEffect(self.shadow)
        widget.installEventFilter(self)

    @pyqtProperty(float)
    def amount(self):
        return self._value

    @amount.setter
    def amount(self, value):
        self._value = value
        if self.shadow:
            try:
                self.shadow.hover = value; self.shadow.update()
            except RuntimeError:
                self.shadow = None  # 宿主已析构：丢弃悬空 effect 引用
        if self.wash:
            self.wash.amount = value; self.wash.update()
        self._apply()
        if self.card:
            self.widget.update()

    @pyqtProperty(float)
    def press(self):
        return self._press

    @press.setter
    def press(self, value):
        self._press = value; self._apply()

    def _animate(self, animation, value):
        animation.stop()
        if reduced_motion():
            # 减弱动效：不播过渡，直接落到目标状态。
            if animation is self.animation:
                self.amount = value
            else:
                self.press = value
            return
        animation.setStartValue(self.amount if animation is self.animation else self.press)
        animation.setEndValue(value); animation.start()

    def _apply(self):
        self._internal = True
        try:
            rect = QRect(self._base)
            if self.card:
                rect.translate(0, -round(2*self.amount))
            else:
                scale = 1 + .02*self.amount - (.02 + .02*self.amount)*self.press
                width, height = round(rect.width()*scale), round(rect.height()*scale)
                rect.setSize(rect.size().__class__(width, height))
                rect.moveCenter(self._base.center()+QPoint(0, round(self.press)))
            self.widget.setGeometry(rect)
            if self.wash:
                self.wash.setGeometry(self.widget.rect()); self.wash.raise_()
        finally:
            self._internal = False

    def reset(self):
        self.animation.stop(); self.press_animation.stop()
        self.hovered = False; self._value = 0; self._press = 0
        if self.wash:
            self.wash.amount = 0; self.wash.update()
        if self.shadow:
            try:
                self.shadow.hover = 0; self.shadow.update()
            except RuntimeError:
                self.shadow = None  # 宿主已析构：丢弃悬空 effect 引用
        self._apply()

    def detach(self):
        """显式解绑：停止动画并移除事件过滤器（重复样式化时替换旧实例）。"""
        try:
            self.animation.stop(); self.press_animation.stop()
        except RuntimeError:
            pass
        try:
            self.widget.removeEventFilter(self)
        except (RuntimeError, TypeError):
            pass

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind in (QEvent.Move, QEvent.Resize) and not self._internal:
            self._base = QRect(obj.geometry())
            if self.wash:
                self.wash.setGeometry(obj.rect())
        elif kind == QEvent.Enter and obj.isEnabled():
            self.hovered = True; self._animate(self.animation, 1.)
        elif kind == QEvent.Leave:
            self.hovered = False; self._animate(self.animation, 0.)
        elif kind == QEvent.MouseButtonPress and obj.isEnabled() and not self.card:
            self._animate(self.press_animation, 1.)
        elif kind == QEvent.MouseButtonRelease and not self.card:
            self._animate(self.press_animation, 0.)
        elif kind in (QEvent.Hide, QEvent.EnabledChange):
            self.reset()
        return False


class VisualClock(QObject):
    """One timer shared by visible progress/dots, asleep when nothing can animate."""
    def __init__(self, parent):
        super().__init__(parent)
        self.items = weakref.WeakSet()
        _clocks.add(self)
        self.timer = QTimer(self); self.timer.setInterval(16)
        self.timer.timeout.connect(self.tick)
        parent.installEventFilter(self)

    def add(self, item):
        self.items.add(item); self.wake()

    def wake(self):
        try:
            if not self.timer.isActive():
                self.timer.start()
        except RuntimeError:
            pass

    def tick(self):
        active = False
        for item in list(self.items):
            if sip.isdeleted(item):
                self.items.discard(item); continue
            visible = item.isVisible() and not item.window().isMinimized() and not item.visibleRegion().isEmpty()
            moving = visible and item.can_animate()
            item.set_running(moving)
            if moving:
                item.update(); active = True
        if not active:
            self.timer.stop()

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Show, QEvent.WindowStateChange, QEvent.Wheel):
            self.wake()
        return False


def clock_for(widget):
    window = widget.window()
    if not hasattr(window, '_visual_clock'):
        window._visual_clock = VisualClock(window)
    return window._visual_clock


class Shimmer(QWidget):
    def __init__(self, bar):
        super().__init__(bar)
        self.bar = bar; self._clock = None
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.NoFocus)
        self.setGeometry(bar.rect()); bar.installEventFilter(self)
        bar.valueChanged.connect(self.wake)
        self.show()

    def wake(self, *_):
        if self.isVisible():
            self._clock = clock_for(self); self._clock.add(self)

    def can_animate(self):
        return (not reduced_motion() and self.bar.minimum() < self.bar.value() < self.bar.maximum()
                and not self.bar.isError() and not self.bar.isPaused())

    def set_running(self, running):
        self.bar.setProperty('shimmerRunning', running)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Resize:
            self.setGeometry(obj.rect())
        elif event.type() in (QEvent.Show, QEvent.Paint):
            # Paint is a wakeup only on state changes (e.g. error -> resume).
            if self.can_animate() and not self.bar.property('shimmerRunning'):
                self.wake()
        elif event.type() == QEvent.Hide:
            self.set_running(False)
        return False

    def paintEvent(self, event):
        if not self.can_animate():
            return
        painter = QPainter(self); painter.setRenderHint(QPainter.Antialiasing)
        span = self.bar.maximum()-self.bar.minimum()
        filled = max(0, (self.bar.val-self.bar.minimum())/max(1, span))*self.width()
        path = QPainterPath(); path.addRoundedRect(QRectF(0, 0, filled, self.height()), 3, 3)
        painter.setClipPath(path)
        band = min(130, self.width()*.25)
        # 3 秒循环的流光从左到右扫过填充段（深空液态玻璃主题）。
        x = (time.monotonic() % 3.0)/3.0*(self.width()+2*band)-band
        brush = QLinearGradient(x-band, 0, x+band, self.height())
        brush.setColorAt(0, QColor(255, 255, 255, 0)); brush.setColorAt(.5, QColor(255, 255, 255, 125)); brush.setColorAt(1, QColor(255, 255, 255, 0))
        painter.fillRect(self.rect(), brush)


class StatusDot(BodyLabel):
    def __init__(self, color, active=False, parent=None):
        # Fluent's text overload calls self.__init__(parent); use its QWidget
        # overload directly so a subclass constructor is not recursively called.
        super().__init__(parent)
        self.setText('●')
        self.color = QColor(color); self.active = active
        self.setFixedSize(14, 18)
        self.setTextColor(color, color)

    def setTextColor(self, light=QColor(0,0,0), dark=QColor(255,255,255)):
        self.color = QColor(dark)
        super().setTextColor(light, dark)

    def can_animate(self):
        return self.active and not reduced_motion()

    def set_running(self, running):
        self.setProperty('pulseRunning', running)

    def showEvent(self, event):
        super().showEvent(event)
        clock_for(self).add(self)

    def hideEvent(self, event):
        self.set_running(False); super().hideEvent(event)

    def paintEvent(self, event):
        # Scrolling a list viewport does not send Show to its row widgets.
        # Repaint after scrolling back into view must wake the sleeping clock.
        if self.active and not self.property('pulseRunning') and not self.window().isMinimized() and not self.visibleRegion().isEmpty():
            clock_for(self).add(self)
        painter = QPainter(self); painter.setRenderHint(QPainter.Antialiasing)
        alpha = .75 + .25*math.cos(time.monotonic()*2*math.pi/1.5) if self.active else 1
        painter.setOpacity(alpha); painter.setPen(Qt.NoPen)
        if self.active:
            painter.setBrush(QColor(64, 158, 255, 25)); painter.drawEllipse(QRectF(1, 3, 12, 12))
        painter.setBrush(self.color); painter.drawEllipse(QRectF(4, 6, 6, 6))


class PageTransition(QWidget):
    """Crossfade cached pages, without nesting graphics effects or blocking input."""
    def __init__(self, stack):
        super().__init__(stack)
        self.stack = stack; self.old = QPixmap(); self.new = QPixmap(); self._blend = 1.
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)
        self.animation = QPropertyAnimation(self, b'blend', self)
        self.animation.setDuration(MOTION['slow']); self.animation.setEasingCurve(QEasingCurve.InOutCubic)
        self.animation.finished.connect(self.finish)
        stack.installEventFilter(self); self.hide()
        self._set_current = stack.setCurrentWidget
        # Both Fluent navigation and its back router call this stack method.
        # Decorating it also covers setCurrentIndex without altering routing.
        stack.setCurrentWidget = self.set_current_widget

    def set_current_widget(self, page, popOut=True):
        current = self.stack.currentWidget()
        if current is page or current is None or not self.stack.isVisible() or reduced_motion():
            return self._set_current(page, popOut)
        old = self.grab() if self.isVisible() else current.grab()
        result = self._set_current(page, popOut)
        self.begin(old)
        return result

    @pyqtProperty(float)
    def blend(self):
        return self._blend

    @blend.setter
    def blend(self, value):
        self._blend = value; self.update()

    def begin(self, old):
        self.animation.stop()
        self.hide()
        self.stack.layout().activate()
        page = self.stack.currentWidget()
        self.setGeometry(QRect(page.mapTo(self.stack, QPoint()), page.size()))
        # Transparent grabs would keep the old text at full opacity and reveal
        # live new-page content below. Flatten both against the window gradient
        # before crossfading, so old and new content really fade out and in.
        self.old = self._opaque_snapshot(old)
        self.new = self._opaque_snapshot(page.grab())
        self.blend = 0.; self.show(); self.raise_()
        self.animation.setStartValue(0.); self.animation.setEndValue(1.); self.animation.start()

    def _opaque_snapshot(self, page):
        snapshot = QPixmap(page.size())
        snapshot.setDevicePixelRatio(page.devicePixelRatioF())
        snapshot.fill(Qt.transparent)
        painter = QPainter(snapshot)
        origin = self.mapTo(self.window(), QPoint())
        painter.save(); painter.translate(-origin)
        paint_background(painter, QRectF(self.window().rect()))
        painter.restore(); painter.drawPixmap(0, 0, page); painter.end()
        return snapshot

    def finish(self):
        self.animation.stop(); self.hide(); self.old = QPixmap(); self.new = QPixmap()
        # The overlay occluded progress/dots, so their shared timer may have
        # gone idle. Reveal is a wakeup even though the page was already shown.
        if self.stack.isVisible():
            clock_for(self.stack).wake()

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Hide, QEvent.Resize):
            self.finish()
        return False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawPixmap(self.rect(), self.old)
        painter.setOpacity(self.blend); painter.drawPixmap(self.rect(), self.new)
