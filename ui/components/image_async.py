"""任务列表与生成占位格共用的有界异步 QImage 解码服务。"""
from collections import OrderedDict
import os
from pathlib import Path
import threading
import weakref

from PyQt5 import sip
from PyQt5.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, pyqtSignal
from PyQt5.QtGui import QImage
from PyQt5.QtWidgets import QApplication


_IMAGE_CACHE = OrderedDict()
_CACHE_LIMIT = 64
_PENDING = {}
_LOCK = threading.Lock()
_DISPATCHER = None


def _image_size(size):
    if isinstance(size, QSize):
        return max(1, size.width()), max(1, size.height())
    if isinstance(size, int):
        return max(1, size), max(1, size)
    return max(1, int(size[0])), max(1, int(size[1]))


def image_cache_key(path, size=(240, 240)):
    """路径、文件版本和解码尺寸共同标识缓存，支持同名文件重新生成。"""
    absolute = os.path.abspath(str(path))
    width, height = _image_size(size)
    try:
        stat = os.stat(absolute)
        version = '%d\0%d' % (stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError, TypeError):
        version = 'missing'
    return '%s\0%s\0%dx%d' % (absolute, version, width, height)


def cached_image(key):
    """只读取缓存，不读盘、不解码；返回 None 表示缓存未命中。"""
    with _LOCK:
        image = _IMAGE_CACHE.get(key)
        if image is not None:
            _IMAGE_CACHE.move_to_end(key)
        return image


def clear_image_cache():
    """清空已完成的解码缓存，保留正在执行的任务及其订阅者。"""
    with _LOCK:
        _IMAGE_CACHE.clear()


def _decode_image(path, size):
    """仅在工作线程读盘、解码和降采样，不接触 QWidget 或 QPixmap。"""
    try:
        image = QImage.fromData(Path(path).read_bytes())
        width, height = _image_size(size)
        if not image.isNull() and (image.width() > width or image.height() > height):
            image = image.scaled(width, height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        return image
    except (OSError, ValueError, TypeError):
        return QImage()


class _ImageDispatcher(QObject):
    loaded = pyqtSignal(str, str, QImage)

    def __init__(self, parent):
        super().__init__(parent)
        self.loaded.connect(self._complete, Qt.QueuedConnection)

    def _complete(self, key, path, image):
        with _LOCK:
            _IMAGE_CACHE[key] = image
            _IMAGE_CACHE.move_to_end(key)
            while len(_IMAGE_CACHE) > _CACHE_LIMIT:
                _IMAGE_CACHE.popitem(last=False)
            callbacks = _PENDING.pop(key, ())
        for reference in callbacks:
            callback = reference() if isinstance(reference, weakref.WeakMethod) else reference
            if callback is None:
                continue
            owner = getattr(callback, '__self__', None)
            if isinstance(owner, QObject) and sip.isdeleted(owner):
                continue
            try:
                callback(key, path, image)
            except RuntimeError:
                # 页面删除或退出时，已经销毁的 Qt 接收者直接丢弃。
                continue


class _ImageLoader(QRunnable):
    def __init__(self, key, path, size, dispatcher):
        super().__init__()
        self.key, self.path, self.size = key, path, size
        self.dispatcher = dispatcher

    def run(self):
        try:
            image = _decode_image(self.path, self.size)
        except Exception:
            # 无效文件及解码器异常统一回传缺失占位。
            image = QImage()
        try:
            self.dispatcher.loaded.emit(self.key, self.path, image)
        except RuntimeError:
            with _LOCK:
                _PENDING.pop(self.key, None)


def load_image_async(path, size, callback):
    """在 GUI 线程调用；去重解码后在 GUI 回调 callback(key, path, image)。

    命中缓存时立即回调；绑定方法弱引用其宿主，页面销毁后不会访问已删除控件。
    返回缓存键，调用方可据此丢弃旧路径或旧文件版本的回调。
    """
    global _DISPATCHER
    app = QApplication.instance()
    if app is None:
        raise RuntimeError('请先初始化界面')
    path = str(path or '')
    size = _image_size(size)
    key = image_cache_key(path, size)
    image = cached_image(key)
    if image is not None:
        callback(key, path, image)
        return key
    if _DISPATCHER is None or sip.isdeleted(_DISPATCHER):
        _DISPATCHER = _ImageDispatcher(app)
    owner = getattr(callback, '__self__', None)
    reference = weakref.WeakMethod(callback) if owner is not None else callback
    with _LOCK:
        pending = _PENDING.get(key)
        if pending is not None:
            pending.append(reference)
            return key
        _PENDING[key] = [reference]
    QThreadPool.globalInstance().start(_ImageLoader(key, path, size, _DISPATCHER))
    return key
