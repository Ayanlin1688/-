"""Durable pre-POST intent; SQLite transactions protect concurrent processes."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid


def account_scope(config):
    api = config['api']
    identity = [api['base_url'].strip().rstrip('/'), api['api_key'].strip()]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def ledger_path(config):
    explicit = config.get('_submission_ledger_path')
    if explicit:
        return Path(explicit)
    # CLI callers without a ConfigManager remain tied to their material root,
    # never a global test/production database in the current directory.
    return Path(config['paths']['prompts']).resolve().parent / '.storyboard-submissions.sqlite3'


class SubmissionLedger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS submissions '
                       '(id TEXT PRIMARY KEY, scope TEXT NOT NULL, prompt_sha TEXT NOT NULL, '
                       'signature TEXT NOT NULL, state TEXT NOT NULL, record TEXT NOT NULL, updated REAL NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS submission_identity ON submissions(scope, signature)')
            db.execute('CREATE INDEX IF NOT EXISTS prompt_identity ON submissions(scope, prompt_sha)')

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _record(row):
        record = json.loads(row['record'])
        record['ledger_id'] = row['id']
        record['ledger_state'] = row['state']
        return record

    def records(self, scope):
        with self._connection() as db:
            return [self._record(row) for row in db.execute(
                'SELECT * FROM submissions WHERE scope=? ORDER BY updated', (scope,))]

    def reserve(self, task, scope, prevent_duplicates=True):
        """Return (claimed/duplicate/busy/unknown, record). Intent never expires."""
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = list(db.execute('SELECT * FROM submissions WHERE scope=? ORDER BY updated DESC', (scope,)))
            for row in rows:
                same_prompt = row['prompt_sha'] == task['prompt_sha256']
                same_request = row['signature'] == task['signature']
                if same_prompt and row['state'] in {'submitting', 'unknown'}:
                    return 'unknown', self._record(row)
                if same_prompt and row['state'] in {'reserved', 'active'}:
                    return ('duplicate' if same_request else 'busy'), self._record(row)
                if prevent_duplicates and same_request and row['state'] == 'completed':
                    return 'duplicate', self._record(row)
            record = dict(task, ledger_id=uuid.uuid4().hex)
            db.execute('INSERT INTO submissions VALUES (?,?,?,?,?,?,?)', (
                record['ledger_id'], scope, task['prompt_sha256'], task['signature'], 'reserved',
                self._encode(record), time.time()))
            return 'claimed', record

    @staticmethod
    def _encode(task):
        # The source snapshot stays in the worker unless the user opts to retain
        # it. Credentials are never part of a task record.
        return json.dumps({key: value for key, value in task.items() if not key.startswith('_')}, ensure_ascii=False)

    def save(self, task, state):
        if not task.get('ledger_id'):
            return
        with self._connection() as db:
            cursor = db.execute('UPDATE submissions SET signature=?, state=?, record=?, updated=? WHERE id=?',
                                (task['signature'], state, self._encode(task), time.time(), task['ledger_id']))
            if cursor.rowcount != 1:
                raise RuntimeError('提交账本记录缺失；已停止，避免重复扣费')

    def resolve(self, record_id, scope, *, task_id='', confirmed_not_created=False):
        """Only explicit recovery actions may release an uncertain intent."""
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM submissions WHERE id=? AND scope=?', (record_id, scope)).fetchone()
            if row is None or row['state'] not in {'unknown', 'submitting', 'reserved'}:
                raise ValueError('此记录不是待确认的提交')
            task = self._record(row)
            if task_id.strip():
                task.update(task_id=task_id.strip(), status='queued', error='')
                state = 'active'
            elif confirmed_not_created:
                task.update(status='failed', error='用户确认服务端未创建任务', confirmed_not_created=True)
                state = 'released'
            else:
                raise ValueError('必须提供已有 task_id 或明确确认服务端未创建')
            db.execute('UPDATE submissions SET state=?, record=?, updated=? WHERE id=?',
                       (state, self._encode(task), time.time(), record_id))
            return task
