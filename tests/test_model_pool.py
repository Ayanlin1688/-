import random
import threading
import unittest

from core.model_pool import ModelPool


class Clock:
    def __init__(self, monotonic=100.0, wall=1_000.0):
        self.monotonic = monotonic
        self.wall = wall

    def now(self):
        return self.monotonic

    def epoch(self):
        return self.wall

    def advance(self, seconds):
        self.monotonic += seconds
        self.wall += seconds


def settings(*models, strategy='轮询', cooldown=30, enabled=True):
    return {
        'enabled': enabled,
        'strategy': strategy,
        'cooldown': cooldown,
        'models': list(models),
    }


def model(name, enabled=True, status='健康', **extra):
    return dict(name=name, enabled=enabled, status=status, **extra)


class ModelPoolSelectionTests(unittest.TestCase):
    def test_disabled_pool_uses_only_default_model(self):
        pool = ModelPool(settings(model('A'), enabled=False), 'fallback')

        self.assertFalse(pool.enabled)
        self.assertEqual(pool.names, ['fallback'])
        self.assertEqual([pool.pick(), pool.pick()], ['fallback', 'fallback'])

    def test_round_robin_ignores_disabled_and_duplicate_entries(self):
        pool = ModelPool(settings(model('A'), model('B', enabled=False), model('C'), model('A')), 'fallback')

        self.assertEqual(pool.names, ['A', 'C'])
        self.assertEqual([pool.pick(), pool.pick(), pool.pick()], ['A', 'C', 'A'])

    def test_no_enabled_models_raises_for_pick_and_wait(self):
        pool = ModelPool(settings(model('A', enabled=False)), 'fallback')

        with self.assertRaises(ValueError):
            pool.pick()
        with self.assertRaises(ValueError):
            pool.wait_seconds()

    def test_initially_cooled_model_is_excluded(self):
        clock = Clock()
        pool = ModelPool(
            settings(model('A', status='冷却中'), model('B'), cooldown=12),
            'fallback', now=clock.now, wall=clock.epoch,
        )

        self.assertEqual(pool.pick(), 'B')
        self.assertEqual(pool.pick(), 'B')
        self.assertEqual(pool.wait_seconds(preferred='A'), 12)

    def test_preferred_model_never_switches_while_cooling(self):
        clock = Clock()
        pool = ModelPool(
            settings(model('A', status='冷却中'), model('B'), cooldown=8),
            'fallback', now=clock.now, wall=clock.epoch,
        )

        self.assertIsNone(pool.pick(preferred='A'))
        self.assertEqual(pool.wait_seconds(preferred='A'), 8)
        self.assertEqual(pool.pick(preferred='B'), 'B')

    def test_after_uses_configured_order_and_wraps(self):
        pool = ModelPool(settings(model('A'), model('B'), model('C')), 'fallback')

        self.assertEqual(pool.pick(after='A'), 'B')
        self.assertEqual(pool.pick(after='C'), 'A')

    def test_after_can_reuse_recovered_source_only_when_others_are_cooling(self):
        clock = Clock()
        pool = ModelPool(
            settings(model('A'), model('B', status='冷却中'), model('C', status='冷却中')),
            'fallback', now=clock.now, wall=clock.epoch,
        )

        self.assertEqual(pool.pick(after='A'), 'A')

    def test_priority_always_selects_first_eligible_model(self):
        clock = Clock()
        pool = ModelPool(
            settings(model('A', status='冷却中'), model('B'), model('C'), strategy='优先级'),
            'fallback', now=clock.now, wall=clock.epoch,
        )

        self.assertEqual([pool.pick(), pool.pick()], ['B', 'B'])

    def test_seeded_random_selection_excludes_unusable_entries(self):
        clock = Clock()
        pool = ModelPool(
            settings(
                model('A'), model('disabled', enabled=False), model('cooled', status='冷却中'),
                model('C'), model('A'), strategy='随机',
            ),
            'fallback', now=clock.now, wall=clock.epoch, rng=random.Random(7),
        )

        self.assertEqual([pool.pick() for _ in range(8)], ['C', 'A', 'C', 'A', 'A', 'A', 'C', 'A'])

    def test_round_robin_picks_are_serialized_under_thread_contention(self):
        pool = ModelPool(settings(model('A'), model('B'), model('C')), 'fallback')
        barrier = threading.Barrier(61)
        results = []
        result_lock = threading.Lock()

        def pick_once():
            barrier.wait()
            chosen = pool.pick()
            with result_lock:
                results.append(chosen)

        threads = [threading.Thread(target=pick_once) for _ in range(60)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual({name: results.count(name) for name in pool.names}, {'A': 20, 'B': 20, 'C': 20})


class ModelPoolHealthTests(unittest.TestCase):
    def test_delayed_old_notification_cannot_overwrite_new_health(self):
        snapshots = []; delayed = threading.Event(); release = threading.Event()
        pool = ModelPool(settings(model('A')), 'fallback', failure_threshold=2, on_change=snapshots.append)
        publish = pool._publish
        def reordered(recovered, snapshot):
            if snapshot and snapshot[0]['consecutive_failures'] == 1:
                delayed.set(); release.wait(2)
            publish(recovered, snapshot)
        pool._publish = reordered
        worker = threading.Thread(target=lambda: pool.failed('A'))
        worker.start(); delayed.wait(2)
        pool.failed('A'); release.set(); worker.join(2)
        self.assertEqual(snapshots[-1][0]['status'], '冷却中')
        self.assertEqual(snapshots[-1][0]['consecutive_failures'], 2)

    def test_all_cooling_reports_earliest_wait_then_recovers_once(self):
        clock = Clock()
        logs = []
        changes = []
        pool = ModelPool(
            settings(
                model('A', status='冷却中'),
                model('B', status='冷却中', cooldown_until=clock.wall + 20),
                cooldown=10,
            ),
            'fallback', now=clock.now, wall=clock.epoch,
            log=lambda message, level: logs.append((message, level)),
            on_change=changes.append,
        )

        self.assertIsNone(pool.pick())
        self.assertEqual(pool.wait_seconds(), 10)
        clock.advance(10)
        self.assertEqual(pool.pick(), 'A')
        self.assertEqual(logs, [('模型 A 冷却结束，恢复可用', 'info')])
        self.assertEqual(len(changes), 1)
        pool.refresh()
        self.assertEqual(len(changes), 1)
        clock.advance(10)
        pool.refresh()
        self.assertEqual(logs[-1], ('模型 B 冷却结束，恢复可用', 'info'))
        self.assertEqual(len(changes), 2)

    def test_failure_threshold_and_forced_cooldown_update_snapshot(self):
        clock = Clock()
        changes = []
        pool = ModelPool(
            settings(model('A'), model('B'), cooldown=15),
            'fallback', failure_threshold=3, now=clock.now, wall=clock.epoch,
            on_change=changes.append,
        )

        self.assertEqual(pool.failed('A'), 0)
        self.assertEqual(pool.failed('A'), 0)
        self.assertEqual(pool.failed('A'), 15)
        state = pool.snapshot()[0]
        self.assertEqual(state['status'], '冷却中')
        self.assertEqual(state['consecutive_failures'], 3)
        self.assertEqual(state['cooldown_until'], 1_015)
        self.assertEqual(state['remaining'], 15)
        self.assertEqual(len(changes), 3)

        self.assertEqual(pool.failed('B', force_cooldown=True), 15)
        self.assertEqual(pool.snapshot()[1]['consecutive_failures'], 1)

    def test_stale_parallel_success_cannot_clear_active_cooldown(self):
        clock = Clock()
        pool = ModelPool(
            settings(model('A'), cooldown=10),
            'fallback', failure_threshold=1, now=clock.now, wall=clock.epoch,
        )

        pool.failed('A')
        pool.succeeded('A')
        state = pool.snapshot()[0]
        self.assertEqual(state['status'], '冷却中')
        self.assertEqual(state['remaining'], 10)
        self.assertEqual(state['consecutive_failures'], 0)
        self.assertIsNone(pool.pick())

    def test_persisted_wall_expiry_is_converted_to_monotonic_deadline(self):
        clock = Clock()
        pool = ModelPool(
            settings(
                model('future', status='冷却中', cooldown_until=clock.wall + 7),
                model('expired', status='冷却中', cooldown_until=clock.wall - 1),
            ),
            'fallback', now=clock.now, wall=clock.epoch,
        )

        self.assertEqual(pool.wait_seconds(preferred='future'), 7)
        self.assertEqual(pool.pick(preferred='expired'), 'expired')
        self.assertEqual(pool.snapshot()[1]['cooldown_until'], 0)
        clock.advance(7)
        self.assertEqual(pool.pick(preferred='future'), 'future')

    def test_expiry_resets_failures_and_callbacks_run_outside_lock(self):
        clock = Clock()
        callback_snapshots = []
        callback_completed = threading.Event()
        holder = {}

        def on_change(snapshot):
            callback_snapshots.append(snapshot)
            holder['pool'].snapshot()
            callback_completed.set()

        pool = ModelPool(
            settings(model('A'), cooldown=2),
            'fallback', failure_threshold=1, now=clock.now, wall=clock.epoch,
            on_change=on_change,
        )
        holder['pool'] = pool
        pool.failed('A')
        callback_completed.clear()
        clock.advance(2)

        worker = threading.Thread(target=pool.refresh, daemon=True)
        worker.start()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertTrue(callback_completed.is_set())
        self.assertEqual(callback_snapshots[-1][0]['consecutive_failures'], 0)

    def test_callback_changes_are_queued_without_recursive_notification(self):
        snapshots = []
        callback_depth = 0
        maximum_depth = 0
        holder = {}

        def on_change(snapshot):
            nonlocal callback_depth, maximum_depth
            callback_depth += 1
            maximum_depth = max(maximum_depth, callback_depth)
            snapshots.append(snapshot)
            if len(snapshots) == 1:
                holder['pool'].failed('B')
            callback_depth -= 1

        pool = ModelPool(
            settings(model('A'), model('B')),
            'fallback', on_change=on_change,
        )
        holder['pool'] = pool

        pool.failed('A')

        self.assertEqual(len(snapshots), 2)
        self.assertEqual(maximum_depth, 1)
        self.assertEqual(snapshots[-1][1]['consecutive_failures'], 1)


if __name__ == '__main__':
    unittest.main()
