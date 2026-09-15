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
        # 新日志行从左滑入的高亮淡出。
        painter.fillRect(round(-14*self.amount), 0, self.width(), self.height(), QColor(91, 141, 239, round(28*self.amount)))


class FilterTabs(QWidget):
    """圆角标签筛选栏：全部/INFO/SUCCESS/WARNING/ERROR（激活态蓝紫渐变）。"""
    currentTextChanged = pyqtSignal(str)
    LEVELS = ['全部', 'INFO', 'SUCCESS', 'WARNING', 'ERROR', 'DEBUG']

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self); row.setContentsMargins(0, 0, 0, 0); row.setSpacing(6)
        self._buttons = {}
        for name in self.LEVELS:
            chip = PushButton(name)
            chip.setProperty('studioStyled', True)
            chip.setFixedHeight(26)
            chip.setCursor(Qt.PointingHandCursor)
            chip.clicked.connect(lambda checked=False, n=name: self.setCurrentText(n))
            self._buttons[name] = chip
            row.addWidget(chip)
        self._current = '全部'
        self._apply()
        self._buttons['DEBUG'].hide()

    def currentText(self):
        return self._current

    def setCurrentText(self, text):
        if text not in self._buttons or text == self._current:
            return
        self._current = text
        self._apply()
        self.currentTextChanged.emit(self._current)

    def set_debug_visible(self, visible):
        self._buttons['DEBUG'].setVisible(bool(visible))
        if not visible and self._current == 'DEBUG':
            self.setCurrentText('全部')

    def _apply(self):
        for name, chip in self._buttons.items():
            if name == self._current:
                chip.setStyleSheet(
                    'QPushButton {background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #5B8DEF,stop:1 #7C6CF0);'
                    ' color:#FFFFFF; border:0; border-radius:6px; padding:0 10px; font-size:11px; font-weight:500;}'
                    ' QPushButton:hover {padding:0 10px;}')
            else:
                chip.setStyleSheet(
                    'QPushButton {background:rgba(255,255,255,0.06); color:#9CA3AF; border:1px solid rgba(255,255,255,0.10);'
                    ' border-radius:6px; padding:0 10px; font-size:11px;}'
                    ' QPushButton:hover {color:#F4F5F7; background:rgba(255,255,255,0.10);}')


class WorkspaceLog(LogDrawer):
    view_changed = pyqtSignal()
    COLORS = {'debug': '#AE9CD6', 'info': '#8FB4EE', 'success': '#7CC79A', 'warning': '#E0C16B', 'error': '#E39A9A'}
    DISPLAY_LIMIT = 200

    def _build_ui(self):
        self.setObjectName('workspaceLog')
        self.setAttribute(Qt.WA_StyledBackground)
        self.setStyleSheet('#workspaceLog {background:rgba(15,16,26,0.55); border:1px solid rgba(255,255,255,0.10); border-top-left-radius:14px; border-top-right-radius:14px;}')
        root = QVBoxLayout(self); root.setContentsMargins(16, 8, 16, 8); root.setSpacing(6)
        header = QHBoxLayout(); self.header = header
        header.addWidget(label('执行日志', 13, '#F4F5F7', True))
        self.count_label = label('本次运行 0 条', 11, '#7A8294', mono=True)
        header.addWidget(self.count_label)
        header.addStretch(1)
        self.search_label = label(''); header.addWidget(self.search_label)
        self.filter_box = FilterTabs()
        self.filter_box.currentTextChanged.connect(self._render)
        header.addWidget(self.filter_box)
        header.addSpacing(12)
        self.export_button = style_button(PushButton('导出')); self.export_button.clicked.connect(self.export)
        self.clear_button = style_button(PushButton('清空')); self.clear_button.clicked.connect(self.clear)
        self.toggle_button = style_button(TransparentToolButton(FIF.UP)); self.toggle_button.setFixedSize(28, 24)
        self.toggle_button.setToolTip('折叠 / 展开日志'); self.toggle_button.clicked.connect(self.toggle)
        for button in (self.clear_button, self.export_button):
            button.setFixedHeight(24); header.addWidget(button)
        header.addWidget(self.toggle_button); root.addLayout(header)
        self.browser = TextBrowser(); self.browser.setProperty('studioStyled', True)
        self.browser.setOpenExternalLinks(False); self.browser.setMinimumHeight(0)
        self.browser.document().setDefaultStyleSheet('div.logline {font-family:"Cascadia Mono","Consolas"; font-size:11px; line-height:160%; margin:1px 0;}')
        self.browser.setStyleSheet('TextBrowser {background:transparent; border:0; color:#E5E7EB; font-family:"Cascadia Mono","Consolas"; font-size:11px;}')
        root.addWidget(self.browser, 1)
        self.highlight = LogHighlight(self.browser.viewport())
        self._animation = QPropertyAnimation(self, b'maximumHeight', self); self._animation.setDuration(180)
        self._animation.finished.connect(self._toggle_finished)
        self._saved_height = 140

    def _update_count(self):
        self.count_label.setText(f'本次运行 {len(self.entries)} 条')

    def append_log(self, message, level='info'):
        super().append_log(message, level)
        self._update_count()

    def clear(self):
        super().clear()
        self._update_count()

    def _selected_level(self):
        value = self.filter_box.currentText()
        return None if value == '全部' else value.lower()

    def set_debug_mode(self, enabled):
        self.debug_mode = bool(enabled)
        self.filter_box.set_debug_visible(self.debug_mode)
        if not enabled and self.filter_box.currentText() == 'DEBUG':
            self.filter_box.setCurrentText('全部')
        self._render()

    def _append_entry(self, timestamp, message, level):
        color = self.COLORS.get(level, self.COLORS['info'])
        text = str(message)
        # 超长行（如长路径）仅截断展示，完整内容仍保留在导出中，避免撑乱面板行距。
        if len(text) > self.DISPLAY_LIMIT:
            text = text[:self.DISPLAY_LIMIT - 1] + '…'
        self.browser.append(
            f'<div class="logline"><span style="color:#6B7280">{escape(timestamp)}</span>'
            f' &nbsp;<span style="color:{color}">[{escape(level.upper())}]</span>'
            f' &nbsp;<span style="color:#E5E7EB">{escape(text)}</span></div>')
        self.browser.moveCursor(QTextCursor.End)
        if self.isVisible() and self._expanded:
            rect = self.browser.cursorRect(); rect.setLeft(0); rect.setWidth(self.browser.viewport().width()); rect.setHeight(18)
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
