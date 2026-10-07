"""待确认提交恢复弹窗。"""

from pathlib import Path

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, ComboBox, LineEdit, PushButton, SubtitleLabel

from core.i18n import tr
from .studio_dialog import StudioDialog


def _context_text(record):
    record = record or {}
    prompt_path = record.get('prompt_path') or ''
    prompt_name = Path(prompt_path).name if prompt_path else record.get('prompt_name') or '—'
    model = record.get('model') or record.get('requested_model') or '—'
    submitted_at = (record.get('submitted_at') or record.get('created_at') or
                    record.get('finished_at') or record.get('updated_at') or '—')
    product = record.get('product') or '—'
    return tr('产品名：{product}\n提示词文件：{prompt_name}\n模型：{model}\n提交时间：{submitted_at}').format(
        product=product, prompt_name=prompt_name, model=model, submitted_at=submitted_at)


class _RecoveryDialogBase(StudioDialog):
    """带即时生效按钮的恢复弹窗外壳。"""

    def _add_footer(self):
        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.addStretch(1)
        self.cancelButton = PushButton(tr('暂不处理'))
        self.yesButton = PushButton(tr('保存确认结果'))
        self.cancelButton.setMinimumHeight(34)
        self.yesButton.setMinimumHeight(34)
        self.cancelButton.clicked.connect(self.reject)
        footer.addWidget(self.cancelButton)
        footer.addWidget(self.yesButton)
        self.body_layout.addLayout(footer)
        self.buttonLayout = footer
        self.finished.connect(lambda _result: QTimer.singleShot(0, self.deleteLater))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)


class SubmissionRecoveryDialog(_RecoveryDialogBase):
    def __init__(self, parent=None, record=None):
        super().__init__(parent, tr('确认提交结果'))
        self.record = record or {}
        self.viewLayout = self.body_layout
        self.resize(680, 560)
        self.viewLayout.addWidget(SubtitleLabel(tr('确认提交结果')))
        self.context_label = CaptionLabel(_context_text(self.record))
        self.context_label.setWordWrap(True)
        self.viewLayout.addWidget(self.context_label)
        hint = CaptionLabel(tr('请先在服务商后台核对该任务。关闭或“暂不处理”只会保持待确认，不会产生任何提交。'))
        hint.setWordWrap(True)
        self.viewLayout.addWidget(hint)
        self.action = ComboBox()
        self.action.addItems([tr('保持待确认'), tr('填写已创建的任务ID'), tr('我已确认服务端未创建任务')])
        self.action.setMinimumWidth(420)
        self.action.setFocusPolicy(Qt.StrongFocus)
        self.action.setToolTip(tr('选择已在服务商后台核对后的处理方式'))
        self.viewLayout.addWidget(self.action)
        self.retry_not_created_button = PushButton(
            tr('我已在服务商后台确认该任务未创建 → 仅重试这一条，不重复提交其他任务'))
        self.retry_not_created_button.setMinimumHeight(36)
        self.retry_not_created_button.setToolTip(tr('选择“未创建”并立即保存，只释放当前提示词的防重复保护'))
        self.retry_not_created_button.clicked.connect(self._retry_not_created)
        self.viewLayout.addWidget(self.retry_not_created_button)
        self.lookup_existing_button = PushButton(
            tr('服务商后台已创建，我填写 task_id → 仅查询并下载，不重新创建'))
        self.lookup_existing_button.setMinimumHeight(36)
        self.lookup_existing_button.setToolTip(tr('展开 task_id 输入框；填写后只查询并下载'))
        self.lookup_existing_button.clicked.connect(self._select_existing)
        self.viewLayout.addWidget(self.lookup_existing_button)
        self.task_id = LineEdit()
        self.task_id.setPlaceholderText(tr('服务商返回的 task_id'))
        self.task_id.setMinimumWidth(420)
        self.viewLayout.addWidget(self.task_id)
        self.reason_label = CaptionLabel()
        self.reason_label.setWordWrap(True)
        self.reason_label.setObjectName('submissionRecoveryReason')
        self.viewLayout.addWidget(self.reason_label)
        self._add_footer()
        self.action.currentIndexChanged.connect(self._changed)
        self.task_id.textChanged.connect(self._changed)
        self.yesButton.clicked.connect(self._accept_if_valid)
        self.cancelButton.setFocus()
        self._changed()

    def _retry_not_created(self):
        self.action.setCurrentIndex(2)
        self.accept()

    def _select_existing(self):
        self.action.setCurrentIndex(1)
        self.task_id.setFocus()
        if self.task_id.text().strip():
            self.accept()

    def _accept_if_valid(self):
        if self.action.currentIndex() == 2:
            self.accept()
        elif self.action.currentIndex() == 1 and self.task_id.text().strip():
            self.accept()
        else:
            self._changed()

    def _reason(self, index, task_id):
        if index == 1:
            if not task_id:
                return tr('请填写服务商后台已创建的 task_id；填写后“保存确认结果”即可点击。')
            return tr('将仅查询并下载该 task_id，不会创建新任务。')
        if index == 2:
            return tr('将只重试当前这一条，不会提交其他任务。')
        return tr('请先选择上面的核对结果；关闭或暂不处理不会提交。')

    def _changed(self, *_):
        index = self.action.currentIndex()
        task_id = self.task_id.text().strip()
        self.task_id.setVisible(index == 1)
        self.task_id.setEnabled(index == 1)
        self.yesButton.setEnabled(index == 2 or (index == 1 and bool(task_id)))
        self.reason_label.setText(self._reason(index, task_id))
        self.yesButton.setToolTip('' if self.yesButton.isEnabled() else self.reason_label.text())


class SubmissionRecoveryBatchDialog(_RecoveryDialogBase):
    """Require an explicit decision for every uncertain submission before saving."""

    def __init__(self, records, parent=None):
        super().__init__(parent, tr('批量处理待确认提交'))
        self.records = list(records or [])
        self._rows = []
        self.viewLayout = self.body_layout
        self.resize(900, min(820, max(480, 260 + len(self.records) * 160)))
        self.viewLayout.addWidget(SubtitleLabel(tr('批量处理待确认提交')))
        hint = CaptionLabel(tr(
            '请逐条在服务商后台核对。每条都必须明确选择；关闭或取消不会提交任何任务，'
            '“未创建”也只会逐条重试，不会静默重建其他任务。'))
        hint.setWordWrap(True)
        self.viewLayout.addWidget(hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        content = QWidget()
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(2, 2, 2, 2)
        self._content_layout.setSpacing(8)
        for number, record in enumerate(self.records, 1):
            self._add_row(number, record)
        self._content_layout.addStretch(1)
        scroll.setWidget(content)
        self.viewLayout.addWidget(scroll, 1)
        self.reason_label = CaptionLabel()
        self.reason_label.setWordWrap(True)
        self.viewLayout.addWidget(self.reason_label)
        self._add_footer()
        self.yesButton.clicked.connect(self.accept)
        self.yesButton.setEnabled(False)
        self.yesButton.setToolTip(tr('请逐条选择处理结果后才能保存'))
        self.cancelButton.setFocus()
        self._changed()

    def _add_row(self, number, record):
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(5)
        title = CaptionLabel(f'{number}. {_context_text(record)}')
        title.setWordWrap(True)
        layout.addWidget(title)
        controls = QHBoxLayout()
        controls.addWidget(CaptionLabel(tr('核对结果')))
        action = ComboBox()
        action.addItems([
            tr('请选择核对结果'),
            tr('保持待确认'),
            tr('未创建：仅重试这一条'),
            tr('已创建：填写 task_id，仅查询并下载'),
        ])
        action.setMinimumWidth(330)
        controls.addWidget(action, 1)
        layout.addLayout(controls)
        task_id = LineEdit()
        task_id.setPlaceholderText(tr('服务商后台的 task_id'))
        task_id.setVisible(False)
        task_id.setEnabled(False)
        layout.addWidget(task_id)
        reason = CaptionLabel()
        reason.setWordWrap(True)
        layout.addWidget(reason)
        self._content_layout.addWidget(frame)
        entry = dict(record=record, action=action, task_id=task_id, reason=reason)
        self._rows.append(entry)
        action.currentIndexChanged.connect(self._changed)
        task_id.textChanged.connect(self._changed)

    def _changed(self, *_):
        valid = True
        for entry in self._rows:
            index = entry['action'].currentIndex()
            task_id = entry['task_id'].text().strip()
            entry['task_id'].setVisible(index == 3)
            entry['task_id'].setEnabled(index == 3)
            if index == 0:
                entry['reason'].setText(tr('请先选择：保持待确认、未创建或已创建。'))
                valid = False
            elif index == 1:
                entry['reason'].setText(tr('保持待确认：不调用提交接口，仍阻止该任务再次创建。'))
            elif index == 2:
                entry['reason'].setText(tr('仅释放并重试这一条提示词，不会提交其他任务。'))
            else:
                entry['reason'].setText(
                    tr('将仅查询并下载该 task_id，不会重新创建任务。') if task_id else
                    tr('请填写服务商后台已创建的 task_id。'))
                valid = valid and bool(task_id)
        self.yesButton.setEnabled(bool(self._rows) and valid)
        self.yesButton.setToolTip('' if self.yesButton.isEnabled() else tr('请逐条选择处理结果；已创建项还需填写 task_id'))
        self.reason_label.setText(
            tr('所有条目都已明确核对，可以保存；保存后仍将逐条执行并保留防重复提交保护。')
            if self.yesButton.isEnabled() else
            tr('请逐条选择上面的核对结果；取消或关闭不会产生任何提交。'))

    def selections(self):
        """Return (record, action, task_id), using single-dialog action indexes."""
        result = []
        for entry in self._rows:
            index = entry['action'].currentIndex()
            action = {1: 0, 2: 2, 3: 1}.get(index, 0)
            result.append((entry['record'], action, entry['task_id'].text().strip()))
        return result
