"""Pure Python ecommerce pipeline; callers choose their own UI/thread boundary."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from uuid import uuid4
from weakref import WeakValueDictionary

from core.log_redaction import redact_structure, redact_text
from .analysis import DEFAULT_ANALYSIS_MODEL, EcommerceAnalysisError, analyze_and_parse, parse_prompt_items
from .image_client import DEFAULT_IMAGE_MODEL, generate_image, image_dimensions
from .platforms import _resolve_platforms, detect_conflicts
from .prompt_builder import append_reference_order, build_system, build_user


def _valid_file(path):
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _atomic_write(path, content):
    """Publish only a complete file, leaving the previous result intact on failure."""
    if not content:
        raise OSError('输出内容为空')
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.stem + '-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
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
                 on_group=None, on_item=None, on_log=None):
        self.base_url = base_url
        self.api_key = api_key
        self.output_root = Path(output_root).expanduser().resolve()
        self.store = store
        self.analysis_model = analysis_model
        self.image_model = image_model
        self.concurrency = max(1, int(concurrency))
        self.log = log
        self.analysis_func = analysis_func if analysis_func is not None else analyze_and_parse
        self.image_func = image_func if image_func is not None else generate_image
        self.on_group, self.on_item, self.on_log = on_group, on_item, on_log
        self._warned_persistence = False

    @classmethod
    def build_from_config(cls, config_manager, store=None):
        config = config_manager.config
        api, ecommerce = config.get('api', {}), config.get('ecommerce', {})
        output = (config.get('paths', {}).get('output') or config.get('output_dir')
                  or config_manager.path.parent / 'output')
        return cls(
            api.get('base_url', ''), api.get('api_key', ''), output,
            store if store is not None else config_manager.ecommerce_store(),
            ecommerce.get('analysis_model', DEFAULT_ANALYSIS_MODEL),
            ecommerce.get('image_model', DEFAULT_IMAGE_MODEL),
            ecommerce.get('max_concurrency', config.get('task_strategy', {}).get('max_concurrency', 5)))

    def _clean(self, value):
        return redact_text(value, (self.api_key,))

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
        saved = self.store.upsert_group(self._safe_copy(dict(record, updated_at=time.time())))
        self._check_persistence()
        self._callback(self.on_group, deepcopy(saved))
        return saved

    def _item(self, record):
        saved = self.store.upsert_item(self._safe_copy(dict(record, updated_at=time.time())))
        self._check_persistence()
        self._callback(self.on_item, deepcopy(saved))
        return saved

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
                    product_image_paths=None, style_image_path=None, *, cancel_event=None):
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
        has_style = bool(style_image_path)
        references = [str(Path(p).expanduser().resolve()) for p in products]
        if has_style:
            references.append(str(Path(style_image_path).expanduser().resolve()))
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
                            reference_paths=references)))
            try:
                items, _reasoning = self.analysis_func(
                    self.base_url, self.api_key, self.analysis_model, system, user,
                    carousel_count, detail_count)
                # Keep injection and future adapters subject to the same strict contract.
                items = parse_prompt_items(json.dumps(items, ensure_ascii=False),
                                           carousel_count, detail_count)
            except Exception as error:
                message = self._clean(f'AI 分析失败：{error}')
                self._group(dict(group_id=group_id, status='failed', error=message))
                raise EcommerceAnalysisError(
                    message, getattr(error, 'status_code', None),
                    self._clean(getattr(error, 'body_excerpt', ''))) from None
            records = []
            for item in items:
                records.append(dict(
                    group_id=group_id, kind=item['type'], seq=item['index'], title=item['desc'],
                    prompt=append_reference_order(item['prompt'], len(products), has_style),
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
            self._item(dict(item, status='skipped', error='已取消，未发起图片请求'))
            return
        item = self._item(dict(item, status='running', error=''))
        # Cancellation may arrive during persistence/callback processing.
        if cancel_event is not None and cancel_event.is_set():
            self._item(dict(item, status='skipped', error='已取消，未发起图片请求'))
            return
        try:
            result = self.image_func(
                self.base_url, self.api_key, item['model'], item['prompt'], item['ratio'], item['tier'],
                reference_paths=list(item['reference_paths']))
            name = ('轮' if item['kind'] == 'carousel' else '详') + str(item['seq']) + '.png'
            path = Path(group['output_dir']).resolve() / name
            _atomic_write(path, result.png_bytes)
            if not _valid_file(path):
                raise OSError('图片输出文件不存在或为空')
            self._item(dict(item, status='succeeded', file_path=str(path), size=result.size,
                            image_tokens=result.image_tokens, warnings=list(result.warnings), error=''))
        except Exception as error:
            message = self._clean(str(error))
            self._item(dict(item, status='failed', error=message))
            self._log(f'{item["kind"]} {item["seq"]} 出图失败：{message}', 'error')

    def _execute(self, group_id, items, cancel_event):
        group = self._group(dict(group_id=group_id, status='generating', error=''))
        try:
            Path(group['output_dir']).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            error = self._clean(f'输出目录不可用：{exc}')
            self._log(error, 'error')
            self._group(dict(group_id=group_id, status='failed', error=error,
                             totals=dict(self.store.item_counts(group_id),
                                         total=len(self.store.list_items(group_id)))))
            return
        with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix='ecommerce-image') as pool:
            futures = [pool.submit(self._generate, group, item, cancel_event) for item in items]
            for future in futures:
                future.result()
        self._finalize(group_id)

    def _finalize(self, group_id):
        group = self.store.get_group(group_id)
        items = self.store.list_items(group_id)
        counts = self.store.item_counts(group_id)
        total = len(items)
        if total and counts['succeeded'] == total:
            status = 'succeeded'
        elif counts['succeeded']:
            status = 'partial'
        elif counts['skipped']:
            status = 'cancelled'
        else:
            status = 'failed'
        config = group['config']
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
                        status=i['status'], warnings=i['warnings'], error=i['error']) for i in items])
        manifest = self._safe_copy(manifest)
        error = ''
        try:
            _atomic_write(Path(group['output_dir']) / 'manifest.json',
                          (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
        except OSError as exc:
            status, error = 'failed', self._clean(f'输出清单写入失败：{exc}')
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
