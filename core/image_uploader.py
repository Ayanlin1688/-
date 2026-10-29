"""Sequential multipart image uploads, preserving reference positions."""
import mimetypes
import struct
import zlib
import tempfile
import time
from urllib.parse import urlsplit
from pathlib import Path
from .api_errors import classify_connection_error, classify_http_error
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
                    return self._upload_once(path, retry_direct=False, retry_timeouts=True)
                except Cancelled:
                    raise
                except Exception as error:
                    self.log(f'图片上传失败（{attempt+1}/2）：{Path(file_path).name}：{self.redact(error)}', 'error')
                    if attempt or not self._should_retry_upload(error):
                        error = self._classify_upload_error(error, 'images')
                        if 'upload token required' in str(error).lower():
                            self.log('服务端要求独立素材上传 Token；请在设置中的“图床 API Key”填写服务商提供的上传凭据', 'error')
                        raise error from None
                    self.log('图片上传将在 2 秒后重试一次', 'warning')
                    for _ in range(20):
                        self.check_cancel()
                        time.sleep(0.1)
            finally:
                if cleanup:
                    Path(cleanup).unlink(missing_ok=True)

    def upload_file(self, file_path, kind=None):
        """Upload arbitrary reference media without image decoding or conversion."""
        if kind is not None:
            from .video_capabilities import validate_media_file
            validate_media_file(file_path, kind)
        for attempt in range(2):
            self.check_cancel()
            try:
                # Large reference videos are allowed up to 200 MB by the
                # relay and need a longer read timeout than image uploads.
                timeout = (15, 300) if kind == 'videos' else (15, 120) if kind == 'audios' else None
                return self._upload_once(file_path, timeout=timeout,
                                         retry_direct=False, retry_timeouts=True)
            except Cancelled:
                raise
            except Exception as error:
                if attempt or not self._should_retry_upload(error):
                    raise self._classify_upload_error(error, kind) from None
                self.log('素材上传失败，将重试一次', 'warning')
                for _ in range(20):
                    self.check_cancel()
                    time.sleep(0.1)

    @staticmethod
    def _should_retry_upload(error):
        status = getattr(error, 'status_code', None)
        if status is not None:
            try:
                return 500 <= int(status) <= 599
            except (TypeError, ValueError):
                return False
        return bool(getattr(error, 'retry_safe', False))

    @staticmethod
    def _classify_upload_error(error, kind=None):
        status = getattr(error, 'status_code', None)
        body = str(getattr(error, 'body', '') or error).casefold()
        label = '视频' if kind == 'videos' else '音频' if kind == 'audios' else '素材'
        limit = '200MB' if kind == 'videos' else '50MB' if kind == 'audios' else '服务商限制'
        if status == 413 or 'too large' in body or 'payload too large' in body:
            return RequestError(f'参考{label}超过服务商上传上限（{limit}），请压缩后重试', status, str(getattr(error, 'body', '') or ''))
        if status in (401, 403):
            return RequestError(f'参考{label}上传鉴权失败，请检查素材上传凭据', status, str(getattr(error, 'body', '') or ''))
        if status in (400, 415):
            return RequestError(f'参考{label}上传参数或格式不被服务商接受，请检查文件后重试', status, str(getattr(error, 'body', '') or ''))
        if status is not None:
            classification = classify_http_error(status, str(getattr(error, 'body', '') or error), kind='video')
            if status == 429:
                message = f'参考{label}上传被限流，请稍后重试'
            elif status >= 500:
                message = f'参考{label}上传服务暂时不可用，请稍后重试'
            else:
                message = f'参考{label}上传失败，请检查线路与素材后重试'
            return RequestError(message, status, str(getattr(error, 'body', '') or ''),
                                retry_safe=bool(classification.get('retryable')))
        classification = classify_connection_error(error)
        return RequestError(f'参考{label}上传失败：{classification["message"]}', None,
                            str(getattr(error, 'body', '') or ''), retry_safe=True)

    def _upload_once(self, file_path, timeout=None, *, retry_direct=False,
                     retry_timeouts=False):
        try:
            path = Path(file_path)
            headers = {}
            # The live provider declares X-Upload-Token in OPTIONS Allow-Headers.
            # Keep the requested Bearer header too. A video API key is not a
            # substitute for a valid separately issued upload token.
            if self.upload_url.rstrip('/') == DEFAULT_UPLOAD_URL and self.api_key:
                headers['X-Upload-Token'] = self.api_key
            with path.open('rb') as stream:
                mime = {'.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime',
                        '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.m4a': 'audio/mp4',
                        '.aac': 'audio/aac', '.ogg': 'audio/ogg'}.get(path.suffix.lower())
                mime = mime or mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
                request_kwargs = dict(headers=headers, authenticated=bool(self.api_key),
                                      files={'files': (path.name, stream, mime)},
                                      retry_direct=retry_direct,
                                      retry_timeouts=retry_timeouts)
                if timeout is not None:
                    request_kwargs['timeout'] = timeout
                with self.request('POST', self.upload_url, **request_kwargs) as response:
                    self.last_status_code = response.status_code
                    data = self.json(response)
                    url = None
                    for obj in objects(data):
                        files = obj.get('files')
                        if isinstance(files, list) and files and isinstance(files[0], dict) and files[0].get('url'):
                            url = files[0]['url']
                            break
                    url = url or extract(data, ('url',))
                    if isinstance(url, str) and urlsplit(url.strip()).scheme in ('http', 'https') and urlsplit(url.strip()).hostname:
                        url = url.strip()
                        self.log('素材上传完成：' + path.name, 'success')
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
