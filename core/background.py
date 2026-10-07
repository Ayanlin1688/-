"""Retained QThreads for short background jobs; never destroy a running thread."""
from PyQt5 import sip
from PyQt5.QtCore import QObject, QThread, pyqtSignal


class FunctionWorker(QThread):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function

    def run(self):
        try:
            self.succeeded.emit(self.function())
        except Exception as error:
            self.failed.emit(str(error))


class BackgroundJobs(QObject):
    idle = pyqtSignal()
    log_message = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.workers = []

    @property
    def busy(self):
        return bool(self.workers)

    def start(self, function, success, failure):
        worker = FunctionWorker(function, self)
        self.workers.append(worker)
        worker.succeeded.connect(success)
        worker.failed.connect(failure)
        worker.finished.connect(lambda: self._finished(worker))
        worker.start()
        return worker

    def request_stop(self):
        """Ask active workers to stop before the application begins closing."""
        for worker in list(self.workers):
            try:
                if worker.isRunning():
                    worker.requestInterruption()
            except RuntimeError:
                continue

    def force_stop(self, wait_ms=500):
        """Stop remaining workers so QApplication can exit without live QThreads."""
        for worker in list(self.workers):
            try:
                if worker.isRunning():
                    worker.terminate()
                    worker.wait(max(0, int(wait_ms)))
            except RuntimeError:
                continue
        for worker in list(self.workers):
            try:
                if not sip.isdeleted(worker):
                    worker.deleteLater()
            except RuntimeError:
                continue
        self.workers.clear()

    def _finished(self, worker):
        # 竞态兜底：窗口销毁收尾时，回调可能晚于 C++ 对象释放送达；对已失效的
        # 对象调用 deleteLater/emit 会抛 RuntimeError，并被 PyQt 升级为 qFatal
        # 直接终止进程（表现为偶发的 0xC0000409 崩溃）。
        removed = True
        try:
            self.workers.remove(worker)
        except ValueError:
            removed = False
        if sip.isdeleted(self):
            return
        if not sip.isdeleted(worker):
            worker.deleteLater()
        if removed and not self.workers:
            self.idle.emit()
