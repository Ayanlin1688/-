"""Shared QFluentWidgets helpers.

PyQt-Fluent-Widgets is a PyQt5 package, so the application uses PyQt5
throughout to keep every Fluent widget on one Qt binding.
"""

from __future__ import annotations

import os
import logging
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont, QFontDatabase, QColor, QPainter, QPen
from PyQt5.QtWidgets import QApplication, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, ComboBox, ElevatedCardWidget,
    FluentIcon, HyperlinkButton, LineEdit, PrimaryPushButton, ProgressBar,
    PushButton, SpinBox, StrongBodyLabel, SubtitleLabel, SwitchButton,
    TextBrowser, TitleLabel, TransparentToolButton,
)
from ..materials import paint_surface
from ..fonts import CJK_FAMILIES, resolve_font_families

_font_loaded = False
_font_result = ([], False, '')


def ensure_ui_font() -> tuple[list[str], bool, str]:
    """Select a CJK font without resetting an already scaled application size."""
    global _font_loaded, _font_result
    app = QApplication.instance()
    if app is None:
        return [], False, ''
    if not _font_loaded:
        preferred, source = '', ''
        windows_dir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        for name in ('msyh.ttc', 'msyhbd.ttc', 'msyhl.ttc', 'simhei.ttf', 'simsun.ttc', 'msjh.ttc'):
            font_path = windows_dir / 'Fonts' / name
            if not font_path.is_file():
                continue
            try:
                font_id = QFontDatabase.addApplicationFont(str(font_path))
                families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
            except (RuntimeError, OSError):
                continue
            if families:
                preferred, source = families[0], str(font_path)
                break
        if not preferred:
            try:
                available = QFontDatabase().families()
            except (RuntimeError, OSError):
                available = []
            preferred = next((family for family in resolve_font_families(available)
                              if family.casefold() in {name.casefold() for name in CJK_FAMILIES}), '')
        if preferred:
            try:
                fallback_families = resolve_font_families()
            except (RuntimeError, OSError):
                fallback_families = []
            families = list(dict.fromkeys([preferred, *fallback_families]))
        else:
            families = []
        _font_result = families, bool(source), source
        _font_loaded = True
        from .. import materials
        materials._UI_FONT_FAMILY = None
        if not families:
            logging.getLogger(__name__).warning('No CJK UI font found; keeping the system font')
    families, registered, source = _font_result
    if families:
        font = QFont(app.font())
        if hasattr(font, 'setFamilies'):
            font.setFamilies(families)
        else:
            font.setFamily(families[0])
            font.setStyleHint(QFont.AnyStyle)
        font.setStyleStrategy(QFont.PreferMatch | QFont.PreferAntialias)
        if app.font() != font:
            app.setFont(font)
    return list(families), registered, source


class StudioCard(CardWidget):
    """Real Fluent CardWidget with the studio palette and 12px corners."""
    def __init__(self, parent=None, elevated=False):
        super().__init__(parent)
        self.elevated = elevated
        # Graphics effects must capture transparent rounded corners, otherwise
        # the native Windows backing store can leave a black rectangular frame.
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAutoFillBackground(False)
        self.setBorderRadius(12)

    def _normalBackgroundColor(self):
        return QColor(255,255,255,13)

    def _hoverBackgroundColor(self):
        return QColor(255,255,255,21)

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
