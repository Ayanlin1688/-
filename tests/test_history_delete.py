import concurrent.futures
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.history_store import HistoryStore
from core.submission_ledger import SubmissionLedger


class HistoryDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'history.sqlite3'
        self.store = HistoryStore(self.path)
        self.store.upsert_many([dict(local_id=key, status='completed') for key in ('a', 'b', 'c', 'd')])

    def test_delete_many_counts_real_rows_and_persists(self):
        self.assertEqual(self.store.delete_many(['a', 'c', 'a', 'missing']), 2)
        self.assertEqual([record['local_id'] for record in self.store.records()], ['b', 'd'])
        self.assertEqual(self.store.count(), 2)
        self.assertEqual(HistoryStore(self.path).records(), self.store.records())
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM history').fetchone()[0], 2)

    def test_repeated_and_empty_deletions_do_not_fail(self):
        self.assertEqual(self.store.delete_many(['b']), 1)
        self.assertEqual(self.store.delete_many(['b', 'missing']), 0)
        self.assertEqual(self.store.delete_many([]), 0)
        self.assertEqual(self.store.count(), 3)

    def test_delete_rebuilds_positions_for_subsequent_upserts(self):
        self.store.delete_many(['a', 'c'])
        self.assertEqual(self.store._positions, {'b': 0, 'd': 1})
        self.store.upsert(dict(local_id='d', status='failed'))
        self.store.upsert(dict(local_id='a', status='queued'))
        self.assertEqual([record['local_id'] for record in self.store.records()], ['b', 'd', 'a'])
        self.assertEqual(self.store.records()[1]['status'], 'failed')
        self.assertEqual(self.store._positions, {'b': 0, 'd': 1, 'a': 2})
        self.assertEqual(HistoryStore(self.path).records(), self.store.records())

    def test_delete_all_clears_cache_and_accepts_generator(self):
        self.assertEqual(self.store.delete_many(key for key in ('a', 'b', 'c', 'd')), 4)
        self.assertEqual(self.store.records(), [])
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(self.store._positions, {})
        self.assertEqual(HistoryStore(self.path).count(), 0)
        self.store.upsert(dict(local_id='fresh', status='queued'))
        self.assertEqual(self.store._positions, {'fresh': 0})

    def test_failed_transaction_rolls_back_rows_and_preserves_cache(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER deny_delete BEFORE DELETE ON history "
                       "WHEN OLD.local_id='c' BEGIN SELECT RAISE(ABORT, 'delete blocked'); END")
        before = self.store.records()
        positions = dict(self.store._positions)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.delete_many(['a', 'c'])
        self.assertEqual(self.store.records(), before)
        self.assertEqual(self.store._positions, positions)
        self.assertEqual(HistoryStore(self.path).records(), before)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP TRIGGER deny_delete')
        self.assertEqual(self.store.delete_many(['a', 'c']), 2)

    def test_concurrent_delete_counts_each_row_only_once(self):
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(self.store.delete_many, (['b', 'c'], ['b', 'c'])))
        self.assertEqual(sorted(results), [0, 2])
        self.assertEqual(self.store.count(), 2)
        self.assertEqual(self.store._positions, {'a': 0, 'd': 1})
        self.assertEqual(HistoryStore(self.path).records(), self.store.records())

    def test_large_batch_is_not_limited_by_sql_parameter_count(self):
        records = [dict(local_id=f'bulk-{index}', status='failed') for index in range(1100)]
        self.store.upsert_many(records)
        self.assertEqual(self.store.delete_many(record['local_id'] for record in records), 1100)
        self.assertEqual(self.store.count(), 4)
        self.assertEqual(HistoryStore(self.path).records(), self.store.records())

    def test_deleting_history_does_not_release_uncertain_ledger_intent(self):
        ledger = SubmissionLedger(Path(self.temp.name) / 'ledger.sqlite3')
        task = dict(local_id='a', prompt_sha256='prompt', signature='generation', status='submission_unknown')
        _, record = ledger.reserve(task, 'account')
        ledger.save(record, 'unknown')
        self.store.upsert(record)
        before = ledger.records('account')
        self.assertEqual(self.store.delete_many(['a']), 1)
        self.assertEqual(ledger.records('account'), before)
        self.assertEqual(ledger.reserve(task, 'account')[0], 'unknown')


if __name__ == '__main__':
    unittest.main()
