"""Workspace page composed from Fluent cards and controls."""

import copy
from collections import Counter
from pathlib import Path
import threading
import time
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QSplitter, QVBoxLayout, QWidget, QScrollArea, QLayout, QSizePolicy
from qfluentwidgets import CaptionLabel, FluentIcon as FIF, PrimaryPushButton, PushButton, TitleLabel, ScrollArea, InfoBar, ProgressBar, SwitchButton, IconWidget
from qframelesswindow import FramelessDialog
from core.task_manager import TaskManager, TERMINAL, ACTIVE, stamp
from core.matcher import StoryboardMatcher
from core.background import BackgroundJobs
from core.prompt_detector import annotate_tasks
from core.task_state import parameters_for_model
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
from ..widgets.workspace_log import WorkspaceLog
from ..widgets.params_card import ParamsCard
from ..widgets.recent_completed_panel import RecentCompletedPanel
from ..widgets.workspace_task_table import WorkspaceTaskTable
from ..widgets.workspace_summary import WorkspaceSummary, directory_metrics
from ..widgets.workspace_directory_bar import WorkspaceDirectoryBar
from ..widgets.workspace_surface import ImagePreview, BreathingDot, label, style_button, BLUE, GREEN, MUTED, RED


class WorkspacePage(QWidget):
    history_changed = pyqtSignal(object)
    navigation_requested = pyqtSignal(str)
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
        self._started_at = None
        self._elapsed = 0
        self._metrics_busy = False
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
        self.queue_panel.reorder_requested.connect(self.reorder_references)
        self.queue_panel.preview_requested.connect(self.preview_image)
        self.queue_panel.action_requested.connect(self.task_action)
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
        self.params_card.values_changed.connect(lambda *_: self._refresh_row_parameters())
        self.recent_panel.update_history(config_manager.config['history'])
        self._running_changed(False)
        self.append_log('工作台已加载：支持模型池、自动重试和并发控制；关闭模型池时使用工作台所选模型', 'info')
        QTimer.singleShot(0, self.scan_sources)
        self.stats_timer = QTimer(self); self.stats_timer.setInterval(1000)
        self.stats_timer.timeout.connect(self._update_summary); self.stats_timer.start()
        self.metrics_timer = QTimer(self); self.metrics_timer.setInterval(15000)
        self.metrics_timer.timeout.connect(self.refresh_metrics); self.metrics_timer.start()
        QTimer.singleShot(0, self.refresh_metrics)

    def _build_ui(self):
        root = QVBoxLayout(self); root.setContentsMargins(20, 0, 20, 12); root.setSpacing(16)
        self.toolbar = QWidget(); self.toolbar.setFixedHeight(48)
        header = QHBoxLayout(self.toolbar); header.setContentsMargins(0, 0, 0, 0); header.setSpacing(12)
        logo = IconWidget(FIF.VIDEO); logo.setFixedSize(23, 23); header.addWidget(logo)
        header.addWidget(label('StoryboardVideoStudio', 16, '#f5f5f5', True)); header.addStretch(1)
        self.navigation_tabs = {}
        for key, text in [('workspace', '工作台'), ('history', '任务历史'), ('settings', '设置')]:
            button = PushButton(text); button.setProperty('studioStyled', True); button.setFixedSize(76, 44)
            button.setStyleSheet('QPushButton {color:' + ('#f5f5f5' if key == 'workspace' else '#71717a') +
                                 '; background:transparent; border:0; border-bottom:' + ('2px solid #3b82f6' if key == 'workspace' else '2px solid transparent') + '; font-size:12px;} QPushButton:hover {color:white;}')
            button.clicked.connect(lambda checked=False, route=key: self.navigation_requested.emit(route))
            self.navigation_tabs[key] = button; header.addWidget(button)
        for button in self.navigation_tabs.values():
            button.hide()
        self.status_line = QHBoxLayout(); self.status_line.setSpacing(12)
        self.status_dot = BreathingDot(BLUE, active=False); self.status_dot.setFixedSize(14, 18)
        self.status_text = label('就绪', 12, '#8b8b9e', True)
        self.status_line.addWidget(self.status_dot); self.status_line.addWidget(self.status_text)
        self.product_status = label('产品 0/0', 12, '#8b8b9e'); self.task_status = label('任务 0/0', 12, '#8b8b9e')
        self.concurrent_status = label('并发 1', 12, '#8b8b9e'); self.elapsed_status = label('已运行 00:00:00', 12, '#8b8b9e', mono=True)
        for widget in (self.product_status, self.task_status, self.concurrent_status, self.elapsed_status):
            self.status_line.addWidget(widget)
        header.insertLayout(1, self.status_line)
        header.addStretch(1)
        self.api_dot = BreathingDot(MUTED); self.api_status = label('未检测', 11)
        self.api_status.setToolTip('连接状态来自本次上游模型同步结果；上传鉴权请在设置中测试')
        header.addWidget(self.api_dot); header.addWidget(self.api_status)
        self.auto_save = SwitchButton(); self.auto_save.setOnText(''); self.auto_save.setOffText('')
        view = self.config_manager.config.get('workspace_view', {})
        self.auto_save.setChecked(view.get('auto_save', True))
        self.auto_save.setToolTip('自动保存日志高度与任务筛选；参考图顺序、生成参数和任务记录始终即时保存')
        header.addWidget(label('自动保存', 11)); header.addWidget(self.auto_save)
        self.start_button = style_button(PrimaryPushButton(FIF.PLAY, '开始生成'), primary=True)
        self.pause_button = style_button(PushButton(FIF.PAUSE, '暂停'))
        for button in (self.start_button, self.pause_button):
            button.setFixedHeight(34); header.addWidget(button)
        root.addWidget(self.toolbar)
        self.summary = WorkspaceSummary(); self.summary.update_paths(self.config_manager.config['paths'])
        self.summary.directory_requested.connect(self.choose_directory); root.addWidget(self.summary)
        self.directory_bar = WorkspaceDirectoryBar()
        self.directory_bar.choose_requested.connect(self.choose_directory)
        root.addWidget(self.directory_bar)
        self.product_progress_label = label('当前：—，总进度：产品 0/0', 11)
        self.product_progress = ProgressBar()
        self.product_progress.hide()
        self.product_progress.setRange(0, 1)
        self.product_progress.setValue(0)
        self.status_row = QHBoxLayout(); self.status_row.addWidget(self.product_progress_label); self.status_row.addStretch(1)
        self.splitter = QSplitter(Qt.Vertical); self.splitter.setHandleWidth(8); self.splitter.setChildrenCollapsible(False)
        self.queue_panel = WorkspaceTaskTable(self.config_manager.config['workspace'])
        self.cancel_button = self.queue_panel.cancel_button
        self.splitter.addWidget(self.queue_panel)
        self.log_drawer = WorkspaceLog(); self.log_drawer.setMinimumHeight(44)
        self.log_drawer.header.insertLayout(1, self.status_row)
        self.splitter.addWidget(self.log_drawer)
        self.splitter.setStretchFactor(0, 1); self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([600, view.get('log_height', 140)])
        root.addWidget(self.splitter, 1)
        self.splitter.splitterMoved.connect(self._save_view)
        self.auto_save.checkedChanged.connect(self._auto_save_changed)
        self.queue_panel.filter_box.setCurrentText(view.get('task_filter', '全部'))
        self.queue_panel.filter_box.currentTextChanged.connect(self._save_view)
        self.log_drawer.view_changed.connect(self._log_toggled)
        # Existing parameter, matching and submission recovery controls remain
        # accessible in a workspace tool dialog, without occupying table space.
        self.controls_dialog = FramelessDialog(self)
        self.controls_dialog.setObjectName('workspaceControls'); self.controls_dialog.setWindowTitle('生成参数与任务详情')
        self.controls_dialog.resize(900, 760)
        dialog_layout = QVBoxLayout(self.controls_dialog); dialog_layout.setContentsMargins(20, 40, 20, 20)
        center = QWidget(); center_layout = QVBoxLayout(center); center_layout.setContentsMargins(8, 8, 8, 8); center_layout.setSpacing(16)
        self.data_source = DataSourceCard(self.config_manager, self.append_log); self.params_card = ParamsCard(self.config_manager); self.current_task = CurrentTaskCard(self.append_log)
        # Wire the match-details action only after the data source card exists.
        self.directory_bar.match_requested.connect(self.data_source.open_match_dialog)
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
        self.center_scroll.setWidget(center)
        self.recent_panel = RecentCompletedPanel(self.append_log)
        center_layout.addWidget(self.recent_panel)
        dialog_layout.addWidget(self.center_scroll)
        close = style_button(PushButton('关闭')); close.clicked.connect(self.controls_dialog.accept); dialog_layout.addWidget(close, 0, Qt.AlignRight)
        self.controls_dialog.setStyleSheet('#workspaceControls {background:#101014;}')
        self.queue_panel.params_button.clicked.connect(self.open_controls)
        self.queue_panel.match_button.clicked.connect(self.data_source.open_match_dialog)
        self.set_debug_mode(self.config_manager.config.get('diagnostics', {}).get('debug_mode', False))
        self.params_card.parameters_adjusted.connect(self.append_log)

    def paintEvent(self, event):
        from PyQt5.QtGui import QPainter, QColor
        painter = QPainter(self); painter.fillRect(self.rect(), QColor('#0d0d10'))

    def open_controls(self):
        self.controls_dialog.show(); self.controls_dialog.raise_(); self.controls_dialog.activateWindow()

    def choose_directory(self, key):
        if not self.task_manager.is_running:
            self.data_source._choose(key, self.data_source.fields[key])

    def _auto_save_changed(self, enabled):
        self.config_manager.update(('workspace_view', 'auto_save'), bool(enabled))
        self._save_view()

    def _save_view(self, *_):
        if self.auto_save.isChecked():
            current = dict(self.config_manager.config.get('workspace_view', {}))
            current.update(auto_save=True, log_height=self.log_drawer.height() if self.log_drawer._expanded else self.log_drawer._saved_height,
                           task_filter=self.queue_panel.filter_box.currentText())
            self.config_manager.update(('workspace_view',), current)

    def _log_toggled(self):
        sizes = self.splitter.sizes(); height = self.log_drawer._saved_height if self.log_drawer._expanded else 44
        self.splitter.setSizes([max(100, sum(sizes)-height), height]); self._save_view()

    def set_api_connection(self, connected, message=''):
        color = GREEN if connected else RED
        self.api_dot.setTextColor(color, color)
        self.api_status.setText('已连接' if connected else '离线缓存')
        self.api_status.setToolTip(message or '已验证模型列表接口；上传鉴权请在设置中测试')

    def refresh_api_state(self):
        controller = getattr(self.config_manager, 'model_catalog_controller', None)
        identity = controller.identity() if controller else None
        if identity != getattr(self, '_api_identity', None):
            self._api_identity = identity
            self.api_status.setText('未检测' if identity and identity[1] else '未配置')
            self.api_dot.setTextColor(MUTED, MUTED)
        if controller and controller.syncing:
            self.api_status.setText('检测中')

    def _refresh_row_parameters(self):
        for row in self.queue_panel.rows:
            row.defaults = self.config_manager.config['workspace']
        self._tasks_updated(self.queue_panel._tasks)

    def refresh_metrics(self):
        if self.closing.is_set() or self._metrics_busy:
            return
        self._metrics_busy = True
        paths = dict(self.config_manager.config['paths'])
        self.summary.update_paths(paths)
        self.directory_bar.refresh(paths, self.queue_panel._tasks)
        def done(metrics):
            self._metrics_busy = False
            if not self.closing.is_set() and paths == self.config_manager.config['paths']:
                self.summary.update_metrics(metrics)
                self.directory_bar.refresh(paths, self.queue_panel._tasks, metrics)
            elif not self.closing.is_set():
                QTimer.singleShot(0, self.refresh_metrics)
        def failed(message):
            self._metrics_busy = False
            self.append_log('目录统计暂不可用：' + message, 'warning')
        self.jobs.start(lambda: directory_metrics(paths), done, failed)

    def _update_summary(self):
        elapsed = time.monotonic()-self._started_at if self._started_at is not None else self._elapsed
        self.summary.update_tasks(self.queue_panel._tasks, self.config_manager.config['history'], elapsed, self.task_manager.is_running)

    def reorder_references(self, index, paths):
        tasks = self.queue_panel._tasks
        if self.task_manager.is_running or self._redownloading or not 0 <= index < len(tasks):
            return
        task = tasks[index]
        if task.get('status') in ACTIVE or Counter(paths) != Counter(task.get('images', [])):
            self.append_log('参考图顺序未保存：排序不能增加或移除图片', 'warning'); return
        key = task.get('prompt_path')
        if not key:
            return
        previous = dict(self.config_manager.config.get('match_overrides', {}))
        overrides = dict(previous); overrides[key] = list(paths)
        if not self.config_manager.update(('match_overrides',), overrides):
            self.config_manager.update(('match_overrides',), previous, save=False)
            return
        task = copy.deepcopy(task); task['images'] = list(paths)
        updated = list(tasks); updated[index] = task; self._tasks_updated(updated)
        for match in self.data_source.matches:
            if match.get('prompt_path') == key:
                match['images'] = list(paths)
        self.append_log(f'{task["prompt_name"]}：已保存 {len(paths)} 张参考图顺序，对应 Picture 1–{len(paths)}', 'success')

    def preview_image(self, path):
        if hasattr(self, 'image_preview'):
            self.image_preview.close(); self.image_preview.deleteLater()
        self.image_preview = ImagePreview(path, self.window()); self.image_preview.show()

    def task_action(self, index, action):
        if not 0 <= index < len(self.queue_panel._tasks):
            return
        task = self.queue_panel._tasks[index]
        if action == 'menu':
            menu = self.queue_panel.action_menu(index)
            button = self.queue_panel.rows[index].more_button
            menu.exec(button.mapToGlobal(button.rect().bottomLeft())); menu.deleteLater()
        elif action == 'download':
            self.redownload(task)
        elif action == 'folder':
            path = task.get('result_path') or task.get('output_dir') or self.config_manager.config['paths']['output']
            if path:
                # A moved/deleted output video must not hide its existing folder.
                directory = str(Path(path).parent) if task.get('result_path') else path
                open_local(directory, self.append_log)
        elif action == 'logs':
            self.log_drawer.focus_task(task['prompt_name'])
        elif action == 'details':
            self.current_task.update_task(index, {**task, 'model': task.get('model') or task.get('requested_model', '')})
            self.open_controls(); self.center_scroll.ensureWidgetVisible(self.current_task)
        elif action == 'skip':
            if self.task_manager.is_running and task.get('status') in ACTIVE:
                self.task_manager.select_current(index); self.task_manager.skip_current()
        elif action == 'retry':
            if self.task_manager.is_running or self._redownloading:
                return
            if task.get('status') == 'submission_unknown':
                self.resolve_submission(task); return
            if task.get('status') == 'completed':
                self.regenerate(task); return
            if not task.get('prompt_path'):
                return
            config = runtime_config(self.config_manager)
            config['_only_prompt_paths'] = [task['prompt_path']]
            try:
                # The normal ledger still decides resume/duplicate/uncertain;
                # retry never bypasses safety or manufactures a new signature.
                self.task_manager.start_tasks(config)
            except Exception as error:
                self.append_log(f'无法重试：{error}', 'error')

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
        self.summary.update_paths(self.config_manager.config['paths'])
        self.refresh_metrics()
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
        worker = self.task_manager.worker
        if running and worker is not None and worker is not getattr(self, '_progress_worker', None):
            # Download progress does not publish a full queue snapshot. Listen
            # to every indexed worker update, including nonselected concurrent jobs.
            worker.indexed_progress.connect(self._row_progress)
            self._progress_worker = worker
        if running and self._started_at is None:
            self._started_at = time.monotonic()
        elif not running and self._started_at is not None:
            self._elapsed = time.monotonic()-self._started_at; self._started_at = None
        self.queue_panel.busy = running or self._redownloading
        for card in self.summary.cards[:3]:
            card.choose_button.setEnabled(not running)
        self.queue_panel.match_button.setEnabled(not running)
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
        self._update_summary()

    def _row_progress(self, index, value, elapsed, eta):
        if self.closing.is_set() or not 0 <= index < len(self.queue_panel.rows):
            return
        row = self.queue_panel.rows[index]
        row.task.update(progress=value, elapsed=elapsed, eta=eta)
        self.queue_panel._tasks[index].update(progress=value, elapsed=elapsed, eta=eta)
        row.progress.setValue(max(0, min(100, int(value))))
        row.percentage.setText(f'{value:.0f}%')

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
        config = self.config_manager.config
        catalog = catalog_snapshot(self.config_manager)
        self.queue_panel.set_model_catalog(catalog, config['workspace']['model'])
        displayed = []
        for task in tasks:
            view = dict(task)
            model = task.get('model') or task.get('requested_model') or config['workspace']['model']
            view['_display_parameters'] = task.get('effective_parameters') or parameters_for_model(
                model, config['workspace'], config['model_pool'].get('enabled') or task.get('model_source') in {'auto', 'manual', 'fallback'},
                len(task.get('images', [])), catalog)
            displayed.append(view)
        self.queue_panel.update_tasks(displayed)
        self.current_task.update_counts(sum(t.get('status') in ACTIVE for t in tasks),
                                        sum(t.get('status', 'waiting') == 'waiting' for t in tasks))
        total = len(tasks)
        self.product_progress.setRange(0, max(1, total))
        self.product_progress.setValue(sum(task.get('status') in TERMINAL for task in tasks))
        if not tasks:
            self.product_progress_label.setText('当前：—，总进度：产品 0/0')
        self._update_summary()

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
        self._update_summary()
        self.refresh_metrics()
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
        self.stats_timer.stop(); self.metrics_timer.stop()
        self.controls_dialog.close()
        if hasattr(self, 'image_preview'):
            self.image_preview.close()
        self._scan_version += 1
        self.task_manager.cancel_all()
        self._running_changed(self.task_manager.is_running)
