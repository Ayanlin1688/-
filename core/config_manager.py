"""Atomic JSON configuration and history persistence."""

from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .history_store import HistoryStore


DEFAULT_CONFIG: dict[str, Any] = {
    "paths": {"prompts": "", "images": "", "output": ""},
    "workspace": {
        "model": "video-v3",
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "duration": 8,
        "generate_audio": True,
        "poll_interval": 5,
        "max_retries": 3,
        "skip_threshold": 3,
        "seed": "",
    },
    "api": {
        "base_url": "https://api.kkone.vip/v1",
        "api_key": "",
        "upload_url": "https://video.kkone.vip/api/uploads",
        "upload_api_key": "",
        "auto_upload_missing": True,
    },
    "model_pool": {
        "enabled": False,
        "strategy": "轮询",
        "auto_failover": True,
        "cooldown": 30,
        "models": [
            {"name": "seedance-2.5", "enabled": True, "status": "健康"},
            {"name": "video-v3", "enabled": True, "status": "健康"},
            {"name": "video-v2", "enabled": True, "status": "健康"},
        ],
    },
    "task_strategy": {
        "prevent_duplicates": True,
        "max_concurrency": 5,
        "auto_retry": True,
        "max_retries": 3,
        "retry_interval": 3,
        "failure_skip_threshold": 3,
        "unmatched_prompt": "跳过并警告",
        "naming_rule": "{序号}_{提示词名}.mp4",
        "open_folder_after_download": False,
        "watch_interval": 0,
        "disk_cleanup_days": 7,
        "disk_min_free_gb": 2,
    },
    "defaults": {
        "model": "video-v3",
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "duration": 8,
        "poll_interval": 5,
    },
    "appearance": {"theme": "dark", "language": "简体中文", "blur": False},
    "diagnostics": {"debug_mode": False},
    "updates": {"manifest_url": "", "check_on_start": True},
    "license": {"key": "", "trial_started": "", "trial_days": 14, "enforce": False},
    "prompt_detection": {"enabled": True, "fallback_model": ""},
    "prompt_conversion": {"enabled": True, "preserve_original": True, "prefer_same_format": True},
    "model_overrides": {},
    "schedule": {
        "enabled": False, "time": "23:00", "mode": "once", "after_finish": "keep",
        "next_run": "", "last_run": "",
    },
    "matching_order": {},
    "match_overrides": {},
    "history": [],
    "scan_settings": {"recursive": True},
    "download_settings": {"overwrite_existing": False, "naming_rule": "{序号}_{提示词名}.mp4"},
    "stations": [],
    "stations_active": "",
    "prompts_dir": "",
    "images_dir": "",
    "output_dir": "",
}

# The three directory selectors persist under `paths`; these flat aliases keep
# config.json compatible with the named fields prompts_dir/images_dir/output_dir.
DIRECTORY_ALIASES = (('prompts', 'prompts_dir'), ('images', 'images_dir'), ('output', 'output_dir'))

LEGACY_CONFIG = Path(__file__).resolve().parent.parent / "config.json"

_LEGACY_DATA_FILES = ('config.json', 'submissions.sqlite3', 'history.sqlite3', 'models_cache.json')


def default_config_path() -> Path:
    """默认数据目录：%APPDATA%\\Yanlin；YANLIN_CONFIG_DIR 可覆盖为便携模式。"""
    override = os.environ.get('YANLIN_CONFIG_DIR', '').strip()
    if override:
        return Path(override).expanduser() / 'config.json'
    base = os.environ.get('APPDATA') or os.environ.get('LOCALAPPDATA')
    if base:
        return Path(base) / 'Yanlin' / 'config.json'
    return LEGACY_CONFIG


def _copy_sqlite(source: Path, target: Path) -> None:
    """通过 SQLite 备份接口拷贝：即使源库正被运行中的实例使用，也是安全快照。"""
    if target.exists():
        target.unlink()
    source_conn = sqlite3.connect(str(source), timeout=10)
    try:
        target_conn = sqlite3.connect(str(target))
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
    finally:
        source_conn.close()


def _migrate_legacy_files(target: Path) -> None:
    """首次升级：把旧位置（仓库旁）的数据搬到新数据目录；逐项尽力，失败保持原状。"""
    try:
        if target.exists() or not LEGACY_CONFIG.is_file() or target.parent == LEGACY_CONFIG.parent:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for name in _LEGACY_DATA_FILES:
        source = LEGACY_CONFIG.parent / name
        destination = target.parent / name
        if not source.is_file() or destination.exists():
            continue
        try:
            if name.endswith('.sqlite3'):
                _copy_sqlite(source, destination)
            else:
                shutil.copy2(source, destination)
        except Exception:
            continue


def _deep_merge(defaults: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(defaults)
    for key, value in values.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class ConfigManager:
    """Owns the in-memory configuration and its JSON representation."""

    def __init__(self, path: str | Path | None = None, migrate: bool = False) -> None:
        if path is not None:
            self.path = Path(path)
        else:
            self.path = default_config_path()
            if migrate and not self.path.exists():
                _migrate_legacy_files(self.path)
            if (not self.path.exists() and LEGACY_CONFIG.exists()
                    and not os.environ.get('YANLIN_CONFIG_DIR', '').strip()):
                # 新目录尚未就绪（或未迁移）时回退旧位置，优先保住既有配置。
                self.path = LEGACY_CONFIG
        self.config: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        self.error_callback = None
        self._history_store: HistoryStore | None = None

    def load_config(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("configuration root must be an object")
            self.config = _deep_merge(DEFAULT_CONFIG, raw)
            if 'naming_rule' not in raw.get('download_settings', {}):
                self.config['download_settings']['naming_rule'] = self.config['task_strategy']['naming_rule']
            self.config['task_strategy']['naming_rule'] = self.config['download_settings']['naming_rule']
            self.config['workspace']['max_retries'] = self.config['task_strategy']['max_retries']
            self.config['workspace']['skip_threshold'] = self.config['task_strategy']['failure_skip_threshold']
            # 一次性迁移：旧版默认值（并发 1、重试 5）升级为无人值守新默认（全队列并发 5、重试 3）。
            migrations = raw.get('migrations')
            if not isinstance(migrations, dict) or not migrations.get('r6_unattended_defaults'):
                strategy = self.config['task_strategy']
                upgraded = False
                if strategy.get('max_concurrency') == 1:
                    strategy['max_concurrency'] = DEFAULT_CONFIG['task_strategy']['max_concurrency']
                    upgraded = True
                if strategy.get('max_retries') == 5:
                    strategy['max_retries'] = DEFAULT_CONFIG['task_strategy']['max_retries']
                    self.config['workspace']['max_retries'] = strategy['max_retries']
                    upgraded = True
                self.config['migrations'] = dict(migrations or {}, r6_unattended_defaults=True)
                if upgraded:
                    try:
                        self.save_config()
                    except Exception:
                        pass
            # 历史记录迁移：config.json 大数组 → SQLite 历史库（终结写放大）。
            migrations = self.config.get('migrations')
            if not isinstance(migrations, dict) or not migrations.get('history_to_sqlite'):
                stored = self.config.get('history')
                if isinstance(stored, list) and stored:
                    try:
                        store = self.history_store()
                        imported = store.upsert_many(stored)
                        if imported >= len(stored) or store.count() >= len(stored):
                            self.config['migrations'] = dict(self.config.get('migrations') or {}, history_to_sqlite=True)
                            self.config['history'] = []
                            try:
                                self.save_config()
                            except Exception:
                                pass
                    except Exception:
                        pass  # 迁移失败保持原状，下次启动重试
                else:
                    self.config['migrations'] = dict(migrations or {}, history_to_sqlite=True)
            # Mirror the spec-named directory fields; old configs may carry the
            # aliases instead of the canonical paths entries.
            for key, alias in DIRECTORY_ALIASES:
                value = self.config['paths'].get(key)
                if value:
                    self.config[alias] = value
                elif self.config.get(alias):
                    self.config['paths'][key] = self.config[alias]
            # Only migrate real Stage 1 bindings; synthetic demo entries never become production references.
            for name, images in raw.get('matching_order', {}).items():
                if all(not p.startswith('demo:') for p in images) and self.config['paths']['prompts']:
                    key = str((Path(self.config['paths']['prompts']) / name).resolve())
                    self.config['match_overrides'].setdefault(key, images)
        except (OSError, ValueError, json.JSONDecodeError):
            self.config = copy.deepcopy(DEFAULT_CONFIG)
        return copy.deepcopy(self.config)

    def save_config(self, config: dict[str, Any] | None = None) -> None:
        if config is not None:
            self.config = _deep_merge(DEFAULT_CONFIG, config)
        for key, alias in DIRECTORY_ALIASES:
            self.config[alias] = self.config['paths'].get(key, '')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.config-', suffix='.tmp', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(self.config, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if Path(temporary).exists():
                Path(temporary).unlink()

    def update(self, path: Iterable[str], value: Any, *, save: bool = True) -> bool:
        keys = tuple(path)
        if not keys:
            raise ValueError("configuration path cannot be empty")
        target: dict[str, Any] = self.config
        for key in keys[:-1]:
            child = target.get(key)
            if not isinstance(child, dict):
                child = {}
                target[key] = child
            target = child
        target[keys[-1]] = value
        for key, alias in DIRECTORY_ALIASES:
            if keys == ('paths', key):
                self.config[alias] = value
            elif keys == (alias,):
                self.config['paths'][key] = value
        if keys in {('task_strategy', 'naming_rule'), ('download_settings', 'naming_rule')}:
            self.config['task_strategy']['naming_rule'] = value
            self.config['download_settings']['naming_rule'] = value
        for workspace_key, strategy_key in [('max_retries', 'max_retries'), ('skip_threshold', 'failure_skip_threshold')]:
            if keys in {('workspace', workspace_key), ('task_strategy', strategy_key)}:
                self.config['workspace'][workspace_key] = value
                self.config['task_strategy'][strategy_key] = value
        if save:
            try:
                self.save_config()
            except OSError as error:
                # GUI callers report disk errors without letting a Qt slot exception abort the process.
                if self.error_callback:
                    self.error_callback(f'配置保存失败，修改暂存于内存：{error}', 'error')
                    return False
                else:
                    raise
        return True

    # —— 历史记录（SQLite 历史库；写放大治理） ——
    def history_store(self) -> HistoryStore:
        """懒加载历史记录库，路径与 config.json 同目录。"""
        if self._history_store is None:
            self._history_store = HistoryStore(self.path.with_name('history.sqlite3'))
        return self._history_store

    def history_records(self, limit=None):
        return self.history_store().records(limit)

    def history_upsert(self, record):
        return self.history_store().upsert(record)
