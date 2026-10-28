"""段3：电商图批量制作页 offscreen 回归（无联网、临时 ConfigManager）。

覆盖：初始空态 / 共享默认渲染 / 加行与 30 行上限 / 复制删除全选清空 /
校验顺序与就绪计数 / 平台自动套用 / 轮播详情开关与比例 chip / 提交参数形状 /
页头切换信号 / 草稿保存恢复与损坏容错 / Ctrl+V 粘贴 / 拖拽 / 徽章与淡红行 /
中文文案 tr / 2K 分辨率档 / 960 宽多缩放不横向溢出。
"""
import base64
import json
import os
import re
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt, QUrl
from PyQt5.QtGui import QColor, QDragEnterEvent, QDropEvent, QImage, QKeyEvent
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtWidgets import QApplication, QWidget

from core.config_manager import ConfigManager
from core.ecommerce.platforms import (
    LANGUAGE_TEXT, MAX_PLATFORMS, RATIOS, domestic, get_platform, overseas,
)
from core.i18n import set_language, tr
from ui import materials, tokens
from ui.components.image_pickers import image_cache_dir
from ui.pages import ecommerce_batch_page as batch_module
from ui.pages.ecommerce_batch_page import (
    DRAFT_FILENAME, MAX_PRODUCT_IMAGES, MAX_ROWS, EcommerceBatchPage,
)

CJK = re.compile(r'[\u4e00-\u9fff]')
PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==')


def solid_png(width=8, height=8, rgb=(42, 157, 102)):
    def chunk(name, payload):
        return (struct.pack('!I', len(payload)) + name + payload +
                struct.pack('!I', zlib.crc32(name + payload) & 0xFFFFFFFF))

    scanline = b'\x00' + bytes(rgb) * width
    raw = scanline * height
    header = struct.pack('!IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) +
            chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


class _FakeClipboard:
    """最小剪贴板替身：只实现页面用到的 image()/mimeData()。"""

    def __init__(self, image=None, text='', urls=None):
        self._image = image or QImage()
        self._text = text
        self._urls = list(urls or [])

    def image(self):
        return self._image

    def mimeData(self):
        mime = QMimeData()
        if self._urls:
            mime.setUrls([QUrl.fromLocalFile(str(path)) for path in self._urls])
        elif self._text:
            mime.setText(self._text)
        return mime


class _BatchCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        set_language('zh-CN')
        tokens.set_interface_scale(1.0)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.config = ConfigManager(self.root / 'config.json')
        self.pages = []
        self.page = self.make_page()

    def tearDown(self):
        for page in self.pages:
            try:
                page.close()
            except RuntimeError:
                pass
            try:
                page.deleteLater()
            except RuntimeError:
                pass
        self.pages = []
        QTest.qWait(10)
        self.app.processEvents()
        tokens.set_interface_scale(1.0)
        set_language('zh-CN')
        self.temp.cleanup()

    # —— 工具 ——
    def make_page(self, config=None):
        page = EcommerceBatchPage(config or self.config)
        self.pages.append(page)
        return page

    def make_page_in(self, name):
        config = ConfigManager(self.root / name / 'config.json')
        return self.make_page(config)

    def png_file(self, name='product.png'):
        path = self.root / name
        path.write_bytes(solid_png())
        return str(path.resolve())

    def make_ready(self, row=None, *, platforms=None):
        page = self.page
        row = row or page.rows[0]
        page.add_product_images(row, [self.png_file()])
        row.set_info('耐热陶瓷，便携易清洁')
        row.set_name('陶瓷茶杯')
        page.select_platform(platforms or ['taobao'])
        return row

    def draft_path(self):
        return self.config.path.parent / DRAFT_FILENAME

    # —— 用例 ——
    def test_initial_state_single_blank_row(self):
        page = self.page
        self.assertEqual(len(page.rows), 1)
        row = page.rows[0]
        self.assertEqual(row.product_image_paths, [])
        self.assertFalse(row.ready)
        self.assertEqual(row.failure_key, 'images')
        self.assertEqual(row.status_badge.text(), tr('待完善'))
        self.assertTrue(row.invalid)
        self.assertEqual(page.ready_count(), 0)
        self.assertFalse(page.submit_button.isEnabled())
        self.assertEqual(page.submit_button.toolTip(), tr('没有可提交的行'))
        self.assertEqual(page.stats_label.text(),
                         tr('共 {total} 行 · 就绪 {ready}').format(total=1, ready=0))
        self.assertEqual(page.copy_button.text(), tr('复制选中（{count}）').format(count=0))
        self.assertFalse(page.copy_button.isEnabled())
        self.assertFalse(page.delete_button.isEnabled())
        # 行对象对外暴露的测试 API。
        for attribute in ('product_image_paths', 'product_name', 'product_info',
                          'ready', 'status_badge', 'checkbox', 'copy_button',
                          'delete_button', 'image_cell'):
            self.assertTrue(hasattr(row, attribute), attribute)
        self.assertEqual(row.product_name, '')
        self.assertEqual(row.product_info, '')
        self.assertEqual(row.row_index, 1)

    def test_shared_defaults_render_from_config(self):
        page = self.page
        self.assertEqual(len(page.platform_selector.chips), 25)
        self.assertEqual(page.language_combo.currentData(), 'zh')
        self.assertEqual((page.carousel_spin.minimum(), page.carousel_spin.maximum()), (1, 6))
        self.assertEqual((page.detail_spin.minimum(), page.detail_spin.maximum()), (1, 15))
        self.assertEqual(page.carousel_spin.value(), 3)
        self.assertEqual(page.detail_spin.value(), 4)
        self.assertEqual(page.carousel_ratios.current_ratio(), '1:1')
        self.assertEqual(page.detail_ratios.current_ratio(), '3:4')
        self.assertEqual(len(page.carousel_ratios.buttons()), 9)
        self.assertEqual(len(page.detail_ratios.buttons()), 9)
        self.assertEqual(sorted(page.carousel_ratios.buttons()), sorted(RATIOS))
        self.assertEqual(page.analysis_model_value(), 'gemini-3.8-flash')
        self.assertEqual(page.image_model_value(), 'gpt-image-2.5-sunburst')
        self.assertEqual(page.tier_value(), '1K')
        self.assertTrue(page.carousel_check.isChecked())
        self.assertTrue(page.detail_check.isChecked())
        self.assertEqual(page.style_grid.paths(), [])

    def test_shared_defaults_from_custom_config(self):
        config = ConfigManager(self.root / 'custom' / 'config.json')
        config.config['ecommerce'].update(
            language='en', default_carousel_count=2, default_detail_count=7,
            default_carousel_ratio='9:16', default_detail_ratio='2:3',
            default_tier='4K', analysis_model='deepseek-chat',
            image_model='gpt-image-2')
        page = self.make_page(config)
        self.assertEqual(page.language_combo.currentData(), 'en')
        self.assertEqual(page.carousel_spin.value(), 2)
        self.assertEqual(page.detail_spin.value(), 7)
        self.assertEqual(page.carousel_ratios.current_ratio(), '9:16')
        self.assertEqual(page.detail_ratios.current_ratio(), '2:3')
        self.assertEqual(page.tier_value(), '4K')
        self.assertEqual(page.analysis_model_value(), 'deepseek-chat')
        self.assertEqual(page.image_model_value(), 'gpt-image-2')

    def test_header_switch_signals(self):
        page = self.page
        history = QSignalSpy(page.history_requested)
        single = QSignalSpy(page.single_requested)
        self.assertEqual(page.title_label.text(), tr('电商图批量制作'))
        self.assertEqual(page.subtitle_label.text(), tr(
            '每行一个商品，提交后在历史任务中各自显示为独立任务组'))
        page.history_button.click()
        page.single_button.click()
        self.assertEqual(len(history), 1)
        self.assertEqual(len(single), 1)

    def test_add_one_and_five_rows(self):
        page = self.page
        row = page.add_row()
        self.assertIsNotNone(row)
        self.assertEqual(len(page.rows), 2)
        created = page.add_rows(5)
        self.assertEqual(len(created), 5)
        self.assertEqual(len(page.rows), 7)
        self.assertEqual([item.index_label.text() for item in page.rows],
                         ['#%d' % index for index in range(1, 8)])
        self.assertEqual(page.stats_label.text(),
                         tr('共 {total} 行 · 就绪 {ready}').format(total=7, ready=0))

    def test_add_rows_respects_thirty_row_limit(self):
        page = self.page
        page.add_rows(40)
        self.assertEqual(len(page.rows), MAX_ROWS)
        self.assertIn('30', page.last_notice)
        self.assertEqual(page.add_rows(1), [])
        self.assertIn('30', page.last_notice)
        page.clear_rows()
        self.assertEqual(len(page.rows), 1)

    def test_select_all_toggle_and_copy_count(self):
        page = self.page
        page.add_rows(2)
        page.select_all()
        self.assertEqual(page.selected_count(), 3)
        self.assertEqual(page.select_all_button.text(), tr('取消全选'))
        self.assertEqual(page.copy_button.text(),
                         tr('复制选中（{count}）').format(count=3))
        self.assertTrue(page.copy_button.isEnabled())
        self.assertTrue(page.delete_button.isEnabled())
        page.toggle_select_all()
        self.assertEqual(page.selected_count(), 0)
        self.assertEqual(page.select_all_button.text(), tr('全选'))
        self.assertFalse(page.copy_button.isEnabled())

    def test_copy_selected_duplicates_content(self):
        page = self.page
        row = self.make_ready()
        row.checkbox.setChecked(True)
        copies = page.copy_selected()
        self.assertEqual(len(copies), 1)
        self.assertEqual(len(page.rows), 2)
        copied = page.rows[1]
        self.assertEqual(copied.product_image_paths, row.product_image_paths)
        self.assertEqual(copied.name_text(), row.name_text())
        self.assertEqual(copied.info_text(), row.info_text())
        self.assertTrue(copied.ready)
        self.assertIn(tr('已复制 {count} 行').format(count=1), page.last_notice)

    def test_copy_selected_without_selection_notice(self):
        page = self.page
        self.assertEqual(page.copy_selected(), [])
        self.assertEqual(page.last_notice, tr('请先选择要复制的行'))

    def test_delete_selected_removes_rows_and_keeps_blank(self):
        page = self.page
        page.add_rows(2)
        page.rows[0].checkbox.setChecked(True)
        page.rows[1].checkbox.setChecked(True)
        self.assertEqual(page.delete_selected(), 2)
        self.assertEqual(len(page.rows), 1)
        self.assertEqual(page.rows[0].index_label.text(), '#1')
        page.rows[0].checkbox.setChecked(True)
        self.assertEqual(page.delete_selected(), 1)
        self.assertEqual(len(page.rows), 1)
        self.assertFalse(page.rows[0].checkbox.isChecked())
        page.delete_selected()
        self.assertEqual(page.last_notice, tr('请先选择要删除的行'))

    def test_clear_rows_resets_to_single_blank(self):
        page = self.page
        self.make_ready()
        page.add_rows(2)
        self.assertEqual(page.clear_rows(), 3)
        self.assertEqual(len(page.rows), 1)
        blank = page.rows[0]
        self.assertEqual(blank.product_image_paths, [])
        self.assertEqual(blank.name_text(), '')
        self.assertEqual(blank.info_text(), '')
        self.assertFalse(blank.ready)
        self.assertIn(tr('已清空 {count} 行').format(count=3), page.last_notice)

    def test_validation_order_images_info_platform_output_models(self):
        page = self.page
        row = page.rows[0]
        self.assertEqual(row.failure_key, 'images')
        page.add_product_images(row, [self.png_file()])
        self.assertEqual(row.failure_key, 'product_info')
        row.set_info('材质与卖点')
        self.assertEqual(row.failure_key, 'platform')
        page.select_platform(['taobao'])
        self.assertTrue(row.ready)
        page.set_shared_defaults(enable_carousel=False, enable_detail=False)
        self.assertEqual(row.failure_key, 'output')
        page.set_shared_defaults(enable_carousel=True)
        page.set_shared_defaults(analysis_model='')
        self.assertEqual(row.failure_key, 'analysis_model')
        page.set_shared_defaults(analysis_model='deepseek-chat', image_model='')
        self.assertEqual(row.failure_key, 'image_model')
        page.set_shared_defaults(image_model='gpt-image-2')
        self.assertTrue(row.ready)
        self.assertEqual(page.ready_count(), 1)

    def test_ready_count_and_submit_button_state(self):
        page = self.page
        page.add_rows(1)
        self.make_ready(page.rows[0])
        self.make_ready(page.rows[1])
        self.assertEqual(page.ready_count(), 2)
        self.assertTrue(page.submit_button.isEnabled())
        self.assertEqual(page.submit_button.text(),
                         tr('开始批量制作（{count}）').format(count=2))
        self.assertEqual(page.submit_button.toolTip(), '')

    def test_platform_selection_applies_first_platform_defaults(self):
        page = self.page
        record = get_platform('taobao')
        page.select_platform(['taobao'])
        self.assertEqual(page.selected_platforms(), ['taobao'])
        self.assertEqual(page.carousel_spin.value(), record['defaultCarouselCount'])
        self.assertEqual(page.detail_spin.value(), record['defaultDetailCount'])
        self.assertEqual(page.carousel_ratios.current_ratio(),
                         record['defaultCarouselRatio'])
        self.assertEqual(page.detail_ratios.current_ratio(),
                         record['defaultDetailRatio'])
        # 多平台时取用户选中顺序里的第一个平台。
        first = get_platform('jd')
        page.select_platform(['jd', 'amazon'])
        self.assertEqual(page.selected_platforms(), ['jd', 'amazon'])
        self.assertEqual(page.carousel_spin.value(), first['defaultCarouselCount'])
        self.assertEqual(page.detail_ratios.current_ratio(),
                         first['defaultDetailRatio'])

    def test_platform_selection_limited_to_five(self):
        page = self.page
        ids = [record['id'] for record in (domestic() + overseas())][:6]
        page.select_platform(ids)
        self.assertEqual(len(page.selected_platforms()), MAX_PLATFORMS)
        self.assertIn(tr('最多选择 {count} 个平台').format(count=MAX_PLATFORMS),
                      page.last_notice)
        # 用户再勾选第 6 个平台：自动回退并提示。
        page.platform_selector.chips[ids[5]].setChecked(True)
        self.assertEqual(len(page.selected_platforms()), MAX_PLATFORMS)
        self.assertFalse(page.platform_selector.chips[ids[5]].isChecked())

    def test_output_toggles_disable_controls_and_zero_payload(self):
        page = self.page
        row = self.make_ready()
        page.carousel_check.setChecked(False)
        self.assertFalse(page.carousel_spin.isEnabled())
        self.assertFalse(page.carousel_ratios.chip_for('1:1').isEnabled())
        self.assertTrue(page.detail_spin.isEnabled())
        submission = page.build_submissions()[0]
        self.assertEqual(submission['carousel_count'], 0)
        self.assertEqual(submission['detail_count'], page.detail_spin.value())
        page.carousel_check.setChecked(True)
        page.detail_check.setChecked(False)
        submission = page.build_submissions()[0]
        self.assertEqual(submission['detail_count'], 0)
        self.assertTrue(page.detail_ratios.chip_for('1:1').isEnabled() is False)
        page.detail_check.setChecked(True)
        self.assertTrue(page.detail_spin.isEnabled())
        self.assertTrue(page.build_submissions())

    def test_ratio_chips_select_and_payload(self):
        page = self.page
        row = self.make_ready()
        page.carousel_ratios.chip_for('9:16').click()
        page.detail_ratios.chip_for('2:3').click()
        self.assertEqual(page.carousel_ratios.current_ratio(), '9:16')
        self.assertEqual(page.detail_ratios.current_ratio(), '2:3')
        self.assertTrue(page.carousel_ratios.chip_for('9:16').isChecked())
        self.assertFalse(page.carousel_ratios.chip_for('1:1').isChecked())
        submission = page.build_submissions()[0]
        self.assertEqual(submission['carousel_ratio'], '9:16')
        self.assertEqual(submission['detail_ratio'], '2:3')
        self.assertTrue(row.ready)

    def test_spinbox_ranges_clamp(self):
        page = self.page
        page.carousel_spin.setValue(99)
        self.assertEqual(page.carousel_spin.value(), 6)
        page.carousel_spin.setValue(0)
        self.assertEqual(page.carousel_spin.value(), 1)
        page.detail_spin.setValue(99)
        self.assertEqual(page.detail_spin.value(), 15)
        page.detail_spin.setValue(0)
        self.assertEqual(page.detail_spin.value(), 1)

    def test_build_submissions_shape_and_ready_only(self):
        page = self.page
        page.add_rows(1)
        ready_row = self.make_ready(page.rows[0], platforms=['taobao'])
        page.rows[1].set_name('缺信息')
        submissions = page.build_submissions()
        self.assertEqual(len(submissions), 1)
        submission = submissions[0]
        self.assertEqual(set(submission), {
            'platforms', 'product_name', 'product_info', 'language',
            'carousel_count', 'detail_count', 'carousel_ratio',
            'detail_ratio', 'tier', 'product_image_paths',
            'style_image_paths', 'thinking', 'row_index'})
        self.assertEqual(submission['platforms'], ['taobao'])
        self.assertEqual(submission['product_name'], '陶瓷茶杯')
        self.assertEqual(submission['product_info'], ready_row.info_text())
        self.assertEqual(ready_row.product_name, '陶瓷茶杯')
        self.assertEqual(ready_row.product_info, ready_row.info_text())
        self.assertEqual(ready_row.row_index, 1)
        self.assertEqual(submission['language'], 'zh')
        self.assertEqual(submission['tier'], '1K')
        self.assertEqual(submission['row_index'], 1)
        self.assertEqual(submission['product_image_paths'],
                         ready_row.product_image_paths)
        self.assertEqual(submission['style_image_paths'], [])
        self.assertFalse(submission['thinking'])

    def test_submit_emits_payload(self):
        page = self.page
        self.make_ready()
        spy = QSignalSpy(page.submit_requested)
        self.assertTrue(page.submit())
        self.assertEqual(len(spy), 1)
        payload = list(spy[0])[0]
        self.assertIsInstance(payload, list)
        self.assertEqual(payload[0]['row_index'], 1)
        self.assertEqual(payload[0]['platforms'], ['taobao'])

    def test_submit_without_ready_rows_notice(self):
        page = self.page
        spy = QSignalSpy(page.submit_requested)
        self.assertFalse(page.submit())
        self.assertEqual(len(spy), 0)
        self.assertEqual(page.last_notice, tr('没有可提交的行'))
        self.assertEqual(page.notice_label.text(), tr('没有可提交的行'))
        self.assertFalse(page.notice_label.isHidden())

    def test_model_change_persists_to_config(self):
        page = self.page
        page.analysis_model_picker.set_value('deepseek-chat')
        page.analysis_model_picker.textEdited.emit('deepseek-chat')
        self.assertEqual(self.config.config['ecommerce']['analysis_model'],
                         'deepseek-chat')
        page.image_model_picker.set_value('gpt-image-2')
        page.image_model_picker.textEdited.emit('gpt-image-2')
        self.assertEqual(self.config.config['ecommerce']['image_model'],
                         'gpt-image-2')

    def test_style_reference_shared_into_payload(self):
        page = self.page
        style = self.png_file('style.png')
        page.set_shared_defaults(style_image_path=style, thinking=True)
        self.assertEqual(page.style_grid.paths(), [style])
        row = self.make_ready()
        submission = page.build_submissions()[0]
        self.assertEqual(submission['style_image_paths'], [style])
        self.assertTrue(submission['thinking'])
        self.assertTrue(row.ready)

    def test_three_shared_styles_draft_payload_fourth_rejected_and_lightbox(self):
        styles = [self.png_file(f'style-{n}.png') for n in range(3)]
        self.page.set_shared_defaults(style_image_paths=styles)
        self.make_ready()
        self.assertEqual(self.page.build_submissions()[0]['style_image_paths'], styles)
        self.page._merge_style_images([self.png_file('fourth.png')])
        self.assertEqual(self.page.style_grid.paths(), styles)
        self.assertIn('最多3张', self.page.last_notice)
        with patch('ui.pages.ecommerce_batch_page.ImageLightbox.show_images') as show:
            self.page.style_grid.tile_at(1).clicked.emit(1)
        self.assertEqual(show.call_args.args[1:], (styles, 1))
        self.assertTrue(self.page.save_draft())
        restored = self.make_page()
        self.assertEqual(restored.style_grid.paths(), styles)
        self.assertEqual(restored.build_submissions()[0]['style_image_paths'], styles)

    def test_language_selection_and_payload(self):
        page = self.page
        self.make_ready()
        page.set_shared_defaults(language='en')
        self.assertEqual(page.language_key(), 'en')
        self.assertEqual(page.build_submissions()[0]['language'], 'en')
        keys = [page.language_combo.itemData(index)
                for index in range(page.language_combo.count())]
        self.assertEqual(keys, list(LANGUAGE_TEXT))

    def test_draft_save_and_restore_rows_with_images(self):
        page = self.page
        image = self.png_file()
        row = page.rows[0]
        page.add_product_images(row, [image])
        row.set_name('陶瓷杯')
        row.set_info('耐热陶瓷')
        self.assertTrue(page.save_draft())
        self.assertTrue(self.draft_path().is_file())
        data = json.loads(self.draft_path().read_text(encoding='utf-8'))
        self.assertEqual(data['rows'][0]['product_name'], '陶瓷杯')
        self.assertEqual(data['rows'][0]['product_image_paths'], [image])
        restored = self.make_page()
        self.assertEqual(len(restored.rows), len(page.rows))
        self.assertEqual(restored.rows[0].product_image_paths, [image])
        self.assertEqual(restored.rows[0].name_text(), '陶瓷杯')
        self.assertEqual(restored.rows[0].info_text(), '耐热陶瓷')
        self.assertEqual(restored.last_notice, '')

    def test_draft_restores_shared_defaults(self):
        page = self.page
        style = self.png_file('style.png')
        page.set_shared_defaults(
            platforms=['jd'], language='en', carousel_count=2,
            carousel_ratio='9:16', detail_count=9, detail_ratio='2:3',
            tier='4K', style_image_path=style, thinking=True)
        self.assertTrue(page.save_draft())
        restored = self.make_page()
        shared = restored.shared_defaults()
        self.assertEqual(shared['platforms'], ['jd'])
        self.assertEqual(shared['language'], 'en')
        self.assertEqual(shared['carousel_count'], 2)
        self.assertEqual(shared['carousel_ratio'], '9:16')
        self.assertEqual(shared['detail_count'], 9)
        self.assertEqual(shared['detail_ratio'], '2:3')
        self.assertEqual(shared['tier'], '4K')
        self.assertEqual(shared['style_image_paths'], [style])
        self.assertTrue(shared['thinking'])
        self.assertEqual(restored.language_combo.currentData(), 'en')

    def test_draft_corrupt_json_ignored_with_chinese_notice(self):
        self.draft_path().write_text('{ 这不是合法 JSON', encoding='utf-8')
        page = self.make_page()
        self.assertEqual(len(page.rows), 1)
        self.assertFalse(page.rows[0].ready)
        self.assertTrue(page.draft_load_error)
        self.assertIn('草稿', page.last_notice)
        self.assertTrue(CJK.search(page.last_notice))
        self.assertTrue(page.save_draft())

    def test_draft_save_is_debounced(self):
        page = self.page
        page.rows[0].set_name('防抖草稿')
        self.assertTrue(page._draft_timer.isActive())
        QTest.qWait(700)
        self.assertTrue(self.draft_path().is_file())
        data = json.loads(self.draft_path().read_text(encoding='utf-8'))
        self.assertEqual(data['rows'][0]['product_name'], '防抖草稿')
        self.assertFalse(page._draft_timer.isActive())

    def test_paste_clipboard_image_adds_to_row(self):
        page = self.page
        row = page.rows[0]
        image = QImage(8, 8, QImage.Format_ARGB32)
        image.fill(QColor('#22C55E'))
        page._clipboard = lambda: _FakeClipboard(image=image)
        added = page.paste_into_row(row)
        self.assertEqual(len(added), 1)
        self.assertTrue(Path(added[0]).is_file())
        self.assertTrue(Path(added[0]).is_relative_to(
            image_cache_dir(self.config)))
        self.assertEqual(row.product_image_paths, added)
        self.assertIn('1/6', row.image_cell.count_label.text())

    def test_paste_local_paths_text_respects_max(self):
        page = self.page
        row = page.rows[0]
        files = [self.png_file('p%d.png' % index) for index in range(7)]
        page._clipboard = lambda: _FakeClipboard(text='\n'.join(files))
        added = page.paste_into_row(row)
        self.assertEqual(len(added), MAX_PRODUCT_IMAGES)
        self.assertEqual(len(row.product_image_paths), MAX_PRODUCT_IMAGES)
        self.assertIn('6', page.last_notice)
        # 已满后再粘贴不再增加。
        page._clipboard = lambda: _FakeClipboard(text=files[0])
        self.assertEqual(page.paste_into_row(row), [])

    def test_ctrl_v_key_event_routes_image_paste(self):
        page = self.page
        row = page.rows[0]
        image = QImage(4, 4, QImage.Format_ARGB32)
        image.fill(QColor('#007AFF'))
        page._clipboard = lambda: _FakeClipboard(image=image)
        event = QKeyEvent(QEvent.KeyPress, Qt.Key_V, Qt.ControlModifier)
        row.name_edit.keyPressEvent(event)
        self.assertEqual(len(row.product_image_paths), 1)
        self.assertTrue(event.isAccepted())
        self.assertEqual(row.name_text(), '')
        spy = QSignalSpy(row.paste_requested)
        cell_event = QKeyEvent(QEvent.KeyPress, Qt.Key_V, Qt.ControlModifier)
        row.image_cell.keyPressEvent(cell_event)
        self.assertEqual(len(spy), 1)
        self.assertTrue(cell_event.isAccepted())
        self.assertIsNotNone(row.name_edit.paste_handler)
        self.assertIsNotNone(row.info_edit.paste_handler)

    def test_drag_drop_adds_image_files(self):
        page = self.page
        row = page.rows[0]
        image = self.png_file('drop.png')
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(image)])
        enter = QDragEnterEvent(QPoint(4, 4), Qt.CopyAction, mime,
                                Qt.LeftButton, Qt.NoModifier)
        row.image_cell.dragEnterEvent(enter)
        self.assertTrue(enter.isAccepted())
        drop = QDropEvent(QPointF(4, 4), Qt.CopyAction, mime,
                          Qt.LeftButton, Qt.NoModifier)
        row.image_cell.dropEvent(drop)
        self.assertIn(image, row.product_image_paths)
        self.assertIn('1/6', row.image_cell.count_label.text())

    def test_status_badge_and_invalid_row_style(self):
        page = self.page
        row = page.rows[0]
        danger_rgb = QColor(materials.ECOMMERCE['danger']).getRgb()[:3]
        invalid_background = materials.rgba(danger_rgb, 16)
        self.assertEqual(row.status_badge.text(), tr('待完善'))
        self.assertTrue(row.invalid)
        self.assertIn(invalid_background, row.styleSheet())
        self.make_ready(row)
        self.assertEqual(row.status_badge.text(), tr('就绪'))
        self.assertFalse(row.invalid)
        self.assertNotIn(invalid_background, row.styleSheet())
        self.assertEqual(row.status_badge.toolTip(), '')

    def test_product_images_capped_at_six(self):
        page = self.page
        row = page.rows[0]
        files = [self.png_file('cap%d.png' % index) for index in range(8)]
        added = page.add_product_images(row, files)
        self.assertEqual(len(added), MAX_PRODUCT_IMAGES)
        self.assertEqual(len(row.product_image_paths), MAX_PRODUCT_IMAGES)
        self.assertIn('6', page.last_notice)
        self.assertIn('6/6', row.image_cell.count_label.text())

    def test_retranslate_ui_uses_tr(self):
        platform_names = {record['name'] for record in (domestic() + overseas())}
        calls = []

        def fake_tr(text):
            calls.append(str(text))
            return '«%s»' % text

        with patch.object(batch_module, 'tr', fake_tr), \
                patch('ui.components.platform_multi_select.tr', fake_tr):
            page = self.make_page_in('translate')
            page.retranslate_ui()
        self.assertTrue(calls)
        self.assertEqual(page.title_label.text(), '«电商图批量制作»')
        self.assertEqual(page.history_button.text(), '«历史任务»')
        self.assertEqual(page.submit_button.text(), '«开始批量制作（{count}）»'.format(count=0))
        self.assertEqual(page.rows[0].name_edit.placeholderText(),
                         '«产品名称（建议填写，用作任务标签）»')
        style_widgets = set(id(widget) for widget in page.style_grid.findChildren(QWidget))
        offenders = []

        def texts(widget):
            for name in ('text', 'placeholderText', 'toolTip'):
                getter = getattr(widget, name, None)
                if callable(getter):
                    try:
                        value = getter()
                    except TypeError:
                        continue
                    if isinstance(value, str) and value:
                        yield value
            item_text = getattr(widget, 'itemText', None)
            if callable(item_text):
                for index in range(widget.count()):
                    yield widget.itemText(index)

        for widget in page.findChildren(QWidget):
            if id(widget) in style_widgets:
                continue
            for value in texts(widget):
                if CJK.search(value) and not value.startswith('«'):
                    offenders.append(value)
        self.assertEqual([value for value in offenders if value not in platform_names],
                         [])

    def test_tier_defaults_from_config_and_2k_fallback(self):
        self.assertEqual(self.page.tier_value(), '1K')
        config = ConfigManager(self.root / 'two2k' / 'config.json')
        config.config['ecommerce']['default_tier'] = '2K'
        page = self.make_page(config)
        self.assertEqual(page.tier_value(), '2K')
        page.rows[0].set_info('产品信息')
        page.add_product_images(page.rows[0], [self.png_file()])
        page.select_platform(['taobao'])
        self.assertEqual(page.build_submissions()[0]['tier'], '2K')
        fallback_config = ConfigManager(self.root / 'nokey' / 'config.json')
        del fallback_config.config['ecommerce']['default_tier']
        fallback = self.make_page(fallback_config)
        self.assertEqual(fallback.tier_value(), '2K')

    def test_row_copy_and_delete_buttons_wired(self):
        page = self.page
        row = page.rows[0]
        copy_spy = QSignalSpy(row.copy_requested)
        row.copy_button.click()
        self.assertEqual(len(copy_spy), 1)
        self.assertEqual(len(page.rows), 2)
        target = page.rows[1]
        delete_spy = QSignalSpy(target.delete_requested)
        target.delete_button.click()
        self.assertEqual(len(delete_spy), 1)
        self.assertEqual(len(page.rows), 1)

    def test_row_image_picker_links_emit_requests(self):
        page = self.page
        row = page.rows[0]
        self.assertEqual(row.image_cell.gallery_button.text(), tr('画廊选图'))
        self.assertEqual(row.image_cell.used_button.text(), tr('已用图片'))
        self.assertEqual(row.image_cell.url_button.text(), tr('输入'))
        gallery = QSignalSpy(row.gallery_requested)
        used = QSignalSpy(row.used_requested)
        url = QSignalSpy(row.url_requested)

        class _RejectedDialog:
            Accepted = 1
            Rejected = 0

            def __init__(self, *args, **kwargs):
                self.result_paths = []

            def exec_(self):
                return 0

            def deleteLater(self):
                return None

        # 真实点击会弹模态选择器（offscreen 会阻塞），替身只验证接线。
        with patch.object(batch_module, 'GalleryPickerDialog', _RejectedDialog) as _g, \
                patch.object(batch_module, 'UsedImagePickerDialog', _RejectedDialog) as _u, \
                patch.object(batch_module, 'UrlImageDialog', _RejectedDialog) as _v:
            row.image_cell.gallery_button.click()
            row.image_cell.used_button.click()
            row.image_cell.url_button.click()
            self.assertEqual(len(gallery), 1)
            self.assertEqual(len(used), 1)
            self.assertEqual(len(url), 1)
        self.assertIsNotNone(page.style_used_button)
        self.assertIsNotNone(page.style_gallery_button)

    def test_row_thumbnail_preview_signal_and_empty_browse(self):
        page = self.page
        row = page.rows[0]
        image = self.png_file('preview.png')
        preview = QSignalSpy(row.image_cell.preview_requested)
        browse = QSignalSpy(row.image_cell.browse_requested)
        row.set_images([image])
        row.image_cell._on_thumb_clicked()
        self.assertEqual(len(preview), 1)
        self.assertEqual(list(preview[0]), [[image], 0])
        row.set_images([])
        with patch.object(batch_module.QFileDialog, 'getOpenFileNames', return_value=([], '')):
            row.image_cell._on_thumb_clicked()
        self.assertEqual(len(browse), 1)

    def test_batch_row_preview_lightbox_and_actions(self):
        page = self.page
        row = page.rows[0]
        paths = [self.png_file('one.png'), self.png_file('two.png')]
        row.set_images(paths)
        with patch('ui.pages.ecommerce_batch_page.ImageLightbox.show_images') as show:
            row.image_cell._on_thumb_clicked()
            self.assertEqual(show.call_args.args[1:], (paths, 0))

        row.set_group_state({
            'group_id': 'batch-group', 'status': 'succeeded',
            'totals': {'total': 2, 'succeeded': 2, 'failed': 0},
        })
        self.assertEqual(row.group_id, 'batch-group')
        self.assertIn('完成', row.progress_label.text())
        platform = QSignalSpy(page.preview_group_requested)
        detail = QSignalSpy(page.detail_group_requested)
        regenerate = QSignalSpy(page.regenerate_group_requested)
        row.platform_preview_button.click()
        row.detail_button.click()
        row.regenerate_button.click()
        self.assertEqual(list(platform[0]), ['batch-group'])
        self.assertEqual(list(detail[0]), ['batch-group'])
        self.assertEqual(list(regenerate[0]), ['batch-group'])
        page.set_busy(True)
        self.assertTrue(row.platform_preview_button.isEnabled())
        self.assertFalse(row.detail_button.isEnabled())
        self.assertFalse(row.regenerate_button.isEnabled())
        page.set_busy(False)

    def test_running_state_locks_editing_keeps_row_actions_and_reports_completion(self):
        page, row = self.page, self.page.rows[0]
        path = self.png_file('running.png')
        row.set_images([path])
        row.set_name('运行商品')
        row.set_info('运行信息')
        page.begin_run({1})
        row.set_group_state({
            'group_id': 'running-group', 'status': 'succeeded',
            'totals': {'total': 1, 'succeeded': 1},
        })
        stop = QSignalSpy(page.stop_requested)
        page.show()
        self.app.processEvents()
        page.set_running(True, total=1)
        self.assertTrue(page._running)
        self.assertTrue(page.stop_button.isVisible())
        self.assertIn('批量制作中', page.run_status_label.text())
        self.assertFalse(page.add_one_button.isEnabled())
        self.assertFalse(row.name_edit.isEnabled())
        self.assertTrue(row.image_cell.thumb.isEnabled())
        self.assertTrue(row.platform_preview_button.isEnabled())
        page.stop_button.click()
        self.assertEqual(len(stop), 1)

        page.set_running(False)
        page.end_run()
        self.assertFalse(page._running)
        self.assertFalse(page.stop_button.isVisible())
        self.assertTrue(page.add_one_button.isEnabled())
        self.assertIn('完成 1/1', page.run_status_label.text())

    def test_batch_row_group_progress_states(self):
        row = self.page.rows[0]
        cases = [
            ('analyzing', {}, '分析中…', False, False),
            ('generating', {'total': 3, 'succeeded': 1}, '出图中 1/3', True, False),
            ('succeeded', {'total': 3, 'succeeded': 3}, '完成 3/3', True, True),
            ('partial', {'total': 3, 'succeeded': 2, 'failed': 1}, '部分失败', True, True),
            ('failed', {'total': 3, 'failed': 3}, '失败', False, True),
            ('cancelled', {'total': 3, 'cancelled': 3}, '已取消', False, True),
        ]
        for status, totals, expected, preview, terminal in cases:
            with self.subTest(status=status):
                row.set_group_state({
                    'group_id': 'g-' + status, 'status': status,
                    'totals': totals,
                })
                self.assertEqual(row.progress_label.text(), expected)
                self.assertEqual(row.platform_preview_button.isEnabled(), preview)
                self.assertEqual(row.detail_button.isEnabled(), terminal)
                self.assertEqual(row.regenerate_button.isEnabled(), terminal)

    def test_row_lightbox_six_images_navigation_close_and_reopen(self):
        page, row = self.page, self.page.rows[0]
        paths = [self.png_file(f'preview-{n}.png') for n in range(6)]
        row.set_images(paths)
        page.resize(1280, 900)
        page.show()
        QTest.mouseClick(row.image_cell.thumb, Qt.LeftButton)
        dialog = page._lightbox
        self.assertIsNotNone(dialog)
        self.assertEqual(dialog.images(), paths)
        self.assertEqual(dialog.current_index(), 0)
        self.assertEqual(dialog.counter_label.text(), '1 / 6')
        self.assertFalse(dialog.prev_button.isEnabled())
        QTest.mouseClick(dialog.next_button, Qt.LeftButton)
        self.assertEqual(dialog.current_index(), 1)
        QTest.keyClick(dialog, Qt.Key_Left)
        self.assertEqual(dialog.current_index(), 0)
        QTest.keyClick(dialog, Qt.Key_Escape)
        self.assertIsNone(page._lightbox)
        self.app.processEvents()
        QTest.mouseClick(row.image_cell.thumb, Qt.LeftButton)
        self.assertIsNotNone(page._lightbox)
        self.assertIsNot(page._lightbox, dialog)
        self.assertEqual(page._lightbox.images(), paths)

    def test_busy_keeps_partial_preview_and_blocks_edit_detail_regenerate(self):
        page, row = self.page, self.make_ready()
        row.set_group_state(dict(group_id='partial-live', status='generating',
                                totals=dict(total=3, succeeded=1, running=1, pending=1)))
        page.set_busy(True)
        preview = QSignalSpy(page.preview_group_requested)
        detail = QSignalSpy(page.detail_group_requested)
        regenerate = QSignalSpy(page.regenerate_group_requested)
        page.refresh_colors()
        page.refresh_interface_scale()
        row.platform_preview_button.click()
        page.open_detail(row)
        page.regenerate_row(row)
        self.assertEqual(list(preview[0]), ['partial-live'])
        self.assertEqual(len(detail), 0)
        self.assertEqual(len(regenerate), 0)
        self.assertFalse(row.name_edit.isEnabled())
        self.assertFalse(page.submit_button.isEnabled())
        self.assertFalse(page.submit())
        self.assertEqual(row.progress_label.text(), '出图中 1/3')
        page.set_busy(False)
        self.assertTrue(row.name_edit.isEnabled())
        self.assertTrue(page.submit_button.isEnabled())
        self.assertFalse(row.detail_button.isEnabled())  # Still generating.

    def test_run_snapshot_survives_row_removal_and_renumbering(self):
        page = self.page
        first, second = page.rows[0], page.add_row()
        snapshot = page.begin_run({1, 2})
        self.assertIs(snapshot[2], second)
        page.delete_row(first)
        self.assertEqual(second.row_index, 1)
        self.assertFalse(page.update_row_group(1, dict(group_id='removed', status='succeeded')))
        self.assertTrue(page.update_row_group(2, dict(group_id='original-second', status='analyzing')))
        self.assertEqual(second.group_id, 'original-second')
        page.end_run()

    def test_failure_and_progress_survive_theme_language_and_reset(self):
        from ui.theme import apply_ui_mode
        row = self.page.rows[0]
        self.assertTrue(row.detail_button.isHidden())
        row.set_failed_state('本地测试失败')
        for mode in ('light', 'dark'):
            apply_ui_mode(mode)
            self.page.refresh_colors()
            self.assertEqual(row.progress_label.text(), '失败')
            self.assertEqual(row.progress_label.toolTip(), '本地测试失败')
        row.set_group_state(dict(group_id='live', status='generating',
                                totals=dict(total=3, succeeded=1)))
        set_language('English')
        self.page.retranslate_ui()
        self.assertEqual(row.progress_label.text(), 'Generating 1/3')
        self.assertFalse(CJK.search(row.detail_button.text()))
        self.assertTrue(row.platform_preview_button.isEnabled())
        row.reset_state()
        self.assertEqual(row.group_id, '')
        self.assertTrue(row.progress_label.isHidden())
        self.assertTrue(row.platform_preview_button.isHidden())
        apply_ui_mode('light')

    def test_group_counts_prefer_live_totals_then_manifest_and_handle_bad_values(self):
        row = self.page.rows[0]
        items = [dict(status='succeeded'), dict(status='failed'), dict(status='pending')]
        group = dict(group_id='counts', status='generating', manifest=dict(items=items))
        row.set_group_state(group)
        self.assertEqual(row.progress_label.text(), '出图中 2/3')
        row.set_group_state(dict(group, totals=dict(pending=2, running=1, succeeded=0, failed=0)))
        self.assertEqual(row.progress_label.text(), '出图中 0/3')
        self.assertFalse(row.platform_preview_button.isEnabled())
        row.set_group_state(dict(group, totals=dict(total='bad', succeeded=None, failed='bad')))
        self.assertEqual(row.progress_label.text(), '出图中 2/3')

    def test_completed_row_actions_fit_four_scales_and_both_themes(self):
        from tests.qt_scale_support import flush_layout
        from ui.theme import apply_ui_mode
        from ui.tokens import px
        page, row = self.page, self.page.rows[0]
        row.set_group_state(dict(group_id='layout', status='succeeded',
                                totals=dict(total=6, succeeded=6)))
        page.show()
        try:
            for mode in ('light', 'dark'):
                apply_ui_mode(mode)
                for scale in (1., 1.25, 1.5, 1.75):
                    with self.subTest(mode=mode, scale=scale):
                        tokens.set_interface_scale(scale)
                        page.refresh_interface_scale()
                        page.resize(px(1280), px(900))
                        page.body_scroll.ensureWidgetVisible(row)
                        flush_layout(self.app, page)
                        buttons = (row.platform_preview_button, row.detail_button, row.regenerate_button)
                        for button in buttons:
                            self.assertTrue(button.isVisible())
                            self.assertTrue(row.rect().contains(button.geometry()))
                            self.assertGreaterEqual(button.height(), px(28))
                        for a, b in zip(buttons, buttons[1:]):
                            self.assertFalse(a.geometry().intersects(b.geometry()))
                        self.assertEqual(row.progress_label.text(), '完成 6/6')
        finally:
            apply_ui_mode('light')

    def test_callable_theme_scale_and_language_refresh(self):
        page = self.page
        page.refresh_colors()
        page.refresh_interface_scale()
        page.retranslate_ui()
        self.assertEqual(page.subtitle_label.text(), tr(
            '每行一个商品，提交后在历史任务中各自显示为独立任务组'))
        page.add_rows(2)
        page.refresh_interface_scale()
        page.refresh_colors()

    def test_no_horizontal_overflow_at_960_and_scaled(self):
        page = self.page
        page.add_rows(5)
        self.make_ready(page.rows[0])
        page.show()
        self.app.processEvents()
        for width in (960, 1280):
            for scale in (1.0, 1.25, 1.5, 1.75):
                tokens.set_interface_scale(scale)
                page.refresh_interface_scale()
                page.resize(width, 720)
                self.app.processEvents()
                QTest.qWait(20)
                self.assertEqual(
                    page.body_scroll.horizontalScrollBar().maximum(), 0,
                    '宽度 %s / 缩放 %s 时出现横向溢出' % (width, scale))
                self.assertLessEqual(
                    page.body_content.minimumSizeHint().width(),
                    page.body_scroll.viewport().width() + 2,
                    '宽度 %s / 缩放 %s 时内容最小宽度超过视口' % (width, scale))
        tokens.set_interface_scale(1.0)
        page.refresh_interface_scale()


    def test_model_grid_minimum_tooltips_and_language_at_four_scales(self):
        from tests.qt_scale_support import flush_layout
        from ui.tokens import px
        page = self.page
        page.show()
        for scale in (1., 1.25, 1.5, 1.75):
            tokens.set_interface_scale(scale)
            page.refresh_interface_scale()
            page.resize(px(1440), px(1000))
            flush_layout(self.app, page)
            self.assertEqual(page._model_columns, 3)
            controls = (page.analysis_model_picker, page.image_model_picker, page.tier_combo)
            for control in controls:
                self.assertGreaterEqual(control.width(), px(220))
                self.assertEqual(control.toolTip(), control.currentText())
            self.assertEqual(len({control.y() for control in controls}), 1)
            self.assertEqual(page.language_combo.height(), px(40))
            self.assertEqual(page.style_grid.max_count, 3)

    def test_model_grid_tracks_viewport_resize_without_manual_refresh(self):
        from tests.qt_scale_support import flush_layout
        from PyQt5.QtWidgets import QWidget, QVBoxLayout
        host = QWidget()
        host_layout = QVBoxLayout(host)
        host_layout.addWidget(self.page)
        try:
            host.resize(700, 900)
            host.show()
            flush_layout(self.app, host)
            self.assertEqual(self.page._model_columns, 2)
            host.resize(1680, 1100)
            flush_layout(self.app, host)
            self.assertEqual(self.page._model_columns, 3)
            controls = (self.page.analysis_model_picker,
                        self.page.image_model_picker, self.page.tier_combo)
            self.assertEqual(len({control.y() for control in controls}), 1)
        finally:
            self.page.setParent(None)
            host.close()
            host.deleteLater()



if __name__ == '__main__':
    unittest.main()
