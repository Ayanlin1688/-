"""Independent video creation tasks and history storage.

The manager deliberately sits beside the existing video automation pipeline.  It
uses the same configured video route and API client, but keeps its records and
output directories independent from workspace tasks.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import json
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

from .api_client import ApiClient
from .api_errors import classify_connection_error, classify_http_error
from .http_client import Cancelled, HttpClient, RequestError
from .line_routing import resolve_line
from .model_catalog import family_for
from .video_downloader import VideoDownloader, safe_filename


TASK_STATUSES = ('pending', 'running', 'succeeded', 'failed', 'cancelled')

_SCHEMA = """
CREATE TABLE IF NOT EXISTS video_tasks (
    local_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    status TEXT NOT NULL,
    record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_manual_video_created ON video_tasks(created_at DESC);
"""


class ManualVideoError(RuntimeError):
    """A user-safe error with enough metadata for protocol/error handling."""

    def __init__(self, message, status_code=None, *, retryable=False,
                 category='video', body_excerpt=''):
        self.status_code = status_code
        self.retryable = bool(retryable)
        self.category = str(category or 'video')
        self.body_excerpt = str(body_excerpt or '')[:500]
        super().__init__(str(message))


def _log(log, message, level='info'):
    if not callable(log):
        return
    try:
        log(message, level)
    except Exception:
        pass


class ManualVideoStore:
    """SQLite store for independent video tasks with memory fallback."""

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
            _log(self.log, '视频生成任务数据库不可用，任务历史暂存于内存', 'warning')

    def _refresh(self):
        if not self.persistence_available or self._closed:
            return
        try:
            with self._connection() as conn:
                rows = conn.execute('SELECT local_id, record FROM video_tasks').fetchall()
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
            raise ValueError('视频生成任务缺少任务编号')
        if value.get('status') not in TASK_STATUSES:
            raise ValueError('视频生成任务状态无效')
        value['local_id'] = local_id
        encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)
        with self.lock:
            self._refresh()
            if self.persistence_available:
                try:
                    with self._connection() as conn:
                        with conn:
                            conn.execute(
                                'INSERT INTO video_tasks(local_id,created_at,status,record) VALUES(?,?,?,?) '
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

    def get(self, local_id):
        key = str(local_id or '')
        with self.lock:
            self._refresh()
            value = self._cache.get(key)
            return deepcopy(value) if value is not None else None

    def delete(self, local_id):
        with self.lock:
            self._refresh()
            key = str(local_id)
            existed = key in self._cache
            if self.persistence_available:
                try:
                    with self._connection() as conn:
                        with conn:
                            conn.execute('DELETE FROM video_tasks WHERE local_id=?', (key,))
                except (OSError, sqlite3.Error):
                    self._disable()
            self._cache.pop(key, None)
            return existed

    def count(self, status=None):
        records = self.list(limit=None)
        return sum(status is None or item.get('status') == status for item in records)

    def close(self):
        with self.lock:
            self._closed = True


def get_manual_video_store(config_manager, log=None):
    store = getattr(config_manager, '_manual_video_store', None)
    if store is None:
        path = Path(config_manager.path).with_name('manual_generations.sqlite3')
        store = ManualVideoStore(path, log=log)
        config_manager._manual_video_store = store
    return store


def _safe_response_error(status, body, *, api_key=''):
    classification = classify_http_error(status, body, kind='video')
    if api_key:
        body = str(body).replace(api_key, '[REDACTED]')
    return ManualVideoError(classification['message'], status,
                            retryable=classification['retryable'],
                            category=classification['category'], body_excerpt=body)


def _json_response(client, response, api_key=''):
    if not 200 <= response.status_code < 300:
        raise _safe_response_error(response.status_code, client.redact(response.text), api_key=api_key)
    try:
        payload = response.json()
    except (ValueError, TypeError):
        raise ManualVideoError('视频服务返回了无法识别的响应', response.status_code,
                               category='invalid_response') from None
    if not isinstance(payload, dict):
        raise ManualVideoError('视频服务返回了无效响应', response.status_code,
                               category='invalid_response')
    return payload


def create_veo_task(base_url, api_key, model, prompt, ratio='9:16', duration=5,
                    output_size='', refs=None, client=None):
    """Create a Gemini Veo long-running operation through the shared HTTP stack."""
    own_client = client is None
    client = client or HttpClient(api_key=api_key, timeout=(10, 45))
    refs = refs if isinstance(refs, dict) else {}
    body = {
        'instances': [{'prompt': prompt}],
        'parameters': {'aspectRatio': ratio, 'durationSeconds': int(duration)},
    }
    if output_size:
        body['parameters']['outputSize'] = output_size
    # Gemini accepts image references as either URI strings or inline parts;
    # uploaded URLs are kept in the instances object for relay compatibility.
    images = refs.get('images') or refs.get('reference_images') or []
    if images:
        body['instances'][0]['images'] = list(images)
    url = base_url.rstrip('/') + '/models/' + quote(str(model), safe='/') + ':predictLongRunning'
    response = None
    try:
        response = client.request('POST', url, check_status=False, json=body,
                                  headers={'Authorization': 'Bearer ' + api_key,
                                           'x-goog-api-key': api_key})
        return _json_response(client, response, api_key)
    except ManualVideoError:
        raise
    except (RequestError, requests.RequestException) as error:
        classification = classify_connection_error(error)
        raise ManualVideoError(classification['message'], getattr(error, 'status_code', None),
                               retryable=classification['retryable'], category=classification['category']) from None
    finally:
        if response is not None:
            response.close()
        if own_client:
            client.close()


def fetch_veo_operation(base_url, api_key, model, operation_name, client=None):
    own_client = client is None
    client = client or HttpClient(api_key=api_key, timeout=(10, 45))
    url = base_url.rstrip('/') + '/models/' + quote(str(model), safe='/') + ':fetchPredictOperation'
    response = None
    try:
        response = client.request('POST', url, check_status=False,
                                  json={'operationName': operation_name, 'name': operation_name},
                                  headers={'Authorization': 'Bearer ' + api_key,
                                           'x-goog-api-key': api_key})
        return _json_response(client, response, api_key)
    except ManualVideoError:
        raise
    except (RequestError, requests.RequestException) as error:
        classification = classify_connection_error(error)
        raise ManualVideoError(classification['message'], getattr(error, 'status_code', None),
                               retryable=classification['retryable'], category=classification['category']) from None
    finally:
        if response is not None:
            response.close()
        if own_client:
            client.close()


def _walk_values(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key), item
            yield from _walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_values(item)


def _operation_result(payload):
    """Return (done, progress, url, bytes) from common Veo operation shapes."""
    done = bool(payload.get('done')) if isinstance(payload, dict) else False
    progress = None
    url = None
    encoded = None
    for key, value in _walk_values(payload):
        lower = key.casefold().replace('_', '')
        if progress is None and lower in {'progress', 'progresspercent', 'percent'}:
            try:
                progress = max(0, min(100, float(str(value).rstrip('%'))))
            except (TypeError, ValueError):
                pass
        if url is None and lower in {'uri', 'videouri', 'videourl', 'resulturl', 'url'} and isinstance(value, str):
            if value.startswith(('http://', 'https://', 'gs://')):
                url = value
        if encoded is None and lower in {'base64', 'videobase64', 'bytesbase64encoded', 'data'} and isinstance(value, str):
            # Avoid treating ordinary prompt/text fields as binary payloads.
            try:
                decoded = base64.b64decode(value, validate=True)
            except Exception:
                decoded = None
            if decoded and (b'ftyp' in decoded[:64] or len(decoded) > 1024):
                encoded = decoded
    if not done and (url or encoded):
        done = True
    return done, progress, url, encoded


def _atomic_write(path, content):
    if not content:
        raise OSError('输出内容为空')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.stem + '-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(bytes(content))
            stream.flush()
        os.replace(temporary, path)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass


class ManualVideoManager:
    """Create and poll independent video tasks, bounded to two workers."""

    def __init__(self, config_manager, store, log=None, on_task=None):
        self.config_manager = config_manager
        self.store = store
        self.log = log
        self.on_task = on_task
        self._slots = threading.BoundedSemaphore(2)
        self._state_lock = threading.RLock()
        self._active_ids = set()
        # Overlapping batches share one cancellation event. A later batch gets
        # a fresh event after the prior batch exits; already-running workers
        # keep their original, possibly-set event.
        self._running_batches = 0
        self._closed = threading.Event()
        self._cancel_event = threading.Event()
        self.poll_interval = 2.5
        self.poll_timeout = 15 * 60

    def _publish(self, record):
        value = deepcopy(record)
        value['updated_at'] = time.time()
        self.store.upsert(value)
        if callable(self.on_task):
            try:
                self.on_task(deepcopy(value))
            except Exception:
                _log(self.log, '视频生成任务状态通知失败', 'warning')
        return value

    def prepare_tasks(self, prompt, model, mode='text', ratio='9:16', output_size='',
                      duration=5, count=1, refs=None, reference_images=None,
                      reference_videos=None, reference_audios=None):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 5000:
            raise ValueError('画面描述不能为空且不能超过 5000 个字符')
        model = str(model or '').strip()
        if not model:
            raise ValueError('请选择视频生成模型')
        if mode not in ('text', 'reference'):
            raise ValueError('生成模式无效')
        if type(duration) is not int or not 4 <= duration <= 15:
            raise ValueError('渲染时长必须为 4 到 15 秒')
        if type(count) is not int or not 1 <= count <= 10:
            raise ValueError('生成次数必须为 1 到 10')
        if refs is None:
            refs = {}
        if not isinstance(refs, dict):
            raise ValueError('参考素材参数无效')
        refs = deepcopy(refs)
        if reference_images is not None:
            refs['images'] = list(reference_images)
        if reference_videos is not None:
            refs['videos'] = list(reference_videos)
        if reference_audios is not None:
            refs['audios'] = list(reference_audios)
        for key, limit in (('images', 3), ('videos', 3), ('audios', 3)):
            value = refs.get(key) or refs.get('reference_' + key) or []
            if not isinstance(value, (list, tuple)) or len(value) > limit:
                raise ValueError(f'参考{key}最多 {limit} 个')
        if any(refs.get(key) or refs.get('reference_' + key) for key in ('videos', 'audios')):
            raise ValueError('当前线路未配置视频/音频上传能力')
        config = getattr(self.config_manager, 'config', {})
        catalog = self._video_catalog()
        protocol = self._preferred_protocol(model, config, catalog)
        now = time.time()
        tasks = []
        for index in range(1, count + 1):
            record = {
                'local_id': uuid4().hex,
                'kind': 'video',
                'prompt': prompt,
                'prompt_name': prompt[:60],
                'model': model,
                'mode': mode,
                'ratio': ratio,
                'output_size': output_size,
                'duration': duration,
                'count_index': index,
                'status': 'pending',
                'progress': 0,
                'task_id': '',
                'protocol': protocol,
                'video_url': '',
                'file_path': '',
                'error': '',
                'created_at': now + (index - 1) * 1e-6,
                'finished_at': None,
                'duration_ms': None,
                'refs': refs,
                'output_dir': '',
            }
            tasks.append(self._publish(record))
        return tasks

    def _video_catalog(self):
        """Return the route-specific runtime catalog when one is available."""
        config = getattr(self.config_manager, 'config', {})
        if isinstance(config, dict) and config.get('_model_catalog') is not None:
            return config.get('_model_catalog')
        controller = getattr(self.config_manager, 'model_catalog_controller', None)
        if controller is not None:
            try:
                return controller.snapshot_for('video')
            except Exception:
                pass
        return None

    @staticmethod
    def _preferred_protocol(model, config, catalog=None):
        if re.search(r'(?:veo|gemini)', str(model), re.I):
            return 'gemini'
        try:
            family = str(family_for(model, catalog) or '').casefold()
            if 'gemini' in family or 'veo' in family:
                return 'gemini'
        except Exception:
            pass
        # A model/line metadata hint is authoritative where available.
        values = [config.get('video_protocol', '')] if isinstance(config, dict) else []
        pool = config.get('video_model_pool', {}) if isinstance(config, dict) else {}
        rows = pool.get('models', ()) if isinstance(pool, dict) else ()
        for row in rows:
            if isinstance(row, dict) and str(row.get('name') or row.get('id')) == str(model):
                values.extend(row.get(key, '') for key in ('protocol', 'provider', 'family'))
        text = ' '.join(map(str, values)).casefold()
        return 'gemini' if any(mark in text for mark in ('gemini', 'google', 'veo')) else 'openai'

    @staticmethod
    def _openai_endpoint_unsupported(error, record):
        """Allow one Gemini retry only when OpenAI submission was rejected.

        A task id means the provider accepted the request and may already have
        charged it; querying that task must never be replaced by a second
        protocol submission.  Only endpoint/model rejection responses qualify.
        """
        if record.get('task_id'):
            return False
        status = getattr(error, 'status_code', None)
        try:
            status = int(status) if status is not None else None
        except (TypeError, ValueError):
            status = None
        body = str(getattr(error, 'body', '') or error).casefold()
        if status is None:
            match = re.search(r'\bhttp\s+(\d{3})\b', body)
            status = int(match.group(1)) if match else None
        if status in {404, 405}:
            return True
        return status == 400 and any(marker in body for marker in (
            'unsupported', 'unknown endpoint', 'not found', 'endpoint not supported'))

    def _is_cancelled(self, cancel_event):
        return (self._closed.is_set() or self._cancel_event.is_set()
                or bool(cancel_event and cancel_event.is_set()))

    def run_tasks(self, tasks, cancel_event=None):
        records = [deepcopy(dict(task)) for task in (tasks or ())]
        if not records:
            return []
        with self._state_lock:
            if self._running_batches == 0 and self._cancel_event.is_set():
                self._cancel_event = threading.Event()
            self._running_batches += 1

        config = getattr(self.config_manager, 'config', {})
        line = resolve_line(config, 'video')
        output_root = config.get('paths', {}).get('output') if isinstance(config, dict) else ''
        if not output_root:
            output_root = Path(self.config_manager.path).parent / 'output'
        output_root = Path(output_root).expanduser()

        def execute(record):
            local_id = record.get('local_id')
            with self._state_lock:
                if local_id:
                    self._active_ids.add(local_id)
            try:
                if self._is_cancelled(cancel_event):
                    record.update(status='cancelled', error='任务已取消', finished_at=time.time())
                    return self._publish(record)
                while not self._is_cancelled(cancel_event):
                    if self._slots.acquire(timeout=.05):
                        break
                else:
                    record.update(status='cancelled', error='任务已取消', finished_at=time.time())
                    return self._publish(record)
                try:
                    return self._execute_one(record, line, output_root, cancel_event)
                finally:
                    self._slots.release()
            finally:
                with self._state_lock:
                    self._active_ids.discard(local_id)

        try:
            with ThreadPoolExecutor(max_workers=min(2, len(records)),
                                    thread_name_prefix='manual-video') as executor:
                futures = [executor.submit(execute, record) for record in records]
                return [future.result() for future in futures]
        finally:
            with self._state_lock:
                self._running_batches = max(0, self._running_batches - 1)

    def _execute_one(self, record, line, output_root, cancel_event):
        started = time.monotonic()
        record = self._publish(dict(record, status='running', error='', finished_at=None))
        base_url = line.get('base_url') or ''
        api_key = line.get('api_key') or ''
        model = record.get('model') or ''
        try:
            if not base_url:
                raise ManualVideoError('尚未配置视频生成线路', category='configuration')
            if self._is_cancelled(cancel_event):
                return self._finish(record, started, 'cancelled', '任务已取消')
            if record.get('protocol') == 'gemini':
                result = self._run_veo(record, base_url, api_key, cancel_event)
            else:
                try:
                    result = self._run_openai(record, base_url, api_key, cancel_event)
                except Exception as error:
                    if not self._openai_endpoint_unsupported(error, record):
                        raise
                    record['protocol'] = 'gemini'
                    self._publish(record)
                    result = self._run_veo(record, base_url, api_key, cancel_event)
            if self._is_cancelled(cancel_event):
                return self._finish(record, started, 'cancelled', '任务已取消')
            record.update(video_url=result.get('video_url', ''), progress=100)
            folder = self._task_output_dir(output_root, record)
            filename = folder / f"video_{int(record.get('count_index') or 1)}.mp4"
            if result.get('video_bytes'):
                _atomic_write(filename, result['video_bytes'])
            elif result.get('video_url'):
                downloader = VideoDownloader(api_key=api_key, acceleration=False,
                                             check_cancel=lambda: self._check_cancel(cancel_event))
                try:
                    downloaded = downloader.download_video(result['video_url'], folder,
                                                          filename.name,
                                                          generation_key=record.get('task_id') or record.get('local_id'))
                    filename = Path(downloaded)
                finally:
                    downloader.close()
            else:
                raise ManualVideoError('视频服务没有返回下载地址', category='empty_result')
            if self._is_cancelled(cancel_event):
                return self._finish(record, started, 'cancelled', '任务已取消')
            self._validate_mp4(filename)
            record.update(status='succeeded', file_path=str(filename), output_dir=str(folder), error='')
            return self._finish(record, started, 'succeeded', '')
        except Exception as error:
            if isinstance(error, ManualVideoError) and error.category == 'cancelled':
                return self._finish(record, started, 'cancelled', str(error))
            if self._is_cancelled(cancel_event):
                return self._finish(record, started, 'cancelled', '任务已取消')
            message, category, status, retryable = self._classify_failure(error)
            record.update(error=message, error_category=category, retryable=retryable)
            if status is not None:
                record['error_status_code'] = status
            _log(self.log, f'视频生成任务失败：{message}', 'warning')
            return self._finish(record, started, 'failed', message)

    def _check_cancel(self, event=None):
        if self._is_cancelled(event):
            raise Cancelled()

    def _run_openai(self, record, base_url, api_key, cancel_event):
        catalog = self._video_catalog()
        client = ApiClient(base_url=base_url, api_key=api_key, log=lambda msg, level='info': _log(self.log, msg, level))
        refs = record.get('refs') or {}
        family = family_for(record.get('model') or '', catalog)
        images = list(refs.get('images') or refs.get('reference_images') or [])
        if family != 'grok-imagine-1.5-video':
            images = self._reference_image_urls(images, cancel_event)
        resolution = self._api_resolution(record.get('model') or '', record.get('output_size') or '', catalog)
        params = {
            'duration': int(record.get('duration') or 5),
            'aspect_ratio': record.get('ratio') or '9:16',
            'ratio': record.get('ratio') or '9:16',
            'resolution': resolution,
            'output_size': record.get('output_size') or '',
            'videos': list(refs.get('videos') or refs.get('reference_videos') or []),
            'audios': list(refs.get('audios') or refs.get('reference_audios') or []),
            'generate_audio': True,
        }
        try:
            task_id = client.create_task(record['model'], record['prompt'], images, params, catalog)
            record['task_id'] = str(task_id)
            self._publish(record)
            deadline = time.monotonic() + self.poll_timeout
            while True:
                if self._is_cancelled(cancel_event):
                    raise ManualVideoError('任务已取消', category='cancelled')
                result = client.query_task(record['task_id'], record['model'], catalog)
                status = str(result.get('status') or 'queued').lower()
                progress = result.get('progress')
                if progress is not None:
                    try:
                        record['progress'] = max(0, min(100, float(progress)))
                    except (TypeError, ValueError):
                        pass
                self._publish(record)
                if status in {'completed', 'succeeded', 'success', 'done'}:
                    return {'video_url': result.get('result_url') or result.get('video_url') or result.get('url') or ''}
                if status in {'failed', 'error', 'cancelled', 'canceled'}:
                    failure = result.get('failure')
                    if isinstance(failure, dict) and failure.get('category') == 'upstream_reference_fetch_failed':
                        message = '参考图片暂时无法读取，请检查图片链接后重试'
                    else:
                        message = '视频生成失败，请修改提示词或检查模型线路后重试'
                    raise ManualVideoError(message, category='upstream')
                if time.monotonic() >= deadline:
                    raise ManualVideoError('视频生成轮询超时，请稍后重试', category='timeout', retryable=True)
                time.sleep(max(0.0, float(self.poll_interval)))
        finally:
            client.close()

    def _reference_image_urls(self, images, cancel_event=None):
        """Upload local reference images through the existing image uploader.

        Remote URLs are already usable by the video relay and are left intact.
        A failed local upload is surfaced as a task error instead of being sent
        as a false-success ``None`` reference.
        """
        values = list(images or [])
        local = [str(value) for value in values
                 if isinstance(value, (str, os.PathLike))
                 and not str(value).lower().startswith(('http://', 'https://', 'gs://'))]
        if not local:
            return values
        for path in local:
            if not Path(path).is_file():
                raise ManualVideoError('参考图片文件不存在，请重新选择', category='reference')
        try:
            from .image_uploader import ImageUploader
            uploader = ImageUploader.from_config(self.config_manager,
                                                 check_cancel=lambda: self._check_cancel(cancel_event))
            try:
                uploaded = uploader.upload_images(local, check_cancel=lambda: self._check_cancel(cancel_event))
            finally:
                uploader.close()
        except Cancelled:
            raise ManualVideoError('任务已取消', category='cancelled')
        except ManualVideoError:
            raise
        except Exception:
            raise ManualVideoError('参考图片上传失败，请检查图床线路后重试', category='reference', retryable=True) from None
        if len(uploaded) != len(local) or any(not isinstance(url, str) or not url.strip() for url in uploaded):
            raise ManualVideoError('参考图片上传失败，请检查图床线路后重试', category='reference', retryable=True)
        replacement = iter(uploaded)
        return [next(replacement) if str(value) in local else value for value in values]

    @staticmethod
    def _api_resolution(model, output_size, catalog=None):
        """Map UI dimensions (or labels) to the API client's resolution enum."""
        family = family_for(model, catalog)
        if family == 'video-v1':
            return ''
        text = str(output_size or '').strip()
        allowed = ()
        try:
            from .model_parameters import model_options
            allowed = tuple(model_options(model, catalog).get('resolutions') or ())
        except Exception:
            allowed = ()
        if text in allowed:
            return text
        if text in {'2K', '4K'} and text in allowed:
            return text
        match = re.search(r'(\d+)\s*x\s*(\d+)', text)
        longest = max((int(match.group(1)), int(match.group(2))), default=0) if match else 0
        if longest >= 1800 and '1080p' in allowed:
            return '1080p'
        if longest >= 1100 and '720p' in allowed:
            return '720p'
        if '480p' in allowed:
            return '480p'
        if allowed:
            return allowed[0]
        return text

    def _run_veo(self, record, base_url, api_key, cancel_event):
        client = HttpClient(api_key=api_key, timeout=(10, 45), log=lambda msg, level='info': _log(self.log, msg, level))
        try:
            refs = record.get('refs') or {}
            images = list(refs.get('images') or refs.get('reference_images') or [])
            if images:
                uploaded_images = self._reference_image_urls(images, cancel_event)
                refs = dict(refs, images=uploaded_images)
            operation = create_veo_task(base_url, api_key, record['model'], record['prompt'],
                                        record.get('ratio') or '9:16', record.get('duration') or 5,
                                        record.get('output_size') or '', refs, client=client)
            operation_name = operation.get('name') or operation.get('operation', {}).get('name')
            if not operation_name:
                done, progress, url, data = _operation_result(operation)
                if done and (url or data):
                    return {'video_url': url or '', 'video_bytes': data}
                raise ManualVideoError('视频服务返回了无效任务编号', category='invalid_response')
            record['task_id'] = str(operation_name)
            self._publish(record)
            deadline = time.monotonic() + self.poll_timeout
            while True:
                if self._is_cancelled(cancel_event):
                    raise ManualVideoError('任务已取消', category='cancelled')
                payload = fetch_veo_operation(base_url, api_key, record['model'], operation_name, client=client)
                done, progress, url, data = _operation_result(payload)
                if isinstance(payload, dict) and payload.get('error'):
                    raise ManualVideoError('视频生成失败，请稍后重试', category='upstream')
                if progress is not None:
                    record['progress'] = progress
                    self._publish(record)
                if done:
                    if url or data:
                        return {'video_url': url or '', 'video_bytes': data}
                    raise ManualVideoError('视频服务没有返回视频结果', category='empty_result')
                if time.monotonic() >= deadline:
                    raise ManualVideoError('视频生成轮询超时，请稍后重试', category='timeout', retryable=True)
                time.sleep(max(0.0, float(self.poll_interval)))
        finally:
            client.close()

    @staticmethod
    def _task_output_dir(output_root, record):
        prompt = str(record.get('prompt_name') or 'video')
        slug = re.sub(r'[^\w-]+', '_', prompt, flags=re.UNICODE).strip('_-')[:32] or 'video'
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        local_id = re.sub(r'[^A-Za-z0-9_-]+', '', str(record.get('local_id') or ''))[:12]
        suffix = '_' + local_id if local_id else ''
        folder = Path(output_root) / '单独视频' / f'{timestamp}_{slug}{suffix}'
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    @staticmethod
    def _validate_mp4(path):
        path = Path(path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise OSError('视频文件为空')
        with path.open('rb') as stream:
            header = stream.read(64)
        if b'ftyp' not in header:
            raise OSError('视频文件格式无效（缺少 MP4 标识）')

    def download_task(self, record, cancel_event=None):
        """Download a completed remote result and persist its local path.

        The method is intentionally synchronous so callers can run it in their
        existing worker thread.  Already downloaded files are returned after
        validation and are never fetched again.
        """
        value = deepcopy(dict(record or {}))
        local_path = Path(str(value.get('file_path') or '')) if value.get('file_path') else None
        if local_path and local_path.is_file():
            self._validate_mp4(local_path)
            return str(local_path)
        url = str(value.get('video_url') or '').strip()
        if not url:
            raise ManualVideoError('视频服务没有返回下载地址', category='empty_result')
        config = getattr(self.config_manager, 'config', {})
        output_root = config.get('paths', {}).get('output') if isinstance(config, dict) else ''
        if not output_root:
            output_root = Path(self.config_manager.path).parent / 'output'
        folder = Path(value.get('output_dir') or self._task_output_dir(Path(output_root), value))
        filename = f"video_{int(value.get('count_index') or 1)}.mp4"
        line = resolve_line(getattr(self.config_manager, 'config', {}) or {}, 'video')
        downloader = VideoDownloader(api_key=line.get('api_key') or '', acceleration=False,
                                     check_cancel=lambda: self._check_cancel(cancel_event))
        try:
            downloaded = Path(downloader.download_video(url, folder, filename,
                                                        generation_key=value.get('task_id') or value.get('local_id')))
        finally:
            downloader.close()
        if self._is_cancelled(cancel_event):
            raise ManualVideoError('任务已取消', category='cancelled')
        self._validate_mp4(downloaded)
        value.update(file_path=str(downloaded), output_dir=str(downloaded.parent))
        self._publish(value)
        return str(downloaded)

    download_result = download_task

    def _classify_failure(self, error):
        if isinstance(error, ManualVideoError):
            return str(error), error.category, error.status_code, error.retryable
        if isinstance(error, RequestError) or isinstance(error, requests.RequestException):
            status = getattr(error, 'status_code', None)
            if status is not None:
                body = getattr(error, 'body', '') or str(error)
                config = getattr(self.config_manager, 'config', {})
                line = resolve_line(config, 'video') if isinstance(config, dict) else {}
                api_key = str(line.get('api_key') or '')
                if api_key:
                    body = str(body).replace(api_key, '[REDACTED]')
                classified = classify_http_error(status, str(body), kind='video')
                return classified['message'], classified['category'], status, classified['retryable']
            classification = classify_connection_error(error)
            return classification['message'], classification['category'], getattr(error, 'status_code', None), classification['retryable']
        if isinstance(error, (OSError, sqlite3.Error)):
            return '视频保存失败，请检查输出目录权限和磁盘空间', 'file_write', None, False
        if isinstance(error, (TimeoutError,)):
            return '视频生成超时，请稍后重试', 'timeout', None, True
        return '视频生成失败，请检查模型与线路后重试', 'unknown', getattr(error, 'status_code', None), False

    def _finish(self, record, started, status, error=''):
        record['status'] = status
        record['error'] = error or ''
        record['finished_at'] = time.time() if status in ('succeeded', 'failed', 'cancelled') else None
        record['duration_ms'] = max(0, int((time.monotonic() - started) * 1000))
        if status == 'succeeded':
            record['progress'] = 100
        return self._publish(record)

    def close(self):
        self._closed.set()

    def cancel(self):
        self._cancel_event.set()

    create_tasks = prepare_tasks


__all__ = [
    'ManualVideoError', 'ManualVideoStore', 'ManualVideoManager',
    'get_manual_video_store', 'create_veo_task', 'fetch_veo_operation',
]
