"""Pure Python ecommerce pipeline; callers choose their own UI/thread boundary."""
from __future__ import annotations

from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4
from weakref import WeakValueDictionary

from core.log_redaction import redact_structure, redact_text
from core.line_routing import resolve_line
from core.i18n import tr
from core.api_errors import classify_http_error
from core.http_client import Cancelled
from core.image_protocols import preferred_image_protocol
from core import manual_image
from core.model_catalog import normalize_model_id
from .analysis import DEFAULT_ANALYSIS_MODEL, EcommerceAnalysisError, analyze_and_parse, parse_prompt_items
from .image_client import EcommerceImageError, DEFAULT_IMAGE_MODEL, generate_image, image_dimensions
from .platforms import _resolve_platforms, detect_conflicts
from .prompt_builder import append_reference_order, build_system, build_user


def _valid_file(path):
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _atomic_write(path, content, *, durable=False):
    """Publish only a complete file, leaving the previous result intact on failure."""
    if not content:
        raise OSError('输出内容为空')
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.stem + '-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            if durable:
                os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class EcommerceImageManager:
    # Shared by managers using the same database, including after a UI rebuild.
    _locks_guard = threading.Lock()
    _group_locks = WeakValueDictionary()

    def __init__(self, base_url, api_key, output_root, store,
                 analysis_model=DEFAULT_ANALYSIS_MODEL, image_model=DEFAULT_IMAGE_MODEL,
                 concurrency=5, log=None, *, analysis_func=None, image_func=None,
                 on_group=None, on_item=None, on_log=None, analysis_base_url=None,
                 analysis_api_key=None, image_base_url=None, image_api_key=None,
                 analysis_model_candidates=(), image_model_candidates=(), model_catalog=None):
        self.base_url = base_url
        self.api_key = api_key
        self.analysis_base_url = base_url if analysis_base_url is None else analysis_base_url
        self.analysis_api_key = api_key if analysis_api_key is None else analysis_api_key
        self.image_base_url = base_url if image_base_url is None else image_base_url
        self.image_api_key = api_key if image_api_key is None else image_api_key
        self.output_root = Path(output_root).expanduser().resolve()
        self.store = store
        self.analysis_model = normalize_model_id(analysis_model)
        self.image_model = normalize_model_id(image_model)
        self.analysis_model_candidates = self._unique_models(analysis_model_candidates)
        self.image_model_candidates = self._unique_models(image_model_candidates)
        self.model_catalog = model_catalog if isinstance(model_catalog, dict) else {}
        self.concurrency = max(1, int(concurrency))
        self.log = log
        self.analysis_func = analysis_func if analysis_func is not None else analyze_and_parse
        self.image_func = image_func if image_func is not None else self._generate_image_by_protocol
        self.on_group, self.on_item, self.on_log = on_group, on_item, on_log
        self._warned_persistence = False
        self._warned_touch = False
        self._diagnostics_lock = threading.Lock()
        self.timings = deque(maxlen=200)
        self._deliveries = {}

    @classmethod
    def build_from_config(cls, config_manager, store=None, overrides=None, model_catalog=None):
        config = config_manager.config
        analysis_line = resolve_line(config, 'chat')
        image_line = resolve_line(config, 'image')
        ecommerce = config.get('ecommerce', {})
        overrides = overrides if isinstance(overrides, dict) else {}
        analysis_model = normalize_model_id(overrides.get('analysis_model', ecommerce.get('analysis_model', DEFAULT_ANALYSIS_MODEL)))
        image_model = normalize_model_id(overrides.get('image_model', ecommerce.get('image_model', DEFAULT_IMAGE_MODEL)))
        controller = getattr(config_manager, 'model_catalog_controller', None)
        catalog = model_catalog if isinstance(model_catalog, dict) else {}
        if not catalog and controller is not None:
            for capability in ('chat', 'image'):
                try:
                    snapshot = controller.snapshot_for(capability)
                except Exception:
                    snapshot = {}
                if isinstance(snapshot, dict):
                    catalog[capability] = snapshot
        output = (config.get('paths', {}).get('output') or config.get('output_dir')
                  or config_manager.path.parent / 'output')
        return cls(
            analysis_line['base_url'], analysis_line['api_key'], output,
            store if store is not None else config_manager.ecommerce_store(),
            analysis_model,
            image_model,
            ecommerce.get('max_concurrency', config.get('task_strategy', {}).get('max_concurrency', 5)),
            analysis_base_url=analysis_line['base_url'], analysis_api_key=analysis_line['api_key'],
            image_base_url=image_line['base_url'], image_api_key=image_line['api_key'],
            analysis_model_candidates=cls._pool_candidates(config.get('chat_model_pool'), analysis_model),
            image_model_candidates=cls._pool_candidates(config.get('image_model_pool'), image_model),
            model_catalog=catalog)

    @staticmethod
    def _unique_models(models):
        result = []
        for value in models or ():
            model = normalize_model_id(value)
            if model and model not in result:
                result.append(model)
        return result

    @classmethod
    def _pool_candidates(cls, pool, chosen):
        if (not isinstance(pool, dict) or not pool.get('enabled')
                or not pool.get('auto_failover', True)):
            return []
        models = []
        for item in pool.get('models', ()):
            if not isinstance(item, dict) or not item.get('enabled'):
                continue
            name = normalize_model_id(item.get('name'))
            if name and name != normalize_model_id(chosen) and name not in models:
                models.append(name)
        return models

    @staticmethod
    def _can_switch(error):
        status = getattr(error, 'status_code', None)
        if status is None and getattr(error, 'retry_safe', None) is False:
            return False
        if status is not None:
            if 200 <= status < 300:
                return False
            classification = classify_http_error(
                status, getattr(error, 'body_excerpt', '') or str(error),
                kind='image' if error.__class__.__name__.endswith('ImageError') else 'chat')
            category = getattr(error, 'category', classification['category'])
            # Failover is reserved for a model/parameter rejection.  Auth,
            # network, rate-limit and upstream failures are line-wide and
            # must not silently try another model with the same credentials.
            return category in {'model_not_found', 'model_unsupported', 'reasoning_conflict'}
        return False

    @staticmethod
    def _can_skip_to_next(error):
        """Return whether a failed candidate may be replaced by the next one."""
        return EcommerceImageManager._can_switch(error)

    @staticmethod
    def _line_host(base_url):
        return (urlsplit(str(base_url or '').strip()).hostname or '').lower() or tr('未配置')

    @staticmethod
    def _user_error(error):
        return getattr(error, 'user_message', None) or getattr(error, 'message', None) or str(error)

    def _catalog_model_ids(self, capability):
        """Return usable model ids from the live catalog snapshot.

        Older snapshots used ``text``/``llm`` for chat records and some test or
        provider snapshots omit ``kind`` while retaining endpoint metadata.
        Accept those equivalent forms without inventing model names.
        """
        catalog = self.model_catalog.get(capability, {})
        if not isinstance(catalog, dict):
            return []
        result = []
        accepted = {'chat', 'text', 'llm'} if capability == 'chat' else {'image'}
        for key, record in catalog.items():
            if not isinstance(record, dict) or not record.get('available', True):
                continue
            model = normalize_model_id(record.get('id') or key)
            if not model:
                continue
            kind = str(record.get('kind') or record.get('type') or '').casefold()
            endpoints = record.get('supported_endpoint_types', record.get('endpoint_types', ()))
            if isinstance(endpoints, str):
                endpoints = [endpoints]
            endpoint_text = {str(value).casefold() for value in (endpoints or ())}
            if kind in accepted or (
                    capability == 'chat' and endpoint_text.intersection({'chat', 'chat/completions', 'llm'})) or (
                    capability == 'image' and endpoint_text.intersection({'image', 'images', 'openai-image'})):
                if model not in result:
                    result.append(model)
        return result

    def _analysis_candidates(self):
        return self._unique_models([self.analysis_model, *self.analysis_model_candidates,
                                    *self._catalog_model_ids('chat')])[:3]

    @staticmethod
    def _wait_analysis_retry(delay, cancel_event):
        event = cancel_event if cancel_event is not None else threading.Event()
        for _ in range(int(delay * 10)):
            if event.wait(0.1):
                raise Cancelled('已取消 AI 分析')

    def _run_analysis(self, system, user, carousel_count, detail_count, thinking, log_reasoning,
                      cancel_event=None, reference_paths=(), fallback_paths=None,
                      style_image_paths=()):
        candidates = self._analysis_candidates()
        reference_paths = list(reference_paths)
        reduced = False
        attempted, retries, failures = [], 0, []
        for position, model in enumerate(candidates):
            attempted.append(model)
            failed = False
            attempt = 0
            while attempt < 3:
                if cancel_event is not None and cancel_event.is_set():
                    raise Cancelled('已取消 AI 分析')
                try:
                    raw_items, reasoning = self.analysis_func(
                        self.analysis_base_url, self.analysis_api_key, model, system, user,
                        carousel_count, detail_count, thinking=bool(thinking), on_reasoning=log_reasoning,
                        **({'reference_paths': reference_paths} if reference_paths else {}),
                        **({'style_image_paths': style_image_paths} if style_image_paths else {}))
                    break
                except EcommerceAnalysisError as error:
                    failures.append(error)
                    if not reduced and fallback_paths is not None and self._multi_image_rejection(error):
                        reference_paths = list(fallback_paths)
                        user = append_reference_order(
                            user.split('\n\nReference image order (critical):')[0],
                            len(reference_paths) - 1, style_count=1)
                        reduced = True
                        self._log(tr('中转站限制多图，本次仅使用第1张风格图'), 'warning')
                        continue
                    if error.retryable and not self._multi_image_rejection(error) and attempt < 2:
                        self._wait_analysis_retry(2 ** (attempt + 1), cancel_event)
                        retries += 1
                        attempt += 1
                        continue
                    if (not self._multi_image_rejection(error)
                            and position + 1 < len(candidates) and self._can_skip_to_next(error)):
                        next_model = candidates[position + 1]
                        self._log(tr('分析模型 {old} 不可用（{reason}），切换 {new}').format(
                            old=model, reason=error.user_message, new=next_model), 'warning')
                        failed = True
                        break
                    reason = getattr(error, 'user_message', self._clean(error.message))
                    if failures and all(getattr(item, 'status_code', None) in (401, 403)
                                        for item in failures):
                        message = (f'密钥无效或没有该模型的调用权限（已尝试模型 '
                                   f'{"、".join(attempted)}），请检查密钥或更换文本模型')
                    else:
                        message = (f'AI 分析失败：{reason}（已重试 {retries} 次、尝试模型 '
                                   f'{"、".join(attempted)}），请稍后再试或更换文本模型')
                    summary_error = EcommerceAnalysisError(
                        self._clean(message), error.status_code,
                        self._clean(error.body_excerpt), retryable=False,
                        switchable=False)
                    # Keep the complete Chinese candidate summary for the UI;
                    # status classification remains available for diagnostics.
                    summary_error.user_message = self._clean(message)
                    raise summary_error from None
                except Cancelled:
                    raise
                except Exception:
                    raise EcommerceAnalysisError('AI 分析失败：分析处理异常，请检查配置或诊断日志',
                                                 retryable=False, switchable=False) from None
            if failed:
                continue

            try:
                items = parse_prompt_items(json.dumps(raw_items, ensure_ascii=False),
                                           carousel_count, detail_count)
            except EcommerceAnalysisError as error:
                # The analysis function returned after its successful response; never switch here.
                message = f'分析模型尝试失败（{"、".join(attempted)}）：{self._clean(error)}'
                raise EcommerceAnalysisError(message, 200, self._clean(error.body_excerpt)) from None
            return items, reasoning
        if failures and all(getattr(failure, 'status_code', None) in (401, 403)
                            for failure in failures):
            raise EcommerceAnalysisError(
                f'密钥无效或没有该模型的调用权限（已尝试模型 {"、".join(attempted)}），请检查密钥或更换文本模型',
                retryable=False, switchable=False)
        raise EcommerceAnalysisError(f'分析模型尝试失败（{"、".join(attempted)}）')

    def _image_candidates(self, model):
        return self._unique_models([model, self.image_model, *self.image_model_candidates,
                                    *self._catalog_model_ids('image')])[:3]

    def _image_protocol_metadata(self, model):
        catalog = self.model_catalog.get('image', {}) if isinstance(self.model_catalog, dict) else {}
        record = catalog.get(normalize_model_id(model), {}) if isinstance(catalog, dict) else {}
        return record if isinstance(record, dict) else {}

    def _image_protocol(self, model):
        return preferred_image_protocol(model, self._image_protocol_metadata(model))

    def _generate_image_by_protocol(self, base_url, api_key, model, prompt, ratio, tier, *,
                                    reference_paths=None):
        protocol = self._image_protocol(model)
        if protocol == 'gemini':
            endpoint = f"/models/{model}:generateContent"
            self._log(f'图片模型 {model} 使用 Gemini 协议，端点 {endpoint}', 'info')
            try:
                return manual_image.generate_gemini_image(
                    base_url, api_key, model, prompt, ratio, tier, reference_paths or ())
            except manual_image.ManualImageError as error:
                raise EcommerceImageError(
                    str(error), getattr(error, 'status_code', None),
                    getattr(error, 'body_excerpt', ''),
                    retry_safe=getattr(error, 'retryable', False)) from None
            except Exception:
                raise EcommerceImageError('Gemini 图片请求失败，请检查线路配置或参考图') from None
        endpoint = '/images/edits' if reference_paths else '/images/generations'
        self._log(f'图片模型 {model} 使用 OpenAI 协议，端点 {endpoint}', 'info')
        return generate_image(
            base_url, api_key, model, prompt, ratio, tier,
            reference_paths=reference_paths)

    @staticmethod
    def _multi_image_rejection(error):
        if getattr(error, 'status_code', None) != 400:
            return False
        body = str(getattr(error, 'body_excerpt', '') or error).lower()
        category = classify_http_error(400, body, kind='image')['category']
        if category in ('authentication', 'content_policy', 'rate_limit'):
            return False
        if any(word in body for word in ('unauthorized', 'api key', 'authentication',
                                         'rate limit', 'quota', 'permission', '限流', '鉴权')):
            return False
        return bool(re.search(
            r'image[s]?\s*(count|number|limit)|'
            r'(too many|maximum|only one|single).{0,40}image|'
            r'image.{0,40}(too many|maximum|only one|single)|'
            r'(unsupported|not support).{0,20}(multiple|multi[- ]?|more than one)\s*image|'
            r'(multiple|multi[- ]?)\s*image.{0,30}(unsupported|not support)|'
            r'图片.{0,20}(数量|过多|上限)|不支持多图', body))

    def _request_image(self, item, cancel_event=None):
        candidates = self._image_candidates(item.get('model') or self.image_model)
        group = self.store.get_group(item['group_id']) if item.get('group_id') else None
        config = (group or {}).get('config') or {}
        styles = config.get('style_image_paths') or []
        references = list(item['reference_paths'])
        fallback = references[:len(references) - len(styles) + 1] if len(styles) > 1 else None
        reduced = False
        attempted, failures = [], []
        for position, model in enumerate(candidates):
            attempted.append(model)
            try:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise Cancelled('已取消，未发起图片请求')
                    try:
                        prompt = item['prompt']
                        if reduced:
                            prompt = append_reference_order(prompt.split('\n\nReference image order (critical):')[0],
                                                            len(references) - 1, style_count=1)
                        result = self.image_func(
                            self.image_base_url, self.image_api_key, model, prompt, item['ratio'], item['tier'],
                            reference_paths=references)
                        if reduced:
                            result.warnings = list(result.warnings) + [tr('中转站限制多图，本次仅使用第1张风格图')]
                        break
                    except EcommerceImageError as error:
                        if not reduced and fallback is not None and self._multi_image_rejection(error):
                            references = fallback
                            reduced = True
                            self._log(tr('中转站限制多图，本次仅使用第1张风格图'), 'warning')
                            continue
                        raise
                return result, model
            except EcommerceImageError as error:
                if cancel_event is not None and cancel_event.is_set():
                    raise Cancelled('已取消，不再重试图片请求') from None
                failures.append(error)
                current_protocol = self._image_protocol(model)
                next_protocol = self._image_protocol(candidates[position + 1]) if position + 1 < len(candidates) else ''
                if (not self._multi_image_rejection(error)
                        and not (current_protocol == 'gemini' and next_protocol == 'openai')
                        and position + 1 < len(candidates) and self._can_skip_to_next(error)):
                    next_model = candidates[position + 1]
                    self._log(tr('图片模型 {old} 不可用（{reason}），切换 {new}').format(
                        old=model, reason=self._clean(self._user_error(error)), new=next_model), 'warning')
                    continue
                if failures and all(getattr(failure, 'status_code', None) in (401, 403)
                                    for failure in failures):
                    message = (f'密钥无效或没有该模型的调用权限（已尝试模型 '
                               f'{"、".join(attempted)}），请检查密钥或更换图片模型')
                    summary_error = EcommerceImageError(
                        message, error.status_code,
                        self._clean(error.body_excerpt),
                        error.failed_before_response,
                        retry_safe=False)
                    summary_error.user_message = self._clean(message)
                    raise summary_error from None
                if len(attempted) > 1:
                    message = f'图片模型尝试失败（{"、".join(attempted)}）：{self._clean(self._user_error(error))}'
                    summary_error = EcommerceImageError(
                        message, error.status_code,
                        self._clean(error.body_excerpt),
                        error.failed_before_response,
                        retry_safe=error.retry_safe)
                    summary_error.user_message = self._clean(message)
                    raise summary_error from None
                raise
        if failures and all(getattr(failure, 'status_code', None) in (401, 403)
                            for failure in failures):
            summary_error = EcommerceImageError(
                f'密钥无效或没有该模型的调用权限（已尝试模型 {"、".join(attempted)}），请检查密钥或更换图片模型',
                retry_safe=False)
            summary_error.user_message = self._clean(summary_error.message)
            raise summary_error
        raise EcommerceImageError(f'图片模型尝试失败（{"、".join(attempted)}）')

    def _clean(self, value):
        return redact_text(value, (self.api_key, self.analysis_api_key, self.image_api_key))

    def _safe_copy(self, value):
        return redact_structure(deepcopy(value), self._clean)

    @staticmethod
    def _callback(callback, *args):
        if callback is not None:
            try:
                callback(*args)
            except Exception:
                # A presentation callback must not turn a completed billable request into failure.
                pass

    def _log(self, message, level='info'):
        message = self._clean(message)
        self._callback(self.on_log, message, level)
        if callable(self.log):
            self._callback(self.log, message, level)
        elif self.log is not None:
            self._callback(getattr(self.log, level, None), message)

    def _check_persistence(self):
        if not self.store.persistence_available and not self._warned_persistence:
            self._warned_persistence = True
            self._log('电商图数据库不可用，记录暂存于内存；文件输出继续，但重启后无法保证恢复。', 'warning')

    def _group(self, record):
        with self.store.lock:
            previous = self.store.get_group(record['group_id'])
            updated_at = time.time()
            if previous and updated_at <= previous['updated_at']:
                updated_at = math.nextafter(previous['updated_at'], math.inf)
            saved = self.store.upsert_group(self._safe_copy(dict(record, updated_at=updated_at)))
        self._check_persistence()
        self._callback(self.on_group, deepcopy(saved))
        return saved

    def _item(self, record):
        saved = self.store.upsert_item(self._safe_copy(dict(record, updated_at=time.time())))
        self._check_persistence()
        self._callback(self.on_item, deepcopy(saved))
        return saved

    def _touch_group(self, group_id, *, totals=None):
        try:
            touch = getattr(self.store, 'touch_group', None)
            if touch is None:
                return None
            if totals is None:
                totals = self.store.item_counts(group_id)
            saved = touch(group_id, totals=totals)
            if saved is not None:
                # Item 的通知可能已被界面消费；提交新时间戳后再通知，避免漏掉缓存失效。
                self._callback(self.on_group, self._safe_copy(saved))
            return saved
        except Exception:
            with self._diagnostics_lock:
                warn = not self._warned_touch
                self._warned_touch = True
            if warn:
                self._log('电商图组刷新失败，文件输出继续；任务列表将在任务结束后更新。', 'warning')
            return None

    def timings_snapshot(self):
        """Return independent diagnostic records without exposing live worker state."""
        with self._diagnostics_lock:
            return deepcopy(list(self.timings))

    @contextmanager
    def _running(self, group_id):
        key = (str(self.store.path.resolve()).casefold(), group_id)
        with self._locks_guard:
            lock = self._group_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._group_locks[key] = lock
        if not lock.acquire(blocking=False):
            raise ValueError('该电商图组正在运行，请等待当前任务结束')
        try:
            yield
        finally:
            lock.release()

    def _output_directory(self, name, group_id, created_at):
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '', name or '')[:60].strip().rstrip('. ')
        name = name or group_id[:12]
        stem = f'{name}_{time.strftime("%Y%m%d_%H%M", time.localtime(created_at))}'
        parent = self.output_root / '电商图'
        parent.mkdir(parents=True, exist_ok=True)
        candidate = parent / stem
        # Reserve the directory so groups created in the same minute cannot overwrite each other.
        while True:
            try:
                candidate.mkdir()
                return str(candidate.resolve())
            except FileExistsError:
                candidate = parent / f'{stem}_{group_id[:12]}_{uuid4().hex[:6]}'

    def start_group(self, platforms, product_name, product_info, language,
                    carousel_count, detail_count, carousel_ratio, detail_ratio, tier,
                    product_image_paths=None, style_image_path=None, *, cancel_event=None,
                    thinking=False, style_image_paths=None):
        """Run synchronously; image callbacks execute on the pool's worker threads."""
        selected = _resolve_platforms(platforms)
        conflicts = detect_conflicts(selected)
        for conflict in conflicts:
            if conflict['type'] == 'background':
                raise ValueError(conflict['tip'])
        system = build_system(selected, language, carousel_count, detail_count,
                              carousel_ratio, detail_ratio)
        user = build_user(product_name, product_info, [p['name'] for p in selected],
                          carousel_count, detail_count, carousel_ratio, detail_ratio)
        image_dimensions(carousel_ratio, tier)
        image_dimensions(detail_ratio, tier)
        if isinstance(product_image_paths, (str, bytes, os.PathLike)):
            raise ValueError('产品图必须以有序路径列表提供')
        products = list(product_image_paths or [])
        if len(products) > 6:
            raise ValueError('产品图最多 6 张')
        if style_image_path is not None and not isinstance(style_image_path, (str, os.PathLike)):
            raise ValueError('风格图最多 1 张，必须提供单个路径')
        if isinstance(style_image_paths, (str, bytes, os.PathLike)):
            raise ValueError('风格图必须以有序路径列表提供')
        styles = list(style_image_paths if style_image_paths is not None else
                      ([style_image_path] if style_image_path else []))
        if len(styles) > 3:
            raise ValueError('风格图最多3张')
        styles = [str(Path(p).expanduser().resolve()) for p in styles]
        references = [str(Path(p).expanduser().resolve()) for p in products]
        references.extend(styles)
        for conflict in conflicts:
            self._log(conflict['tip'] + ' 本次使用显式指定的比例。', 'warning')

        group_id, now = uuid4().hex, time.time()
        with self._running(group_id):
            output_dir = self._output_directory(product_name, group_id, now)
            self._group(dict(
                group_id=group_id, created_at=now, updated_at=now, status='analyzing',
                product_name=product_name or '', platforms=selected, output_dir=output_dir,
                config=dict(analysis_model=self.analysis_model, image_model=self.image_model,
                            carousel_ratio=carousel_ratio, detail_ratio=detail_ratio, tier=tier,
                            carousel_count=carousel_count, detail_count=detail_count,
                            language=language, product_info=product_info,
                            reference_paths=references, style_image_paths=styles,
                            product_image_paths=references[:len(products)], thinking=bool(thinking))))
            try:
                self._log(
                    tr('分析线路：{host} · 模型 {model}').format(
                        host=self._line_host(self.analysis_base_url), model=self.analysis_model), 'info')
                self._log(
                    tr('图片线路：{host} · 模型 {model}').format(
                        host=self._line_host(self.image_base_url), model=self.image_model), 'info')
                last_reasoning = [float('-inf')]
                reasoning_lock = threading.Lock()

                def log_reasoning(chunk):
                    now = time.monotonic()
                    with reasoning_lock:
                        if now - last_reasoning[0] < 1.0:
                            return
                        last_reasoning[0] = now
                    self._log('[思考中] ' + str(chunk)[:200], 'info')

                items, _reasoning = self._run_analysis(
                    system, append_reference_order(user, len(products), style_count=len(styles)),
                    carousel_count, detail_count, thinking, log_reasoning, cancel_event,
                    reference_paths=references, style_image_paths=styles,
                    fallback_paths=references[:len(products) + 1] if len(styles) > 1 else None)
            except Cancelled:
                # No prompts exist yet. Keep every planned output visibly skipped,
                # with a manifest, without submitting analysis or image requests.
                for kind, count, ratio in (('carousel', carousel_count, carousel_ratio),
                                           ('detail', detail_count, detail_ratio)):
                    for seq in range(1, count + 1):
                        self._item(dict(group_id=group_id, kind=kind, seq=seq,
                                        title='AI 分析已取消，未生成提示词', prompt='',
                                        reference_paths=references, ratio=ratio, tier=tier,
                                        model=self.image_model, status='cancelled', error='已取消 AI 分析'))
                self._finalize(group_id, cancelled=True)
                return group_id
            except Exception as error:
                message = self._clean(self._user_error(error))
                if not message.startswith('AI 分析失败：'):
                    message = 'AI 分析失败：' + message
                self._group(dict(group_id=group_id, status='failed', error=message))
                summary_error = EcommerceAnalysisError(
                    message, getattr(error, 'status_code', None),
                    self._clean(getattr(error, 'body_excerpt', '')))
                summary_error.user_message = self._clean(message)
                raise summary_error from None
            records = []
            for item in items:
                records.append(dict(
                    group_id=group_id, kind=item['type'], seq=item['index'], title=item['desc'],
                    prompt=append_reference_order(item['prompt'], len(products), style_count=len(styles)),
                    reference_paths=references,
                    ratio=carousel_ratio if item['type'] == 'carousel' else detail_ratio,
                    tier=tier, model=self.image_model, status='pending'))
            self.store.upsert_items_many(self._safe_copy(records))
            self._check_persistence()
            for item in self.store.list_items(group_id):
                self._callback(self.on_item, deepcopy(item))
            self._execute(group_id, self.store.list_items(group_id), cancel_event)
        return group_id

    def _generate(self, group, item, cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            self._item(dict(item, status='cancelled', error='已取消，未发起图片请求'))
            return
        item = self._item(dict(item, status='running', error=''))
        # Cancellation may arrive during persistence/callback processing.
        if cancel_event is not None and cancel_event.is_set():
            self._item(dict(item, status='cancelled', error='已取消，未发起图片请求'))
            return
        started = time.monotonic()
        requested = None
        write_started = None
        timing = dict(kind=item['kind'], seq=item['seq'], model=item.get('model') or self.image_model,
                      delivery=None, request_seconds=0., decode_seconds=None,
                      write_seconds=0., total_seconds=0., status='failed')
        try:
            result, used_model = self._request_image(item, cancel_event)
            requested = time.monotonic()
            timing.update(model=used_model, delivery=getattr(result, 'delivery', 'b64'),
                          request_seconds=requested - started)
            item = dict(item, model=used_model)
            name = ('轮' if item['kind'] == 'carousel' else '详') + str(item['seq']) + '.png'
            path = Path(group['output_dir']).resolve() / name
            write_started = time.monotonic()
            _atomic_write(path, result.png_bytes)
            if not _valid_file(path):
                raise OSError('图片输出文件不存在或为空')
            written = time.monotonic()
            timing.update(write_seconds=written - write_started, total_seconds=written - started,
                          status='succeeded')
            self._deliveries[item['item_id']] = getattr(result, 'delivery', 'b64')
            self._item(dict(item, status='succeeded', file_path=str(path), size=result.size,
                            image_tokens=result.image_tokens, warnings=list(result.warnings), error=''))
            self._touch_group(group['group_id'])
        except Cancelled as error:
            timing.update(total_seconds=time.monotonic() - started, status='cancelled')
            self._item(dict(item, status='cancelled', error=self._clean(str(error))))
        except Exception as error:
            finished = time.monotonic()
            timing.update(total_seconds=finished - started, status='failed')
            if requested is None:
                timing['request_seconds'] = finished - started
            elif write_started is not None:
                timing['write_seconds'] = finished - write_started
            message = self._clean(self._user_error(error))
            self._item(dict(item, status='failed', error=message))
            self._touch_group(group['group_id'])
            self._log(f'{item["kind"]} {item["seq"]} 出图失败：{message}', 'error')
        finally:
            with self._diagnostics_lock:
                self.timings.append(timing)

    def _execute(self, group_id, items, cancel_event):
        group = self._group(dict(group_id=group_id, status='generating', error=''))
        error = ''
        try:
            try:
                Path(group['output_dir']).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                error = self._clean(f'输出目录不可用：{exc}')
                raise
            with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix='ecommerce-image') as pool:
                remaining = iter(items)
                active = set()
                def fill_slots():
                    while len(active) < self.concurrency and not (cancel_event and cancel_event.is_set()):
                        item = next(remaining, None)
                        if item is None:
                            break
                        active.add(pool.submit(self._generate, group, item, cancel_event))
                fill_slots()
                settled_cancel = False
                while active:
                    done, active = wait(active, timeout=0.02, return_when=FIRST_COMPLETED)
                    for future in done:
                        try:
                            future.result()
                        except Exception as exc:
                            # Settle a crashed worker after the pool drains; unrelated
                            # items can still run, without retrying a billed request.
                            error = self._clean(f'图片任务执行失败：{exc}')
                            self._log(error, 'error')
                    if cancel_event is not None and cancel_event.is_set():
                        if not settled_cancel:
                            for item in remaining:
                                self._item(dict(item, status='cancelled', error='已取消，未发起图片请求'))
                            settled_cancel = True
                    else:
                        fill_slots()
        except Exception as exc:
            error = error or self._clean(f'图片任务执行失败：{exc}')
            self._log(error, 'error')
        # The pool has stopped before settling leftovers. Never issue another
        # request for a worker that failed after a potentially billed response.
        cancelled = cancel_event is not None and cancel_event.is_set()
        for item in self.store.list_items(group_id):
            if item['status'] in ('pending', 'running'):
                status = 'cancelled' if cancelled else ('skipped' if item['status'] == 'pending' else 'failed')
                message = error or ('已取消，未发起图片请求' if cancelled else '图片任务未完成')
                self._item(dict(item, status=status, error=message))
        self._touch_group(group_id)
        self._finalize(group_id, error=error, cancelled=cancelled)

    def _finalize(self, group_id, *, error='', cancelled=False):
        group = self.store.get_group(group_id)
        items = self.store.list_items(group_id)
        counts = self.store.item_counts(group_id)
        total = len(items)
        if cancelled:
            status = 'cancelled'
        elif error:
            status = 'failed'
        elif total and counts['succeeded'] == total:
            status = 'succeeded'
        elif counts['succeeded']:
            status = 'partial'
        elif counts['skipped'] or counts['cancelled']:
            status = 'cancelled'
        else:
            status = 'failed'
        config = group['config']
        previous_deliveries = {}
        previous_manifest = group.get('manifest')
        if isinstance(previous_manifest, dict):
            for previous in previous_manifest.get('items', ()):
                if not isinstance(previous, dict):
                    continue
                key = (previous.get('kind'), previous.get('seq'))
                delivery = previous.get('delivery')
                if key[0] and type(key[1]) is int and delivery in ('b64', 'url'):
                    previous_deliveries[key] = delivery
        manifest = dict(
            group_id=group_id, product_name=group['product_name'], product_info=config.get('product_info', ''),
            platforms=group['platforms'], created_at=group['created_at'], status=status,
            analysis_model=config.get('analysis_model'), image_model=config.get('image_model'),
            carousel_ratio=config.get('carousel_ratio'), detail_ratio=config.get('detail_ratio'),
            tier=config.get('tier'), carousel_count=config.get('carousel_count'),
            detail_count=config.get('detail_count'), language=config.get('language'),
            totals=dict(counts, total=total), persistence_available=self.store.persistence_available,
            items=[dict(kind=i['kind'], seq=i['seq'], title=i['title'], prompt=i['prompt'],
                        file=i['file_path'], size=i['size'], image_tokens=i['image_tokens'],
                        model=i['model'], delivery=self._deliveries.get(
                            i['item_id'], previous_deliveries.get((i['kind'], i['seq']), 'b64')),
                        status=i['status'], warnings=i['warnings'], error=i['error'])
                   for i in items])
        manifest = self._safe_copy(manifest)
        if error:
            manifest['error'] = error
        try:
            _atomic_write(Path(group['output_dir']) / 'manifest.json',
                          (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8'),
                          durable=True)
        except OSError as exc:
            status, error = 'failed', error or self._clean(f'输出清单写入失败：{exc}')
            manifest.update(status=status, error=error)
            self._log(error, 'error')
        return self._group(dict(group_id=group_id, status=status, totals=manifest['totals'],
                                manifest=manifest, error=error))

    def _load_group(self, group_id):
        group = self.store.get_group(group_id)
        if group is None:
            raise ValueError('电商图组不存在或未持久化，无法恢复')
        items = self.store.list_items(group_id)
        if not items:
            raise ValueError('该组尚无分析结果，无法恢复图片生成')
        return group, items

    def resume_group(self, group_id, *, cancel_event=None):
        """Reuse persisted prompts; skipped items require explicit regeneration."""
        with self._running(group_id):
            _group, items = self._load_group(group_id)
            pending = [i for i in items if i['status'] in ('pending', 'running', 'failed')
                       or (i['status'] == 'succeeded' and not _valid_file(i['file_path']))]
            self._execute(group_id, pending, cancel_event)
        return group_id

    def regenerate_items(self, group_id, item_keys, *, cancel_event=None):
        with self._running(group_id):
            _group, items = self._load_group(group_id)
            by_id = {i['item_id']: i for i in items}
            selected = {}
            for key in item_keys:
                if isinstance(key, tuple) and len(key) == 2:
                    kind, seq = key
                    if kind not in ('carousel', 'detail') or type(seq) is not int:
                        raise ValueError(f'无效的图片任务：{key}')
                    key = f'{group_id}:{kind}:{seq}'
                if not isinstance(key, str) or key not in by_id:
                    raise ValueError(f'图片任务不属于该组：{key}')
                selected[key] = by_id[key]
            if selected:
                self._execute(group_id, list(selected.values()), cancel_event)
        return group_id
