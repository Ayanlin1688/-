import concurrent.futures
import tempfile
from pathlib import Path
import unittest

from core.submission_ledger import SubmissionLedger
from core.submission_safety import SubmissionGate


class LedgerTests(unittest.TestCase):
    def test_atomic_reservation_allows_only_one_owner(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'ledger.sqlite3'
            clients = [SubmissionLedger(path), SubmissionLedger(path)]
            task = dict(prompt_sha256='prompt', signature='generation', local_id='one')
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda client: client.reserve(task, 'account'), clients))
            self.assertEqual(sorted(item[0] for item in results), ['claimed', 'duplicate'])

    def test_uncertain_intent_blocks_other_model_and_survives_reopen(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'ledger.sqlite3'
            ledger = SubmissionLedger(path)
            task = dict(prompt_sha256='prompt', signature='model-a', local_id='one')
            _, record = ledger.reserve(task, 'account')
            ledger.save(record, 'submitting')
            other = dict(task, signature='model-b')
            self.assertEqual(SubmissionLedger(path).reserve(other, 'account', False)[0], 'unknown')
            self.assertEqual(ledger.reserve(other, 'other-account')[0], 'claimed')
            ledger.resolve(record['ledger_id'], 'account', task_id='known-id')
            self.assertEqual(ledger.records('account')[0]['task_id'], 'known-id')

    def test_confirmation_defaults_to_not_releasing_intent(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = SubmissionLedger(Path(root) / 'ledger.sqlite3')
            task = dict(prompt_sha256='prompt', signature='model-a', local_id='one')
            _, record = ledger.reserve(task, 'account')
            ledger.save(record, 'unknown')
            with self.assertRaises(ValueError):
                ledger.resolve(record['ledger_id'], 'account')
            self.assertEqual(ledger.reserve(task, 'account')[0], 'unknown')
            ledger.resolve(record['ledger_id'], 'account', confirmed_not_created=True)
            self.assertEqual(ledger.reserve(task, 'account')[0], 'claimed')

    def test_gate_backoff_reduces_429_admission_and_pauses_only_new_submits(self):
        clock = [0.0]
        gate = SubmissionGate(5, now=lambda: clock[0])
        delays = []
        for _ in range(5):
            delays.append(gate.failed(429))
            clock[0] += delays[-1]
        self.assertEqual(delays, [5, 10, 20, 40, 60])
        self.assertEqual(gate.limit, 1)
        self.assertTrue(gate.paused)
        gate.resume()
        self.assertFalse(gate.paused)

    def test_5xx_backoff_does_not_reduce_concurrency(self):
        gate = SubmissionGate(3, now=lambda: 100.0)
        self.assertEqual(gate.failed(503), 5)
        self.assertEqual(gate.remaining(), 5)
        self.assertEqual(gate.limit, 3)
        gate.succeeded()
        self.assertEqual(gate.failed(503), 5)
