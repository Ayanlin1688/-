"""Synchronous ecommerce images using the verified gpt-image contract."""
from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import requests
from PyQt5.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PyQt5.QtGui import QImage, QImageWriter

from core.ecommerce.platforms import RATIOS
from core.http_client import HttpClient, RequestError
from core.api_errors import classify_http_error, classify_connection_error
from core.model_catalog import normalize_model_id


DEFAULT_IMAGE_MODEL = 'gpt-image-2.5-sunburst'
TIER_SHORT_EDGE = {'1K': 1024, '2K': 2048, '4K': 4096}
REFERENCE_MAX_BYTES = int(1.5 * 1024 * 1024)
REFERENCE_MAX_EDGE = 2048


class EcommerceImageError(Exception):
    def __init__(self, message, status_code=None, body_excerpt='',
                 failed_before_response=False, *, redact=None, retry_safe=None):
        clean = redact or str
        self.message = clean(str(message))
        self.status_code = status_code
        # Redact before truncation, including credentials crossing the boundary.
        self.body_excerpt = clean(str(body_excerpt))[:500]
        self.failed_before_response = failed_before_response
        classification = classify_http_error(status_code, clean(str(body_excerpt)) or self.message, kind='image')
        self.category = classification['category']
        self.switchable = classification['switchable'] if status_code is not None else retry_safe is not False
        self.retry_safe = (classification['retryable']
                           if retry_safe is None and status_code is not None else retry_safe)
        if status_code is not None and 200 <= status_code < 300:
            self.retry_safe = False
            self.switchable = False
        super().__init__(self.message)


@dataclass
class ImageResult:
    png_bytes: bytes
    model: str
    size: str
    tier_used: str
    quality: str
    endpoint: str
    revised_prompt: str | None = None
    image_tokens: int | None = None
    warnings: list[str] = field(default_factory=list)
    delivery: str = 'b64'


def _even(n):
    return int(round(n)) // 2 * 2


def _ratio_numbers(ratio):
    if not isinstance(ratio, str) or ratio not in RATIOS:
        raise ValueError('不支持的图片比例')
    return tuple(int(part) for part in ratio.split(':'))


def _quality(tier):
    if not isinstance(tier, str) or tier not in TIER_SHORT_EDGE:
        raise ValueError('不支持的图片分辨率档位')
    return 'auto' if tier == '1K' else 'high'


def image_dimensions(ratio, tier):
    """Calculate each tier from its short edge, rounding the long edge to even."""
    w, h = _ratio_numbers(ratio)
    _quality(tier)
    short_edge = TIER_SHORT_EDGE[tier]
    if w == h:
        return short_edge, short_edge
    if w < h:
        return short_edge, _even(short_edge * h / w)
    return _even(short_edge * w / h), short_edge


def _standard_tile(ratio, S):
    w, h = _ratio_numbers(ratio)
    if w == h:
        return S, S
    if w < h:
        return S, _even(S * 1.5)
    return _even(S * 1.5), S


def build_size_plan(ratio, tier):
    """Try exact then standard dimensions at each tier, without duplicates."""
    image_dimensions(ratio, tier)
    tiers = ['4K', '2K', '1K']
    plan, seen = [], set()
    for candidate_tier in tiers[tiers.index(tier):]:
        dimensions = (
            image_dimensions(ratio, candidate_tier),
            _standard_tile(ratio, TIER_SHORT_EDGE[candidate_tier]),
        )
        for w, h in dimensions:
            size = f'{w}x{h}'
            if size not in seen:
                plan.append(dict(size=size, tier=candidate_tier,
                                 quality=_quality(candidate_tier)))
                seen.add(size)
    return plan


def _is_size_rejection(status_code, body):
    return (status_code is not None and 400 <= status_code < 500
            and any(word in str(body).lower() for word in (
                'size', 'dimension', 'resolution', '分辨率', '尺寸', 'too large')))


def _is_response_format_rejection(status_code, body):
    text = str(body).lower()
    return (status_code == 400
            and 'response_format' in text
            and any(word in text for word in (
                'unsupported', 'unknown parameter', 'not supported',
                '不支持', '未知参数', '不支持的参数')))


def _has_transparency(image):
    """Return whether any pixel has an alpha value below fully opaque."""
    if not image.hasAlphaChannel():
        return False
    rgba = image.convertToFormat(QImage.Format_RGBA8888)
    bits = rgba.constBits()
    bits.setsize(rgba.byteCount())
    raw = bytes(bits)
    opaque = b'\xff' * rgba.width()
    stride = rgba.bytesPerLine()
    for row_index in range(rgba.height()):
        row = raw[row_index * stride:row_index * stride + rgba.width() * 4]
        if row[3::4] != opaque:
            return True
    return False


def _compress_reference(path, content, redact):
    """Return a bounded reference payload, or the original on conversion errors."""
    logger = logging.getLogger('storyboard')
    original_size = len(content)
    suffix = path.suffix.lower()
    mime_types = {'.png': 'image/png', '.jpg': 'image/jpeg',
                  '.jpeg': 'image/jpeg', '.webp': 'image/webp'}
    original = (path.name, content, mime_types.get(suffix, 'image/png'))
    if original_size <= REFERENCE_MAX_BYTES:
        logger.debug('参考图 %s：%d→%d 字节，格式 %s',
                     redact(path.name), original_size, original_size,
                     suffix.lstrip('.').upper() or 'PNG')
        return original

    try:
        image = QImage.fromData(content)
        if image.isNull():
            raise ValueError('无法解码')
        width, height = image.width(), image.height()
        longest = max(width, height)
        if longest > REFERENCE_MAX_EDGE:
            scale = REFERENCE_MAX_EDGE / longest
            scaled = image.scaled(
                max(1, int(round(width * scale))),
                max(1, int(round(height * scale))),
                Qt.KeepAspectRatio, Qt.SmoothTransformation)
        else:
            scaled = image
        if scaled.isNull():
            raise ValueError('无法缩放')

        transparent = _has_transparency(scaled)
        output = QByteArray()
        buffer = QBuffer(output)
        if not buffer.open(QIODevice.WriteOnly):
            raise ValueError('无法打开编码缓冲区')
        try:
            image_format = 'PNG' if transparent else 'JPEG'
            writer = QImageWriter(buffer, image_format.encode('ascii'))
            if not transparent:
                writer.setQuality(92)
            if not writer.write(scaled):
                raise ValueError('无法编码')
        finally:
            buffer.close()
        encoded = bytes(output)
        if not encoded:
            raise ValueError('编码结果为空')
        if transparent:
            filename, mime = path.stem + '.png', 'image/png'
        else:
            filename, mime = path.stem + '.jpg', 'image/jpeg'
        logger.debug('参考图 %s：%d→%d 字节，格式 %s',
                     redact(path.name), original_size, len(encoded), image_format)
        return filename, encoded, mime
    except Exception:
        # Reference compression is an optimization. Preserve the original if
        # Qt cannot decode, scale, or encode the source bytes.
        logger.warning('参考图压缩失败，按原图发送：%s', redact(path.name))
        logger.debug('参考图 %s：%d→%d 字节，格式 %s',
                     redact(path.name), original_size, original_size,
                     suffix.lstrip('.').upper() or 'PNG')
        return original


def _reference_files(paths, redact):
    """Read every reference before sending a potentially billable request."""
    files = []
    for value in paths:
        try:
            path = Path(value)
            content = path.read_bytes()
        except (OSError, TypeError, ValueError) as error:
            raise EcommerceImageError(
                f'读取参考图失败：{error}', redact=redact) from None
        files.append(('image', _compress_reference(path, content, redact)))
    return files


def _image_format(content):
    if content.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'PNG'
    if content.startswith(b'\xff\xd8\xff'):
        return 'JPEG'
    if content.startswith(b'RIFF') and content[8:12] == b'WEBP':
        return 'WEBP'
    if content.startswith((b'GIF87a', b'GIF89a')):
        return 'GIF'
    if content.startswith(b'BM'):
        return 'BMP'
    return None


def _to_png(content, image_format, redact):
    image = QImage.fromData(content)
    if image.isNull():
        prefix = content[:8].hex(' ').upper()
        raise EcommerceImageError(
            f'图片生成响应不是受支持的图片格式（前 8 字节：{prefix}）', 200, redact=redact)
    output = QByteArray()
    buffer = QBuffer(output)
    if not buffer.open(QIODevice.WriteOnly):
        raise EcommerceImageError('图片生成响应无法转换为 PNG', 200, redact=redact)
    try:
        if not image.save(buffer, 'PNG'):
            raise EcommerceImageError('图片生成响应无法转换为 PNG', 200, redact=redact)
    finally:
        buffer.close()
    return bytes(output), [f'源图为 {image_format}，已转为 PNG']


def _download_image(url, redact):
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
    except ValueError:
        parsed = None
    if (parsed is None or parsed.scheme.lower() not in ('http', 'https')
            or not parsed.netloc or parsed.username is not None or parsed.password is not None):
        raise EcommerceImageError('图片生成响应的 url 必须是 http/https 地址', 200, redact=redact)
    response = None
    try:
        # This independent request deliberately carries no API or upload credentials.
        response = requests.get(url, stream=True, allow_redirects=True, timeout=(15, 300))
    except requests.RequestException as error:
        raise EcommerceImageError(
            f'图片下载连接失败：{type(error).__name__}', 200, redact=redact) from None
    try:
        status = response.status_code
        if not 200 <= status < 300:
            excerpt = redact(response.text)[:500]
            raise EcommerceImageError(
                f'图片下载失败：HTTP {status}: {excerpt}', 200, excerpt, redact=redact)
        try:
            return response.content
        except requests.RequestException as error:
            raise EcommerceImageError(
                f'图片下载响应读取失败：{type(error).__name__}', 200, redact=redact) from None
    except EcommerceImageError:
        raise
    finally:
        response.close()


def _decode_image(response, redact):
    """Decode inline base64 or anonymously download a returned image URL."""
    def invalid(message):
        return EcommerceImageError(
            message, response.status_code, response.text, redact=redact)

    try:
        payload = response.json()
    except ValueError:
        raise invalid('图片生成响应不是合法 JSON') from None
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise invalid('图片生成响应缺少有效的 data 数组')
    encoded = data[0].get('b64_json') if 'b64_json' in data[0] else data[0].get('b64')
    warnings = []
    delivery = 'b64'
    if isinstance(encoded, str) and encoded.strip():
        encoded = encoded.strip()
        if encoded.startswith('data:') and ',' in encoded:
            encoded = encoded.split(',', 1)[1]
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise invalid('图片生成响应的 b64_json 不是合法 Base64') from None
        image_format = _image_format(content)
        if image_format == 'PNG':
            png_bytes = content
        else:
            png_bytes, warnings = _to_png(content, image_format or '未知格式', redact)
    else:
        image_url = data[0].get('url')
        if not isinstance(image_url, str) or not image_url.strip():
            raise invalid('图片生成响应缺少 b64_json 或可下载的 url')
        delivery = 'url'
        content = _download_image(image_url.strip(), redact)
        image_format = _image_format(content)
        if image_format is None:
            raise EcommerceImageError(
                '图片下载响应无法识别图片格式', 200, response.text, redact=redact)
        if image_format == 'PNG':
            png_bytes = content
        else:
            png_bytes, warnings = _to_png(content, image_format, redact)

    usage = payload.get('usage')
    details = usage.get('input_tokens_details') if isinstance(usage, dict) else None
    tokens = details.get('image_tokens') if isinstance(details, dict) else None
    image_tokens = tokens if type(tokens) is int and tokens >= 0 else None
    revised_prompt = data[0].get('revised_prompt', payload.get('revised_prompt'))
    if not isinstance(revised_prompt, str):
        revised_prompt = None
    return png_bytes, revised_prompt, image_tokens, delivery, warnings


def generate_image(base_url, api_key, model, prompt, ratio, tier, *,
                   reference_paths=None, timeout=(15, 180)):
    """Generate once per size; only explicit size rejections permit fallback.

    Headers are obtained before reading the whole body, so the caller can tell
    a connection failure from a failed read after a potentially billed response.
    Neither case is retried here.
    """
    model = normalize_model_id(model)
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('图片提示词必须为非空字符串')
    if reference_paths is None:
        reference_paths = []
    if (not isinstance(reference_paths, Sequence)
            or isinstance(reference_paths, (str, bytes, bytearray))):
        raise ValueError('参考图必须以有序路径列表提供')
    if len(reference_paths) > 9:
        raise ValueError('参考图最多9张（产品图最多6张，风格图最多3张）')
    plan = build_size_plan(ratio, tier)
    client = HttpClient(api_key=api_key, timeout=timeout)
    status_code = None
    try:
        files = _reference_files(reference_paths, client.redact)
        endpoint = '/images/edits' if files else '/images/generations'
        url = base_url.strip().rstrip('/') + endpoint
        warnings = []
        for position, candidate in enumerate(plan):
            status_code = None
            body = dict(model=model, prompt=prompt, n=1,
                        size=candidate['size'], quality=candidate['quality'],
                        response_format='b64_json')
            without_format = False
            while True:
                request_body = dict(body)
                if without_format:
                    request_body.pop('response_format', None)
                kwargs = dict(data=request_body, files=files) if files else dict(json=request_body)
                with client.request('POST', url, check_status=False, stream=True,
                                    allow_redirects=False, **kwargs) as response:
                    status_code = response.status_code
                    # Read completely once. Any read failure after headers is final.
                    response.content
                    if not 200 <= status_code < 300:
                        excerpt = client.redact(response.text)[:500]
                        classification = classify_http_error(status_code, client.redact(response.text), kind='image')
                        if classification['category'] in ('content_policy', 'authentication'):
                            raise EcommerceImageError(classification['message'], status_code,
                                                      response.text, redact=client.redact, retry_safe=False)
                        if (not without_format and _is_response_format_rejection(
                                status_code, response.text)):
                            warnings.append('服务不支持 response_format，已移除参数重试一次')
                            without_format = True
                            continue
                        if (_is_size_rejection(status_code, response.text)
                                and position + 1 < len(plan)):
                            warnings.append(
                                f"{candidate['tier']} 尺寸 {candidate['size']} 被拒绝："
                                f'HTTP {status_code}: {excerpt}；尝试下一候选尺寸')
                            break
                        raise EcommerceImageError(
                            classification['message'], status_code,
                            response.text, redact=client.redact, retry_safe=classification['retryable'])
                    png_bytes, revised_prompt, image_tokens, delivery, decode_warnings = _decode_image(
                        response, client.redact)
                warnings.extend(decode_warnings)
                if files and image_tokens == 0:
                    warnings.append('参考图可能未被模型采用（image_tokens=0）')
                if position:
                    warnings.append(
                        f"尺寸从 {tier} {plan[0]['size']} 回退至 "
                        f"{candidate['tier']} {candidate['size']}（实际使用尺寸）")
                return ImageResult(
                    png_bytes=png_bytes, model=model, size=candidate['size'],
                    tier_used=candidate['tier'], quality=candidate['quality'],
                    endpoint=endpoint, revised_prompt=revised_prompt,
                    image_tokens=image_tokens, warnings=warnings, delivery=delivery)
    except (RequestError, requests.RequestException) as error:
        raise EcommerceImageError(
            classify_connection_error(error)['message'], status_code,
            getattr(error, 'body', ''),
            failed_before_response=status_code is None,
            redact=client.redact, retry_safe=getattr(error, 'retry_safe', False)) from None
    finally:
        client.close()


def generate_text_to_image(base_url, api_key, model, prompt, ratio, tier, *,
                           timeout=(15, 180)):
    return generate_image(base_url, api_key, model, prompt, ratio, tier,
                          reference_paths=None, timeout=timeout)


def generate_image_edit(base_url, api_key, model, prompt, ratio, tier,
                        reference_paths, *, timeout=(15, 180)):
    model = normalize_model_id(model)
    return generate_image(base_url, api_key, model, prompt, ratio, tier,
                          reference_paths=reference_paths, timeout=timeout)
