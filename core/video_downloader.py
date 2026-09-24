"""Stream to a temporary file; commit only complete downloads."""
from datetime import datetime
from pathlib import Path
import hashlib
import json
import os
import re
import tempfile
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


class IncompleteDownload(IOError):
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


class VideoDownloader(HttpClient):
    def __init__(self, overwrite_existing=False, check_cancel=None, **kwargs):
        super().__init__(**kwargs)
        self.overwrite_existing = overwrite_existing
        self.check_cancel = check_cancel or (lambda: None)

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
            state = dict(count=0, digest=hashlib.sha256(), last_percent=-1, length=0)
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
                    self.log(f'下载中断，保留已下载 {state["count"]} 字节后继续：{transfer_error}', 'warning')
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
            self.log(f'下载完成：{destination} ({count / 1024 / 1024:.2f} MB)', 'success')
            return str(destination)
        except Exception as error:
            self.log(f'下载失败：{error}', 'error')
            raise
        finally:
            if partial and Path(partial).exists():
                Path(partial).unlink()

    def _transfer(self, url, partial, state):
        headers = {'Accept-Encoding': 'identity'}
        if state['count']:
            headers['Range'] = f'bytes={state["count"]}-'
        with self.request('GET', url, authenticated=False, stream=True, headers=headers, timeout=DOWNLOAD_TIMEOUT) as response:
            if state['count'] and response.status_code == 200:
                state.update(count=0, digest=hashlib.sha256(), last_percent=-1, length=0)
            elif state['count'] and response.status_code != 206:
                raise RequestError(f'续传被拒绝：HTTP {response.status_code}', response.status_code)
            content_type = response.headers.get('Content-Type', '').partition(';')[0].strip().lower()
            if content_type.startswith('text/') or content_type in {'application/json', 'application/xml'} or content_type.endswith(('+json', '+xml')):
                raise IOError(f'下载响应不是视频：Content-Type={content_type}')
            total = _body_length(response)
            if total:
                state['length'] = total
            with open(partial, 'r+b') as stream:
                stream.seek(state['count'])
                stream.truncate(state['count'])
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    self.check_cancel()
                    if not chunk:
                        continue
                    stream.write(chunk)
                    state['digest'].update(chunk)
                    state['count'] += len(chunk)
                    length = state['length']
                    percent = min(100, int(state['count'] * 100 / length)) if length else None
                    if percent is not None and percent >= state['last_percent'] + 5:
                        self.log(f'下载进度：{percent}%', 'info')
                        state['last_percent'] = percent
                stream.flush()
                os.fsync(stream.fileno())
            length = state['length']
            encoded = bool(response.headers.get('Content-Encoding'))
        if not state['count'] or (length and not encoded and state['count'] != length):
            raise IncompleteDownload(f'下载不完整：预期 {length} 字节，收到 {state["count"]} 字节')
