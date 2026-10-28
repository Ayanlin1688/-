"""分析过程卡片：折叠思考内容与排序后的生成提示词预览。"""
from collections import deque

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QSizePolicy, QToolButton,
    QVBoxLayout, QWidget,
)
from PyQt5.QtCore import pyqtSignal
from qfluentwidgets import PushButton

from core.i18n import register_retranslate, tr
from .. import materials
from ..tokens import px

MAX_REASONING_CHUNKS = 200
MAX_REASONING_TEXT = 60000
MAX_PROMPT_LENGTH = 8000
MAX_PROMPT_TEXT = 60000
MAX_RAW_OUTPUT = 60000


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

    retry_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('ecommerceAnalysisView')
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumWidth(0)
        self._reasoning_chunks = deque(maxlen=MAX_REASONING_CHUNKS)
        self._prompt_items = []
        self._reasoning_expanded = True
        self._prompt_expanded = False
        self._raw_expanded = False
        self._analysis_state = 'idle'
        self._analysis_elapsed = 0
        self._analysis_model = ''
        self._analysis_error = ''
        self._raw_output = ''
        self._prompt_full_expanded = False
        self._layout = QVBoxLayout(self)

        self.status_card = QFrame(self)
        self.status_card.setObjectName('ecommerceAnalysisStatusCard')
        self.status_layout = QVBoxLayout(self.status_card)
        self.status_header = QHBoxLayout()
        self.status_dot = QLabel(self.status_card)
        self.status_dot.setObjectName('ecommerceAnalysisStatusDot')
        self.status_label = _label(self.status_card, bold=True)
        self.retry_button = PushButton(self.status_card)
        self.retry_button.setObjectName('ecommerceAnalysisRetry')
        self.status_header.addWidget(self.status_dot)
        self.status_header.addWidget(self.status_label, 1)
        self.status_header.addWidget(self.retry_button)
        self.status_layout.addLayout(self.status_header)
        self.status_detail = _label(self.status_card)
        self.status_detail.setWordWrap(True)
        self.status_layout.addWidget(self.status_detail)
        self.status_error = _label(self.status_card)
        self.status_error.setWordWrap(True)
        self.status_error.setObjectName('analysisStatusError')
        self.status_layout.addWidget(self.status_error)
        self.inactivity_warning = _label(self.status_card)
        self.inactivity_warning.setWordWrap(True)
        self.inactivity_warning.setObjectName('analysisInactivityWarning')
        self.inactivity_warning.hide()
        self.status_layout.addWidget(self.inactivity_warning)
        self.slow_warning = _label(self.status_card)
        self.slow_warning.setWordWrap(True)
        self.slow_warning.setObjectName('analysisSlowWarning')
        self.slow_warning.hide()
        self.status_layout.addWidget(self.slow_warning)
        self._layout.addWidget(self.status_card)

        self.reasoning_title, self.reasoning_toggle, self.reasoning_header = self._section_header()
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

        self.prompt_title, self.prompt_toggle, self.prompt_header = self._section_header()
        self.prompt_expand_button = QToolButton(self)
        self.prompt_expand_button.setAutoRaise(True)
        self.prompt_expand_button.setCursor(Qt.PointingHandCursor)
        self.prompt_header.insertWidget(1, self.prompt_expand_button)
        self.prompt_edit = self._make_edit('ecommercePromptPreview', self)
        self._layout.addWidget(self.prompt_edit)
        self.raw_title, self.raw_toggle, self.raw_header = self._section_header()
        self.raw_edit = self._make_edit('ecommerceRawAnalysis', self)
        self._layout.addWidget(self.raw_edit)
        self.reasoning_toggle.clicked.connect(self._toggle_reasoning)
        self.prompt_toggle.clicked.connect(self._toggle_prompt)
        self.prompt_expand_button.clicked.connect(self._toggle_prompt_full)
        self.raw_toggle.clicked.connect(self._toggle_raw)
        self.retry_button.clicked.connect(self.retry_requested.emit)
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
        return title, toggle, row

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
    def is_raw_expanded(self):
        return self._raw_expanded

    @property
    def is_reasoning_expanded(self):
        return self._reasoning_expanded

    def _toggle_reasoning(self):
        self._reasoning_expanded = not self._reasoning_expanded
        self._apply_expansion()

    def _toggle_prompt(self):
        self._prompt_expanded = not self._prompt_expanded
        self._apply_expansion()

    def _toggle_prompt_full(self):
        self._prompt_full_expanded = not self._prompt_full_expanded
        self._render_prompt_items()

    def _toggle_raw(self):
        self._raw_expanded = not self._raw_expanded
        self._apply_expansion()
        if self._raw_expanded and self.raw_edit.toPlainText() != self._raw_output:
            self.raw_edit.setPlainText(self._raw_output)

    def _apply_expansion(self):
        self.reasoning_body.setVisible(self._reasoning_expanded)
        self.prompt_edit.setVisible(self._prompt_expanded)
        self.raw_edit.setVisible(self._raw_expanded)
        for toggle, expanded in ((self.reasoning_toggle, self._reasoning_expanded),
                                 (self.prompt_toggle, self._prompt_expanded),
                                 (self.raw_toggle, self._raw_expanded)):
            toggle.setText('⌃' if expanded else '⌄')
            toggle.setToolTip(tr('收起') if expanded else tr('展开'))
            toggle.setAccessibleName(toggle.toolTip())
        self.updateGeometry()

    def set_analysis_state(self, state, elapsed_s=0, model='', error=''):
        state = str(state or 'idle')
        if state not in ('idle', 'analyzing', 'running', 'finished', 'failed', 'cancelled'):
            state = 'idle'
        self._analysis_state = state
        self._analysis_elapsed = max(0, int(elapsed_s or 0))
        if model:
            self._analysis_model = str(model)
        self._analysis_error = str(error or '')
        self._render_analysis_state()

    def set_analysis_elapsed(self, seconds):
        self._analysis_elapsed = max(0, int(seconds or 0))
        self._render_analysis_state()
        if not self._reasoning_chunks:
            self.reasoning_placeholder.setText(
                tr('正在思考… 已用时 {seconds} 秒').format(seconds=self._analysis_elapsed)
                if self._analysis_state in ('analyzing', 'running')
                else tr('等待模型返回思考内容…'))

    def set_analysis_warnings(self, *, inactive=False, slow=False):
        self.inactivity_warning.setText(tr('模型响应较慢，可取消后重试'))
        self.inactivity_warning.setVisible(bool(inactive))
        self.slow_warning.setText(tr('分析较慢，可取消/切换模型'))
        self.slow_warning.setVisible(bool(slow))
        self.inactivity_warning.setStyleSheet(
            'background: transparent; color: %s;' % materials.ecommerce_color('analysis_failed'))
        self.slow_warning.setStyleSheet(
            'background: transparent; color: %s;' % materials.palette()['warning'])

    def _render_analysis_state(self):
        state = self._analysis_state
        if state in ('analyzing', 'running'):
            dots = '…' * (self._analysis_elapsed % 3 + 1)
            title = tr('思考中') + dots
            color_key = 'analysis_active'
        elif state == 'finished':
            title = tr('分析完成 · 用时 {seconds} 秒').format(seconds=self._analysis_elapsed)
            color_key = 'analysis_finished'
        elif state == 'failed':
            title = tr('分析失败')
            color_key = 'analysis_failed'
        elif state == 'cancelled':
            title = tr('分析已取消')
            color_key = 'analysis_idle'
        else:
            title = tr('思考过程')
            color_key = 'analysis_idle'
        self.status_label.setText(title)
        model = self._analysis_model or tr('未选择')
        self.status_detail.setText(tr('模型：{model} · 已用时 {seconds} 秒').format(
            model=model, seconds=self._analysis_elapsed))
        self.status_detail.setVisible(state not in ('idle',))
        self.status_error.setText(tr('错误：{error}').format(error=self._analysis_error))
        self.status_error.setVisible(state == 'failed' and bool(self._analysis_error))
        self.retry_button.setText(tr('重试分析'))
        self.retry_button.setVisible(state == 'failed')
        self.status_detail.setToolTip(self._analysis_error if state == 'failed' else '')
        self.status_label.setToolTip(self._analysis_error if state == 'failed' else '')
        colors = materials.ecommerce_color(color_key)
        border = materials.ecommerce_color(color_key + '_border')
        self.status_card.setStyleSheet(
            'QFrame#ecommerceAnalysisStatusCard {background: %s; border: 1px solid %s; border-radius: %dpx;}'
            % (materials.ecommerce_color(color_key + '_bg'), border, px(9)))
        self.status_dot.setStyleSheet('background: %s; border-radius: %dpx;' % (colors, px(5)))
        self.status_label.setStyleSheet('background: transparent; color: %s;' % colors)
        self.status_detail.setStyleSheet('background: transparent; color: %s;' % materials.text_color(2))
        self.status_error.setStyleSheet(
            'background: transparent; color: %s;' % materials.ecommerce_color('analysis_failed'))
        self.retry_button.setStyleSheet(
            'QPushButton {background: %s; color: %s; border: 1px solid %s; border-radius: %dpx; padding: 0 %dpx;}'
            % (materials.ecommerce_color('analysis_retry_bg'), materials.text_color(1), border,
               px(7), px(10)))

    def set_raw_output(self, text):
        self._raw_output = str(text or '')[-MAX_RAW_OUTPUT:]
        if self._raw_expanded and self.raw_edit.toPlainText() != self._raw_output:
            self.raw_edit.setPlainText(self._raw_output)

    def append_raw_output(self, text):
        if text:
            self.set_raw_output(self._raw_output + str(text))

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
        self.reasoning_placeholder.setText(
            tr('正在思考… 已用时 {seconds} 秒').format(seconds=self._analysis_elapsed)
            if self._analysis_state in ('analyzing', 'running') else tr('等待模型返回思考内容…'))
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
        full_sections = []
        truncated = False
        for item in sorted(self._prompt_items, key=order):
            template = tr('轮{seq} · {title}') if item.get('kind') == 'carousel' else tr('详{seq} · {title}')
            title = template.format(seq=item.get('seq', ''), title=str(item.get('title') or '')[:200])
            full_prompt = str(item.get('prompt') or '')
            full_sections.append(title + '\n' + full_prompt)
            prompt = full_prompt
            if not self._prompt_full_expanded and len(prompt) > MAX_PROMPT_LENGTH:
                prompt = prompt[:MAX_PROMPT_LENGTH] + '…'
                truncated = True
            sections.append(title + '\n' + prompt)
        text = '\n\n'.join(full_sections if self._prompt_full_expanded else sections)
        if len(text) > MAX_PROMPT_TEXT:
            text = text[:MAX_PROMPT_TEXT] + '…'
            truncated = True
        text = text or tr('分析完成后显示生成提示词')
        if self.prompt_edit.toPlainText() != text:
            self.prompt_edit.setPlainText(text)
        if self.prompt_edit.property('hasPromptItems') != bool(sections):
            self.prompt_edit.setProperty('hasPromptItems', bool(sections))
        self.prompt_expand_button.setVisible(bool(sections) and truncated)
        self.prompt_expand_button.setText(
            tr('收起完整提示词') if self._prompt_full_expanded else tr('展开完整提示词'))
        self.prompt_expand_button.setToolTip(self.prompt_expand_button.text())

    def clear(self):
        self._reasoning_chunks.clear()
        self._prompt_items.clear()
        self._reasoning_expanded, self._prompt_expanded, self._raw_expanded = True, False, False
        self._prompt_full_expanded = False
        self._analysis_state, self._analysis_elapsed = 'idle', 0
        self._analysis_model, self._analysis_error = '', ''
        self._raw_output = ''
        self.set_analysis_warnings()
        self.raw_edit.clear()
        self._render_reasoning()
        self.retranslate_ui()

    def retranslate_ui(self):
        self.reasoning_title.setText(tr('思考过程'))
        self.prompt_title.setText(tr('提示词'))
        self.raw_title.setText(tr('查看原始输出'))
        self.status_label.setText(tr('思考过程'))
        self.retry_button.setText(tr('重试分析'))
        self.prompt_expand_button.setText(tr('展开完整提示词'))
        self.reasoning_placeholder.setText(tr('等待模型返回思考内容…'))
        self.reasoning_edit.setPlaceholderText(tr('等待模型返回思考内容…'))
        self.raw_edit.setPlaceholderText(tr('等待模型返回思考内容…'))
        self._render_prompt_items()
        self._apply_expansion()
        self._render_analysis_state()

    def refresh_interface_scale(self):
        self._layout.setContentsMargins(px(20), px(20), px(20), px(20))
        self._layout.setSpacing(px(12))
        self._reasoning_layout.setSpacing(0)
        self.status_layout.setContentsMargins(px(12), px(10), px(12), px(10))
        self.status_layout.setSpacing(px(5))
        self.status_header.setSpacing(px(8))
        self.status_dot.setFixedSize(px(10), px(10))
        self.reasoning_placeholder.setMinimumHeight(px(90))
        self.reasoning_edit.setMaximumHeight(px(180))
        self.reasoning_edit.setMinimumHeight(px(90))
        self.prompt_edit.setMaximumHeight(px(220))
        self.prompt_edit.setMinimumHeight(px(120))
        self.raw_edit.setMaximumHeight(px(180))
        self.raw_edit.setMinimumHeight(px(90))
        for widget in (self.reasoning_title, self.prompt_title, self.reasoning_placeholder,
                       self.reasoning_edit, self.prompt_edit, self.raw_title, self.raw_edit,
                       self.status_label, self.status_detail, self.status_error,
                       self.inactivity_warning,
                       self.slow_warning):
            font = widget.font()
            font.setFamily(materials.ui_font_family())
            font.setPixelSize(px(13 if widget in (self.reasoning_title, self.prompt_title,
                                                  self.raw_title, self.status_label) else 12))
            widget.setFont(font)
        self.retry_button.setFixedHeight(px(30))
        for toggle in (self.reasoning_toggle, self.prompt_toggle, self.raw_toggle):
            toggle.setFixedSize(px(28), px(28))
        self.prompt_expand_button.setMinimumHeight(px(28))
        self.prompt_expand_button.setMaximumWidth(px(230))
        self.refresh_colors()

    def refresh_colors(self):
        self.setStyleSheet(
            'QFrame#ecommerceAnalysisView {background: %s; border: 1px solid %s; border-radius: %dpx;}'
            % (materials.ecommerce_color('single_card'),
               materials.ecommerce_color('single_card_border'), px(12)))
        for label in (self.reasoning_title, self.prompt_title, self.raw_title):
            label.setStyleSheet('background: transparent; color: %s;' % materials.text_color(1))
        self.reasoning_placeholder.setStyleSheet('background: transparent; color: %s;' % materials.text_color(3))
        for edit in (self.reasoning_edit, self.prompt_edit, self.raw_edit):
            edit.setStyleSheet('QPlainTextEdit {background: transparent; border: none; color: %s;}'
                              % materials.text_color(2))
        for toggle in (self.reasoning_toggle, self.prompt_toggle, self.raw_toggle):
            toggle.setStyleSheet('QToolButton {background: transparent; border: none; color: %s;}'
                                % materials.text_color(2))
        self.prompt_expand_button.setStyleSheet(
            'QToolButton {background: transparent; border: none; color: %s;}' % materials.text_color(2))
        self._render_analysis_state()
