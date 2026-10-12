import concurrent.futures
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.submission_ledger import SubmissionLedger, is_orphan_uncertain


class SubmissionDiscardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'ledger.sqlite3'
        self.ledger = SubmissionLedger(self.path)
        self.scope = 'account'
        self.task = dict(prompt_sha256='prompt', signature='generation', local_id='one')
        result, self.record = self.ledger.reserve(self.task, self.scope)
        self.assertEqual(result, 'claimed')

    def test_discard_unknown_persists_terminal_state_and_default_reason(self):
        self.ledger.save(dict(self.record, status='submission_unknown', error='连接中断'), 'unknown')
        with patch('core.submission_ledger.time.time', return_value=1234.0):
            discarded = self.ledger.discard(self.record['ledger_id'], self.scope)
        self.assertEqual(discarded['status'], 'failed')
        self.assertIs(discarded['discarded'], True)
        self.assertEqual(discarded['discard_reason'], '用户确认无效并作废')
        self.assertEqual(discarded['ledger_state'], 'discarded')
        self.assertEqual(discarded['error'], '连接中断')
        self.assertNotIn('task_id', discarded)
        self.assertEqual(SubmissionLedger(self.path).records(self.scope), [discarded])
        with self.ledger._connection() as db:
            row = db.execute('SELECT state, updated FROM submissions WHERE id=?',
                             (self.record['ledger_id'],)).fetchone()
        self.assertEqual((row['state'], row['updated']), ('discarded', 1234.0))

    def test_discard_reserved_and_submitting_with_explicit_reason(self):
        for state in ('reserved', 'submitting'):
            with self.subTest(state=state):
                result, record = self.ledger.reserve(dict(self.task, prompt_sha256=state, signature=state), self.scope)
                self.assertEqual(result, 'claimed')
                self.ledger.save(record, state)
                discarded = self.ledger.discard(record['ledger_id'], self.scope, reason='用户确认此记录无效')
                self.assertEqual(discarded['discard_reason'], '用户确认此记录无效')
                self.assertEqual(discarded['ledger_state'], 'discarded')

    def test_discard_allows_same_prompt_to_be_reserved_again(self):
        self.ledger.save(self.record, 'unknown')
        self.ledger.discard(self.record['ledger_id'], self.scope)
        for signature in ('generation', 'other-model'):
            with self.subTest(signature=signature):
                result, record = self.ledger.reserve(dict(self.task, signature=signature), self.scope)
                self.assertEqual(result, 'claimed')
                self.assertNotEqual(record['ledger_id'], self.record['ledger_id'])
                self.assertNotIn('discarded', record)
                self.ledger.discard(record['ledger_id'], self.scope)

    def test_discard_does_not_release_other_uncertain_records(self):
        other_task = dict(self.task, local_id='two', prompt_sha256='other-prompt', signature='other-request')
        _, other_record = self.ledger.reserve(other_task, self.scope)
        self.ledger.save(other_record, 'unknown')
        self.ledger.discard(self.record['ledger_id'], self.scope)
        result, blocker = self.ledger.reserve(dict(other_task, signature='changed-model'), self.scope, False)
        self.assertEqual(result, 'unknown')
        self.assertEqual(blocker['ledger_id'], other_record['ledger_id'])

    def test_discard_rejects_active_and_completed_without_changing_data(self):
        for state in ('active', 'completed'):
            with self.subTest(state=state):
                self.ledger.save(dict(self.record, status=state), state)
                before = self.ledger.records(self.scope)
                with self.assertRaises(ValueError):
                    self.ledger.discard(self.record['ledger_id'], self.scope)
                self.assertEqual(self.ledger.records(self.scope), before)

    def test_discard_rejects_task_id_in_all_recoverable_states(self):
        for state in ('unknown', 'submitting', 'reserved'):
            with self.subTest(state=state):
                self.ledger.save(dict(self.record, task_id='provider-id'), state)
                before = self.ledger.records(self.scope)
                with self.assertRaisesRegex(ValueError, 'task_id'):
                    self.ledger.discard(self.record['ledger_id'], self.scope)
                self.assertEqual(self.ledger.records(self.scope), before)

    def test_discard_rejects_missing_id_and_wrong_scope(self):
        before = self.ledger.records(self.scope)
        for record_id, scope in (('missing', self.scope), (self.record['ledger_id'], 'other-account')):
            with self.subTest(record_id=record_id, scope=scope):
                with self.assertRaises(ValueError):
                    self.ledger.discard(record_id, scope)
                self.assertEqual(self.ledger.records(self.scope), before)

    def test_discarded_record_cannot_be_discarded_or_resolved_again(self):
        discarded = self.ledger.discard(self.record['ledger_id'], self.scope)
        with self.assertRaises(ValueError):
            self.ledger.discard(self.record['ledger_id'], self.scope, reason='覆盖原因')
        with self.assertRaises(ValueError):
            self.ledger.resolve(self.record['ledger_id'], self.scope, task_id='provider-id')
        self.assertEqual(self.ledger.records(self.scope), [discarded])

    def test_concurrent_discard_and_resolve_have_only_one_winner(self):
        self.ledger.save(self.record, 'unknown')

        def recover(action):
            client = SubmissionLedger(self.path)
            try:
                if action == 'discard':
                    return client.discard(self.record['ledger_id'], self.scope)['ledger_state']
                return client.resolve(self.record['ledger_id'], self.scope, task_id='provider-id')['ledger_state']
            except ValueError:
                return 'rejected'

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(recover, ('discard', 'resolve')))
        self.assertEqual(results.count('rejected'), 1)
        stored = self.ledger.records(self.scope)[0]
        self.assertIn(stored['ledger_state'], {'active', 'discarded'})
        if stored['ledger_state'] == 'active':
            self.assertEqual(stored['task_id'], 'provider-id')
            self.assertNotIn('discarded', stored)
        else:
            self.assertIs(stored['discarded'], True)
            self.assertNotIn('task_id', stored)


class OrphanUncertainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'ledger.sqlite3'
        self.ledger = SubmissionLedger(self.path)
        self.scope = 'account'
        task = dict(prompt_sha256='prompt', signature='generation', local_id='one')
        _, self.record = self.ledger.reserve(task, self.scope)
        self.record['status'] = 'submission_unknown'
        self.ledger.save(self.record, 'unknown')

    def assert_readonly_result(self, record, expected, scope=None):
        content = self.path.read_bytes()
        modified = self.path.stat().st_mtime_ns
        snapshot = dict(record)
        with patch.object(self.ledger, '_connection', side_effect=AssertionError('不允许打开可写连接')):
            result = is_orphan_uncertain(record, self.ledger, scope or self.scope)
        self.assertIs(result, expected)
        self.assertEqual(record, snapshot)
        self.assertEqual(self.path.read_bytes(), content)
        self.assertEqual(self.path.stat().st_mtime_ns, modified)

    def test_missing_ledger_id_is_orphan(self):
        record = dict(self.record)
        record.pop('ledger_id')
        self.assert_readonly_result(record, True)
        self.assert_readonly_result(dict(record, ledger_id=''), True)

    def test_missing_ledger_record_is_orphan(self):
        self.assert_readonly_result(dict(self.record, ledger_id='missing'), True)

    def test_terminal_ledger_states_are_orphans(self):
        for state in ('active', 'completed', 'released', 'rejected', 'discarded'):
            with self.subTest(state=state):
                self.ledger.save(self.record, state)
                self.assert_readonly_result(self.record, True)

    def test_recoverable_ledger_states_are_not_orphans(self):
        for state in ('unknown', 'submitting', 'reserved'):
            with self.subTest(state=state):
                self.ledger.save(self.record, state)
                self.assert_readonly_result(self.record, False)

    def test_other_account_record_is_orphan_in_current_scope(self):
        self.assert_readonly_result(self.record, True, scope='other-account')

    def test_non_uncertain_status_is_never_orphan(self):
        for status in ('queued', 'failed', 'completed', None):
            with self.subTest(status=status):
                self.assert_readonly_result(dict(self.record, status=status, ledger_id='missing'), False)

    def test_missing_database_is_not_created_by_orphan_check(self):
        self.path.unlink()
        self.assertTrue(is_orphan_uncertain(self.record, self.ledger, self.scope))
        self.assertFalse(self.path.exists())

    def test_empty_database_is_orphan_and_remains_empty(self):
        self.path.write_bytes(b'')
        self.assert_readonly_result(self.record, True)
        self.assertEqual(self.path.stat().st_size, 0)

    def test_corrupt_database_is_not_silently_classified_as_orphan(self):
        self.path.write_bytes(b'invalid sqlite database')
        before = self.path.read_bytes()
        with self.assertRaises(sqlite3.DatabaseError):
            is_orphan_uncertain(self.record, self.ledger, self.scope)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
