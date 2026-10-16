"""Stream to a temporary file; commit only complete downloads."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import requests
from urllib3.exceptions import HTTPError
from urllib3.response import HTTPResponse
from .http_client import HttpClient, RequestError, Cancelled


def safe_filename(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(name)).strip().rstrip('. ')
    if not name or name in {'.', '..'}:
        name = 'video.mp4'
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', name, re.I):
        name = '_' + name
    if not name.lower().endswith('.mp4'):
        name += '.mp4'
    return name[:170] + '.mp4' if len(name) > 180 else name


def build_filename(rule, task, sequence):
    now = datetime.now()
    values = {'序号': f'{sequence:03d}', '提示词名': task['prompt_name'], '模型': task['model'],
              '日期': now.strftime('%Y%m%d'), '时间': now.strftime('%H%M%S'), 'task_id': task.get('task_id', '')}
    try:
        return safe_filename(rule.format_map(values))
    except (KeyError, ValueError) as error:
        raise ValueError(f'下载命名规则无效：{error}') from error


DOWNLOAD_TIMEOUT = (10, 120)
DOWNLOAD_ATTEMPTS = 4
DOWNLOAD_CHUNK_SIZE = 512 * 1024
PARALLEL_MIN_BYTES = 8 * 1024 * 1024


class IncompleteDownload(IOError):
    pass


class _RangeRejected(IncompleteDownload):
    pass


def _retryable_download(error):
    if isinstance(error, IncompleteDownload):
        return True
    if isinstance(error, IOError) and '下载响应不是视频' in str(error):
        return False
    if getattr(error, 'status_code', None) is not None:
        return False
    return isinstance(error, (RequestError, OSError, TimeoutError))


def _body_length(response):
    content_range = response.headers.get('Content-Range', '')
    if '/' in content_range:
        total = content_range.rsplit('/', 1)[-1].strip()
        if total.isdigit():
            return int(total)
    if response.status_code == 206:
        return 0
    length = response.headers.get('Content-Length', '')
    return int(length) if str(length).isdigit() else 0


def _content_range(response):
    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', '').strip())
    if not match:
        raise _RangeRejected('Content-Range 缺失或格式无效')
    start, end, total = map(int, match.groups())
    if not 0 <= start <= end < total:
        raise _RangeRejected('Content-Range 范围无效')
    length = response.headers.get('Content-Length')
    if length is not None and (not str(length).isdigit() or int(length) != end - start + 1):
        raise _RangeRejected('Content-Length 与 Content-Range 不一致')
    return start, end, total


def _strong_etag(response):
    value = response.headers.get('ETag', '').strip()
    return value if re.fullmatch(r'"[^"\r\n]*"', value) else None


def _encoded(response):
    return response.headers.get('Content-Encoding', '').strip().lower() not in {'', 'identity'}


def _reset_transfer(state):
    state.update(count=0, digest=hashlib.sha256(), last_percent=-1, length=0, etag=None, encoded=False)


class VideoDownloader(HttpClient):
    """Use four independent range sessions for large, strongly validated videos.

    Set acceleration=False for single-stream downloads, or parallel_workers=2..8
    to bound concurrency. Custom session transports retain single-stream behavior.
    """

    def __init__(self, overwrite_existing=False, check_cancel=None, *, acceleration=True, parallel_workers=4, **kwargs):
        if isinstance(parallel_workers, bool) or not isinstance(parallel_workers, int) or not 2 <= parallel_workers <= 8:
            raise ValueError('parallel_workers 必须为 2 到 8 的整数')
        super().__init__(**kwargs)
        self.overwrite_existing = overwrite_existing
        self.check_cancel = check_cancel or (lambda: None)
        self.acceleration = acceleration
        self.parallel_workers = parallel_workers

    def redact(self, text):
        return re.sub(r'\?[^\s\'"<>]+', '?[REDACTED]', super().redact(text))

    @staticmethod
    def _ownership_path(destination):
        return destination.with_name(destination.name + '.storyboard-output-diagnostics.json')

    def _owns_file(self, destination, identity):
        try:
            record = json.loads(self._ownership_path(destination).read_text(encoding='utf-8'))
            if not isinstance(record, dict) or record.get('identity') != identity:
                return False
            if not destination.is_file() or destination.stat().st_size != record.get('size_bytes'):
                return False
            digest = hashlib.sha256()
            with destination.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    self.check_cancel()
                    digest.update(chunk)
            return digest.hexdigest() == record.get('sha256')
        except (OSError, ValueError):
            return False

    def _destination(self, folder, filename, identity):
        original = folder / safe_filename(filename)
        candidate = original
        suffix = 1
        while True:
            if candidate.resolve().parent != folder:
                raise ValueError('下载路径超出输出目录')
            if not candidate.exists() and not candidate.is_symlink():
                return candidate, False
            if self._owns_file(candidate, identity):
                return candidate, True
            tail = '_' + identity[:12] + (f'_{suffix}' if suffix > 1 else '')
            candidate = folder / (original.stem[:140] + tail + '.mp4')
            suffix += 1

    def _save_ownership(self, destination, identity, size, digest):
        target = self._ownership_path(destination)
        descriptor, temporary = tempfile.mkstemp(prefix='.storyboard-output-', suffix='-diagnostics.json', dir=destination.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(dict(identity=identity, size_bytes=size, sha256=digest), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def download_video(self, url, save_dir, filename, *, generation_key=None):
        partial = None
        started = time.monotonic()
        try:
            folder = Path(save_dir).expanduser().resolve()
            folder.mkdir(parents=True, exist_ok=True)
            identity = hashlib.sha256(json.dumps(generation_key if generation_key is not None else url,
                                                 ensure_ascii=False).encode('utf-8')).hexdigest()
            destination, owned = self._destination(folder, filename, identity)
            if owned and not self.overwrite_existing:
                self.log(f'已核验当前任务的视频，跳过下载：{destination}', 'info')
                return str(destination)
            self.check_cancel()
            descriptor, partial = tempfile.mkstemp(prefix='.' + destination.stem[:40], suffix='.part', dir=folder)
            os.close(descriptor)
            state = dict(parallel_disabled=False, range_logged=False,
                         metrics=dict(started=time.monotonic(), first_byte=None, reported=False, lock=threading.Lock()))
            _reset_transfer(state)
            error = None
            for attempt in range(DOWNLOAD_ATTEMPTS):
                try:
                    self._transfer(url, partial, state)
                    error = None
                    break
                except Cancelled:
                    raise
                except Exception as transfer_error:
                    error = transfer_error
                    if attempt + 1 >= DOWNLOAD_ATTEMPTS or not _retryable_download(transfer_error):
                        raise
                    self.log(f'下载中断，保留已下载 {state["count"]} 字节后继续：{self.redact(transfer_error)}', 'warning')
            if error is not None:
                raise error
            count = state['count']
            while True:
                self.check_cancel()
                destination, owned = self._destination(folder, filename, identity)
                if owned and not self.overwrite_existing:
                    return str(destination)
                if owned and self.overwrite_existing:
                    os.replace(partial, destination)
                    partial = None
                else:
                    try:
                        # Exclusive publication handles another process taking this name mid-download.
                        if os.name == 'nt':
                            os.rename(partial, destination)
                            partial = None
                        else:
                            os.link(partial, destination)
                    except FileExistsError:
                        continue
                self._save_ownership(destination, identity, count, state['digest'].hexdigest())
                break
            elapsed = max(time.monotonic() - started, .001)
            self.log(f'下载完成：{destination} ({count / 1024 / 1024:.2f} MB，'
                     f'总耗时 {elapsed:.3f} 秒，平均吞吐量 {count / 1024 / 1024 / elapsed:.2f} MB/s)', 'success')
            return str(destination)
        except Exception as error:
            self.log(f'下载失败：{self.redact(error)}，总耗时 {time.monotonic() - started:.3f} 秒', 'error')
            raise
        finally:
            if partial and Path(partial).exists():
                Path(partial).unlink()

    def _transfer(self, url, partial, state):
        if state['count'] and state.get('encoded'):
            _reset_transfer(state)
        headers = {'Accept-Encoding': 'identity'}
        if state['count']:
            headers['Range'] = f'bytes={state["count"]}-'
            if state.get('etag'):
                headers['If-Range'] = state['etag']
        with self.request('GET', url, authenticated=False, stream=True, headers=headers, timeout=DOWNLOAD_TIMEOUT) as response:
            if response.status_code == 200:
                _reset_transfer(state)
            elif state['count'] and response.status_code != 206:
                raise RequestError(f'续传被拒绝：HTTP {response.status_code}', response.status_code)
            self._validate_content_type(response)
            if response.status_code not in {200, 206}:
                raise RequestError(f'下载响应无效：HTTP {response.status_code}', response.status_code)
            total = _body_length(response)
            end = None
            etag = _strong_etag(response)
            if response.status_code == 206:
                try:
                    start, end, total = _content_range(response)
                    if start != state['count'] or _encoded(response):
                        raise _RangeRejected('续传范围起点或 Content-Encoding 不一致')
                    if state['count'] and ((state['length'] and total != state['length'])
                                           or (state.get('etag') and etag != state['etag'])):
                        raise _RangeRejected('续传内容长度或 ETag 已变化')
                except _RangeRejected:
                    _reset_transfer(state)
                    raise
            if total:
                state['length'] = total
            state.update(etag=etag, encoded=_encoded(response))
            if self._parallel_eligible(response, state):
                response.close()
                try:
                    self._parallel_transfer(url, partial, state)
                    return
                except Cancelled:
                    raise
                except Exception as error:
                    state['parallel_disabled'] = True
                    self.check_cancel()
                    reason = str(error) if isinstance(error, _RangeRejected) else type(error).__name__
                    self.log(f'分段下载退回单流：{self.redact(reason)}；保留连续已验证前缀 {state["count"]} 字节', 'warning')
                    self._transfer(url, partial, state)
                    return
            with open(partial, 'r+b') as stream:
                stream.seek(state['count'])
                stream.truncate(state['count'])
                for chunk in self._chunks(response, state):
                    self.check_cancel()
                    self._report_first_byte(state)
                    if not state['encoded'] and ((total and state['count'] + len(chunk) > total)
                                                or (end is not None and state['count'] + len(chunk) > end + 1)):
                        _reset_transfer(state)
                        raise _RangeRejected('下载响应超过声明长度')
                    self._write_chunk(stream, chunk, state)
                stream.flush()
                os.fsync(stream.fileno())
            length = state['length']
            encoded = state['encoded']
        if not state['count'] or (length and not encoded and state['count'] != length):
            raise IncompleteDownload(f'下载不完整：预期 {length} 字节，收到 {state["count"]} 字节')

    @staticmethod
    def _validate_content_type(response):
        content_type = response.headers.get('Content-Type', '').partition(';')[0].strip().lower()
        if content_type.startswith('text/') or content_type in {'application/json', 'application/xml'} or content_type.endswith(('+json', '+xml')):
            raise IOError(f'下载响应不是视频：Content-Type={content_type}')

    def _chunks(self, response, state):
        raw = getattr(response, 'raw', None)
        if isinstance(raw, HTTPResponse) and callable(getattr(raw, 'read1', None)):
            def read_chunks():
                while True:
                    chunk = raw.read1(DOWNLOAD_CHUNK_SIZE, decode_content=True)
                    if not chunk:
                        break
                    yield chunk
            chunks = read_chunks()
        else:
            chunks = response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE)
        try:
            for chunk in chunks:
                if chunk:
                    metrics = state['metrics']
                    with metrics['lock']:
                        if metrics['first_byte'] is None:
                            metrics['first_byte'] = time.monotonic()
                    yield chunk
        except (HTTPError, requests.RequestException) as error:
            raise RequestError(f'下载连接中断：{type(error).__name__}') from None

    def _report_first_byte(self, state):
        metrics = state['metrics']
        with metrics['lock']:
            if metrics['reported'] or metrics['first_byte'] is None:
                return
            elapsed = metrics['first_byte'] - metrics['started']
            metrics['reported'] = True
        self.log(f'下载首字节：{elapsed:.3f} 秒', 'info')

    def _progress(self, state, count):
        percent = min(100, int(count * 100 / state['length'])) if state['length'] else None
        if percent is not None and percent >= state['last_percent'] + 5:
            self.log(f'下载进度：{percent}%', 'info')
            state['last_percent'] = percent

    def _write_chunk(self, stream, chunk, state):
        stream.write(chunk)
        state['digest'].update(chunk)
        state['count'] += len(chunk)
        self._progress(state, state['count'])

    def _parallel_eligible(self, response, state):
        if state['count'] or state['parallel_disabled']:
            return False
        supports_range = response.headers.get('Accept-Ranges', '').strip().lower() == 'bytes' or response.status_code == 206
        reason = ''
        if not self.acceleration:
            reason = '加速已关闭'
        elif not supports_range:
            reason = '服务端未声明字节范围支持'
        elif state['length'] < PARALLEL_MIN_BYTES:
            reason = '文件小于 8 MiB'
        elif state['encoded']:
            reason = '服务端返回压缩内容'
        elif not state['etag']:
            reason = '缺少强 ETag，无法保证分段版本一致'
        elif (type(self.session) is not requests.Session or self.session.auth is not None
              or any(self.session.hooks.values())
              or any(type(adapter) is not requests.adapters.HTTPAdapter for adapter in self.session.adapters.values())):
            reason = '自定义会话行为无法安全复制'
        if not state['range_logged']:
            self.log(f'Range 探测：HTTP {response.status_code}，声明字节范围支持={supports_range}，长度={state["length"]} 字节', 'info')
            self.log(f'分段下载未启用：{reason}' if reason else f'启用分段下载：{self.parallel_workers} 段，独立会话', 'info')
            state['range_logged'] = True
        return not reason

    def _segment_client(self):
        session = requests.Session()
        session.headers = self.session.headers.copy()
        session.cookies = self.session.cookies.copy()
        session.params = self.session.params.copy()
        session.proxies = self.session.proxies.copy()
        session.verify = self.session.verify
        session.cert = self.session.cert
        session.trust_env = self.session.trust_env
        session.max_redirects = self.session.max_redirects
        return HttpClient(session=session, log=lambda *args: None)

    def _download_segment(self, url, path, start, end, state, stop, counts, index, lock):
        if stop.is_set():
            raise Cancelled()
        client = self._segment_client()
        try:
            headers = {'Accept-Encoding': 'identity', 'Range': f'bytes={start}-{end}', 'If-Range': state['etag']}
            timeout = (DOWNLOAD_TIMEOUT[0], min(DOWNLOAD_TIMEOUT[1], 10))
            with client.request('GET', url, authenticated=False, check_status=False, stream=True, headers=headers, timeout=timeout) as response:
                if response.status_code != 206:
                    raise _RangeRejected(f'分段请求未返回 206：HTTP {response.status_code}')
                if _content_range(response) != (start, end, state['length']):
                    raise _RangeRejected('分段 Content-Range 与请求不一致')
                if _encoded(response) or _strong_etag(response) != state['etag']:
                    raise _RangeRejected('分段 Content-Encoding 或 ETag 不一致')
                self._validate_content_type(response)
                count = 0
                with path.open('xb') as stream:
                    for chunk in self._chunks(response, state):
                        if stop.is_set():
                            raise Cancelled()
                        count += len(chunk)
                        if count > end - start + 1:
                            raise _RangeRejected('分段响应超过声明长度')
                        stream.write(chunk)
                        with lock:
                            counts[index] = count
                if count != end - start + 1:
                    raise IncompleteDownload(f'分段下载不完整：预期 {end - start + 1} 字节，收到 {count} 字节')
        finally:
            client.close()

    def _merge_segments(self, paths, partial, state):
        with open(partial, 'wb') as stream:
            for path in paths:
                with path.open('rb') as segment:
                    while True:
                        self.check_cancel()
                        chunk = segment.read(DOWNLOAD_CHUNK_SIZE)
                        if not chunk:
                            break
                        self._write_chunk(stream, chunk, state)
            stream.flush()
            os.fsync(stream.fileno())

    def _parallel_transfer(self, url, partial, state):
        total = state['length']
        etag = state['etag']
        stop = threading.Event()
        lock = threading.Lock()
        counts = [0] * self.parallel_workers
        with tempfile.TemporaryDirectory(prefix='.storyboard-ranges-', dir=Path(partial).parent) as directory:
            paths = [Path(directory) / f'{index}.part' for index in range(self.parallel_workers)]
            executor = ThreadPoolExecutor(max_workers=self.parallel_workers, thread_name_prefix='video-range')
            futures = []
            error = None
            try:
                for index, path in enumerate(paths):
                    start = total * index // self.parallel_workers
                    end = total * (index + 1) // self.parallel_workers - 1
                    futures.append(executor.submit(self._download_segment, url, path, start, end, state, stop, counts, index, lock))
                pending = set(futures)
                while pending:
                    self.check_cancel()
                    completed, pending = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
                    self._report_first_byte(state)
                    for future in completed:
                        future.result()
                    with lock:
                        received = sum(counts)
                    self._progress(state, received)
                self.check_cancel()
            except Exception as transfer_error:
                error = transfer_error
            finally:
                stop.set()
                executor.shutdown(wait=True, cancel_futures=True)
            if isinstance(error, Cancelled):
                raise error
            completed_paths = []
            rejected = any(isinstance(future.exception(), _RangeRejected) for future in futures if not future.cancelled())
            if not rejected:
                for path, future in zip(paths, futures):
                    if future.cancelled() or future.exception() is not None:
                        break
                    completed_paths.append(path)
            _reset_transfer(state)
            state.update(length=total, etag=etag)
            self._merge_segments(completed_paths, partial, state)
            if error is not None:
                raise error
            if state['count'] != total:
                raise IncompleteDownload(f'分段合并长度不一致：预期 {total} 字节，收到 {state["count"]} 字节')
