"""Fluent model selectors keep API identifiers separate from display metadata."""
from PyQt5.QtCore import Qt, QSignalBlocker, QSize
from PyQt5.QtWidgets import QSizePolicy
from qfluentwidgets import ComboBox
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu
from core.model_catalog import builtin_models, resolve_model_id
from core.i18n import tr


def catalog_snapshot(config_manager):
    controller = getattr(config_manager, 'model_catalog_controller', None)
    return controller.snapshot() if controller is not None else builtin_models()


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
