"""Immediate, explicitly confirmed history deletion dialogs."""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QHBoxLayout
from qfluentwidgets import CaptionLabel, CheckBox, PushButton, RadioButton

from core.i18n import tr
from .studio_dialog import StudioDialog


class DeleteHistoryDialog(StudioDialog):
    def __init__(self, records, summary, parent=None):
        super().__init__(parent, tr('批量删除历史'))
        self.resize(680, 350)
        label = CaptionLabel(tr('将删除 {count} 条记录，涉及 {files} 个输出文件，合计 {size:.2f} MB。').format(
            count=len(records), files=summary['file_count'], size=summary['size_bytes'] / 1048576))
        label.setWordWrap(True)
        self.body_layout.addWidget(label)
        self.keep_files = RadioButton(tr('仅删除记录，保留已生成视频文件'))
        self.delete_files = RadioButton(tr('删除记录并清理输出文件（优先放入回收站，可恢复）'))
        self.keep_files.setChecked(True)
        self.body_layout.addWidget(self.keep_files)
        self.body_layout.addWidget(self.delete_files)
        hint = CaptionLabel(tr('回收站不可用时会再次询问是否永久删除；取消不会永久删除输出文件。'))
        hint.setWordWrap(True)
        self.body_layout.addWidget(hint)
        self.uncertain_confirm = CheckBox(tr('我确认待确认任务未在服务商后台创建，同意作废后删除记录'))
        self.has_uncertain = any(record.get('status') == 'submission_unknown' for record in records)
        self.uncertain_confirm.setVisible(self.has_uncertain)
        self.body_layout.addWidget(self.uncertain_confirm, 0, Qt.AlignLeft)
        self.reason = CaptionLabel()
        self.reason.setWordWrap(True)
        self.body_layout.addWidget(self.reason)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancelButton = PushButton(tr('取消'))
        self.yesButton = PushButton(tr('确认删除'))
        footer.addWidget(self.cancelButton)
        footer.addWidget(self.yesButton)
        self.body_layout.addLayout(footer)
        self.cancelButton.clicked.connect(self.reject)
        self.yesButton.clicked.connect(self._confirm)
        self.uncertain_confirm.stateChanged.connect(self._changed)
        self.cancelButton.setFocus()
        self._changed()

    def _changed(self, *_):
        valid = not self.has_uncertain or self.uncertain_confirm.isChecked()
        self.yesButton.setEnabled(valid)
        self.reason.setText(tr('请先确认待确认任务未创建；删除会作废，不会重试或提交。') if not valid else
                            tr('只处理已选择的非运行态记录；不会创建任务或重复扣费。'))
        self.yesButton.setToolTip('' if valid else self.reason.text())

    def _confirm(self):
        self._changed()
        if self.yesButton.isEnabled():
            self.accept()


class PermanentDeleteDialog(StudioDialog):
    def __init__(self, file_count, parent=None):
        super().__init__(parent, tr('回收站不可用：确认永久删除？'))
        self.resize(600, 260)
        hint = CaptionLabel(tr('{count} 个输出文件无法放入回收站。永久删除不可恢复；取消将保留这些文件和对应历史记录。').format(
            count=file_count))
        hint.setWordWrap(True)
        self.body_layout.addWidget(hint)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancelButton = PushButton(tr('保留文件与记录'))
        self.yesButton = PushButton(tr('确认永久删除'))
        footer.addWidget(self.cancelButton)
        footer.addWidget(self.yesButton)
        self.body_layout.addLayout(footer)
        self.cancelButton.clicked.connect(self.reject)
        self.yesButton.clicked.connect(self.accept)
        self.cancelButton.setFocus()
