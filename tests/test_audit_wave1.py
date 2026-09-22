"""波次一审计修复的回归护栏：DPR 除零、非 ASCII 激活码、脱敏词表、records(0)、配置损坏备份、历史页截断。"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


class SurfaceShadowZeroDprTests(unittest.TestCase):
    """materials.SurfaceShadow 在 devicePixelRatioF()=0 时不得除零（回归日志实抓场景）。"""

    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication
        # 必须保留 app 引用：丢弃引用会让 QApplication 被回收，之后构建 QWidget 直接崩溃。
        cls.app = QApplication.instance() or QApplication([])

    def test_zero_dpr_does_not_divide_by_zero(self):
        from PyQt5.QtCore import QPoint
        from ui import materials

        class FakePixmap:
            def isNull(self): return False
            def width(self): return 200
            def height(self): return 100
            def devicePixelRatioF(self): return 0.0

        class FakePainter:
            def drawPixmap(self, *args): pass

        effect = materials.SurfaceShadow(None)
        effect.sourcePixmap = lambda *a, **k: (FakePixmap(), QPoint(0, 0))
        recorded = []
        original = materials.draw_shadow
        materials.draw_shadow = lambda painter, rect, hover, primary: recorded.append((rect.width(), rect.height()))
        try:
            effect.draw(FakePainter())  # 修复前 DPR=0 在此处抛 ZeroDivisionError
        finally:
            materials.draw_shadow = original
        self.assertEqual(recorded, [(200.0, 100.0)])


class LicenseAsciiGuardTests(unittest.TestCase):
    """非 ASCII 激活码必须给 ValueError（可提示），而不是 TypeError 逃逸。"""

    def test_non_ascii_key_raises_value_error(self):
        from core.licensing import parse_key
        for raw in ('YL1.abc.中文', 'YL1.abc.qQ==中文', 'YL2.abc.中文', '中文激活码'):
            with self.assertRaises(ValueError, msg=raw) as ctx:
                parse_key(raw)
            self.assertNotIsInstance(ctx.exception, TypeError)
            self.assertIn('无法识别', str(ctx.exception))

    def test_valid_key_with_surrounding_whitespace_still_parses(self):
        from core.licensing import make_key, parse_key
        key = make_key('测试客户', 'pro')
        data = parse_key('  ' + key + '  ')
        self.assertEqual(data['customer'], '测试客户')


class DiagnosticsRedactionTests(unittest.TestCase):
    """诊断包脱敏词表：license.key 等不带 _key 后缀的秘密字段也要替换。"""

    def test_key_like_fields_are_redacted(self):
        from core.diagnostics_pack import _redacted
        node = {
            'license': {'key': 'YL1.payload.sig', 'trial_started': '2026-01-01'},
            'token': 'abc', 'password': 'p@ss', 'secret': 's', 'api_key': 'k', 'upload_api_key': 'u',
            'nested': [{'key': 'inner'}],
            'plain': 'keep',
        }
        red = _redacted(node)
        self.assertEqual(red['license']['key'], '***')
        self.assertEqual(red['token'], '***')
        self.assertEqual(red['password'], '***')
        self.assertEqual(red['secret'], '***')
        self.assertEqual(red['api_key'], '***')
        self.assertEqual(red['upload_api_key'], '***')
        self.assertEqual(red['nested'][0]['key'], '***')
        self.assertEqual(red['plain'], 'keep')
        self.assertEqual(red['license']['trial_started'], '2026-01-01')


class HistoryStoreEdgeTests(unittest.TestCase):
    """records(0) 必须返回空列表，而不是全量（负索引切片陷阱）。"""

    def test_records_zero_returns_empty(self):
        from core.history_store import HistoryStore
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / 'history.sqlite3')
            store.upsert_many([{'local_id': str(i), 'status': 'completed'} for i in range(3)])
            self.assertEqual(store.records(0), [])
            self.assertEqual(len(store.records(2)), 2)
            self.assertEqual(len(store.records()), 3)


class ConfigCorruptionBackupTests(unittest.TestCase):
    """配置损坏：回退默认值前先备份原件，并留下可提示的 load_error。"""

    def test_corrupt_config_backs_up_and_reports(self):
        from core.config_manager import ConfigManager
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            path.write_text('{ not valid json', encoding='utf-8')
            manager = ConfigManager(path)
            manager.load_config()
            self.assertTrue(manager.load_error)
            self.assertIn('已回退默认设置', manager.load_error)
            backups = list(path.parent.glob('config.json.corrupt-*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding='utf-8'), '{ not valid json')


class HistoryPageDisplayLimitTests(unittest.TestCase):
    """历史页只渲染最近 100 条；同一批数据重复刷新应短路跳过重建。"""

    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_only_latest_hundred_rendered_and_short_circuit(self):
        from core.config_manager import ConfigManager
        from ui.pages.history_page import HistoryPage, HISTORY_DISPLAY_LIMIT
        with tempfile.TemporaryDirectory() as temp:
            manager = ConfigManager(Path(temp) / 'config.json')
            manager.load_config()
            page = HistoryPage(lambda *a: None, manager)
            records = [{'local_id': f'id{i}', 'status': 'completed', 'prompt_name': f'p{i}',
                        'created_at': '2026-09-22T10:00:00', 'finished_at': '2026-09-22T10:01:00',
                        'size_bytes': 1024} for i in range(150)]
            page.update_history(records)
            self.assertEqual(page.table.rowCount(), HISTORY_DISPLAY_LIMIT)
            self.assertEqual(len(page.records), HISTORY_DISPLAY_LIMIT)
            first_item = page.table.item(0, 1)
            page.update_history(list(records))
            self.assertIs(page.table.item(0, 1), first_item)


if __name__ == '__main__':
    unittest.main()
