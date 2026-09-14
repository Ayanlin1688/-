"""Resizable persistent log with a lightweight new-entry highlight."""
from html import escape
from PyQt5.QtCore import Qt, QPropertyAnimation, pyqtProperty, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QTextCursor
from PyQt5.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout
from qfluentwidgets import ComboBox, PushButton, TransparentToolButton, TextBrowser, FluentIcon as FIF
from .log_drawer import LogDrawer
from .workspace_surface import label, style_button


class LogHighlight(QWidget):
    def __init__(self, parent):
        super().__init__(parent); self._amount = 0.
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.animation = QPropertyAnimation(self, b'amount', self)
        self.animation.setDuration(480); self.animation.setStartValue(1.); self.animation.setEndValue(0.)

    @pyqtProperty(float)
    def amount(self):
        return self._amount

    @amount.setter
    def amount(self, value):
        self._amount = value; self.update()

    def flash(self, rect):
        self.setGeometry(rect); self.show(); self.raise_()
        self.animation.stop(); self.animation.start()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(round(-12*self.amount), 0, self.width(), self.height(), QColor(59, 130, 246, round(25*self.amount)))


class WorkspaceLog(LogDrawer):
    view_changed = pyqtSignal()
    COLORS = {'debug': '#9b8ac4', 'info': '#79a4e8', 'success': '#70bc8e', 'warning': '#d4bb64', 'error': '#dc8585'}

    def _build_ui(self):
        self.setObjectName('workspaceLog')
        self.setAttribute(Qt.WA_StyledBackground)
        self.setStyleSheet('#workspaceLog {background:rgba(0,0,0,0.3); border:1px solid rgba(255,255,255,0.08); border-top-left-radius:16px; border-top-right-radius:16px;}')
        root = QVBoxLayout(self); root.setContentsMargins(16, 7, 16, 8); root.setSpacing(4)
        header = QHBoxLayout(); self.header = header
        header.addWidget(label('执行日志', 13, '#f5f5f5', True)); header.addStretch(1)
        self.search_label = label(''); header.addWidget(self.search_label)
        self.filter_box = ComboBox(); self.filter_box.addItems(['全部', 'INFO', 'SUCCESS', 'WARNING', 'ERROR', 'DEBUG'])
        self.filter_box.setFixedSize(110, 28); self.filter_box.currentTextChanged.connect(self._render)
        header.addWidget(self.filter_box)
        self.export_button = style_button(PushButton('导出')); self.export_button.clicked.connect(self.export)
        self.clear_button = style_button(PushButton('清空')); self.clear_button.clicked.connect(self.clear)
        self.toggle_button = style_button(TransparentToolButton(FIF.UP)); self.toggle_button.setFixedSize(28, 28)
        self.toggle_button.setToolTip('折叠 / 展开日志'); self.toggle_button.clicked.connect(self.toggle)
        for button in (self.export_button, self.clear_button):
            button.setFixedHeight(28); header.addWidget(button)
        header.addWidget(self.toggle_button); root.addLayout(header)
        self.browser = TextBrowser(); self.browser.setProperty('studioStyled', True)
        self.browser.setOpenExternalLinks(False); self.browser.setMinimumHeight(0)
        self.browser.setStyleSheet('TextBrowser {background:transparent; border:0; color:#a1a1aa; font-family:"Cascadia Mono","Consolas"; font-size:11px;}')
        root.addWidget(self.browser, 1)
        self.highlight = LogHighlight(self.browser.viewport())
        self._animation = QPropertyAnimation(self, b'maximumHeight', self); self._animation.setDuration(180)
        self._animation.finished.connect(self._toggle_finished)
        self._saved_height = 140

    def _selected_level(self):
        value = self.filter_box.currentText()
        return None if value == '全部' else value.lower()

    def set_debug_mode(self, enabled):
        self.debug_mode = bool(enabled)
        if not enabled and self.filter_box.currentText() == 'DEBUG':
            self.filter_box.setCurrentText('全部')
        self._render()

    def _append_entry(self, timestamp, message, level):
        color = self.COLORS.get(level, self.COLORS['info'])
        self.browser.append(f'<span style="color:#62626d">{escape(timestamp)}</span> &nbsp; <span style="color:{color}">[{escape(level.upper())}]</span> &nbsp; <span style="color:#b0b0ba">{escape(str(message))}</span>')
        self.browser.moveCursor(QTextCursor.End)
        if self.isVisible() and self._expanded:
            rect = self.browser.cursorRect(); rect.setLeft(0); rect.setWidth(self.browser.viewport().width()); rect.setHeight(17)
            self.highlight.flash(rect)

    def focus_task(self, name):
        self.filter_box.setCurrentText('全部')
        self.search_label.setText(f'查找：{name}')
        self.browser.moveCursor(QTextCursor.Start)
        if not self.browser.find(name):
            self.search_label.setText(f'暂无该任务日志：{name}')
        if not self._expanded:
            self.toggle()

    def toggle(self):
        self._animation.stop()
        self._expanded = not self._expanded
        if not self._expanded:
            self._saved_height = max(120, self.height())
        self.browser.show()
        self.setMinimumHeight(44)
        self._animation.setStartValue(self.height()); self._animation.setEndValue(self._saved_height if self._expanded else 44)
        self._animation.start(); self.toggle_button.setIcon(FIF.UP if self._expanded else FIF.DOWN)

    def _toggle_finished(self):
        self.browser.setVisible(self._expanded)
        self.setMaximumHeight(16777215 if self._expanded else 44)
        self.view_changed.emit()
