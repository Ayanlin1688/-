"""Offline integration checks for ecommerce records in the main history table."""
from datetime import datetime
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import Qt
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtWidgets import QApplication, QLabel
from qfluentwidgets import CheckBox, TransparentToolButton

from core.config_manager import ConfigManager
from core.i18n import set_language, tr
from ui.pages.history_page import HistoryPage


class HistoryEcommerceMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        set_language('zh-CN')
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = ConfigManager(self.root / 'config.json')
        self.output = self.root / 'output'
        self.output.mkdir()
        self.config.config['paths']['output'] = str(self.output)
        self.store = self.config.ecommerce_store()
        self.page = HistoryPage(Mock(), self.config)

    def tearDown(self):
        for worker in list(self.page.jobs.workers):
            worker.wait(3000)
        self.page.deleteLater()
        QTest.qWait(20)
        self.temp.cleanup()

    @staticmethod
    def stamp(day):
        return datetime(2026, 10, day, 12, 0, 0).timestamp()

    def add_group(self, group_id, status, day, *, product='', carousel=2, detail=1,
                  platforms=None, items=None):
        output_dir = self.output / group_id
        output_dir.mkdir()
        manifest = dict(carousel_count=carousel, detail_count=detail,
                        analysis_model='chat-fixture', image_model='image-fixture')
        self.store.upsert_group(dict(
            group_id=group_id, created_at=self.stamp(day), updated_at=self.stamp(day) + 1,
            status=status, product_name=product,
            platforms=platforms or [dict(id='amazon', name='Amazon')],
            config=dict(carousel_count=carousel, detail_count=detail,
                        analysis_model='chat-fixture', image_model='image-fixture'),
            output_dir=str(output_dir), manifest=manifest,
            totals=dict(succeeded=0, failed=0, skipped=0, total=0), error=''))
        for item in items or []:
            self.store.upsert_item(dict(group_id=group_id, **item))
        return output_dir

    def add_video(self, local_id, day):
        self.config.history_upsert(dict(
            local_id=local_id, product='视频产品', prompt_name='视频任务', model='video-fixture',
            status='completed', created_at=datetime(2026, 10, day, 12).isoformat(),
            finished_at='', size_bytes=12, task_id='', result_path='', output_dir=''))

    def row_for(self, local_id):
        return next(row for row, record in enumerate(self.page.records)
                    if record.get('local_id') == local_id)

    def test_merge_sort_type_status_filters_size_and_in_place_update(self):
        image_a = self.root / 'carousel.png'
        image_b = self.root / 'detail.png'
        image_a.write_bytes(b'a' * 17)
        image_b.write_bytes(b'b' * 23)
        self.add_group('partial-group', 'partial', 18, product='茶杯', carousel=2, detail=1,
                       platforms=[dict(id='amazon', name='Amazon'), dict(id='etsy', name='Etsy')],
                       items=[dict(kind='carousel', seq=1, status='succeeded', file_path=str(image_a)),
                              dict(kind='detail', seq=1, status='succeeded', file_path=str(image_b))])
        self.add_group('running-group', 'generating', 17, product='待生成')
        self.add_group('failed-group', 'failed', 16)
        self.add_group('success-group', 'succeeded', 15)
        self.add_video('video-record', 19)
        self.page.refresh_records()

        self.assertEqual([r['local_id'] for r in self.page.records], [
            'video-record', 'ec:partial-group', 'ec:running-group',
            'ec:failed-group', 'ec:success-group'])
        self.assertEqual(self.page.table.horizontalHeaderItem(2).text(), tr('类型'))
        partial_row = self.row_for('ec:partial-group')
        self.assertEqual(self.page.records[partial_row]['prompt_name'], 'Amazon、Etsy（轮2/详1）')
        self.assertEqual(self.page.records[partial_row]['size_bytes'], 40)
        self.assertEqual(self.page.table.item(partial_row, 9).text(), '0.0 MB')
        type_tag = self.page.table.cellWidget(partial_row, 2).findChild(QLabel)
        self.assertEqual(type_tag.text(), tr('电商图制作'))
        video_row = self.row_for('video-record')
        self.assertEqual(self.page.table.cellWidget(video_row, 2).findChild(QLabel).text(), tr('自动化视频'))
        self.assertEqual(self.page.records[self.row_for('ec:success-group')]['product'], '')
        self.assertTrue(self.page.records[partial_row]['local_id'].startswith('ec:'))
        self.assertNotEqual(self.page.records[partial_row]['local_id'], 'partial-group')

        running_checkbox = self.page.table.cellWidget(self.row_for('ec:running-group'), 0).findChild(CheckBox)
        self.assertFalse(running_checkbox.isEnabled())
        self.assertIn('运行', running_checkbox.toolTip())

        self.page.type_filter_box.setCurrentText(tr('电商图制作'))
        self.page.filter_box.setCurrentText(tr('部分完成'))
        self.assertEqual([r['local_id'] for row, r in enumerate(self.page.records)
                          if not self.page.table.isRowHidden(row)], ['ec:partial-group'])
        self.page.filter_box.setCurrentText(tr('全部状态'))
        self.assertEqual(sum(not self.page.table.isRowHidden(row)
                             for row in range(self.page.table.rowCount())), 4)
        self.page.type_filter_box.setCurrentText(tr('全部类型'))

        view_spy = QSignalSpy(self.page.view_ecommerce_group)
        partial_row = self.row_for('ec:partial-group')
        view = self.page.table.cellWidget(partial_row, 10).findChild(
            TransparentToolButton, 'ecommerceViewResult')
        action_buttons = self.page.table.cellWidget(partial_row, 10).findChildren(TransparentToolButton)
        self.assertEqual(len(action_buttons), 2)
        folder = self.page.table.cellWidget(partial_row, 10).findChild(
            TransparentToolButton, 'ecommerceOpenFolder')
        with patch('ui.pages.history_page.open_local') as open_local:
            folder.click()
            open_local.assert_called_once_with(
                self.page.records[partial_row]['output_dir'], self.page.log_callback, folder=True)
        view.click()
        self.assertEqual(list(view_spy[0]), ['partial-group'])

        checkbox = self.page.table.cellWidget(partial_row, 0).findChild(CheckBox)
        action_host = self.page.table.cellWidget(partial_row, 10)
        self.store.upsert_group(dict(group_id='partial-group', status='succeeded', updated_at=self.stamp(18) + 2))
        self.page.refresh_records()
        updated_row = self.row_for('ec:partial-group')
        self.assertIs(self.page.table.cellWidget(updated_row, 0).findChild(CheckBox), checkbox)
        self.assertIs(self.page.table.cellWidget(updated_row, 10), action_host)
        self.assertEqual(self.page.table.cellWidget(updated_row, 6).findChild(QLabel).text(), tr('已完成'))

    def test_ecommerce_view_button_and_database_only_cascade_delete(self):
        output_dir = self.add_group('deletable-group', 'partial', 18)
        generated = output_dir / '轮1.png'
        generated.write_bytes(b'generated image bytes')
        self.page.refresh_records()
        row = self.row_for('ec:deletable-group')
        self.page.table.cellWidget(row, 0).findChild(CheckBox).setChecked(True)
        deleted = []

        def delete_groups(group_ids):
            deleted.extend(group_ids)
            for group_id in group_ids:
                self.store.delete_group(group_id)
            self.page.refresh_records()

        self.page.ecommerce_delete_requested.connect(delete_groups)
        dialog = Mock()
        dialog.has_uncertain = False
        dialog.exec.return_value = 1
        dialog.delete_files.isChecked.return_value = True
        with patch('ui.pages.history_page.DeleteHistoryDialog', return_value=dialog) as create_dialog:
            self.page._request_delete()
            deadline = time.monotonic() + 4
            while self.page.jobs.busy and time.monotonic() < deadline:
                QTest.qWait(10)
            QTest.qWait(30)

        self.assertEqual(deleted, ['deletable-group'])
        self.assertEqual(create_dialog.call_args.args[0][0]['prompt_name'], 'Amazon（轮2/详1）')
        self.assertEqual(dialog.keep_files.setText.call_args.args[0], tr('仅删除记录，不删除已生成文件'))
        self.assertFalse(create_dialog.return_value.delete_files.setEnabled.call_args.args[0])
        self.assertIsNone(self.store.get_group('deletable-group'))
        self.assertEqual(self.store.list_items('deletable-group'), [])
        self.assertEqual(generated.read_bytes(), b'generated image bytes')
        self.assertNotIn('ec:deletable-group', [r['local_id'] for r in self.page.records])


if __name__ == '__main__':
    unittest.main()
