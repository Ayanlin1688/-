"""One task owns its clients and retries only the failed stage."""
import copy
import json
from pathlib import Path
import time

from .api_client import ApiClient
from .http_client import Cancelled, extract
from .image_uploader import ImageUploader
from .log_redaction import redact_structure
from .model_parameters import GROK, validate_task_parameters, model_prompt
from .prompt_processor import process_prompt, reference_warnings
from .reference_diagnostics import ReferenceDiagnostics, describe_image
from .task_state import stamp, parameters_for_model, task_signature, resolve_output_directory
from .video_downloader import VideoDownloader, build_filename


class RemoteGenerationFailed(RuntimeError):
    pass


class TaskExecution:
    def __init__(self, owner, index, task, model):
        self.owner = owner
        self.index = index
        self.task = copy.deepcopy(task)
        self.model = model
        self.config = owner.config
        self.control = owner.control
        self.pool = owner.pool
        self.log = owner.log
        self.phase = 'validation'
        self.started = time.monotonic()
        self.urls = None

    def publish(self, record=False, **values):
        self.task.update(values)
        self.owner.update_task(self.index, self.task, record=record)

    def terminal(self, status, error=''):
        self.publish(record=True, status=status, error=self.owner.redact(error), finished_at=stamp())

    def progress(self, value, elapsed=None, eta=None):
        elapsed = time.monotonic() - self.started if elapsed is None else elapsed
        eta = (elapsed*(100-value)/value if value > 0 else -1) if eta is None else eta
        self.task.update(progress=value, elapsed=elapsed, eta=eta)
        self.owner.progress_for(self.index, value, elapsed, eta)

    def select_retry_model(self, after=None):
        preferred = None if after is not None else self.model
        waiting = False
        while True:
            self.control.check()
            model = self.pool.pick(after=after, preferred=preferred)
            if model is not None:
                if after is not None:
                    self.log(f'模型 {after} 失败，切换到 {model} 重试；{after} 冷却时间 {self.config["model_pool"]["cooldown"]}秒', 'warning')
                self.model = model
                return
            if not waiting:
                self.log('可用模型都在冷却，等待最快恢复的模型', 'warning')
                self.publish(status='cooling'); waiting = True
            self.control.delay(min(.2, max(.01, self.pool.wait_seconds(preferred=preferred))))

    def run(self):
        self.control.begin(self.index)
        api = self.config['api']
        client = ApiClient(api['base_url'], api['api_key'], debug_mode=lambda: self.owner.debug_mode,
                           log_secrets=(api.get('upload_api_key'),), log=self.log)
        uploader = ImageUploader.from_config(self.config, log=self.log, check_cancel=self.control.check)
        downloader = VideoDownloader(self.config['download_settings']['overwrite_existing'], self.control.check, log=self.log)
        diagnostics = ReferenceDiagnostics(self.log, self.control.check)
        try:
            self.progress(0, 0, -1)
            self.control.before_task()
            if self.task.get('skip_reason'):
                self.log(self.task['skip_reason'] + '，已跳过', 'warning')
                self.terminal('skipped', self.task['skip_reason']); return
            policy = self.config['task_strategy']['unmatched_prompt']
            if not self.task['images'] and policy == '跳过并警告':
                self.log(f'任务{self.index+1}：未匹配图片，按策略跳过', 'warning')
                self.terminal('skipped'); return
            if not self.task['images'] and policy == '暂停任务':
                self.control.set('paused', True); self.owner.pause_changed.emit(True)
                self.publish(status='paused')
                self.log('未匹配图片，已暂停；点击继续将对此任务提交文生视频，或点击跳过/取消', 'warning')
                self.control.before_task()
            original = Path(self.task['prompt_path']).read_text(encoding='utf-8-sig')
            prompt = process_prompt(original)
            self.log(f'提示词文件名: {Path(self.task["prompt_path"]).name}', 'debug')
            self.log(f'任务"{self.task["prompt_name"]}"：绑定{len(self.task["images"])}张参考图；Picture 1 对应 images[0]，按绑定顺序提交', 'debug')
            self.log('绑定来源: ' + self.task.get('match_method', '历史记录'), 'debug')
            self.log('提示词替换前: ' + original, 'debug'); self.log('提示词替换后: ' + prompt, 'debug')
            for warning in reference_warnings(original, len(self.task['images'])):
                self.log(warning + '；请在匹配详情核对绑定顺序和数量', 'warning')
            strategy = self.config['task_strategy']
            retries = max(0, int(strategy.get('max_retries', 5))) if strategy.get('auto_retry', False) else 0
            failover = self.pool.enabled and self.config['model_pool'].get('auto_failover', False)
            for attempt in range(retries + 1):
                self.control.check()
                self.task.setdefault('attempts', []).append(dict(number=attempt+1, model=self.model, task_id=self.task.get('task_id', ''), status='running', started_at=stamp()))
                entry = self.task['attempts'][-1]
                self.publish(model=self.model, retry_count=attempt, error='', finished_at='')
                try:
                    self._attempt(prompt, client, uploader, downloader, diagnostics)
                    entry.update(status='completed', task_id=self.task['task_id'], finished_at=stamp())
                    self.pool.succeeded(self.model)
                    self.progress(100, eta=0)
                    self.terminal('completed'); return
                except Cancelled:
                    raise
                except Exception as error:
                    message = self.owner.redact(error)
                    entry.update(status='failed', task_id=self.task.get('task_id', ''), phase=self.phase, error=message, finished_at=stamp())
                    remote_failed = isinstance(error, RemoteGenerationFailed)
                    model_failure = self.phase in {'validation', 'submit'} or remote_failed
                    remaining = self.pool.failed(self.model, force_cooldown=bool(failover)) if model_failure else 0
                    if remaining > 0:
                        self.log(f'模型 {self.model} 进入冷却{remaining:g}秒', 'warning')
                    self.log(f'任务{self.index+1} {self.phase}失败：{message}', 'error')
                    # Local standalone validation cannot be repaired by repeating it.
                    if attempt >= retries or (self.phase == 'validation' and not self.pool.enabled):
                        self.terminal('failed', message); return
                    self.publish(record=True, status='retry_wait', error=message, retry_count=attempt+1)
                    self.log(f'任务{self.index+1}：第{attempt+1}次重试，剩余{retries-attempt-1}次；间隔{strategy.get("retry_interval", 3)}秒', 'warning')
                    self.control.delay(max(0, float(strategy.get('retry_interval', 3))))
                    if remote_failed:
                        self.task.update(task_id='', result_url='', result_path='', filename='')
                    # Known IDs always keep their original model for query/download recovery.
                    if model_failure and not self.task.get('task_id'):
                        self.select_retry_model(after=self.model if failover else None)
                    self.task['error'] = ''
        except Cancelled as error:
            if self.task.get('attempts') and self.task['attempts'][-1]['status'] == 'running':
                self.task['attempts'][-1].update(status='cancelled' if self.control.cancelled else 'skipped', task_id=self.task.get('task_id', ''))
            self.terminal('cancelled' if self.control.cancelled else 'skipped', str(error))
            self.log(f'任务{self.index+1}：{error}；仅停止本地处理，远端任务可能继续执行', 'warning')
        except Exception as error:
            self.terminal('failed', str(error)); self.log(f'任务{self.index+1}失败：{error}', 'error')
        finally:
            client.close(); uploader.close(); downloader.close(); diagnostics.close()
            self.control.release(self.index)

    def _attempt(self, prompt, client, uploader, downloader, diagnostics):
        task = self.task
        prefix = f'任务{self.index+1}/{len(self.owner.tasks)}'
        if not task.get('task_id'):
            self.phase = 'validation'
            params = parameters_for_model(self.model, self.config['workspace'], self.pool.enabled, len(task['images']))
            fields = validate_task_parameters(self.model, prompt, params, len(task['images']))
            changed = {key: value for key, value in params.items() if self.config['workspace'].get(key) != value}
            if changed:
                self.log(f'{self.model} 按支持范围调整参数：' + json.dumps(changed, ensure_ascii=False), 'warning')
            self.log('实际提交提示词: ' + model_prompt(self.model, prompt), 'debug')
            self.log(f'{self.model} 参数校验通过：' + json.dumps(fields, ensure_ascii=False))
            self.publish(request_parameters=fields, effective_parameters=params,
                         signature=task_signature(task, self.model, params, self.config['api']['base_url']),
                         created_at=task.get('created_at') or stamp(), status='uploading')
            self.phase = 'upload'
            originals = [diagnostics.inspect_local(path, i+1) for i, path in enumerate(task['images'])] if self.owner.debug_mode else []
            if self.model == GROK:
                urls = task['images']
                self.log(f'{prefix}：Grok 使用本地参考文件 {len(urls)} 张')
            else:
                if self.urls is None:
                    self.log(f"{prefix}：上传图片{len(task['images'])}张...")
                    uploaded = uploader.upload_images(task['images'], self.control.check)
                    if any(url is None for url in uploaded):
                        raise RuntimeError('图片上传失败，本次未提交视频任务')
                    self.urls = uploaded
                    if self.owner.debug_mode:
                        for i, (path, url) in enumerate(zip(task['images'], self.urls), 1):
                            if not self.owner.debug_mode:
                                break
                            original = originals[i-1] if i <= len(originals) else diagnostics.inspect_local(path, i)
                            description = describe_image(original) if original else '尺寸/格式未知'
                            self.log(f'  图{i}: {path} ({description}) -> {url}', 'debug')
                            diagnostics.verify_uploaded(original, url, i)
                urls = self.urls
            field_name = 'input_reference（真实文件）' if self.model == GROK else 'images'
            self.log(f'实际提交{field_name}: ' + json.dumps(redact_structure(urls, self.owner.redact), ensure_ascii=False), 'debug')
            self.control.check()
            self.phase = 'submit'; self.publish(status='submitting')
            self.log(f'{prefix}：提交创建任务，模型={self.model}')
            task['task_id'] = client.create_task(self.model, prompt, urls, params)
            task['attempts'][-1]['task_id'] = task['task_id']
            self.publish(record=True, submitted_image_count=len(urls), status='queued')
            self.log(f"任务创建成功，task_id={task['task_id']}，状态=queued", 'success')
        if not task.get('result_url'):
            self.phase = 'poll'
            deadline = time.monotonic() + float(self.config['workspace'].get('poll_timeout', 3600))
            while True:
                self.control.check()
                if time.monotonic() >= deadline:
                    raise TimeoutError('轮询超过最长等待时间；保留 task_id')
                result = client.query_task(task['task_id'], self.model)
                self.control.check()
                self.progress(result['progress'])
                self.publish(status='downloading' if result['status'] == 'completed' else ('processing' if result['status'] == 'failed' else result['status']))
                self.log(f"{prefix}：轮询中... 状态={result['status']}，进度={result['progress']:g}%")
                if result['status'] == 'failed':
                    raise RemoteGenerationFailed('远端生成失败：' + str(extract(result['raw'], ('error', 'message')) or result['raw']))
                if result['status'] == 'completed':
                    if not result['result_url']:
                        raise RuntimeError('完成响应没有视频下载地址')
                    self.publish(record=True, status='downloading', result_url=result['result_url'])
                    break
                self.control.delay(max(.01, float(self.config['workspace']['poll_interval'])))
        self.phase = 'download'
        self.control.check()
        self.log(f'{prefix}：生成完成，开始下载...', 'success')
        task['filename'] = build_filename(self.config['download_settings']['naming_rule'], task, task.get('product_task_index', self.index+1))
        task['output_dir'] = resolve_output_directory(self.config['paths']['output'], task.get('output_subdir', ''))
        self.publish(record=True, status='downloading')
        task['result_path'] = downloader.download_video(task['result_url'], task['output_dir'], task['filename'])
        task['size_bytes'] = Path(task['result_path']).stat().st_size
