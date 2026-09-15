"""极简运行时多语言：中文为源语言；英文映射表在 i18n_catalog.py。

- tr('设置')：按当前语言返回译文；未收录的字符串自动回退原文（渐进翻译，不阻塞）。
- set_language() 在启动与设置切换时调用；导航等界面骨架支持即时刷新。
- 新增界面代码直接写 tr('中文原文')，即可被后续翻译覆盖，无需改动调用点。
"""
from __future__ import annotations

from .i18n_catalog import EN

CURRENT = 'zh-CN'


def normalize(code) -> str:
    return 'en-US' if str(code or '').lower().startswith('en') else 'zh-CN'


def set_language(code) -> str:
    global CURRENT
    CURRENT = normalize(code)
    return CURRENT


def current_language() -> str:
    return CURRENT


def tr(text: str) -> str:
    if CURRENT == 'en-US':
        return EN.get(text, text)
    return text
