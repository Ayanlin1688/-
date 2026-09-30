"""SQLite 任务历史：终结 config.json 全量重写（写放大），支持保留策略与快速读取。

- 单文件数据库落在 config.json 旁（与 submissions.sqlite3 同一约定）。
- 连接按操作开关（不持有长期文件句柄，Windows 下临时目录可正常清理）；
  记录以 JSON 载荷存储，附带常用索引列；WAL 模式、synchronous=NORMAL。
- records() 返回浅拷贝列表；约定调用方只读，不修改返回的 dict。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

DEFAULT_KEEP = 5000
AUTO_TRIM_MARGIN = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    sequence    INTEGER PRIMARY KEY AUTOINCREMENT,
    local_id    TEXT NOT NULL UNIQUE,
    updated_at  REAL NOT NULL DEFAULT 0,
    status      TEXT,
    product     TEXT,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_updated ON history(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_status ON history(status);
"""


class HistoryStore:
    """Task history persisted in SQLite instead of the config.json array."""

    def __init__(self, path, keep=DEFAULT_KEEP):
        self.path = Path(path)
        self.keep = max(100, int(keep or DEFAULT_KEEP))
        self.lock = threading.RLock()
        self._cache = []
        self._positions = {}
        self.persistence_available = False
        self.persistence_error = ''
        self.last_maintain_error = ''  # 最近一次整理失败原因（空 = 正常）
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock:
                conn = self._open()
                try:
                    conn.executescript(_SCHEMA)
                    conn.commit()
                finally:
                    conn.close()
                self.persistence_available = True
                self._load()
                self._trim()
                self.maintain()
        except (OSError, sqlite3.Error) as error:
            self._disable_persistence(error)
            self._load_readonly()

    # —— 连接与落盘（按操作开关，不持有长期句柄） ——
    def _open(self):
        conn = sqlite3.connect(str(self.path), timeout=15)
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
        except sqlite3.DatabaseError:
            pass
        return conn

    def _open_readonly(self):
        return sqlite3.connect(
            self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5
        )

    def _disable_persistence(self, error):
        self.persistence_available = False
        self.persistence_error = str(error)

    def _load_rows(self, conn):
        rows = conn.execute('SELECT payload FROM history ORDER BY sequence ASC').fetchall()
        records = []
        for (payload,) in rows:
            try:
                record = json.loads(payload)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get('local_id'):
                records.append(record)
        self._cache = records
        self._positions = {
            record.get('local_id'): index for index, record in enumerate(records)
        }

    def _write(self, records):
        if not self.persistence_available:
            return False
        stamp_value = time.time()
        try:
            conn = self._open()
            try:
                with conn:
                    for record in records:
                        conn.execute(
                            'INSERT INTO history(local_id, updated_at, status, product, payload) '
                            'VALUES(?,?,?,?,?) '
                            'ON CONFLICT(local_id) DO UPDATE SET updated_at=excluded.updated_at, '
                            'status=excluded.status, product=excluded.product, payload=excluded.payload',
                            (str(record.get('local_id')), stamp_value, str(record.get('status') or ''),
                             str(record.get('product') or ''),
                             json.dumps(record, ensure_ascii=False, default=str)))
            finally:
                conn.close()
            return True
        except (OSError, sqlite3.Error) as error:
            self._disable_persistence(error)
            return False

    # —— 内部 ——
    def _load(self):
        conn = self._open()
        try:
            self._load_rows(conn)
        finally:
            conn.close()

    def _load_readonly(self):
        try:
            if not self.path.is_file():
                return
            conn = self._open_readonly()
            try:
                self._load_rows(conn)
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            return

    def _trim(self):
        extra = len(self._cache) - self.keep
        if extra <= 0:
            return 0
        removed = self._cache[:extra]
        self._cache = self._cache[extra:]
        self._positions = {record.get('local_id'): index for index, record in enumerate(self._cache)}
        try:
            if not self.persistence_available:
                return extra
            conn = self._open()
            try:
                with conn:
                    for record in removed:
                        conn.execute('DELETE FROM history WHERE local_id=?', (record.get('local_id'),))
            finally:
                conn.close()
        except (OSError, sqlite3.Error) as error:
            self._disable_persistence(error)
        return extra

    def maintain(self, interval_days=7):
        """定期整理（默认 7 天一次）：VACUUM 回收空间并记录整理时间。

        整理失败（磁盘满、被占用等）不阻断启动：保留错误详情并留待下次重试。
        """
        with self.lock:
            try:
                conn = self._open()
            except (OSError, sqlite3.Error) as error:
                self.last_maintain_error = str(error)
                return False
            try:
                conn.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)')
                row = conn.execute("SELECT value FROM meta WHERE key='last_maintain'").fetchone()
                now = time.time()
                if row:
                    try:
                        if now - float(row[0]) < max(1, int(interval_days)) * 86400:
                            return False
                    except (TypeError, ValueError):
                        pass
                conn.execute('VACUUM')
                conn.execute("INSERT INTO meta(key, value) VALUES('last_maintain', ?) "
                             'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (str(now),))
                conn.commit()
                self.last_maintain_error = ''
                return True
            except (sqlite3.Error, OSError) as error:
                self.last_maintain_error = str(error)
                return False
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    # —— 对外 ——
    def upsert(self, record):
        record = dict(record)
        key = record.get('local_id')
        if not key:
            raise ValueError('历史记录缺少 local_id')
        with self.lock:
            self._write([record])
            position = self._positions.get(key)
            if position is None:
                self._positions[key] = len(self._cache)
                self._cache.append(record)
            else:
                self._cache[position] = record
            if len(self._cache) > self.keep + AUTO_TRIM_MARGIN:
                self._trim()
            return record

    def upsert_many(self, records):
        batch = []
        for record in records:
            record = dict(record)
            if not record.get('local_id'):
                continue
            batch.append(record)
        if not batch:
            return 0
        with self.lock:
            self._write(batch)
            for record in batch:
                key = record['local_id']
                position = self._positions.get(key)
                if position is None:
                    self._positions[key] = len(self._cache)
                    self._cache.append(record)
                else:
                    self._cache[position] = record
            if len(self._cache) > self.keep + AUTO_TRIM_MARGIN:
                self._trim()
        return len(batch)

    def records(self, limit=None):
        with self.lock:
            if limit is None:
                return list(self._cache)
            count = max(0, int(limit))
            if count == 0:
                return []
            return list(self._cache[-count:])

    def count(self):
        with self.lock:
            return len(self._cache)

    def trim(self, keep=None):
        with self.lock:
            if keep is not None:
                self.keep = max(100, int(keep))
            self._trim()
            return len(self._cache)

    def close(self):
        """连接按操作开关；此方法仅为接口兼容保留。"""
