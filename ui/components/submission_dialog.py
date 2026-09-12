"""Default-cancel recovery; no UI action silently authorizes another POST."""
from qfluentwidgets import MessageBoxBase, SubtitleLabel, CaptionLabel, LineEdit, ComboBox


class SubmissionRecoveryDialog(MessageBoxBase):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel('确认提交结果'))
        hint = CaptionLabel('请先在服务商后台核对该任务。无法确认时关闭此窗口，软件会继续阻止重新提交。')
        hint.setWordWrap(True)
        self.viewLayout.addWidget(hint)
        self.action = ComboBox()
        self.action.addItems(['保持待确认', '填写已创建的任务ID', '我已确认服务端未创建任务'])
        self.viewLayout.addWidget(self.action)
        self.task_id = LineEdit()
        self.task_id.setPlaceholderText('服务商返回的 task_id')
        self.viewLayout.addWidget(self.task_id)
        self.yesButton.setText('保存确认结果')
        self.cancelButton.setText('暂不处理')
        self.action.currentIndexChanged.connect(self._changed)
        self.task_id.textChanged.connect(self._changed)
        self.cancelButton.setFocus()
        self._changed()

    def _changed(self, *_):
        index = self.action.currentIndex()
        self.task_id.setVisible(index == 1)
        self.yesButton.setEnabled(index == 2 or (index == 1 and bool(self.task_id.text().strip())))
