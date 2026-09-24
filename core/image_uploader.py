"""Sequential multipart image uploads, preserving reference positions."""
import mimetypes
import struct
import zlib
import tempfile
import time
from pathlib import Path
from .http_client import HttpClient, RequestError, Cancelled, objects, extract

DEFAULT_UPLOAD_URL = 'https://video.kkone.vip/api/uploads'


def connection_probe_png():
    """Build a small valid 64x64 RGB PNG, including each chunk's CRC."""
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)
    header = struct.pack('!2I5B', 64, 64, 8, 2, 0, 0, 0)
    pixels = (b'\x00' + bytes((94, 106, 210)) * 64) * 64
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b'')


def upload_credentials(api_config):
    url = (api_config.get('upload_url') or '').strip() or DEFAULT_UPLOAD_URL
    key = api_config.get('upload_api_key', '').strip()
    # The generation key belongs only to the built-in provider. A separate image
    # host receives only its explicitly configured upload key (or anonymous upload).
    if not key and url.rstrip('/') == DEFAULT_UPLOAD_URL:
        key = api_config.get('api_key', '').strip()
    return url, key



MAX_REFERENCE_SIDE = 5760
MIN_REFERENCE_RATIO = 0.4
MAX_REFERENCE_RATIO = 2.5


def fit_reference_image(image):
    """Scale or pad a reference so the provider accepts its pixel size.

    The live provider rejects width or height above 5760 and aspect ratios
    outside 0.4-2.5 only after a paid task has been created. Smaller files
    are uploaded unchanged.
    """
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QImage, QPainter
    if image is None or image.isNull() or image.width() <= 0 or image.height() <= 0:
        return image, False
    changed = False
    if max(image.width(), image.height()) > MAX_REFERENCE_SIDE:
        image = image.scaled(MAX_REFERENCE_SIDE, MAX_REFERENCE_SIDE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        changed = True
    width, height = image.width(), image.height()
    ratio = width / height
    if MIN_REFERENCE_RATIO <= ratio <= MAX_REFERENCE_RATIO and max(width, height) <= MAX_REFERENCE_SIDE:
        return image, changed
    if ratio > MAX_REFERENCE_RATIO:
        target_width, target_height = width, int((width / MAX_REFERENCE_RATIO) + 0.999)
    else:
        target_width, target_height = int((height * MIN_REFERENCE_RATIO) + 0.999), height
    if max(target_width, target_height) > MAX_REFERENCE_SIDE:
        image = image.scaled(MAX_REFERENCE_SIDE, MAX_REFERENCE_SIDE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        changed = True
        width, height = image.width(), image.height()
        ratio = width / height
        if ratio > MAX_REFERENCE_RATIO:
            target_width, target_height = width, int((width / MAX_REFERENCE_RATIO) + 0.999)
        elif ratio < MIN_REFERENCE_RATIO:
            target_width, target_height = int((height * MIN_REFERENCE_RATIO) + 0.999), height
        else:
            return image, True
    canvas = QImage(target_width, target_height, QImage.Format_RGB32)
    canvas.fill(0xFFF7F4EF)
    painter = QPainter(canvas)
    painter.drawImage((target_width - width) // 2, (target_height - height) // 2, image)
    painter.end()
    return canvas, True


def normalized_reference_path(file_path):
    """Return the upload path and a temporary path to delete, if one was made."""
    from PyQt5.QtGui import QImage
    source = Path(file_path)
    image = QImage(str(source))
    if image.isNull():
        return str(source), None
    fitted, changed = fit_reference_image(image)
    if not changed:
        return str(source), None
    handle = tempfile.NamedTemporaryFile(prefix="storyboard-reference-", suffix=".jpg", delete=False)
    handle.close()
    if not fitted.save(handle.name, "JPEG", 92):
        Path(handle.name).unlink(missing_ok=True)
        raise ValueError("参考图超出服务商尺寸限制，且无法自动缩小：" + source.name)
    return handle.name, handle.name

class ImageUploader(HttpClient):
    def __init__(self, upload_url=DEFAULT_UPLOAD_URL, api_key='', check_cancel=None, **kwargs):
        super().__init__(api_key, timeout=(15, 60), **kwargs)
        self.upload_url = (upload_url or '').strip() or DEFAULT_UPLOAD_URL
        self.check_cancel = check_cancel or (lambda: None)
        self.last_status_code = None

    @classmethod
    def from_config(cls, config_manager, **kwargs):
        config = getattr(config_manager, 'config', config_manager)
        return cls(*upload_credentials(config['api']), **kwargs)

    def upload_image(self, file_path):
        for attempt in range(2):
            self.check_cancel()
            cleanup = None
            try:
                try:
                    path, cleanup = normalized_reference_path(file_path)
                    if cleanup:
                        self.log('参考图超出服务商尺寸限制，已自动缩小后上传：' + Path(file_path).name, 'warning')
                    return self._upload_once(path)
                except Cancelled:
                    raise
                except Exception as error:
                    self.log(f'图片上传失败（{attempt+1}/2）：{Path(file_path).name}：{self.redact(error)}', 'error')
                    if attempt == 1:
                        if 'upload token required' in str(error).lower():
                            self.log('服务端要求独立素材上传 Token；请在设置中的“图床 API Key”填写服务商提供的上传凭据', 'error')
                        raise
                    self.log('图片上传将在 2 秒后重试一次', 'warning')
                    for _ in range(20):
                        self.check_cancel()
                        time.sleep(0.1)
            finally:
                if cleanup:
                    Path(cleanup).unlink(missing_ok=True)

    def _upload_once(self, file_path):
        try:
            path = Path(file_path)
            headers = {}
            # The live provider declares X-Upload-Token in OPTIONS Allow-Headers.
            # Keep the requested Bearer header too. A video API key is not a
            # substitute for a valid separately issued upload token.
            if self.upload_url.rstrip('/') == DEFAULT_UPLOAD_URL and self.api_key:
                headers['X-Upload-Token'] = self.api_key
            with path.open('rb') as stream:
                with self.request('POST', self.upload_url, headers=headers, authenticated=bool(self.api_key), files={'files': (path.name, stream, mimetypes.guess_type(path.name)[0] or 'application/octet-stream')}) as response:
                    self.last_status_code = response.status_code
                    data = self.json(response)
                    url = None
                    for obj in objects(data):
                        files = obj.get('files')
                        if isinstance(files, list) and files and isinstance(files[0], dict) and files[0].get('url'):
                            url = files[0]['url']
                            break
                    url = url or extract(data, ('url',))
                    if isinstance(url, str) and url.strip():
                        url = url.strip()
                        self.log(f'图片上传完成：{self.redact(url)[:240]}', 'success')
                        return url
                    raise RequestError(f'HTTP {response.status_code}: 上传响应缺少图片 URL：{self.redact(response.text)}', response.status_code, self.redact(response.text))
        except Exception as error:
            self.log(f'图片上传失败：{Path(file_path).name}：{self.redact(error)}', 'error')
            raise

    def test_connection(self):
        """Upload a tiny PNG to verify multipart permission, without generating video."""
        try:
            with tempfile.TemporaryDirectory(prefix='storyboard-upload-check-') as directory:
                path = Path(directory) / 'connection-check.png'
                path.write_bytes(connection_probe_png())
                url = self._upload_once(path)
            return dict(ok=True, status_code=self.last_status_code, message='微型测试图片上传成功', url=url)
        except Exception as error:
            return dict(ok=False, status_code=getattr(error, 'status_code', None), message=self.redact(error))

    def upload_images(self, file_paths, check_cancel=None):
        urls = []
        for path in file_paths:
            if check_cancel:
                check_cancel()
            try:
                urls.append(self.upload_image(path))
            except Cancelled:
                raise
            except Exception as error:
                self.log(f'图片上传失败，保留引用位置：{self.redact(error)}', 'warning')
                urls.append(None)
        return urls
