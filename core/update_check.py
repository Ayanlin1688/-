"""启动时可选更新检查：读取版本清单 JSON，与本机版本比较。

- 任何网络/解析异常一律静默返回 None（检查永不打扰正常使用）。
- fetch 参数仅供测试注入；生产走 urllib。
- 返回结果携带资产 sha256（若清单提供）：未来自动更新下载产物时必须校验；
  建议发布流程同时对该清单做签名（后续版本接入）。
"""
from __future__ import annotations

import json
import urllib.request

from .version import APP_VERSION


def _version_tuple(text):
    parts = []
    for piece in str(text or '').split('.'):
        digits = ''.join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple((parts + [0, 0, 0, 0])[:4])


def is_newer(candidate) -> bool:
    return _version_tuple(candidate) > _version_tuple(APP_VERSION)


def check_for_update(manifest_url, timeout=10, fetch=None):
    """返回 {'version', 'url', 'notes'} 或 None；不抛出任何异常。"""
    if not manifest_url:
        return None
    try:
        if fetch is None:
            with urllib.request.urlopen(manifest_url, timeout=timeout) as response:
                data = json.loads(response.read().decode('utf-8'))
        else:
            data = fetch(manifest_url)
        if not isinstance(data, dict):
            return None
        version = str(data.get('version') or '').strip()
        if not version or not is_newer(version):
            return None
        url = ''
        sha256 = ''
        assets = data.get('assets') or []
        if isinstance(assets, list) and assets and isinstance(assets[0], dict):
            url = str(assets[0].get('download_url') or assets[0].get('file') or '')
            sha256 = str(assets[0].get('sha256') or '')
        if url and '://' in url and not url.lower().startswith('https://'):
            url = ''  # 非 https 的绝对链接一律不下发；相对文件名保留（仅作展示用途）
        return {'version': version, 'url': url, 'notes': str(data.get('notes') or ''), 'sha256': sha256}
    except Exception:
        return None
