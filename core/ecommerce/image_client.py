"""Synchronous ecommerce images using the verified gpt-image contract."""
from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import requests

from core.ecommerce.platforms import RATIOS
from core.http_client import HttpClient, RequestError


DEFAULT_IMAGE_MODEL = 'gpt-image-2.5-sunburst'
TIER_SHORT_EDGE = {'1K': 1024, '2K': 2048, '4K': 4096}


class EcommerceImageError(Exception):
    def __init__(self, message, status_code=None, body_excerpt='',
                 failed_before_response=False, *, redact=None):
        clean = redact or str
        self.message = clean(str(message))
        self.status_code = status_code
        # Redact before truncation, including credentials crossing the boundary.
        self.body_excerpt = clean(str(body_excerpt))[:500]
        self.failed_before_response = failed_before_response
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


def _reference_files(paths, redact):
    """Read every reference before sending a potentially billable request."""
    mime_types = {'.png': 'image/png', '.jpg': 'image/jpeg',
                  '.jpeg': 'image/jpeg', '.webp': 'image/webp'}
    files = []
    for value in paths:
        try:
            path = Path(value)
            content = path.read_bytes()
        except (OSError, TypeError, ValueError) as error:
            raise EcommerceImageError(
                f'读取参考图失败：{error}', redact=redact) from None
        files.append(('image', (path.name, content,
                                mime_types.get(path.suffix.lower(), 'image/png'))))
    return files


def _decode_image(response, redact):
    """A successful response must contain inline PNG bytes, never a task ID."""
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
    encoded = data[0].get('b64_json')
    if not isinstance(encoded, str) or not encoded.strip():
        raise invalid('图片生成响应缺少 b64_json')
    try:
        png_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise invalid('图片生成响应的 b64_json 不是合法 Base64') from None
    if not png_bytes.startswith(b'\x89PNG'):
        raise invalid('图片生成响应不是 PNG 图片（魔数错误）')

    usage = payload.get('usage')
    details = usage.get('input_tokens_details') if isinstance(usage, dict) else None
    tokens = details.get('image_tokens') if isinstance(details, dict) else None
    image_tokens = tokens if type(tokens) is int and tokens >= 0 else None
    revised_prompt = data[0].get('revised_prompt', payload.get('revised_prompt'))
    if not isinstance(revised_prompt, str):
        revised_prompt = None
    return png_bytes, revised_prompt, image_tokens


def generate_image(base_url, api_key, model, prompt, ratio, tier, *,
                   reference_paths=None, timeout=(15, 180)):
    """Generate once per size; only explicit size rejections permit fallback.

    Headers are obtained before reading the whole body, so the caller can tell
    a connection failure from a failed read after a potentially billed response.
    Neither case is retried here.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('图片提示词必须为非空字符串')
    if reference_paths is None:
        reference_paths = []
    if (not isinstance(reference_paths, Sequence)
            or isinstance(reference_paths, (str, bytes, bytearray))):
        raise ValueError('参考图必须以有序路径列表提供')
    if len(reference_paths) > 7:
        raise ValueError('参考图最多 7 张（产品图最多 6 张，风格图 1 张）')
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
                        size=candidate['size'], quality=candidate['quality'])
            kwargs = dict(data=body, files=files) if files else dict(json=body)
            with client.request('POST', url, check_status=False, stream=True,
                                allow_redirects=False, **kwargs) as response:
                status_code = response.status_code
                # Read completely once. Any read failure after headers is final.
                response.content
                if not 200 <= status_code < 300:
                    excerpt = client.redact(response.text)[:500]
                    if (_is_size_rejection(status_code, response.text)
                            and position + 1 < len(plan)):
                        warnings.append(
                            f"{candidate['tier']} 尺寸 {candidate['size']} 被拒绝："
                            f'HTTP {status_code}: {excerpt}；尝试下一候选尺寸')
                        continue
                    raise EcommerceImageError(
                        f'HTTP {status_code}: {excerpt}', status_code,
                        response.text, redact=client.redact)
                png_bytes, revised_prompt, image_tokens = _decode_image(
                    response, client.redact)
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
                    image_tokens=image_tokens, warnings=warnings)
    except (RequestError, requests.RequestException) as error:
        raise EcommerceImageError(
            f'图片生成连接或读取失败：{error}', status_code,
            getattr(error, 'body', ''),
            failed_before_response=status_code is None,
            redact=client.redact) from None
    finally:
        client.close()


def generate_text_to_image(base_url, api_key, model, prompt, ratio, tier, *,
                           timeout=(15, 180)):
    return generate_image(base_url, api_key, model, prompt, ratio, tier,
                          reference_paths=None, timeout=timeout)


def generate_image_edit(base_url, api_key, model, prompt, ratio, tier,
                        reference_paths, *, timeout=(15, 180)):
    return generate_image(base_url, api_key, model, prompt, ratio, tier,
                          reference_paths=reference_paths, timeout=timeout)
