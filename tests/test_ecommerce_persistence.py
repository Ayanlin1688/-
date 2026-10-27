import json
import math
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from core.ecommerce.persistence import EcommerceImageStore, ITEM_STATUSES
from core.history_store import HistoryStore


class EcommercePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'history.sqlite3'
        self.store = EcommerceImageStore(self.path)

    def group(self, group_id='g', **updates):
        return self.store.upsert_group(dict(group_id=group_id, product_name='产品',
                                            platforms=['douyin'], config={'tier': '1K'}, **updates))

    def item(self, kind='carousel', seq=1, **updates):
        return dict(group_id='g', kind=kind, seq=seq, prompt='Product photo', **updates)

    def test_schema_indexes_and_json_round_trip(self):
        self.assertTrue(self.store.persistence_available)
        record = self.group()
        with closing(sqlite3.connect(self.path)) as conn:
            indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertIn('idx_pimg_items_group', indexes)
            self.assertIn('idx_pimg_groups_updated', indexes)
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            platforms, config = conn.execute('SELECT platforms, config FROM product_image_groups').fetchone()
        self.assertEqual(json.loads(platforms), ['douyin'])
        self.assertEqual(json.loads(config), {'tier': '1K'})
        record['config']['tier'] = '4K'
        fetched = self.store.get_group('g')
        fetched['platforms'].append('amazon')
        self.assertEqual(self.store.get_group('g')['config']['tier'], '1K')
        self.assertEqual(self.store.get_group('g')['platforms'], ['douyin'])

    def test_group_upserts_order_limit_and_copy(self):
        self.group('old', updated_at=1)
        self.group('new', updated_at=3)
        self.store.upsert_group(dict(group_id='old', status='generating', updated_at=4))
        self.assertEqual([r['group_id'] for r in self.store.list_groups()], ['old', 'new'])
        self.assertEqual(len(self.store.list_groups(1)), 1)
        self.assertEqual(self.store.list_groups(0), [])
        self.assertEqual(self.store.get_group('old')['product_name'], '产品')
        records = self.store.list_groups()
        records[0]['config']['tier'] = '2K'
        self.assertEqual(self.store.get_group('old')['config']['tier'], '1K')
        self.assertIsNone(self.store.get_group('absent'))

    def test_item_upserts_sort_counts_and_delete_cascade(self):
        self.group()
        self.group('other')
        self.store.upsert_items_many([
            self.item('detail', 2, status='failed'), self.item('carousel', 2),
            self.item('detail', 1, status='succeeded'), self.item('carousel', 1),
            dict(group_id='other', kind='carousel', seq=1)])
        item = self.store.upsert_item(self.item(seq=2, status='running', warnings=['警告']))
        self.assertEqual(item['item_id'], 'g:carousel:2')
        records = self.store.list_items('g')
        self.assertEqual([(r['kind'], r['seq']) for r in records],
                         [('carousel', 1), ('carousel', 2), ('detail', 1), ('detail', 2)])
        records[1]['warnings'].append('不应保存')
        self.assertEqual(self.store.list_items('g')[1]['warnings'], ['警告'])
        self.assertEqual(self.store.item_counts('g'),
                         dict(pending=1, running=1, succeeded=1, failed=1, skipped=0, cancelled=0))
        self.assertTrue(self.store.delete_group('g'))
        self.assertIsNone(self.store.get_group('g'))
        self.assertEqual(self.store.list_items('g'), [])
        self.assertEqual(self.store.item_counts('g'), dict.fromkeys(ITEM_STATUSES, 0))
        self.assertEqual(len(self.store.list_items('other')), 1)
        self.assertFalse(self.store.delete_group('absent'))

    def test_touch_group_advances_timestamp_without_replacing_other_fields(self):
        before = self.group(status='generating', updated_at=10,
                            totals={'succeeded': 1}, manifest={'items': [{'title': '茶杯'}]},
                            error='保留诊断')
        with patch('core.ecommerce.persistence.time.time', return_value=20):
            touched = self.store.touch_group('g')
        self.assertGreater(touched['updated_at'], before['updated_at'])
        self.assertEqual(touched, dict(before, updated_at=20))
        touched['config']['tier'] = '4K'
        touched['manifest']['items'][0]['title'] = '不应保留'
        self.assertEqual(self.store.get_group('g'), dict(before, updated_at=20))

    def test_touch_group_updates_totals_and_copies_inputs_and_result(self):
        self.group(updated_at=10)
        totals = {'succeeded': 1, 'failed': 2}
        touched = self.store.touch_group('g', totals=totals, updated_at=25)
        self.assertEqual(touched['updated_at'], 25)
        self.assertEqual(touched['totals'], totals)
        totals['succeeded'] = 99
        touched['totals']['failed'] = 99
        self.assertEqual(self.store.get_group('g')['totals'], {'succeeded': 1, 'failed': 2})
        self.assertEqual(self.store.touch_group('g', totals={}, updated_at=30)['totals'], {})

    def assert_default_touch_timestamp_advances(self, clock_time):
        for memory in (False, True):
            for supplied_time in (None, 0):
                with self.subTest(memory=memory, supplied_time=supplied_time, clock_time=clock_time):
                    path = self.path.with_name(f'touch-clock-{memory}-{supplied_time}.sqlite3')
                    store = EcommerceImageStore(path)
                    self.addCleanup(store.close)
                    before = store.upsert_group(dict(
                        group_id='g', status='partial', updated_at=1000,
                        product_name='保留的产品', config={'tier': '2K'},
                        totals={'succeeded': 2, 'failed': 1},
                        manifest={'items': [{'title': '保留的说明'}]}, error='保留诊断'))
                    if memory:
                        store._disable(sqlite3.OperationalError('readonly database'))
                    previous = before['updated_at']
                    with patch('core.ecommerce.persistence.time.time', return_value=clock_time):
                        for _ in range(2):
                            touched = store.touch_group('g', updated_at=supplied_time)
                            self.assertEqual(touched['updated_at'], math.nextafter(previous, math.inf))
                            self.assertGreater(touched['updated_at'], previous)
                            self.assertEqual(touched, dict(before, updated_at=touched['updated_at']))
                            self.assertEqual(store.get_group('g'), touched)
                            previous = touched['updated_at']
                    if not memory:
                        store.close()
                        reopened = EcommerceImageStore(path)
                        self.addCleanup(reopened.close)
                        self.assertEqual(reopened.get_group('g'), touched)

    def test_touch_group_advances_timestamp_with_same_windows_clock_tick(self):
        self.assert_default_touch_timestamp_advances(1000)

    def test_touch_group_advances_timestamp_when_system_clock_moves_backwards(self):
        self.assert_default_touch_timestamp_advances(900)

    def test_touch_group_respects_explicit_nonzero_timestamp_before_previous(self):
        for memory in (False, True):
            with self.subTest(memory=memory):
                path = self.path.with_name(f'touch-explicit-{memory}.sqlite3')
                store = EcommerceImageStore(path)
                self.addCleanup(store.close)
                before = store.upsert_group(dict(
                    group_id='g', status='succeeded', product_name='保留的产品',
                    config={'tier': '2K'}, totals={'succeeded': 2}, updated_at=1000))
                if memory:
                    store._disable(sqlite3.OperationalError('readonly database'))
                with patch('core.ecommerce.persistence.time.time') as clock:
                    touched = store.touch_group('g', updated_at=900)
                clock.assert_not_called()
                self.assertEqual(touched, dict(before, updated_at=900))
                self.assertEqual(store.get_group('g'), touched)
                if not memory:
                    store.close()
                    reopened = EcommerceImageStore(path)
                    self.addCleanup(reopened.close)
                    self.assertEqual(reopened.get_group('g'), touched)

    def test_touch_missing_group_returns_none_without_writing(self):
        with patch.object(self.store, '_write', wraps=self.store._write) as write:
            self.assertIsNone(self.store.touch_group('absent', totals={'failed': 1}))
        write.assert_not_called()
        self.assertEqual(self.store.list_groups(), [])

    def test_touch_group_in_memory_fallback_does_not_open_database(self):
        before = self.group(updated_at=10, status='generating', totals={'succeeded': 0})
        self.store._disable(sqlite3.OperationalError('readonly database'))
        with patch.object(self.store, '_open', side_effect=AssertionError('must not open')):
            touched = self.store.touch_group('g', totals={'succeeded': 1}, updated_at=20)
            self.assertEqual(self.store.get_group('g'), touched)
        self.assertEqual(touched, dict(before, updated_at=20, totals={'succeeded': 1}))
        touched['totals']['succeeded'] = 99
        self.assertEqual(self.store.get_group('g')['totals'], {'succeeded': 1})

    def test_touch_group_reloads_latest_fields_before_writing_and_persists(self):
        self.group(updated_at=10, status='generating')
        other = EcommerceImageStore(self.path)
        self.addCleanup(other.close)
        other.upsert_group(dict(group_id='g', status='succeeded', product_name='最新产品',
                                config={'tier': '2K'}, error='最新诊断', updated_at=15))
        touched = self.store.touch_group('g', totals={'succeeded': 2}, updated_at=20)
        self.assertEqual(touched['status'], 'succeeded')
        self.assertEqual(touched['product_name'], '最新产品')
        self.assertEqual(touched['config'], {'tier': '2K'})
        self.assertEqual(touched['error'], '最新诊断')
        self.store.close()
        other.close()
        reopened = EcommerceImageStore(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get_group('g'), touched)

    def test_touch_group_write_failure_keeps_new_values_in_memory(self):
        self.group(updated_at=10, status='generating')
        original_write = self.store._write

        def fail_write(table, records):
            with patch.object(self.store, '_open', side_effect=sqlite3.OperationalError('readonly database')):
                original_write(table, records)

        with patch.object(self.store, '_write', side_effect=fail_write):
            touched = self.store.touch_group('g', totals={'failed': 1}, updated_at=20)
        self.assertFalse(self.store.persistence_available)
        self.assertEqual(self.store.get_group('g'), touched)
        self.assertEqual(touched['totals'], {'failed': 1})
        self.assertEqual(touched['updated_at'], 20)

    def test_concurrent_batch_upserts_keep_groups_and_sequences_separate(self):
        barrier = threading.Barrier(6)

        def write(worker):
            group_id = f'g{worker}'
            self.group(group_id)
            barrier.wait(timeout=10)
            for start in (1, 11):
                self.store.upsert_items_many([
                    dict(group_id=group_id, kind='carousel', seq=i, title=f'{worker}:{i}')
                    for i in range(start, start + 10)])
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(write, range(6)))
        for worker in range(6):
            items = self.store.list_items(f'g{worker}')
            self.assertEqual(len(items), 20)
            self.assertEqual([i['title'] for i in items], [f'{worker}:{i}' for i in range(1, 21)])

    def test_reopen_persists_records(self):
        self.group(status='succeeded')
        self.store.upsert_item(self.item(status='succeeded', reference_paths=['产品.png'], warnings=['测试']))
        self.store.close()
        reopened = EcommerceImageStore(self.path)
        self.assertEqual(reopened.get_group('g'), self.store.get_group('g'))
        self.assertEqual(reopened.list_items('g'), self.store.list_items('g'))

    def test_startup_reconciles_interrupted_groups_and_items_atomically(self):
        for status in ('analyzing', 'generating'):
            self.group(status, status=status)
            self.store.upsert_items_many([
                dict(group_id=status, kind='carousel', seq=i, status=item_status)
                for i, item_status in enumerate(('running', 'pending', 'succeeded', 'failed'), 1)])
        self.group('finished', status='succeeded')
        self.store.close()
        reopened = EcommerceImageStore(self.path)
        self.addCleanup(reopened.close)
        for group_id in ('analyzing', 'generating'):
            group = reopened.get_group(group_id)
            self.assertEqual(group['status'], 'failed')
            self.assertEqual(group['error'], '上次运行未正常结束（程序中断）')
            self.assertEqual(group['totals'], dict(pending=0, running=0, succeeded=1, failed=1,
                                                  skipped=2, cancelled=0, total=4))
            items = reopened.list_items(group_id)
            self.assertEqual([item['status'] for item in items], ['skipped', 'skipped', 'succeeded', 'failed'])
            self.assertTrue(all(item['error'] == group['error'] for item in items[:2]))
        self.assertEqual(reopened.get_group('finished')['status'], 'succeeded')
        self.assertEqual(reopened.reconcile_interrupted(), {'groups': 0, 'items': 0})
        reopened.close()
        again = EcommerceImageStore(self.path)
        self.assertEqual(again.get_group('generating'), reopened.get_group('generating'))
        again.close()

    def test_second_live_store_does_not_reconcile_current_work(self):
        self.group(status='generating')
        self.store.upsert_item(self.item(status='running'))
        other = EcommerceImageStore(self.path)
        self.addCleanup(other.close)
        self.assertEqual(other.get_group('g')['status'], 'generating')
        self.assertEqual(other.list_items('g')[0]['status'], 'running')

    def test_explicit_reconcile_returns_counts_and_readonly_store_skips(self):
        self.group()
        self.store.upsert_item(self.item())
        self.assertEqual(self.store.reconcile_interrupted('测试中断'), {'groups': 1, 'items': 1})
        self.assertEqual(self.store.get_group('g')['error'], '测试中断')
        self.store.persistence_available = False
        with patch.object(self.store, '_open', side_effect=AssertionError('must not write')):
            self.assertEqual(self.store.reconcile_interrupted(), {'groups': 0, 'items': 0})

    def test_real_history_store_can_share_database_in_both_creation_orders(self):
        for ecommerce_first in (True, False):
            with self.subTest(ecommerce_first=ecommerce_first):
                path = self.path.with_name(f'shared-{ecommerce_first}.sqlite3')
                if ecommerce_first:
                    images, history = EcommerceImageStore(path), HistoryStore(path)
                else:
                    history, images = HistoryStore(path), EcommerceImageStore(path)
                history.upsert(dict(local_id='video', status='done', product='原视频'))
                images.upsert_group(dict(group_id='ecommerce'))
                images.upsert_item(dict(group_id='ecommerce', kind='detail', seq=1))
                self.assertTrue(images.persistence_available)
                self.assertTrue(history.persistence_available)
                self.assertEqual(history.records()[0]['product'], '原视频')
                self.assertEqual(len(images.list_items('ecommerce')), 1)
                images.delete_group('ecommerce')
                self.assertEqual(history.count(), 1)
                images.upsert_group(dict(group_id='retained'))
                history.delete_many(['video'])
                self.assertIsNotNone(images.get_group('retained'))
                self.assertEqual(HistoryStore(path).count(), 0)
                images.close()
                history.close()

    def assert_fallback_crud(self, store, *, updated_at=None):
        self.assertFalse(store.persistence_available)
        store.upsert_group(dict(group_id='memory', config={'offline': True}, updated_at=updated_at))
        store.upsert_items_many([dict(group_id='memory', kind='detail', seq=1)])
        store.upsert_item(dict(group_id='memory', kind='detail', seq=1, status='failed'))
        self.assertEqual(store.list_groups()[0]['group_id'], 'memory')
        self.assertEqual(store.get_group('memory')['config'], {'offline': True})
        self.assertEqual(store.item_counts('memory')['failed'], 1)
        self.assertTrue(store.delete_group('memory'))
        self.assertEqual(store.list_items('memory'), [])
        store.close()

    def test_corrupt_database_falls_back_without_raising(self):
        path = self.path.with_name('corrupt.sqlite3')
        path.write_bytes(b'not a database')
        self.assert_fallback_crud(EcommerceImageStore(path), updated_at=1000.0)

    def test_unwritable_database_path_falls_back_without_raising(self):
        directory = self.path.with_name('directory.sqlite3')
        directory.mkdir()
        self.assert_fallback_crud(EcommerceImageStore(directory), updated_at=1000.0)

    def test_runtime_database_failure_keeps_last_loaded_records(self):
        self.group('prior')
        with patch.object(self.store, '_open', side_effect=sqlite3.OperationalError('readonly database')):
            self.store.get_group('prior')
            self.assert_fallback_crud(
                self.store, updated_at=self.store.get_group('prior')['updated_at'] + 1.0)
        self.assertEqual(self.store.get_group('prior')['product_name'], '产品')

    def test_invalid_batch_is_not_partially_written(self):
        with self.assertRaises(ValueError):
            self.store.upsert_items_many([self.item(), self.item(seq=0)])
        self.assertEqual(self.store.list_items('g'), [])
        with self.assertRaises(ValueError):
            self.store.upsert_item(self.item(item_id='incorrect'))


    def test_cancelled_round_trip_preserves_existing_schema_and_legacy_skips(self):
        self.group()
        with closing(sqlite3.connect(self.path)) as connection:
            schema = connection.execute("SELECT sql FROM sqlite_master WHERE name='product_image_items'").fetchone()[0]
        self.store.upsert_items_many([self.item(seq=1,status='cancelled',error='已取消，未发起图片请求'),
                                     self.item(seq=2,status='skipped',error='旧中断恢复')])
        self.assertEqual(self.store.list_items('g')[0]['status'], 'cancelled')
        self.store.close()
        reopened = EcommerceImageStore(self.path)
        try:
            self.assertEqual([item['status'] for item in reopened.list_items('g')], ['cancelled','skipped'])
            with closing(sqlite3.connect(self.path)) as connection:
                self.assertEqual(connection.execute("SELECT sql FROM sqlite_master WHERE name='product_image_items'").fetchone()[0], schema)
                self.assertEqual(connection.execute('SELECT status FROM product_image_items ORDER BY seq').fetchall(), [('skipped',),('skipped',)])
        finally:
            reopened.close()



if __name__ == '__main__':
    unittest.main()
