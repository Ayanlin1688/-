"""Streaming ecommerce analysis using the verified chat-completions contract."""
from __future__ import annotations

import json
import base64
import mimetypes
from pathlib import Path
import re
from urllib.parse import urlsplit

import requests

from core.http_client import HttpClient, RequestError
from core.api_errors import classify_http_error, classify_connection_error
from core.thinking_params import (
    REASONING_KEYS, apply_thinking_to_payload, request_model as resolve_request_model,
)


DEFAULT_ANALYSIS_MODEL = 'gemini-3.8-flash'
ANALYSIS_TIMEOUT_SECONDS = 180
DEFAULT_ANALYSIS_TIMEOUT = (15, ANALYSIS_TIMEOUT_SECONDS)


class EcommerceAnalysisError(Exception):
    def __init__(self, message, status_code=None, body_excerpt='', *, category=None,
                 retryable=None, switchable=None):
        self.message = str(message)
        self.status_code = status_code
        self.body_excerpt = str(body_excerpt)[:500]
        accepted = status_code is not None and 200 <= status_code < 300
        if status_code is None or accepted:
            classification = dict(message=self.message, category='analysis_error',
                                  retryable=False, switchable=False)
        else:
            classification = classify_http_error(status_code, body_excerpt or self.message)
        self.category = category or classification['category']
        self.user_message = self.message if category is not None else classification['message']
        self.retryable = not accepted and (classification['retryable'] if retryable is None else retryable)
        self.switchable = not accepted and (classification['switchable'] if switchable is None else switchable)
        super().__init__(self.message)


def is_gemini_native(base_url):
    try:
        parts = urlsplit(str(base_url or '').strip())
        return (parts.hostname == 'generativelanguage.googleapis.com'
                or '/v1beta' in parts.path.lower())
    except ValueError:
        return False


def _read_sse(response, redact, on_content, on_reasoning, *, gemini=False):
    content_parts, reasoning_parts = [], []
    # SSE is UTF-8 even when the server omits charset in Content-Type.
    response.encoding = 'utf-8'
    for line in response.iter_lines(decode_unicode=True):
        line = line.strip()
        if not line.startswith('data:'):
            continue
        data = line[len('data:'):].strip()
        if not data:
            continue
        if data == '[DONE]':
            break
        try:
            frame = json.loads(data)
        except ValueError:
            raise EcommerceAnalysisError(
                'AI 流式响应帧不是合法 JSON', response.status_code,
                body_excerpt=redact(data)) from None
        if not isinstance(frame, dict):
            raise EcommerceAnalysisError('AI 流式响应帧格式错误：期望 JSON 对象', response.status_code)
        if frame.get('error'):
            error = frame['error']
            code = error.get('code') if isinstance(error, dict) else None
            if isinstance(code, str) and code.isdecimal():
                code = int(code)
            if type(code) is int and 400 <= code < 600:
                classification = classify_http_error(code, redact(data))
            else:
                classification = classify_http_error(400, redact(data))
                if classification['category'] != 'content_policy':
                    classification = classify_http_error(500, redact(data))
            raise EcommerceAnalysisError(
                classification['message'], response.status_code, redact(data),
                category=classification['category'])
        if gemini:
            candidates = frame.get('candidates') or []
            if not isinstance(candidates, list):
                raise EcommerceAnalysisError('AI 流式响应 candidates 格式错误：期望数组', response.status_code)
            candidate = candidates[0] if candidates else {}
            content = candidate.get('content', {}) if isinstance(candidate, dict) else {}
            parts = content.get('parts', []) if isinstance(content, dict) else []
            if not isinstance(parts, list):
                raise EcommerceAnalysisError('AI 流式响应 parts 格式错误：期望数组', response.status_code)
            for part in parts:
                value = part.get('text') if isinstance(part, dict) else None
                if value is None:
                    continue
                if not isinstance(value, str):
                    raise EcommerceAnalysisError('AI 流式响应 text 格式错误：期望字符串', response.status_code)
                content_parts.append(value)
                if on_content is not None:
                    on_content(value)
            continue
        choices = frame.get('choices')
        if not choices:
            # The final usage-only event has choices=[].
            continue
        if not isinstance(choices, list):
            raise EcommerceAnalysisError('AI 流式响应 choices 格式错误：期望数组', response.status_code)
        for choice in choices:
            delta = choice.get('delta') if isinstance(choice, dict) else None
            if not isinstance(delta, dict):
                raise EcommerceAnalysisError('AI 流式响应 delta 格式错误：期望对象', response.status_code)
            for field, parts, callback in (
                ('content', content_parts, on_content),
                *((key, reasoning_parts, on_reasoning) for key in REASONING_KEYS),
            ):
                value = delta.get(field)
                if value is None or value == '':
                    continue
                if not isinstance(value, str):
                    raise EcommerceAnalysisError(
                        f'AI 流式响应 {field} 格式错误：期望字符串', response.status_code)
                parts.append(value)
                if callback is not None:
                    callback(value)
    return ''.join(content_parts), ''.join(reasoning_parts)


def analyze_stream(base_url, api_key, model, system_prompt, user_prompt, *,
                   timeout=DEFAULT_ANALYSIS_TIMEOUT, on_content=None, on_reasoning=None, thinking=False,
                   reference_paths=None):
    """Return complete content and reasoning, notifying callbacks per delta.

    Reuse the shared requests session and redaction. POST is sent once; a failed
    stream is reported to the caller without replaying an accepted analysis.
    """
    client = HttpClient(api_key=api_key, timeout=timeout)
    status_code = None
    request_model = resolve_request_model(base_url, model, thinking)
    payload = {
        'model': request_model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        'stream': True,
    }
    if request_model != 'deepseek-reasoner':
        payload['temperature'] = 0.7
    gemini = is_gemini_native(base_url)
    url = base_url.strip().rstrip('/') + '/chat/completions'
    headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream'}
    if gemini:
        url = base_url.strip().rstrip('/') + f'/models/{request_model}:streamGenerateContent?alt=sse'
        headers['x-goog-api-key'] = api_key
        payload = {
            'systemInstruction': {'parts': [{'text': system_prompt}]},
            'contents': [{'role': 'user', 'parts': [{'text': user_prompt}]}],
            'generationConfig': {'temperature': 0.7, 'responseMimeType': 'application/json'},
        }
        payload = apply_thinking_to_payload(payload, 'gemini', request_model, thinking)
    else:
        # 矩阵参数按模型名映射；OpenAI 兼容线路上的 claude 不再盲发 thinking。
        payload = apply_thinking_to_payload(payload, 'openai', request_model, thinking)
    if reference_paths:
        images = []
        for path in reference_paths:
            file = Path(path)
            mime = mimetypes.guess_type(str(file))[0] or 'image/png'
            try:
                encoded = base64.b64encode(file.read_bytes()).decode('ascii')
            except OSError:
                client.close()
                raise EcommerceAnalysisError('参考图无法读取，请重新选择图片') from None
            images.append((mime, encoded))
        if gemini:
            payload['contents'][0]['parts'].extend(
                {'inlineData': {'mimeType': mime, 'data': data}} for mime, data in images)
        else:
            payload['messages'][1]['content'] = [
                {'type': 'text', 'text': user_prompt},
                *({'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{data}'}}
                  for mime, data in images)]
    try:
        with client.request(
            'POST', url,
            check_status=False, stream=True,
            headers=headers,
            json=payload,
        ) as response:
            status_code = response.status_code
            if not 200 <= status_code < 300:
                excerpt = client.redact(response.text)[:500]
                classification = classify_http_error(status_code, client.redact(response.text))
                raise EcommerceAnalysisError(
                    classification['message'], status_code, excerpt, **{
                        name: classification[name] for name in ('category', 'retryable', 'switchable')})
            return _read_sse(response, client.redact, on_content, on_reasoning, gemini=gemini)
    except EcommerceAnalysisError as error:
        message = error.message
        if client.redact(request_model) not in message:
            message = f'模型 {client.redact(request_model)}：{message}'
        response_status = error.status_code if error.status_code is not None else status_code
        raise EcommerceAnalysisError(message, response_status, client.redact(error.body_excerpt),
                                     category=error.category, retryable=error.retryable,
                                     switchable=error.switchable) from None
    except (RequestError, requests.RequestException) as error:
        response_status = status_code if status_code is not None else getattr(error, 'status_code', None)
        read_timeout = ('read timed out' in str(error).casefold()
                        or 'read timeout' in str(error).casefold())
        try:
            read_timeout_seconds = float(timeout[1])
        except (TypeError, ValueError, IndexError):
            read_timeout_seconds = 0
        if read_timeout and read_timeout_seconds >= 30:
            classification = dict(
                message=f'分析超时（{int(read_timeout_seconds)} 秒），请切换模型或线路后重试',
                category='analysis_timeout', retryable=False, switchable=False)
        else:
            classification = (classify_connection_error(error) if response_status is None or 200 <= response_status < 300
                          else classify_http_error(response_status, client.redact(getattr(error, 'body', ''))))
        raise EcommerceAnalysisError(
            f'模型 {client.redact(request_model)}：{classification["message"]}',
            response_status,
            client.redact(getattr(error, 'body', '')),
            **{name: classification[name] for name in ('category', 'retryable', 'switchable')},
        ) from None
    finally:
        client.close()


def parse_prompt_items(text, carousel_count, detail_count):
    """Extract and validate one array, with separate numbering for each type."""
    counts = {'carousel': carousel_count, 'detail': detail_count}
    for kind, count in counts.items():
        if type(count) is not int or count < 0:
            raise EcommerceAnalysisError(f'{kind} 数量配置错误：期望非负整数、实际 {count!r}')
    if not isinstance(text, str):
        raise EcommerceAnalysisError('AI 返回不是合法 JSON，期望文本、实际为非字符串')
    candidate = text.strip()
    start, end = candidate.find('['), candidate.rfind(']')
    if start >= 0 and end >= start:
        candidate = candidate[start:end + 1]
    try:
        items = json.loads(candidate)
    except ValueError as error:
        raise EcommerceAnalysisError(f'AI 返回不是合法 JSON，{error}') from None
    if not isinstance(items, list):
        raise EcommerceAnalysisError(
            f'AI 返回格式错误：期望 JSON 数组、实际 {type(items).__name__}')
    expected = carousel_count + detail_count
    if len(items) != expected:
        raise EcommerceAnalysisError(f'提示词总数不符：期望 {expected}、实际 {len(items)}')
    normalized = []
    indexes = {'carousel': [], 'detail': []}
    for position, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise EcommerceAnalysisError(
                f'第 {position} 项格式错误：期望 JSON 对象、实际 {type(item).__name__}')
        missing = [field for field in ('type', 'index', 'prompt', 'desc') if field not in item]
        if missing:
            raise EcommerceAnalysisError(
                f'第 {position} 项缺少字段：期望 type/index/prompt/desc、实际缺少 {"、".join(missing)}')
        kind, index = item['type'], item['index']
        if kind not in ('carousel', 'detail'):
            raise EcommerceAnalysisError(
                f'第 {position} 项 type 错误：期望 carousel 或 detail、实际 {kind!r}')
        if type(index) is not int or index <= 0:
            raise EcommerceAnalysisError(
                f'第 {position} 项 {kind} 序号错误：期望从 1 开始的正整数、实际 {index!r}')
        cleaned = {}
        for field, label, pattern in (
            ('prompt', '英文 prompt', r'[A-Za-z]'),
            ('desc', '中文 desc', r'[\u3400-\u4dbf\u4e00-\u9fff]'),
        ):
            value = item[field]
            if not isinstance(value, str) or not value.strip():
                raise EcommerceAnalysisError(
                    f'第 {position} 项 {kind} {field} 错误：期望非空{label}字符串、实际为空或非字符串')
            cleaned[field] = value.strip()
            # English prompts may contain Chinese product names and branding.
            if not re.search(pattern, cleaned[field]):
                raise EcommerceAnalysisError(
                    f'第 {position} 项 {kind} {field} 语言错误：期望{label}、实际缺少对应语言文字')
        indexes[kind].append(index)
        normalized.append(dict(type=kind, index=index, **cleaned))
    for kind, count in counts.items():
        actual = indexes[kind]
        if len(actual) != count:
            raise EcommerceAnalysisError(
                f'{kind} 类型数量不符：期望 {count}、实际 {len(actual)}')
        expected_indexes = list(range(1, count + 1))
        if sorted(actual) != expected_indexes:
            raise EcommerceAnalysisError(
                f'{kind} 序号错误（缺号、重复或未各自从 1 开始）：'
                f'期望 {expected_indexes}、实际 {actual}')
    return normalized


def analyze_and_parse(base_url, api_key, model, system_prompt, user_prompt,
                      carousel_count, detail_count, *, on_content=None, on_reasoning=None, thinking=False,
                      reference_paths=None):
    full_text, reasoning_text = analyze_stream(
        base_url, api_key, model, system_prompt, user_prompt,
        on_content=on_content, on_reasoning=on_reasoning, thinking=thinking,
        **({'reference_paths': reference_paths} if reference_paths else {}))
    try:
        items = parse_prompt_items(full_text, carousel_count, detail_count)
    except EcommerceAnalysisError as error:
        # A complete 2xx stream has already been received; parsing failure must
        # never make the manager submit the analysis to another model.
        raise EcommerceAnalysisError(error.message, 200, error.body_excerpt) from None
    return items, reasoning_text
