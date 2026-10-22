"""Offline-testable connection probes for configured API capabilities."""
from urllib.parse import urlsplit

import requests

from .api_errors import classify_http_error, classify_connection_error
from .ecommerce.analysis import is_gemini_native

from .http_client import HttpClient, RequestError
from .image_uploader import DEFAULT_UPLOAD_URL, ImageUploader
from .model_catalog import classify_model


_CHAT_MODEL_ORDER = (
    'claude-haiku-4-5',
    'claude-haiku-4-5-20251001',
    'gemini-3.8-flash',
    'gemini-3-flash-preview',
    'gpt-5.6-sol',
    'claude-sonnet-5',
)
_TOKEN_PREFIXES = ('vup_', 'sk-')
_CHAT_PROMPT = '只回复两个字：好的'


def looks_like_upload_token(value):
    text = str(value or '').strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.startswith(_TOKEN_PREFIXES):
        return True
    return len(text) >= 24 and not any(marker in text for marker in ('.', '/', ':'))


def valid_http_url(value):
    text = str(value or '').strip()
    if not text:
        return True
    try:
        parts = urlsplit(text)
        return parts.scheme.lower() in {'http', 'https'} and bool(parts.hostname)
    except (TypeError, ValueError):
        return False


def _response_models(payload):
    if not isinstance(payload, dict):
        return []
    rows = payload.get('data')
    if rows is None and isinstance(payload.get('models'), list):
        rows = [dict(row, id=row['name'].rsplit('/', 1)[-1])
                for row in payload['models'] if isinstance(row, dict) and isinstance(row.get('name'), str)]
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get('id'), str)]


def _counts(rows):
    counts = {'video': 0, 'chat': 0, 'image': 0}
    for row in rows:
        model_id = str(row.get('id') or '').strip()
        if not model_id or model_id.lower().endswith('-pack'):
            continue
        kind = classify_model(model_id, row.get('supported_endpoint_types', ()))
        if kind in counts:
            counts[kind] += 1
    return counts


def _snapshot_chat_models(models):
    if isinstance(models, dict):
        rows = []
        for model_id, record in models.items():
            if isinstance(record, dict):
                rows.append(dict(record, id=record.get('id') or model_id))
            elif isinstance(model_id, str):
                rows.append({'id': model_id})
    elif isinstance(models, (list, tuple)):
        rows = [row for row in models if isinstance(row, dict)]
    else:
        rows = []
    candidates = []
    for row in rows:
        model_id = str(row.get('id') or row.get('name') or '').strip()
        if (model_id and row.get('available', True)
                and (row.get('kind') == 'chat'
                     or classify_model(model_id, row.get('supported_endpoint_types', ())) == 'chat')
                and model_id not in candidates):
            candidates.append(model_id)
    return candidates


def _result(ok, level, message, counts, **extra):
    result = dict(ok=bool(ok), level=level, message=message, counts=dict(counts or {}))
    result.update(extra)
    return result


def probe_line(base_url, api_key, capability, *, models=None):
    """Check a model-list route and, for chat, one small completion request."""
    if capability not in {'video', 'chat', 'image'}:
        raise ValueError('不支持的线路能力')
    base = str(base_url or '').strip().rstrip('/')
    key = str(api_key or '').strip()
    counts = {'video': 0, 'chat': 0, 'image': 0}
    if not base:
        return _result(False, 'error', '未填写 API Base URL', counts)

    client = HttpClient(key, timeout=(10, 30), log=lambda *_: None)
    gemini = is_gemini_native(base)
    headers = {'x-goog-api-key': key} if gemini else {}
    status = None
    model = None
    try:
        response = client.request('GET', base + '/models', check_status=False,
                                  timeout=(10, 30), headers=headers)
        try:
            status = response.status_code
            if not 200 <= status < 300:
                message = classify_http_error(status, client.redact(response.text))['message']
                return _result(False, 'error', message, counts, status_code=status)
            try:
                payload = client.json(response)
            except RequestError:
                return _result(False, 'error', '模型列表响应格式无效，请检查中转站地址', counts, status_code=status)
        finally:
            response.close()

        rows = _response_models(payload)
        counts = _counts(rows)
        if capability == 'video':
            ok = counts['video'] >= 1
            message = ('模型列表可达，发现视频模型' if ok else '模型列表可达，但未发现视频模型')
            return _result(ok, 'success' if ok else 'error', message, counts, status_code=status)
        if capability == 'image':
            ok = counts['image'] >= 1
            message = ('模型列表可达，未实际出图' if ok else '模型列表可达，但未发现图片模型；未实际出图')
            return _result(ok, 'success' if ok else 'error', message, counts, status_code=status)

        try:
            hostname = (urlsplit(base).hostname or '').lower()
        except ValueError:
            hostname = ''
        if 'deepseek.com' in hostname:
            candidates = ['deepseek-flash']
        else:
            available = _snapshot_chat_models(models) if models is not None else []
            if not available:
                available = _snapshot_chat_models(rows)
            preferred = [name for name in _CHAT_MODEL_ORDER if name in available]
            candidates = preferred or available[:3]
        if not candidates:
            return _result(False, 'error', '模型列表可达，但没有可用的文本模型', counts, status_code=status)

        last_message = ''
        for model in candidates:
            status = None
            url = base + '/chat/completions'
            payload = {'model': model, 'stream': False, 'max_tokens': 8,
                       'messages': [{'role': 'user', 'content': _CHAT_PROMPT}]}
            if gemini:
                url = base + f'/models/{model}:generateContent'
                payload = {'contents': [{'role': 'user', 'parts': [{'text': _CHAT_PROMPT}]}]}
            response = client.request('POST', url, check_status=False, timeout=60,
                                      headers=headers, json=payload)
            try:
                status = response.status_code
                if not 200 <= status < 300:
                    last_message = classify_http_error(status, client.redact(response.text))['message']
                    if status in (404, 503):
                        continue
                    return _result(False, 'error', last_message, counts, status_code=status, model=model)
                if gemini:
                    try:
                        payload = client.json(response)
                    except RequestError:
                        return _result(False, 'error', '文本模型响应格式无效', counts, status_code=status, model=model)
                    if not isinstance(payload, dict):
                        return _result(False, 'error', '文本模型响应格式无效', counts, status_code=status, model=model)
                    if payload.get('error'):
                        error = payload['error']
                        code = error.get('code', 500) if isinstance(error, dict) else 500
                        message = classify_http_error(code, client.redact(response.text))['message']
                        return _result(False, 'error', message, counts, status_code=status, model=model)
                    if not payload.get('candidates'):
                        return _result(False, 'error', '文本模型未返回有效内容', counts, status_code=status, model=model)
                return _result(True, 'success', f'文本调用成功，命中模型 {model}', counts,
                               status_code=status, model=model)
            finally:
                response.close()
        return _result(False, 'error', last_message or '候选文本模型均不可用', counts,
                       status_code=status, model=candidates[-1])
    except (RequestError, requests.RequestException) as error:
        message = classify_connection_error(error)['message']
        return _result(False, 'error', message, counts,
                       status_code=getattr(error, 'status_code', None) or status, model=model)
    finally:
        client.close()


def probe_upload(api_config):
    """Validate upload fields and test configured credentials with a tiny PNG."""
    config = api_config if isinstance(api_config, dict) else {}
    raw_url = str(config.get('upload_url') or '').strip()
    token = str(config.get('upload_api_key') or '').strip()
    if raw_url and looks_like_upload_token(raw_url) and not token:
        if raw_url.lower().startswith('vup_'):
            message = ('vup_ 开头的是上传 Token，请粘贴到「上传 Token/图床 API Key」字段；'
                       '「上传 URL」应保持 https:// 开头或留空')
        elif raw_url.lower().startswith('sk-'):
            message = ('sk- 开头的值看起来是上传 Token，请粘贴到「上传 Token/图床 API Key」字段；'
                       '「上传 URL」应保持 https:// 开头或留空')
        else:
            message = ('该值看起来是上传 Token，请粘贴到「上传 Token/图床 API Key」字段；'
                       '「上传 URL」应保持 https:// 开头或留空')
        return dict(ok=False, level='error', message=message)
    url = raw_url or DEFAULT_UPLOAD_URL
    if not valid_http_url(url):
        return dict(ok=False, level='error', message='上传 URL 必须是有效的 http:// 或 https:// 地址，或留空使用默认地址')
    if not token:
        return dict(ok=True, level='warning', message='未配置上传 Token，跳过；不影响视频/文本/图片调用', url=url)
    uploader = ImageUploader(url, token, log=lambda *_: None)
    try:
        result = uploader.test_connection()
        return dict(ok=bool(result.get('ok')), level='success' if result.get('ok') else 'error',
                    message=str(result.get('message') or ''), status_code=result.get('status_code'), url=url)
    finally:
        uploader.close()
