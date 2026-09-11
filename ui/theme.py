"""Small Fluent style overrides shared by the app and capture script."""

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPalette, QColor, QPainter
from PyQt5.QtWidgets import QWidget
from qfluentwidgets import (
    PrimaryPushButton, PushButton, LineEdit, SpinBox, ComboBox, SettingCard,
    TextBrowser, setCustomStyleSheet,
)


def apply_palette(app):
    palette = app.palette()
    for role, color in ((QPalette.Window, "#0a0a0b"), (QPalette.Base, "#141416"),
                        (QPalette.AlternateBase, "#19191e"), (QPalette.Text, "#f4f4f5"),
                        (QPalette.WindowText, "#f4f4f5"), (QPalette.ButtonText, "#f4f4f5"),
                        (QPalette.Highlight, "#5e6ad2"), (QPalette.HighlightedText, "#ffffff")):
        palette.setColor(role, QColor(color))
    app.setPalette(palette)


class SettingSurface:
    """Palette-only paint override; all interaction/layout comes from Fluent."""
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor("#141416"))
        painter.setPen(QColor(255, 255, 255, 20))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 12, 12)


def style_controls(root):
    for widget in [root, *root.findChildren(QWidget)]:
        rules = ""
        if isinstance(widget, PrimaryPushButton):
            rules = "PrimaryPushButton {background:#5e6ad2; color:white; border:1px solid #7983e0; border-radius:8px;} PrimaryPushButton:hover {background:#6d78df;}"
        elif isinstance(widget, PushButton):
            rules = "PushButton {border-radius:8px;}"
        elif isinstance(widget, (LineEdit, SpinBox, ComboBox)):
            rules = "LineEdit, SpinBox, ComboBox {border-radius:8px; background:#1c1c20; color:#f4f4f5;}"
        elif isinstance(widget, SettingCard):
            rules = "SettingCard {background:#141416; border:1px solid rgba(255,255,255,0.08); border-radius:12px;}"
        elif isinstance(widget, TextBrowser):
            rules = "TextBrowser {background:#141416; border-radius:8px;}"
        if rules:
            setCustomStyleSheet(widget, rules, rules)


def style_page(page):
    page.setAttribute(Qt.WA_StyledBackground, True)
    page.setStyleSheet(
        f"#{page.objectName()} {{background:#0a0a0b;}}"
        "QScrollArea, #settingsContent {background:#0a0a0b; border:0;}"
        "QSplitter::handle {background:rgba(255,255,255,0.08);}"
        "QScrollBar:vertical {background:#0a0a0b; width:8px; margin:0;}"
        "QScrollBar::handle:vertical {background:#3e3e46; min-height:28px; border-radius:4px;}"
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {height:0;}"
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {background:none;}"
    )
