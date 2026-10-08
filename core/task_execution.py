"""One task owns its clients and retries only the failed stage."""
import copy
import hashlib
import json
from pathlib import Path
import time

from .aigc import write_aigc_metadata
from .api_client import ApiClient, SubmissionUncertain, _is_definitive_precreation_rejection
from .http_client import Cancelled, extract
from .image_uploader import ImageUploader
from .log_redaction import redact_structure
from .model_catalog import family_for
from .model_parameters import GROK, validate_task_parameters, model_prompt
from .prompt_processor import process_prompt, reference_warnings
from .prompt_converter import convert_for_model, model_format
from .reference_diagnostics import ReferenceDiagnostics, describe_image
from .task_state import stamp, parameters_for_model, task_signature, resolve_output_directory, submission_images
from .video_downloader import VideoDownloader, build_filename


class RemoteGenerationFailed(RuntimeError):
    pass


class DuplicateSubmission(RuntimeError):
    pass


class PromptConversionFailed(ValueError):
    """A lossy or invalid local conversion cannot be repaired by model failover."""


def video_cache_expired(error):
    """Provider file is gone; repeating the same download URL cannot recover it."""
    if getattr(error, 'status_code', None) != 404:
        return False
    text = f'{error}\n{getattr(error, "body", "")}'.lower()
    return 'cache has expired' in text or '视频缓存已过期' in text


class VideoUnavailable(RuntimeError):
    """A recovered task id can no longer provide a video file."""


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
        self.uploaded_paths = None
        self.intent_sent = bool(task.get('task_id'))

    def publish(self, record=False, **values):
        self.task.update(values)
        if record and self.task.get('ledger_id'):
            state = ('completed' if self.task.get('status') == 'completed' else
                     'failed' if self.task.get('remote_failed') else
                     'active' if self.task.get('task_id') else 'unknown' if self.intent_sent else
                     'released' if self.task.get('status') in {'failed', 'cancelled', 'skipped'} else 'reserved')
            self.owner.ledger.save(self.task, state)
        self.owner.update_task(self.index, self.task, record=record)

    def terminal(self, status, error=''):
        self.publish(record=True, status=status, error=self.owner.redact(error), finished_at=stamp())

    def progress(self, value, elapsed=None, eta=None):
        elapsed = time.monotonic() - self.started if elapsed is None else elapsed
        eta = (elapsed*(100-value)/value if value > 0 else -1) if eta is None else eta
        self.task.update(progress=value, elapsed=elapsed, eta=eta)
        self.owner.progress_for(self.index, value, elapsed, eta)

    def select_retry_model(self, after=None):
        if self.task.get('model_locked'):
            return
        waiting = False
        while True:
            self.control.check()
            preferred_names = None
            if after and self.config.get('prompt_conversion', {}).get('prefer_same_format', True):
                catalog = self.config.get('_model_catalog')
                preferred_names = [name for name in self.pool.names
                                   if name != after and model_format(name, catalog) == model_format(after, catalog)]
            model = self.pool.pick(after=after, preferred_names=preferred_names) if after in self.pool.names else self.pool.pick(preferred_names=preferred_names)
            if model is not None:
                if after is not None:
                    self.log(f'模型 {after} 失败，切换到 {model} 重试；{after} 冷却时间 {self.config["model_pool"]["cooldown"]}秒', 'warning')
                self.model = model
                return
            if not waiting:
                self.log('可用模型都在冷却，等待最快恢复的模型', 'warning')
                self.publish(status='cooling'); waiting = True
            self.control.delay(min(.2, max(.01, self.pool.wait_seconds())))

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
            recovering = bool(self.task.get('task_id'))
            if self.task.get('prompt_read_error') and not recovering:
                raise ValueError(self.task['prompt_read_error'])
            if self.task.get('skip_reason') and not recovering:
                self.log(self.task['skip_reason'] + '，已跳过', 'warning')
                self.terminal('skipped', self.task['skip_reason']); return
            policy = self.config['task_strategy']['unmatched_prompt']
            if not recovering and not self.task['images'] and policy == '跳过并警告':
                self.log(f'任务{self.index+1}：未匹配图片，按策略跳过', 'warning')
                self.terminal('skipped'); return
            if not recovering and not self.task['images'] and policy == '暂停任务':
                self.control.set('paused', True); self.owner.pause_changed.emit(True)
                self.publish(status='paused')
                self.log('未匹配图片，已暂停；点击继续将对此任务提交文生视频，或点击跳过/取消', 'warning')
                self.control.before_task()
            original = self.task.get('_original_prompt')
            if original is None:
                original = Path(self.task['prompt_path']).read_text(encoding='utf-8-sig')
            prompt = process_prompt(original)
            self.log(f'提示词文件名: {Path(self.task["prompt_path"]).name}', 'debug')
            self.log(f'任务"{self.task["prompt_name"]}"：绑定{len(self.task["images"])}张参考图；Picture 1 对应 images[0]，按绑定顺序提交', 'debug')
            self.log('绑定来源: ' + self.task.get('match_method', '历史记录'), 'debug')
            self.log('提示词替换前: ' + original, 'debug'); self.log('提示词替换后: ' + prompt, 'debug')
            for warning in reference_warnings(original, len(self.task['images'])):
                self.log(warning + '；请在匹配详情核对绑定顺序和数量', 'warning')
            strategy = self.config['task_strategy']
            retries = max(0, int(strategy.get('max_retries', 3))) if strategy.get('auto_retry', False) else 0
            failover = self.pool.enabled and bool(self.pool.names) and self.config['model_pool'].get('auto_failover', False)
            self.recovered_id = self.task.get('task_id') or ''
            self._resubmitted = False
            for attempt in range(retries + 1):
                self.control.check()
                self.task.setdefault('attempts', []).append(dict(number=attempt+1, model=self.model, task_id=self.task.get('task_id', ''), status='running', started_at=stamp()))
                entry = self.task['attempts'][-1]
                self.publish(model=self.model, retry_count=attempt, error='', finished_at='')
                try:
                    self._attempt(original, client, uploader, downloader, diagnostics)
                    entry.update(status='completed', task_id=self.task['task_id'], finished_at=stamp())
                    if self.model in self.pool.names:
                        self.pool.succeeded(self.model)
                    self.progress(100, eta=0)
                    self.terminal('completed'); return
                except Cancelled:
                    raise
                except DuplicateSubmission:
                    return
                except PromptConversionFailed as error:
                    message = self.owner.redact(error)
                    entry.update(status='failed', phase='conversion', error=message, finished_at=stamp())
                    self.log(f'任务{self.index+1}：提示词转换失败，禁止自动切换模型：{message}', 'error')
                    self.terminal('failed', message)
                    return
                except SubmissionUncertain as error:
                    self.owner.submission_error(getattr(error, 'status_code', None))
                    message = '无法确认是否创建成功，已暂停此任务，禁止自动重新创建：' + self.owner.redact(error)
                    entry.update(status='submission_unknown', phase='submit', error=message, finished_at=stamp())
                    self.terminal('submission_unknown', message)
                    self.log(message, 'error')
                    return
                except Exception as error:
                    message = self.owner.redact(error)
                    entry.update(status='failed', task_id=self.task.get('task_id', ''), phase=self.phase, error=message, finished_at=stamp())
                    if self.phase == 'download' and video_cache_expired(error) and self.task.get('task_id') and not self._can_replace_exhausted(error):
                        self.task['result_url'] = ''
                        message = '视频缓存已过期，无法继续下载；已保留原任务号，未重新提交'
                        entry['error'] = message
                        self.log(f'任务{self.index+1} download失败：{message}', 'error')
                        self.log(f'任务{self.index+1}：标记失败并保存现场（错误与尝试记录已入账），队列继续下一个任务', 'warning')
                        self.terminal('failed', message)
                        return
                    if self._can_replace_exhausted(error):
                        self._release_exhausted_task(message)
                        entry = dict(number=attempt + 2, model=self.model, task_id='', status='running', started_at=stamp())
                        self.task.setdefault('attempts', []).append(entry)
                        try:
                            self._attempt(original, client, uploader, downloader, diagnostics)
                        except Cancelled:
                            raise
                        except DuplicateSubmission:
                            raise
                        except PromptConversionFailed:
                            raise
                        except SubmissionUncertain as uncertain:
                            message = '无法确认是否创建成功，已暂停此任务，禁止自动重新创建：' + self.owner.redact(uncertain)
                            entry.update(status='submission_unknown', phase='submit', error=message, finished_at=stamp())
                            self.owner.submission_error(getattr(uncertain, 'status_code', None))
                            self.terminal('submission_unknown', message)
                            self.log(message, 'error')
                            return
                        except Exception as replaced:
                            error = replaced
                            message = self.owner.redact(error)
                            entry.update(status='failed', task_id=self.task.get('task_id', ''), phase=self.phase, error=message, finished_at=stamp())
                            self.log(f'任务{self.index+1}：旧任务号解除后重新提交失败：{message}', 'error')
                            self.terminal('failed', message)
                            return
                        entry.update(status='completed', task_id=self.task.get('task_id', ''), finished_at=stamp())
                        if self.model in self.pool.names:
                            self.pool.succeeded(self.model)
                        self.progress(100, eta=0)
                        self.terminal('completed')
                        return
                    if self.phase == 'download' and self.task.get('task_id') and getattr(error, 'status_code', None) in {401, 403}:
                        self.task['result_url'] = ''
                        self.log('下载地址已失效；下次重试将查询已有 task_id 获取新地址', 'warning')
                    remote_failed = isinstance(error, RemoteGenerationFailed)
                    if remote_failed:
                        self.task['remote_failed'] = True
                    if self.phase == 'submit' and self.intent_sent:
                        status = getattr(error, 'status_code', None)
                        if not self._mark_precreation_rejected(error):
                            self.owner.submission_error(status)
                            self.terminal('submission_unknown', message)
                            self.log('提交结果不确定，禁止自动重新创建：' + message, 'error')
                            return
                    model_failure = self.phase in {'validation', 'submit'} or remote_failed
                    in_pool = self.model in self.pool.names
                    remaining = self.pool.failed(self.model, force_cooldown=bool(failover)) if model_failure and in_pool else 0
                    if remaining > 0:
                        self.log(f'模型 {self.model} 进入冷却{remaining:g}秒', 'warning')
                    self.log(f'任务{self.index+1} {self.phase}失败：{message}', 'error')
                    # Local standalone validation cannot be repaired by repeating it.
                    if attempt >= retries or (self.phase == 'validation' and not self.pool.enabled):
                        self.log(f'任务{self.index+1}：标记失败并保存现场（错误与尝试记录已入账），队列继续下一个任务', 'warning')
                        self.terminal('failed', message); return
                    self.publish(record=True, status='retry_wait', error=message, retry_count=attempt+1)
                    self.log(f'任务{self.index+1}：第{attempt+1}次重试，剩余{retries-attempt-1}次；间隔{strategy.get("retry_interval", 3)}秒', 'warning')
                    self.control.delay(max(0, float(strategy.get('retry_interval', 3))))
                    if remote_failed:
                        self.owner.ledger.save(self.task, 'failed')
                        self.task.update(task_id='', result_url='', result_path='', filename='')
                        self.task.pop('remote_failed', None)
                        self.task.pop('ledger_id', None)
                        self.task.pop('submit_idempotency_key', None)
                        self.task.pop('submit_idempotency_signature', None)
                        self.intent_sent = False
                    # Known IDs always keep their original model for query/download recovery.
                    if model_failure and not self.task.get('task_id') and not self.task.get('model_locked'):
                        # Recognition may select a model outside the configured
                        # pool. It is still attempted once; only then can
                        # failover choose the next enabled pool model.
                        if failover:
                            self.select_retry_model(after=self.model)
                    self.task['error'] = ''
        except Cancelled as error:
            if self.task.get('attempts') and self.task['attempts'][-1]['status'] == 'running':
                self.task['attempts'][-1].update(status='cancelled' if self.control.cancelled else 'skipped', task_id=self.task.get('task_id', ''))
            self.terminal('submission_unknown' if self.intent_sent and not self.task.get('task_id') else
                          'cancelled' if self.control.cancelled else 'skipped', str(error))
            self.log(f'任务{self.index+1}：{error}；仅停止本地处理，远端任务可能继续执行', 'warning')
        except Exception as error:
            status = 'submission_unknown' if self.intent_sent and not self.task.get('task_id') else 'failed'
            try:
                self.terminal(status, str(error))
            except Exception as persistence_error:
                self.task.update(status=status, error=str(error))
                self.owner.update_task(self.index, self.task, record=True)
                self.log(f'提交账本写入失败，已停止：{persistence_error}', 'error')
            self.log(f'任务{self.index+1}失败：{error}', 'error')
        finally:
            client.close(); uploader.close(); downloader.close(); diagnostics.close()
            self.control.release(self.index)

    def _recovered_video_already_finished(self):
        if not self.recovered_id or self.task.get('task_id') != self.recovered_id or self._resubmitted:
            return False
        return any(item.get('status') == 'completed' and item.get('task_id') == self.recovered_id
                   for item in self.task.get('attempts') or [] if isinstance(item, dict))

    def _can_replace_exhausted(self, error):
        if self._resubmitted or not self.recovered_id or self.task.get('task_id') != self.recovered_id:
            return False
        if video_cache_expired(error):
            return True
        return str(error).startswith('完成响应没有视频下载地址')

    def _release_exhausted_task(self, reason):
        old_id = self.task.get('task_id') or self.recovered_id
        detail = '视频缓存已过期' if 'cache has expired' in reason.lower() or '视频缓存已过期' in reason else '上游已无视频地址'
        self.log(f'任务{self.index+1}：旧任务号 {old_id} {detail}，解除绑定并重新提交', 'warning')
        self.task['exhausted_task_id'] = old_id
        self.task['video_unavailable'] = True
        self.task.update(task_id='', result_url='', result_path='', filename='', error='')
        self.task.pop('remote_failed', None)
        self.task.pop('submit_idempotency_key', None)
        self.task.pop('submit_idempotency_signature', None)
        if self.task.get('ledger_id') and self.owner.ledger is not None:
            self.task.update(status='failed', error=f'旧任务号 {old_id} 已无视频')
            self.owner.ledger.save(self.task, 'failed')
            self.task.pop('ledger_id', None)
        self.intent_sent = False
        self.phase = 'validation'
        self._resubmitted = True

    def _mark_precreation_rejected(self, error):
        """Release the durable intent only for a proven pre-creation failure."""
        if self.phase != 'submit' or not self.intent_sent:
            return False
        status = getattr(error, 'status_code', None)
        body = getattr(error, 'body', '') or str(error)
        if status not in {400, 422} and not _is_definitive_precreation_rejection(status, body):
            return False
        self.intent_sent = False
        self.owner.ledger.save(self.task, 'rejected')
        self.task.pop('ledger_id', None)
        self.task.pop('submit_idempotency_key', None)
        self.task.pop('submit_idempotency_signature', None)
        return True

    def _attempt(self, original, client, uploader, downloader, diagnostics):
        task = self.task
        prefix = f'任务{self.index+1}/{len(self.owner.tasks)}'
        # Keep a complete, stable per-task audit line immediately before any
        # validation/upload/submission work.  This is also emitted for retry
        # and recovery attempts so the log can be read without the task table.
        prompt_name = Path(task.get('prompt_path', '')).name or task.get('prompt_name', '')
        product = task.get('product') or '未分组'
        image_paths = submission_images(task, self.model, self.config.get('_model_catalog'))
        if not task.get('task_id') and len(image_paths) < len(task['images']):
            limit = len(image_paths)
            self.log(f'提示词{prompt_name}绑定了{len(task["images"])}张图，{self.model}最多支持{limit}张，已自动截取前{limit}张', 'warning')
            for warning in reference_warnings(original, limit):
                self.log(warning, 'warning')
        if self.uploaded_paths != image_paths:
            self.urls = None
        image_names = ', '.join(Path(path).name for path in image_paths) or '无'
        self.log(f'{prefix}：提示词={prompt_name}，产品={product}，模型={self.model}')
        self.log(f'{prefix}：绑定参考图{len(image_paths)}张：{image_names}')
        if not task.get('task_id'):
            self.phase = 'validation'
            catalog = self.config.get('_model_catalog')
            task_specific = task.get('model_source') in {'auto', 'manual', 'fallback'}
            params = parameters_for_model(self.model, self.config['workspace'], self.pool.enabled or task_specific,
                                          len(image_paths), catalog)
            ratio = params.get('aspect_ratio', params.get('ratio', ''))
            resolution = params.get('resolution', '')
            duration = params.get('duration', params.get('seconds', ''))
            self.log(f'{prefix}：参数=比例{ratio}，分辨率{resolution}，时长{duration}秒')
            conversion = self.config.get('prompt_conversion', {})
            try:
                result = convert_for_model(original, self.model, duration=params.get('duration'),
                                           enabled=conversion.get('enabled', True), catalog=catalog)
            except Exception as error:
                raise PromptConversionFailed(str(error)) from error
            if result.converted:
                self.log(f'提示词格式已从{result.source_format}转换为{result.target_format}，保留了{getattr(result, "shot_count", 0)}个镜头描述')
            for warning in result.warnings:
                self.log(warning, 'warning')
            if conversion.get('preserve_original', True):
                task['original_prompt'] = original
            else:
                task.pop('original_prompt', None)
            prompt = process_prompt(result.text)
            task.update(converted_prompt=result.text, source_format=result.source_format,
                        target_format=result.target_format, prompt_converted=result.converted)
            fields = validate_task_parameters(self.model, prompt, params, len(image_paths), catalog)
            changed = {key: value for key, value in params.items() if self.config['workspace'].get(key) != value}
            if changed:
                self.log(f'{self.model} 按支持范围调整参数：' + json.dumps(changed, ensure_ascii=False), 'warning')
            self.log('实际提交提示词: ' + model_prompt(self.model, prompt, catalog), 'debug')
            task['submitted_prompt'] = model_prompt(self.model, prompt, catalog)
            self.log(f'{self.model} 参数校验通过：' + json.dumps(fields, ensure_ascii=False))
            self.publish(request_parameters=fields, effective_parameters=params,
                         submission_model=copy.deepcopy(catalog.get(self.model)) if isinstance(catalog, dict) else None,
                         signature=task_signature(task, self.model, params, self.config['api']['base_url'], catalog),
                         created_at=task.get('created_at') or stamp(), status='uploading')
            if not task.get('ledger_id'):
                prevent = self.config['task_strategy'].get('prevent_duplicates', True)
                if task['signature'] in self.config.get('_rerun_signatures', []):
                    prevent = False
                self.control.check()

                def live_sibling(record):
                    # 同文案的兄弟任务仍在本批活动状态中（可等待其收尾）。
                    return any(
                        row.get('local_id') == record.get('local_id') and row.get('status') in
                        {'queued', 'uploading', 'submitting', 'processing', 'downloading', 'retry_wait'}
                        for row in self.owner.tasks)

                def abandoned_sibling(record):
                    # 兄弟已被跳过/取消/终结而账本记录仍活跃：无人再跟进其远端收尾，
                    # 继续等待是死锁（实测复现：跳过已提交的兄弟 → 本任务从未提交却被判重复）。
                    return any(
                        row.get('local_id') == record.get('local_id') and row.get('status') in
                        {'skipped', 'cancelled', 'failed', 'duplicate'}
                        for row in self.owner.tasks)

                # 占用评估状态机：每轮重新 reserve 后决策——可等待的活跃兄弟在线串行等待，
                # 已被弃用的兄弟逐条放行（ignore_ids），避免把“刚收尾/刚被跳过”误当活跃占用。
                ignored = set()
                waiting_logged = False
                outcome, saved = self.owner.ledger.reserve(task, self.owner.scope, prevent)
                while True:
                    if outcome in {'claimed', 'duplicate'}:
                        break
                    if outcome == 'unknown' and saved.get('signature') == task['signature']:
                        break  # 同一请求的活跃/未决兄弟：交给下方“重复”降级处理，不再等待。
                    if outcome == 'busy' and saved.get('ledger_id') and abandoned_sibling(saved):
                        ignored.add(saved['ledger_id'])
                        outcome, saved = self.owner.ledger.reserve(task, self.owner.scope, prevent, ignore_ids=ignored)
                        continue
                    if live_sibling(saved):
                        if not waiting_logged:
                            self.log(f'{prefix}：同文案任务仍在收尾，等待其结束后自动重试提交（防重复扣费）', 'warning')
                            waiting_logged = True
                        # 不同参考图/参数的同文案请求命中瞬时占用窗口（含 submitting）：
                        # 在线串行等待兄弟任务收尾后重试，而不是误判为“待确认”硬挡。
                        self.control.delay(.1)
                        outcome, saved = self.owner.ledger.reserve(task, self.owner.scope, prevent, ignore_ids=ignored)
                        continue
                    break
                if outcome == 'unknown' and saved.get('signature') == task['signature'] and live_sibling(saved):
                    # A live sibling owns this exact request. A persisted intent
                    # from a previous run still requires explicit recovery.
                    outcome = 'duplicate'
                if outcome != 'claimed':
                    state = 'submission_unknown' if outcome == 'unknown' else 'duplicate'
                    message = ('存在待确认提交，禁止再次创建' if outcome == 'unknown' else
                               '同一提示词已有生成中的任务，跳过避免重复扣费' if outcome == 'busy' else
                               '检测到相同任务，跳过避免重复扣费')
                    self.publish(record=True, status=state, duplicate_record=saved, duplicate_of=saved.get('local_id'), error=message)
                    self.log(message, 'warning')
                    raise DuplicateSubmission(message)
                task['ledger_id'] = saved['ledger_id']
            with self.owner.gate.permit(self.control) as seen_epoch:
                self.log(f'{prefix}获取提交许可，开始上传')
                self.phase = 'upload'
                originals = [diagnostics.inspect_local(path, i+1) for i, path in enumerate(image_paths)] if self.owner.debug_mode else []
                self.log(f'{prefix}：上传图片{len(image_paths)}张...')
                if family_for(self.model, catalog) == GROK:
                    urls = image_paths
                    self.log(f'{prefix}：Grok 使用本地参考文件 {len(urls)} 张')
                else:
                    if self.urls is None:
                        uploaded = uploader.upload_images(image_paths, self.control.check)
                        if any(url is None for url in uploaded):
                            raise RuntimeError('图片上传失败，本次未提交视频任务')
                        self.urls = uploaded
                        self.uploaded_paths = list(image_paths)
                        if self.owner.debug_mode:
                            for i, (path, url) in enumerate(zip(image_paths, self.urls), 1):
                                if not self.owner.debug_mode:
                                    break
                                original = originals[i-1] if i <= len(originals) else diagnostics.inspect_local(path, i)
                                description = describe_image(original) if original else '尺寸/格式未知'
                                self.log(f'  图{i}: {path} ({description}) -> {url}', 'debug')
                                diagnostics.verify_uploaded(original, url, i)
                    urls = self.urls
                field_name = 'input_reference（真实文件）' if family_for(self.model, catalog) == GROK else 'images'
                self.log(f'实际提交{field_name}: ' + json.dumps(redact_structure(urls, self.owner.redact), ensure_ascii=False), 'debug')
                self.control.check()
                self.owner.gate.wait(self.control)
                self.control.before_task()
                if task_signature(task, self.model, params, self.config['api']['base_url'], catalog) != task['signature']:
                    self.urls = None
                    raise ValueError('参考图片在上传过程中发生变化，已停止提交；请重新扫描')
                self.phase = 'submit'; self.publish(status='submitting')
                # Commit the intent synchronously before the first network byte.
                self.owner.ledger.save(task, 'submitting')
                self.intent_sent = True
                self.log(f'{prefix}：提交创建任务，模型={self.model}')
                submit_key = task.get('submit_idempotency_key')
                if not submit_key or task.get('submit_idempotency_signature') != task.get('signature'):
                    material = f"{task.get('signature', '')}|attempt-{len(task.get('attempts', []))}"
                    submit_key = 'yl-' + hashlib.sha256(material.encode('utf-8')).hexdigest()[:40]
                    task['submit_idempotency_key'] = submit_key
                    task['submit_idempotency_signature'] = task.get('signature')
                task['task_id'] = client.create_task(self.model, prompt, urls, params, catalog,
                                                     idempotency_key=submit_key)
                task['attempts'][-1]['task_id'] = task['task_id']
                self.publish(record=True, submitted_image_count=len(urls), status='queued')
                if getattr(client, 'last_submit_status', 200) == 429 or getattr(client, 'last_submit_status', 200) >= 500:
                    self.owner.submission_error(client.last_submit_status)
                else:
                    self.owner.gate.succeeded(seen_epoch)
                self.log(f"任务创建成功，task_id={task['task_id']}，状态=queued", 'success')
        else:
            self.log(f"沿用已有task_id={task['task_id']}继续轮询，不重复创建")
        if not task.get('result_url'):
            self.phase = 'poll'
            poll_started = time.monotonic()
            previous_elapsed = float(task.get('poll_elapsed_seconds') or 0)
            deadline = poll_started + float(self.config['workspace'].get('poll_timeout', 7200))
            poll_interval = max(.05, float(self.config['workspace']['poll_interval']))
            last_progress = None
            unchanged_since = poll_started
            last_stall_notice = poll_started
            missing_url_since = None
            while True:
                self.control.check()
                if time.monotonic() >= deadline:
                    raise TimeoutError('轮询超过最长等待时间；保留 task_id')
                frozen = task.get('submission_model')
                catalog = {self.model: frozen} if isinstance(frozen, dict) else self.config.get('_model_catalog')
                try:
                    result = client.query_task(task['task_id'], self.model, catalog)
                except Exception as error:
                    seconds = self.owner.submission_error(getattr(error, 'status_code', None))
                    if seconds:
                        self.control.delay(seconds)
                    raise
                self.control.check()
                self.progress(result['progress'])
                now = time.monotonic()
                poll_elapsed = previous_elapsed + max(0, now - poll_started)
                if result['progress'] != last_progress:
                    last_progress = result['progress']
                    unchanged_since = last_stall_notice = now
                    poll_interval = max(.05, float(self.config['workspace']['poll_interval']))
                elif now - unchanged_since >= 60 and now - last_stall_notice >= 60:
                    self.log(f'{prefix}：上游进度未更新，任务仍在处理中，请耐心等待', 'warning')
                    last_stall_notice = now
                self.publish(poll_elapsed_seconds=poll_elapsed,
                             status='downloading' if result['status'] == 'completed' else ('processing' if result['status'] == 'failed' else result['status']))
                minutes, seconds = divmod(int(poll_elapsed), 60)
                self.log(f"{prefix}：轮询中... 状态={result['status']}，进度={result['progress']:g}%，已轮询{minutes}分{seconds}秒")
                if result['status'] == 'failed':
                    raise RemoteGenerationFailed('远端生成失败：' + str(extract(result['raw'], ('error', 'message')) or result['raw']))
                if result['status'] == 'completed':
                    if not result['result_url']:
                        if self._recovered_video_already_finished():
                            raise VideoUnavailable('完成响应没有视频下载地址')
                        if missing_url_since is None:
                            missing_url_since = now
                        if now - missing_url_since <= 45:
                            self.log(f'{prefix}：任务已完成，下载地址尚未返回，继续查询', 'warning')
                            self.control.delay(poll_interval)
                            continue
                        if self._can_replace_exhausted(RuntimeError('完成响应没有视频下载地址')):
                            raise VideoUnavailable('完成响应没有视频下载地址')
                        raise RuntimeError('完成响应没有视频下载地址')
                    self.publish(record=True, status='downloading', result_url=result['result_url'])
                    break
                missing_url_since = None
                self.control.delay(poll_interval)
                poll_interval = min(30.0, poll_interval * 1.5)
        self.phase = 'download'
        self.control.check()
        self.log(f'{prefix}：生成完成，开始下载...', 'success')
        task['filename'] = build_filename(self.config['download_settings']['naming_rule'], task, task.get('product_task_index', self.index+1))
        task['output_dir'] = resolve_output_directory(self.config['paths']['output'], task.get('output_subdir', ''))
        self.publish(record=True, status='downloading')
        task['result_path'] = downloader.download_video(task['result_url'], task['output_dir'], task['filename'],
                                                       generation_key=(self.owner.scope, task['task_id']))
        task['size_bytes'] = Path(task['result_path']).stat().st_size
        if self.config.get('download_settings', {}).get('aigc_metadata', True):
            try:
                write_aigc_metadata(task['result_path'], model=self.model, task_id=task.get('task_id', ''))
            except Exception as error:
                self.log(f'提示：AIGC 标注写入失败（不影响下载结果）：{error}', 'warning')
