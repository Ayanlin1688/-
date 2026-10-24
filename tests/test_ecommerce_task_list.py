"""电商图任务列表组件 offscreen 回归：卡片 / tab / 分页 / 管理模式 / 删除确认。"""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import struct
import tempfile
import unittest
import zlib
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from PyQt5.QtTest import QSignalSpy
from PyQt5.QtWidgets import QApplication

from core.i18n import set_language, tr
from tests.qt_scale_support import reset_interface_scaling
from ui import materials
from ui.components.ecommerce_task_list import (
    RESUMABLE_STATUSES, EcommerceTaskCard, EcommerceTaskList,
)


def solid_png(width, height, rgb):
    def chunk(name, payload):
        return (struct.pack('!I', len(payload)) + name + payload +
                struct.pack('!I', zlib.crc32(name + payload) & 0xFFFFFFFF))

    scanline = b'\x00' + bytes(rgb) * width
    raw = scanline * height
    header = struct.pack('!IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) +
            chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def stage_text(status):
    return {
        'analyzing': tr('AI分析中'), 'generating': tr('图片生成中'),
        'succeeded': tr('已完成'), 'partial': tr('部分成功'),
        'failed': tr('失败'), 'cancelled': tr('已取消'),
    }.get(status, status)


class EcommerceTaskListTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        set_language('zh-CN')
        self.addCleanup(set_language, 'zh-CN')
        reset_interface_scaling(self.app)
        self.addCleanup(reset_interface_scaling, self.app)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.images = []
        for index in range(5):
            path = self.root / f'image-{index}.png'
            path.write_bytes(solid_png(48, 48, (20 * index, 90, 180)))
            self.images.append(str(path))
        self.panel = EcommerceTaskList(stage_text)
        self.addCleanup(self.dispose_panel)

    def dispose_panel(self):
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()

    def make_group(self, group_id='g1', **overrides):
        now = 1700000000
        group = dict(
            group_id=group_id, created_at=now - 60, updated_at=now,
            status='succeeded', product_name='陶瓷茶杯',
            platforms=[{'name': '某宝'}, {'name': '某东'}],
            totals={'succeeded': 3, 'total': 3}, output_dir='/tmp/output',
            config=dict(product_info='耐热陶瓷，便携易清洁，适合办公室与家庭使用。' * 2,
                        carousel_count=5, detail_count=8,
                        image_model='gpt-image-2.5-sunburst'),
            manifest={'items': []}, image_model='gpt-image-2.5-sunburst',
            thumbs=list(self.images[:3]))
        group.update(overrides)
        return group


class EcommerceTaskCardTests(EcommerceTaskListTestBase):
    def test_card_renders_name_platform_badge_counts_model_and_thumbs(self):
        group = self.make_group()
        self.panel.update_groups([group])
        card = self.panel.cards['g1']
        self.assertIsInstance(card, EcommerceTaskCard)
        self.assertEqual(card.name_label.text(), '陶瓷茶杯')
        self.assertEqual(card.time_label.text(), datetime.fromtimestamp(
            group['created_at']).strftime('%Y-%m-%d %H:%M'))
        self.assertEqual(card.status_badge.text(), '已完成')
        self.assertIn(materials.palette()['success'], card.status_badge.styleSheet())
        self.assertIn('耐热陶瓷', card.desc_label.text())
        self.assertEqual([chip.text() for chip in card.platform_chips],
                         ['某宝', '某东'])
        self.assertIn(tr('轮播图 {count} 张').format(count=5), card.counts_label.text())
        self.assertIn(tr('详情图 {count} 张').format(count=8), card.counts_label.text())
        self.assertEqual(card.model_label.text(), 'gpt-image-2.5-sunburst')
        self.assertEqual(card.thumb_count, 3)
        self.assertEqual(card.thumbs, self.images[:3])
        self.assertEqual(card.index_label.text(), '1')

    def test_status_badges_use_theme_tones(self):
        colors = materials.palette()
        cases = (
            ('analyzing', 'accent'),
            ('generating', 'warning'),
            ('succeeded', 'success'),
            ('partial', 'warning'),
            ('failed', 'error'),
            ('cancelled', 'text2'),
        )
        groups = [self.make_group(f'status-{status}', status=status,
                                  thumbs=list(self.images[:1]))
                  for status, _tone in cases]
        self.panel.update_groups(groups)
        for status, tone in cases:
            card = self.panel.cards[f'status-{status}']
            self.assertEqual(card.status_badge.text(), stage_text(status))
            self.assertIn(colors[tone], card.status_badge.styleSheet(), status)

    def test_name_falls_back_to_group_id_prefix(self):
        group = self.make_group(product_name='', thumbs=[])
        self.panel.update_groups([group])
        self.assertEqual(self.panel.cards['g1'].name_label.text(), 'g1')

    def test_thumbs_fall_back_to_manifest_in_carousel_then_detail_order(self):
        manifest = {'items': [
            dict(kind='carousel', seq=2, status='succeeded', file=self.images[1]),
            dict(kind='detail', seq=1, status='succeeded', file=self.images[3]),
            dict(kind='carousel', seq=1, status='succeeded', file=self.images[0]),
            dict(kind='carousel', seq=3, status='failed', file=self.images[2]),
            dict(kind='carousel', seq=4, status='succeeded', file=self.images[4]),
        ]}
        self.panel.update_groups([self.make_group(thumbs=[], manifest=manifest)])
        card = self.panel.cards['g1']
        self.assertEqual(card.thumb_count, 3)
        self.assertEqual(card.thumbs, [self.images[0], self.images[1], self.images[4]])

    def test_thumbs_are_capped_at_three_and_empty_slots_are_placeholders(self):
        five = self.make_group(thumbs=list(self.images))
        empty = self.make_group('empty', thumbs=[], manifest={})
        self.panel.update_groups([five, empty])
        self.assertEqual(self.panel.cards['g1'].thumb_count, 3)
        self.assertEqual(len(self.panel.cards['g1'].thumb_labels), 3)
        self.assertEqual(self.panel.cards['empty'].thumb_count, 0)
        self.assertEqual(len(self.panel.cards['empty'].thumb_labels), 3)

    def test_missing_group_keys_are_tolerated(self):
        self.panel.update_groups([dict(group_id='abcdefghijkl')])
        card = self.panel.cards['abcdefghijkl']
        self.assertEqual(card.name_label.text(), 'abcdefgh')
        self.assertEqual(card.desc_label.text(), tr('（未填写产品信息）'))
        self.assertEqual(card.model_label.text(), tr('未选模型'))
        self.assertEqual([chip.text() for chip in card.platform_chips],
                         [tr('未选平台')])
        self.assertEqual(card.thumb_count, 0)
        self.assertTrue(card.counts_label.isHidden())
        self.assertEqual(card.status_badge.text(), '')

    def test_interrupted_group_keeps_explicit_interruption_text(self):
        reason = tr('上次运行未正常结束（程序中断）')
        self.panel.update_groups([self.make_group(
            status='failed', error=reason, thumbs=[])])
        self.assertEqual(self.panel.cards['g1'].status_badge.text(), reason)

    def test_refresh_updates_cards_in_place_without_recreating_widgets(self):
        """生成中页面高频刷新：同一可见集合内必须复用卡片实例。"""
        group = self.make_group(thumbs=list(self.images[:1]))
        self.panel.update_groups([group])
        card = self.panel.cards['g1']
        refreshed = dict(group, status='failed', product_name='改名商品',
                         totals={'succeeded': 1, 'total': 2})
        self.panel.update_groups([refreshed])
        self.assertIs(self.panel.cards['g1'], card)
        self.assertEqual(card.name_label.text(), '改名商品')
        self.assertEqual(card.status_badge.text(), tr('失败'))
        self.assertEqual(card.thumb_count, 1)
        self.assertIn(card.group_id, self.panel.cards)

    def test_tab_switch_rebuilds_visible_card_set(self):
        """切 tab / 翻页是结构性变化：只保留当前可见集合的卡片。"""
        self.panel.update_groups([
            self.make_group('done-one', thumbs=[]),
            self.make_group('failed-one', status='failed', thumbs=[]),
        ])
        self.panel.set_tab('issue')
        self.assertEqual(list(self.panel.cards), ['failed-one'])
        self.panel.set_tab('done')
        self.assertEqual(list(self.panel.cards), ['done-one'])


class EcommerceTaskListFilterTests(EcommerceTaskListTestBase):
    def test_tab_counts_and_filtering(self):
        statuses = ('analyzing', 'generating', 'succeeded', 'partial',
                    'failed', 'cancelled')
        groups = [self.make_group(f'g{index}', status=status, thumbs=[])
                  for index, status in enumerate(statuses)]
        self.panel.update_groups(groups)
        self.assertEqual(self.panel.subtitle_label.text(),
                         tr('{count} 个制作任务').format(count=6))
        expected = {'all': 6, 'generating': 2, 'done': 1, 'issue': 3}
        for key, count in expected.items():
            self.assertEqual(self.panel.tab_buttons[key].count_label.text(),
                             str(count), key)
        self.assertEqual(self.panel.current_tab, 'all')

        self.panel.set_tab('generating')
        self.assertEqual(self.panel.current_tab, 'generating')
        self.assertEqual([group['group_id'] for group in self.panel.page_groups()],
                         ['g0', 'g1'])
        self.assertTrue(self.panel.tab_buttons['generating'].active)
        self.assertFalse(self.panel.tab_buttons['all'].active)

        self.panel.set_tab('done')
        self.assertEqual([group['group_id'] for group in self.panel.page_groups()],
                         ['g2'])
        self.panel.set_tab('issue')
        self.assertEqual([group['group_id'] for group in self.panel.page_groups()],
                         ['g3', 'g4', 'g5'])

    def test_tab_active_style_uses_palette_accent(self):
        self.panel.update_groups([self.make_group(thumbs=[])])
        accent = materials.palette()['accent']
        rgba = 'rgba(%d,%d,%d,%d)' % (
            int(accent[1:3], 16), int(accent[3:5], 16), int(accent[5:7], 16), 26)
        active = self.panel.tab_buttons['all']
        self.assertIn(rgba, active.styleSheet())
        self.assertNotIn(rgba, self.panel.tab_buttons['done'].styleSheet())

    def test_tab_buttons_switch_by_click(self):
        self.panel.update_groups([self.make_group(status='succeeded', thumbs=[]),
                                 self.make_group('g2', status='failed', thumbs=[])])
        self.panel.tab_buttons['issue'].click()
        self.assertEqual(self.panel.current_tab, 'issue')
        self.assertEqual([group['group_id'] for group in self.panel.page_groups()],
                         ['g2'])

    def test_empty_filter_state_has_explicit_text(self):
        self.panel.update_groups([self.make_group(thumbs=[])])
        self.panel.set_tab('generating')
        self.assertFalse(self.panel.empty_label.isHidden())
        self.assertEqual(self.panel.empty_label.text(), tr('当前筛选下暂无任务'))


class EcommerceTaskListPaginationTests(EcommerceTaskListTestBase):
    def test_pagination_navigation_and_page_offsets(self):
        groups = [self.make_group(f'g{index:02d}', thumbs=[])
                  for index in range(12)]
        self.panel.update_groups(groups)
        self.assertEqual(self.panel.page_count, 2)
        self.assertEqual(len(self.panel.cards), 10)
        self.assertEqual(self.panel.page_label.text(),
                         tr('第 {current} / {total} 页').format(current=1, total=2))
        self.assertFalse(self.panel.prev_button.isEnabled())
        self.assertTrue(self.panel.next_button.isEnabled())
        self.assertEqual(self.panel.cards['g00'].index_label.text(), '1')
        self.assertEqual(self.panel.cards['g09'].index_label.text(), '10')

        self.panel.next_button.click()
        self.assertEqual(self.panel.page_index, 1)
        self.assertEqual(len(self.panel.cards), 2)
        self.assertEqual(list(self.panel.cards), ['g10', 'g11'])
        self.assertEqual(self.panel.cards['g10'].index_label.text(), '11')
        self.assertEqual(self.panel.page_label.text(),
                         tr('第 {current} / {total} 页').format(current=2, total=2))
        self.assertTrue(self.panel.prev_button.isEnabled())
        self.assertFalse(self.panel.next_button.isEnabled())

        self.panel.prev_button.click()
        self.assertEqual(len(self.panel.cards), 10)
        self.assertEqual(self.panel.page_index, 0)

    def test_page_clamped_when_groups_shrink(self):
        groups = [self.make_group(f'g{index:02d}', thumbs=[]) for index in range(12)]
        self.panel.update_groups(groups)
        self.panel.next_button.click()
        self.panel.update_groups(groups[:3])
        self.assertEqual(self.panel.page_index, 0)
        self.assertEqual(self.panel.page_label.text(),
                         tr('第 {current} / {total} 页').format(current=1, total=1))


class EcommerceTaskListManageModeTests(EcommerceTaskListTestBase):
    def manage_group(self, group_id='g1'):
        return self.make_group(group_id, thumbs=list(self.images[:1]))

    def test_manage_mode_toggles_header_buttons_and_checkboxes(self):
        self.panel.update_groups([self.manage_group()])
        self.assertFalse(self.panel.manage_button.isHidden())
        self.assertTrue(self.panel.cancel_button.isHidden())
        self.assertFalse(self.panel.refresh_button.isHidden())
        self.assertTrue(self.panel.bottom_bar.isHidden())

        self.panel.manage_button.click()
        self.assertTrue(self.panel.manage_mode)
        self.assertTrue(self.panel.manage_button.isHidden())
        self.assertFalse(self.panel.cancel_button.isHidden())
        self.assertTrue(self.panel.refresh_button.isHidden())
        self.assertFalse(self.panel.batch_create_button.isHidden())
        self.assertFalse(self.panel.new_create_button.isHidden())
        self.assertFalse(self.panel.bottom_bar.isHidden())
        self.assertFalse(self.panel.cards['g1'].checkbox.isHidden())

        self.panel.cancel_button.click()
        self.assertFalse(self.panel.manage_mode)
        self.assertFalse(self.panel.manage_button.isHidden())
        self.assertTrue(self.panel.cards['g1'].checkbox.isHidden())

    def test_select_page_clear_and_selection_survives_navigation(self):
        groups = [self.manage_group(f'g{index:02d}') for index in range(12)]
        self.panel.update_groups(groups)
        self.panel.set_manage_mode(True)
        self.panel._select_current_page()
        self.assertEqual(len(self.panel.selected_ids()), 10)
        self.assertEqual(self.panel.selected_label.text(),
                         tr('已选 {selected} / {total}').format(selected=10, total=12))
        self.assertEqual(self.panel.delete_selected_button.text(),
                         tr('删除 {count} 个').format(count=10))
        self.assertTrue(self.panel.delete_selected_button.isEnabled())

        self.panel.next_button.click()
        self.assertEqual(len(self.panel.selected_ids()), 10)
        self.panel._select_current_page()
        self.assertEqual(len(self.panel.selected_ids()), 12)

        self.panel.clear_selection_button.click()
        self.assertEqual(self.panel.selected_ids(), [])
        self.assertFalse(self.panel.delete_selected_button.isEnabled())

        self.panel.prev_button.click()
        self.panel.cards['g03'].checkbox.setChecked(True)
        self.assertEqual(self.panel.selected_ids(), ['g03'])
        self.panel.set_tab('done')
        self.assertEqual(self.panel.selected_ids(), ['g03'])
        self.panel.set_tab('all')
        self.assertEqual(self.panel.selected_ids(), ['g03'])
        self.assertTrue(self.panel.cards['g03'].selected)
        self.assertFalse(self.panel.cards['g00'].selected)

    def test_leaving_manage_mode_clears_selection(self):
        self.panel.update_groups([self.manage_group()])
        self.panel.set_manage_mode(True)
        self.panel.cards['g1'].checkbox.setChecked(True)
        self.assertEqual(self.panel.selected_ids(), ['g1'])
        self.panel.set_manage_mode(False)
        self.assertEqual(self.panel.selected_ids(), [])

    def test_selected_card_highlight_uses_accent(self):
        self.panel.update_groups([self.manage_group()])
        self.panel.set_manage_mode(True)
        card = self.panel.cards['g1']
        card.set_selected(True)
        self.assertTrue(card.selected)
        accent = materials.palette()['accent']
        self.assertEqual(materials.accent_color(), accent)
        # 选中态与未选中态的绘制分支不同：通过 update 后的内部标记验证。
        card.set_selected(False)
        self.assertFalse(card.selected)


class EcommerceTaskListDeleteTests(EcommerceTaskListTestBase):
    def test_single_delete_requires_confirmation(self):
        self.panel.update_groups([self.make_group(thumbs=[])])
        card = self.panel.cards['g1']
        spy = QSignalSpy(self.panel.delete_requested)
        with patch.object(self.panel, 'confirm_delete', return_value=False):
            card.delete_button.click()
        self.assertEqual(len(spy), 0)

        captured = []

        def confirm(count):
            captured.append(count)
            return True

        with patch.object(self.panel, 'confirm_delete', side_effect=confirm):
            card.delete_button.click()
        self.assertEqual(len(spy), 1)
        self.assertEqual(list(spy[0][0]), ['g1'])
        self.assertEqual(captured, [1])

    def test_confirm_dialog_texts_are_exact(self):
        dialog, confirm_button, cancel_button = self.panel._build_delete_dialog(1)
        try:
            first, second = dialog.delete_message_labels
            self.assertEqual(first.text(), tr('删除该记录？'))
            self.assertEqual(second.text(),
                             tr('此操作不可恢复，生成完成的图片不受影响'))
            self.assertEqual(confirm_button.text(), tr('删除'))
            self.assertEqual(cancel_button.text(), tr('取消'))
        finally:
            dialog.close()
            dialog.deleteLater()

        dialog, _confirm, _cancel = self.panel._build_delete_dialog(3)
        try:
            self.assertEqual(dialog.delete_message_labels[0].text(),
                             tr('确认删除选中的 {count} 个任务？').format(count=3))
        finally:
            dialog.close()
            dialog.deleteLater()
        self.app.processEvents()

    def test_batch_delete_emits_selected_ids_once(self):
        groups = [self.make_group(f'g{index}', thumbs=[]) for index in range(3)]
        self.panel.update_groups(groups)
        self.panel.set_manage_mode(True)
        self.panel.cards['g0'].checkbox.setChecked(True)
        self.panel.cards['g2'].checkbox.setChecked(True)
        spy = QSignalSpy(self.panel.delete_requested)
        captured = []

        def confirm(count):
            captured.append(count)
            return True

        with patch.object(self.panel, 'confirm_delete', side_effect=confirm):
            self.panel.delete_selected_button.click()
        self.assertEqual(captured, [2])
        self.assertEqual(len(spy), 1)
        self.assertEqual(list(spy[0][0]), ['g0', 'g2'])
        self.assertEqual(self.panel.selected_ids(), [])

    def test_busy_panel_never_emits_delete(self):
        self.panel.update_groups([self.make_group(thumbs=[])])
        self.panel.set_manage_mode(True)
        self.panel.cards['g1'].checkbox.setChecked(True)
        self.panel.set_busy(True)
        spy = QSignalSpy(self.panel.delete_requested)
        with patch.object(self.panel, 'confirm_delete', return_value=True) as confirm:
            self.panel.delete_selected_button.click()
        self.assertEqual(len(spy), 0)
        self.assertFalse(confirm.called)


class EcommerceTaskListActionTests(EcommerceTaskListTestBase):
    def test_overflow_menu_emits_resume_and_open(self):
        self.panel.update_groups([self.make_group(status='partial', thumbs=[])])
        card = self.panel.cards['g1']
        resume = QSignalSpy(self.panel.resume_requested)
        opened = QSignalSpy(self.panel.open_requested)
        self.assertIn(tr('继续生成（断点恢复）'), card.resume_action.text())
        self.assertIn(tr('打开文件夹'), card.open_action.text())
        card.resume_action.trigger()
        card.open_action.trigger()
        self.assertEqual(list(resume[0]), ['g1'])
        self.assertEqual(list(opened[0]), ['g1'])

    def test_resume_action_only_enabled_for_resumable_statuses(self):
        self.panel.update_groups([
            self.make_group('partial', status='partial', thumbs=[]),
            self.make_group('done', status='succeeded', thumbs=[]),
        ])
        self.assertTrue(self.panel.cards['partial'].resume_action.isEnabled())
        self.assertFalse(self.panel.cards['done'].resume_action.isEnabled())
        self.assertIn(self.panel.cards['partial'].group['status'], RESUMABLE_STATUSES)

    def test_view_preview_and_delete_buttons_emit(self):
        self.panel.update_groups([self.make_group(thumbs=[])])
        card = self.panel.cards['g1']
        view = QSignalSpy(self.panel.view_requested)
        preview = QSignalSpy(self.panel.preview_requested)
        card.view_button.click()
        card.preview_button.click()
        self.assertEqual(list(view[0]), ['g1'])
        self.assertEqual(list(preview[0]), ['g1'])

    def test_busy_disables_card_actions(self):
        self.panel.update_groups([self.make_group(status='partial', thumbs=[])])
        card = self.panel.cards['g1']
        self.panel.set_busy(True)
        for button in (card.view_button, card.preview_button, card.delete_button,
                       card.more_button):
            self.assertFalse(button.isEnabled())
        self.assertFalse(card.resume_action.isEnabled())
        self.assertFalse(card.open_action.isEnabled())
        self.assertFalse(card.checkbox.isEnabled())
        self.panel.set_manage_mode(True)
        self.assertFalse(self.panel.delete_selected_button.isEnabled())
        self.panel.set_busy(False)
        self.assertTrue(card.view_button.isEnabled())
        self.assertTrue(card.resume_action.isEnabled())
        self.assertTrue(card.checkbox.isEnabled())

    def test_header_actions_emit(self):
        refresh = QSignalSpy(self.panel.refresh_requested)
        batch = QSignalSpy(self.panel.batch_create_requested)
        new = QSignalSpy(self.panel.new_create_requested)
        self.panel.refresh_button.click()
        self.panel.batch_create_button.click()
        self.panel.new_create_button.click()
        self.assertEqual(len(refresh), 1)
        self.assertEqual(len(batch), 1)
        self.assertEqual(len(new), 1)

    def test_header_button_texts(self):
        self.assertEqual(self.panel.title_label.text(), tr('电商图任务'))
        self.assertEqual(self.panel.manage_button.text(), tr('管理'))
        self.assertEqual(self.panel.batch_create_button.text(), tr('批量制作'))
        self.assertEqual(self.panel.new_create_button.text(), tr('+ 新建制作'))
        self.assertEqual(self.panel.refresh_button.text(), tr('刷新'))
        self.assertEqual(self.panel.select_page_button.text(), tr('全选本页'))
        self.assertEqual(self.panel.clear_selection_button.text(), tr('清空'))
        self.assertEqual(self.panel.prev_button.text(), tr('上一页'))
        self.assertEqual(self.panel.next_button.text(), tr('下一页'))


class EcommerceTaskListStateTests(EcommerceTaskListTestBase):
    def test_empty_state_and_unavailable_database_notice(self):
        self.panel.update_groups([], persistence_available=False)
        self.assertFalse(self.panel.empty_label.isHidden())
        self.assertEqual(self.panel.empty_label.text(),
                         tr('暂无任务，点「+ 新建制作」开始'))
        self.assertFalse(self.panel.notice_label.isHidden())
        self.assertEqual(self.panel.notice_label.text(),
                         tr('数据库不可用，仅展示本次会话结果'))
        self.assertEqual(self.panel.subtitle_label.text(),
                         tr('{count} 个制作任务').format(count=0))
        self.assertEqual(self.panel.page_label.text(),
                         tr('第 {current} / {total} 页').format(current=1, total=1))

        self.panel.update_groups([self.make_group(thumbs=[])], persistence_available=True)
        self.assertTrue(self.panel.empty_label.isHidden())
        self.assertTrue(self.panel.notice_label.isHidden())

    def test_retranslate_ui_rebuilds_copy_without_raising(self):
        self.panel.update_groups([self.make_group(thumbs=[])])
        set_language('en-US')
        try:
            self.panel.retranslate_ui()
            self.assertEqual(self.panel.title_label.text(), tr('电商图任务'))
            self.assertEqual(self.panel.tab_buttons['generating'].name_label.text(),
                             tr('生成中'))
            self.assertEqual(self.panel.manage_button.text(), tr('管理'))
            self.assertEqual(len(self.panel.cards), 1)
        finally:
            set_language('zh-CN')
            self.panel.retranslate_ui()

    def test_narrow_window_does_not_force_horizontal_overflow(self):
        groups = [self.make_group(f'g{index:02d}', thumbs=list(self.images[:3]))
                  for index in range(10)]
        self.panel.update_groups(groups)
        self.panel.resize(900, 760)
        self.panel.show()
        self.app.processEvents()
        self.assertLessEqual(self.panel.minimumSizeHint().width(), 960)
        self.assertGreater(self.panel.minimumSizeHint().height(), 0)
        for card in self.panel.cards.values():
            self.assertGreaterEqual(card.thumb_count, 0)
        self.panel.hide()


if __name__ == '__main__':
    unittest.main()

