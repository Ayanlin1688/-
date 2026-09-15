"""历史记录库：迁移、读写、保留策略与千级基准（写放大治理验证）。"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from core.config_manager import ConfigManager
from core.history_store import HistoryStore


class HistoryStoreTests(unittest.TestCase):
    def test_upsert_records_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'history.sqlite3'
            store = HistoryStore(path)
            store.upsert({'local_id': 'a', 'status': 'completed', 'product': 'P1'})
            store.upsert({'local_id': 'a', 'status': 'failed'})
            store.upsert({'local_id': 'b', 'status': 'queued'})
            records = store.records()
            self.assertEqual([r['local_id'] for r in records], ['a', 'b'])
            self.assertEqual(records[0]['status'], 'failed')  # 原位更新
            again = HistoryStore(path)
            self.assertEqual([r['local_id'] for r in again.records()], ['a', 'b'])
            again.close(); store.close()

    def test_trim_keeps_newest(self):
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / 'history.sqlite3', keep=100)
            for index in range(150):
                store.upsert({'local_id': f'r{index:04d}', 'status': 'completed'})
            self.assertEqual(store.count(), 150)
            self.assertEqual(store.trim(), 100)
            self.assertEqual(store.records()[0]['local_id'], 'r0050')
            self.assertEqual(store.records()[-1]['local_id'], 'r0149')
            store.close()

    def test_config_migration_moves_history_to_sqlite_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'config.json'
            path.write_text(json.dumps({'history': [{'local_id': 'h1', 'status': 'completed', 'task_id': 't1'}]}),
                            encoding='utf-8')
            manager = ConfigManager(path)
            config = manager.load_config()
            self.assertEqual(config['history'], [])
            self.assertEqual([r['local_id'] for r in manager.history_records()], ['h1'])
            saved = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(saved['history'], [])
            self.assertTrue(saved.get('migrations', {}).get('history_to_sqlite'))
            # 再次加载：不重复迁移、不丢记录
            manager2 = ConfigManager(path)
            manager2.load_config()
            self.assertEqual([r['local_id'] for r in manager2.history_records()], ['h1'])

    def test_thousand_record_upserts_do_not_touch_config_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'config.json'
            manager = ConfigManager(path)
            manager.save_config()
            before = (path.stat().st_mtime_ns, path.stat().st_size)
            start = time.perf_counter()
            for index in range(1000):
                manager.history_upsert({'local_id': f'r{index:04d}', 'status': 'completed',
                                        'prompt_name': f'分镜{index}'})
            elapsed = time.perf_counter() - start
            after = (path.stat().st_mtime_ns, path.stat().st_size)
            self.assertEqual(before, after)  # 配置零写入：写放大终结
            self.assertLess(elapsed, 60.0)   # 宽松门槛：CI 共享磁盘上 1000 次单条事务可能慢数倍；写放大回归由上方 mtime 断言兜底
            start = time.perf_counter()
            records = manager.history_records()
            read_cost = time.perf_counter() - start
            self.assertLess(read_cost, 0.5)
            self.assertEqual(len(records), 1000)
            self.assertEqual(records[-1]['local_id'], 'r0999')


if __name__ == '__main__':
    unittest.main()
