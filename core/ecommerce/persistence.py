"""Independent ecommerce tables sharing the history database, with memory fallback."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import threading
import time
from weakref import WeakSet


GROUP_STATUSES = ('analyzing', 'generating', 'succeeded', 'partial', 'failed', 'cancelled')
ITEM_STATUSES = ('pending', 'running', 'succeeded', 'failed', 'skipped')

_SCHEMA = """
CREATE TABLE IF NOT EXISTS product_image_groups (
    group_id TEXT PRIMARY KEY, created_at REAL, updated_at REAL,
    status TEXT CHECK(status IN ('analyzing','generating','succeeded','partial','failed','cancelled')),
    product_name TEXT, platforms TEXT, config TEXT, output_dir TEXT,
    totals TEXT, manifest TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS product_image_items (
    item_id TEXT PRIMARY KEY, group_id TEXT,
    kind TEXT CHECK(kind IN ('carousel','detail')), seq INTEGER,
    title TEXT, prompt TEXT, reference_paths TEXT, ratio TEXT, tier TEXT,
    model TEXT,
    status TEXT CHECK(status IN ('pending','running','succeeded','failed','skipped')),
    file_path TEXT, size TEXT, image_tokens INTEGER, warnings TEXT, error TEXT,
    updated_at REAL, UNIQUE(group_id,kind,seq)
);
CREATE INDEX IF NOT EXISTS idx_pimg_items_group ON product_image_items(group_id);
CREATE INDEX IF NOT EXISTS idx_pimg_groups_updated ON product_image_groups(updated_at DESC);
"""
_GROUP_DEFAULTS = dict(group_id='', created_at=0., updated_at=0., status='analyzing',
                       product_name='', platforms=[], config={}, output_dir='',
                       totals={}, manifest={}, error='')
_ITEM_DEFAULTS = dict(item_id='', group_id='', kind='', seq=0, title='', prompt='',
                      reference_paths=[], ratio='', tier='', model='', status='pending',
                      file_path='', size='', image_tokens=None, warnings=[], error='',
                      updated_at=0.)
_TABLES = {
    'product_image_groups': (_GROUP_DEFAULTS, 'group_id',
                             {'platforms': [], 'config': {}, 'totals': {}, 'manifest': {}}),
    'product_image_items': (_ITEM_DEFAULTS, 'item_id',
                            {'reference_paths': [], 'warnings': []}),
}


class EcommerceImageStore:
    _startup_lock = threading.RLock()
    _live_stores = WeakSet()

    def __init__(self, path=None):
        if path is None:
            from core.config_manager import default_config_path
            path = default_config_path().with_name('history.sqlite3')
        self.path = Path(path)
        self.lock = threading.RLock()
        self.persistence_available = False
        self.persistence_error = ''
        self._cache = {table: {} for table in _TABLES}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._open()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()
            self.persistence_available = True
            with self.lock:
                for table in _TABLES:
                    self._refresh(table)
        except (OSError, sqlite3.Error) as error:
            self._disable(error)
            # A read-only database may still contain recoverable prior work.
            try:
                conn = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro',
                                       uri=True, timeout=15)
                try:
                    for table in _TABLES:
                        self._load_table(conn, table)
                finally:
                    conn.close()
            except (OSError, sqlite3.Error):
                pass
        # UI rebuilds can open a second store while a manager is still running.
        # Only the first live store for this database performs startup recovery.
        self._database_key = str(self.path.resolve()).casefold()
        with self._startup_lock:
            if not any(store._database_key == self._database_key for store in self._live_stores):
                self.reconcile_interrupted()
            self._live_stores.add(self)

    def reconcile_interrupted(self, reason='上次运行未正常结束（程序中断）'):
        """Atomically settle unfinished work left by an earlier process."""
        result = {'groups': 0, 'items': 0}
        with self.lock:
            if not self.persistence_available:
                return result
            try:
                conn = self._open()
                try:
                    with conn:
                        # Acquire the write lock before selecting the recovery set.
                        conn.execute('BEGIN IMMEDIATE')
                        groups = conn.execute(
                            "SELECT group_id FROM product_image_groups WHERE status IN ('analyzing','generating')"
                        ).fetchall()
                        now = time.time()
                        for (group_id,) in groups:
                            changed = conn.execute(
                                "UPDATE product_image_items SET status='skipped',error=?,updated_at=? "
                                "WHERE group_id=? AND status IN ('pending','running')", (reason, now, group_id))
                            result['items'] += changed.rowcount
                            totals = dict.fromkeys(ITEM_STATUSES, 0)
                            totals.update(dict(conn.execute(
                                'SELECT status,COUNT(*) FROM product_image_items WHERE group_id=? GROUP BY status',
                                (group_id,)).fetchall()))
                            totals['total'] = sum(totals.values())
                            conn.execute(
                                "UPDATE product_image_groups SET status='failed',error=?,updated_at=?,totals=? "
                                'WHERE group_id=?', (reason, now, json.dumps(totals), group_id))
                        result['groups'] = len(groups)
                    for table in _TABLES:
                        self._load_table(conn, table)
                finally:
                    conn.close()
            except (OSError, sqlite3.Error) as error:
                self._disable(error)
                return {'groups': 0, 'items': 0}
        return result

    def _open(self):
        conn = sqlite3.connect(str(self.path), timeout=15)
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    def _disable(self, error):
        self.persistence_available = False
        self.persistence_error = str(error)

    def _load_table(self, conn, table):
        defaults, key, json_fields = _TABLES[table]
        rows = conn.execute(f'SELECT {",".join(defaults)} FROM {table}').fetchall()
        records = {}
        for values in rows:
            record = dict(zip(defaults, values))
            for name, fallback in json_fields.items():
                try:
                    record[name] = json.loads(record[name])
                except (TypeError, ValueError):
                    record[name] = deepcopy(fallback)
            records[record[key]] = record
        self._cache[table] = records

    def _refresh(self, table):
        if not self.persistence_available:
            return
        try:
            conn = self._open()
            try:
                self._load_table(conn, table)
            finally:
                conn.close()
        except (OSError, sqlite3.Error) as error:
            self._disable(error)

    def _write(self, table, records):
        defaults, key, json_fields = _TABLES[table]
        columns = list(defaults)
        if self.persistence_available:
            try:
                conn = self._open()
                try:
                    with conn:
                        conn.executemany(
                            f'INSERT INTO {table} ({",".join(columns)}) '
                            f'VALUES ({",".join("?" for _ in columns)}) '
                            f'ON CONFLICT({key}) DO UPDATE SET ' + ','.join(
                                f'{name}=excluded.{name}' for name in columns if name != key),
                            [tuple(json.dumps(rec[name], ensure_ascii=False, default=str)
                                   if name in json_fields else rec[name] for name in columns)
                             for rec in records])
                finally:
                    conn.close()
            except (OSError, sqlite3.Error) as error:
                self._disable(error)
        for record in records:
            self._cache[table][record[key]] = deepcopy(record)

    def upsert_group(self, rec):
        rec = deepcopy(dict(rec))
        if not rec.get('group_id'):
            raise ValueError('电商图组缺少 group_id')
        with self.lock:
            table = 'product_image_groups'
            self._refresh(table)
            record = deepcopy(_GROUP_DEFAULTS)
            record.update(self._cache[table].get(rec['group_id'], {}))
            record.update({key: value for key, value in rec.items() if key in record})
            if record['status'] not in GROUP_STATUSES:
                raise ValueError('无效的电商图组状态')
            record['created_at'] = record['created_at'] or time.time()
            record['updated_at'] = rec.get('updated_at', time.time())
            self._write(table, [record])
            return deepcopy(record)

    def get_group(self, group_id):
        with self.lock:
            self._refresh('product_image_groups')
            return deepcopy(self._cache['product_image_groups'].get(group_id))

    def list_groups(self, limit=None):
        with self.lock:
            self._refresh('product_image_groups')
            records = sorted(self._cache['product_image_groups'].values(),
                             key=lambda rec: rec['updated_at'], reverse=True)
            if limit is not None:
                records = records[:max(0, int(limit))]
            return deepcopy(records)

    def _item_record(self, rec, previous):
        rec = deepcopy(dict(rec))
        group_id, kind, seq = rec.get('group_id'), rec.get('kind'), rec.get('seq')
        if not group_id or kind not in ('carousel', 'detail') or type(seq) is not int or seq < 1:
            raise ValueError('图片任务需要 group_id、carousel/detail 与从 1 开始的 seq')
        item_id = f'{group_id}:{kind}:{seq}'
        if rec.get('item_id', item_id) != item_id:
            raise ValueError('item_id 必须为 group_id:kind:seq')
        record = deepcopy(_ITEM_DEFAULTS)
        record.update(previous.get(item_id, {}))
        record.update({key: value for key, value in rec.items() if key in record})
        record['item_id'] = item_id
        record['updated_at'] = rec.get('updated_at', time.time())
        if record['status'] not in ITEM_STATUSES:
            raise ValueError('无效的图片任务状态')
        return record

    def upsert_item(self, rec):
        with self.lock:
            table = 'product_image_items'
            self._refresh(table)
            record = self._item_record(rec, self._cache[table])
            self._write(table, [record])
            return deepcopy(record)

    def upsert_items_many(self, recs):
        with self.lock:
            table = 'product_image_items'
            self._refresh(table)
            previous = dict(self._cache[table])
            records = []
            for rec in recs:
                record = self._item_record(rec, previous)
                records.append(record)
                previous[record['item_id']] = record
            if records:
                self._write(table, records)
            return len(records)

    def list_items(self, group_id):
        with self.lock:
            self._refresh('product_image_items')
            records = [rec for rec in self._cache['product_image_items'].values()
                       if rec['group_id'] == group_id]
            return deepcopy(sorted(records, key=lambda rec: (rec['kind'], rec['seq'])))

    def item_counts(self, group_id):
        with self.lock:
            counts = dict.fromkeys(ITEM_STATUSES, 0)
            for rec in self.list_items(group_id):
                counts[rec['status']] += 1
            return counts

    def delete_group(self, group_id):
        with self.lock:
            if self.persistence_available:
                try:
                    conn = self._open()
                    try:
                        with conn:
                            conn.execute('DELETE FROM product_image_items WHERE group_id=?', (group_id,))
                            conn.execute('DELETE FROM product_image_groups WHERE group_id=?', (group_id,))
                    finally:
                        conn.close()
                except (OSError, sqlite3.Error) as error:
                    self._disable(error)
            removed = self._cache['product_image_groups'].pop(group_id, None)
            self._cache['product_image_items'] = {
                key: rec for key, rec in self._cache['product_image_items'].items()
                if rec['group_id'] != group_id}
            return removed is not None

    def close(self):
        """No connection is retained between operations."""
        with self._startup_lock:
            self._live_stores.discard(self)
