import json
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
                         dict(pending=1, running=1, succeeded=1, failed=1, skipped=0))
        self.assertTrue(self.store.delete_group('g'))
        self.assertIsNone(self.store.get_group('g'))
        self.assertEqual(self.store.list_items('g'), [])
        self.assertEqual(self.store.item_counts('g'), dict.fromkeys(ITEM_STATUSES, 0))
        self.assertEqual(len(self.store.list_items('other')), 1)
        self.assertFalse(self.store.delete_group('absent'))

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
        self.group()
        self.store.upsert_item(self.item(reference_paths=['产品.png'], warnings=['测试']))
        self.store.close()
        reopened = EcommerceImageStore(self.path)
        self.assertEqual(reopened.get_group('g'), self.store.get_group('g'))
        self.assertEqual(reopened.list_items('g'), self.store.list_items('g'))

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

    def assert_fallback_crud(self, store):
        self.assertFalse(store.persistence_available)
        store.upsert_group(dict(group_id='memory', config={'offline': True}))
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
        self.assert_fallback_crud(EcommerceImageStore(path))

    def test_unwritable_database_path_falls_back_without_raising(self):
        directory = self.path.with_name('directory.sqlite3')
        directory.mkdir()
        self.assert_fallback_crud(EcommerceImageStore(directory))

    def test_runtime_database_failure_keeps_last_loaded_records(self):
        self.group('prior')
        with patch.object(self.store, '_open', side_effect=sqlite3.OperationalError('readonly database')):
            self.store.get_group('prior')
            self.assert_fallback_crud(self.store)
        self.assertEqual(self.store.get_group('prior')['product_name'], '产品')

    def test_invalid_batch_is_not_partially_written(self):
        with self.assertRaises(ValueError):
            self.store.upsert_items_many([self.item(), self.item(seq=0)])
        self.assertEqual(self.store.list_items('g'), [])
        with self.assertRaises(ValueError):
            self.store.upsert_item(self.item(item_id='incorrect'))


if __name__ == '__main__':
    unittest.main()
