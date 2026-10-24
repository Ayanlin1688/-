"""Open only explicit existing local paths, reporting shell errors in the UI."""
import os
from pathlib import Path

from core.i18n import tr


def open_local(value, log_callback, folder=False):
    try:
        if not value:
            raise ValueError(tr('尚未设置本地路径'))
        path = Path(value).resolve()
        if folder and path.is_file():
            path = path.parent
        if not path.exists():
            raise FileNotFoundError(tr('文件或目录不存在：') + str(path))
        os.startfile(str(path))
        log_callback(tr('已打开：') + str(path), 'info')
    except Exception as error:
        log_callback(tr('无法打开：') + str(error), 'error')
