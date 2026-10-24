"""Live task progress card."""
from core.task_manager import STATUS_TEXT
from core.i18n import tr

from PyQt5.QtWidgets import QVBoxLayout, QWidget

from qfluentwidgets import FluentIcon as FIF
from ..components.custom_widgets import CaptionLabel, ProgressBar, PushButton, StrongBodyLabel, make_card
from ..materials import ERROR
from ..responsive import WrapLayout


class CurrentTaskCard(QWidget):
    def __init__(self, log_callback, parent=None) -> None:
        super().__init__(parent)
        self.log_callback = log_callback
        card = make_card(elevated=True)
        card.setObjectName("currentTaskCard")
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); outer.addWidget(card)
        root = QVBoxLayout(card); root.setContentsMargins(20, 20, 20, 20); root.setSpacing(8)
        self.heading = StrongBodyLabel(tr("当前任务")); root.addWidget(self.heading)
        self.concurrency_label = CaptionLabel(''); self.concurrency_label.setWordWrap(True); root.addWidget(self.concurrency_label)
        self.title = StrongBodyLabel(tr('尚未开始任务')); self.title.setWordWrap(True); root.addWidget(self.title)
        self.details = CaptionLabel(tr('请选择目录并开始生成')); self.details.setWordWrap(True); root.addWidget(self.details)
        self.debug_mode = False
        self.debug_summary = CaptionLabel(''); self.debug_summary.setWordWrap(True); self.debug_summary.setVisible(False); root.addWidget(self.debug_summary)
        self.progress = ProgressBar(); self.progress.setRange(0, 100); self.progress.setValue(0); root.addWidget(self.progress)
        self.timing = CaptionLabel(''); self.timing.setWordWrap(True); root.addWidget(self.timing)
        buttons = WrapLayout(); buttons.addStretch(1)
        self.skip_button = PushButton(FIF.SKIP_FORWARD, tr('跳过当前'))
        self.cancel_button = PushButton(FIF.SYNC, tr('重新下载'))
        self.resolve_button = PushButton(FIF.INFO, tr('处理待确认'))
        self.resolve_button.setVisible(False)
        self.resolve_button.setToolTip(tr('没有待确认提交'))
        self.cancel_button.setToolTip(tr('已有 task_id 时仅查询并下载，不重新创建任务'))
        buttons.addWidget(self.resolve_button); buttons.addWidget(self.skip_button); buttons.addWidget(self.cancel_button); root.addLayout(buttons)
        self.task_info = {}
        self.skip_button.setEnabled(False); self.cancel_button.setEnabled(False)
        self.skip_button.setToolTip(tr('当前没有可跳过的运行中任务'))
        self.cancel_button.setToolTip(tr('当前任务尚无可查询的 task_id'))
        # retranslate_ui 需要复现的动态状态：数量、当前任务、进度、动作可用性。
        self._counts = (0, 0)
        self._last_index = None
        self._last_progress = (0, 0, -1)
        self._action_state = {'running': False, 'redownloading': False, 'closing': False}
        self.update_counts(0, 0)
        self._render_timing()

    def update_counts(self, running, waiting):
        self._counts = (int(running), int(waiting))
        self.concurrency_label.setText(
            tr('正在生成{running}个，排队{waiting}个').format(running=int(running), waiting=int(waiting)))

    def update_task(self, index, task):
        self.task_info = task
        self._last_index = index
        self.resolve_button.setVisible(task.get('status') == 'submission_unknown')
        self.resolve_button.setEnabled(task.get('status') == 'submission_unknown')
        failed = task.get('status') == 'failed'
        self.progress.setError(failed)
        # Tint the track too: an upload failure may occur while progress is still 0%.
        track = ERROR if failed else '#35353c'
        self.progress.setCustomBackgroundColor(track, track)
        # 标题色跟随主题（浅色主题下用深色文字，修复白底白字）。
        from ..materials import text_color
        title_color = ERROR if failed else text_color(1)
        self.title.setTextColor(title_color, title_color)
        self._render_task(failed)
        self._update_debug_summary()

    def _render_task(self, failed=None):
        task = self.task_info
        if not task:
            return
        index = self._last_index or 0
        if failed is None:
            failed = task.get('status') == 'failed'
        self.title.setText(f"{index + 1:02d} · {task['prompt_name']} · {tr(STATUS_TEXT.get(task['status'], task['status']))}")
        self.details.setText(
            tr('参考图 {count} 张    模型 {model}\n任务ID：{task_id}').format(
                count=len(task.get('images', [])), model=task['model'],
                task_id=task.get('task_id') or tr('待提交')))

    def set_action_state(self, running=False, redownloading=False, closing=False):
        """Synchronize current-task actions with the page lifecycle."""
        self._action_state = {'running': bool(running), 'redownloading': bool(redownloading), 'closing': bool(closing)}
        busy = bool(running or redownloading or closing)
        status = self.task_info.get('status')
        resolve_enabled = status == 'submission_unknown' and not busy
        skip_enabled = bool(running and status in {'queued', 'uploading', 'submitting', 'processing', 'downloading'})
        cancel_enabled = bool(self.task_info.get('task_id')) and not busy
        self.resolve_button.setEnabled(resolve_enabled)
        self.skip_button.setEnabled(skip_enabled)
        self.cancel_button.setEnabled(cancel_enabled)
        busy_reason = tr('请先暂停并结束当前队列或下载') if busy else ''
        self.resolve_button.setToolTip('' if resolve_enabled else (busy_reason or tr('当前任务不是提交待确认状态')))
        self.skip_button.setToolTip('' if skip_enabled else (busy_reason or tr('只有运行中的活动任务可以跳过')))
        self.cancel_button.setToolTip('' if cancel_enabled else (busy_reason or tr('当前任务没有可查询的 task_id')))

    def set_debug_mode(self, enabled):
        self.debug_mode = bool(enabled)
        self._update_debug_summary()

    def _update_debug_summary(self):
        count = self.task_info.get('submitted_image_count')
        self.debug_summary.setText(tr('已提交{count}张参考图').format(count=count) if count is not None else '')
        self.debug_summary.setVisible(self.debug_mode and count is not None and bool(self.task_info.get('task_id')))

    def update_progress(self, progress, elapsed, eta):
        self._last_progress = (progress, elapsed, eta)
        self.progress.setValue(int(progress))
        self._render_timing()

    def _render_timing(self):
        progress, elapsed, eta = self._last_progress
        remaining = f'{eta:.0f} {tr("秒")}' if eta >= 0 else '—'
        self.timing.setText(f'{progress:.0f}%    {tr("已用")} {elapsed:.0f} {tr("秒")}    {tr("预计剩余")} {remaining}')

    def retranslate_ui(self):
        """语言切换后重建本卡全部文案（保持任务数据与控件对象不变）。"""
        try:
            self.heading.setText(tr('当前任务'))
            self.skip_button.setText(tr('跳过当前'))
            self.cancel_button.setText(tr('重新下载'))
            self.resolve_button.setText(tr('处理待确认'))
            self.resolve_button.setToolTip(tr('没有待确认提交'))
            self.update_counts(*self._counts)
            if self.task_info:
                self._render_task()
            else:
                self.title.setText(tr('尚未开始任务'))
                self.details.setText(tr('请选择目录并开始生成'))
            self._render_timing()
            self._update_debug_summary()
            self.set_action_state(**self._action_state)
        except RuntimeError:
            pass
