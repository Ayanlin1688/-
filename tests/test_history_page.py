"""Standalone image tasks join history without changing video/ecommerce rows."""
from datetime import datetime
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QLabel
from qfluentwidgets import TransparentToolButton

from core.config_manager import ConfigManager
from core.i18n import set_language, tr
from ui.pages.history_page import HistoryPage


class _ManualStore:
    def __init__(self, records):
        self.records = {record['local_id']: dict(record) for record in records}

    def list(self, limit=200):
        return sorted(self.records.values(), key=lambda item: item.get('created_at', 0), reverse=True)[:limit]

    def delete(self, local_id):
        return self.records.pop(local_id, None) is not None


class _HistoryNotifier(QObject):
    changed = pyqtSignal()


class HistoryPageManualImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        set_language('zh-CN')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = ConfigManager(self.root / 'config.json')
        self.output = self.root / 'output'
        self.output.mkdir()
        self.config.config['paths']['output'] = str(self.output)
        self.image = self.output / 'result.png'
        self.image.write_bytes(b'generated-image')
        self.output_dir = self.output / 'task'
        self.output_dir.mkdir()
        self.manual_store = _ManualStore([self.make_record()])
        self.page = HistoryPage(Mock(), self.config)
        self.page._manual_store = lambda: self.manual_store
        self.page.refresh_records()
        self.addCleanup(self._cleanup_page)

    def _cleanup_page(self):
        for worker in list(self.page.jobs.workers):
            worker.wait(3000)
        self.page.deleteLater()
        QTest.qWait(20)

    def make_record(self, **changes):
        record = dict(
            local_id='manual-1', kind='image', prompt='一张白色陶瓷茶杯商品图',
            prompt_name='白色陶瓷茶杯', model='gpt-image-2.5',
            model_version='gpt-image-2.5-sunburst-vip', ratio='1:1', tier='1K',
            count_index=1, status='succeeded', protocol='openai',
            files=[str(self.image)], error='', created_at=datetime(2026, 10, 24, 12).timestamp(),
            finished_at=datetime(2026, 10, 24, 12, 0, 3).timestamp(), duration_ms=3000,
            output_dir=str(self.output_dir),
        )
        record.update(changes)
        return record

    def row_for(self, local_id):
        return next(row for row, record in enumerate(self.page.records)
                    if record.get('local_id') == local_id)

    def test_manual_image_records_merge_filter_and_type_tag(self):
        task = next(record for record in self.page.records if record.get('kind') == 'manual_image')
        self.assertEqual(task['local_id'], 'image:manual-1')
        self.assertEqual(task['product'], '白色陶瓷茶杯')
        self.assertEqual(task['prompt_name'], '一张白色陶瓷茶杯商品图')
        self.assertEqual(task['model'], 'gpt-image-2.5-sunburst-vip')
        self.assertEqual(task['size_bytes'], os.path.getsize(self.image))
        self.assertEqual(task['status'], 'completed')
        self.assertEqual(self.page._type_text(task), tr('图片生成'))
        row = self.row_for('image:manual-1')
        tag = self.page.table.cellWidget(row, 2).findChild(QLabel)
        self.assertEqual(tag.text(), tr('图片生成'))

        self.page.type_filter_box.setCurrentText(tr('图片生成'))
        self.assertEqual([record['local_id'] for row, record in enumerate(self.page.records)
                          if not self.page.table.isRowHidden(row)], ['image:manual-1'])
        self.page.type_filter_box.setCurrentText(tr('视频'))
        self.assertTrue(all(self.page.table.isRowHidden(row)
                            for row in range(self.page.table.rowCount())))
        self.page.type_filter_box.setCurrentText(tr('全部类型'))

    def test_manual_image_actions_open_paths_and_delete_record_only(self):
        row = self.row_for('image:manual-1')
        action = self.page.table.cellWidget(row, 10)
        open_file = action.findChild(TransparentToolButton, 'manualImageOpenFile')
        open_folder = action.findChild(TransparentToolButton, 'manualImageOpenFolder')
        delete = action.findChild(TransparentToolButton, 'manualImageDelete')
        self.assertTrue(open_file.isEnabled())
        self.assertTrue(open_folder.isEnabled())
        self.assertTrue(delete.isEnabled())
        with patch('ui.pages.history_page.open_local') as open_local:
            open_file.click()
            open_local.assert_called_with(str(self.image), self.page.log_callback)
            open_folder.click()
            open_local.assert_called_with(str(self.output_dir), self.page.log_callback, folder=True)

        delete.click()
        self.assertNotIn('manual-1', self.manual_store.records)
        self.assertTrue(self.image.is_file())
        self.assertFalse(any(record.get('kind') == 'manual_image' for record in self.page.records))

    def test_active_manual_task_is_not_selectable_for_deletion(self):
        self.manual_store.records['manual-1'].update(status='running', finished_at=None)
        self.page.refresh_records()
        row = self.row_for('image:manual-1')
        self.assertFalse(self.page.table.cellWidget(row, 0).findChild(type(self.page.select_all)).isEnabled())
        self.assertFalse(self.page.table.cellWidget(row, 10).findChild(
            TransparentToolButton, 'manualImageDelete').isEnabled())

    def test_manual_video_records_filter_copy_and_delete_without_removing_file(self):
        video = self.output / 'result.mp4'
        video.write_bytes(b'video-fixture')
        video_store = _ManualStore([dict(
            local_id='video-1', kind='video', prompt='A quiet product reveal',
            prompt_name='A quiet product reveal', model='video-v2', status='succeeded',
            video_url='https://relay.example/video.mp4', file_path=str(video),
            output_dir=str(self.output_dir), task_id='remote-1', duration=5,
            output_size='1920x1088', created_at=datetime(2026, 10, 24, 12, 1).timestamp(),
            finished_at=datetime(2026, 10, 24, 12, 1, 5).timestamp(),
        )])
        self.page._manual_video_store = lambda: video_store
        self.page.refresh_records()

        task = next(record for record in self.page.records if record.get('kind') == 'manual_video')
        self.assertEqual(task['local_id'], 'video:video-1')
        self.assertEqual(task['product'], 'A quiet product reveal')
        self.assertEqual(task['task_id'], 'remote-1')
        self.assertEqual(task['size_bytes'], video.stat().st_size)
        row = self.row_for('video:video-1')
        self.assertNotEqual(self.page.table.item(row, 8).text(), '—')
        self.assertNotEqual(self.page.table.item(row, 9).text(), '—')
        self.assertEqual(self.page._type_text(task), tr('视频生成'))
        self.page.type_filter_box.setCurrentText(tr('视频生成'))
        visible = [record for row, record in enumerate(self.page.records)
                   if not self.page.table.isRowHidden(row)]
        self.assertEqual([record['local_id'] for record in visible], ['video:video-1'])

        row = self.row_for('video:video-1')
        actions = self.page.table.cellWidget(row, 10)
        copy_link = actions.findChild(TransparentToolButton, 'manualVideoCopyLink')
        delete = actions.findChild(TransparentToolButton, 'manualVideoDelete')
        self.assertTrue(copy_link.isEnabled())
        self.assertTrue(delete.isEnabled())
        with patch('ui.pages.history_page.InfoBar.success') as success:
            copy_link.click()
        self.assertEqual(self.app.clipboard().text(), 'https://relay.example/video.mp4')
        success.assert_called_once()

        delete.click()
        self.assertNotIn('video-1', video_store.records)
        self.assertTrue(video.is_file())
        self.assertFalse(any(record.get('kind') == 'manual_video'
                             for record in self.page.records))

    def test_manual_video_downloading_state_and_persisted_size_are_mapped(self):
        expected_size = 5 * 1024 * 1024
        video_store = _ManualStore([dict(
            local_id='video-downloading', kind='video', prompt='Downloading clip',
            prompt_name='Downloading clip', model='video-v2', status='downloading',
            video_url='https://relay.example/video.mp4', file_path='', size_bytes=expected_size,
            created_at=datetime(2026, 10, 24, 12, 2).timestamp(), finished_at=None,
        )])
        self.page._manual_video_store = lambda: video_store
        self.page.refresh_records()
        task = next(record for record in self.page.records
                    if record.get('kind') == 'manual_video')
        self.assertEqual(task['status'], 'downloading')
        self.assertEqual(task['manual_status'], 'downloading')
        self.assertEqual(task['size_bytes'], expected_size)
        row = self.row_for('video:video-downloading')
        status_tag = self.page.table.cellWidget(row, 6).findChild(QLabel)
        self.assertEqual(status_tag.text(), tr('下载中'))
        self.assertEqual(self.page.table.item(row, 9).text(), '5.0 MB')

    def test_video_completion_signal_refreshes_history_without_manual_refresh(self):
        from core.manual_video import ManualVideoStore

        store_path = self.root / 'background-video-history.sqlite3'
        video_store = ManualVideoStore(store_path)
        self.addCleanup(video_store.close)
        self.page._manual_video_store = lambda: video_store
        self.page.refresh_records()
        self.assertFalse(self.page.isVisible())
        notifier = _HistoryNotifier()
        notifier.changed.connect(self.page.refresh_records)
        video_store.upsert(dict(
            local_id='video-auto', kind='video', prompt='Auto history clip',
            prompt_name='Auto history clip', model='video-v2', status='succeeded',
            video_url='https://relay.example/video.mp4', file_path='',
            output_dir=str(self.output_dir), task_id='remote-auto', duration=5,
            output_size='1920x1088', size_bytes=12,
            created_at=datetime(2026, 10, 24, 12, 3).timestamp(),
            finished_at=datetime(2026, 10, 24, 12, 3, 1).timestamp(),
        ))
        reopened = ManualVideoStore(store_path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get('video-auto')['status'], 'succeeded')
        notifier.changed.emit()
        QTest.qWait(20)
        task = next(record for record in self.page.records
                    if record.get('local_id') == 'video:video-auto')
        row = self.row_for('video:video-auto')
        self.assertIn('12:03', self.page.table.item(row, 8).text())
        self.assertEqual(task['size_bytes'], 12)

    def test_manual_video_and_image_rows_share_tag_and_action_geometry(self):
        video_store = _ManualStore([dict(
            local_id='video-style', kind='video', prompt='Style clip',
            prompt_name='Style clip', model='video-v2', status='succeeded',
            video_url='https://relay.example/video.mp4', file_path='',
            output_dir=str(self.output_dir), task_id='remote-style',
            created_at=datetime(2026, 10, 24, 12, 4).timestamp(),
            finished_at=datetime(2026, 10, 24, 12, 4, 1).timestamp(),
        )])
        self.page._manual_video_store = lambda: video_store
        self.page.refresh_records()
        image_row = self.row_for('image:manual-1')
        video_row = self.row_for('video:video-style')
        image_tag = self.page.table.cellWidget(image_row, 2).findChild(QLabel)
        video_tag = self.page.table.cellWidget(video_row, 2).findChild(QLabel)
        self.assertEqual(image_tag.objectName(), video_tag.objectName())
        self.assertEqual(self.page.table.rowHeight(image_row), self.page.table.rowHeight(video_row))
        image_actions = self.page.table.cellWidget(image_row, 10).layout()
        video_actions = self.page.table.cellWidget(video_row, 10).layout()
        self.assertEqual(image_actions.spacing(), video_actions.spacing())


if __name__ == '__main__':
    unittest.main()
