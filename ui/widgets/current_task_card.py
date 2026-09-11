"""Live task progress card."""
from core.task_manager import STATUS_TEXT

from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from qfluentwidgets import FluentIcon as FIF
from ..components.custom_widgets import CaptionLabel, ProgressBar, PushButton, StrongBodyLabel, make_card


class CurrentTaskCard(QWidget):
    def __init__(self, log_callback, parent=None) -> None:
        super().__init__(parent)
        self.log_callback = log_callback
        card = make_card(elevated=True)
        card.setObjectName("currentTaskCard")
        card.setStyleSheet("#currentTaskCard { border: 1px solid rgba(94,106,210,0.75); border-radius: 12px; }")
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); outer.addWidget(card)
        root = QVBoxLayout(card); root.setContentsMargins(20, 20, 20, 20); root.setSpacing(9)
        root.addWidget(StrongBodyLabel("当前任务"))
        self.concurrency_label = CaptionLabel('正在生成0个，排队0个'); root.addWidget(self.concurrency_label)
        self.title = StrongBodyLabel('尚未开始任务'); root.addWidget(self.title)
        self.details = CaptionLabel('请选择目录并开始生成'); self.details.setWordWrap(True); root.addWidget(self.details)
        self.debug_mode = False
        self.debug_summary = CaptionLabel(''); self.debug_summary.setVisible(False); root.addWidget(self.debug_summary)
        self.progress = ProgressBar(); self.progress.setRange(0, 100); self.progress.setValue(0); root.addWidget(self.progress)
        self.timing = CaptionLabel('0%    已用 0 秒    预计剩余 —'); root.addWidget(self.timing)
        buttons = QHBoxLayout(); buttons.addStretch(1)
        self.skip_button = PushButton(FIF.SKIP_FORWARD, '跳过当前')
        self.cancel_button = PushButton(FIF.SYNC, '重新下载')
        self.cancel_button.setToolTip('已有 task_id 时仅查询并下载，不重新创建任务')
        buttons.addWidget(self.skip_button); buttons.addWidget(self.cancel_button); root.addLayout(buttons)
        self.task_info = {}
        self.skip_button.setEnabled(False); self.cancel_button.setEnabled(False)

    def update_counts(self, running, waiting):
        self.concurrency_label.setText(f'正在生成{running}个，排队{waiting}个')

    def update_task(self, index, task):
        self.task_info = task
        failed = task.get('status') == 'failed'
        self.progress.setError(failed)
        # Tint the track too: an upload failure may occur while progress is still 0%.
        track = '#ef4444' if failed else '#66666c'
        self.progress.setCustomBackgroundColor(track, track)
        self.title.setTextColor('#ef4444' if failed else '#ffffff', '#ef4444' if failed else '#ffffff')
        self.title.setText(f"{index + 1:02d} · {task['prompt_name']} · {STATUS_TEXT.get(task['status'], task['status'])}")
        self.details.setText(f"参考图 {len(task.get('images', []))} 张    模型 {task['model']}\n任务ID：{task.get('task_id') or '待提交'}")
        self._update_debug_summary()

    def set_debug_mode(self, enabled):
        self.debug_mode = bool(enabled)
        self._update_debug_summary()

    def _update_debug_summary(self):
        count = self.task_info.get('submitted_image_count')
        self.debug_summary.setText(f'已提交{count}张参考图' if count is not None else '')
        self.debug_summary.setVisible(self.debug_mode and count is not None and bool(self.task_info.get('task_id')))

    def update_progress(self, progress, elapsed, eta):
        self.progress.setValue(int(progress))
        remaining = f'{eta:.0f} 秒' if eta >= 0 else '—'
        self.timing.setText(f'{progress:.0f}%    已用 {elapsed:.0f} 秒    预计剩余 {remaining}')
