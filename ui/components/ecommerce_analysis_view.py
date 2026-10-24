"""分析过程卡片：折叠思考内容与排序后的生成提示词预览。"""
from collections import deque

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QSizePolicy, QToolButton,
    QVBoxLayout, QWidget,
)

from core.i18n import register_retranslate, tr
from .. import materials
from ..tokens import px

MAX_REASONING_CHUNKS = 200
MAX_REASONING_TEXT = 60000
MAX_PROMPT_LENGTH = 8000
MAX_PROMPT_TEXT = 60000


def _label(parent, bold=False):
    label = QLabel(parent)
    label.setTextFormat(Qt.PlainText)
    font = label.font()
    font.setFamily(materials.ui_font_family())
    font.setBold(bold)
    label.setFont(font)
    return label


class EcommerceAnalysisView(QFrame):
    """只在 GUI 线程接收已经收集好的文本，不触发任何模型调用。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('ecommerceAnalysisView')
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumWidth(0)
        self._reasoning_chunks = deque(maxlen=MAX_REASONING_CHUNKS)
        self._prompt_items = []
        self._reasoning_expanded = True
        self._prompt_expanded = False
        self._layout = QVBoxLayout(self)

        self.reasoning_title, self.reasoning_toggle = self._section_header()
        self.reasoning_body = QWidget(self)
        self._reasoning_layout = QVBoxLayout(self.reasoning_body)
        self._reasoning_layout.setContentsMargins(0, 0, 0, 0)
        self.reasoning_placeholder = _label(self.reasoning_body)
        self.reasoning_placeholder.setWordWrap(True)
        self.reasoning_placeholder.setAlignment(Qt.AlignCenter)
        self.reasoning_edit = self._make_edit('ecommerceReasoning', self.reasoning_body)
        self._reasoning_layout.addWidget(self.reasoning_placeholder)
        self._reasoning_layout.addWidget(self.reasoning_edit)
        self._layout.addWidget(self.reasoning_body)

        self.prompt_title, self.prompt_toggle = self._section_header()
        self.prompt_edit = self._make_edit('ecommercePromptPreview', self)
        self._layout.addWidget(self.prompt_edit)
        self.reasoning_toggle.clicked.connect(self._toggle_reasoning)
        self.prompt_toggle.clicked.connect(self._toggle_prompt)
        register_retranslate(self)
        materials.register_theme_callback(self.refresh_colors)
        self.refresh_interface_scale()
        self.clear()

    def _section_header(self):
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        title = _label(self, bold=True)
        toggle = QToolButton(self)
        toggle.setAutoRaise(True)
        toggle.setCursor(Qt.PointingHandCursor)
        row.addWidget(title, 1)
        row.addWidget(toggle)
        self._layout.addLayout(row)
        return title, toggle

    def _make_edit(self, name, parent):
        edit = QPlainTextEdit(parent)
        edit.setObjectName(name)
        edit.setReadOnly(True)
        edit.setFrameShape(QFrame.NoFrame)
        edit.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        edit.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        return edit

    @property
    def is_prompt_expanded(self):
        return self._prompt_expanded

    @property
    def is_reasoning_expanded(self):
        return self._reasoning_expanded

    def _toggle_reasoning(self):
        self._reasoning_expanded = not self._reasoning_expanded
        self._apply_expansion()

    def _toggle_prompt(self):
        self._prompt_expanded = not self._prompt_expanded
        self._apply_expansion()

    def _apply_expansion(self):
        self.reasoning_body.setVisible(self._reasoning_expanded)
        self.prompt_edit.setVisible(self._prompt_expanded)
        for toggle, expanded in ((self.reasoning_toggle, self._reasoning_expanded),
                                 (self.prompt_toggle, self._prompt_expanded)):
            toggle.setText('⌃' if expanded else '⌄')
            toggle.setToolTip(tr('收起') if expanded else tr('展开'))
            toggle.setAccessibleName(toggle.toolTip())
        self.updateGeometry()

    def set_reasoning_chunks(self, chunks):
        self._reasoning_chunks.clear()
        for chunk in chunks or ():
            if chunk is not None and str(chunk):
                self._reasoning_chunks.append(str(chunk))
        self._render_reasoning()

    def append_reasoning(self, text):
        if text is None or not str(text):
            return
        self._reasoning_chunks.append(str(text))
        self._render_reasoning()

    def _render_reasoning(self):
        text = '\n'.join(self._reasoning_chunks)[-MAX_REASONING_TEXT:]
        self.reasoning_edit.setPlainText(text)
        self.reasoning_edit.setVisible(bool(text))
        self.reasoning_placeholder.setVisible(not bool(text))
        self._scroll_reasoning_to_bottom()
        QTimer.singleShot(0, self._scroll_reasoning_to_bottom)

    def _scroll_reasoning_to_bottom(self):
        bar = self.reasoning_edit.verticalScrollBar()
        bar.setValue(bar.maximum())

    def set_prompt_items(self, items):
        self._prompt_items = [dict(item) for item in items or () if isinstance(item, dict)]
        self._render_prompt_items()

    def _render_prompt_items(self):
        def order(item):
            try:
                seq = int(item.get('seq', 0))
            except (TypeError, ValueError):
                seq = 0
            return (0 if item.get('kind') == 'carousel' else 1, seq)

        sections = []
        for item in sorted(self._prompt_items, key=order):
            template = tr('轮{seq} · {title}') if item.get('kind') == 'carousel' else tr('详{seq} · {title}')
            title = template.format(seq=item.get('seq', ''), title=str(item.get('title') or '')[:200])
            prompt = str(item.get('prompt') or '')
            if len(prompt) > MAX_PROMPT_LENGTH:
                prompt = prompt[:MAX_PROMPT_LENGTH] + '…'
            sections.append(title + '\n' + prompt)
        text = '\n\n'.join(sections)
        if len(text) > MAX_PROMPT_TEXT:
            text = text[:MAX_PROMPT_TEXT] + '…'
        text = text or tr('分析完成后显示生成提示词')
        if self.prompt_edit.toPlainText() != text:
            self.prompt_edit.setPlainText(text)
        if self.prompt_edit.property('hasPromptItems') != bool(sections):
            self.prompt_edit.setProperty('hasPromptItems', bool(sections))

    def clear(self):
        self._reasoning_chunks.clear()
        self._prompt_items.clear()
        self._reasoning_expanded, self._prompt_expanded = True, False
        self._render_reasoning()
        self.retranslate_ui()

    def retranslate_ui(self):
        self.reasoning_title.setText(tr('思考过程'))
        self.prompt_title.setText(tr('提示词'))
        self.reasoning_placeholder.setText(tr('等待模型返回思考内容…'))
        self.reasoning_edit.setPlaceholderText(tr('等待模型返回思考内容…'))
        self._render_prompt_items()
        self._apply_expansion()

    def refresh_interface_scale(self):
        self._layout.setContentsMargins(px(20), px(20), px(20), px(20))
        self._layout.setSpacing(px(12))
        self._reasoning_layout.setSpacing(0)
        self.reasoning_placeholder.setMinimumHeight(px(90))
        self.reasoning_edit.setMaximumHeight(px(180))
        self.reasoning_edit.setMinimumHeight(px(90))
        self.prompt_edit.setMaximumHeight(px(220))
        self.prompt_edit.setMinimumHeight(px(120))
        for widget in (self.reasoning_title, self.prompt_title, self.reasoning_placeholder,
                       self.reasoning_edit, self.prompt_edit):
            font = widget.font()
            font.setFamily(materials.ui_font_family())
            font.setPixelSize(px(13 if widget in (self.reasoning_title, self.prompt_title) else 12))
            widget.setFont(font)
        for toggle in (self.reasoning_toggle, self.prompt_toggle):
            toggle.setFixedSize(px(28), px(28))
        self.refresh_colors()

    def refresh_colors(self):
        self.setStyleSheet(
            'QFrame#ecommerceAnalysisView {background: %s; border: 1px solid %s; border-radius: %dpx;}'
            % (materials.ecommerce_color('single_card'),
               materials.ecommerce_color('single_card_border'), px(12)))
        for label in (self.reasoning_title, self.prompt_title):
            label.setStyleSheet('background: transparent; color: %s;' % materials.text_color(1))
        self.reasoning_placeholder.setStyleSheet('background: transparent; color: %s;' % materials.text_color(3))
        for edit in (self.reasoning_edit, self.prompt_edit):
            edit.setStyleSheet('QPlainTextEdit {background: transparent; border: none; color: %s;}'
                              % materials.text_color(2))
        for toggle in (self.reasoning_toggle, self.prompt_toggle):
            toggle.setStyleSheet('QToolButton {background: transparent; border: none; color: %s;}'
                                % materials.text_color(2))
