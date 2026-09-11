"""Open only explicit existing local paths, reporting shell errors in the UI."""
import os
from pathlib import Path


def open_local(value, log_callback, folder=False):
    try:
        if not value:
            raise ValueError('尚未设置本地路径')
        path = Path(value).resolve()
        if folder and path.is_file():
            path = path.parent
        if not path.exists():
            raise FileNotFoundError(f'文件或目录不存在：{path}')
        os.startfile(str(path))
        log_callback(f'已打开：{path}', 'info')
    except Exception as error:
        log_callback(f'无法打开：{error}', 'error')
