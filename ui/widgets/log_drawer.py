"""Collapsible rich-text execution log."""

from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path

from PyQt5.QtCore import Qt, QPropertyAnimation
from PyQt5.QtGui import QTextCursor
from PyQt5.QtWidgets import QVBoxLayout, QWidget, QHBoxLayout, QFileDialog

from ..components.custom_widgets import ComboBox, PushButton, TextBrowser, TransparentToolButton, CaptionLabel
from core.i18n import tr


class LogDrawer(QWidget):
    COLORS = {"debug": "#8db9dc", "info": "#9999a0", "success": "#8ab978", "warning": "#ccb080", "error": "#d79292"}

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._expanded = True
        self.entries = []
        self.debug_mode = False
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 4, 8, 4)
        header_layout.addWidget(CaptionLabel("执行日志"))
        self.toggle_button = TransparentToolButton(self._icon("UP"))
        self.toggle_button.setIcon(self._icon("UP"))
        self.toggle_button.clicked.connect(self.toggle)
        header_layout.addWidget(self.toggle_button)
        header_layout.addStretch(1)
        self.filter_box = ComboBox()
        self.filter_box.addItems(["全部", "调试", "成功", "警告", "错误"])
        self.filter_box.currentTextChanged.connect(self._render)
        header_layout.addWidget(self.filter_box)
        export_button = PushButton("导出")
        clear_button = PushButton("清空")
        export_button.clicked.connect(self.export)
        clear_button.clicked.connect(self.clear)
        header_layout.addWidget(export_button)
        header_layout.addWidget(clear_button)
        root.addWidget(header)
        self.browser = TextBrowser()
        self.browser.setOpenExternalLinks(False)
        self.browser.setMinimumHeight(0)
        self.browser.setMaximumHeight(120)
        root.addWidget(self.browser)
        self._animation = QPropertyAnimation(self.browser, b"maximumHeight", self)
        self._animation.setDuration(180)
        self._animation.finished.connect(lambda: self.browser.setVisible(self._expanded))

    def append_log(self, message: str, level: str = "info") -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        level = level.lower()
        if level == 'debug' and not self.debug_mode:
            return
        self.entries.append((timestamp, str(message), level))
        if self._selected_level() in (None, level):
            self._append_entry(timestamp, message, level)

    def _selected_level(self):
        return {"调试": "debug", "成功": "success", "警告": "warning", "错误": "error"}.get(self.filter_box.currentText())

    def set_debug_mode(self, enabled):
        self.debug_mode = bool(enabled)
        if not enabled and self.filter_box.currentText() == '调试':
            self.filter_box.setCurrentText('全部')
        self._render()
        if self._expanded:
            self.browser.setMaximumHeight(260 if enabled else 120)

    def _append_entry(self, timestamp, message, level):
        color = self.COLORS.get(level, self.COLORS['info'])
        self.browser.append(f'<span style="color:{color}">[{timestamp}] [{level.upper()}] {escape(str(message))}</span>')
        self.browser.moveCursor(QTextCursor.End)

    def _render(self, *_):
        selected = self._selected_level()
        self.browser.clear()
        for timestamp, message, level in self.entries:
            if level == 'debug' and not self.debug_mode:
                continue
            if selected is None or selected == level:
                self._append_entry(timestamp, message, level)

    def clear(self) -> None:
        self.entries.clear()
        self.browser.clear()

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, tr("导出日志"), "execution.log", tr("日志 (*.log *.txt)"))
        if not path:
            return
        try:
            Path(path).write_text("\n".join(f"[{t}] [{level.upper()}] {message}" for t, message, level in self.entries
                                          if self.debug_mode or level != 'debug'), encoding="utf-8")
        except OSError as error:
            self.append_log(f"日志导出失败：{error}", "error")
        else:
            self.append_log(f"日志已导出：{path}", "success")

    def toggle(self) -> None:
        self._animation.stop()
        self._expanded = not self._expanded
        target = (260 if self.debug_mode else 120) if self._expanded else 0
        self._animation.setStartValue(0 if self._expanded else self.browser.height())
        self._animation.setEndValue(target)
        if self._expanded:
            self.browser.setVisible(True)
        self._animation.start()
        self.toggle_button.setIcon(self._icon("UP" if self._expanded else "DOWN"))

    @staticmethod
    def _icon(name):
        from qfluentwidgets import FluentIcon
        return getattr(FluentIcon, name)
