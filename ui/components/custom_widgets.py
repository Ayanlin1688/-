"""Shared QFluentWidgets helpers.

PyQt-Fluent-Widgets is a PyQt5 package, so the application uses PyQt5
throughout to keep every Fluent widget on one Qt binding.
"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFontDatabase, QColor, QPainter, QPen
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, ComboBox, ElevatedCardWidget,
    FluentIcon, HyperlinkButton, LineEdit, PrimaryPushButton, ProgressBar,
    PushButton, SpinBox, StrongBodyLabel, SubtitleLabel, SwitchButton,
    TextBrowser, TitleLabel, TransparentToolButton,
)
from ..materials import paint_surface

_font_loaded = False


def ensure_ui_font() -> list[str]:
    """Register a CJK-capable font when Qt cannot enumerate Windows fonts."""
    global _font_loaded
    if _font_loaded:
        return ["Microsoft YaHei", "Microsoft YaHei UI"]

    windows_dir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    font_path = windows_dir / "Fonts" / "msyh.ttc"
    if not font_path.is_file():
        return []

    font_id = QFontDatabase.addApplicationFont(str(font_path))
    families = QFontDatabase.applicationFontFamilies(font_id)
    _font_loaded = font_id >= 0
    return families


class StudioCard(CardWidget):
    """Real Fluent CardWidget with the studio palette and 12px corners."""
    def __init__(self, parent=None, elevated=False):
        super().__init__(parent)
        self.elevated = elevated
        self.setBorderRadius(12)

    def _normalBackgroundColor(self):
        return QColor(255,255,255,10)

    def _hoverBackgroundColor(self):
        return QColor(255,255,255,16)

    def _pressedBackgroundColor(self):
        return QColor(255,255,255,8)

    def paintEvent(self, event):
        painter = QPainter(self)
        paint_surface(self, painter, self.elevated, getattr(getattr(self, '_studio_motion', None), 'amount', 0))


def make_card(parent: QWidget | None = None, elevated: bool = False):
    return StudioCard(parent, elevated)


def vertical_layout(parent: QWidget, margins: int = 16, spacing: int = 10):
    layout = QVBoxLayout(parent)
    layout.setContentsMargins(margins, margins, margins, margins)
    layout.setSpacing(spacing)
    return layout


def horizontal_layout(parent: QWidget, margins: int = 0, spacing: int = 8):
    layout = QHBoxLayout(parent)
    layout.setContentsMargins(margins, margins, margins, margins)
    layout.setSpacing(spacing)
    return layout


def set_button_text(button, text: str) -> None:
    button.setText(text)
    button.setMinimumHeight(32)


__all__ = [
    "BodyLabel", "CaptionLabel", "CardWidget", "ComboBox", "ElevatedCardWidget",
    "FluentIcon", "HyperlinkButton", "LineEdit", "PrimaryPushButton", "ProgressBar",
    "PushButton", "SpinBox", "StrongBodyLabel", "SubtitleLabel", "SwitchButton",
    "TextBrowser", "TitleLabel", "TransparentToolButton", "make_card",
    "vertical_layout", "horizontal_layout", "set_button_text", "ensure_ui_font",
]
