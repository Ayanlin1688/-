"""Retained QThreads for short background jobs; never destroy a running thread."""
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

    def _finished(self, worker):
        self.workers.remove(worker)
        worker.deleteLater()
        if not self.workers:
            self.idle.emit()
