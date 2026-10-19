"""Streaming ecommerce analysis using the verified chat-completions contract."""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlsplit

import requests

from core.http_client import HttpClient, RequestError


DEFAULT_ANALYSIS_MODEL = 'gemini-3.8-flash'


class EcommerceAnalysisError(Exception):
    def __init__(self, message, status_code=None, body_excerpt=''):
        self.message = str(message)
        self.status_code = status_code
        self.body_excerpt = str(body_excerpt)[:500]
        super().__init__(self.message)


def _read_sse(response, redact, on_content, on_reasoning):
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
                'AI 流式响应帧不是合法 JSON', body_excerpt=redact(data)) from None
        if not isinstance(frame, dict):
            raise EcommerceAnalysisError('AI 流式响应帧格式错误：期望 JSON 对象')
        if frame.get('error'):
            raise EcommerceAnalysisError(
                f'AI 分析返回错误：{redact(data)[:500]}', body_excerpt=redact(data))
        choices = frame.get('choices')
        if not choices:
            # The final usage-only event has choices=[].
            continue
        if not isinstance(choices, list):
            raise EcommerceAnalysisError('AI 流式响应 choices 格式错误：期望数组')
        for choice in choices:
            delta = choice.get('delta') if isinstance(choice, dict) else None
            if not isinstance(delta, dict):
                raise EcommerceAnalysisError('AI 流式响应 delta 格式错误：期望对象')
            for field, parts, callback in (
                ('content', content_parts, on_content),
                ('reasoning_content', reasoning_parts, on_reasoning),
            ):
                value = delta.get(field)
                if value is None or value == '':
                    continue
                if not isinstance(value, str):
                    raise EcommerceAnalysisError(f'AI 流式响应 {field} 格式错误：期望字符串')
                parts.append(value)
                if callback is not None:
                    callback(value)
    return ''.join(content_parts), ''.join(reasoning_parts)


def _request_model(base_url, model, thinking):
    host = (urlsplit(str(base_url or '').strip()).hostname or '').lower()
    if thinking and model == 'deepseek-chat' and 'deepseek.com' in host:
        logging.getLogger('storyboard').info('deepseek-chat 已切换为 deepseek-reasoner')
        return 'deepseek-reasoner'
    return model


def analyze_stream(base_url, api_key, model, system_prompt, user_prompt, *,
                   timeout=(15, 120), on_content=None, on_reasoning=None, thinking=False):
    """Return complete content and reasoning, notifying callbacks per delta.

    Reuse the shared requests session and redaction. POST is sent once; a failed
    stream is reported to the caller without replaying an accepted analysis.
    """
    client = HttpClient(api_key=api_key, timeout=timeout)
    status_code = None
    request_model = _request_model(base_url, model, thinking)
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
    if thinking and (request_model.lower().startswith('claude-') or 'thinking' in request_model.lower()):
        payload['thinking'] = {'type': 'enabled', 'budget_tokens': 1024}
    try:
        with client.request(
            'POST', base_url.strip().rstrip('/') + '/chat/completions',
            check_status=False, stream=True,
            headers={'Content-Type': 'application/json', 'Accept': 'text/event-stream'},
            json=payload,
        ) as response:
            status_code = response.status_code
            if not 200 <= status_code < 300:
                excerpt = client.redact(response.text)[:500]
                raise EcommerceAnalysisError(
                    f'模型 {client.redact(request_model)} HTTP {status_code}: {excerpt}', status_code, excerpt)
            return _read_sse(response, client.redact, on_content, on_reasoning)
    except EcommerceAnalysisError as error:
        message = error.message
        if client.redact(request_model) not in message:
            message = f'模型 {client.redact(request_model)}：{message}'
        response_status = error.status_code if error.status_code is not None else status_code
        raise EcommerceAnalysisError(message, response_status, client.redact(error.body_excerpt)) from None
    except (RequestError, requests.RequestException) as error:
        raise EcommerceAnalysisError(
            f'模型 {client.redact(request_model)} AI 分析连接或读取失败：{client.redact(error)}',
            status_code if status_code is not None else getattr(error, 'status_code', None),
            client.redact(getattr(error, 'body', '')),
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
                      carousel_count, detail_count, *, on_content=None, on_reasoning=None, thinking=False):
    full_text, reasoning_text = analyze_stream(
        base_url, api_key, model, system_prompt, user_prompt,
        on_content=on_content, on_reasoning=on_reasoning, thinking=thinking)
    try:
        items = parse_prompt_items(full_text, carousel_count, detail_count)
    except EcommerceAnalysisError as error:
        # A complete 2xx stream has already been received; parsing failure must
        # never make the manager submit the analysis to another model.
        raise EcommerceAnalysisError(error.message, 200, error.body_excerpt) from None
    return items, reasoning_text
