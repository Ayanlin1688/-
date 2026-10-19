"""GUI-thread catalog ownership with identity-guarded asynchronous discovery."""
import copy
from urllib.parse import urlparse

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from core.api_client import ApiClient
from core.background import BackgroundJobs
from core.i18n import tr
from core.line_routing import distinct_lines, normalize_base_url, resolve_line
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
        self._catalogs = {}
        self._identity = set()
        self._line_order = []
        self._route_identities = {}
        self._rebuild_lines()
        config_manager.model_catalog_controller = self
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(700)
        self.debounce.timeout.connect(self.refresh)

    @staticmethod
    def _identity_for(line):
        return normalize_base_url(line.get('base_url', '')), str(line.get('api_key') or '').strip()

    def _new_catalog(self, identity):
        return ModelCatalog(self.config_manager.path, *identity, log=self.jobs.log_message.emit)

    def _resolved_lines(self):
        config = self.config_manager.config
        lines = distinct_lines(config)
        identities = []
        for line in lines:
            identity = self._identity_for(line)
            if identity not in identities:
                identities.append(identity)
        route_identities = {
            capability: self._identity_for(resolve_line(config, capability))
            for capability in ('video', 'chat', 'image')
        }
        priority = []
        for capability in ('video', 'chat', 'image'):
            identity = route_identities[capability]
            if identity not in priority:
                priority.append(identity)
        for identity in identities:
            if identity not in priority:
                priority.append(identity)
        return identities, priority, route_identities

    def _rebuild_lines(self):
        identities, order, routes = self._resolved_lines()
        identity_set = set(identities)
        changed = identity_set != self._identity
        self._catalogs = {
            identity: self._catalogs.get(identity) or self._new_catalog(identity)
            for identity in identities
        }
        self._identity = identity_set
        self._line_order = order
        self._route_identities = routes
        return changed

    def identity(self):
        return self._route_identities.get('video', ('', ''))

    @property
    def catalog(self):
        identity = self._route_identities.get('video')
        return self._catalogs.get(identity) or self._new_catalog(identity or ('', ''))

    def snapshot(self):
        merged = {}
        for identity in self._line_order:
            catalog = self._catalogs.get(identity)
            if catalog is None:
                continue
            for model, record in catalog.snapshot().items():
                merged.setdefault(model, record)
        return merged

    def snapshot_for(self, capability):
        identity = self._route_identities.get(capability)
        if identity is None:
            identity = self._identity_for(resolve_line(self.config_manager.config, capability))
        catalog = self._catalogs.get(identity)
        return catalog.snapshot() if catalog is not None else self._new_catalog(identity).snapshot()

    @property
    def syncing(self):
        return self._syncing

    def start(self):
        self.changed.emit(self.snapshot())
        self.state_changed.emit()
        if self.auto_sync and any(identity[1] for identity in self._identity):
            self.refresh()

    def credentials_changed(self, *_):
        if self._closing:
            return
        self.reload_lines()

    def reload_lines(self):
        """Rebind route identities and sync newly referenced lines."""
        if self._closing:
            return
        old_identities = set(self._identity)
        old_routes = dict(self._route_identities)
        self._rebuild_lines()
        identity_changed = old_identities != self._identity
        route_changed = old_routes != self._route_identities
        if not identity_changed and not route_changed:
            return
        new_identities = self._identity - old_identities
        if identity_changed:
            self._version += 1
            self._syncing = False
        self.changed.emit(self.snapshot())
        self.state_changed.emit()
        if not identity_changed:
            return
        self.debounce.stop()
        if self.auto_sync and new_identities:
            self._start_refresh(new_identities)

    def refresh(self):
        if self._closing:
            return
        self.debounce.stop()
        self._rebuild_lines()
        if self._syncing:
            return
        if not any(identity[1] for identity in self._identity):
            self.sync_finished.emit(False, len(self.snapshot()), tr('请先填写 API Key；当前使用内置模型'))
            return
        self._start_refresh(set(self._identity))

    @staticmethod
    def _line_name(identity):
        host = (urlparse(identity[0]).hostname or '').lower()
        if 'deepseek.com' in host:
            return 'DeepSeek'
        if 'kkone' in host:
            return 'kkone'
        parts = host.split('.')
        return (parts[-2] if len(parts) > 1 else host) or '线路'

    def _start_refresh(self, identities):
        identities = [identity for identity in self._line_order
                      if identity in identities]
        if not identities:
            return
        self._version += 1
        version = self._version
        expected_identities = set(self._identity)
        self._syncing = True
        self.state_changed.emit()

        def work():
            results = []
            for identity in identities:
                candidate = self._new_catalog(identity)
                if not identity[1]:
                    results.append((identity, candidate, '未配置 API Key'))
                    continue
                client = None
                try:
                    client = ApiClient(*identity, log=lambda *_: None)
                    candidate.refresh(client, persist=False)
                    error = candidate.last_error
                    results.append((identity, candidate, candidate.redact(error) if error else ''))
                except Exception as error:
                    results.append((identity, candidate, candidate.redact(error)))
                finally:
                    if client is not None:
                        client.close()
            return results

        def done(results):
            if (self._closing or version != self._version
                    or expected_identities != self._identity):
                return
            self._syncing = False
            succeeded, failures, total = [], [], 0
            for identity, candidate, error in results:
                if not error:
                    try:
                        candidate.save()
                    except Exception as failure:
                        error = candidate.redact(failure)
                if error:
                    failures.append((identity, error))
                    self._catalogs[identity] = candidate
                else:
                    self._catalogs[identity] = candidate
                    count = len(candidate.snapshot())
                    total += count
                    succeeded.append((identity, count))

            self.changed.emit(self.snapshot())
            self.state_changed.emit()
            details = [f'{self._line_name(identity)} {count} 个' for identity, count in succeeded]
            if failures:
                names = '、'.join(self._line_name(identity) for identity, _ in failures)
                if details:
                    message = tr('已同步：{details}；{failure_count} 条线路失败已用离线缓存').format(
                        details='、'.join(details), failure_count=len(failures))
                else:
                    message = tr('{failure_count} 条线路失败已用离线缓存').format(
                        failure_count=len(failures))
                self.log(tr('模型目录同步部分完成：{names}，使用离线缓存').format(names=names), 'warning')
            elif details:
                message = tr('已同步：{details}').format(details='、'.join(details))
                self.log(message, 'success')
            else:
                message = tr('请先填写 API Key；当前使用内置模型')
            if failures and not succeeded:
                total = len(self.snapshot())
            self.sync_finished.emit(not failures, total, message)

        def failed(message):
            safe = str(message)
            for identity in identities:
                if identity[1]:
                    safe = safe.replace(identity[1], '[REDACTED]')
            done([(identity, self._new_catalog(identity), safe) for identity in identities])

        self.jobs.start(work, done, failed)

    def shutdown(self):
        self._closing = True
        self._version += 1
        self.debounce.stop()
        self.jobs.request_stop()
