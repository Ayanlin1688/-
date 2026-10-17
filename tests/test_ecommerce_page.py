"""Offline Qt integration tests: real orchestration/files, injected model clients."""
import base64
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import Qt, QThread
from PyQt5.QtGui import QColor, QImage
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtWidgets import QApplication, QLabel

from core.config_manager import ConfigManager
from core.ecommerce.manager import EcommerceImageManager
from core.ecommerce.platforms import domestic, overseas, LANGUAGE_TEXT, RATIOS, MAX_PLATFORMS
from core.ecommerce.image_client import TIER_SHORT_EDGE
from core.i18n import set_language
from tests.test_ecommerce_manager import TEST_KEY, fake_analysis, fake_image as manager_fake_image
from ui import materials
from ui.tokens import px
from ui.pages.ecommerce_page import EcommercePage, EcommerceWorker, _LIVE_WORKERS


# The networking fixture only checks PNG magic; Qt thumbnails need valid CRCs.
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==')


def fake_image(*args, **kwargs):
    result = manager_fake_image(*args, **kwargs)
    result.png_bytes = PNG
    return result


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
        with patch('ui.pages.ecommerce_page.QFileDialog.getOpenFileNames', return_value=(paths, '')):
            self.page.add_products_button.click()
        self.assertEqual(self.page.product_image_paths, paths)
        self.assertEqual(self.page.product_list.item(0).text(), '产品一.png')
        self.page.product_list.setCurrentRow(0)
        self.page.remove_product_button.click()
        self.assertEqual(self.page.product_image_paths, paths[1:])
        with patch('ui.pages.ecommerce_page.QFileDialog.getOpenFileName', return_value=(paths[0], '')):
            self.page.choose_style_button.click()
        with patch('ui.pages.ecommerce_page.QFileDialog.getOpenFileName', return_value=(paths[1], '')):
            self.page.choose_style_button.click()
        self.assertEqual(self.page.style_image_paths, paths[1:])
        self.page.clear_products_button.click()
        self.page.clear_style_button.click()
        self.assertEqual(self.page.product_image_paths, [])
        self.assertEqual(self.page.style_image_paths, [])

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

    def test_analysis_error_is_redacted_in_notice_and_log_and_restores_form(self):
        self.fill_valid()
        self.analysis.side_effect = RuntimeError('无文本权限 ' + TEST_KEY)
        group_id, error = self.run_to_idle()
        self.assertEqual(group_id, '')
        self.assertIn('[REDACTED]', error)
        self.assertEqual(self.page.stage_label.text(), '失败')
        self.assertNotIn(TEST_KEY, self.ui_text() + str(self.notices.call_args))
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
            self.assertEqual(card.status_label.text(), '成功')
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
        self.assertEqual(failed.status_label.text(), '失败')
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

    def test_result_warnings_zero_image_tokens_and_missing_file_placeholder(self):
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
        self.assertIn('参考图可能未被模型采用，请核对', cards[0].warning_label.text())
        self.assertIn('尺寸已降档', cards[1].warning_label.text())
        self.assertIn('[REDACTED]', cards[1].warning_label.text())
        self.assertIn(materials.palette()['warning'], cards[0].warning_label.styleSheet())
        Path(cards[0].record['file_path']).unlink()
        self.page._refresh_results()
        self.assertIs(self.page.result_panel.ordered_cards()[0], cards[0])
        placeholder = '\n'.join(label.text() for label in cards[0].findChildren(QLabel))
        self.assertIn('文件缺失', placeholder)
        pixmap = cards[0].thumbnail.pixmap()
        self.assertTrue(pixmap is None or pixmap.isNull())
        # The same warning can be persisted even when usage details are absent.
        store = self.config.ecommerce_store()
        store.upsert_item(dict(cards[1].record, image_tokens=None,
                               warnings=['参考图可能未被模型采用（image_tokens=0）']))
        self.page._refresh_results()
        self.assertIn('参考图可能未被模型采用，请核对', cards[1].warning_label.text())
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
