"""Stream to a temporary file; commit only complete downloads."""
from datetime import datetime
from pathlib import Path
import os
import re
import tempfile
from .http_client import HttpClient


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


class VideoDownloader(HttpClient):
    def __init__(self, overwrite_existing=False, check_cancel=None, **kwargs):
        super().__init__(**kwargs)
        self.overwrite_existing = overwrite_existing
        self.check_cancel = check_cancel or (lambda: None)

    def download_video(self, url, save_dir, filename):
        partial = None
        try:
            folder = Path(save_dir).expanduser().resolve()
            folder.mkdir(parents=True, exist_ok=True)
            destination = folder / safe_filename(filename)
            if destination.resolve().parent != folder:
                raise ValueError('下载路径超出输出目录')
            if destination.exists() and not self.overwrite_existing:
                self.log(f'文件已存在，跳过下载：{destination}', 'warning')
                return str(destination)
            self.check_cancel()
            # Never forward the provider Bearer key to a returned CDN URL.
            with self.request('GET', url, authenticated=False, stream=True, headers={'Accept-Encoding': 'identity'}) as response:
                length = int(response.headers.get('Content-Length', 0))
                descriptor, partial = tempfile.mkstemp(prefix='.' + destination.stem[:40], suffix='.part', dir=folder)
                count, last_percent = 0, -1
                with os.fdopen(descriptor, 'wb') as stream:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        self.check_cancel()
                        if not chunk:
                            continue
                        stream.write(chunk)
                        count += len(chunk)
                        percent = min(100, int(count * 100 / length)) if length else None
                        if percent is not None and percent >= last_percent + 5:
                            self.log(f'下载进度：{percent}%', 'info')
                            last_percent = percent
                    stream.flush()
                    os.fsync(stream.fileno())
                self.check_cancel()
                if not count or (length and not response.headers.get('Content-Encoding') and count != length):
                    raise IOError(f'下载不完整：预期 {length} 字节，收到 {count} 字节')
                if destination.exists() and not self.overwrite_existing:
                    self.log(f'文件已由其他任务保存，跳过覆盖：{destination}', 'warning')
                elif self.overwrite_existing:
                    os.replace(partial, destination)
                    partial = None
                else:
                    # Exclusive publication also protects against a destination created during streaming.
                    if os.name == 'nt':
                        # Windows rename refuses existing destinations and also works on non-NTFS disks.
                        os.rename(partial, destination)
                        partial = None
                    else:
                        os.link(partial, destination)
                self.log(f'下载完成：{destination} ({count / 1024 / 1024:.2f} MB)', 'success')
                return str(destination)
        except Exception as error:
            self.log(f'下载失败：{error}', 'error')
            raise
        finally:
            if partial and Path(partial).exists():
                Path(partial).unlink()
