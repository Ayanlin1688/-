"""Read bundled platform rules without depending on the working directory."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from functools import lru_cache
from importlib import resources
import json

import assets as platform_assets


LANGUAGE_TEXT = {
    'none': '图片中不包含任何文字',
    'zh': '中文',
    'en': '英文',
    'ja': '日文',
    'ko': '韩文',
    'ar': '阿拉伯语',
    'fr': '法语',
    'de': '德语',
    'es': '西班牙语',
    'pt': '葡萄牙语',
    'ru': '俄语',
    'th': '泰语',
    'vi': '越南语',
    'id': '印尼语',
    'ms': '马来语',
    'tr': '土耳其语',
    'it': '意大利语',
    'nl': '荷兰语',
    'pl': '波兰语',
    'multi': '图片文字同时包含中英文',
}
RATIOS = ['1:1', '5:4', '9:16', '16:9', '3:2', '4:3', '4:5', '3:4', '2:3']
MAX_PLATFORMS = 5


@lru_cache(maxsize=1)
def _catalog() -> dict[str, dict]:
    # PyInstaller puts the JSON alongside the collected assets package.
    resource = resources.files(platform_assets).joinpath('ecommerce_platforms.json')
    rows = json.loads(resource.read_text(encoding='utf-8'))
    return {row['id']: row for row in rows}


def all_platforms() -> list[dict]:
    """Return independent copies of all platform records in catalog order."""
    return deepcopy(list(_catalog().values()))


def domestic() -> list[dict]:
    return [platform for platform in all_platforms() if platform['group'] == 'domestic']


def overseas() -> list[dict]:
    return [platform for platform in all_platforms() if platform['group'] == 'overseas']


def get_platform(id: str) -> dict:
    """Return a copy of one record; unknown IDs raise KeyError."""
    return deepcopy(_catalog()[id])


def _resolve_platforms(platforms: Iterable[str | Mapping]) -> list[dict]:
    """Accept IDs or records while applying the shared selection limit."""
    if isinstance(platforms, (str, bytes)):
        raise ValueError('平台必须以列表形式提供')
    selected = [get_platform(item) if isinstance(item, str) else deepcopy(dict(item))
                for item in platforms]
    if len(selected) > MAX_PLATFORMS:
        raise ValueError(f'最多选择 {MAX_PLATFORMS} 个平台')
    if len({item['id'] for item in selected}) != len(selected):
        raise ValueError('不能重复选择同一平台')
    return selected


def detect_conflicts(ids: Iterable[str]) -> list[dict]:
    """Report incompatible backgrounds and default ratios, preserving order."""
    selected = _resolve_platforms(ids)
    conflicts = []
    white = [item['name'] for item in selected if item['bgRule'] == 'white']
    no_white = [item['name'] for item in selected if item['bgRule'] == 'no-white']
    if white and no_white:
        conflicts.append({
            'type': 'background',
            'names': [item['name'] for item in selected if item['bgRule'] != 'free'],
            'tip': (f'要求白底的平台：{"、".join(white)}；'
                    f'禁止白底的平台：{"、".join(no_white)}。背景要求互斥，请分两批生成。'),
        })
    for kind, label, field in (
        ('ratio_carousel', '轮播图', 'defaultCarouselRatio'),
        ('ratio_detail', '详情图', 'defaultDetailRatio'),
    ):
        ratios = list(dict.fromkeys(item[field] for item in selected))
        if len(ratios) > 1:
            defaults = '、'.join(f'{item["name"]} {item[field]}' for item in selected)
            conflicts.append({
                'type': kind,
                'names': [item['name'] for item in selected],
                'tip': (f'{label}默认比例不一致：{defaults}。'
                        f'可统一为 {ratios[0]} 或分两批生成。'),
            })
    return conflicts
