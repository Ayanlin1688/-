"""Fluent model selectors keep API identifiers separate from display metadata."""
import re

from PyQt5.QtCore import Qt, QSignalBlocker, QSize
from PyQt5.QtWidgets import QAction, QSizePolicy
from qfluentwidgets import ComboBox, EditableComboBox
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu
from core.model_catalog import builtin_models, classify_model, normalize_model_id, resolve_model_id
from core.i18n import tr


def catalog_snapshot(config_manager, capability=None):
    controller = getattr(config_manager, 'model_catalog_controller', None)
    if controller is None:
        return builtin_models()
    if capability is not None:
        return controller.snapshot_for(capability)
    return controller.snapshot()


def usable(record):
    return bool(record.get('available', True) and record.get('kind') == 'video'
                and record.get('protocol_known', False))


def model_label(record):
    parts = [record['id'], ' / '.join(record.get('resolutions', ())), record.get('pricing_text') or tr('计费未提供')]
    if record.get('kind') != 'video':
        parts.append(tr('非视频模型'))
    elif not record.get('protocol_known'):
        parts.append(tr('协议待确认'))
    return ' · '.join(part for part in parts if part)


def compact_model_label(record):
    """收起状态下只显示「模型 · 主分辨率」，完整信息留在下拉菜单与悬停提示里。"""
    resolutions = record.get('resolutions') or ()
    primary = resolutions[0] if resolutions else ''
    label = f"{record['id']} · {primary}" if primary else str(record['id'])
    if record.get('kind') != 'video':
        label += ' · ' + tr('非视频')
    elif not record.get('protocol_known'):
        label += ' · ' + tr('协议待确认')
    return label


class _ModelMenu(ComboBoxMenu):
    def addAction(self, action):
        owner = self.parent()
        description = owner.descriptions.get(action.text(), '')
        action.setToolTip(description)
        super().addAction(action)
        self.view.item(self.view.count() - 1).setToolTip(description)


class ModelComboBox(ComboBox):
    def __init__(self, parent=None):
        self.descriptions = {}
        self._display_text = ''
        self._compact = {}
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setMaxVisibleItems(12)
        self.currentIndexChanged.connect(self._update_tooltip)

    def sizeHint(self):
        return QSize(220, 33)

    def minimumSizeHint(self):
        return QSize(80, 33)

    def currentText(self):
        return self.currentData() or ''

    def setCurrentText(self, text):
        index = self.findData(text)
        if index < 0:
            index = self.findText(text)
        if index >= 0:
            self.setCurrentIndex(index)

    def setText(self, text):
        self._display_text = text
        compact = self._compact.get(text, text)
        super().setText(self.fontMetrics().elidedText(compact, Qt.ElideRight, max(30, self.width()-42)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setText(self._display_text)

    def _createComboMenu(self):
        return _ModelMenu(self)

    def _update_tooltip(self, *_):
        text = self.itemText(self.currentIndex())
        self.setToolTip(text + '\n' + self.descriptions.get(text, ''))

    def set_models(self, records, selected=None, automatic=False, keep_missing=False):
        selected = self.currentText() if selected is None else selected
        selected = resolve_model_id(selected, records)
        blocker = QSignalBlocker(self)
        self._closeComboMenu()
        self.clear()
        self.descriptions = {}
        self._compact = {}
        if automatic:
            self.addItem(tr('自动识别 / 工作台默认'), userData='')
        for model, record in records.items():
            label = model_label(record)
            self._compact[label] = compact_model_label(record)
            self.descriptions[label] = record.get('description') or record.get('display_name') or model
            self.addItem(label, userData=model)
            self.setItemEnabled(self.count()-1, usable(record))
        if selected and selected not in records and keep_missing:
            self.addItem(selected + ' · ' + tr('已下架'), userData=selected)
            self.setItemEnabled(self.count()-1, False)
        index = self.findData(selected)
        if index < 0 or (not keep_missing and not self.items[index].isEnabled):
            index = next((i for i, item in enumerate(self.items) if item.isEnabled), -1)
        self.setCurrentIndex(index)
        if index < 0:
            self.setText(tr('暂无可提交模型'))
        del blocker
        self._update_tooltip()


_PROVIDER_PATTERNS = (
    ('GPT', re.compile(r'^gpt(?:[-_]|$)', re.I)),
    ('CLAUDE', re.compile(r'^claude(?:[-_]|$)', re.I)),
    ('GEMINI', re.compile(r'^gemini(?:[-_]|$)', re.I)),
    ('DEEPSEEK', re.compile(r'^deepseek(?:[-_]|$)', re.I)),
    ('GROK', re.compile(r'^grok(?:[-_]|$)', re.I)),
    ('QWEN', re.compile(r'^qwen(?:[-_]|$)', re.I)),
)
_PROVIDER_ORDER = ('GPT', 'CLAUDE', 'GEMINI', 'DEEPSEEK', 'GROK', 'QWEN', 'OTHER')


class _ModelPickMenu(ComboBoxMenu):
    def __init__(self, parent):
        super().__init__(parent)
        self.tooltips = parent._item_tooltips

    def addAction(self, action):
        if isinstance(action, QAction):
            tooltip = self.tooltips.get(action.text())
            if tooltip:
                action.setToolTip(tooltip)
        return super().addAction(action)


def _provider_group(model_id):
    """Group model IDs using the same family tokens recognized by the catalog."""
    value = re.sub(r'-pack$', '', str(model_id or '').strip(), flags=re.I)
    if classify_model(value) not in {'chat', 'image'}:
        return 'OTHER'
    for provider, pattern in _PROVIDER_PATTERNS:
        if pattern.search(value):
            return provider
    if re.match(r'^nano[\s_-]+banana(?:[\s_-]|$)', value, re.I):
        return 'GEMINI'
    return 'OTHER'


class ModelPickComboBox(EditableComboBox):
    """Editable model picker for chat and image models, including custom IDs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setMaxVisibleItems(16)
        self._kind = 'chat'
        self._item_tooltips = {}
        self._full_value = ''
        self._rendered_value = ''
        self._rendering_value = False
        self.textChanged.connect(self._edited_value_changed)

    def sizeHint(self):
        return QSize(260, 33)

    def minimumSizeHint(self):
        return QSize(140, 33)

    def _createComboMenu(self):
        return _ModelPickMenu(self)

    @staticmethod
    def _model_id(value):
        value = normalize_model_id(value)
        return '' if value.startswith('__separator__:') else value

    def value(self):
        text = self.text().strip()
        if self._rendered_value and text == self._rendered_value:
            return self._model_id(self._full_value)
        index = self.findText(text)
        if index >= 0:
            data = self.itemData(index)
            if data is not None and str(data).startswith('__separator__:'):
                return self._model_id(self._full_value)
            if data is not None:
                return self._model_id(data)
        return self._model_id(text)

    def _onComboTextChanged(self, text):
        """Keep EditableComboBox's signal contract in terms of wire IDs."""
        text = str(text or '')
        if self._rendering_value:
            self.currentTextChanged.emit(self._full_value.strip())
            return
        index = self.findText(text)
        if index >= 0:
            data = self.itemData(index)
            if data is not None and str(data).startswith('__separator__:'):
                self._currentIndex = -1
                self.currentTextChanged.emit(self._full_value.strip())
                return
            if data is not None and not str(data).startswith('__separator__:'):
                value = self._model_id(data)
                self._full_value = value
                self._rendered_value = ''
                self._currentIndex = index
                self.setToolTip(value)
                self.currentTextChanged.emit(value)
                self.currentIndexChanged.emit(index)
                return
        value = self._model_id(text)
        self._full_value = value
        self._rendered_value = ''
        self._currentIndex = -1
        self.setToolTip(value)
        self.currentTextChanged.emit(value)

    def _onItemClicked(self, index):
        """Emit activation with the wire ID instead of the rendered label."""
        data = self.itemData(index)
        if data is not None and str(data).startswith('__separator__:'):
            return
        if index != self.currentIndex():
            self.setCurrentIndex(index)
        value = self._model_id(data if data is not None else self.itemText(index))
        self.set_value(value)
        self.activated.emit(index)
        self.textActivated.emit(value)

    def set_value(self, text):
        value = self._model_id(text)
        index = self.findData(value)
        if index >= 0 and str(self.itemData(index) or '').startswith('__separator__:'):
            index = -1
        self._full_value = value
        self._rendering_value = True
        self.setCurrentIndex(index)
        self.setText(value)
        self._rendering_value = False
        self._render_value()

    def _edited_value_changed(self, text):
        # A persistence callback may already have rendered the canonical ID
        # while the earlier label's textChanged signal is still being delivered.
        if self._rendering_value or str(text or '') != self.text():
            return
        index = self.findText(str(text or ''))
        data = self.itemData(index) if index >= 0 else None
        if data is not None and str(data).startswith('__separator__:'):
            return
        if data is not None and not str(data).startswith('__separator__:'):
            self._full_value = self._model_id(data)
        else:
            self._full_value = self._model_id(text)
        self._rendered_value = ''
        self.setToolTip(self._full_value)

    def _render_value(self):
        value = self._full_value
        if not value:
            self._rendered_value = ''
            self.setToolTip('')
            return
        if self.hasFocus():
            self.setToolTip(value)
            return
        display = self.fontMetrics().elidedText(value, Qt.ElideRight, max(30, self.width() - 42))
        self._rendered_value = display
        self._rendering_value = True
        self.setText(display)
        self._rendering_value = False
        self.setToolTip(value)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render_value()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self._rendered_value = ''
        self._rendering_value = True
        self.setText(self._full_value)
        self._rendering_value = False

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._render_value()

    def set_models(self, records, selected=None, kind='chat'):
        self._kind = kind
        records = records if isinstance(records, dict) else {}
        selected = normalize_model_id(
            self.value() if selected is None else selected,
            records.keys())
        grouped = {provider: [] for provider in _PROVIDER_ORDER}
        for model_id, record in records.items():
            if not isinstance(record, dict):
                continue
            if record.get('kind') != kind or not record.get('available', True):
                continue
            provider = _provider_group(model_id)
            grouped[provider].append((model_id, record))

        blocker = QSignalBlocker(self)
        self.clear()
        self._item_tooltips = {}
        for provider in _PROVIDER_ORDER:
            rows = grouped[provider]
            if not rows:
                continue
            label_text = tr('其他') if provider == 'OTHER' else provider
            self.addItem(f'── {label_text} ──', userData=f'__separator__:{provider}')
            separator = self.count() - 1
            self.setItemEnabled(separator, False)
            for model_id, record in rows:
                display = str(record.get('display_name') or record.get('name') or model_id)
                label_text = f'{model_id} · {display}' if display != model_id else model_id
                self.addItem(label_text, userData=model_id)
                description = str(record.get('description') or display)
                self._item_tooltips[label_text] = description

        # A stored/custom ID remains editable even when it is not in the catalog.
        self.set_value(selected)
        del blocker
