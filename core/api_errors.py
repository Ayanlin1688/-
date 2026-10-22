"""User-facing API error categories, independent of UI and transport code."""
import json
import re


def _safe_message(text):
    text = re.sub(r'(?i)\b(?:sk-[\w-]+|vup_[\w-]+)', '[REDACTED]', text)
    text = re.sub(r'(?i)(bearer\s+)\S+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)((?:api[_ -]?key|token|authorization)\s*[=:]\s*)\S+',
                  r'\1[REDACTED]', text)
    return ' '.join(text.split())[:120]


def classify_http_error(status_code, body_text, *, kind='chat'):
    """Classify an HTTP rejection without exposing its raw JSON or credentials."""
    body = body_text if isinstance(body_text, str) else ''
    text = body.casefold()
    if isinstance(status_code, str) and status_code.isdecimal():
        status_code = int(status_code)

    def result(message, category, retryable=False, switchable=False):
        return dict(message=message, category=category, retryable=retryable, switchable=switchable)

    if status_code in (400, 422) and any(word in text for word in (
            'nudity', 'nude', 'desnudez', 'sexual', 'erotic', 'erótico', 'sexualidad',
            'safety', 'content policy', 'policy', 'invalid_request_error',
            'upstream_text_reply', '敏感', '审核', '违规', '色情')):
        return result('内容被上游安全审核拒绝，请修改提示词或参考图后重新生成', 'content_policy')
    if status_code in (401, 403):
        return result('密钥无效或没有该模型的调用权限', 'authentication')
    if status_code == 404 and any(word in text for word in (
            'model', '模型', 'not_found', 'not found', '无渠道')):
        return result('模型不存在或未在该中转站开通', 'model_not_found', switchable=True)
    if status_code == 429:
        return result('上游限流（429），请稍后重试或更换模型', 'rate_limit', True, True)
    if status_code in (500, 502, 503, 504) or any(word in text for word in (
            'overloaded', 'upstream_error', 'temporarily unavailable', 'service unavailable')):
        return result('上游服务暂时不可用，请稍后重试或更换模型', 'upstream', True, True)
    upstream = ''
    try:
        payload = json.loads(body)
        if isinstance(payload, dict):
            error = payload.get('error', payload)
            upstream = error.get('message', '') if isinstance(error, dict) else error
    except (ValueError, TypeError):
        upstream = body if not body.lstrip().startswith(('{', '[')) else ''
    short = _safe_message(upstream) if isinstance(upstream, str) else ''
    return result(f'HTTP {status_code}：{short or "上游请求被拒绝，请检查线路与参数"}', 'http')


def classify_connection_error(exc):
    text = (type(exc).__name__ + ' ' + str(exc)).casefold()
    if any(word in text for word in ('timeout', 'timed out')):
        message = '连接中转站超时，请检查网络或代理后重试'
    elif any(word in text for word in ('ssl', 'unexpected_eof', 'ssl_c_syscall', 'handshake')):
        message = '与中转站的 SSL 连接被中断，请检查网络、代理或中转站地址'
    else:
        message = '无法连接到中转站，请检查网络、代理或 API 地址'
    return dict(message=message, category='connection', retryable=True, switchable=True)
