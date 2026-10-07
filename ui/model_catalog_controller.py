"""GUI-thread catalog ownership with identity-guarded asynchronous discovery."""
import copy
from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from core.api_client import ApiClient
from core.background import BackgroundJobs
from core.model_catalog import ModelCatalog


def runtime_config(config_manager):
    result = copy.deepcopy(config_manager.config)
    result['_submission_ledger_path'] = str(config_manager.path.with_name('submissions.sqlite3'))
    history = getattr(config_manager, 'history_records', None)
    if callable(history):
        result['history'] = history()
    controller = getattr(config_manager, 'model_catalog_controller', None)
    if controller is not None:
        result['_model_catalog'] = controller.snapshot()
    return result


class ModelCatalogController(QObject):
    changed = pyqtSignal(object)
    sync_finished = pyqtSignal(bool, int, str)
    state_changed = pyqtSignal()

    def __init__(self, config_manager, log_callback, parent=None, auto_sync=True):
        super().__init__(parent)
        self.config_manager = config_manager
        self.log = log_callback
        self.auto_sync = auto_sync
        self.jobs = BackgroundJobs(self)
        self.jobs.log_message.connect(self.log)
        self._version = 0
        self._closing = False
        self._syncing = False
        self._identity = self.identity()
        self.catalog = self._new_catalog()
        config_manager.model_catalog_controller = self
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(700)
        self.debounce.timeout.connect(self.refresh)

    def identity(self):
        api = self.config_manager.config['api']
        return api.get('base_url', '').strip().rstrip('/'), api.get('api_key', '').strip()

    def _new_catalog(self):
        base, key = self.identity()
        return ModelCatalog(self.config_manager.path, base, key, log=self.jobs.log_message.emit)

    def snapshot(self):
        return self.catalog.snapshot()

    @property
    def syncing(self):
        return self._syncing

    def start(self):
        self.changed.emit(self.snapshot())
        self.state_changed.emit()
        if self.auto_sync and self.identity()[1]:
            self.refresh()

    def credentials_changed(self, *_):
        if self._closing or self.identity() == self._identity:
            return
        self._version += 1
        self._identity = self.identity()
        self.catalog = self._new_catalog()
        self._syncing = False
        self.changed.emit(self.snapshot())
        self.state_changed.emit()
        if self.auto_sync and self._identity[1]:
            self.debounce.start()
        else:
            self.debounce.stop()

    def refresh(self):
        if self._closing:
            return
        self.debounce.stop()
        if self.identity() != self._identity:
            self.credentials_changed()
            self.debounce.stop()
        if self._syncing:
            return
        if not self._identity[1]:
            self.sync_finished.emit(False, len(self.snapshot()), '请先填写 API Key；当前使用内置模型')
            return
        self._version += 1
        version, identity = self._version, self._identity
        candidate = self._new_catalog()
        self._syncing = True
        self.state_changed.emit()

        def work():
            client = ApiClient(*identity, log=lambda *_: None)
            try:
                # Only the still-current identity may persist data on completion.
                candidate.refresh(client, persist=False)
                return candidate
            finally:
                client.close()

        def done(result):
            if self._closing or version != self._version or identity != self.identity():
                return
            self._syncing = False
            error = result.last_error
            if not error:
                try:
                    result.save()
                except OSError as failure:
                    error = result.redact(failure)
            if error:
                self.log('使用离线模型缓存：' + error, 'warning')
                self.sync_finished.emit(False, len(self.snapshot()), '使用离线模型缓存：' + error)
            else:
                self.catalog = result
                self.changed.emit(self.snapshot())
                message = f'已同步{len(self.snapshot())}个模型'
                self.log(message, 'success')
                self.sync_finished.emit(True, len(self.snapshot()), message)
            self.state_changed.emit()

        def failed(message):
            candidate.last_error = candidate.redact(message)
            done(candidate)
        self.jobs.start(work, done, failed)

    def shutdown(self):
        self._closing = True
        self._version += 1
        self.debounce.stop()
        self.jobs.request_stop()
