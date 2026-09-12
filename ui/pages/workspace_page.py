"""Workspace page composed from Fluent cards and controls."""

import copy
from pathlib import Path
import threading
import time
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QSplitter, QVBoxLayout, QWidget, QScrollArea, QLayout, QSizePolicy
from qfluentwidgets import CaptionLabel, FluentIcon as FIF, PrimaryPushButton, PushButton, TitleLabel, ScrollArea, InfoBar, ProgressBar
from core.task_manager import TaskManager, TERMINAL, ACTIVE, stamp
from core.matcher import StoryboardMatcher
from core.background import BackgroundJobs
from core.prompt_detector import annotate_tasks
from ..model_catalog_controller import runtime_config
from ..components.model_selector import catalog_snapshot, usable
from core.api_client import ApiClient
from core.http_client import Cancelled
from core.submission_ledger import SubmissionLedger, account_scope, ledger_path
from ..components.submission_dialog import SubmissionRecoveryDialog
from core.video_downloader import VideoDownloader, build_filename
from ..file_actions import open_local

from ..widgets.current_task_card import CurrentTaskCard
from ..widgets.data_source_card import DataSourceCard
from ..widgets.log_drawer import LogDrawer
from ..widgets.params_card import ParamsCard
from ..widgets.recent_completed_panel import RecentCompletedPanel
from ..widgets.task_queue_panel import TaskQueuePanel


class WorkspacePage(QWidget):
    history_changed = pyqtSignal(object)
    def __init__(self, config_manager, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("workspacePage")
        self.config_manager = config_manager
        self.task_manager = TaskManager(self)
        self.jobs = BackgroundJobs(self)
        self.closing = threading.Event()
        self._scan_version = 0
        self._redownloading = False
        self._catalog_pending = False
        self._build_ui()
        manager = self.task_manager
        self.start_button.clicked.connect(self.start_generation)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.cancel_button.clicked.connect(manager.cancel_all)
        self.current_task.skip_button.clicked.connect(manager.skip_current)
        self.current_task.cancel_button.clicked.connect(lambda: self.redownload(self.current_task.task_info))
        self.current_task.resolve_button.clicked.connect(lambda: self.resolve_submission(self.current_task.task_info))
        self.queue_panel.task_selected.connect(manager.select_current)
        self.queue_panel.model_override_requested.connect(self.force_task_model)
        self.queue_panel.reset_models_requested.connect(self.reset_task_models)
        manager.task_list_updated.connect(self._tasks_updated)
        manager.current_task_changed.connect(self._current_changed)
        manager.task_progress.connect(self.current_task.update_progress)
        manager.log_message.connect(self.append_log)
        manager.record_updated.connect(self._record)
        manager.running_changed.connect(self._running_changed)
        manager.pause_changed.connect(lambda paused: self.pause_button.setText('继续' if paused else '暂停'))
        manager.all_finished.connect(self._finished)
        self.jobs.log_message.connect(self.append_log)
        self.data_source.directories_changed.connect(self.scan_sources)
        self.data_source.overrides_changed.connect(self.scan_sources)
        self.params_card.values_changed.connect(lambda key, value: self.scan_sources() if key == 'model' else None)
        self.recent_panel.update_history(config_manager.config['history'])
        self._running_changed(False)
        self.append_log('工作台已加载：支持模型池、自动重试和并发控制；关闭模型池时使用工作台所选模型', 'info')
        QTimer.singleShot(0, self.scan_sources)

    def _build_ui(self):
        root = QVBoxLayout(self); root.setContentsMargins(22, 18, 22, 16); root.setSpacing(16)
        header = QHBoxLayout(); title_box = QVBoxLayout(); title_box.addWidget(TitleLabel("工作台")); title_box.addWidget(CaptionLabel("批量生成分镜视频")); header.addLayout(title_box); header.addStretch(1)
        self.start_button = PrimaryPushButton(FIF.PLAY, "开始生成"); self.pause_button = PushButton(FIF.PAUSE, "暂停"); self.cancel_button = PushButton(FIF.CANCEL, "取消全部")
        header.addWidget(self.start_button); header.addWidget(self.pause_button); header.addWidget(self.cancel_button); root.addLayout(header)
        self.product_progress_label = CaptionLabel('当前：—，总进度：产品 0/0')
        self.product_progress = ProgressBar()
        self.product_progress.setRange(0, 1)
        self.product_progress.setValue(0)
        root.addWidget(self.product_progress_label)
        root.addWidget(self.product_progress)
        splitter = QSplitter(Qt.Horizontal)
        self.splitter = splitter
        splitter.setChildrenCollapsible(False)
        self.queue_panel = TaskQueuePanel(); splitter.addWidget(self.queue_panel)
        center = QWidget(); center_layout = QVBoxLayout(center); center_layout.setContentsMargins(8, 0, 8, 0); center_layout.setSpacing(16)
        self.data_source = DataSourceCard(self.config_manager, self.append_log); self.params_card = ParamsCard(self.config_manager); self.current_task = CurrentTaskCard(self.append_log)
        self.config_row = QHBoxLayout()
        self.config_row.setSpacing(16)
        self.config_row.addWidget(self.data_source, 1, Qt.AlignTop)
        self.config_row.addWidget(self.params_card, 1, Qt.AlignTop)
        center_layout.addLayout(self.config_row)
        center_layout.addWidget(self.current_task)
        for card in (self.data_source, self.params_card, self.current_task):
            card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        center_layout.addStretch(1)
        center_layout.setSizeConstraint(QLayout.SetMinAndMaxSize)
        self.center_scroll = ScrollArea()
        self.center_scroll.setFrameShape(QScrollArea.NoFrame)
        self.center_scroll.setWidgetResizable(True)
        self.center_scroll.setMinimumWidth(580)
        self.center_scroll.setWidget(center)
        self.queue_panel.setMinimumWidth(190)
        self.recent_panel = RecentCompletedPanel(self.append_log)
        self.recent_panel.setMinimumWidth(150)
        splitter.addWidget(self.center_scroll); splitter.addWidget(self.recent_panel); splitter.setStretchFactor(0, 18); splitter.setStretchFactor(1, 58); splitter.setStretchFactor(2, 14); splitter.setSizes([252, 812, 196]); root.addWidget(splitter, 1)
        self.log_drawer = LogDrawer(); root.addWidget(self.log_drawer)
        self.set_debug_mode(self.config_manager.config.get('diagnostics', {}).get('debug_mode', False))
        self.params_card.parameters_adjusted.connect(self.append_log)

    def append_log(self, message: str, level: str = "info"):
        self.log_drawer.append_log(message, level)

    def set_debug_mode(self, enabled):
        self.log_drawer.set_debug_mode(enabled)
        self.current_task.set_debug_mode(enabled)
        if self.task_manager.worker is not None:
            self.task_manager.worker.debug_mode = bool(enabled)

    def scan_sources(self):
        self._scan_version += 1
        version = self._scan_version
        if self.closing.is_set() or self.task_manager.is_running:
            return
        config = runtime_config(self.config_manager)
        if not config['paths']['prompts']:
            self.data_source.set_matches([], [])
            self._tasks_updated([])
            return
        self.data_source.status_label.setText('正在扫描匹配...')
        def scan():
            matcher = StoryboardMatcher.from_config(config)
            matched = matcher.scan_and_match(config['paths'])
            annotate_tasks(matched, config)
            automatic_matcher = StoryboardMatcher(matcher.recursive)
            automatic = automatic_matcher.scan_and_match(config['paths'])
            return matched, automatic, matcher.warnings
        def done(result):
            if version != self._scan_version or self.task_manager.is_running or self.closing.is_set():
                return
            matched, automatic, warnings = result
            self.data_source.set_matches(matched, automatic)
            self._tasks_updated(matched)
            for warning in warnings:
                self.append_log(warning, 'warning')
            self.append_log(f'扫描匹配完成：{len(matched)}个提示词，{sum(t["matched"] for t in matched)}个已匹配', 'info')
        def failed(message):
            if version == self._scan_version and not self.closing.is_set():
                self.data_source.set_matches([], [])
                self._tasks_updated([])
                self.data_source.status_label.setText('扫描失败，请检查目录')
                self.append_log(message, 'error')
        self.jobs.start(scan, done, failed)

    def start_generation(self):
        if self.closing.is_set() or self._redownloading:
            return
        self.append_log('开始生成：检查配置并准备后台队列', 'info')
        try:
            self._scan_version += 1
            self.task_manager.start_tasks(runtime_config(self.config_manager))
        except Exception as error:
            self.append_log(f'无法开始：{error}', 'error')
            InfoBar.warning('尚未开始', str(error), parent=self, duration=4500)

    def toggle_pause(self):
        if self.task_manager.is_paused:
            self.task_manager.resume_tasks()
        else:
            self.task_manager.pause_tasks()

    def refresh_catalog(self, *_):
        if self.task_manager.is_running:
            self._catalog_pending = True
            return
        self.params_card.refresh_catalog()
        self.scan_sources()

    def force_task_model(self, index, model):
        tasks = self.queue_panel._tasks
        if self.task_manager.is_running or not 0 <= index < len(tasks):
            return
        if model and not usable(catalog_snapshot(self.config_manager).get(model, {})):
            return
        overrides = dict(self.config_manager.config.get('model_overrides', {}))
        path = tasks[index]['prompt_path']
        if model:
            overrides[path] = model
        else:
            overrides.pop(path, None)
        self.config_manager.update(('model_overrides',), overrides)
        self.scan_sources()

    def reset_task_models(self):
        if not self.task_manager.is_running:
            self.config_manager.update(('model_overrides',), {})
            self.scan_sources()

    def _running_changed(self, running):
        if not running and self._catalog_pending and not self.closing.is_set():
            self._catalog_pending = False
            # Refresh controls without replacing completed rows with a new scan.
            blocker = self.params_card.blockSignals(True)
            try:
                self.params_card.refresh_catalog()
            finally:
                self.params_card.blockSignals(blocker)
        self.start_button.setEnabled(not running and not self._redownloading and not self.closing.is_set())
        self.pause_button.setEnabled(running)
        self.cancel_button.setEnabled(running)
        self.current_task.skip_button.setEnabled(running and self.current_task.task_info.get('status') in ACTIVE)
        self.current_task.cancel_button.setEnabled(not running and not self._redownloading and bool(self.current_task.task_info.get('task_id')))
        self.params_card.setEnabled(not running)
        self.data_source.setEnabled(not running)
        self.queue_panel.set_models_editable(not running)

    def _current_changed(self, index, task):
        self.current_task.update_task(index, task)
        self.current_task.skip_button.setEnabled(self.task_manager.is_running and task.get('status') in ACTIVE)
        self.queue_panel.select_task(index)
        product = task.get('product') or '未分组'
        task_index = task.get('product_task_index', index + 1)
        task_total = task.get('product_task_total', len(self.task_manager.tasks) or 1)
        product_index = task.get('product_index', 1)
        product_total = task.get('product_total', 1)
        self.product_progress_label.setText(
            f'当前：{product} ({task_index}/{task_total})，总进度：产品 {product_index}/{product_total}'
        )

    def _tasks_updated(self, tasks):
        self.queue_panel.set_model_catalog(catalog_snapshot(self.config_manager), self.config_manager.config['workspace']['model'])
        self.queue_panel.update_tasks(tasks)
        self.current_task.update_counts(sum(t.get('status') in ACTIVE for t in tasks),
                                        sum(t.get('status', 'waiting') == 'waiting' for t in tasks))
        total = len(tasks)
        self.product_progress.setRange(0, max(1, total))
        self.product_progress.setValue(sum(task.get('status') in TERMINAL for task in tasks))
        if not tasks:
            self.product_progress_label.setText('当前：—，总进度：产品 0/0')

    def _record(self, record):
        history = copy.deepcopy(self.config_manager.config['history'])
        existing = next((i for i, item in enumerate(history) if item.get('local_id') == record['local_id']), None)
        if existing is None:
            history.append(record)
        else:
            history[existing] = record
        try:
            self.config_manager.update(('history',), history)
        except Exception as error:
            self.append_log(f'历史记录写入失败（本次仍保留在内存）：{error}', 'error')
        self.history_changed.emit(history)
        self.recent_panel.update_history(history)
        if record['status'] == 'completed' and self.config_manager.config['task_strategy']['open_folder_after_download'] and not self.closing.is_set():
            open_local(record['result_path'], self.append_log, folder=True)

    def _finished(self, success, failed):
        skipped = sum(t['status'] == 'skipped' for t in self.task_manager.tasks)
        cancelled = sum(t['status'] == 'cancelled' for t in self.task_manager.tasks)
        duplicates = sum(t['status'] == 'duplicate' for t in self.task_manager.tasks)
        uncertain = sum(t['status'] == 'submission_unknown' for t in self.task_manager.tasks)
        message = f'成功 {success} · 失败 {failed} · 跳过 {skipped} · 取消 {cancelled} · 重复 {duplicates} · 待确认 {uncertain}'
        self.append_log('队列结束：' + message, 'info')
        if not self.closing.is_set():
            InfoBar.info('队列结束', message, parent=self, duration=5000)
            if duplicates:
                InfoBar.warning('防重复提交', f'已跳过{duplicates}个重复任务，避免重复扣费', parent=self, duration=6000)
            if uncertain:
                InfoBar.warning('提交待确认', '提交结果未确认，已阻止重新创建。请在当前任务或历史记录中处理待确认提交。', parent=self, duration=8000)

    def resolve_submission(self, record):
        if self.task_manager.is_running or self._redownloading or self.closing.is_set():
            self.append_log('请先暂停并结束当前队列，再处理待确认提交', 'warning')
            return
        config = runtime_config(self.config_manager)
        pending = record.get('duplicate_record') or record
        dialog = SubmissionRecoveryDialog(self.window())
        if pending.get('legacy_task_id'):
            hint = CaptionLabel('旧记录候选ID：' + str(pending['legacy_task_id']) +
                                '。该ID尚未绑定当前账号；请先核对设置中的账号，并在服务商后台确认后手动填写。')
            hint.setWordWrap(True)
            dialog.viewLayout.addWidget(hint)
        if not dialog.exec():
            return
        try:
            ledger = SubmissionLedger(ledger_path(config))
            result = ledger.resolve(pending.get('ledger_id'), account_scope(config),
                                    task_id=dialog.task_id.text() if dialog.action.currentIndex() == 1 else '',
                                    confirmed_not_created=dialog.action.currentIndex() == 2)
            self._record(result)
            for index, task in enumerate(self.task_manager.tasks):
                candidate = task.get('duplicate_record') or task
                if candidate.get('ledger_id') == pending.get('ledger_id'):
                    self.task_manager.tasks[index] = copy.deepcopy(result)
                    self._current_changed(index, result)
            self._tasks_updated(self.task_manager.tasks)
            self.append_log('确认结果已保存；填写已有ID的任务将仅查询原任务，不重新创建', 'info')
        except Exception as error:
            InfoBar.error('未保存确认结果', str(error), parent=self, duration=6500)

    def regenerate(self, record):
        if self.task_manager.is_running or self._redownloading or self.closing.is_set():
            return
        from qfluentwidgets import Dialog
        original = record.get('duplicate_record') or record
        if original.get('status') != 'completed':
            return
        dialog = Dialog('重新生成确认', '该任务已生成过，是否重新生成？这会创建一个新的付费任务。', self.window())
        dialog.yesButton.setText('确认重新生成')
        dialog.cancelButton.setText('不重新生成')
        dialog.cancelButton.setFocus()
        if not dialog.exec():
            return
        config = runtime_config(self.config_manager)
        config['_rerun_signatures'] = [original['signature']]
        config['_only_prompt_paths'] = [original['prompt_path']]
        config['workspace'].update(original.get('effective_parameters', {}))
        config['workspace']['model'] = original['model']
        config['prompt_detection']['enabled'] = True
        config['model_overrides'][original['prompt_path']] = original['model']
        try:
            self.task_manager.start_tasks(config)
        except Exception as error:
            InfoBar.warning('未开始重新生成', str(error), parent=self, duration=6000)

    def redownload(self, record):
        if self.task_manager.is_running or self._redownloading or self.closing.is_set():
            self.append_log('请等待当前队列或下载结束后再重新下载', 'warning')
            return
        if not record.get('task_id'):
            self.append_log('此记录没有远端 task_id，无法重新下载', 'warning')
            return
        config = runtime_config(self.config_manager)
        task = copy.deepcopy(record)
        if task.get('api_scope') != account_scope(config):
            message = ('此旧记录尚未确认账号归属，请先开始队列建立待确认保护，再通过“确认提交结果”核对当前账号和任务ID'
                       if not task.get('api_scope') else
                       '此任务属于其他 API 账号，请先在设置中切回原账号及对应密钥')
            self.append_log(message, 'warning')
            InfoBar.warning('请先核对任务账号', message, parent=self, duration=6500)
            return
        if task.get('api_base_url', config['api']['base_url']).strip().rstrip('/') != config['api']['base_url'].strip().rstrip('/'):
            self.append_log('此任务来自其他 API 地址，请先在设置中切回原接口及对应密钥：' + task['api_base_url'], 'warning')
            return
        self._redownloading = True
        self._running_changed(False)
        def download():
            client = ApiClient(config['api']['base_url'], config['api']['api_key'], log=self.jobs.log_message.emit)
            def check():
                if self.closing.is_set():
                    raise Cancelled('窗口正在关闭，停止下载')
            downloader = VideoDownloader(config['download_settings']['overwrite_existing'], check, log=self.jobs.log_message.emit)
            try:
                check()
                self.jobs.log_message.emit(f"沿用已有task_id={task['task_id']}继续轮询，不重复创建", 'info')
                frozen = task.get('submission_model')
                catalog = {task['model']: frozen} if isinstance(frozen, dict) else config.get('_model_catalog')
                deadline = time.monotonic() + float(config['workspace'].get('poll_timeout', 3600))
                while True:
                    check()
                    result = client.query_task(task['task_id'], task['model'], catalog)
                    if result['status'] == 'completed' and result['result_url']:
                        break
                    if result['status'] == 'failed' or time.monotonic() >= deadline:
                        raise ValueError(f"远端状态 {result['status']}；保留 task_id")
                    self.jobs.log_message.emit(f"继续轮询：{task['task_id']}，状态={result['status']}", 'info')
                    self.closing.wait(max(.01, float(config['workspace']['poll_interval'])))
                folder = task.get('output_dir') or config['paths']['output']
                if not folder:
                    raise ValueError('请先选择视频保存目录')
                index = task.get('product_task_index') or task.get('sequence') or next((i+1 for i, row in enumerate(config['history']) if row.get('local_id') == task.get('local_id')), 1)
                filename = task.get('filename') or build_filename(config['download_settings']['naming_rule'], task, index)
                task['result_path'] = downloader.download_video(result['result_url'], folder, filename)
                task.update(status='completed', result_url=result['result_url'], error='', finished_at=stamp(),
                            size_bytes=Path(task['result_path']).stat().st_size, filename=filename, output_dir=str(Path(folder).resolve()))
                if task.get('ledger_id'):
                    SubmissionLedger(ledger_path(config)).save(task, 'completed')
                return task
            finally:
                client.close(); downloader.close()
        def reset():
            self._redownloading = False
            self._running_changed(False)
        def done(result):
            self._record(result)
            for index, row in enumerate(self.task_manager.tasks):
                if row.get('local_id') == result['local_id']:
                    self.task_manager.tasks[index] = result
                    self._current_changed(index, result)
            self.queue_panel.update_tasks(self.task_manager.tasks)
            reset()
        def failed(message):
            self.append_log(f'重新下载失败：{message}', 'error')
            reset()
        self.jobs.start(download, done, failed)

    def shutdown(self):
        self.closing.set()
        self._scan_version += 1
        self.task_manager.cancel_all()
        self._running_changed(self.task_manager.is_running)
