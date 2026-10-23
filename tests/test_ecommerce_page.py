"""Offline Qt integration tests: real orchestration/files, injected model clients."""
import base64
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
import zlib
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import Qt, QThread, QTimer
from PyQt5.QtGui import QColor, QImage
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtWidgets import QApplication, QLabel
from qfluentwidgets import ComboBox

from core.config_manager import ConfigManager
from core.i18n import tr
from core.ecommerce.manager import EcommerceImageManager
from core.ecommerce.platforms import domestic, overseas, LANGUAGE_TEXT, RATIOS, MAX_PLATFORMS
from core.ecommerce.image_client import TIER_SHORT_EDGE
from core.ecommerce.persistence import EcommerceImageStore
from core.ecommerce.platforms import get_platform
from core.i18n import set_language
from tests.test_ecommerce_manager import TEST_KEY, fake_analysis, fake_image as manager_fake_image
from tests.test_task_manager import wait_until
from ui import materials
from ui.tokens import px
from ui.components.ecommerce_result_panel import EcommerceResultCard, EcommerceResultPanel
from ui.pages.ecommerce_page import EcommercePage, EcommerceWorker, _LIVE_WORKERS


# The networking fixture only checks PNG magic; Qt thumbnails need valid CRCs.
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==')


def solid_png(width, height, rgb):
    def chunk(name, payload):
        return (struct.pack('!I', len(payload)) + name + payload +
                struct.pack('!I', zlib.crc32(name + payload) & 0xFFFFFFFF))

    scanline = b'\x00' + bytes(rgb) * width
    raw = scanline * height
    header = struct.pack('!IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) +
            chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def fake_image(*args, **kwargs):
    result = manager_fake_image(*args, **kwargs)
    result.png_bytes = PNG
    return result


class EcommerceResultCardWarningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        set_language('zh-CN')

    def test_reference_token_notices(self):
        base = dict(item_id='fixture-item', kind='carousel', seq=1, status='pending')

        cases = (
            ('no references and zero tokens',
             dict(reference_paths=[], image_tokens=0,
                  warnings=['参考图可能未被模型采用（image_tokens=0）',
                            '参考图可能未被模型采用，请核对']),
             '', False, 'warning'),
            ('references and zero tokens',
             dict(reference_paths=['reference.png'], image_tokens=0, warnings=[]),
             '参考图可能未被模型采用，请核对', False, 'warning'),
            ('references and positive tokens',
             dict(reference_paths=['one.png', 'two.png'], image_tokens=24, warnings=[]),
             '已参考 2 张图（image_tokens=24）', True, 'success'),
            ('references and unknown tokens',
             dict(reference_paths=['reference.png'], image_tokens=None,
                  warnings=['参考图可能未被模型采用（image_tokens=0）']),
             '', False, 'warning'),
        )
        colors = materials.palette()
        for label, fields, expected_text, feedback_positive, color in cases:
            with self.subTest(case=label):
                card = EcommerceResultCard(dict(base, **fields))
                self.assertEqual(card.warning_label.text(), expected_text)
                self.assertEqual(card.warning_label.isHidden(), not bool(expected_text))
                self.assertEqual(card._feedback_positive, feedback_positive)
                self.assertIn(colors[color], card.warning_label.styleSheet())
                card.deleteLater()
                self.app.processEvents()

    def test_thumbnail_uses_responsive_four_to_five_cover_and_shared_placeholder_size(self):
        from tests.qt_scale_support import reset_interface_scaling
        from tests.test_interface_scaling import FakeScreen
        from ui.scaling import apply_interface_scale
        reset_interface_scaling(self.app)
        self.addCleanup(reset_interface_scaling, self.app)
        image_path = Path(__file__).with_name('_result_thumbnail_fixture.png')
        card = failed = None
        try:
            image_path.write_bytes(solid_png(1024, 1820, (42, 157, 102)))

            base = dict(item_id='portrait-item', kind='carousel', seq=1,
                        file_path=str(image_path), status='succeeded')
            card = EcommerceResultCard(base)
            card.resize(280, 760)
            card.show()
            self.app.processEvents()
            wait_until(lambda: not card.thumbnail._resize_timer.isActive())

            initial = card.thumbnail.size()
            self.assertGreater(initial.width(), 0)
            self.assertAlmostEqual(initial.width() / initial.height(), 4 / 5, delta=0.04)
            pixmap = card.thumbnail.pixmap()
            self.assertIsNotNone(pixmap)
            self.assertAlmostEqual((pixmap.width() / pixmap.devicePixelRatioF()) /
                                   (pixmap.height() / pixmap.devicePixelRatioF()),
                                   4 / 5, delta=0.04)

            # Ignore the rounded edge and sample the image canvas for uncovered pixels.
            rendered = pixmap.toImage()
            dpr = pixmap.devicePixelRatioF()
            left, top = round(5 * dpr), round(5 * dpr)
            right = rendered.width() - left
            bottom = rendered.height() - top
            covered = 0
            total = 0
            for y in range(top, bottom, max(1, round(8 * dpr))):
                for x in range(left, right, max(1, round(8 * dpr))):
                    total += 1
                    if rendered.pixelColor(x, y).alpha() > 240:
                        covered += 1
            self.assertGreaterEqual(covered / max(total, 1), 0.99)

            old_size = card.thumbnail.size()
            card.resize(420, 760)
            self.app.processEvents()
            wait_until(lambda: not card.thumbnail._resize_timer.isActive())
            self.assertGreater(card.thumbnail.width(), old_size.width())
            self.assertFalse(card.thumbnail.pixmap().isNull())

            failed = EcommerceResultCard(dict(base, item_id='failed-item', status='failed',
                                              error='失败'))
            failed.resize(420, 760)
            failed.show()
            self.app.processEvents()
            self.assertEqual(card.thumbnail.size(), failed.thumbnail.size())
            for scale in (1.0, 1.25, 1.5, 1.75):
                with self.subTest(scale=scale):
                    apply_interface_scale(self.app, FakeScreen(), '1.0')
                    self.app._interface_scaler.apply(scale)
                    for entry in (card, failed):
                        entry.resize(round(280 * scale), round(760 * scale))
                    self.app.processEvents()
                    wait_until(lambda: all(not entry.thumbnail._resize_timer.isActive()
                                           for entry in (card, failed)))
                    self.assertEqual(card.thumbnail.size(), failed.thumbnail.size())
                    self.assertAlmostEqual(card.thumbnail.width() / card.thumbnail.height(),
                                           4 / 5, delta=0.04)
                    pixmap = card.thumbnail.pixmap()
                    self.assertAlmostEqual(pixmap.width() / pixmap.devicePixelRatioF(),
                                           card.thumbnail.width(), delta=1)
                    rendered = pixmap.toImage()
                    # The vertical centre crosses both sides of a portrait cover.
                    for x in (round(rendered.width() * part) for part in (0.02, 0.5, 0.98)):
                        self.assertGreater(rendered.pixelColor(x, rendered.height() // 2).alpha(), 240)
        finally:
            if failed is not None:
                failed.deleteLater()
            if card is not None:
                card.deleteLater()
            self.app.processEvents()
            image_path.unlink(missing_ok=True)

    def test_multi_card_panel_keeps_failed_action_clear_after_initial_layout_and_resize(self):
        panel = EcommerceResultPanel()
        panel.resize(980, 1800)
        for index in range(6):
            panel.update_item(dict(
                item_id=f'failed-{index}', kind='carousel', seq=index + 1,
                status='failed', error='生成失败', warnings=[],
            ))
        panel.show()
        try:
            for width in (980, 620, 1080, 760):
                panel.resize(width, 1800)
                self.app.processEvents()
                QTest.qWait(20)

                button_rect = panel.regenerate_failed_button.geometry()
                self.assertTrue(panel.regenerate_failed_button.isEnabled())
                self.assertEqual(len(panel.cards), 6)
                for card in panel.ordered_cards():
                    self.assertGreaterEqual(card.geometry().top(), button_rect.bottom())
                    self.assertFalse(card.geometry().intersects(button_rect))
                    expected_height = min(
                        px(360), max(px(180), round(card.thumbnail.width() * 5 / 4)))
                    self.assertEqual(card.thumbnail.height(), expected_height)
        finally:
            panel.close()
            panel.deleteLater()
            self.app.processEvents()

    def test_legacy_error_details_are_summarized_with_original_in_tooltip(self):
        records = (
            ('{"error":{"type":"invalid_request_error","message":"Unknown parameter"}}',
             'HTTP 400'),
            ('AI 分析失败：{"error":{"code":503,"message":"Service unavailable"}}（旧诊断）',
             '上游服务暂时不可用'),
            ("HTTPSConnectionPool(host='api.example.test', port=443): SSLError: UNEXPECTED_EOF_WHILE_READING",
             'SSL'),
        )
        for raw_error, expected_fragment in records:
            with self.subTest(error=expected_fragment):
                card = EcommerceResultCard(dict(item_id='legacy-error', kind='carousel', seq=1,
                                                status='failed', error=raw_error))
                self.assertIn(expected_fragment, card.error_label.text())
                self.assertNotIn('HTTPSConnectionPool', card.error_label.text())
                self.assertNotIn('{"error"', card.error_label.text())
                self.assertEqual(card.error_label.toolTip(), raw_error)
                card.deleteLater()
                self.app.processEvents()


class EcommercePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        set_language('zh-CN')
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.config = ConfigManager(self.root / 'config.json')
        self.config.config['api'].update(base_url='offline-fixture', api_key=TEST_KEY)
        self.config.config['paths']['output'] = str(self.root / 'output')
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('禁止真实联网'))
        self.network_mock = self.network.start()
        self.notices = patch('ui.pages.ecommerce_page.InfoBar.error').start()
        self.warnings = patch('ui.pages.ecommerce_page.InfoBar.warning').start()
        self.page = EcommercePage(self.config)
        self.analysis = Mock(side_effect=fake_analysis)
        self.image = Mock(side_effect=fake_image)
        self.factory_threads = []
        self.factory = Mock(side_effect=self.make_manager)
        self.page._make_manager = self.factory

    def tearDown(self):
        if self.page.busy:
            self.page.cancel_generation()
            self.wait_until(lambda: not self.page.busy)
        self.page.close()
        self.page.deleteLater()
        QTest.qWait(20)
        self.network_mock.assert_not_called()
        patch.stopall()
        self.temp.cleanup()

    def make_manager(self):
        self.factory_threads.append(QThread.currentThread())
        manager = EcommerceImageManager.build_from_config(self.config)
        manager.analysis_func = self.analysis
        manager.image_func = self.image
        return manager

    def wait_until(self, predicate, timeout=8000):
        deadline = time.monotonic() + timeout / 1000
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate(), '等待电商图后台线程超时')

    def fill_valid(self):
        self.page.platform_checkboxes['douyin'].setChecked(True)
        self.page.product_name.setText('陶瓷茶杯')
        self.page.product_info.setPlainText('耐热陶瓷，便携易清洁')
        self.page.carousel_count.setValue(2)
        self.page.detail_count.setValue(1)

    def assert_blocked(self):
        self.page.start_generation()
        self.assertIsNone(self.page._worker)
        self.factory.assert_not_called()
        self.analysis.assert_not_called()
        self.image.assert_not_called()
        self.notices.assert_called()
        self.assertFalse((self.root / 'output').exists())

    def run_to_idle(self):
        idle = QSignalSpy(self.page.idle)
        self.page.start_generation()
        self.assertIsNotNone(self.page._worker)
        self.last_worker = self.page._worker
        result = QSignalSpy(self.page._worker.finished)
        self.wait_until(lambda: len(idle) == 1)
        self.assertEqual(len(result), 1)
        return list(result[0])

    def ui_text(self):
        return '\n'.join([self.page.log_box.toPlainText()] +
                         [label.text() for label in self.page.findChildren(QLabel)])

    def run_regenerate_to_idle(self, action):
        idle = QSignalSpy(self.page.idle)
        action()
        self.assertIsNotNone(self.page._worker)
        self.last_worker = self.page._worker
        self.wait_until(lambda: len(idle) == 1)

    def assert_results_match_store(self):
        records = self.config.ecommerce_store().list_items(self.page._group['group_id'])
        self.assertTrue(records)
        self.assertTrue(all(record['status'] not in ('pending', 'running') for record in records))
        self.assertEqual(len(self.page.result_panel.cards), len(records))
        for record in records:
            card = self.page.result_panel.cards[record['item_id']]
            for field, value in record.items():
                self.assertEqual(card.record[field], value, (record['item_id'], field))
            self.assertNotIn('生成中', card.status_label.text())

    def seed_history(self, group_id='history-fixture', status='partial', *,
                     states=('succeeded', 'failed', 'pending'), name='历史茶杯', updated=1000):
        store = self.config.ecommerce_store()
        output = self.root / 'saved-output' / group_id
        output.mkdir(parents=True, exist_ok=True)
        items = []
        for seq, state in enumerate(states, 1):
            file = output / f'轮{seq}.png'
            if state == 'succeeded':
                file.write_bytes(PNG)
            items.append(dict(group_id=group_id, kind='carousel', seq=seq, title=f'展示{seq}',
                              prompt=f'Saved product carousel {seq}', status=state,
                              reference_paths=[], ratio='1:1', tier='1K',
                              model=self.config.config['ecommerce']['image_model'],
                              file_path=str(file) if state == 'succeeded' else '',
                              size='1024x1024' if state == 'succeeded' else ''))
        totals = {state: states.count(state) for state in ('pending', 'running', 'succeeded', 'failed', 'skipped')}
        store.upsert_group(dict(group_id=group_id, created_at=updated - 10, updated_at=updated,
                                status=status, product_name=name, platforms=[get_platform('douyin')],
                                output_dir=str(output), totals=dict(totals, total=len(states)),
                                config=dict(self.config.config['ecommerce'], carousel_count=len(states),
                                            detail_count=0, product_info='保存的资料',
                                            carousel_ratio='1:1', detail_ratio='3:4', tier='1K')))
        store.upsert_items_many(items)
        return store.get_group(group_id), store.list_items(group_id)

    def test_history_lists_latest_groups_names_platforms_stages_times_and_totals(self):
        from datetime import datetime
        records = []
        for index, status in enumerate(('succeeded', 'partial', 'failed', 'analyzing')):
            group, _items = self.seed_history(f'history-{index}', status, updated=1000 + index,
                                             name='' if index == 3 else f'商品{index}',
                                             states=('succeeded',) if status == 'succeeded' else ('failed',))
            records.append(group)
        self.page.history_panel.refresh_button.click()
        rows = list(self.page.history_panel.rows.values())
        self.assertEqual([row.group['group_id'] for row in rows], [r['group_id'] for r in records[::-1]])
        self.assertEqual([row.status_label.text() for row in rows], ['AI分析中', '失败', '部分成功', '已完成'])
        self.assertEqual(rows[0].name_label.text(), records[-1]['group_id'][:8])
        self.assertEqual(rows[1].name_label.text(), '商品2')
        self.assertEqual(rows[0].platform_label.text(), get_platform('douyin')['name'])
        self.assertEqual(rows[0].time_label.text(), datetime.fromtimestamp(993).strftime('%Y-%m-%d %H:%M'))
        self.assertEqual(rows[-1].summary_label.text(), '成功 1 / 失败 0 / 跳过 0 / 总数 1')
        self.assertIn(materials.palette()['error'], rows[1].status_label.styleSheet())
        self.assertIn(materials.palette()['warning'], rows[2].status_label.styleSheet())
        self.assertTrue(all(row.resume_button.isEnabled() for row in rows[:-1]))
        self.assertFalse(rows[-1].resume_button.isEnabled())
        self.factory.assert_not_called()

    def test_interrupted_history_group_shows_reconciliation_reason(self):
        group, _items = self.seed_history('interrupted-history', 'failed', states=('failed',))
        group['error'] = '上次运行未正常结束（程序中断）'
        self.config.ecommerce_store().upsert_group(group)

        self.page._refresh_history()

        status = self.page.history_panel.rows[group['group_id']].status_label.text()
        self.assertEqual(status, tr('上次运行未正常结束（程序中断）'))
        self.assertNotEqual(status, tr('AI分析中'))

    def test_history_item_progress_summarizes_raw_errors_and_preserves_diagnostics(self):
        cases = (
            ('{"error":{"code":503,"message":"service unavailable","type":"upstream_error"}}',
             '上游服务暂时不可用'),
            ('HTTPSConnectionPool(host=probe.invalid): SSLError: UNEXPECTED_EOF',
             '与中转站的 SSL 连接被中断'),
            ('上次运行未正常结束（程序中断）', '上次运行未正常结束（程序中断）'),
        )
        for index, (raw, expected) in enumerate(cases):
            with self.subTest(error=raw):
                group, items = self.seed_history(f'raw-error-{index}', 'failed', states=('failed',))
                items[0]['error'] = raw
                store = self.config.ecommerce_store()
                store.upsert_items_many(items)
                self.page._on_group(group)
                label = self.page.item_labels[('carousel', 1)]
                self.assertIn(expected, label.text())
                self.assertNotIn('HTTPSConnectionPool', label.text())
                self.assertNotIn('{"error"', label.text())
                self.assertEqual(label.toolTip(), raw)
                self.assertEqual(store.list_items(group['group_id'])[0]['error'], raw)

    def test_group_error_is_logged_once_per_worker_run(self):
        record = dict(group_id='dedup-group', status='failed', updated_at=10,
                      totals={'succeeded': 0, 'failed': 1, 'skipped': 0},
                      error='同一组错误')
        self.page._on_group(record)
        self.page._on_group(dict(record))
        self.page._on_result('dedup-group', '同一组错误')
        self.page._thread_stopped()
        self.assertEqual(sum(message == '同一组错误' for message, _level in self.page._logs), 1)

        self.page._clear_results()
        self.page._on_group(dict(record, updated_at=11))
        self.assertEqual(sum(message == '同一组错误' for message, _level in self.page._logs), 2)

    def test_history_refreshes_on_page_entry_and_limits_to_fifty(self):
        self.assertFalse(self.page.history_panel.empty_label.isHidden())
        store = self.config.ecommerce_store()
        for index in range(52):
            store.upsert_group(dict(group_id=f'group-{index:02d}', status='succeeded', updated_at=index))
        self.page.show()
        self.assertEqual(len(self.page.history_panel.rows), 50)
        self.assertEqual(next(iter(self.page.history_panel.rows)), 'group-51')
        self.assertNotIn('group-00', self.page.history_panel.rows)
        self.assertTrue(self.page.history_panel.empty_label.isHidden())
        self.factory.assert_not_called()

    def test_history_view_switches_groups_without_factory_and_allows_regeneration(self):
        first, _ = self.seed_history('first-history', states=('succeeded', 'failed'))
        second, _ = self.seed_history('second-history', 'succeeded', states=('succeeded',))
        self.page._refresh_history()
        rows = self.page.history_panel.rows
        rows[first['group_id']].view_button.click()
        self.assertEqual(len(self.page.result_panel.cards), 2)
        rows[second['group_id']].view_button.click()
        self.assertEqual(self.page._group['group_id'], second['group_id'])
        self.assertEqual(len(self.page.result_panel.cards), 1)
        self.assertEqual(len(self.page.item_labels), 1)
        self.assertEqual(self.page.stage_label.text(), '已完成')
        self.factory.assert_not_called()
        self.image.assert_not_called()
        self.analysis.assert_not_called()
        self.run_regenerate_to_idle(self.page.result_panel.ordered_cards()[0].regenerate_button.click)
        self.assertEqual(self.image.call_count, 1)
        self.assertEqual(self.image.call_args.args[3], 'Saved product carousel 1')
        self.analysis.assert_not_called()

    def test_history_resume_after_reopening_store_only_requests_unfinished_or_missing_files(self):
        group, items = self.seed_history(states=('succeeded', 'pending', 'running', 'failed', 'succeeded'))
        Path(items[-1]['file_path']).unlink()
        # A fresh store has no session cache; recovery must use persisted prompts.
        self.config._ecommerce_store = EcommerceImageStore(self.config.ecommerce_store().path)
        self.page._refresh_history()
        self.run_regenerate_to_idle(self.page.history_panel.rows[group['group_id']].resume_button.click)
        prompts = sorted(call.args[3] for call in self.image.call_args_list)
        self.assertEqual(prompts, [f'Saved product carousel {i}' for i in range(2, 6)])
        self.analysis.assert_not_called()
        self.assertEqual(Path(items[0]['file_path']).read_bytes(), PNG)
        self.assertEqual(self.page.stage_label.text(), '已完成')
        row = self.page.history_panel.rows[group['group_id']]
        self.assertEqual(row.status_label.text(), '已完成')
        self.assertEqual(row.summary_label.text(), '成功 5 / 失败 0 / 跳过 0 / 总数 5')
        self.assertFalse(row.resume_button.isEnabled())
        self.assertTrue(all(card.record['status'] == 'succeeded' for card in self.page.result_panel.ordered_cards()))
        self.assertNotEqual(self.factory_threads[0], self.app.thread())
        self.assertNotIn(TEST_KEY, self.ui_text())

    def test_history_resume_leaves_skipped_items_for_explicit_regeneration(self):
        group, _ = self.seed_history(states=('succeeded', 'failed', 'skipped'))
        self.page._refresh_history()
        self.run_regenerate_to_idle(lambda: self.page._run_resume(group['group_id']))
        self.assertEqual(self.image.call_count, 1)
        self.assertEqual(self.image.call_args.args[3], 'Saved product carousel 2')
        cards = self.page.result_panel.ordered_cards()
        self.assertEqual(cards[-1].record['status'], 'skipped')
        self.run_regenerate_to_idle(cards[-1].regenerate_button.click)
        self.assertEqual(self.image.call_count, 2)
        self.assertEqual(self.page.stage_label.text(), '已完成')
        self.analysis.assert_not_called()

    def test_history_resume_locks_actions_updates_live_rows_and_supports_cancellation(self):
        group, _ = self.seed_history('resume-running', 'generating', states=('running', 'pending', 'failed'))
        self.page._refresh_history()
        self.config.config['ecommerce']['max_concurrency'] = 1
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        callback_threads = []
        original = self.page._on_item

        def observe(record):
            callback_threads.append(QThread.currentThread())
            original(record)

        def blocking_image(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('未释放断点恢复任务')
            return fake_image(*args, **kwargs)

        self.page._on_item = observe
        self.image.side_effect = blocking_image
        self.page._run_resume(group['group_id'])
        self.wait_until(entered.is_set)
        row = self.page.history_panel.rows[group['group_id']]
        self.assertEqual(row.status_label.text(), '图片生成中')
        self.assertFalse(row.view_button.isEnabled())
        self.assertFalse(row.resume_button.isEnabled())
        self.assertFalse(row.delete_button.isEnabled())
        self.assertTrue(all(not c.isEnabled() for c in self.page._form_cards))
        self.assertTrue(self.page.cancel_button.isEnabled())
        self.assertEqual(self.page._worker.operation, 'resume')
        self.page._view_history_group('missing')  # Busy navigation is ignored.
        self.page._delete_history_group(group['group_id'])
        self.page.cancel_button.click()
        release.set()
        self.wait_until(lambda: not self.page.busy)
        self.assertEqual(self.image.call_count, 1)
        self.assertEqual(row.status_label.text(), '部分成功')
        self.assertEqual(row.summary_label.text(), '成功 1 / 失败 0 / 跳过 2 / 总数 3')
        self.assertTrue(row.view_button.isEnabled())
        self.assertTrue(all(t == self.app.thread() for t in callback_threads))
        self.assertNotEqual(self.factory_threads[0], self.app.thread())
        self.analysis.assert_not_called()

    def test_history_preset_resume_cancellation_makes_no_image_requests(self):
        group, _ = self.seed_history(status='failed', states=('failed', 'pending'))
        self.page.cancel_event.set()
        self.run_regenerate_to_idle(lambda: self.page._run_resume(group['group_id']))
        self.assertEqual(self.page.stage_label.text(), '已取消')
        self.assertFalse(self.page.history_panel.rows[group['group_id']].resume_button.isEnabled())
        self.image.assert_not_called()
        self.analysis.assert_not_called()

    def test_history_no_analysis_results_reports_error_without_reanalyzing(self):
        group, _ = self.seed_history(status='analyzing', states=())
        self.run_regenerate_to_idle(lambda: self.page._run_resume(group['group_id']))
        self.assertIn('尚无分析结果', self.page.log_box.toPlainText())
        self.assertTrue(self.page.start_button.isEnabled())
        self.image.assert_not_called()
        self.analysis.assert_not_called()

    def test_history_missing_groups_and_completed_resume_are_rejected_without_worker(self):
        group, _ = self.seed_history(status='succeeded', states=('succeeded',))
        self.page._run_resume(group['group_id'])
        self.page._run_resume('missing')
        self.page._view_history_group('missing')
        self.factory.assert_not_called()
        self.assertEqual(self.notices.call_count, 3)

    def test_history_delete_confirmation_cascades_only_records_and_keeps_disk_files(self):
        group, items = self.seed_history(name='删除测试 ' + TEST_KEY)
        manifest = Path(group['output_dir']) / 'manifest.json'
        manifest.write_text('{}', encoding='utf-8')
        self.page._refresh_history()
        self.page._view_history_group(group['group_id'])
        for accept in (False, True):
            observations = []

            def choose():
                dialog = QApplication.activeModalWidget()
                observations.append('\n'.join(label.text() for label in dialog.findChildren(QLabel)))
                name = 'confirmDeleteRecord' if accept else 'keepRecord'
                from qfluentwidgets import PushButton
                dialog.findChild(PushButton, name).click()

            QTimer.singleShot(0, choose)
            self.page.history_panel.rows[group['group_id']].delete_button.click()
            self.assertIn('仅删除记录，不删除已生成文件', observations[0])
            self.assertNotIn(TEST_KEY, observations[0])
            store = self.config.ecommerce_store()
            if accept:
                self.assertIsNone(store.get_group(group['group_id']))
                self.assertEqual(store.list_items(group['group_id']), [])
                self.assertNotIn(group['group_id'], self.page.history_panel.rows)
                self.assertFalse(self.page.result_panel.cards)
            else:
                self.assertIsNotNone(store.get_group(group['group_id']))
                self.assertEqual(len(store.list_items(group['group_id'])), 3)
            self.assertEqual(Path(items[0]['file_path']).read_bytes(), PNG)
            self.assertTrue(manifest.is_file())
        self.factory.assert_not_called()

    def test_history_folder_opens_existing_directory_and_reports_missing_directory(self):
        group, _ = self.seed_history()
        self.page._refresh_history()
        with patch('ui.pages.ecommerce_page.open_local') as opener:
            self.page.history_panel.rows[group['group_id']].open_button.click()
            opener.assert_called_once_with(group['output_dir'], self.page._append_log, folder=True)
            self.config.ecommerce_store().upsert_group(dict(group, output_dir=str(self.root / 'absent')))
            opener.reset_mock()
            self.page._open_history_folder(group['group_id'])
            opener.assert_not_called()
        self.assertIn('输出文件夹不存在', self.notices.call_args.args[1])

    def test_history_database_corruption_preserves_current_session_results(self):
        corrupt = self.root / 'corrupt.sqlite3'
        corrupt.write_bytes(b'invalid sqlite database')
        self.config._ecommerce_store = EcommerceImageStore(corrupt)
        self.page._refresh_history()
        self.assertFalse(self.config.ecommerce_store().persistence_available)
        self.assertEqual(self.page.history_panel.notice_label.text(), '数据库不可用，仅展示本次会话结果')
        self.fill_valid()
        group_id, _ = self.run_to_idle()
        self.assertEqual(self.page.stage_label.text(), '已完成')
        row = self.page.history_panel.rows[group_id]
        row.view_button.click()
        self.assertEqual(len(self.page.result_panel.cards), 3)
        self.assertEqual(self.factory.call_count, 1)
        self.assertNotIn(TEST_KEY, self.ui_text())

    def test_new_group_completion_refreshes_history_and_moves_it_to_top(self):
        self.seed_history('older-group', 'succeeded', states=('succeeded',))
        self.fill_valid()
        group_id, _ = self.run_to_idle()
        self.assertEqual(next(iter(self.page.history_panel.rows)), group_id)
        self.assertEqual(self.page.history_panel.rows[group_id].status_label.text(), '已完成')
        self.assertEqual(len(self.page.history_panel.rows), 2)

    def test_form_uses_all_platforms_and_catalog_options_and_config_defaults(self):
        page = self.page
        self.assertEqual(page.objectName(), 'ecommercePage')
        self.assertEqual(len(page.platform_checkboxes), 25)
        self.assertEqual(set(page.platform_groups), {'国内', '海外'})
        for group, records in (('国内', domestic()), ('海外', overseas())):
            for record in records:
                checkbox = page.platform_checkboxes[record['id']]
                self.assertEqual(checkbox.text(), record['name'])
                self.assertTrue(page.platform_groups[group].isAncestorOf(checkbox))
        self.assertEqual([page.language.itemData(i) for i in range(page.language.count())], list(LANGUAGE_TEXT))
        self.assertEqual([page.language.itemText(i) for i in range(page.language.count())], list(LANGUAGE_TEXT.values()))
        for combo in (page.carousel_ratio, page.detail_ratio):
            self.assertEqual([combo.itemText(i) for i in range(combo.count())], RATIOS)
        self.assertEqual([page.tier.itemText(i) for i in range(page.tier.count())], list(TIER_SHORT_EDGE))
        defaults = self.config.config['ecommerce']
        self.assertEqual(page.language.currentData(), defaults['language'])
        self.assertEqual(page.carousel_count.value(), defaults['default_carousel_count'])
        self.assertEqual(page.detail_count.value(), defaults['default_detail_count'])
        self.assertEqual(page.carousel_ratio.currentText(), defaults['default_carousel_ratio'])
        self.assertEqual(page.detail_ratio.currentText(), defaults['default_detail_ratio'])
        self.assertEqual(page.tier.currentText(), defaults['default_tier'])
        self.assertEqual((page.carousel_count.minimum(), page.carousel_count.maximum()), (1, 12))
        self.assertEqual((page.detail_count.minimum(), page.detail_count.maximum()), (0, 12))

    def test_sixth_platform_cannot_be_checked(self):
        keys = list(self.page.platform_checkboxes)
        for key in keys[:MAX_PLATFORMS + 1]:
            self.page.platform_checkboxes[key].setChecked(True)
        self.assertEqual(len(self.page.selected_platforms()), MAX_PLATFORMS)
        self.assertFalse(self.page.platform_checkboxes[keys[MAX_PLATFORMS]].isChecked())
        self.warnings.assert_called_once()

    def test_background_conflict_is_red_and_blocks_start(self):
        self.fill_valid()
        self.page.platform_checkboxes['amazon'].setChecked(True)
        self.page.platform_checkboxes['xiaohongshu'].setChecked(True)
        label = self.page.background_conflict
        self.assertIn('白底', label.text())
        self.assertIn(materials.palette()['error'], label.styleSheet())
        self.assertFalse(label.isHidden())
        self.assertFalse(self.page.start_button.isEnabled())
        self.assert_blocked()

    def test_ratio_warning_allows_explicit_ratio(self):
        self.fill_valid()
        records = domestic() + overseas()
        base = next(p for p in records if p['id'] == 'douyin')
        other = next(p for p in records if p['bgRule'] != 'white' and
                     p['defaultCarouselRatio'] != base['defaultCarouselRatio'])
        self.page.platform_checkboxes[other['id']].setChecked(True)
        self.assertTrue(self.page.ratio_conflict.text())
        self.assertIn(materials.palette()['warning'], self.page.ratio_conflict.styleSheet())
        self.assertTrue(self.page.start_button.isEnabled())
        self.assertEqual(self.page._arguments()['carousel_ratio'], self.page.carousel_ratio.currentText())

    def test_no_platform_blocks_before_worker_creation(self):
        self.assert_blocked()

    def test_validation_also_rejects_injected_excess_platform_selection(self):
        self.fill_valid()
        for checkbox in list(self.page.platform_checkboxes.values())[:MAX_PLATFORMS + 1]:
            checkbox.blockSignals(True)
            checkbox.setChecked(True)
            checkbox.blockSignals(False)
        self.assert_blocked()

    def test_empty_product_name_blocks_before_worker_creation(self):
        self.fill_valid()
        self.page.product_name.setText('  ')
        self.assert_blocked()

    def test_empty_product_information_blocks_before_worker_creation(self):
        self.fill_valid()
        self.page.product_info.setPlainText('  \n ')
        self.assert_blocked()

    def test_empty_api_key_or_base_url_blocks_before_worker_creation(self):
        self.fill_valid()
        for key in ('base_url', 'api_key'):
            original = self.config.config['api'][key]
            self.config.config['api'][key] = ' '
            self.assert_blocked()
            self.assertIn('文本模型', self.notices.call_args.args[1])
            self.config.config['api'][key] = original

    def test_zero_total_blocks_before_worker_creation(self):
        self.fill_valid()
        self.page.carousel_count.setMinimum(0)
        self.page.carousel_count.setValue(0)
        self.page.detail_count.setValue(0)
        self.assert_blocked()

    def test_too_many_reference_images_block_before_worker_creation(self):
        self.fill_valid()
        self.page.product_image_paths = [str(self.root / f'{i}.png') for i in range(7)]
        self.assert_blocked()
        self.page.product_image_paths = []
        self.page.style_image_paths = [str(self.root / f'style{i}.png') for i in range(2)]
        self.assert_blocked()

    def test_success_pipeline_updates_main_thread_progress_files_and_summary(self):
        self.fill_valid()
        callback_threads = []
        original = self.page._on_item

        def observe(record):
            callback_threads.append(QThread.currentThread())
            original(record)

        self.page._on_item = observe
        group_id, error = self.run_to_idle()
        self.assertTrue(group_id)
        self.assertEqual(error, '')
        self.assertEqual(self.page.stage_label.text(), '已完成')
        self.assertEqual(self.page.summary_label.text(), '成功 3 / 失败 0 / 跳过 0')
        self.assertEqual(len(self.page.item_labels), 3)
        for label in self.page.item_labels.values():
            self.assertIn('成功', label.text())
            self.assertIn('1024x1024', label.text())
            self.assertIn('离线示例', label.text())
        self.assertTrue(self.page.open_output_button.isEnabled())
        self.assertTrue(self.page.start_button.isEnabled())
        self.assertFalse(self.page.cancel_button.isEnabled())
        self.assertTrue(all(c.isEnabled() for c in self.page._form_cards))
        self.assertTrue(all(t == self.app.thread() for t in callback_threads))
        self.assertNotEqual(self.factory_threads[0], self.app.thread())
        output = Path(self.page._group['output_dir'])
        for name in ('轮1.png', '轮2.png', '详1.png'):
            self.assertEqual((output / name).read_bytes(), PNG)
        manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest, self.config.ecommerce_store().get_group(group_id)['manifest'])
        with patch('ui.pages.ecommerce_page.open_local') as opener:
            self.page.open_output_button.click()
        self.assertEqual(opener.call_args.args[0], str(output))
        self.assertTrue(opener.call_args.kwargs['folder'])
        self.assertNotIn(TEST_KEY, self.ui_text())
        self.assertNotIn(self.last_worker, _LIVE_WORKERS)

    def test_reference_choosers_preserve_order_remove_clear_and_replace(self):
        paths = [str(self.root / name) for name in ('产品一.png', '产品二.jpg')]
        self.page._refresh_references()
        self.assertEqual(self.page.product_count_label.text(), '产品图 0/6')
        self.assertEqual(self.page.product_list.count(), 0)
        self.assertEqual(self.page.style_preview.count(), 0)
        self.assertEqual(self.page.style_label.text(), '未选择风格图')
        with patch('ui.pages.ecommerce_page.QFileDialog.getOpenFileNames', return_value=(paths, '')):
            self.page.add_products_button.click()
        self.assertEqual(self.page.product_image_paths, paths)
        self.assertEqual(self.page.product_list.item(0).text(), '产品一.png')
        self.assertEqual(self.page.product_count_label.text(), '产品图 2/6')
        self.assertEqual([self.page.product_list.item(i).widget.path
                          for i in range(self.page.product_list.count())], paths)
        self.page.product_list.setCurrentRow(0)
        self.assertTrue(self.page.product_list.item(0).widget.selected)
        self.assertFalse(self.page.product_list.item(1).widget.selected)
        self.page.remove_product_button.click()
        self.assertEqual(self.page.product_image_paths, paths[1:])
        self.assertEqual(self.page.product_count_label.text(), '产品图 1/6')
        self.assertEqual(self.page.product_list.count(), 1)
        self.assertEqual(self.page.product_list.item(0).widget.path, paths[1])
        with patch('ui.pages.ecommerce_page.QFileDialog.getOpenFileName', return_value=(paths[0], '')):
            self.page.choose_style_button.click()
        self.assertEqual(self.page.style_preview.count(), 1)
        self.assertEqual(self.page.style_preview.item(0).widget.path, paths[0])
        self.assertEqual(self.page.style_label.text(), '产品一.png')
        with patch('ui.pages.ecommerce_page.QFileDialog.getOpenFileName', return_value=(paths[1], '')):
            self.page.choose_style_button.click()
        self.assertEqual(self.page.style_image_paths, paths[1:])
        self.assertEqual(self.page.style_preview.count(), 1)
        self.assertEqual(self.page.style_preview.item(0).widget.path, paths[1])
        self.assertEqual(self.page.style_label.text(), '产品二.jpg')
        self.page.clear_products_button.click()
        self.assertEqual(self.page.product_count_label.text(), '产品图 0/6')
        self.assertEqual(self.page.product_list.count(), 0)
        self.page.clear_style_button.click()
        self.assertEqual(self.page.product_image_paths, [])
        self.assertEqual(self.page.style_image_paths, [])
        self.assertEqual(self.page.style_preview.count(), 0)
        self.assertEqual(self.page.style_label.text(), '未选择风格图')

    def test_thumbnail_close_button_removes_its_product_and_missing_style_is_safe(self):
        paths = [str(self.root / name) for name in ('first.png', 'missing.png', 'third.png')]
        self.page.product_image_paths = paths.copy()
        self.page._refresh_references()
        self.page.product_list.item(1).widget.close_button.click()
        self.assertEqual(self.page.product_image_paths, [paths[0], paths[2]])
        self.assertEqual([self.page.product_list.item(i).widget.path
                          for i in range(self.page.product_list.count())], [paths[0], paths[2]])
        self.page.style_image_paths = [str(self.root / 'absent-style.png')]
        self.page._refresh_references()
        self.assertEqual(self.page.style_preview.item(0).widget.preview.text(), '无法预览')
        self.assertEqual(self.page.style_preview.item(0).toolTip(), str(self.root / 'absent-style.png'))

    def test_arguments_preserve_product_order_and_submit_style_last(self):
        self.fill_valid()
        products = [str(self.root / name) for name in ('second.png', 'first.png')]
        style = str(self.root / 'style.png')
        self.page.product_image_paths = products.copy()
        self.page.style_image_paths = [style]
        self.page._refresh_references()
        arguments = self.page._arguments()
        self.assertEqual(arguments['product_image_paths'], [str(Path(path).resolve()) for path in products])
        self.assertEqual(arguments['style_image_path'], str(Path(style).resolve()))

    def test_product_picker_rejects_seventh_image_without_changing_order(self):
        paths = [str(self.root / f'{i}.png') for i in range(6)]
        self.page.product_image_paths = paths.copy()
        with patch('ui.pages.ecommerce_page.QFileDialog.getOpenFileNames',
                   return_value=([str(self.root / 'extra.png')], '')):
            self.page.add_products_button.click()
        self.assertEqual(self.page.product_image_paths, paths)
        self.notices.assert_called_once()

    def test_style_references_flow_through_factory_in_product_first_order(self):
        self.fill_valid()
        products = [str(self.root / name) for name in ('first.png', 'second.jpg')]
        style = str(self.root / 'style.webp')
        self.page.product_image_paths = products
        self.page.style_image_paths = [style]
        self.run_to_idle()
        for call in self.image.call_args_list:
            self.assertEqual(call.kwargs['reference_paths'], products + [style])
            self.assertIn('Reference image order', call.args[3])

    def test_preset_cancellation_skips_every_image_and_finishes_cancelled(self):
        self.fill_valid()
        token = self.page.cancel_event
        token.set()
        self.run_to_idle()
        self.image.assert_not_called()
        self.assertEqual(self.page.stage_label.text(), '已取消')
        self.assertEqual(self.page.summary_label.text(), '成功 0 / 失败 0 / 跳过 3')
        self.assertTrue(all('跳过' in label.text() for label in self.page.item_labels.values()))
        self.assertIsNot(self.page.cancel_event, token)
        self.assertFalse(self.page.cancel_event.is_set())

    def test_cancel_button_locks_form_and_skips_queued_images_without_termination(self):
        self.fill_valid()
        self.config.config['ecommerce']['max_concurrency'] = 1
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def blocking_image(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('测试未释放出图任务')
            return fake_image(*args, **kwargs)

        self.image.side_effect = blocking_image
        self.page.start_generation()
        self.wait_until(entered.is_set)
        self.assertFalse(self.page.start_button.isEnabled())
        self.assertTrue(all(not c.isEnabled() for c in self.page._form_cards))
        self.assertIn(self.page._worker, _LIVE_WORKERS)
        self.page.cancel_button.click()
        self.assertTrue(self.page.cancel_event.is_set())
        self.assertFalse(self.page.cancel_button.isEnabled())
        release.set()
        self.wait_until(lambda: not self.page.busy)
        self.assertEqual(self.image.call_count, 1)
        self.assertEqual(self.page.stage_label.text(), '部分成功')
        self.assertEqual(self.page.summary_label.text(), '成功 1 / 失败 0 / 跳过 2')
        self.assert_results_match_store()

    def test_analysis_error_is_redacted_in_notice_and_log_and_restores_form(self):
        self.fill_valid()
        self.analysis.side_effect = RuntimeError('无文本权限 ' + TEST_KEY)
        group_id, error = self.run_to_idle()
        self.assertEqual(group_id, self.page._group['group_id'])
        self.assertEqual(self.config.ecommerce_store().get_group(group_id)['status'], 'failed')
        self.assertEqual(error, 'AI 分析失败：分析处理异常，请检查配置或诊断日志')
        self.assertEqual(self.page.stage_label.text(), '失败')
        self.assertIn(error, self.ui_text())
        self.notices.assert_called_once()
        self.assertIn(error, str(self.notices.call_args))
        self.assertNotIn(TEST_KEY, error + self.ui_text() + str(self.notices.call_args))
        self.assertTrue(self.page.start_button.isEnabled())
        self.image.assert_not_called()

    def test_partial_image_failure_and_warning_logs_are_redacted(self):
        self.fill_valid()

        def sometimes_fails(*args, **kwargs):
            if args[3].endswith('carousel 1'):
                raise RuntimeError('出图失败 ' + TEST_KEY)
            result = fake_image(*args, **kwargs)
            result.warnings.append('降档提示 ' + TEST_KEY)
            return result

        self.image.side_effect = sometimes_fails
        self.run_to_idle()
        self.assertEqual(self.page.stage_label.text(), '部分成功')
        self.assertEqual(self.page.summary_label.text(), '成功 2 / 失败 1 / 跳过 0')
        self.assert_results_match_store()
        self.assertTrue(self.page.result_panel.regenerate_failed_button.isEnabled())
        self.assertNotIn(TEST_KEY, self.ui_text())
        self.page._append_log('测试警告 ' + TEST_KEY, 'warning')
        self.assertIn('[warning]', self.page.log_box.toPlainText())
        self.assertNotIn(TEST_KEY, self.ui_text())
        self.page._refresh_colors()
        self.assertNotIn(TEST_KEY, self.ui_text())

    def test_factory_failure_is_redacted_and_thread_released(self):
        self.fill_valid()
        self.factory.side_effect = RuntimeError(TEST_KEY)
        _group_id, error = self.run_to_idle()
        self.assertEqual(error, '[REDACTED]')
        self.assertEqual(self.page.stage_label.text(), '失败')
        self.assertFalse(_LIVE_WORKERS)

    def test_another_run_uses_a_fresh_token_and_does_not_overwrite_previous_output(self):
        self.fill_valid()
        first_id, _ = self.run_to_idle()
        first_output = self.page._group['output_dir']
        second_id, _ = self.run_to_idle()
        self.assertNotEqual(first_id, second_id)
        self.assertNotEqual(first_output, self.page._group['output_dir'])
        self.assertEqual(len(self.page.item_labels), 3)
        self.assertEqual(self.page.stage_label.text(), '已完成')
        self.assertTrue((Path(first_output) / 'manifest.json').is_file())

    def test_result_cards_show_ordered_thumbnails_sizes_and_open_actions(self):
        self.fill_valid()
        self.run_to_idle()
        panel = self.page.result_panel
        self.assertEqual(len(panel.cards), 3)
        cards = panel.ordered_cards()
        self.assertEqual([card.badge_label.text() for card in cards], ['轮1', '轮2', '详1'])
        for card in cards:
            self.assertEqual(card.record['status'], 'succeeded')
            self.assertFalse(card.thumbnail.pixmap().isNull())
            self.assertIn('1024x1024', card.size_label.text())
            self.assertEqual(card.status_label.text(), '成功 · 模型：gpt-image-2.5-sunburst')
            self.assertTrue(card.regenerate_button.isEnabled())
            self.assertTrue(card.folder_button.isEnabled())
        self.assertFalse(self.page.regenerate_failed_button.isEnabled())
        first = cards[0]
        with patch('ui.components.ecommerce_result_panel.open_local') as opener:
            QTest.mouseClick(first.thumbnail, Qt.LeftButton)
            self.assertEqual(opener.call_args.args[0], first.record['file_path'])
            self.assertFalse(opener.call_args.kwargs['folder'])
            first.folder_button.click()
            self.assertEqual(opener.call_args.args[0], str(Path(first.record['file_path']).parent))
            self.assertTrue(opener.call_args.kwargs['folder'])
        self.page._refresh_results()
        self.assertEqual(panel.ordered_cards(), cards, '刷新结果应就地更新已有卡片')
        self.page.show()
        for width, multiple in ((480, False), (1200, True)):
            self.page.resize(width, 800)
            QTest.qWait(30)
            self.assertEqual(panel.grid.columns > 1, multiple)
            self.assertEqual(self.page.body_scroll.horizontalScrollBar().maximum(), 0)
        self.assertNotIn(TEST_KEY, self.ui_text())

    def test_thumbnail_refresh_reads_replaced_file_without_using_old_pixmap_cache(self):
        self.fill_valid()
        self.run_to_idle()
        card = self.page.result_panel.ordered_cards()[0]
        path = Path(card.record['file_path'])
        previous = card.thumbnail.pixmap().toImage().pixelColor(px(110), px(90))
        image = QImage(1, 1, QImage.Format_ARGB32)
        color = QColor('#12ab56')
        self.assertNotEqual(previous, color)
        image.fill(color)
        self.assertTrue(image.save(str(path), 'PNG'))
        self.page._refresh_results()
        self.assertIs(self.page.result_panel.ordered_cards()[0], card)
        self.assertEqual(card.thumbnail.pixmap().toImage().pixelColor(px(110), px(90)), color)

    def test_failed_cards_redact_errors_and_retry_only_failed_items(self):
        self.fill_valid()

        def sometimes_fails(*args, **kwargs):
            if args[3].endswith('carousel 1'):
                raise RuntimeError('出图失败 ' + TEST_KEY)
            return fake_image(*args, **kwargs)

        self.image.side_effect = sometimes_fails
        self.run_to_idle()
        cards = self.page.result_panel.ordered_cards()
        failed = cards[0]
        self.assertEqual(failed.record['status'], 'failed')
        self.assertEqual(failed.status_label.text(), '失败 · 模型：gpt-image-2.5-sunburst')
        self.assertIn('[REDACTED]', failed.error_label.text())
        self.assertNotIn(TEST_KEY, failed.error_label.text())
        self.assertTrue(failed.regenerate_button.isEnabled())
        self.assertTrue(failed.folder_button.isEnabled())
        with patch('ui.components.ecommerce_result_panel.open_local') as opener:
            failed.folder_button.click()
        self.assertEqual(opener.call_args.args[0], self.page._group['output_dir'])
        self.assertTrue(opener.call_args.kwargs['folder'])
        self.assertTrue(self.page.regenerate_failed_button.isEnabled())
        old_calls = self.image.call_count
        self.image.side_effect = fake_image
        self.run_regenerate_to_idle(self.page.regenerate_failed_button.click)
        self.assertEqual(self.image.call_count - old_calls, 1)
        self.assertEqual(self.image.call_args.args[3], failed.record['prompt'])
        self.assertEqual(self.analysis.call_count, 1, '重生成不得再次分析产品')
        self.assertEqual(self.page.result_panel.ordered_cards(), cards)
        self.assertTrue(all(card.record['status'] == 'succeeded' for card in cards))
        self.assertEqual(self.page.stage_label.text(), '已完成')
        self.assertEqual(self.page.summary_label.text(), '成功 3 / 失败 0 / 跳过 0')
        self.assertFalse(self.page.regenerate_failed_button.isEnabled())
        self.assertNotIn(TEST_KEY, self.ui_text())

    def test_failed_selection_visibility_all_toggle_busy_and_clear_reset(self):
        group, records = self.seed_history(states=('failed', 'failed', 'failed', 'succeeded', 'succeeded'))
        self.page._view_history_group(group['group_id'])
        panel = self.page.result_panel
        cards = panel.ordered_cards()
        self.assertEqual([not card.select_checkbox.isHidden() for card in cards],
                         [True, True, True, False, False])
        self.assertFalse(panel.regenerate_selected_button.isEnabled())
        cards[0].select_checkbox.setChecked(True)
        cards[2].select_checkbox.setChecked(True)
        self.assertEqual(panel.selected_keys(), [records[0]['item_id'], records[2]['item_id']])
        self.assertEqual(panel.regenerate_selected_button.text(), '重生成选中（2）')
        self.assertTrue(panel.regenerate_selected_button.isEnabled())
        self.assertFalse(panel.select_all_checkbox.isChecked())
        panel.select_all_checkbox.setChecked(True)
        self.assertEqual(panel.selected_keys(), [record['item_id'] for record in records[:3]])
        cards[1].select_checkbox.setChecked(False)
        self.assertFalse(panel.select_all_checkbox.isChecked())
        cards[1].select_checkbox.setChecked(True)
        self.assertTrue(panel.select_all_checkbox.isChecked())
        panel.select_all_checkbox.setChecked(False)
        self.assertEqual(panel.selected_keys(), [])
        panel.select_all_checkbox.setChecked(True)
        panel.set_busy(True)
        self.assertEqual(panel.selected_keys(), [])
        self.assertFalse(panel.select_all_checkbox.isEnabled())
        self.assertFalse(panel.regenerate_selected_button.isEnabled())
        self.assertFalse(panel.regenerate_failed_button.isEnabled())
        self.assertTrue(all(not card.regenerate_button.isEnabled() for card in cards))
        self.assertTrue(all(not card.select_checkbox.isEnabled() for card in cards))
        panel.set_busy(False)
        self.assertFalse(panel.regenerate_selected_button.isEnabled())
        panel.select_all_checkbox.setChecked(True)
        panel.clear()
        self.assertEqual(panel.selected_keys(), [])
        self.assertTrue(all(not card.select_checkbox.isChecked() for card in cards))
        self.assertFalse(panel.select_all_checkbox.isChecked())
        self.assertEqual(panel.regenerate_selected_button.text(), '重生成选中（0）')
        self.assertFalse(panel.regenerate_selected_button.isEnabled())

    def test_selected_signal_launches_regenerate_with_current_item_ids(self):
        group, records = self.seed_history(states=('failed', 'failed', 'failed', 'succeeded', 'succeeded'))
        self.page._view_history_group(group['group_id'])
        panel = self.page.result_panel
        keys = [records[0]['item_id'], records[2]['item_id']]
        for key in keys:
            panel.cards[key].select_checkbox.setChecked(True)
        selected = QSignalSpy(panel.regenerate_selected_requested)
        with patch.object(self.page, '_launch_worker') as launch:
            panel.regenerate_selected_button.click()
        self.assertEqual(list(selected[0]), [keys])
        launch.assert_called_once_with(dict(group_id=group['group_id'], item_keys=keys),
                                       operation='regenerate')

    def test_selected_regeneration_updates_only_two_chosen_failed_items(self):
        group, records = self.seed_history(states=('failed', 'failed', 'failed', 'succeeded', 'succeeded'))
        self.page._view_history_group(group['group_id'])
        panel = self.page.result_panel
        for index in (0, 2):
            panel.cards[records[index]['item_id']].select_checkbox.setChecked(True)
        self.run_regenerate_to_idle(panel.regenerate_selected_button.click)
        self.assertEqual(self.last_worker.operation, 'regenerate')
        self.assertEqual(self.last_worker.arguments['item_keys'],
                         [records[0]['item_id'], records[2]['item_id']])
        self.assertEqual(self.image.call_count, 2)
        self.assertEqual({call.args[3] for call in self.image.call_args_list},
                         {records[0]['prompt'], records[2]['prompt']})
        self.analysis.assert_not_called()
        current = self.config.ecommerce_store().list_items(group['group_id'])
        for index in (1, 3, 4):
            self.assertEqual(current[index], records[index])
        self.assertEqual([record['status'] for record in current],
                         ['succeeded', 'failed', 'succeeded', 'succeeded', 'succeeded'])
        self.assertEqual(panel.selected_keys(), [])
        self.assertTrue(panel.regenerate_failed_button.isEnabled())
        self.assertTrue(all(panel.cards[records[index]['item_id']].select_checkbox.isHidden()
                            for index in (0, 2)))
        self.assert_results_match_store()

    def test_finished_refreshes_database_even_without_terminal_progress_callbacks(self):
        self.fill_valid()

        def make_quiet_manager():
            manager = self.make_manager()
            start = manager.start_group

            def quiet_start(**arguments):
                item_callback, group_callback = manager.on_item, manager.on_group
                manager.on_item = lambda record: (
                    item_callback(record) if record['status'] in ('pending', 'running') else None)
                manager.on_group = lambda record: (
                    group_callback(record) if record['status'] in ('analyzing', 'generating') else None)
                return start(**arguments)

            manager.start_group = quiet_start
            return manager

        self.factory.side_effect = make_quiet_manager

        def image(*args, **kwargs):
            if args[3].endswith('carousel 2'):
                raise RuntimeError('offline image failed')
            return fake_image(*args, **kwargs)

        self.image.side_effect = image
        self.run_to_idle()
        self.assertEqual(self.page.stage_label.text(), '部分成功')
        self.assertEqual(self.page.summary_label.text(), '成功 2 / 失败 1 / 跳过 0')
        self.assert_results_match_store()
        self.assertTrue(self.page.regenerate_failed_button.isEnabled())

    def test_worker_exception_settles_cards_and_late_progress_cannot_restore_running(self):
        self.fill_valid()

        def make_crashing_manager():
            manager = self.make_manager()
            generate = manager._generate

            def crash(group, item, cancel_event):
                if item['kind'] == 'carousel' and item['seq'] == 2:
                    manager._item(dict(item, status='running'))
                    raise RuntimeError('worker failed ' + TEST_KEY)
                return generate(group, item, cancel_event)

            manager._generate = crash
            return manager

        self.factory.side_effect = make_crashing_manager
        self.run_to_idle()
        self.assert_results_match_store()
        group = self.page._group.copy()
        record = self.config.ecommerce_store().list_items(group['group_id'])[0]
        self.page._on_item(dict(record, status='running', updated_at=record['updated_at'] - 1))
        self.page._on_group(dict(group, status='generating', updated_at=group['updated_at'] - 1))
        self.assert_results_match_store()
        self.assertEqual(self.page._group['status'], 'failed')
        self.assertEqual(self.page.stage_label.text(), '失败')
        self.assertNotIn(TEST_KEY, self.ui_text())

    def test_successful_card_can_explicitly_regenerate_its_own_prompt(self):
        self.fill_valid()
        self.run_to_idle()
        cards = self.page.result_panel.ordered_cards()
        selected = cards[1]
        prompt = selected.record['prompt']
        old_calls = self.image.call_count
        self.run_regenerate_to_idle(selected.regenerate_button.click)
        self.assertEqual(self.image.call_count - old_calls, 1)
        self.assertEqual(self.image.call_args.args[3], prompt)
        self.assertEqual(self.analysis.call_count, 1)
        self.assertEqual(self.page.result_panel.ordered_cards(), cards)
        self.assertEqual(selected.record['status'], 'succeeded')
        self.assertNotIn(TEST_KEY, self.ui_text())

    def test_model_pickers_filter_by_kind_and_available_and_refresh_effective_lines(self):
        records = {
            'chat-good': {'id': 'chat-good', 'kind': 'chat', 'available': True},
            'chat-hidden-pack': {'id': 'chat-hidden-pack', 'kind': 'chat', 'available': False},
            'image-good': {'id': 'image-good', 'kind': 'image', 'available': True},
            'image-hidden-pack': {'id': 'image-hidden-pack', 'kind': 'image', 'available': False},
            'video-good': {'id': 'video-good', 'kind': 'video', 'available': True},
        }
        with patch('ui.pages.ecommerce_page.catalog_snapshot', return_value=records):
            self.page.refresh_model_pickers()

        def values(combo):
            return [combo.itemData(index) for index in range(combo.count())
                    if not str(combo.itemData(index) or '').startswith('__separator__:')]

        self.assertEqual(values(self.page.analysis_model_picker), ['chat-good'])
        self.assertEqual(values(self.page.image_model_picker), ['image-good'])
        for picker in (self.page.analysis_model_picker, self.page.image_model_picker):
            self.assertGreaterEqual(picker.minimumWidth(), px(260))
            self.assertGreaterEqual(picker.sizeHint().width(), px(260))
        self.page.analysis_model_picker.set_value('chat-good')
        self.page.image_model_picker.set_value('image-good')
        self.assertEqual(self.page.analysis_model_picker.value(), 'chat-good')
        self.assertEqual(self.page.image_model_picker.value(), 'image-good')

        self.config.config['stations'] = [
            {'id': 'chat-route', 'name': 'Chat', 'enabled': True,
             'base_url': 'https://chat.example/v1', 'api_key': 'test-chat-key'},
            {'id': 'image-route', 'name': 'Image', 'enabled': True,
             'base_url': 'https://image.example/v1', 'api_key': 'test-image-key'},
        ]
        self.config.config['line_routing'] = {'chat': 'chat-route', 'image': 'image-route'}
        self.page.refresh_effective_lines()
        self.assertEqual(self.page.analysis_line_label.text(), '已生效')
        self.assertEqual(self.page.image_line_label.text(), '已生效')
        self.assertIn('chat.example', self.page.analysis_line_label.toolTip())
        self.assertIn('image.example', self.page.image_line_label.toolTip())

        self.config.config['line_routing']['chat'] = 'deleted-route'
        self.config.config['api']['base_url'] = 'https://active.example/v1'
        self.page.refresh_effective_lines()
        self.assertEqual(self.page.analysis_line_label.text(), '已生效')
        self.assertIn('active.example', self.page.analysis_line_label.toolTip())

    def test_in_page_routes_persist_refresh_pickers_and_sync_settings_combos(self):
        self.config.config['stations'] = [
            {'id': 'chat-station', 'name': '文本站', 'enabled': True,
             'base_url': 'https://chat.example/v1', 'api_key': 'test-chat-key'},
            {'id': 'image-station', 'name': '图片站', 'enabled': True,
             'base_url': 'https://image.example/v1', 'api_key': 'test-image-key'},
            {'id': 'disabled-station', 'name': '停用站', 'enabled': False,
             'base_url': 'https://disabled.example/v1', 'api_key': 'test-disabled-key'},
        ]
        self.config.config['line_routing'] = {'chat': 'active', 'image': 'active'}
        self.page.refresh_line_routes()
        for combo in (self.page.analysis_line_combo, self.page.image_line_combo):
            self.assertEqual(combo.itemData(0), 'active')
            self.assertNotIn('disabled-station', [combo.itemData(index) for index in range(combo.count())])

        self.config.model_catalog_controller = Mock()
        chat_settings_combo, image_settings_combo = ComboBox(), ComboBox()
        chat_settings_combo.addItem('当前线路', userData='active')
        chat_settings_combo.addItem('文本站', userData='chat-station')
        image_settings_combo.addItem('当前线路', userData='active')
        image_settings_combo.addItem('图片站', userData='image-station')
        settings_refreshes = []

        def refresh_settings_routes():
            from PyQt5.QtCore import QSignalBlocker
            for combo, capability in ((chat_settings_combo, 'chat'),
                                      (image_settings_combo, 'image')):
                blocker = QSignalBlocker(combo)
                index = combo.findData(self.config.config['line_routing'][capability])
                combo.setCurrentIndex(index)
                del blocker
            settings_refreshes.append(True)

        self.page.line_changed.connect(refresh_settings_routes)
        records_seen = []

        def snapshot(_config, capability):
            route = self.config.config['line_routing'][capability]
            model_id = f'{route}-{capability}-model'
            records_seen.append((capability, route))
            return {model_id: {'id': model_id, 'kind': capability, 'available': True}}

        with patch('ui.pages.ecommerce_page.catalog_snapshot', side_effect=snapshot), \
                patch.object(self.page, 'refresh_model_pickers', wraps=self.page.refresh_model_pickers) as refresh:
            self.page.analysis_line_combo.setCurrentIndex(
                self.page.analysis_line_combo.findData('chat-station'))
            self.assertEqual(self.config.config['line_routing']['chat'], 'chat-station')
            self.assertEqual(chat_settings_combo.currentData(), 'chat-station')
            self.assertEqual(image_settings_combo.currentData(), 'active')
            self.config.model_catalog_controller.reload_lines.assert_called_once_with()
            refresh.assert_called_once_with()
            self.assertIn('chat-station-chat-model', [self.page.analysis_model_picker.itemData(index)
                                                       for index in range(self.page.analysis_model_picker.count())])
            self.assertTrue(self.page.image_model_picker.minimumWidth() >= px(260))
            self.assertTrue(any(item == ('chat', 'chat-station') for item in records_seen))

            self.page.image_line_combo.setCurrentIndex(
                self.page.image_line_combo.findData('image-station'))
            self.assertEqual(self.config.config['line_routing']['image'], 'image-station')
            self.assertEqual(chat_settings_combo.currentData(), 'chat-station')
            self.assertEqual(image_settings_combo.currentData(), 'image-station')
            self.assertEqual(self.config.model_catalog_controller.reload_lines.call_count, 2)
            self.assertEqual(refresh.call_count, 2)
            self.assertIn('image-station-image-model', [self.page.image_model_picker.itemData(index)
                                                        for index in range(self.page.image_model_picker.count())])
            self.assertTrue(any(item == ('image', 'image-station') for item in records_seen))

            self.page.analysis_line_combo.setCurrentIndex(
                self.page.analysis_line_combo.findData('active'))
            self.assertEqual(self.config.config['line_routing']['chat'], 'active')
            self.assertEqual(chat_settings_combo.currentData(), 'active')
            self.page.image_line_combo.setCurrentIndex(
                self.page.image_line_combo.findData('active'))
            self.assertEqual(self.config.config['line_routing']['image'], 'active')
            self.assertEqual(image_settings_combo.currentData(), 'active')
            self.assertEqual(self.config.model_catalog_controller.reload_lines.call_count, 4)
            self.assertEqual(refresh.call_count, 4)
            self.assertIn('active-chat-model', [self.page.analysis_model_picker.itemData(index)
                                                for index in range(self.page.analysis_model_picker.count())])
            self.assertIn('active-image-model', [self.page.image_model_picker.itemData(index)
                                                 for index in range(self.page.image_model_picker.count())])
        self.assertEqual(len(settings_refreshes), 4)

    def test_model_picker_values_and_thinking_persist_and_empty_overrides_are_omitted(self):
        records = {
            'chat-choice': {'id': 'chat-choice', 'kind': 'chat', 'available': True,
                            'display_name': 'Chat Choice'},
            'image-choice': {'id': 'image-choice', 'kind': 'image', 'available': True,
                             'display_name': 'Image Choice'},
        }
        with patch('ui.pages.ecommerce_page.catalog_snapshot', return_value=records):
            self.page.refresh_model_pickers()

        self.page.analysis_model_picker.setCurrentIndex(
            self.page.analysis_model_picker.findData('chat-choice'))
        self.page.image_model_picker.setCurrentIndex(
            self.page.image_model_picker.findData('image-choice'))
        self.page.thinking_switch.setChecked(True)
        self.assertEqual(self.config.config['ecommerce']['analysis_model'], 'chat-choice')
        self.assertEqual(self.config.config['ecommerce']['image_model'], 'image-choice')
        self.assertTrue(self.config.config['ecommerce']['thinking'])
        self.assertEqual(self.page.model_overrides(), {
            'analysis_model': 'chat-choice', 'image_model': 'image-choice'})

        self.page.analysis_model_picker.set_value('')
        self.page.image_model_picker.set_value('')
        self.assertEqual(self.page.model_overrides(), {})

    def test_regeneration_locks_all_actions_supports_cancel_and_queues_ui_callbacks(self):
        self.fill_valid()
        self.config.config['ecommerce']['max_concurrency'] = 1
        self.run_to_idle()
        cards = self.page.result_panel.ordered_cards()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        callback_threads = []
        original = self.page._on_item

        def observe(record):
            callback_threads.append(QThread.currentThread())
            original(record)

        def blocking_image(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('测试未释放重生成任务')
            return fake_image(*args, **kwargs)

        self.page._on_item = observe
        self.image.side_effect = blocking_image
        old_calls = self.image.call_count
        self.page._run_regenerate([(card.record['kind'], card.record['seq']) for card in cards])
        self.wait_until(entered.is_set)
        self.assertTrue(self.page.busy)
        self.assertFalse(self.page.start_button.isEnabled())
        self.assertTrue(all(not form.isEnabled() for form in self.page._form_cards))
        self.assertTrue(all(not card.regenerate_button.isEnabled() for card in cards))
        self.assertFalse(self.page.regenerate_failed_button.isEnabled())
        self.assertTrue(self.page.cancel_button.isEnabled())
        self.assertNotEqual(self.factory_threads[-1], self.app.thread())
        self.page.cancel_button.click()
        self.assertTrue(self.page.cancel_event.is_set())
        release.set()
        self.wait_until(lambda: not self.page.busy)
        self.assertEqual(self.image.call_count - old_calls, 1)
        self.assertTrue(callback_threads)
        self.assertTrue(all(thread == self.app.thread() for thread in callback_threads))
        self.assertTrue(all(form.isEnabled() for form in self.page._form_cards))
        self.assertFalse(self.page.cancel_button.isEnabled())
        self.assertTrue(all(card.regenerate_button.isEnabled() for card in cards))
        self.assertEqual(self.page.summary_label.text(), '成功 1 / 失败 0 / 跳过 2')
        self.assertNotIn(TEST_KEY, self.ui_text())

    def test_result_without_references_hides_token_warning_and_missing_file_placeholder(self):
        self.fill_valid()

        def warned_image(*args, **kwargs):
            result = fake_image(*args, **kwargs)
            if args[3].endswith('carousel 1'):
                result.image_tokens = 0
                result.warnings = []
            else:
                result.warnings = ['尺寸已降档 4K → 1K ' + TEST_KEY]
            return result

        self.image.side_effect = warned_image
        self.run_to_idle()
        cards = self.page.result_panel.ordered_cards()
        self.assertNotIn('参考图可能未被模型采用', cards[0].warning_label.text())
        self.assertIn('尺寸已降档', cards[1].warning_label.text())
        self.assertIn('[REDACTED]', cards[1].warning_label.text())
        Path(cards[0].record['file_path']).unlink()
        self.page._refresh_results()
        self.assertIs(self.page.result_panel.ordered_cards()[0], cards[0])
        placeholder = '\n'.join(label.text() for label in cards[0].findChildren(QLabel))
        self.assertIn('文件缺失', placeholder)
        pixmap = cards[0].thumbnail.pixmap()
        self.assertTrue(pixmap is None or pixmap.isNull())
        # A stale persisted reference warning must not affect pure text-to-image items.
        store = self.config.ecommerce_store()
        store.upsert_item(dict(cards[1].record, image_tokens=None,
                               warnings=['参考图可能未被模型采用（image_tokens=0）']))
        self.page._refresh_results()
        self.assertNotIn('参考图可能未被模型采用', cards[1].warning_label.text())
        Path(cards[0].record['file_path']).write_bytes(b'invalid-image')
        self.page._refresh_results()
        self.assertEqual(cards[0].thumbnail.text(), '文件缺失')
        self.assertNotIn(TEST_KEY, self.ui_text())

    def test_regeneration_rejects_missing_group_or_foreign_item_before_worker(self):
        self.page._run_regenerate()
        self.assertIsNone(self.page._worker)
        self.factory.assert_not_called()
        self.assertTrue(self.notices.called)
        self.fill_valid()
        self.run_to_idle()
        group = self.page._group.copy()
        old_factory_calls = self.factory.call_count
        old_image_calls = self.image.call_count
        for key in [('carousel', 99), 'foreign-group:carousel:1']:
            with self.subTest(key=key):
                self.notices.reset_mock()
                self.page._run_regenerate([key])
                self.assertIsNone(self.page._worker)
                self.notices.assert_called()
        self.page._group = dict(group, group_id='missing-group')
        self.page._run_regenerate([('carousel', 1)])
        self.assertIsNone(self.page._worker)
        self.page._group = group
        self.assertEqual(self.factory.call_count, old_factory_calls)
        self.assertEqual(self.image.call_count, old_image_calls)

    def test_worker_resume_operation_uses_cancel_event_in_background(self):
        manager = Mock(api_key=TEST_KEY)
        manager.resume_group.return_value = 'resume-fixture-group'
        token = threading.Event()
        factory_threads = []

        def factory():
            factory_threads.append(QThread.currentThread())
            return manager

        worker = EcommerceWorker(factory, {'group_id': 'resume-fixture-group'}, token,
                                 (TEST_KEY,), operation='resume')
        finished = QSignalSpy(worker.finished)
        worker.start()
        self.wait_until(lambda: len(finished) == 1 and not worker.isRunning())
        self.assertEqual(list(finished[0]), ['resume-fixture-group', ''])
        manager.resume_group.assert_called_once_with(group_id='resume-fixture-group', cancel_event=token)
        manager.start_group.assert_not_called()
        manager.regenerate_items.assert_not_called()
        self.assertNotEqual(factory_threads[0], self.app.thread())
        worker.deleteLater()

    def test_responsive_form_fits_narrow_and_wide_viewports(self):
        self.page.show()
        for width in (480, 1100):
            self.page.resize(width, 800)
            QTest.qWait(20)
            self.assertLessEqual(self.page.body_content.width(), self.page.body_scroll.viewport().width())
            self.assertGreater(self.page.body_scroll.verticalScrollBar().maximum(), 0)
            self.assertEqual(self.page.body_scroll.horizontalScrollBar().maximum(), 0)

    def test_main_window_close_waits_for_ecommerce_thread(self):
        from ui.main_window import MainWindow

        window = MainWindow(ConfigManager(self.root / 'window-config.json'), network_time=False)
        entered, release = threading.Event(), threading.Event()
        try:
            page = window.ecommerce_page
            page.config_manager.config['api'].update(base_url='offline-fixture', api_key=TEST_KEY)
            page.config_manager.config['paths']['output'] = str(self.root / 'window-output')

            def blocking_analysis(*args, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise AssertionError('测试未释放窗口分析任务')
                return fake_analysis(*args, **kwargs)

            def factory():
                manager = EcommerceImageManager.build_from_config(page.config_manager)
                manager.analysis_func = blocking_analysis
                manager.image_func = self.image
                return manager

            page._make_manager = factory
            page.platform_checkboxes['douyin'].setChecked(True)
            page.product_name.setText('茶杯')
            page.product_info.setPlainText('耐热陶瓷')
            page.start_generation()
            self.wait_until(entered.is_set)
            self.assertFalse(window.close())
            self.assertTrue(page.busy)
            self.assertTrue(page.cancel_event.is_set())
            release.set()
            self.wait_until(lambda: not page.busy)
            self.image.assert_not_called()
        finally:
            release.set()
            self.wait_until(lambda: not window.ecommerce_page.busy)
            window.close()
            window.deleteLater()
            QTest.qWait(20)

    def test_page_close_requests_cancel_and_keeps_worker_until_it_finishes(self):
        self.fill_valid()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def blocking_analysis(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('测试未释放分析任务')
            return fake_analysis(*args, **kwargs)

        self.analysis.side_effect = blocking_analysis
        self.page.start_generation()
        self.wait_until(entered.is_set)
        self.assertFalse(self.page.close())
        self.assertTrue(self.page.cancel_event.is_set())
        self.assertIsNotNone(self.page._worker)
        release.set()
        self.wait_until(lambda: not self.page.busy)
        self.image.assert_not_called()
        self.assertEqual(self.page.stage_label.text(), '已取消')


if __name__ == '__main__':
    unittest.main()
