"""一键诊断包：日志 + 脱敏配置 + 环境信息 + 最近任务摘要 → 单文件 zip。

- 密钥类字段（api_key / *_key）在打包前统一替换为 `***`，日志按新到旧择取。
- 供售后支持与远程排障使用；也可作为用户自助排查的素材。
"""
from __future__ import annotations

import json
import platform
import time
import zipfile
from pathlib import Path

from .version import APP_NAME, APP_VERSION

_LOG_BUDGET = 12 * 1024 * 1024  # 日志总体积预算（12MB）
_HISTORY_LIMIT = 50


def _redacted(node):
    """递归脱敏：密钥类字符串字段替换为 ***。"""
    if isinstance(node, dict):
        result = {}
        for key, value in node.items():
            lower = str(key).lower()
            if isinstance(value, str) and (lower in {'api_key', 'upload_api_key'} or lower.endswith('_key')):
                result[key] = '***' if value.strip() else ''
            else:
                result[key] = _redacted(value)
        return result
    if isinstance(node, list):
        return [_redacted(item) for item in node]
    return node


def _collect_logs(data_dir: Path):
    """按修改时间从新到旧选取日志文件，控制在预算内。"""
    logs_dir = data_dir / 'logs'
    if not logs_dir.is_dir():
        return []
    files = sorted((path for path in logs_dir.iterdir() if path.is_file()),
                   key=lambda path: path.stat().st_mtime, reverse=True)
    selected, budget = [], _LOG_BUDGET
    for path in files:
        size = path.stat().st_size
        if size <= budget:
            selected.append(path)
            budget -= size
        if budget <= 0:
            break
    return selected


def build_diagnostics_pack(config_manager, out_path) -> Path:
    """收集信息并写入 zip；返回最终文件路径。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data_dir = Path(config_manager.path).parent
    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    environment = (
        f'{APP_NAME} v{APP_VERSION}\n'
        f'打包时间：{stamp}\n'
        f'Python：{platform.python_version()}（{platform.architecture()[0]}）\n'
        f'系统：{platform.platform()}\n'
        f'数据目录：{data_dir}\n'
        f'配置文件：{config_manager.path}\n'
    )
    try:
        history = list(config_manager.history_records(limit=_HISTORY_LIMIT) or [])
    except Exception:
        history = []
    config_dump = _redacted(config_manager.config)
    with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('environment.txt', environment)
        archive.writestr('config.redacted.json', json.dumps(config_dump, ensure_ascii=False, indent=2, default=str))
        archive.writestr('history-recent.json', json.dumps(history, ensure_ascii=False, indent=2, default=str))
        for log_path in _collect_logs(data_dir):
            archive.write(log_path, f'logs/{log_path.name}')
    return out_path
