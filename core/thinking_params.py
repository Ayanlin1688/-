"""深度思考参数矩阵（纯函数模块：无 Qt、无网络、无 I/O）。

矩阵来源：参考软件 app.asar 前端 chunk（Electron v1.4.20261009，
``frontend/dist/assets/index-DB5p_9BG.js``）中的 ``XA/IC/wV/Xw/kV/y1e`` 五个函数
及其请求体映射，与交接文档 §5.2「深度思考参数矩阵」逐字一致：

- ``is_deepseek``：``/deepseek/i``。
- ``is_reasoning``：deepseek 或
  ``deepseek-r1|reasoner|qwq|[-/_]r1[-/]|[-/_]think|thinking`` 或
  ``o1/o3/o4`` 家族（``-mini/-preview/-pro`` 或词尾）或 gemini 或
  ``qwen*thinking|reasoner``。
- 开启参数：openai 兼容线路按模型族区分（deepseek / gemini / 其他 reasoning）；
  anthropic 线路为 ``thinking + max_tokens``；gemini 原生线路为 ``thinkingConfig``。
  非 reasoning 模型在 openai 兼容线路不下发任何参数。
- 关闭参数：仅 openai 兼容线路的 reasoning 模型下发（deepseek 为
  ``thinkingMode: disabled``，其他 reasoning 为 ``enable_thinking: false``）；
  anthropic/gemini 原生线路与非 reasoning 模型不下发。
- OpenAI 兼容请求体映射：``thinkingMode`` → 顶层 ``thinking``；``reasoning_effort``/
  ``enable_thinking``/``chat_template_kwargs`` 顶层直拼；gemini 模型的
  ``thinkingConfig`` → ``extra_body.google.thinking_config``（``include_thoughts``
  加 ``thinking_level`` 或 ``thinking_budget``）。
- 原生 gemini 请求体：``thinkingConfig`` 合并进 ``generationConfig``。
- anthropic 请求体：``thinking``/``max_tokens`` 顶层直拼。
- reasoning 增量键：``reasoning_content`` / ``reasoning`` / ``reasoningContent`` /
  ``reasoning_text``。

矩阵本身**不做模型 id 替换**；唯一保留的 id 改写是仓库既有契约
``request_model``（deepseek.com 主机 + 开启思考 + deepseek-chat/flash →
deepseek-v4-pro）。矩阵参数对新 id 同样按模型名重新判定。
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

from core.model_catalog import normalize_model_id


SUPPORTED_KINDS = ('openai', 'anthropic', 'gemini')

# 参考实现 jA 的抽取顺序：任一键命中即回调，空串与非字符串按原逻辑处理。
REASONING_KEYS = ('reasoning_content', 'reasoning', 'reasoningContent', 'reasoning_text')

_DEEPSEEK_RE = re.compile(r'deepseek', re.IGNORECASE)
_REASONING_RE = re.compile(
    r'deepseek-r1|reasoner|qwq|[-/_]r1[-/]|[-/_]think|thinking'
    r'|\bo[134](?:-mini|-preview|-pro|-|$)'
    r'|gemini'
    r'|qwen.*(?:thinking|reasoner)',
    re.IGNORECASE,
)
_GEMINI_RE = re.compile(r'gemini', re.IGNORECASE)
_GEMINI_3_RE = re.compile(r'gemini-3', re.IGNORECASE)

_ANTHROPIC_THINKING_BUDGET = 8000
_ANTHROPIC_THINKING_MAX_TOKENS = 16000
_MODEL_SWITCHES = {'deepseek-chat', 'deepseek-flash'}
_MODEL_SWITCH_TARGET = 'deepseek-v4-pro'


def is_deepseek(model):
    """模型名是否属于 DeepSeek 族（大小写不敏感）。"""
    return bool(_DEEPSEEK_RE.search(str(model or '')))


def is_reasoning(model):
    """模型是否支持/默认启用深度思考（参考实现 wV 的完整正则）。"""
    text = str(model or '')
    return bool(is_deepseek(text) or _REASONING_RE.search(text))


def gemini_thinking_config(model):
    """参考实现 XA：gemini-3 系用 thinkingLevel，其他 gemini 用 thinkingBudget=-1。"""
    if _GEMINI_3_RE.search(str(model or '')):
        return {
            'includeThoughts': True,
            'include_thoughts': True,
            'thinkingLevel': 'medium',
            'thinking_level': 'medium',
        }
    return {
        'includeThoughts': True,
        'include_thoughts': True,
        'thinkingBudget': -1,
        'thinking_budget': -1,
    }


def _thinking_off(kind, model):
    if kind == 'openai' and is_deepseek(model):
        return {'thinkingMode': {'type': 'disabled'}}
    if kind == 'openai' and is_reasoning(model):
        return {'enable_thinking': False,
                'chat_template_kwargs': {'enable_thinking': False}}
    return {}


def _thinking_on(kind, model):
    if kind == 'openai':
        if not is_reasoning(model):
            return {}
        if is_deepseek(model):
            return {'reasoning_effort': 'high',
                    'thinkingMode': {'type': 'enabled'}}
        params = {'reasoning_effort': 'medium',
                  'enable_thinking': True,
                  'chat_template_kwargs': {'enable_thinking': True}}
        if _GEMINI_RE.search(str(model or '')):
            params['thinkingConfig'] = gemini_thinking_config(model)
        return params
    if kind == 'anthropic':
        return {
            'thinking': {'type': 'enabled', 'budget_tokens': _ANTHROPIC_THINKING_BUDGET},
            'max_tokens': _ANTHROPIC_THINKING_MAX_TOKENS,
        }
    return {'thinkingConfig': gemini_thinking_config(model)}


def thinking_payload(kind, model, enabled):
    """返回矩阵定义的“逻辑思考参数”（尚未映射到真实请求体字段）。

    ``kind`` 只能是 ``openai`` / ``anthropic`` / ``gemini``。关闭时只对
    openai 兼容线路的 reasoning 模型下发关闭参数；其余返回空字典。
    """
    if kind not in SUPPORTED_KINDS:
        raise ValueError(f'不支持的思考参数线路：{kind!r}（期望 openai/anthropic/gemini）')
    if not enabled:
        return _thinking_off(kind, model)
    if kind in ('anthropic', 'gemini') or is_reasoning(model):
        return _thinking_on(kind, model)
    return {}


def _copy_value(value):
    return dict(value) if isinstance(value, dict) else value


def _google_thinking_config(config):
    """参考实现 mapping：includeThoughts → include_thoughts，二选一 level/budget。"""
    include = config.get('includeThoughts')
    mapped = {'include_thoughts': True if include is None else include}
    if config.get('thinkingLevel'):
        mapped['thinking_level'] = config['thinkingLevel']
    if config.get('thinkingBudget') is not None:
        mapped['thinking_budget'] = config['thinkingBudget']
    return mapped


def apply_thinking_to_payload(payload, kind, model, enabled):
    """把逻辑思考参数映射成真实请求体字段并合并进 ``payload``，返回新字典。

    - openai：``thinkingMode`` → 顶层 ``thinking``；其余顶层直拼；gemini 模型的
      ``thinkingConfig`` → ``extra_body.google.thinking_config``，已有
      ``extra_body`` 的其他键原样保留（参考实现只替换 ``google`` 子树）。
    - gemini 原生：``thinkingConfig`` 合并进 ``generationConfig``（保留基础字段）。
    - anthropic：``thinking``/``max_tokens`` 顶层直拼。

    入参 ``payload`` 不会被修改。
    """
    if kind not in SUPPORTED_KINDS:
        raise ValueError(f'不支持的思考参数线路：{kind!r}（期望 openai/anthropic/gemini）')
    result = dict(payload) if isinstance(payload, dict) else {}
    params = thinking_payload(kind, model, enabled)
    if not params:
        return result

    if kind == 'openai':
        for key in ('reasoning_effort', 'enable_thinking', 'chat_template_kwargs'):
            if key in params:
                result[key] = _copy_value(params[key])
        if 'thinkingMode' in params:
            result['thinking'] = _copy_value(params['thinkingMode'])
        config = params.get('thinkingConfig')
        if isinstance(config, dict) and _GEMINI_RE.search(str(model or '')):
            extra_body = result.get('extra_body')
            extra_body = dict(extra_body) if isinstance(extra_body, dict) else {}
            extra_body['google'] = {'thinking_config': _google_thinking_config(config)}
            result['extra_body'] = extra_body
        return result

    if kind == 'gemini':
        config = params.get('thinkingConfig')
        if isinstance(config, dict):
            generation = result.get('generationConfig')
            generation = dict(generation) if isinstance(generation, dict) else {}
            generation['thinkingConfig'] = dict(config)
            result['generationConfig'] = generation
        return result

    for key, value in params.items():
        result[key] = _copy_value(value)
    return result


def request_model(base_url, model, enabled):
    """仓库既有契约：deepseek.com 主机 + 开启思考 + chat/flash → deepseek-v4-pro。

    该 id 改写与矩阵相互独立；矩阵参数对返回后的模型名重新判定。
    """
    normalized = normalize_model_id(model)
    host = (urlsplit(str(base_url or '').strip()).hostname or '').lower()
    if enabled and 'deepseek.com' in host and normalized in _MODEL_SWITCHES:
        logging.getLogger('storyboard').info('%s 已切换为 %s', normalized, _MODEL_SWITCH_TARGET)
        return _MODEL_SWITCH_TARGET
    return normalized


def extract_reasoning(delta):
    """从流式 delta 中取第一段非空 reasoning 文本（参考实现 jA 语义）。"""
    if not isinstance(delta, dict):
        return ''
    for key in REASONING_KEYS:
        value = delta.get(key)
        if isinstance(value, str) and value:
            return value
    return ''


__all__ = [
    'REASONING_KEYS', 'SUPPORTED_KINDS', 'apply_thinking_to_payload',
    'extract_reasoning', 'gemini_thinking_config', 'is_deepseek', 'is_reasoning',
    'request_model', 'thinking_payload',
]
