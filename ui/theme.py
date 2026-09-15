"""Shared dark Fluent palette, materials and non-invasive visual installation."""

import weakref
from types import MethodType
from PyQt5.QtCore import Qt, QEvent, QObject, QSize
from PyQt5.QtGui import QPalette, QColor, QPainter
from PyQt5.QtWidgets import QWidget, QAbstractScrollArea, QApplication
from qframelesswindow.titlebar.title_bar_buttons import TitleBarButton
from qfluentwidgets import (
    PrimaryPushButton, PushButton, LineEdit, SpinBox, ComboBox, SettingCard,
    TextBrowser, setCustomStyleSheet, CardWidget, CaptionLabel, StrongBodyLabel,
    TitleLabel, SwitchButton, ProgressBar, ToolButton, SettingCardGroup, Theme, setTheme,
    setThemeColor,
)
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu
from qfluentwidgets.components.widgets.acrylic_label import isAcrylicAvailable
from . import materials
from .materials import ACCENT, ERROR, WARNING, SECONDARY, paint_surface
from .motion import WidgetMotion, Shimmer
from core.i18n import tr

# 状态标签颜色豁免表：这些文案保留语义色，不参与 Caption 次级灰化（按当前语言匹配）。
_STATUS_TEXTS = {'生成中', '失败', '已完成', '重试中', '等待冷却', '等待中', '已跳过'}

def _rgba_str(color, alpha):
    """把 #rrggbb 或 QColor 转成 rgba(...) 字符串供样式表使用。"""
    value = color if isinstance(color, QColor) else QColor(color)
    return 'rgba(%d,%d,%d,%s)' % (value.red(), value.green(), value.blue(), alpha)


def scroll_style():
    """滚动条样式：颜色跟随当前主题墨色（深色用白、浅色用墨）。"""
    ir, ig, ib = materials.palette()['ink']
    first, second = (0.28, 0.46) if materials.is_light() else (0.15, 0.3)
    return f"""
QScrollBar:vertical {{background:transparent; width:6px; margin:0;}}
QScrollBar:horizontal {{background:transparent; height:6px; margin:0;}}
QScrollBar::handle {{background:rgba({ir},{ig},{ib},{first}); border-radius:3px; min-height:28px; min-width:28px;}}
QScrollBar::handle:hover {{background:rgba({ir},{ig},{ib},{second});}}
QScrollBar::add-line, QScrollBar::sub-line {{width:0; height:0;}}
QScrollBar::add-page, QScrollBar::sub-page {{background:transparent;}}
"""


def studio_combo_menu(combo):
    # Acrylic is optional in Fluent's [full] installation. The gradient material
    # fallback avoids pulling scipy/numpy into a lightweight desktop app.
    if isAcrylicAvailable:
        from qfluentwidgets.components.material.acrylic_combo_box import AcrylicComboBoxMenu
        menu = AcrylicComboBoxMenu(combo)
        menu.view.acrylicBrush.setBlurPicSize(QSize(240, 240))
    else:
        menu = ComboBoxMenu(combo)
    th = materials.palette()
    top = _rgba_str('#FCFDFF' if th['light'] else th['bg2'], 0.97)
    bottom = _rgba_str(th['bg1'], 0.97)
    fg = th['text1'] if th['light'] else '#ffffff'
    border = _rgba_str(th['text1'], 0.12) if th['light'] else 'rgba(255,255,255,0.08)'
    ar, ag, ab = th['accent_rgb']
    br, bv, bb = th['accent2_rgb']
    rules = f"""
    QListWidget {{background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {top},stop:1 {bottom});
                 color:{fg}; border:1px solid {border}; border-radius:12px;}}
    QListWidget::item {{border-radius:8px; padding:4px;}}
    QListWidget::item:hover, QListWidget::item:selected {{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 rgba({ar},{ag},{ab},0.24),stop:1 rgba({br},{bv},{bb},0.12));}}
    """ + scroll_style()
    setCustomStyleSheet(menu.view, rules, rules)
    return menu


def apply_palette(app, light=None):
    th = materials.palette()
    if light is None:
        light = bool(th['light'])
    palette = app.palette()
    if light:
        colors = ((QPalette.Window, th['bg1']), (QPalette.Base, "#FFFFFF"),
                  (QPalette.AlternateBase, "#EEF1F8"), (QPalette.Text, th['text1']),
                  (QPalette.WindowText, th['text1']), (QPalette.ButtonText, th['text1']),
                  (QPalette.Highlight, th['accent']), (QPalette.HighlightedText, "#ffffff"))
    else:
        colors = ((QPalette.Window, th['bg1']), (QPalette.Base, "#141418"),
                  (QPalette.AlternateBase, "#19191e"), (QPalette.Text, th['text1']),
                  (QPalette.WindowText, th['text1']), (QPalette.ButtonText, th['text1']),
                  (QPalette.Highlight, th['accent']), (QPalette.HighlightedText, "#ffffff"))
    for role, color in colors:
        palette.setColor(role, QColor(color))
    app.setPalette(palette)


# ---------------------------------------------------------------------------
# 主题模式：控件样式注册表 + 整体应用/刷新
_MODE_STYLED = []


def _theme_rules(kind):
    """控件样式规则：颜色全部来自当前主题调色板。"""
    th = materials.palette()
    light = bool(th['light'])
    ir, ig, ib = th['ink']
    ar, ag, ab = th['accent_rgb']
    t1 = th['text1']
    lr, lg, lb = th.get('line', th['ink'])

    def rgba(r, g, b, a):
        return 'rgba(%d,%d,%d,%s)' % (r, g, b, a)

    if kind == 'primary':
        border = rgba(ar, ag, ab, '0.6' if light else '0.55')
        hover = rgba(ar, ag, ab, '0.9' if light else '0.85')
        if light:
            disabled = ('background:%s; color:rgba(255,255,255,0.75); border-color:%s;'
                        % (rgba(ar, ag, ab, '0.28'), rgba(ar, ag, ab, '0.3')))
        else:
            disabled = ('background:%s; color:rgba(255,255,255,0.4); border-color:rgba(255,255,255,0.08);'
                        % rgba(ar, ag, ab, '0.16'))
        return ('PrimaryPushButton {background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 %s,stop:1 %s);'
                ' color:white; border:1px solid %s; border-radius:8px;}'
                ' PrimaryPushButton:hover {border-color:%s;}'
                ' PrimaryPushButton:disabled {%s}') % (th['accent'], th['accent2'], border, hover, disabled)
    if kind == 'push':
        if light:
            return ('PushButton {border-radius:8px; background:%s; border:1px solid %s; color:%s;}'
                    ' PushButton:hover {background:%s; border-color:%s;}'
                    ' PushButton:disabled {color:%s; background:%s;}') % (
                rgba(lr, lg, lb, '0.06'), rgba(lr, lg, lb, '0.16'), t1,
                rgba(lr, lg, lb, '0.1'), rgba(lr, lg, lb, '0.3'),
                rgba(ir, ig, ib, '0.35'), rgba(ir, ig, ib, '0.04'))
        return ('PushButton {border-radius:8px; background:rgba(255,255,255,0.06); border:1px solid rgba(255,255,255,0.12); color:white;}'
                ' PushButton:hover {background:rgba(255,255,255,0.09); border-color:rgba(255,255,255,0.2);}'
                ' PushButton:disabled {color:rgba(255,255,255,0.4); background:rgba(255,255,255,0.02);}')
    if kind == 'input':
        focus = rgba(ar, ag, ab, '0.9')
        if light:
            return ('LineEdit, SpinBox, ComboBox {border-radius:8px; background:rgba(255,255,255,0.78); color:%s; border:1px solid %s;}'
                    ' LineEdit:focus, SpinBox:focus {border:1px solid %s; background:rgba(255,255,255,0.96);}'
                    ' LineEdit:disabled, SpinBox:disabled, ComboBox:disabled {color:%s;}'
                    ' SpinBox QToolButton:hover {background:%s; border-radius:6px;}') % (
                t1, rgba(lr, lg, lb, '0.16'), focus, rgba(ir, ig, ib, '0.4'), rgba(ar, ag, ab, '0.16'))
        return ('LineEdit, SpinBox, ComboBox {border-radius:8px; background:rgba(255,255,255,0.06); color:white; border:1px solid rgba(255,255,255,0.12);}'
                ' LineEdit:focus, SpinBox:focus {border:1px solid %s; background:rgba(255,255,255,0.08);}'
                ' LineEdit:disabled, SpinBox:disabled, ComboBox:disabled {color:rgba(255,255,255,0.4);}'
                ' SpinBox QToolButton:hover {background:%s; border-radius:6px;}') % (focus, rgba(ar, ag, ab, '0.18'))
    if kind == 'card':
        return 'SettingCard {background:transparent; border:0; border-radius:12px;}'
    if kind == 'browser':
        if light:
            return ('TextBrowser {background:rgba(255,255,255,0.55); color:%s; border:1px solid %s; border-radius:12px;}'
                    % (th['text2'], rgba(lr, lg, lb, '0.12')))
        return 'TextBrowser {background:rgba(0,0,0,0.38); color:rgba(255,255,255,0.72); border:1px solid rgba(255,255,255,0.08); border-radius:12px;}'
    return ''


def _set_control_rules(widget, kind):
    rules = _theme_rules(kind)
    if rules:
        setCustomStyleSheet(widget, rules, rules)


def _register_mode_widget(widget, kind):
    _MODE_STYLED.append((weakref.ref(widget), kind))


def refresh_mode_styles():
    light = materials.is_light()
    th = materials.palette()
    for ref, kind in list(_MODE_STYLED):
        widget = ref()
        if widget is None:
            continue
        try:
            if kind == 'group':
                widget.titleLabel.setStyleSheet('color:%s; font-size:18px; font-weight:600;' % th['text1'])
            elif kind == 'caption2':
                color = QColor(th['text2'])
                widget.setTextColor(color, color)
            elif kind == 'progress':
                accent = QColor(th['accent'])
                widget.setCustomBarColor(accent, accent)
                track = QColor(255, 255, 255, 24) if not light else QColor(*th['ink'], 28)
                widget.setCustomBackgroundColor(track, track)
            elif kind == 'switch':
                accent = QColor(th['accent'])
                widget.setCheckedIndicatorColor(accent, accent)
            elif kind == 'titlebtn':
                color = QColor('#ffffff') if not light else QColor(*th['ink'], 210)
                widget.setNormalColor(color)
                widget.setHoverColor(color)
                widget.setPressedColor(color)
            elif kind == 'scrollbar':
                widget.handle.setDarkColor(QColor(*th['ink'], 64 if light else 38))
                widget.groove.darkBackgroundColor = QColor(0, 0, 0, 0)
            elif kind == 'page':
                opaque = bool(widget.property('studioOpaque'))
                widget.setStyleSheet(_page_stylesheet(widget, opaque))
            else:
                _set_control_rules(widget, kind)
        except RuntimeError:
            continue


def apply_ui_mode(theme_id='dark'):
    """切换主题：全局调色板、Fluent 主题、注册控件与窗口重绘一次做完。"""
    active = materials.apply_theme_tokens(theme_id)
    light = materials.is_light()
    try:
        setTheme(Theme.LIGHT if light else Theme.DARK, save=False)
    except Exception:
        pass
    try:
        setThemeColor(QColor(materials.palette()['accent']), save=False)
    except Exception:
        pass
    app = QApplication.instance()
    if app is not None:
        apply_palette(app, light)
    materials.apply_theme_to_labels()
    try:
        materials.restyle_theme_buttons()
    except Exception:
        pass
    refresh_mode_styles()
    materials.run_theme_callbacks()
    if app is not None:
        for widget in app.topLevelWidgets():
            try:
                widget.update()
            except Exception:
                pass
    return active


class SettingSurface:
    """Palette-only paint override; all interaction/layout comes from Fluent."""
    def paintEvent(self, event):
        painter = QPainter(self)
        paint_surface(self, painter, hover=getattr(getattr(self, '_studio_motion', None), 'amount', 0))


def style_controls(root):
    for widget in [root, *root.findChildren(QWidget)]:
        if widget.property('studioStyled'):
            continue
        widget.setProperty('studioStyled', True)
        kind = None
        if isinstance(widget, PrimaryPushButton):
            kind = 'primary'
        elif isinstance(widget, PushButton):
            kind = 'push'
        elif isinstance(widget, (LineEdit, SpinBox, ComboBox)):
            kind = 'input'
        elif isinstance(widget, SettingCard):
            kind = 'card'
        elif isinstance(widget, TextBrowser):
            kind = 'browser'
        if kind:
            _set_control_rules(widget, kind)
            _register_mode_widget(widget, kind)
        if isinstance(widget, TitleBarButton):
            _light = materials.is_light()
            _color = QColor('#ffffff') if not _light else QColor(*materials.palette()['ink'], 210)
            widget.setNormalColor(_color)
            widget.setHoverColor(_color)
            widget.setPressedColor(_color)
            _register_mode_widget(widget, 'titlebtn')
        if isinstance(widget, SettingCardGroup):
            widget.titleLabel.setStyleSheet('color:%s; font-size:18px; font-weight:600;' % materials.palette()['text1'])
            _register_mode_widget(widget, 'group')
        if isinstance(widget, ComboBox):
            widget._createComboMenu = MethodType(studio_combo_menu, widget)
        if isinstance(widget, (PushButton, ToolButton, ComboBox)) and widget.__class__.__name__ != 'Indicator':
            widget._studio_motion = WidgetMotion(widget, primary=isinstance(widget, PrimaryPushButton))
        if isinstance(widget, (CardWidget, SettingCard)):
            widget._studio_motion = WidgetMotion(widget, card=True)
        if isinstance(widget, ProgressBar):
            _accent = QColor(materials.palette()['accent'])
            widget.setCustomBarColor(_accent, _accent)
            _track = QColor(255, 255, 255, 24) if not materials.is_light() else QColor(*materials.palette()['ink'], 28)
            widget.setCustomBackgroundColor(_track, _track)
            original_bar_color = widget.barColor
            def bar_color(bar, original=original_bar_color):
                return QColor(ERROR) if bar.isError() else QColor(WARNING) if bar.isPaused() else original()
            widget.barColor = MethodType(bar_color, widget)
            widget._studio_shimmer = Shimmer(widget)
            _register_mode_widget(widget, 'progress')
        if isinstance(widget, SwitchButton):
            _accent = QColor(materials.palette()['accent'])
            widget.setCheckedIndicatorColor(_accent, _accent)
            widget.indicator.slideAni.setDuration(200)
            indicator = widget.indicator
            original_background = indicator._backgroundColor
            def sliding_color(control, original=original_background):
                color = original()
                if not control.isEnabled():
                    return color
                fraction = max(0, min(1, (control.sliderX-5)/20))
                ar, ag, ab = materials.accent_rgb()
                return QColor(ar, ag, ab, round(255*fraction)) if fraction > 0 else color
            indicator._backgroundColor = MethodType(sliding_color, indicator)
            _register_mode_widget(widget, 'switch')
        if isinstance(widget, CaptionLabel):
            font = widget.font(); font.setPixelSize(12); widget.setFont(font)
            if not widget.text().startswith('●') and widget.text() not in {tr(text) for text in _STATUS_TEXTS}:
                widget.setTextColor(SECONDARY, SECONDARY)
                _register_mode_widget(widget, 'caption2')
        elif isinstance(widget, StrongBodyLabel):
            font = widget.font(); font.setPixelSize(14 if widget.parentWidget().__class__.__name__ in ('TaskQueueRow', 'TaskGroupHeader', 'RecentCompletedRow') else 18)
            font.setBold(True); widget.setFont(font)
        elif isinstance(widget, TitleLabel):
            font = widget.font(); font.setPixelSize(28); font.setBold(True); widget.setFont(font)
        if widget.__class__.__name__ in ('ScrollBar', 'SmoothScrollBar') and hasattr(widget, 'handle'):
            widget.handle.setDarkColor(QColor(*materials.palette()['ink'], 64 if materials.is_light() else 38))
            widget.groove.darkBackgroundColor = QColor(0,0,0,0)
            if widget.orientation() == Qt.Vertical:
                widget.handle.setFixedWidth(6)
            else:
                widget.handle.setFixedHeight(6)
            widget._studio_scroll_hover = ScrollHover(widget)
            _register_mode_widget(widget, 'scrollbar')


def _page_stylesheet(page, opaque_window):
    background = 'transparent'
    if opaque_window:
        th = materials.palette()
        background = 'qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 %s,stop:1 %s)' % (th['bg1'], th['bg2'])
    return ("#%s {background:%s;}"
            "QScrollArea, #settingsContent {background:transparent; border:0;}"
            "QSplitter::handle {background:transparent;}%s") % (page.objectName(), background, scroll_style())


def style_page(page, opaque_window=None):
    page.setAttribute(Qt.WA_StyledBackground, True)
    # Child pages inherit the main window material. Standalone frameless dialogs
    # (now rounded via StudioDialog) should stay transparent so their own
    # rounded backing shows through.
    if opaque_window is None:
        opaque_window = page.isWindow()
    page.setProperty('studioOpaque', bool(opaque_window))
    page.setStyleSheet(_page_stylesheet(page, opaque_window))
    _register_mode_widget(page, 'page')
    page.setProperty('isStackedTransparent', True)
    for area in page.findChildren(QAbstractScrollArea):
        area.viewport().setAutoFillBackground(False)
        # QScrollArea.setWidget enables autoFillBackground on its content.
        # Clear it as well so the window gradient shows between the cards.
        if hasattr(area, 'widget') and area.widget() is not None:
            area.widget().setAutoFillBackground(False)


class ScrollHover(QObject):
    def __init__(self, bar):
        super().__init__(bar)
        bar.installEventFilter(self)

    def eventFilter(self, bar, event):
        if event.type() in (QEvent.Enter, QEvent.Leave):
            light = materials.is_light()
            alpha = (110 if light else 77) if event.type() == QEvent.Enter else (64 if light else 38)
            bar.handle.setDarkColor(QColor(*materials.palette()['ink'], alpha))
        return False
