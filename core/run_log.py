"""Append-only per-day run log so unattended batches stay auditable after a restart."""
from datetime import datetime
import threading
import time
from pathlib import Path


class RunLog:
    """Write every UI-visible log line to logs/run-YYYYMMDD.log (best effort)."""

    RETENTION_DAYS = 14

    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.Lock()
        self.ok = True
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._trim(self.RETENTION_DAYS)
        except OSError:
            self.ok = False

    def _trim(self, retention_days):
        cutoff = time.time() - max(1, int(retention_days)) * 86400
        for path in self.root.glob('run-*.log'):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    def write(self, message, level='info', now=None):
        if not self.ok:
            return
        stamp = datetime.fromtimestamp(now or time.time())
        body = str(message).replace('\r', ' ').replace('\n', ' ')
        line = f"{stamp.strftime('%Y-%m-%d %H:%M:%S')} [{str(level).upper()}] {body}\n"
        try:
            with self.lock:
                with open(self.root / f"run-{stamp.strftime('%Y%m%d')}.log", 'a', encoding='utf-8') as stream:
                    stream.write(line)
        except OSError:
            self.ok = False
