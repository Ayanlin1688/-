"""Extra interface scale, independent of Qt's system DPI scaling."""

import math
import weakref

from PyQt5 import sip
from PyQt5.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QAbstractButton, QHeaderView, QLayout, QBoxLayout, QGridLayout, QWidget
from qfluentwidgets import CardWidget, ImageLabel

from . import tokens

SCALE_CHOICES = ('auto', '1.0', '1.1', '1.25', '1.3', '1.4', '1.5', '1.6', '1.75')


def screen_factors(screen) -> dict[str, float | int]:
    """Return DPI / 96 and Qt logical screen/work-area dimensions, without re-scaling."""
    size = screen.size()
    available = screen.availableGeometry()
    return {
        'dpi': float(screen.logicalDotsPerInch()) / 96.0,
        'logical_width': size.width(),
        'logical_height': size.height(),
        'available_width': available.width(),
        'available_height': available.height(),
    }


def _clamp(value, low, high):
    return max(low, min(high, value))


def interface_factors(screen, ui_scale='auto'):
    factors = screen_factors(screen)
    dpi = factors['dpi']
    if not math.isfinite(dpi) or dpi <= 0:
        dpi = 1.0
    factor_size = _clamp(factors['logical_height'] / 1080.0, 1.0, 1.6)
    if str(ui_scale).strip().lower() == 'auto':
        extra = _clamp(max(dpi, factor_size) / dpi, 1.0, 2.0 / dpi)
    else:
        try:
            extra = float(ui_scale)
        except (TypeError, ValueError):
            return interface_factors(screen, 'auto')
        if not math.isfinite(extra):
            return interface_factors(screen, 'auto')
        extra = _clamp(extra, 1.0, 1.75)
    extra = _clamp(extra, 1.0, 2.0)
    return dict(factors, dpi=dpi, z=extra, total=dpi * extra)


def _alive(widget):
    return widget is not None and not sip.isdeleted(widget)


def _scalable(widget):
    if not _alive(widget):
        return False
    root = widget.window()
    return (_alive(root) and not getattr(root, '_closing', False)
            and not getattr(root, '_studio_delete_pending', False)
            and (root.isVisible() or not root.testAttribute(Qt.WA_WState_ExplicitShowHide)))


class InterfaceScaler(QObject):
    """Reapply immutable 100% baselines; never multiply previously scaled values."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.base_font = QFont(app.font())
        if self.base_font.pointSizeF() <= 0:
            self.base_font.setPointSizeF(10)
        self.scale = 1.0
        self.widgets = weakref.WeakKeyDictionary()
        self.layouts = weakref.WeakKeyDictionary()
        self.pending = weakref.WeakSet()
        self.applying = False
        self.disposed = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._refresh_pending)
        app.installEventFilter(self)

    def eventFilter(self, watched, event):
        if (self.disposed or self.applying
                or event.type() not in (QEvent.Polish, QEvent.Show, QEvent.StyleChange, QEvent.FontChange)
                or not isinstance(watched, QWidget) or not _scalable(watched)):
            return False
        state = self.widgets.get(watched)
        if (state is not None and state['applied_scale'] == self.scale
                and watched.styleSheet() == state['last_style']
                and not self._font_size_changed(watched, state)):
            return False
        self.pending.add(watched)
        if not self.timer.isActive():
            self.timer.start(0)
        return False

    def _font_baseline(self, widget):
        if not widget.testAttribute(Qt.WA_SetFont):
            return None
        font = QFont(widget.font())
        if (font.pixelSize() <= 0 and self.scale != 1.0
                and font.pointSizeF() == self.app.font().pointSizeF()):
            font.setPointSizeF(self.base_font.pointSizeF())
        return font

    def _font_size_changed(self, widget, state):
        if state['font'] is None:
            return False
        font = widget.font()
        return (font.pixelSize(), font.pointSizeF()) != state['last_font_size']

    def _capture(self, widget):
        minimum, maximum = widget.minimumSize(), widget.maximumSize()
        state = dict(font=self._font_baseline(widget), applied_scale=None,
                     style=widget.styleSheet(), last_style=None, style_sources={}, last_font_size=None,
                     minimum=minimum, maximum=maximum)
        if isinstance(widget, QAbstractButton):
            state['icon'] = widget.iconSize()
        if isinstance(widget, QHeaderView):
            state['section_size'] = widget.defaultSectionSize()
        if isinstance(widget, CardWidget):
            state['radius'] = (widget.getBorderRadius(),)
        elif isinstance(widget, ImageLabel):
            state['radius'] = tuple(getattr(widget, name) for name in (
                '_topLeftRadius', '_topRightRadius', '_bottomLeftRadius', '_bottomRightRadius'))
        self.widgets[widget] = state
        return state

    def _refresh_widget(self, widget):
        state = self.widgets.get(widget)
        if state is None:
            state = self._capture(widget)
        style = widget.styleSheet()
        scale_changed = state['applied_scale'] != self.scale
        if not scale_changed and style == state['last_style'] and not self._font_size_changed(widget, state):
            return
        if state['last_style'] is not None and style != state['last_style']:
            state['style'] = state['style_sources'].get(style, style)
        scaled_style = tokens.scale_stylesheet(state['style'], self.scale)
        state['style_sources'][scaled_style] = state['style']
        state['last_style'] = scaled_style
        if style != scaled_style:
            widget.setStyleSheet(scaled_style)
        if state['font'] is not None:
            scaled_font = QFont(widget.font())
            if state['font'].pixelSize() > 0:
                scaled_font.setPixelSize(max(1, round(state['font'].pixelSize() * self.scale)))
            else:
                scaled_font.setPointSizeF(state['font'].pointSizeF() * self.scale)
            if widget.font() != scaled_font:
                widget.setFont(scaled_font)
        if not widget.isWindow() and state['applied_scale'] != self.scale:
            minimum = state['minimum']
            maximum = state['maximum']
            scaled_minimum = QSize(round(minimum.width() * self.scale), round(minimum.height() * self.scale))
            scaled_maximum = QSize(*[16777215 if value == 16777215 else round(value * self.scale)
                                    for value in (maximum.width(), maximum.height())])
            if widget.minimumSize() != scaled_minimum:
                widget.setMinimumSize(scaled_minimum)
            if widget.maximumSize() != scaled_maximum:
                widget.setMaximumSize(scaled_maximum)
        if 'icon' in state and state['applied_scale'] != self.scale:
            icon = state['icon']
            widget.setIconSize(QSize(round(icon.width() * self.scale), round(icon.height() * self.scale)))
        if 'section_size' in state and state['applied_scale'] != self.scale:
            widget.setDefaultSectionSize(round(state['section_size'] * self.scale))
        if 'radius' in state and state['applied_scale'] != self.scale:
            widget.setBorderRadius(*[round(value * self.scale) for value in state['radius']])
        effect = widget.graphicsEffect()
        if effect is not None and state['applied_scale'] != self.scale:
            effect.updateBoundingRect()
        state['applied_scale'] = self.scale
        font = widget.font()
        state['last_font_size'] = (font.pixelSize(), font.pointSizeF())
        widget.updateGeometry()
        widget.update()

    def _refresh_layout(self, layout):
        state = self.layouts.get(layout)
        if state is None:
            margins = layout.contentsMargins()
            state = dict(margins=(margins.left(), margins.top(), margins.right(), margins.bottom()),
                         spacing=layout.spacing(), spacers=[])
            if isinstance(layout, QGridLayout):
                state['grid_spacing'] = (layout.horizontalSpacing(), layout.verticalSpacing())
            self.layouts[layout] = state
        current_spacers = {layout.itemAt(index).spacerItem() for index in range(layout.count())}
        state['spacers'] = [entry for entry in state['spacers'] if entry[0] in current_spacers]
        if isinstance(layout, QBoxLayout):
            known_spacers = {entry[0] for entry in state['spacers']}
            for spacer in current_spacers - known_spacers - {None}:
                size = spacer.sizeHint()
                policy = spacer.sizePolicy()
                state['spacers'].append((spacer, size, policy.horizontalPolicy(), policy.verticalPolicy()))
        scaled_margins = tuple(round(value * self.scale) for value in state['margins'])
        margins = layout.contentsMargins()
        if not layout.property('studioManagedMargins') and scaled_margins != (margins.left(), margins.top(), margins.right(), margins.bottom()):
            layout.setContentsMargins(*scaled_margins)
        if state['spacing'] >= 0 and layout.spacing() != round(state['spacing'] * self.scale):
            layout.setSpacing(round(state['spacing'] * self.scale))
        if 'grid_spacing' in state:
            horizontal, vertical = state['grid_spacing']
            if horizontal >= 0 and layout.horizontalSpacing() != round(horizontal * self.scale):
                layout.setHorizontalSpacing(round(horizontal * self.scale))
            if vertical >= 0 and layout.verticalSpacing() != round(vertical * self.scale):
                layout.setVerticalSpacing(round(vertical * self.scale))
        for spacer, size, horizontal, vertical in state['spacers']:
            if spacer in current_spacers and _alive(spacer):
                scaled_size = QSize(round(size.width() * self.scale), round(size.height() * self.scale))
                if spacer.sizeHint() != scaled_size:
                    spacer.changeSize(scaled_size.width(), scaled_size.height(), horizontal, vertical)
                    layout.invalidate()

    def _refresh_pending(self):
        self.timer.stop()
        if self.disposed or self.applying:
            return
        pending = list(self.pending)
        self.pending.clear()
        self.applying = True
        try:
            layouts = set()
            for widget in pending:
                if _scalable(widget):
                    self._refresh_widget(widget)
                    layouts.update(widget.findChildren(QLayout))
            for layout in layouts:
                if _alive(layout):
                    self._refresh_layout(layout)
        finally:
            self.applying = False

    def apply(self, scale):
        if self.disposed:
            return
        self.timer.stop()
        self.pending.clear()
        self.applying = True
        old_scale = self.scale
        roots = [root for root in self.app.topLevelWidgets() if _scalable(root)]
        frozen = [(root, root.updatesEnabled()) for root in roots if root.isVisible()]
        try:
            for root, _enabled in frozen:
                root.setUpdatesEnabled(False)
            widgets = [widget for widget in self.app.allWidgets() if _scalable(widget)]
            for widget in widgets:
                if _alive(widget) and widget not in self.widgets:
                    self._capture(widget)
            self.scale = scale
            tokens.set_interface_scale(scale)
            font = QFont(self.base_font)
            font.setPointSizeF(self.base_font.pointSizeF() * scale)
            if self.app.font() != font:
                self.app.setFont(font)
            for widget in widgets:
                if _alive(widget):
                    self._refresh_widget(widget)
            layouts = {layout for root in self.app.topLevelWidgets() if _scalable(root)
                       for layout in root.findChildren(QLayout)}
            for layout in layouts:
                if _alive(layout):
                    self._refresh_layout(layout)
            for widget in widgets:
                refresh = getattr(widget, 'refresh_interface_scale', None)
                if _alive(widget) and callable(refresh):
                    refresh()
            for root in roots:
                committed = getattr(root, 'on_interface_scale_committed', None)
                if _alive(root) and callable(committed):
                    committed(old_scale, scale)
            for layout in layouts:
                if _alive(layout):
                    layout.invalidate()
                    layout.activate()
        finally:
            self.applying = False
            for root, enabled in frozen:
                if _alive(root):
                    root.setUpdatesEnabled(enabled)
                    if enabled:
                        root.update()

    def dispose(self):
        if self.disposed:
            return
        self.app.removeEventFilter(self)
        self.apply(1.0)
        self.disposed = True
        self.timer.stop()
        self.pending.clear()
        self.widgets.clear()
        self.layouts.clear()
        if getattr(self.app, '_interface_scaler', None) is self:
            del self.app._interface_scaler
        self.deleteLater()


def apply_interface_scale(app, screen, ui_scale='auto'):
    screen = screen or app.primaryScreen()
    factors = interface_factors(screen, ui_scale)
    scaler = getattr(app, '_interface_scaler', None)
    if not _alive(scaler) or scaler.disposed:
        scaler = InterfaceScaler(app)
        app._interface_scaler = scaler
    scaler.apply(factors['z'])
    return factors
