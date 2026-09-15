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
)
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu
from qfluentwidgets.components.widgets.acrylic_label import isAcrylicAvailable
from . import materials
from .materials import ACCENT, ERROR, WARNING, SECONDARY, paint_surface
from .motion import WidgetMotion, Shimmer

SCROLL_STYLE = """
QScrollBar:vertical {background:transparent; width:6px; margin:0;}
QScrollBar:horizontal {background:transparent; height:6px; margin:0;}
QScrollBar::handle {background:rgba(255,255,255,0.15); border-radius:3px; min-height:28px; min-width:28px;}
QScrollBar::handle:hover {background:rgba(255,255,255,0.3);}
QScrollBar::add-line, QScrollBar::sub-line {width:0; height:0;}
QScrollBar::add-page, QScrollBar::sub-page {background:transparent;}
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
    rules = """
    QListWidget {background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 rgba(24,26,38,0.97),stop:1 rgba(14,15,24,0.97));
                 color:white; border:1px solid rgba(255,255,255,0.08); border-radius:12px;}
    QListWidget::item {border-radius:8px; padding:4px;}
    QListWidget::item:hover, QListWidget::item:selected {background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 rgba(91,141,239,0.24),stop:1 rgba(124,108,240,0.12));}
    """ + SCROLL_STYLE
    setCustomStyleSheet(menu.view, rules, rules)
    return menu


def apply_palette(app, light=False):
    palette = app.palette()
    if light:
        colors = ((QPalette.Window, "#F4F6FB"), (QPalette.Base, "#FFFFFF"),
                  (QPalette.AlternateBase, "#EEF1F8"), (QPalette.Text, "#1A1D24"),
                  (QPalette.WindowText, "#1A1D24"), (QPalette.ButtonText, "#1A1D24"),
                  (QPalette.Highlight, ACCENT), (QPalette.HighlightedText, "#ffffff"))
    else:
        colors = ((QPalette.Window, "#0a0a0b"), (QPalette.Base, "#141418"),
                  (QPalette.AlternateBase, "#19191e"), (QPalette.Text, "#f4f4f5"),
                  (QPalette.WindowText, "#f4f4f5"), (QPalette.ButtonText, "#f4f4f5"),
                  (QPalette.Highlight, ACCENT), (QPalette.HighlightedText, "#ffffff"))
    for role, color in colors:
        palette.setColor(role, QColor(color))
    app.setPalette(palette)


# ---------------------------------------------------------------------------
# 主题模式：控件样式注册表 + 整体应用/刷新
_MODE_STYLED = []


def _dark_rules(kind):
    return {
        'primary': """PrimaryPushButton {background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #5B8DEF,stop:1 #7C6CF0);
            color:white; border:1px solid rgba(160,180,255,0.55); border-radius:8px;}
            PrimaryPushButton:hover {border-color:rgba(190,205,255,0.8); background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #6996f2,stop:1 #8A7BF3);}
            PrimaryPushButton:disabled {background:rgba(91,141,239,0.16); color:rgba(255,255,255,0.4); border-color:rgba(255,255,255,0.08);}""",
        'push': "PushButton {border-radius:8px; background:rgba(255,255,255,0.06); border:1px solid rgba(255,255,255,0.12); color:white;} PushButton:hover {background:rgba(255,255,255,0.09); border-color:rgba(255,255,255,0.2);} PushButton:disabled {color:rgba(255,255,255,0.4); background:rgba(255,255,255,0.02);}",
        'input': """LineEdit, SpinBox, ComboBox {border-radius:8px; background:rgba(255,255,255,0.06); color:white; border:1px solid rgba(255,255,255,0.12);}
            LineEdit:focus, SpinBox:focus {border:1px solid rgba(91,141,239,0.9); background:rgba(255,255,255,0.08);}
            LineEdit:disabled, SpinBox:disabled, ComboBox:disabled {color:rgba(255,255,255,0.4);}
            SpinBox QToolButton:hover {background:rgba(91,141,239,0.18); border-radius:6px;}""",
        'card': "SettingCard {background:transparent; border:0; border-radius:12px;}",
        'browser': "TextBrowser {background:rgba(0,0,0,0.4); color:rgba(255,255,255,0.6); border:1px solid rgba(255,255,255,0.08); border-radius:12px;}",
    }.get(kind, '')


def _light_rules(kind):
    return {
        'primary': """PrimaryPushButton {background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #5B8DEF,stop:1 #7C6CF0);
            color:white; border:1px solid rgba(130,150,235,0.6); border-radius:8px;}
            PrimaryPushButton:hover {border-color:rgba(150,170,250,0.9);}
            PrimaryPushButton:disabled {background:rgba(91,141,239,0.28); color:rgba(255,255,255,0.75); border-color:rgba(130,150,235,0.3);}""",
        'push': "PushButton {border-radius:8px; background:rgba(15,26,52,0.06); border:1px solid rgba(15,26,52,0.16); color:#16233B;} PushButton:hover {background:rgba(15,26,52,0.1); border-color:rgba(15,26,52,0.3);} PushButton:disabled {color:rgba(15,26,52,0.35); background:rgba(15,26,52,0.04);}",
        'input': """LineEdit, SpinBox, ComboBox {border-radius:8px; background:rgba(255,255,255,0.78); color:#1A1D24; border:1px solid rgba(15,26,52,0.16);}
            LineEdit:focus, SpinBox:focus {border:1px solid rgba(91,141,239,0.9); background:rgba(255,255,255,0.96);}
            LineEdit:disabled, SpinBox:disabled, ComboBox:disabled {color:rgba(15,26,52,0.4);}
            SpinBox QToolButton:hover {background:rgba(91,141,239,0.16); border-radius:6px;}""",
        'card': "SettingCard {background:transparent; border:0; border-radius:12px;}",
        'browser': "TextBrowser {background:rgba(255,255,255,0.55); color:#2A2F3A; border:1px solid rgba(15,26,52,0.12); border-radius:12px;}",
    }.get(kind, '')


def _set_control_rules(widget, kind):
    rules = _light_rules(kind) if materials.is_light() else _dark_rules(kind)
    if rules:
        setCustomStyleSheet(widget, rules, rules)


def _register_mode_widget(widget, kind):
    _MODE_STYLED.append((weakref.ref(widget), kind))


def refresh_mode_styles():
    light = materials.is_light()
    for ref, kind in list(_MODE_STYLED):
        widget = ref()
        if widget is None:
            continue
        try:
            if kind == 'group':
                widget.titleLabel.setStyleSheet('color:#1A1D24; font-size:18px; font-weight:600;' if light
                                                else 'color:#ffffff; font-size:18px; font-weight:600;')
            elif kind == 'caption2':
                color = QColor('#5A6271') if light else SECONDARY
                widget.setTextColor(color, color)
            else:
                _set_control_rules(widget, kind)
        except RuntimeError:
            continue


def _system_prefers_light():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize')
        value, _ = winreg.QueryValueEx(key, 'AppsUseLightTheme')
        return bool(value)
    except Exception:
        return False


def apply_ui_mode(mode='dark'):
    """切换深空 / 白玉主题：全局材料、Fluent 主题、注册控件与窗口重绘。"""
    if mode == 'system':
        light = _system_prefers_light()
    else:
        light = (mode == 'light')
    materials.set_light_mode(light)
    try:
        setTheme(Theme.LIGHT if light else Theme.DARK, save=False)
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
            widget.setNormalColor(QColor('#ffffff'))
            widget.setHoverColor(QColor('#ffffff'))
            widget.setPressedColor(QColor('#ffffff'))
        if isinstance(widget, SettingCardGroup):
            widget.titleLabel.setStyleSheet('color:#ffffff; font-size:18px; font-weight:600;')
            _register_mode_widget(widget, 'group')
        if isinstance(widget, ComboBox):
            widget._createComboMenu = MethodType(studio_combo_menu, widget)
        if isinstance(widget, (PushButton, ToolButton, ComboBox)) and widget.__class__.__name__ != 'Indicator':
            widget._studio_motion = WidgetMotion(widget, primary=isinstance(widget, PrimaryPushButton))
        if isinstance(widget, (CardWidget, SettingCard)):
            widget._studio_motion = WidgetMotion(widget, card=True)
        if isinstance(widget, ProgressBar):
            widget.setCustomBarColor(ACCENT, ACCENT)
            widget.setCustomBackgroundColor(QColor(255,255,255,24), QColor(255,255,255,24))
            original_bar_color = widget.barColor
            def bar_color(bar, original=original_bar_color):
                return QColor(ERROR) if bar.isError() else QColor(WARNING) if bar.isPaused() else original()
            widget.barColor = MethodType(bar_color, widget)
            widget._studio_shimmer = Shimmer(widget)
        if isinstance(widget, SwitchButton):
            widget.setCheckedIndicatorColor(ACCENT, ACCENT)
            widget.indicator.slideAni.setDuration(200)
            indicator = widget.indicator
            original_background = indicator._backgroundColor
            def sliding_color(control, original=original_background):
                color = original()
                if not control.isEnabled():
                    return color
                fraction = max(0, min(1, (control.sliderX-5)/20))
                return QColor(91, 141, 239, round(255*fraction)) if fraction > 0 else color
            indicator._backgroundColor = MethodType(sliding_color, indicator)
        if isinstance(widget, CaptionLabel):
            font = widget.font(); font.setPixelSize(12); widget.setFont(font)
            if not widget.text().startswith('●') and widget.text() not in {'生成中', '失败', '已完成', '重试中', '等待冷却', '等待中', '已跳过'}:
                widget.setTextColor(SECONDARY, SECONDARY)
                _register_mode_widget(widget, 'caption2')
        elif isinstance(widget, StrongBodyLabel):
            font = widget.font(); font.setPixelSize(14 if widget.parentWidget().__class__.__name__ in ('TaskQueueRow', 'TaskGroupHeader', 'RecentCompletedRow') else 18)
            font.setBold(True); widget.setFont(font)
        elif isinstance(widget, TitleLabel):
            font = widget.font(); font.setPixelSize(28); font.setBold(True); widget.setFont(font)
        if widget.__class__.__name__ in ('ScrollBar', 'SmoothScrollBar') and hasattr(widget, 'handle'):
            widget.handle.setDarkColor(QColor(255,255,255,38))
            widget.groove.darkBackgroundColor = QColor(0,0,0,0)
            if widget.orientation() == Qt.Vertical:
                widget.handle.setFixedWidth(6)
            else:
                widget.handle.setFixedHeight(6)
            widget._studio_scroll_hover = ScrollHover(widget)


def style_page(page, opaque_window=None):
    page.setAttribute(Qt.WA_StyledBackground, True)
    # Child pages inherit the main window material. Standalone frameless dialogs
    # (now rounded via StudioDialog) should stay transparent so their own
    # rounded backing shows through.
    if opaque_window is None:
        opaque_window = page.isWindow()
    background = 'qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #0A0B12,stop:1 #12131F)' if opaque_window else 'transparent'
    page.setStyleSheet(
        f"#{page.objectName()} {{background:{background};}}"
        "QScrollArea, #settingsContent {background:transparent; border:0;}"
        "QSplitter::handle {background:transparent;}" + SCROLL_STYLE
    )
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
            bar.handle.setDarkColor(QColor(255,255,255,77 if event.type() == QEvent.Enter else 38))
        return False
