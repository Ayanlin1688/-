"""AIGC 内容标识：为下载产物写入机器可读的标注旁车文件。

- 与视频同目录生成 `<视频名>.aigc.json`（完全不改动视频文件本身）。
- 可见标识（画内水印 / 角标）需要视频转码能力，留待后续版本可选接入；
  当前先落地机器可读标注与导出说明，满足「可追溯」的第一步。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

AIGC_NOTICE = '本内容由人工智能生成合成'


def write_aigc_metadata(video_path, *, model='', task_id='', tool='Yanlin Smart-Creation Matrix'):
    """写入旁车标注并返回标注文件路径。"""
    video = Path(video_path)
    target = video.with_name(video.name + '.aigc.json')
    payload = {
        'notice': AIGC_NOTICE,
        'label': 'AIGC',
        'tool': tool,
        'model': str(model or ''),
        'task_id': str(task_id or ''),
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'video': video.name,
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return target
