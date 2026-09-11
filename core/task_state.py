"""Shared task state, identity and interruptible batch controls."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import threading
import time

from .http_client import Cancelled
from .model_parameters import GROK, V3_MODELS, model_options

TERMINAL = {'completed', 'failed', 'cancelled', 'skipped'}
ACTIVE = {'queued', 'uploading', 'submitting', 'processing', 'downloading', 'retry_wait', 'cooling', 'paused'}
STATUS_TEXT = {'waiting': '等待中', 'queued': '生成中', 'uploading': '上传中', 'submitting': '提交中',
               'processing': '生成中', 'downloading': '下载中', 'completed': '已完成', 'retry_wait': '重试中',
               'cooling': '等待冷却', 'failed': '失败', 'cancelled': '已取消', 'skipped': '已跳过', 'paused': '已暂停'}


def stamp():
    return datetime.now().isoformat(timespec='seconds')


def resolve_output_directory(output_root, output_subdir=''):
    root = Path(output_root).expanduser().resolve()
    subdir = str(output_subdir or '')
    if subdir and (Path(subdir).name != subdir or subdir in {'.', '..'}):
        raise ValueError('产品输出目录无效')
    target = (root / subdir).resolve()
    try:
        inside_root = os.path.commonpath((str(root), str(target))) == str(root)
    except ValueError:
        inside_root = False
    if not inside_root:
        raise ValueError('产品输出目录超出视频保存目录')
    target.mkdir(parents=True, exist_ok=True)
    resolved = target.resolve()
    if os.path.commonpath((str(root), str(resolved))) != str(root):
        raise ValueError('产品输出目录超出视频保存目录')
    return str(resolved)


def parameters_for_model(model, values, pooled=False, image_count=0):
    """Pool requests adapt supported UI dimensions; standalone requests stay strict."""
    params = dict(values)
    if not pooled:
        return params
    options = model_options(model)
    if type(params.get('duration')) is int and params['duration'] not in options['durations']:
        params['duration'] = min(options['durations'], key=lambda v: (abs(v-params['duration']), -v))
    if params.get('aspect_ratio') not in options['ratios']:
        params['aspect_ratio'] = options['ratios'][0]
    resolutions = options['resolutions']
    if resolutions and params.get('resolution') not in resolutions:
        params['resolution'] = '768p' if model == 'MiniMax-H3' and params.get('resolution') == '720p' else ('720p' if '720p' in resolutions else resolutions[0])
    if model == GROK and image_count > 1 and params.get('resolution') == '1080p':
        params['resolution'] = '720p'
    return params


def task_signature(task, model, params, base_url):
    inputs = [task['prompt_path'], *task['images']]
    stats = [(p, Path(p).stat().st_mtime_ns, Path(p).stat().st_size) if Path(p).is_file() else (p, None, None) for p in inputs]
    effective = dict(model=model, duration=params.get('duration'), aspect_ratio=params.get('aspect_ratio'))
    if model in V3_MODELS:
        effective['resolution'] = '720p'
    elif model != 'video-v1':
        effective['resolution'] = params.get('resolution')
    if model in V3_MODELS or model in {'video-v2', 'video-v2-fast'}:
        if params.get('generate_audio') is not True:
            effective['generate_audio'] = params.get('generate_audio')
    if model in V3_MODELS and params.get('seed') not in ('', None):
        try:
            effective['seed'] = int(params['seed'])
        except (ValueError, TypeError):
            effective['seed'] = params['seed']
    return hashlib.sha256(json.dumps([stats, effective, base_url.rstrip('/')], sort_keys=True).encode()).hexdigest()


class TaskControl:
    def __init__(self):
        self.condition = threading.Condition()
        self.cancelled = False
        self.paused = False
        self.active_index = -1
        self._active = set()
        self._skipped = set()
        self._local = threading.local()

    @property
    def skipped(self):
        # The coordinator has no task binding; a worker skip must not cancel it.
        return getattr(self._local, 'index', -1) in self._skipped

    def begin(self, index):
        with self.condition:
            self.active_index = index
            self._local.index = index
            self._active.add(index)
            self._skipped.discard(index)

    def release(self, index):
        with self.condition:
            self._active.discard(index)
            self._skipped.discard(index)
            self._local.index = -1
            self.condition.notify_all()

    def skip_for(self, index):
        with self.condition:
            if index in self._active:
                self._skipped.add(index)
                self.condition.notify_all()

    def set(self, name, value):
        with self.condition:
            setattr(self, name, value)
            self.condition.notify_all()

    def check(self):
        with self.condition:
            if self.cancelled or self.skipped:
                raise Cancelled('已取消' if self.cancelled else '已跳过')

    def delay(self, seconds):
        deadline = time.monotonic() + seconds
        with self.condition:
            while time.monotonic() < deadline:
                self.check()
                self.condition.wait(max(0, deadline - time.monotonic()))
            self.check()

    def before_task(self):
        with self.condition:
            while self.paused:
                self.check()
                self.condition.wait()
            self.check()
