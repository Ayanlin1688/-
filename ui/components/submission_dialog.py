"""待确认提交恢复弹窗。"""

from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, CheckBox, ComboBox, LineEdit, PushButton, SubtitleLabel

from core.i18n import tr
from .studio_dialog import StudioDialog


def _can_discard(record):
    return not record.get('task_id') and record.get('status') in {'submission_unknown', 'failed'}


def _discard_reason(record, confirmed):
    if record.get('task_id'):
        return tr('该记录已有 task_id，不能作废；请选择“已创建：仅查询下载”。')
    if not _can_discard(record):
        return tr('仅无 task_id 的待确认或创建前失败记录可作废。')
    if not confirmed:
        return tr('作废需确认服务端未创建，以防漏单/重复扣费')
    return tr('将作废该条、移出待处理，不重试、不提交其他任务')


def _context_text(record):
    record = record or {}
    prompt_path = record.get('prompt_path') or ''
    prompt_name = Path(prompt_path).name if prompt_path else record.get('prompt_name') or '—'
    model = record.get('model') or record.get('requested_model') or '—'
    submitted_at = (record.get('submitted_at') or record.get('created_at') or
                    record.get('finished_at') or record.get('updated_at') or '—')
    product = record.get('product') or '—'
    error = str(record.get('error') or '').strip()
    if len(error) > 600:
        error = error[:597] + '...'
    summary = tr('\n上次上游返回摘要：{error}').format(error=error) if error else ''
    return tr('产品名：{product}\n提示词文件：{prompt_name}\n模型：{model}\n提交时间：{submitted_at}{summary}').format(
        product=product, prompt_name=prompt_name, model=model, submitted_at=submitted_at, summary=summary)


class _RecoveryDialogBase(StudioDialog):
    """带即时生效按钮的恢复弹窗外壳。"""

    TITLE = ''

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

    def retranslate_ui(self):
        super().retranslate_ui()
        try:
            self.setWindowTitle(tr(self.TITLE))
            self.set_title(tr(self.TITLE))
        except Exception:
            pass
        try:
            self._changed()
        except Exception:
            pass

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)


class SubmissionRecoveryDialog(_RecoveryDialogBase):
    TITLE = '确认提交结果'

    def __init__(self, parent=None, record=None):
        super().__init__(parent, tr(self.TITLE))
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
        self.action.addItems([tr('保持待确认'), tr('填写已创建的任务ID'), tr('我已确认服务端未创建任务'),
                              tr('作废：确认无效，移出待处理（不重试）')])
        self.action.setItemEnabled(3, _can_discard(self.record))
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
        self.discard_confirm = CheckBox(tr('我确认该任务未在服务商后台创建，作废后不再提示'))
        self.viewLayout.addWidget(self.discard_confirm, 0, Qt.AlignLeft)
        self.reason_label = CaptionLabel()
        self.reason_label.setWordWrap(True)
        self.reason_label.setObjectName('submissionRecoveryReason')
        self.viewLayout.addWidget(self.reason_label)
        self._add_footer()
        self.action.currentIndexChanged.connect(self._changed)
        self.task_id.textChanged.connect(self._changed)
        self.discard_confirm.stateChanged.connect(self._changed)
        self.yesButton.clicked.connect(self._accept_if_valid)
        self.cancelButton.setFocus()
        self._changed()

    def retranslate_ui(self):
        super().retranslate_ui()
        try:
            self.context_label.setText(_context_text(self.record))
        except Exception:
            pass

    def _retry_not_created(self):
        self.action.setCurrentIndex(2)
        self.accept()

    def _select_existing(self):
        self.action.setCurrentIndex(1)
        self.task_id.setFocus()
        if self.task_id.text().strip():
            self.accept()

    def _accept_if_valid(self):
        self._changed()
        if self.yesButton.isEnabled():
            self.accept()

    def _reason(self, index, task_id):
        if index == 1:
            if not task_id:
                return tr('请填写服务商后台已创建的 task_id；填写后“保存确认结果”即可点击。')
            return tr('将仅查询并下载该 task_id，不会创建新任务。')
        if index == 2:
            return tr('将只重试当前这一条，不会提交其他任务。')
        if index == 3:
            return _discard_reason(self.record, self.discard_confirm.isChecked())
        if self.record.get('task_id'):
            return _discard_reason(self.record, False)
        return tr('请先选择上面的核对结果；关闭或暂不处理不会提交。')

    def _changed(self, *_):
        index = self.action.currentIndex()
        task_id = self.task_id.text().strip()
        self.task_id.setVisible(index == 1)
        self.task_id.setEnabled(index == 1)
        self.discard_confirm.setVisible(index == 3)
        self.discard_confirm.setEnabled(_can_discard(self.record))
        self.yesButton.setEnabled(index == 2 or (index == 1 and bool(task_id)) or
                                  (index == 3 and _can_discard(self.record) and self.discard_confirm.isChecked()))
        self.reason_label.setText(self._reason(index, task_id))
        self.yesButton.setToolTip('' if self.yesButton.isEnabled() else self.reason_label.text())


class SubmissionRecoveryBatchDialog(_RecoveryDialogBase):
    """Require an explicit decision for every uncertain submission before saving."""

    TITLE = '批量处理待确认提交'

    def __init__(self, records, parent=None):
        super().__init__(parent, tr(self.TITLE))
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
        self.yesButton.clicked.connect(self._accept_if_valid)
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
            tr('作废：确认无效，移出待处理（不重试）'),
        ])
        action.setItemEnabled(4, _can_discard(record))
        action.setMinimumWidth(330)
        controls.addWidget(action, 1)
        layout.addLayout(controls)
        task_id = LineEdit()
        task_id.setPlaceholderText(tr('服务商后台的 task_id'))
        task_id.setVisible(False)
        task_id.setEnabled(False)
        layout.addWidget(task_id)
        discard_confirm = CheckBox(tr('我确认该任务未在服务商后台创建，作废后不再提示'))
        layout.addWidget(discard_confirm, 0, Qt.AlignLeft)
        reason = CaptionLabel()
        reason.setWordWrap(True)
        layout.addWidget(reason)
        self._content_layout.addWidget(frame)
        entry = dict(record=record, action=action, task_id=task_id, reason=reason, discard_confirm=discard_confirm,
                     number=number, title=title)
        self._rows.append(entry)
        action.currentIndexChanged.connect(self._changed)
        task_id.textChanged.connect(self._changed)
        discard_confirm.stateChanged.connect(self._changed)

    def retranslate_ui(self):
        super().retranslate_ui()
        for entry in getattr(self, '_rows', []):
            try:
                entry['title'].setText(f"{entry['number']}. {_context_text(entry['record'])}")
            except Exception:
                pass
        try:
            self._changed()
        except Exception:
            pass

    def _accept_if_valid(self):
        self._changed()
        if self.yesButton.isEnabled():
            self.accept()

    def _changed(self, *_):
        valid = True
        for entry in self._rows:
            index = entry['action'].currentIndex()
            task_id = entry['task_id'].text().strip()
            entry['task_id'].setVisible(index == 3)
            entry['task_id'].setEnabled(index == 3)
            entry['discard_confirm'].setVisible(index == 4)
            entry['discard_confirm'].setEnabled(_can_discard(entry['record']))
            if index == 0:
                entry['reason'].setText(_discard_reason(entry['record'], False) if entry['record'].get('task_id') else
                                        tr('请先选择：保持待确认、未创建、已创建或作废。'))
                valid = False
            elif index == 1:
                entry['reason'].setText(tr('保持待确认：不调用提交接口，仍阻止该任务再次创建。'))
            elif index == 2:
                entry['reason'].setText(tr('仅释放并重试这一条提示词，不会提交其他任务。'))
            elif index == 3:
                entry['reason'].setText(
                    tr('将仅查询并下载该 task_id，不会重新创建任务。') if task_id else
                    tr('请填写服务商后台已创建的 task_id。'))
                valid = valid and bool(task_id)
            elif index == 4:
                confirmed = entry['discard_confirm'].isChecked()
                entry['reason'].setText(_discard_reason(entry['record'], confirmed))
                valid = valid and _can_discard(entry['record']) and confirmed
            else:
                valid = False
        self.yesButton.setEnabled(bool(self._rows) and valid)
        self.yesButton.setToolTip('' if self.yesButton.isEnabled() else
                                  tr('请逐条选择处理结果；已创建项需填写 task_id，作废项需勾选服务端未创建确认'))
        self.reason_label.setText(
            tr('所有条目都已明确核对，可以保存；保存后仍将逐条执行并保留防重复提交保护。')
            if self.yesButton.isEnabled() else
            tr('请逐条选择上面的核对结果；已创建项需填写 task_id，作废项需勾选服务端未创建确认。取消或关闭不会产生任何提交。'))

    def selections(self):
        """Return (record, action, task_id), using single-dialog action indexes."""
        result = []
        for entry in self._rows:
            index = entry['action'].currentIndex()
            action = {1: 0, 2: 2, 3: 1, 4: 3}.get(index, 0)
            result.append((entry['record'], action, entry['task_id'].text().strip() if index != 4 else ''))
        return result
