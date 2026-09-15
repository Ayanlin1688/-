"""Product-aware bounded task coordination with isolated HTTP execution."""
import copy
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from itertools import groupby
from pathlib import Path
import threading
import uuid

from PyQt5.QtCore import QObject, QThread, pyqtSignal
from .api_client import ApiClient, GROK, V3_MODELS
from .http_client import Cancelled
from .image_uploader import ImageUploader
from .reference_diagnostics import ReferenceDiagnostics
from .matcher import StoryboardMatcher
from .log_redaction import redact_text
from .model_pool import ModelPool
from .task_state import (ACTIVE, TERMINAL, STATUS_TEXT, TaskControl, stamp, resolve_output_directory,
                         parameters_for_model, task_signature, prompt_content, prompt_sha256, submission_images)
from .task_execution import TaskExecution
from .prompt_detector import SOURCE_TEXT, annotate_tasks
from .prompt_processor import reference_warnings
from .submission_ledger import SubmissionLedger, account_scope, ledger_path
from .submission_safety import SubmissionGate
from .disk_guard import cleanup_disk


class TaskWorker(QThread):
    task_list_updated = pyqtSignal(object)
    current_task_changed = pyqtSignal(int, object)
    task_progress = pyqtSignal(float, float, float)
    indexed_progress = pyqtSignal(int, float, float, float)
    pool_updated = pyqtSignal(object)
    log_message = pyqtSignal(str, str)
    record_updated = pyqtSignal(object)
    pause_changed = pyqtSignal(bool)
    summary = pyqtSignal(int, int)

    def __init__(self, config, previous, parent=None):
        super().__init__(parent)
        self.config = copy.deepcopy(config)
        for key in ('base_url', 'api_key', 'upload_url', 'upload_api_key'):
            self.config['api'][key] = self.config['api'].get(key, '').strip()
        self.previous = copy.deepcopy(previous)
        self.control = TaskControl()
        self.tasks = []
        self._requested_consumed = set()
        self.index = -1
        self.lock = threading.RLock()
        self.debug_mode = bool(self.config.get('diagnostics', {}).get('debug_mode', False))
        self.max_concurrency = max(1, min(5, int(self.config['task_strategy'].get('max_concurrency', 1))))
        try:
            probe_seconds = float(self.config.get('_submission_probe_seconds') or 60)
        except (TypeError, ValueError):
            probe_seconds = 60.0
        self.gate = SubmissionGate(self.max_concurrency, probe_seconds=probe_seconds)
        self.gate.on_recover = self._gate_recovered
        self.ledger = None
        self.scope = account_scope(self.config)
        pool_settings = copy.deepcopy(self.config['model_pool'])
        catalog = self.config.get('_model_catalog')
        if pool_settings.get('enabled') and isinstance(catalog, dict):
            pool_settings['models'] = [
                entry for entry in pool_settings.get('models', [])
                if isinstance(entry, dict)
                and isinstance(catalog.get(entry.get('name')), dict)
                and catalog[entry['name']].get('available', True)
                and catalog[entry['name']].get('kind') == 'video'
                and catalog[entry['name']].get('protocol_known', False)
            ]
        self.pool = ModelPool(pool_settings, self.config['workspace']['model'],
                              failure_threshold=self.config['task_strategy'].get('failure_skip_threshold', 3),
                              log=self.log, on_change=self.pool_updated.emit)

    @staticmethod
    def _uses_task_model(task):
        return task.get('model_source') in {'auto', 'manual', 'fallback'}

    def _parameters(self, task, model):
        return parameters_for_model(
            model, self.config['workspace'], self.pool.enabled or self._uses_task_model(task),
            len(submission_images(task, model, self.config.get('_model_catalog'))), self.config.get('_model_catalog'))

    def redact(self, message):
        api = self.config['api']
        return redact_text(message, (api.get('api_key'), api.get('upload_api_key')))

    def log(self, message, level='info'):
        if level.lower() == 'debug' and not self.debug_mode:
            return
        self.log_message.emit(self.redact(message), level)

    def publish(self, record=False):
        with self.lock:
            self.task_list_updated.emit(copy.deepcopy(self.tasks))
            if 0 <= self.index < len(self.tasks):
                task = copy.deepcopy(self.tasks[self.index])
                self.current_task_changed.emit(self.index, task)
                if record:
                    self.record_updated.emit({key: value for key, value in task.items() if not key.startswith('_')})

    def update_task(self, index, task, record=False):
        with self.lock:
            self.tasks[index] = copy.deepcopy(task)
            self.index = index
            self.publish(record)

    def _gate_recovered(self):
        """限流冷却到点：自动试探恢复，不再要求人工点击。"""
        self.log(f'限流冷却结束：自动恢复提交（当前并发上限 {self.gate.limit}）', 'info')
        self.pause_changed.emit(False)

    def submission_error(self, status_code):
        seconds = self.gate.failed(status_code)
        if seconds:
            self.log(f'HTTP {status_code}：新提交退避{seconds}秒，当前并发上限{self.gate.limit}；已有任务继续轮询', 'warning')
        if self.gate.paused:
            self.log(f'API限流：已暂停新任务提交，约 {max(1, int(self.gate.pause_remaining()))} 秒后自动试探恢复（无需人工操作）', 'warning')
            self.pause_changed.emit(True)
        return seconds

    def progress_for(self, index, value, elapsed, eta):
        with self.lock:
            self.tasks[index].update(progress=value, elapsed=elapsed, eta=eta)
            self.indexed_progress.emit(index, value, elapsed, eta)
            if self.max_concurrency == 1:
                self.task_progress.emit(value, elapsed, eta)

    def terminal(self, task, status, error=''):
        task.update(status=status, error=self.redact(error), finished_at=stamp())
        self.publish(record=True)

    def _quarantine_legacy(self, task, old):
        """Keep an unowned paid ID as evidence, never as a current-account ID."""
        for record in reversed(self.ledger.records(self.scope)):
            if (record.get('legacy_signature') == old['signature']
                    and record.get('legacy_task_id') == old.get('task_id')):
                # A user's explicit noncreation decision must survive old history.
                return None if record['ledger_state'] == 'released' else record
        model = old.get('model') or task['model']
        frozen = old.get('submission_model')
        catalog = {model: frozen} if isinstance(frozen, dict) else self.config.get('_model_catalog')
        params = old.get('effective_parameters') or self._parameters(task, model)
        pending = dict(task, model=model, task_id='', result_url='', result_path='',
                       status='submission_unknown', api_scope=self.scope,
                       legacy_task_id=old.get('task_id', ''), legacy_signature=old['signature'],
                       effective_parameters=copy.deepcopy(params), submission_model=copy.deepcopy(frozen),
                       error='旧记录的账号归属未确认，已阻止创建和查询；请核对当前账号及候选任务ID')
        pending['signature'] = task_signature(task, model, params, self.config['api']['base_url'], catalog)
        outcome, record = self.ledger.reserve(pending, self.scope)
        if outcome == 'claimed':
            self.ledger.save(record, 'unknown')
            record['ledger_state'] = 'unknown'
            self.record_updated.emit({key: value for key, value in record.items() if not key.startswith('_')})
        return record

    def _cleanup_disk(self):
        """无人值守磁盘自检：清理过期临时文件并在空间不足时预警。"""
        try:
            strategy = self.config.get('task_strategy', {})
            cleanup_disk(self.config.get('paths', {}), self.log,
                         retention_days=int(strategy.get('disk_cleanup_days', 7) or 7),
                         min_free_gb=float(strategy.get('disk_min_free_gb', 2) or 2))
        except Exception as error:
            self.log(f'磁盘清理自检未完成：{error}', 'warning')

    def _scan(self):
        paths = self.config['paths']
        self.log(f"开始扫描目录：提示词={paths['prompts']}, 图片={paths.get('images', '')}")
        matcher = StoryboardMatcher.from_config(self.config)
        self.tasks = matcher.scan_and_match(paths)
        if self.config.get('_only_prompt_paths'):
            selected = {str(Path(path).resolve()) for path in self.config['_only_prompt_paths']}
            self.tasks = [task for task in self.tasks if str(Path(task['prompt_path']).resolve()) in selected]
        annotate_tasks(self.tasks, self.config)
        for warning in matcher.warnings:
            self.log(warning, 'warning')
        if self.config.get('_task_limit'):
            self.tasks = self.tasks[:int(self.config['_task_limit'])]
        self.ledger = SubmissionLedger(ledger_path(self.config))
        # The ledger's scope column is authoritative for older ledger records.
        saved = [dict(record, api_scope=self.scope) for record in self.ledger.records(self.scope)]
        previous = {t.get('signature'): t for t in [*self.config.get('history', []), *self.previous, *saved]
                    if t.get('signature') and (not t.get('api_scope') or t['api_scope'] == self.scope)}
        for sequence, task in enumerate(self.tasks, 1):
            try:
                task['_original_prompt'] = prompt_content(task)
            except (OSError, UnicodeError) as error:
                # Keep a bad file local to its task, as execution did before
                # source hashing moved into the batch scan.
                task['_original_prompt'] = ''
                task['prompt_read_error'] = '读取提示词失败：' + str(error)
            task['prompt_sha256'] = prompt_sha256(task)
            model = task.get('requested_model') or self.config['workspace']['model']
            params = self._parameters(task, model)
            signature = task_signature(task, model, params, self.config['api']['base_url'], self.config.get('_model_catalog'))
            output_dir = resolve_output_directory(paths['output'], task.get('output_subdir', ''))
            task.update(status='waiting', task_id='', model=model, retry_count=0, attempts=[], result_path='',
                        local_id=uuid.uuid4().hex, signature=signature, created_at='', finished_at='', error='',
                        api_base_url=self.config['api']['base_url'].rstrip('/'), output_dir=output_dir, sequence=sequence)
            task['api_scope'] = self.scope
            if task.get('model_locked'):
                candidate_models = [model]
            elif self._uses_task_model(task):
                candidate_models = [model, *self.pool.names]
            else:
                candidate_models = list(self.pool.names)
            candidates = []
            for candidate in dict.fromkeys(candidate_models):
                try:
                    params = self._parameters(task, candidate)
                    key = task_signature(task, candidate, params, self.config['api']['base_url'], self.config.get('_model_catalog'))
                    old = previous.get(key) or previous.get(task_signature(task, candidate, params, self.config['api']['base_url'], self.config.get('_model_catalog'), legacy=True))
                    if old and old.get('ledger_state') != 'released' and (old.get('task_id') or old.get('ledger_state') in {'unknown', 'submitting', 'reserved'}):
                        candidates.append(old)
                except ValueError:
                    continue
            # Delisting must never erase an existing paid task's identity.
            # Recompute against its frozen capabilities, but honor changed inputs
            # and an explicit new model choice.
            for old in previous.values():
                historical_model = old.get('model')
                if not old.get('task_id') or old.get('prompt_path') != task['prompt_path']:
                    continue
                current_catalog = self.config.get('_model_catalog')
                if not isinstance(current_catalog, dict):
                    continue
                current_record = current_catalog.get(historical_model)
                if current_record and current_record.get('available', True):
                    continue
                if (task.get('model_locked') or (not self._uses_task_model(task) and not self.pool.enabled)) and historical_model != model:
                    continue
                if self._uses_task_model(task) and old.get('requested_model', model) != model:
                    continue
                record = old.get('submission_model')
                historical_catalog = {historical_model: record} if isinstance(record, dict) else None
                try:
                    params = parameters_for_model(historical_model, self.config['workspace'],
                                                  self.pool.enabled or self._uses_task_model(task),
                                                  len(task['images']), historical_catalog)
                    key = task_signature(task, historical_model, params, self.config['api']['base_url'], historical_catalog)
                    if key == old.get('signature'):
                        candidates.append(old)
                except ValueError:
                    continue
            scoped = [record for record in candidates if record.get('api_scope') == self.scope]
            old = scoped[-1] if scoped else candidates[-1] if candidates else None
            if old and not old.get('api_scope') and not task.get('skip_reason'):
                old = self._quarantine_legacy(task, old)
            if old and not task.get('skip_reason'):
                if old.get('status') == 'completed' and (not self.config['task_strategy'].get('prevent_duplicates', True)
                        or task['signature'] in self.config.get('_rerun_signatures', []) or old.get('signature') in self.config.get('_rerun_signatures', [])):
                    continue
                product_fields = {key: task[key] for key in ('product', 'product_index', 'product_total', 'product_task_index',
                                 'product_task_total', 'output_subdir', 'skip_reason', 'output_dir', 'sequence')}
                decision_fields = {key: task[key] for key in (
                    'detected_model', 'requested_model', 'model_source', 'model_locked', 'model_detection_error')
                    if key in task}
                original = task['_original_prompt']
                content_sha = task['prompt_sha256']
                task.update(copy.deepcopy(old)); task.update(product_fields); task.update(decision_fields)
                task.update(_original_prompt=original, prompt_sha256=content_sha)
                if not task.get('task_id'):
                    task.update(status='submission_unknown', error=old.get('error') or '提交结果未确认，禁止再次创建；请先核对服务端')
                elif old['status'] == 'completed':
                    task.update(status='duplicate', duplicate_of=old.get('local_id'), duplicate_record=copy.deepcopy(old),
                                error='检测到相同任务，跳过避免重复扣费')
                else:
                    task.update(status='queued', error='')
        matched = sum(t['matched'] for t in self.tasks)
        self.log(f'匹配完成：共{len(self.tasks)}个提示词，{matched}个已匹配，{len(self.tasks)-matched}个未匹配')
        misses = []
        for index, task in enumerate(self.tasks, 1):
            missing = []
            if not task.get('images'):
                missing.append('未匹配到参考图')
            else:
                try:
                    missing.extend(reference_warnings(task.get('_original_prompt') or '', len(task['images'])))
                except (TypeError, ValueError, OSError):
                    pass
            if missing:
                task['asset_missing'] = missing
                misses.append((index, Path(task.get('prompt_path', '')).name or task.get('prompt_name', ''), missing))
        if misses:
            self.log(f'资产漏检告警：{len(misses)}/{len(self.tasks)} 个任务存在资产缺失；已逐条标记，队列继续执行不中断', 'warning')
            for index, name, missing in misses[:20]:
                self.log(f'  漏检任务{index}：{name} —— ' + '；'.join(missing), 'warning')
            if len(misses) > 20:
                self.log(f'  其余 {len(misses) - 20} 个漏检任务明细见任务列表与历史记录', 'warning')
        self.publish()
        self.pool_updated.emit(self.pool.snapshot())

    def _pick(self, index):
        """Bind cooldown waiting to a selectable, independently skippable task."""
        warned = False
        self.control.begin(index)
        try:
            while True:
                self.control.before_task()
                task = self.tasks[index]
                requested = task.get('requested_model') or task.get('model') or self.config['workspace']['model']
                # Per-prompt recognition/manual override gets first attempt. A
                # manual override is permanently locked; automatic recognition
                # may fall back to the pool after a failed attempt.
                preferred = None
                if self._uses_task_model(task) and index not in self._requested_consumed:
                    if requested not in self.pool.names:
                        self._requested_consumed.add(index)
                        return requested
                    preferred = requested
                model = self.pool.pick(preferred=preferred)
                if model is not None:
                    if preferred is not None:
                        self._requested_consumed.add(index)
                    return model
                if not warned:
                    self.log('所有启用模型都在冷却，等待最快恢复的模型', 'warning'); warned = True
                    task = copy.deepcopy(self.tasks[index]); task['status'] = 'cooling'
                    self.update_task(index, task)
                self.control.delay(min(.2, max(.01, self.pool.wait_seconds(preferred=preferred))))
        except Cancelled:
            if self.control.cancelled:
                raise
            task = copy.deepcopy(self.tasks[index])
            task.update(status='skipped', error='已跳过冷却等待', finished_at=stamp())
            self.update_task(index, task, record=True)
            self.log(f'任务{index+1}：已跳过冷却等待', 'warning')
            return None
        finally:
            self.control.release(index)

    def _execute(self, index, model):
        task = copy.deepcopy(self.tasks[index])
        source = task.get('model_source', 'workspace')
        if source == 'workspace' and self.pool.enabled:
            source = 'pool'
        self.log(f'任务{index+1}使用模型 {model}（{SOURCE_TEXT.get(source, source)}）')
        task.update(model=model, status='queued')
        self.update_task(index, task)
        TaskExecution(self, index, task, model).run()

    def run(self):
        try:
            self._cleanup_disk()
            self._scan()
            if not self.tasks:
                self.log('目录中没有 .txt 提示词', 'warning')
            if not self.pool.names and any(not self._uses_task_model(task) for task in self.tasks):
                raise ValueError('模型池没有启用的模型，请启用至少一个模型或关闭模型池')
            groups = [(product, [i for i, _ in rows])
                      for product, rows in groupby(enumerate(self.tasks), key=lambda pair: pair[1].get('product', ''))]
            if self.max_concurrency == 1:
                # 串行模式保持逐产品推进与完整边界日志（可在设置中改回 1）。
                for product, indices in groups:
                    self.log(f'开始处理产品：{product or "未分组"}，共{len(indices)}个任务；最大并发{self.max_concurrency}')
                    pending = []
                    for index in indices:
                        task = self.tasks[index]
                        if task.get('status') in {'duplicate', 'submission_unknown'}:
                            self.log(task.get('error', ''), 'warning')
                        else:
                            pending.append(index)
                    for index in pending:
                        model = self.tasks[index]['model'] if self.tasks[index].get('task_id') else self._pick(index)
                        if model is not None:
                            self._execute(index, model)
                        self.control.check()
                    self.control.check()
                    self.log(f'产品处理结束：{product or "未分组"}')
            else:
                # 无人值守并发：全部产品共享一个队列，任务完成立即补位（默认 5 路）。
                pending = []
                for product, indices in groups:
                    self.log(f'开始处理产品：{product or "未分组"}，共{len(indices)}个任务；最大并发{self.max_concurrency}')
                    for index in indices:
                        task = self.tasks[index]
                        if task.get('status') in {'duplicate', 'submission_unknown'}:
                            self.log(task.get('error', ''), 'warning')
                        else:
                            pending.append(index)
                if pending:
                    self._concurrent_batch(pending)
                    self.control.check()
                self.log('队列处理完毕：本轮全部任务已结束')
        except Cancelled:
            self.log('已取消后续排队任务', 'warning')
        except Exception as error:
            self.log(f'任务队列无法继续：{error}', 'error')
            with self.lock:
                for index, task in enumerate(self.tasks):
                    if task.get('status') not in TERMINAL:
                        self.index = index; self.terminal(task, 'failed', str(error))
        finally:
            with self.lock:
                if self.control.cancelled:
                    for index, task in enumerate(self.tasks):
                        if task.get('status') not in TERMINAL:
                            self.index = index; self.terminal(task, 'cancelled', '用户取消全部')
                self.summary.emit(sum(t.get('status') == 'completed' for t in self.tasks), sum(t.get('status') == 'failed' for t in self.tasks))

    def _concurrent_batch(self, pending):
        pending = iter(pending)
        active = set()
        exhausted = False
        with ThreadPoolExecutor(max_workers=self.max_concurrency, thread_name_prefix='storyboard-task') as executor:
            while active or not exhausted:
                self.control.check()
                while len(active) < self.gate.limit and not exhausted and not self.control.paused and not self.gate.paused and self.gate.remaining() <= 0:
                    try:
                        index = next(pending)
                    except StopIteration:
                        exhausted = True; break
                    model = self.tasks[index]['model'] if self.tasks[index].get('task_id') else self._pick(index)
                    if model is not None:
                        active.add(executor.submit(self._execute, index, model))
                if active:
                    finished, active = wait(active, timeout=.05, return_when=FIRST_COMPLETED)
                    for future in finished:
                        future.result()
                elif not exhausted:
                    self.control.before_task()
                    if self.gate.paused or self.gate.remaining() > 0:
                        # 限流暂停 / 退避期间按冷却时钟等待；到点自动试探恢复（可被取消/跳过打断）。
                        self.gate.wait(self.control)
                    else:
                        self.control.delay(.05)


class TaskManager(QObject):
    task_list_updated = pyqtSignal(object)
    current_task_changed = pyqtSignal(int, object)
    task_progress = pyqtSignal(float, float, float)
    pool_updated = pyqtSignal(object)
    log_message = pyqtSignal(str, str)
    all_finished = pyqtSignal(int, int)
    record_updated = pyqtSignal(object)
    running_changed = pyqtSignal(bool)
    pause_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tasks = []
        self.is_running = False
        self.is_paused = False
        self.current_index = -1
        self.worker = None
        self._counts = (0, 0)

    def start_tasks(self, config):
        if self.is_running:
            self.log_message.emit('检测到相同任务，跳过避免重复扣费；当前队列正在运行', 'warning')
            return False
        if not config['api']['api_key'].strip():
            raise ValueError('请先在设置中填写 API Key')
        if not config['paths']['prompts'] or not config['paths']['output']:
            raise ValueError('请选择提示词目录和视频保存目录')
        self.worker = TaskWorker(config, self.tasks, self)
        self.worker.task_list_updated.connect(self._list)
        self.worker.current_task_changed.connect(self._current)
        self.worker.indexed_progress.connect(self._progress)
        self.worker.pool_updated.connect(self.pool_updated)
        self.worker.log_message.connect(self.log_message)
        self.worker.record_updated.connect(self.record_updated)
        self.worker.pause_changed.connect(self._paused)
        self.worker.summary.connect(self._summary)
        self.worker.finished.connect(self._finished)
        self.is_running = True
        self.current_index = -1
        self._paused(False)
        self.running_changed.emit(True)
        self.worker.start()
        return True

    def _list(self, tasks):
        self.tasks = tasks
        self.task_list_updated.emit(copy.deepcopy(tasks))

    def _current(self, index, task):
        selected = self.tasks[self.current_index] if 0 <= self.current_index < len(self.tasks) else None
        if index == self.current_index or selected is None or selected.get('status') not in ACTIVE:
            self.current_index = index
            self.current_task_changed.emit(index, task)
            self.task_progress.emit(task.get('progress', 0), task.get('elapsed', 0), task.get('eta', -1))

    def select_current(self, index):
        if 0 <= index < len(self.tasks):
            self.current_index = index
            self._current(index, self.tasks[index])

    def _progress(self, index, value, elapsed, eta):
        if index == self.current_index:
            self.task_progress.emit(value, elapsed, eta)

    def _paused(self, paused):
        self.is_paused = paused
        self.pause_changed.emit(paused)

    def _summary(self, success, failed):
        self._counts = (success, failed)

    def _finished(self):
        self.is_running = False
        self._paused(False)
        worker, self.worker = self.worker, None
        worker.deleteLater()
        self.running_changed.emit(False)
        self.all_finished.emit(*self._counts)

    def pause_tasks(self):
        if self.is_running:
            self.worker.control.set('paused', True)
            self._paused(True)
            self.log_message.emit('已请求暂停：运行中的任务结束后暂停，不再启动排队任务', 'info')

    def resume_tasks(self):
        if self.is_running:
            self.worker.gate.resume()
            self.worker.control.set('paused', False)
            self._paused(False)
            self.log_message.emit('继续任务队列', 'info')

    def cancel_all(self):
        if self.is_running:
            self.worker.control.set('cancelled', True)
            self.log_message.emit('正在停止本地处理；正在进行的网络请求会在返回或超时后退出', 'warning')

    def skip_current(self):
        if self.is_running:
            self.worker.control.skip_for(self.current_index)
            self.log_message.emit('正在跳过所选当前任务；不会向服务端发送取消请求', 'warning')
