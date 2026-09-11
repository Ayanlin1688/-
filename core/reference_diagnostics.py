"""Opt-in image integrity diagnostics; never modifies or re-uploads image bytes."""
import hashlib
import mimetypes
from pathlib import Path
import tempfile
import requests

from PyQt5.QtGui import QImageReader
from .http_client import HttpClient, Cancelled


def image_metadata(path, check_cancel=lambda: None):
    path = Path(path)
    reader = QImageReader(str(path))
    size = reader.size()
    image_format = bytes(reader.format()).decode('ascii', errors='replace')
    if not size.isValid() or not image_format:
        raise ValueError('无法读取图片尺寸/格式：' + reader.errorString())
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b''):
            check_cancel(); digest.update(chunk)
    return dict(path=str(path.resolve()), filename=path.name, width=size.width(), height=size.height(),
                format=image_format, size_bytes=path.stat().st_size, sha256=digest.hexdigest(),
                content_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream')


def describe_image(info):
    return f"{info['width']}x{info['height']}, {info['size_bytes'] / 1024 / 1024:.3f}MB ({info['size_bytes']}字节), {info['format'].upper()}"


class AnonymousImageSession(requests.Session):
    """Public-image probes must not inherit netrc auth or cookies, even on redirects."""

    def __init__(self):
        super().__init__()
        self.trust_env = False

    @staticmethod
    def strip_credentials(request):
        for header in ('Authorization', 'Cookie', 'Proxy-Authorization'):
            request.headers.pop(header, None)

    def prepare_request(self, request):
        self.cookies.clear()
        prepared = super().prepare_request(request)
        self.strip_credentials(prepared)
        return prepared

    def rebuild_auth(self, prepared_request, response):
        # requests prepares redirect cookies before this hook. Strip them too.
        self.strip_credentials(prepared_request)


class ReferenceDiagnostics(HttpClient):
    MAX_BYTES = 32 * 1024 * 1024

    def __init__(self, log, check_cancel=lambda: None):
        # A dedicated unauthenticated session: no API key, token or upload cookies.
        super().__init__(log=log, timeout=(15, 60), session=AnonymousImageSession())
        self.check_cancel = check_cancel

    def inspect_local(self, path, index):
        try:
            info = image_metadata(path, self.check_cancel)
            self.log(f"  图{index}: {info['path']}；文件名={info['filename']} ({describe_image(info)})；上传 Content-Type={info['content_type']}", 'debug')
            detected = {'jpeg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp'}.get(info['format'])
            if detected and detected != info['content_type']:
                self.log(f'图{index}文件扩展名与实际格式不一致，上传 Content-Type={info["content_type"]}，实际为{detected}', 'warning')
            return info
        except Cancelled:
            raise
        except Exception as error:
            self.log(f'图{index}本地图片检查失败：{error}；未改变上传内容', 'warning')
            return None

    def verify_uploaded(self, original, url, index):
        try:
            self.check_cancel()
            with tempfile.TemporaryDirectory(prefix='storyboard-image-debug-') as directory:
                path = Path(directory) / 'downloaded-image'
                with self.request('GET', url, authenticated=False, check_status=False, stream=True,
                                  headers={'Accept': 'image/*', 'Accept-Encoding': 'identity'}) as response:
                    if not 200 <= response.status_code < 300:
                        raise ValueError(f'HTTP {response.status_code}')
                    count = 0
                    with path.open('wb') as stream:
                        for chunk in response.iter_content(64 * 1024):
                            self.check_cancel()
                            count += len(chunk)
                            if count > self.MAX_BYTES:
                                raise ValueError('下载图片超过32MB诊断上限')
                            stream.write(chunk)
                    remote = image_metadata(path, self.check_cancel)
                    remote['content_type'] = response.headers.get('Content-Type', '')
                self.log(f'  图{index}上传后检查: {url} ({describe_image(remote)})；响应 Content-Type={remote["content_type"]}', 'debug')
                if original is None:
                    return remote
                if (original['width'], original['height']) != (remote['width'], remote['height']):
                    self.log(f'图{index}上传后尺寸变化：{original["width"]}x{original["height"]} → {remote["width"]}x{remote["height"]}，可能被缩放/压缩', 'warning')
                if original['format'] != remote['format']:
                    self.log(f'图{index}上传后格式变化：{original["format"]} → {remote["format"]}，发生转码', 'warning')
                if original['sha256'] == remote['sha256']:
                    self.log(f'  图{index}原图/上传后 SHA256一致：{original["sha256"]}；字节完全相同，未压缩或转码', 'debug')
                else:
                    self.log(f'图{index}上传后字节变化：{original["size_bytes"]} → {remote["size_bytes"]}字节；可能重新编码/压缩或元数据变化，不能仅凭体积判定画质下降', 'warning')
                    self.log(f'  图{index} SHA256 原图={original["sha256"]}；上传后={remote["sha256"]}', 'debug')
                return remote
        except Cancelled:
            raise
        except Exception as error:
            self.log(f'图{index}上传后检查失败：{error}；仅诊断失败，继续原任务', 'warning')
            return None
