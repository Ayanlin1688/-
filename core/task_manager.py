"""Sequential background generation with explicit local pause/cancel/skip."""
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

from PyQt5.QtCore import QObject, QThread, pyqtSignal
from .api_client import ApiClient, GROK, V3_MODELS
from .model_parameters import validate_task_parameters, model_prompt
from .http_client import Cancelled, extract
from .image_uploader import ImageUploader, upload_credentials
from .matcher import StoryboardMatcher
from .prompt_processor import process_prompt, reference_warnings
from .reference_diagnostics import ReferenceDiagnostics, describe_image
from .log_redaction import redact_text, redact_structure
from .video_downloader import VideoDownloader, build_filename

TERMINAL = {'completed', 'failed', 'cancelled', 'skipped'}
STATUS_TEXT = {'waiting': '等待中', 'queued': '生成中', 'uploading': '上传中', 'submitting': '提交中',
               'processing': '生成中', 'downloading': '下载中', 'completed': '已完成',
               'failed': '失败', 'cancelled': '已取消', 'skipped': '已跳过', 'paused': '已暂停'}


def stamp():
    return datetime.now().isoformat(timespec='seconds')


class TaskControl:
    def __init__(self):
        self.condition = threading.Condition()
        self.cancelled = False
        self.skipped = False
        self.paused = False
        self.active_index = -1

    def begin(self, index):
        with self.condition:
            self.active_index = index
            self.skipped = False

    def skip_for(self, index):
        # A delayed UI click for the preceding task must not skip a newer task.
        with self.condition:
            if index == self.active_index:
                self.skipped = True
                self.condition.notify_all()

    def set(self, name, value):
        with self.condition:
            setattr(self, name, value)
            self.condition.notify_all()

    def check(self):
        with self.condition:
            if self.cancelled or self.skipped:
                raise Cancelled('已取消' if self.cancelled else '已跳过')

    def delay(self, seconds):
        deadline = time.monotonic() + seconds
        with self.condition:
            while time.monotonic() < deadline:
                self.check()
                self.condition.wait(max(0, deadline - time.monotonic()))
            self.check()

    def before_task(self):
        with self.condition:
            while self.paused:
                self.check()
                self.condition.wait()
            self.check()


class TaskWorker(QThread):
    task_list_updated = pyqtSignal(object)
    current_task_changed = pyqtSignal(int, object)
    task_progress = pyqtSignal(float, float, float)
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
        self.index = -1
        self.debug_mode = bool(self.config.get('diagnostics', {}).get('debug_mode', False))

    def redact(self, message):
        api = self.config.get('api', {})
        return redact_text(message, (api.get('api_key'), api.get('upload_api_key')))

    def log(self, message, level='info'):
        if level.lower() == 'debug' and not self.debug_mode:
            return
        self.log_message.emit(self.redact(message), level)

    def publish(self, record=False):
        self.task_list_updated.emit(copy.deepcopy(self.tasks))
        if 0 <= self.index < len(self.tasks):
            self.current_task_changed.emit(self.index, copy.deepcopy(self.tasks[self.index]))
            if record:
                self.record_updated.emit(copy.deepcopy(self.tasks[self.index]))

    def terminal(self, task, status, error=''):
        task.update(status=status, error=error, finished_at=stamp())
        self.publish(record=True)

    def run(self):
        api = self.config['api']
        client = ApiClient(api['base_url'], api['api_key'], debug_mode=lambda: self.debug_mode,
                           log_secrets=(api.get('upload_api_key'),), log=self.log)
        uploader = ImageUploader.from_config(self.config, log=self.log, check_cancel=self.control.check)
        download = VideoDownloader(self.config.get('download_settings', {}).get('overwrite_existing', False), self.control.check, log=self.log)
        diagnostics = ReferenceDiagnostics(self.log, self.control.check)
        try:
            paths = self.config['paths']
            self.log(f"开始扫描目录：提示词={paths['prompts']}, 图片={paths.get('images', '')}")
            self.tasks = StoryboardMatcher.from_config(self.config).scan_and_match(paths)
            limit = self.config.get('_task_limit')
            if limit:
                self.tasks = self.tasks[:int(limit)]
            model = self.config['workspace']['model']
            previous = {t.get('signature'): t for t in [*self.config.get('history', []), *self.previous] if t.get('signature')}
            for sequence, task in enumerate(self.tasks, 1):
                # Compare inputs and generation parameters to prevent accidental duplicate paid submissions.
                inputs = [task['prompt_path'], *task['images']]
                stats = [(p, Path(p).stat().st_mtime_ns, Path(p).stat().st_size) if Path(p).is_file() else (p, None, None) for p in inputs]
                params = self.config['workspace']
                effective = dict(model=model, duration=params.get('duration'), aspect_ratio=params.get('aspect_ratio'))
                if model in V3_MODELS:
                    effective['resolution'] = '720p'
                elif model != 'video-v1':
                    effective['resolution'] = params.get('resolution')
                # Preserve legacy signatures for unchanged requests with audio=True/no seed.
                # H3 fixed sizes now depend on ratio; the old code wrongly ignored it.
                if model in V3_MODELS or model in {'video-v2', 'video-v2-fast'}:
                    if params.get('generate_audio') is not True:
                        effective['generate_audio'] = params.get('generate_audio')
                if model in V3_MODELS and params.get('seed') not in ('', None):
                    try:
                        effective['seed'] = int(params['seed'])
                    except (ValueError, TypeError):
                        effective['seed'] = params['seed']
                # Polling, retry placeholders, output folders and unsupported seed/audio settings
                # do not change the submitted video and must not cause another paid request.
                signature = hashlib.sha256(json.dumps([stats, effective, api['base_url'].rstrip('/')], sort_keys=True).encode()).hexdigest()
                task.update(status='waiting', task_id='', model=model, retry_count=0, result_path='',
                            local_id=uuid.uuid4().hex, signature=signature, created_at='', finished_at='', error='',
                            api_base_url=api['base_url'].rstrip('/'), output_dir=str(Path(paths['output']).resolve()),
                            sequence=sequence)
                old = previous.get(signature)
                if old and old.get('task_id'):
                    task.update(copy.deepcopy(old))
                    if old['status'] != 'completed' or not Path(old.get('result_path', '')).is_file():
                        task.update(status='failed', error='已有远端任务ID，请从历史记录重新下载，避免重复提交')
            matched = sum(t['matched'] for t in self.tasks)
            self.log(f'匹配完成：共{len(self.tasks)}个提示词，{matched}个已匹配，{len(self.tasks)-matched}个未匹配')
            self.publish()
            if not self.tasks:
                self.log('目录中没有 .txt 提示词', 'warning')
            for index, task in enumerate(self.tasks):
                self.index = index
                self.control.begin(index)
                if task.get('task_id'):
                    self.log(f"保留已有任务：{task['prompt_name']}，task_id={task['task_id']}，不重复提交", 'warning')
                    self.publish()
                    continue
                started = time.monotonic()
                try:
                    self.publish()
                    self.task_progress.emit(0, 0, -1)
                    self.control.before_task()
                    policy = self.config['task_strategy']['unmatched_prompt']
                    if not task['images'] and policy == '跳过并警告':
                        self.log(f'任务{index+1}/{len(self.tasks)}：未匹配图片，按策略跳过', 'warning')
                        self.terminal(task, 'skipped')
                        continue
                    if not task['images'] and policy == '暂停任务':
                        self.control.set('paused', True)
                        self.pause_changed.emit(True)
                        task['status'] = 'paused'; self.publish()
                        self.log('未匹配图片，已暂停；点击继续将对此任务提交文生视频，或点击跳过/取消', 'warning')
                        self.control.before_task()
                    original_prompt = Path(task['prompt_path']).read_text(encoding='utf-8-sig')
                    prompt = process_prompt(original_prompt)
                    self.log(f'提示词文件名: {Path(task["prompt_path"]).name}', 'debug')
                    self.log(f'任务"{task["prompt_name"]}"：绑定{len(task["images"])}张参考图；Picture 1 对应 images[0]，按绑定顺序提交', 'debug')
                    self.log('绑定来源: ' + task.get('match_method', '历史记录'), 'debug')
                    self.log('提示词替换前: ' + original_prompt, 'debug')
                    self.log('提示词替换后: ' + prompt, 'debug')
                    self.log('实际提交提示词: ' + model_prompt(model, prompt), 'debug')
                    for warning in reference_warnings(original_prompt, len(task['images'])):
                        self.log(warning + '；请在匹配详情核对绑定顺序和数量', 'warning')
                    fields = validate_task_parameters(model, prompt, self.config['workspace'], len(task['images']))
                    self.log(f'{model} 参数校验通过：' + json.dumps(fields, ensure_ascii=False))
                    task['request_parameters'] = fields
                    task.update(created_at=stamp(), status='uploading')
                    self.publish()
                    started = time.monotonic()
                    prefix = f'任务{index+1}/{len(self.tasks)}'
                    originals = [diagnostics.inspect_local(path, i+1) for i, path in enumerate(task['images'])] if self.debug_mode else []
                    if model == GROK:
                        urls = task['images']
                        self.log(f'{prefix}：Grok 使用本地参考文件 {len(urls)} 张')
                    else:
                        self.log(f"{prefix}：上传图片{len(task['images'])}张...")
                        urls = uploader.upload_images(task['images'], self.control.check)
                        if any(url is None for url in urls):
                            raise RuntimeError('图片上传失败，本任务不提交；继续下一个任务')
                        if self.debug_mode:
                            for i, (path, url) in enumerate(zip(task['images'], urls), 1):
                                if not self.debug_mode:
                                    break
                                original = originals[i-1] if i <= len(originals) else diagnostics.inspect_local(path, i)
                                description = describe_image(original) if original else '尺寸/格式未知'
                                self.log(f'  图{i}: {path} ({description}) -> {url}', 'debug')
                                diagnostics.verify_uploaded(original, url, i)
                    field_name = 'input_reference（真实文件）' if model == GROK else 'images'
                    self.log(f'实际提交{field_name}: ' + json.dumps(redact_structure(urls, self.redact), ensure_ascii=False), 'debug')
                    self.control.check()
                    task['status'] = 'submitting'; self.publish()
                    self.log(f'{prefix}：提交创建任务，模型={model}')
                    task['task_id'] = client.create_task(model, prompt, urls, self.config['workspace'])
                    task['submitted_image_count'] = len(urls)
                    task['status'] = 'queued'
                    self.publish(record=True)
                    self.log(f"任务创建成功，task_id={task['task_id']}，状态=queued", 'success')
                    deadline = time.monotonic() + float(self.config['workspace'].get('poll_timeout', 3600))
                    while True:
                        self.control.check()
                        if time.monotonic() >= deadline:
                            raise TimeoutError('轮询超过最长等待时间，已停止本地等待；保留 task_id')
                        result = client.query_task(task['task_id'], model)
                        self.control.check()
                        elapsed = time.monotonic() - started
                        progress = result['progress']
                        self.task_progress.emit(progress, elapsed, elapsed * (100-progress) / progress if progress > 0 else -1)
                        task['status'] = 'downloading' if result['status'] == 'completed' else result['status']; self.publish()
                        self.log(f"{prefix}：轮询中... 状态={result['status']}，进度={progress:g}%")
                        if result['status'] == 'failed':
                            raise RuntimeError('远端生成失败：' + str(extract(result['raw'], ('error', 'message')) or result['raw']))
                        if result['status'] == 'completed':
                            if not result['result_url']:
                                raise RuntimeError('完成响应没有视频下载地址')
                            task.update(status='downloading', result_url=result['result_url'])
                            self.publish(record=True)
                            self.log(f'{prefix}：生成完成，开始下载...', 'success')
                            rule = self.config['download_settings']['naming_rule']
                            task['filename'] = build_filename(rule, task, index + 1)
                            task['output_dir'] = str(Path(paths['output']).resolve())
                            task['result_path'] = download.download_video(task['result_url'], task['output_dir'], task['filename'])
                            task['size_bytes'] = Path(task['result_path']).stat().st_size
                            self.task_progress.emit(100, time.monotonic()-started, 0)
                            self.terminal(task, 'completed')
                            break
                        self.control.delay(max(0.01, float(self.config['workspace']['poll_interval'])))
                except Cancelled as error:
                    self.terminal(task, 'cancelled' if self.control.cancelled else 'skipped', str(error))
                    self.log(f'任务{index+1}：{error}；仅停止本地处理，远端任务可能继续执行', 'warning')
                except Exception as error:
                    message = str(error)
                    for key in ('api_key', 'upload_api_key'):
                        secret = api.get(key)
                        if secret:
                            message = message.replace(secret, '[REDACTED]')
                    self.terminal(task, 'failed', message)
                    self.log(f'任务{index+1}失败：{message}', 'error')
                if self.control.cancelled:
                    for remaining in range(index + 1, len(self.tasks)):
                        if self.tasks[remaining]['status'] not in TERMINAL:
                            self.index = remaining
                            self.terminal(self.tasks[remaining], 'cancelled', '用户取消全部')
                    break
        except Exception as error:
            self.log(f'任务队列无法继续：{error}', 'error')
        finally:
            client.close(); uploader.close(); download.close(); diagnostics.close()
            self.summary.emit(sum(t.get('status') == 'completed' for t in self.tasks),
                              sum(t.get('status') == 'failed' for t in self.tasks))


class TaskManager(QObject):
    task_list_updated = pyqtSignal(object)
    current_task_changed = pyqtSignal(int, object)
    task_progress = pyqtSignal(float, float, float)
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
            return False
        if not config['api']['api_key'].strip():
            raise ValueError('请先在设置中填写 API Key')
        if not config['paths']['prompts'] or not config['paths']['output']:
            raise ValueError('请选择提示词目录和视频保存目录')
        self.worker = TaskWorker(config, self.tasks, self)
        self.worker.task_list_updated.connect(self._list)
        self.worker.current_task_changed.connect(self._current)
        self.worker.task_progress.connect(self.task_progress)
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
        self.current_index = index
        self.current_task_changed.emit(index, task)

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
            self.log_message.emit('已请求暂停：当前任务完成后暂停，点击继续可恢复', 'info')

    def resume_tasks(self):
        if self.is_running:
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
            self.log_message.emit('正在跳过当前任务；不会向服务端发送取消请求', 'warning')
