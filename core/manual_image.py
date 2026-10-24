"""Independent prompt-to-image tasks using the configured image route."""
from __future__ import annotations

import base64
import binascii
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
from urllib.parse import quote
from uuid import uuid4

import requests

from core.api_errors import classify_connection_error, classify_http_error
from core.ecommerce import image_client
from core.http_client import HttpClient, RequestError
from core.line_routing import resolve_line
from core.model_catalog import normalize_model_id


TASK_STATUSES = ('pending', 'running', 'succeeded', 'failed', 'cancelled')
_SCHEMA = """
CREATE TABLE IF NOT EXISTS image_tasks (
    local_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    status TEXT NOT NULL,
    record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_manual_image_created ON image_tasks(created_at DESC);
"""


class ManualImageError(RuntimeError):
    """A short Chinese task error plus the metadata needed for safe fallback."""

    def __init__(self, message, status_code=None, *, retryable=False,
                 category='image', body_excerpt=''):
        self.status_code = status_code
        self.retryable = bool(retryable)
        self.category = str(category or 'image')
        self.body_excerpt = str(body_excerpt or '')[:500]
        super().__init__(str(message))


def _log(log, message, level='info'):
    if not callable(log):
        return
    try:
        log(message, level)
    except Exception:
        pass


class ManualImageStore:
    """Small independent SQLite store with an in-memory failure fallback."""

    def __init__(self, path=None, log=None):
        if path is None:
            from core.config_manager import default_config_path
            path = default_config_path().with_name('manual_generations.sqlite3')
        self.path = Path(path)
        self.log = log
        self.lock = threading.RLock()
        self.persistence_available = False
        self._warned = False
        self._closed = False
        self._cache = {}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as conn:
                conn.executescript(_SCHEMA)
                conn.commit()
            self.persistence_available = True
            with self.lock:
                self._refresh()
        except (OSError, sqlite3.Error):
            self._disable()

    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=15)
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _disable(self):
        self.persistence_available = False
        if not self._warned:
            self._warned = True
            _log(self.log, '图片生成任务数据库不可用，任务历史暂存于内存', 'warning')

    def _refresh(self):
        if not self.persistence_available or self._closed:
            return
        try:
            with self._connection() as conn:
                rows = conn.execute('SELECT local_id, record FROM image_tasks').fetchall()
            records = {}
            for local_id, encoded in rows:
                record = json.loads(encoded)
                if isinstance(record, dict) and record.get('local_id') == local_id:
                    records[local_id] = record
            self._cache = records
        except (OSError, sqlite3.Error, ValueError, TypeError):
            self._disable()

    def upsert(self, record):
        value = deepcopy(dict(record))
        local_id = str(value.get('local_id') or '')
        if not local_id:
            raise ValueError('图片生成任务缺少任务编号')
        if value.get('status') not in TASK_STATUSES:
            raise ValueError('图片生成任务状态无效')
        value['local_id'] = local_id
        encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)
        with self.lock:
            self._refresh()
            if self.persistence_available:
                try:
                    with self._connection() as conn:
                        with conn:
                            conn.execute(
                                'INSERT INTO image_tasks(local_id,created_at,status,record) VALUES(?,?,?,?) '
                                'ON CONFLICT(local_id) DO UPDATE SET created_at=excluded.created_at,'
                                'status=excluded.status,record=excluded.record',
                                (local_id, float(value.get('created_at') or 0), value['status'], encoded))
                except (OSError, sqlite3.Error):
                    self._disable()
            self._cache[local_id] = value
            return deepcopy(value)

    def list(self, limit=200):
        with self.lock:
            self._refresh()
            records = sorted(self._cache.values(),
                             key=lambda item: (float(item.get('created_at') or 0),
                                               str(item.get('local_id') or '')),
                             reverse=True)
            if limit is not None:
                records = records[:max(0, int(limit))]
            return deepcopy(records)

    def delete(self, local_id):
        with self.lock:
            self._refresh()
            key = str(local_id)
            existed = key in self._cache
            if self.persistence_available:
                try:
                    with self._connection() as conn:
                        with conn:
                            conn.execute('DELETE FROM image_tasks WHERE local_id=?', (key,))
                except (OSError, sqlite3.Error):
                    self._disable()
            self._cache.pop(key, None)
            return existed

    def count(self, status=None):
        records = self.list(limit=None)
        return sum(status is None or item.get('status') == status for item in records)

    def close(self):
        # Connections are scoped to each operation, so no connection is held here.
        with self.lock:
            self._closed = True


def get_manual_image_store(config_manager, log=None):
    store = getattr(config_manager, '_manual_image_store', None)
    if store is None:
        path = Path(config_manager.path).with_name('manual_generations.sqlite3')
        store = ManualImageStore(path, log=log)
        config_manager._manual_image_store = store
    return store


def _safe_response_error(status, body, *, api_key=''):
    classification = classify_http_error(status, body, kind='image')
    category = classification['category']
    messages = {
        'content_policy': '内容被上游安全审核拒绝，请修改提示词或参考图',
        'authentication': '线路鉴权失败，请检查 API 密钥与模型权限',
        'rate_limit': '请求过于频繁，请稍后重试',
        'model_not_found': '模型不存在或线路未开通该模型',
        'upstream': '图片服务暂时不可用，请稍后重试',
    }
    message = messages.get(category, '图片生成请求失败，请检查模型和参数')
    if api_key:
        body = str(body).replace(api_key, '[REDACTED]')
    return ManualImageError(message, status, retryable=classification['retryable'],
                            category=category, body_excerpt=body)


def generate_gemini_image(base_url, api_key, model, prompt, ratio, tier, refs):
    """Call Gemini generateContent and return the shared ImageResult shape."""
    plan = image_client.build_size_plan(ratio, tier)
    parts = [{'text': prompt}]
    for value in refs or ():
        path = Path(value)
        try:
            content = path.read_bytes()
        except (OSError, TypeError, ValueError):
            raise ManualImageError('读取参考图失败，请检查文件是否存在', category='reference') from None
        mime_type = mimetypes.guess_type(path.name)[0] or 'image/png'
        if mime_type not in ('image/png', 'image/jpeg', 'image/webp'):
            mime_type = 'image/png'
        parts.append({'inlineData': {'mimeType': mime_type,
                                     'data': base64.b64encode(content).decode('ascii')}})

    client = HttpClient(api_key=api_key, log=lambda *_: None, timeout=(15, 180))
    url = base_url.rstrip('/') + '/models/' + quote(str(model), safe='') + ':generateContent'
    body = {
        'contents': [{'role': 'user', 'parts': parts}],
        'generationConfig': {
            'responseModalities': ['TEXT', 'IMAGE'],
            'imageConfig': {'aspectRatio': ratio},
        },
    }
    response = None
    try:
        # Some relay stations accept only the Google header while others reuse
        # the shared Bearer authentication convention. Send both so protocol
        # selection remains compatible with either gateway.
        response = client.request(
            'POST', url, check_status=False, json=body,
            headers={'Authorization': 'Bearer ' + api_key,
                     'x-goog-api-key': api_key})
        response.content
        if not 200 <= response.status_code < 300:
            raise _safe_response_error(response.status_code, client.redact(response.text), api_key=api_key)
        try:
            payload = response.json()
        except ValueError:
            raise ManualImageError('图片服务返回了无法识别的响应', response.status_code,
                                   category='invalid_response') from None
        candidates = payload.get('candidates') if isinstance(payload, dict) else None
        candidate = candidates[0] if isinstance(candidates, list) and candidates else None
        content_obj = candidate.get('content') if isinstance(candidate, dict) else None
        response_parts = content_obj.get('parts') if isinstance(content_obj, dict) else None
        encoded = None
        if isinstance(response_parts, list):
            for item in response_parts:
                if not isinstance(item, dict):
                    continue
                inline = item.get('inlineData') or item.get('inline_data')
                if isinstance(inline, dict) and inline.get('data'):
                    encoded = inline.get('data')
                    break
        if not isinstance(encoded, str) or not encoded.strip():
            raise ManualImageError('图片服务没有返回生成图片', response.status_code,
                                   category='empty_image')
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ManualImageError('图片服务返回的图片数据无效', response.status_code,
                                   category='invalid_image') from None
        image_format = image_client._image_format(image_bytes)
        if image_format == 'PNG':
            png_bytes = image_bytes
        elif image_format:
            png_bytes, _warnings = image_client._to_png(image_bytes, image_format, client.redact)
        else:
            raise ManualImageError('图片服务返回的格式不受支持', response.status_code,
                                   category='invalid_image')
        size = plan[0]
        return image_client.ImageResult(
            png_bytes=png_bytes, model=model, size=size['size'], tier_used=size['tier'],
            quality=size['quality'], endpoint='/models/{model}:generateContent',
            delivery='b64')
    except ManualImageError:
        raise
    except (RequestError, requests.RequestException) as error:
        classification = classify_connection_error(error)
        raise ManualImageError(classification['message'], retryable=classification['retryable'],
                               category=classification['category']) from None
    finally:
        if response is not None:
            response.close()
        client.close()


def _generate_openai_eight_refs(base_url, api_key, model, prompt, ratio, tier, refs):
    """Eight-reference variant; 0-7 references always use the shared client."""
    plan = image_client.build_size_plan(ratio, tier)
    client = HttpClient(api_key=api_key, timeout=(15, 180))
    status_code = None
    try:
        files = image_client._reference_files(refs, client.redact)
        endpoint = '/images/edits'
        url = base_url.strip().rstrip('/') + endpoint
        warnings = []
        for position, candidate in enumerate(plan):
            status_code = None
            body = dict(model=model, prompt=prompt, n=1, size=candidate['size'],
                        quality=candidate['quality'], response_format='b64_json')
            without_format = False
            while True:
                request_body = dict(body)
                if without_format:
                    request_body.pop('response_format', None)
                with client.request('POST', url, check_status=False, stream=True,
                                    allow_redirects=False, data=request_body, files=files) as response:
                    status_code = response.status_code
                    response.content
                    if not 200 <= status_code < 300:
                        excerpt = client.redact(response.text)[:500]
                        classification = classify_http_error(status_code, excerpt, kind='image')
                        if classification['category'] in ('content_policy', 'authentication'):
                            raise image_client.EcommerceImageError(
                                classification['message'], status_code, response.text,
                                redact=client.redact, retry_safe=False)
                        if not without_format and image_client._is_response_format_rejection(
                                status_code, response.text):
                            warnings.append('服务不支持 response_format，已移除参数重试一次')
                            without_format = True
                            continue
                        if (image_client._is_size_rejection(status_code, response.text)
                                and position + 1 < len(plan)):
                            warnings.append(f"{candidate['tier']} 尺寸 {candidate['size']} 被拒绝，尝试下一候选尺寸")
                            break
                        raise image_client.EcommerceImageError(
                            classification['message'], status_code, response.text,
                            redact=client.redact, retry_safe=classification['retryable'])
                    png_bytes, revised_prompt, image_tokens, delivery, decode_warnings = (
                        image_client._decode_image(response, client.redact))
                warnings.extend(decode_warnings)
                if position:
                    warnings.append(f"尺寸已从 {tier} 回退至 {candidate['tier']} {candidate['size']}")
                return image_client.ImageResult(
                    png_bytes=png_bytes, model=model, size=candidate['size'],
                    tier_used=candidate['tier'], quality=candidate['quality'],
                    endpoint=endpoint, revised_prompt=revised_prompt,
                    image_tokens=image_tokens, warnings=warnings, delivery=delivery)
    except image_client.EcommerceImageError:
        raise
    except (RequestError, requests.RequestException) as error:
        raise image_client.EcommerceImageError(
            classify_connection_error(error)['message'], status_code,
            failed_before_response=status_code is None, redact=client.redact,
            retry_safe=getattr(error, 'retry_safe', False)) from None
    finally:
        client.close()


def _endpoint_unsupported(error):
    status = getattr(error, 'status_code', None)
    if status in (401, 403) or getattr(error, 'category', '') == 'content_policy':
        return False
    if status in (404, 405):
        return True
    if status != 400:
        return False
    body = str(getattr(error, 'body_excerpt', '') or error).casefold()
    if classify_http_error(status, body, kind='image')['category'] == 'content_policy':
        return False
    return any(marker in body for marker in (
        'unsupported', 'unknown endpoint', 'not found', 'endpoint does not exist',
        'unknown route', '不支持的端点', '端点不存在'))


def _preferred_protocol(config_manager, model):
    model_text = str(model or '')
    gemini_name = bool(re.search(r'gemini|imagen|nano[\s_-]?banana', model_text, re.I))
    hints = []
    explicit_protocols = []
    config = getattr(config_manager, 'config', {})
    pool = config.get('image_model_pool', {}) if isinstance(config, dict) else {}
    for item in pool.get('models', ()) if isinstance(pool, dict) else ():
        if not isinstance(item, dict) or normalize_model_id(item.get('name')) != model_text:
            continue
        for key in ('protocol', 'image_protocol'):
            value = str(item.get(key) or '').strip().casefold()
            if value:
                explicit_protocols.append(value)
        hints.append(' '.join(str(item.get(key) or '') for key in
                              ('protocol', 'protocol_hint', 'provider', 'endpoint',
                               'family', 'description')))
    controller = getattr(config_manager, 'model_catalog_controller', None)
    if controller is not None:
        try:
            record = controller.snapshot_for('image').get(model_text, {})
        except Exception:
            record = {}
        if isinstance(record, dict):
            for key in ('protocol', 'image_protocol'):
                value = str(record.get(key) or '').strip().casefold()
                if value:
                    explicit_protocols.append(value)
            hints.append(' '.join(str(record.get(key) or '') for key in
                                  ('protocol', 'protocol_hint', 'provider', 'family', 'description')))
    # A model-specific protocol is authoritative.  This matters for custom
    # model ids whose names do not contain a provider hint (for example a
    # relay alias configured with protocol='gemini').
    for value in explicit_protocols:
        if any(marker in value for marker in ('gemini', 'google', 'imagen')):
            return 'gemini'
        if any(marker in value for marker in ('openai', 'open_ai', 'dall', 'azure')):
            return 'openai'
    line = resolve_line(config, 'image')
    source = str(line.get('source') or '')
    hints.append(' '.join(str(line.get(key) or '') for key in
                          ('protocol', 'image_protocol', 'provider', 'description')))
    stations = config.get('stations', ()) if isinstance(config, dict) else ()
    values = stations.values() if isinstance(stations, dict) else stations
    for station in values or ():
        if not isinstance(station, dict) or str(station.get('id')) != source:
            continue
        hints.append(' '.join(str(station.get(key) or '') for key in
                              ('protocol', 'image_protocol', 'provider', 'description')))
    hint_text = ' '.join(hints).casefold()
    if gemini_name and any(marker in hint_text for marker in ('gemini', 'google', 'imagen')):
        return 'gemini'
    return 'openai'


class ManualImageManager:
    """Create independent image records and run each batch without UI blocking."""

    def __init__(self, config_manager, store, log=None, on_task=None):
        self.config_manager = config_manager
        self.store = store
        self.log = log
        self.on_task = on_task
        self._slots = threading.BoundedSemaphore(2)
        self._state_lock = threading.RLock()
        self._active_ids = set()
        self._closed = threading.Event()
        self._cancel_event = threading.Event()

    def _publish(self, record):
        value = deepcopy(record)
        value['updated_at'] = time.time()
        self.store.upsert(value)
        if callable(self.on_task):
            try:
                self.on_task(deepcopy(value))
            except Exception:
                _log(self.log, '图片生成任务状态通知失败', 'warning')
        return value

    def prepare_tasks(self, prompt, model, model_version='', ratio='9:16', tier='1K',
                      count=1, reference_paths=()):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 10000:
            raise ValueError('提示词不能为空且不能超过 10000 个字符')
        model = normalize_model_id(model)
        model_version = normalize_model_id(model_version) if model_version else ''
        wire_model = model_version or model
        if not model or not wire_model:
            raise ValueError('请选择图片生成模型')
        if type(count) is not int or not 1 <= count <= 10:
            raise ValueError('生成次数必须为 1 到 10')
        if isinstance(reference_paths, (str, bytes, bytearray)):
            raise ValueError('参考图必须是路径列表')
        refs = tuple(str(path) for path in (reference_paths or ()))
        if len(refs) > 8:
            raise ValueError('参考图最多 8 张')
        image_client.build_size_plan(ratio, tier)
        config = getattr(self.config_manager, 'config', {})
        line = resolve_line(config, 'image')
        protocol = _preferred_protocol(self.config_manager, wire_model)
        now = time.time()
        tasks = []
        for index in range(1, count + 1):
            record = {
                'local_id': uuid4().hex,
                'kind': 'image',
                'prompt': prompt,
                'prompt_name': prompt[:60],
                'model': model,
                'model_version': model_version,
                'ratio': ratio,
                'tier': tier,
                'count_index': index,
                'status': 'pending',
                'protocol': protocol,
                'files': [],
                'error': '',
                'created_at': now + (index - 1) * 1e-6,
                'finished_at': None,
                'duration_ms': None,
                'output_dir': '',
                'reference_paths': list(refs),
            }
            tasks.append(self._publish(record))
        return tasks

    def _is_cancelled(self, cancel_event):
        return (self._closed.is_set() or self._cancel_event.is_set()
                or bool(cancel_event and cancel_event.is_set()))

    def run_tasks(self, tasks, cancel_event=None):
        records = [deepcopy(dict(task)) for task in (tasks or ())]
        if not records:
            return []
        # An idle cancellation request belongs to no future batch.
        with self._state_lock:
            if not self._active_ids:
                self._cancel_event.clear()
        config = getattr(self.config_manager, 'config', {})
        line = resolve_line(config, 'image')
        output_root = (config.get('paths', {}).get('output') or config.get('output_dir')
                       if isinstance(config, dict) else '')
        if not output_root:
            output_root = Path(self.config_manager.path).parent / 'output'
        output_root = Path(output_root).expanduser()
        refs_by_id = {record['local_id']: tuple(record.get('reference_paths') or ())
                      for record in records}

        def execute(record):
            local_id = record.get('local_id')
            if not local_id:
                record['status'] = 'failed'
                record['error'] = '图片生成任务编号无效'
                return record
            with self._state_lock:
                if local_id in self._active_ids:
                    current = next((item for item in self.store.list(limit=None)
                                    if item.get('local_id') == local_id), record)
                    return current
                self._active_ids.add(local_id)
            try:
                if record.get('status') != 'pending':
                    return record
                while not self._is_cancelled(cancel_event):
                    if self._slots.acquire(timeout=0.05):
                        break
                else:
                    record.update(status='cancelled', error='任务已取消',
                                  finished_at=time.time(), duration_ms=0)
                    return self._publish(record)
                try:
                    return self._execute_one(record, refs_by_id.get(local_id, ()),
                                             line, output_root, cancel_event)
                finally:
                    self._slots.release()
            finally:
                with self._state_lock:
                    self._active_ids.discard(local_id)

        try:
            with ThreadPoolExecutor(max_workers=min(2, len(records)),
                                    thread_name_prefix='manual-image') as executor:
                futures = [executor.submit(execute, record) for record in records]
                return [future.result() for future in futures]
        finally:
            # A cancellation request applies to the current batch. Clear it
            # after all workers have observed it so the manager can be reused
            # for a later submission while close() remains permanently terminal.
            if not self._closed.is_set():
                self._cancel_event.clear()

    def _execute_one(self, record, refs, line, output_root, cancel_event):
        started = time.monotonic()
        record = self._publish(dict(record, status='running', error='', finished_at=None))
        config = getattr(self.config_manager, 'config', {})
        base_url = line.get('base_url') or record.get('base_url') or ''
        api_key = line.get('api_key') or ''
        model = record.get('model_version') or record.get('model') or ''
        ratio, tier = record.get('ratio', '9:16'), record.get('tier', '1K')
        try:
            if not base_url:
                raise ManualImageError('尚未配置图片生成线路', category='configuration')
            if self._is_cancelled(cancel_event):
                record.update(status='cancelled', error='任务已取消')
                return self._finish(record, started)
            protocol = record.get('protocol') or _preferred_protocol(self.config_manager, model)
            try:
                result = self._generate(protocol, base_url, api_key, model,
                                        record['prompt'], ratio, tier, refs)
            except Exception as error:
                if protocol != 'openai' or not _endpoint_unsupported(error):
                    raise
                protocol = 'gemini'
                record['protocol'] = protocol
                result = generate_gemini_image(base_url, api_key, model,
                                               record['prompt'], ratio, tier, refs)
            content = getattr(result, 'png_bytes', None)
            if not isinstance(content, (bytes, bytearray)) or not content:
                raise ManualImageError('图片服务返回了空图片', category='invalid_image')
            folder = self._task_output_dir(output_root, record)
            filename = folder / f"img_{int(record['count_index'])}.png"
            _atomic_write(filename, bytes(content))
            record.update(status='succeeded', protocol=protocol, files=[str(filename)],
                          error='', output_dir=str(folder),
                          delivery=getattr(result, 'delivery', 'b64'))
            return self._finish(record, started)
        except Exception as error:
            message, category, status_code, retryable = self._classify_failure(error)
            record.update(status='failed', error=message, files=[])
            record['error_category'] = category
            if status_code is not None:
                record['error_status_code'] = status_code
            record['retryable'] = retryable
            _log(self.log, f'图片生成任务失败：{message}', 'warning')
            return self._finish(record, started)

    @staticmethod
    def _task_output_dir(output_root, record):
        prompt = str(record.get('prompt_name') or 'image')
        slug = re.sub(r'[^\w-]+', '_', prompt, flags=re.UNICODE).strip('_-')[:32] or 'image'
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        local_id = re.sub(r'[^A-Za-z0-9_-]+', '', str(record.get('local_id') or ''))[:12]
        suffix = f"_{local_id}" if local_id else ''
        folder = Path(output_root) / '单独生图' / f"{timestamp}_{slug}{suffix}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    @staticmethod
    def _classify_failure(error):
        if isinstance(error, ManualImageError):
            return str(error), error.category, error.status_code, error.retryable
        if isinstance(error, image_client.EcommerceImageError):
            status = error.status_code
            body = getattr(error, 'body_excerpt', '') or str(error)
            if status is not None:
                normalized = _safe_response_error(status, body)
                return str(normalized), normalized.category, status, normalized.retryable
            classification = classify_connection_error(error)
            return classification['message'], classification['category'], None, classification['retryable']
        if isinstance(error, RequestError) or isinstance(error, requests.RequestException):
            classification = classify_connection_error(error)
            return classification['message'], classification['category'], getattr(error, 'status_code', None), classification['retryable']
        if isinstance(error, (OSError, sqlite3.Error)):
            return '图片保存失败，请检查输出目录权限和磁盘空间', 'file_write', None, False
        if isinstance(error, ValueError):
            return '图片生成参数无效，请检查模型、宽高比与分辨率', 'invalid_parameters', None, False
        return '图片生成失败，请检查模型与线路后重试', 'unknown', None, False

    def _finish(self, record, started):
        record['finished_at'] = time.time()
        record['duration_ms'] = max(0, int((time.monotonic() - started) * 1000))
        return self._publish(record)

    @staticmethod
    def _generate(protocol, base_url, api_key, model, prompt, ratio, tier, refs):
        if protocol == 'gemini':
            return generate_gemini_image(base_url, api_key, model, prompt, ratio, tier, refs)
        if len(refs) == 8:
            return _generate_openai_eight_refs(base_url, api_key, model, prompt, ratio, tier, refs)
        return image_client.generate_image(base_url, api_key, model, prompt, ratio, tier,
                                           reference_paths=list(refs))

    def close(self):
        self._closed.set()

    def cancel(self):
        """Request cancellation of pending work without invalidating the manager."""
        self._cancel_event.set()

    create_tasks = prepare_tasks


def _atomic_write(path, content, *, durable=False):
    """Publish a complete image without forcing every image through fsync."""
    if not content:
        raise OSError('输出内容为空')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.stem + '-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            if durable:
                os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass
