"""Offline UI coverage for the standalone video creation page."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtWidgets import QApplication, QBoxLayout, QFrame, QToolButton
from PyQt5.QtGui import QPixmap
from PyQt5.QtTest import QTest
from qfluentwidgets import PushButton, ToolButton

from core.config_manager import ConfigManager
from core.i18n import set_language
from tests.qt_scale_support import flush_layout, reset_interface_scaling
from tests.test_interface_scaling import FakeScreen
from ui.scaling import apply_interface_scale
from ui.tokens import px
from ui.pages.video_creation_page import VideoCreationPage, _HistoryRow


class VideoCreationPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        set_language('zh-CN')
        reset_interface_scaling(self.app)
        self.addCleanup(reset_interface_scaling, self.app)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = ConfigManager(self.root / 'config.json')
        controller = Mock()
        controller.snapshot_for.return_value = {
            'video-v2': {
                'id': 'video-v2', 'display_name': 'Video V2', 'kind': 'video',
                'available': True, 'ratios': ['9:16', '16:9', '1:1'],
                'resolutions': ['1088x1920', '1920x1088', '1024x1024'],
            },
            'veo-3': {
                'id': 'veo-3', 'display_name': 'Veo 3', 'kind': 'video',
                'available': True, 'ratios': ['16:9'],
                'resolutions': ['1920x1088'],
            },
        }
        self.config.model_catalog_controller = controller
        self.page = VideoCreationPage(self.config)
        self.page.resize(1440, 900)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.shutdown_workers(wait_ms=2000)
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()
        set_language('zh-CN')

    def test_header_models_and_api_status(self):
        from ui.materials import ecommerce_color

        self.assertEqual(self.page.title_label.text(), '视频创作')
        self.assertEqual(self.page.caption_label.text(), '把想法写成画面，从这里开始。')
        self.assertGreaterEqual(self.page.model_combo.count(), 2)
        self.assertEqual(self.page.model_combo.currentData(), 'video-v2')
        self.assertEqual(self.page.model_combo.toolTip(), 'video-v2')
        self.assertEqual(self.page.api_status.text(), '未配置')
        self.assertIn(ecommerce_color('video_creation_error').lower(), self.page.api_status.styleSheet().lower())
        self.page.api_status.setText('已配置')
        self.page._style_api_status()
        self.assertIn(ecommerce_color('video_creation_success').lower(), self.page.api_status.styleSheet().lower())
        self.assertEqual(self.page.preview_title.text(), '📄 作品预览')
        self.assertIn('温馨提示', self.page.info_bar.text())

    def test_generation_parameters_can_collapse(self):
        self.assertTrue(self.page.parameters_content.isVisible())
        self.page.parameters_toggle.click()
        self.app.processEvents()
        self.assertFalse(self.page.parameters_content.isVisible())
        self.assertEqual(self.page.parameters_toggle.toolTip(), '展开')
        self.page.parameters_toggle.click()
        self.assertTrue(self.page.parameters_content.isVisible())
        self.assertEqual(self.page.parameters_toggle.toolTip(), '收起')

    def test_duration_ratio_size_count_controls(self):
        self.assertEqual((self.page.duration_spin.minimum(), self.page.duration_spin.maximum()), (4, 15))
        self.page.duration_buttons[1].click()
        self.assertEqual(self.page.duration_spin.value(), 10)
        self.assertTrue(self.page.duration_buttons[1].isChecked())
        self.page.ratio_combo.setCurrentText('16:9')
        self.assertEqual(self.page.output_size_combo.currentText(), '1920x1088')
        self.page.ratio_combo.setCurrentText('1:1')
        self.assertEqual(self.page.output_size_combo.currentText(), '1024x1024')
        self.assertEqual(self.page.count_spin.value(), 1)
        self.assertFalse(self.page.count_spin.minus_button.isEnabled())
        self.page.count_spin.setValue(10)
        self.assertFalse(self.page.count_spin.plus_button.isEnabled())
        self.assertIn('×10', self.page.generate_button.text())
        self.page.count_spin.setValue(3)
        self.assertIn('×3', self.page.generate_button.text())

    def test_duration_shortcuts_fit_separate_row_at_all_scales_and_narrow_width(self):
        for scale in ('1.0', '1.25', '1.5', '1.75'):
            apply_interface_scale(self.app, FakeScreen(), scale)
            for width in (px(1440), 1000):
                with self.subTest(scale=scale, width=width):
                    self.page.resize(width, 900)
                    flush_layout(self.app, self.page)
                    buttons = self.page.duration_buttons
                    self.assertEqual([button.text() for button in buttons], ['5秒', '10秒', '15秒'])
                    self.assertEqual(self.page.settings_body.parentWidget().width(), px(372))
                    self.assertEqual(self.page.body_scroll.horizontalScrollBar().maximum(), 0)
                    self.assertLessEqual(self.page.body_content.width(), self.page.body_scroll.viewport().width())
                    self.assertLess(self.page.duration_spin.geometry().bottom(), buttons[0].geometry().top())
                    self.assertLessEqual(max(button.width() for button in buttons) - min(button.width() for button in buttons), 1)
                    for index, button in enumerate(buttons):
                        self.assertGreaterEqual(button.width(), button.minimumSizeHint().width())
                        self.assertGreaterEqual(button.width(), max(px(56), button.fontMetrics().horizontalAdvance(button.text()) + px(24)))
                        self.assertTrue(button.isVisible())
                        self.assertTrue(self.page.parameters_content.rect().contains(button.geometry()))
                        self.assertEqual(button.y(), buttons[0].y())
                        if index:
                            self.assertGreaterEqual(button.x() - buttons[index - 1].geometry().right() - 1, px(8))
                        button.click()
                        self.assertEqual(self.page.duration_spin.value(), (5, 10, 15)[index])
                        self.assertEqual([item.isChecked() for item in buttons], [i == index for i in range(3)])
                    self.page.duration_spin.setValue(4)
                    self.assertFalse(any(button.isChecked() for button in buttons))

    def test_model_change_refreshes_supported_options(self):
        self.page.model_combo.setCurrentIndex(self.page.model_combo.findData('veo-3'))
        self.assertEqual([self.page.ratio_combo.itemText(i) for i in range(self.page.ratio_combo.count())], ['16:9'])
        self.assertEqual(self.page.output_size_combo.currentText(), '1920x1088')

    def test_model_extra_output_sizes_are_kept_and_unknown_progress_is_indeterminate(self):
        self.page._records['video-v2']['resolutions'] = ['1088x1920', '2K', '1920x1080']
        self.page._model_changed(self.page.model_combo.currentIndex())
        self.assertEqual([self.page.output_size_combo.itemText(i)
                          for i in range(self.page.output_size_combo.count())],
                         ['1088x1920', '2K', '1920x1080'])
        self.page._render_task({'local_id': 'running-1', 'status': 'running', 'model': 'video-v2'})
        self.assertEqual(self.page.progress_bar.minimum(), 0)
        self.assertEqual(self.page.progress_bar.maximum(), 0)
        self.page._render_task({'local_id': 'done-1', 'status': 'succeeded', 'model': 'video-v2'})
        self.assertEqual((self.page.progress_bar.minimum(), self.page.progress_bar.maximum()), (0, 100))
        self.assertEqual(self.page.progress_bar.value(), 100)

    def test_history_action_tooltips_are_stable(self):
        row = _HistoryRow({'local_id': 'done-1', 'status': 'succeeded'}, self.page)
        self.assertEqual(row.findChild(ToolButton, 'videoHistoryRegenerate').toolTip(), '重新生成')
        self.assertEqual(row.findChild(ToolButton, 'videoHistoryFolder').toolTip(), '打开文件夹')
        self.assertEqual(row.findChild(ToolButton, 'videoHistoryDelete').toolTip(), '删除记录')
        row.deleteLater()
        downloading = _HistoryRow({'local_id': 'downloading-1', 'status': 'downloading'}, self.page)
        self.assertEqual(downloading.status.text(), '下载中')
        downloading.deleteLater()

    def test_reference_mode_shows_reference_panel_and_disables_unavailable_media(self):
        self.assertFalse(self.page.reference_panel.isVisible())
        self.page.reference_mode_button.click()
        self.app.processEvents()
        self.assertTrue(self.page.reference_panel.isVisible())
        self.assertEqual(self.page.mode, 'reference')
        self.assertFalse(self.page.reference_videos_button.isEnabled())
        self.assertFalse(self.page.reference_audios_button.isEnabled())
        self.assertIn('0/3', self.page.reference_images_label.text())
        self.page.text_mode_button.click()
        self.assertFalse(self.page.reference_panel.isVisible())

    def test_reference_image_drop_zone_tiles_remove_and_prompt_insertion(self):
        self.page.reference_mode_button.click()
        paths = []
        for index in range(4):
            path = self.root / f'reference-{index}.png'
            image = QPixmap(16, 16)
            image.fill()
            self.assertTrue(image.save(str(path)))
            paths.append(str(path))

        self.assertTrue(self.page.reference_image_drop_zone.isVisible())
        self.assertEqual(self.page.reference_image_drop_zone.label.text(), '点击或拖入参考图片')
        self.page._accept_reference_images(paths)
        self.assertEqual(len(self.page.reference_images), 3)
        self.assertFalse(self.page.reference_image_drop_zone.isVisible())
        self.assertEqual(self.page.reference_images_grid.count(), 3)
        self.assertEqual(self.page.reference_images_label.text(), '参考图片（3/3）')

        first_tile = self.page.reference_images_grid.itemAt(0).widget()
        first_tile.findChild(QToolButton, 'videoReferenceImageInsert').click()
        self.assertIn('@参考图', self.page.prompt_edit.toPlainText())
        first_tile.findChild(QToolButton, 'videoReferenceImageRemove').click()
        self.assertEqual(self.page.reference_images, paths[1:3])
        self.assertEqual(self.page.reference_images_grid.count(), 3)
        self.assertTrue(self.page.reference_images_grid_host.findChild(QFrame, 'videoReferenceImageAddTile').isVisible())

        for _ in range(2):
            tile = self.page.reference_images_grid.itemAt(0).widget()
            tile.findChild(QToolButton, 'videoReferenceImageRemove').click()
        self.assertEqual(self.page.reference_images, [])
        self.assertTrue(self.page.reference_image_drop_zone.isVisible())

    def test_preview_text_uses_readable_theme_token(self):
        from ui.materials import ecommerce_color

        expected = ecommerce_color('video_creation_preview_text').lower()
        self.assertIn(expected, self.page.preview_area.styleSheet().lower())
        for label in (self.page.preview_empty, self.page.preview_loading,
                      self.page.preview_done, self.page.preview_failed):
            self.assertTrue(label.isWidgetType())
        self.page.refresh_colors()
        self.assertIn(expected, self.page.preview_area.styleSheet().lower())

    def test_idle_preview_play_button_and_status_badge_tokens(self):
        from ui.materials import ecommerce_color

        self.assertTrue(self.page.preview_empty_play.isVisible())
        self.assertEqual(self.page.preview_stack.currentWidget(), self.page.preview_empty_page)
        for status, token in (('running', 'video_creation_primary'),
                              ('succeeded', 'video_creation_success'),
                              ('failed', 'video_creation_error')):
            with self.subTest(status=status):
                self.page._render_task({'local_id': f'task-{status}', 'status': status})
                self.assertIn(ecommerce_color(token).lower(),
                              self.page.status_badge.styleSheet().lower())

    def test_preview_geometry_tracks_selected_aspect_ratio(self):
        self.page.resize(1440, 900)
        flush_layout(self.app, self.page)
        expected = {'9:16': 9 / 16, '16:9': 16 / 9, '1:1': 1.0}
        for ratio, value in expected.items():
            with self.subTest(ratio=ratio):
                self.page.ratio_combo.setCurrentText(ratio)
                flush_layout(self.app, self.page)
                width, height = self.page.preview_area.width(), self.page.preview_area.height()
                self.assertLessEqual(height, px(420))
                self.assertAlmostEqual(width / height, value, delta=0.02)

    def test_validation_disables_submit_until_prompt_and_warns(self):
        self.page.prompt_edit.clear()
        self.page._update_form_state()
        self.assertFalse(self.page.generate_button.isEnabled())
        with patch.object(self.page, '_warning') as warning:
            self.assertFalse(self.page.submit_generation())
            warning.assert_called_once()
        self.page.prompt_edit.setPlainText('A cinematic product shot')
        self.assertTrue(self.page.generate_button.isEnabled())
        self.page.model_combo.clear()
        self.page._update_form_state()
        self.app.processEvents()
        self.assertFalse(self.page.generate_button.isEnabled())

    def test_prompt_copy_and_preview_state_rendering(self):
        self.page.prompt_edit.setPlainText('A moving camera')
        self.page._copy_prompt()
        self.assertEqual(self.app.clipboard().text(), 'A moving camera')
        self.page._render_task({'local_id': 'task-1', 'status': 'running', 'progress': 42, 'task_id': 'remote-1', 'model': 'video-v2'})
        self.assertEqual(self.page.preview_stack.currentIndex(), 1)
        self.assertIn('42%', self.page.progress_label.text())
        self.assertEqual(self.page.progress_model_label.text(), 'video-v2')
        self.assertTrue(self.page.copy_task_id_button.isVisible())
        self.page.copy_task_id_button.click()
        self.assertEqual(self.app.clipboard().text(), 'remote-1')
        self.page._render_task({'local_id': 'task-1', 'status': 'failed', 'error': '额度不足'})
        self.assertEqual(self.page.preview_stack.currentIndex(), 3)
        self.assertEqual(self.page.preview_failed.text(), '额度不足')
        self.page._on_task({'local_id': 'task-1', 'status': 'succeeded', 'file_path': str(self.root / 'movie.mp4')})
        self.assertEqual(self.page.preview_stack.currentIndex(), 2)
        self.assertEqual(self.page.url_edit.text(), str(self.root / 'movie.mp4'))
        self.assertEqual(self.page.history_detail_hint.text(), '已保存到本地')

    def test_downloading_state_and_saved_download_metrics_are_visible(self):
        self.page._render_task({'local_id': 'task-download', 'task_id': 'remote-1',
                                'status': 'downloading', 'model': 'video-v2'})
        self.assertEqual(self.page.status_badge.text(), '下载中')
        self.assertEqual(self.page.preview_stack.currentIndex(), 1)
        self.assertEqual((self.page.progress_bar.minimum(), self.page.progress_bar.maximum()), (0, 0))
        self.assertIn('下载中…', self.page.progress_label.text())
        self.page._set_preview_done({
            'local_id': 'task-download', 'status': 'succeeded', 'file_path': str(self.root / 'movie.mp4'),
            'size_bytes': 12 * 1024**2 + 400 * 1024, 'download_ms': 1000,
            'throughput_bps': 18 * 1024**2 + 300 * 1024,
        })
        self.assertIn('已保存到本地', self.page.preview_done.text())
        self.assertIn('12.4 MB · 18.3 MB/s', self.page.preview_done.text())
        self.assertEqual(self.page.history_detail_hint.text(), '已保存到本地 · 12.4 MB · 18.3 MB/s')

    def test_download_button_reports_existing_local_file_without_redownloading(self):
        path = self.root / 'saved.mp4'
        path.write_bytes(b'\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00fixture-video')
        self.page._current_record = {'local_id': 'saved-1', 'file_path': str(path)}
        with patch('ui.pages.video_creation_page.InfoBar.success') as success, \
                patch.object(self.page, '_download_record') as download:
            self.assertFalse(self.page._download_current())
        download.assert_not_called()
        success.assert_called_once()

    def test_download_button_retries_invalid_existing_file(self):
        path = self.root / 'invalid.mp4'
        path.write_bytes(b'not an mp4')
        self.page._current_record = {'local_id': 'invalid-1', 'file_path': str(path),
                                     'video_url': 'https://relay.example/video.mp4'}
        with patch.object(self.page, '_download_record', return_value=True) as download:
            self.assertTrue(self.page._download_current())
        download.assert_called_once()

    def test_history_running_and_failed_states_are_preserved_for_retry(self):
        self.page._load_history_record({'local_id': 'running-1', 'status': 'running',
                                        'progress': 35, 'prompt': 'running prompt'})
        self.assertEqual(self.page.preview_stack.currentIndex(), 1)
        self.page._load_history_record({'local_id': 'failed-1', 'status': 'failed',
                                        'prompt': 'retry this prompt', 'error': '请求失败'})
        self.assertEqual(self.page.preview_stack.currentIndex(), 3)
        self.assertEqual(self.page.preview_failed.text(), '请求失败')
        with patch.object(self.page, 'submit_generation', return_value=True) as submit:
            self.page._retry_current()
        self.assertEqual(self.page.prompt_edit.toPlainText(), 'retry this prompt')
        submit.assert_called_once_with()

    def test_text_mode_does_not_submit_reference_assets(self):
        class QuickManager:
            on_task = None

            def prepare_tasks(inner, **kwargs):
                return []

            def run_tasks(inner, records):
                return None

        self.page._manager = QuickManager()
        self.page.prompt_edit.setPlainText('A clear scene')
        self.page.reference_images = ['reference.png']
        self.page.reference_videos = ['reference.mp4']
        self.page.reference_audios = ['reference.wav']
        self.page._set_mode('text')
        self.assertTrue(self.page.submit_generation())
        self.assertEqual(self.page._worker.kwargs['reference_images'], [])
        self.assertEqual(self.page._worker.kwargs['reference_videos'], [])
        self.assertEqual(self.page._worker.kwargs['reference_audios'], [])

    def test_history_rows_only_allow_download_selection_when_downloadable(self):
        unavailable = _HistoryRow({'local_id': 'failed-1', 'status': 'failed'}, self.page)
        available = _HistoryRow({'local_id': 'done-1', 'status': 'succeeded',
                                 'file_path': str(self.root / 'video.mp4')}, self.page)
        self.assertFalse(unavailable.check.isEnabled())
        self.assertTrue(available.check.isEnabled())
        unavailable.deleteLater()
        available.deleteLater()

    def test_media_time_and_backend_error_fall_back_to_file(self):
        self.assertEqual(self.page._media_time(65_000), '01:05')
        movie = self.root / 'movie.mp4'
        movie.write_bytes(b'fixture')
        self.page._current_record = {'file_path': str(movie)}
        self.page.preview_stack.setCurrentWidget(self.page.preview_media_page)
        self.page._on_media_error()
        self.assertFalse(self.page.media_available)
        self.assertTrue(self.page.preview_done.isVisible())
        self.assertIn(str(movie), self.page.preview_done.text())
        self.assertTrue(self.page.open_video_button.isVisible())

    def test_media_controls_toggle_fake_player(self):
        class FakePlayer:
            def __init__(inner):
                inner._state = 0
                inner.muted = False
                inner.play_calls = inner.pause_calls = 0

            def state(inner):
                return inner._state

            def play(inner):
                inner.play_calls += 1

            def pause(inner):
                inner.pause_calls += 1

            def isMuted(inner):
                return inner.muted

            def setMuted(inner, value):
                inner.muted = bool(value)

        fake = FakePlayer()
        self.page.media_player = fake
        self.page.media_play_button = QToolButton(self.page)
        self.page.media_mute_button = QToolButton(self.page)
        self.page._toggle_media_playback()
        self.assertEqual(fake.play_calls, 1)
        fake._state = 1  # QMediaPlayer.PlayingState in Qt5
        self.page._toggle_media_playback()
        self.assertEqual(fake.pause_calls, 1)
        self.page._toggle_media_mute()
        self.assertTrue(fake.muted)
        self.page._toggle_media_mute()
        self.assertFalse(fake.muted)

    def test_submit_is_non_blocking_and_passes_count_and_form_values(self):
        started, release = threading.Event(), threading.Event()

        class FakeManager:
            on_task = None

            def prepare_tasks(inner, **kwargs):
                inner.kwargs = kwargs
                return [dict(local_id=f'task-{i}', status='pending') for i in range(kwargs['count'])]

            def run_tasks(inner, records):
                started.set()
                if not release.wait(2):
                    raise AssertionError('fake worker gate timed out')
                for record in records:
                    if inner.on_task:
                        inner.on_task(dict(record, status='succeeded', progress=100))

        manager = FakeManager()
        self.page._manager = manager
        self.page.prompt_edit.setPlainText('A studio product video')
        self.page.count_spin.setValue(2)
        with patch.object(self.page, '_success') as success:
            start = time.monotonic()
            self.assertTrue(self.page.submit_generation())
            self.assertLess(time.monotonic() - start, 0.3)
            self.assertTrue(started.wait(2))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not success.called:
                self.app.processEvents()
                time.sleep(0.005)
            success.assert_called_once()
            self.assertIn('已提交 2 个视频生成任务', success.call_args.args[0])
        self.assertEqual(manager.kwargs['count'], 2)
        self.assertEqual(manager.kwargs['ratio'], self.page.ratio_combo.currentText())
        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.page.has_running_workers():
            self.app.processEvents()
            time.sleep(0.005)
        self.assertFalse(self.page.has_running_workers())

    def test_history_search_paging_and_select_row(self):
        records = [
            dict(local_id=f'id-{i}', prompt=f'Prompt {i}', prompt_name=f'Prompt {i}',
                 model='video-v2', status='succeeded', duration=5,
                 output_size='1088x1920', created_at=float(i))
            for i in range(7)
        ]

        class Store:
            def list(self, limit=20):
                return list(reversed(records))[:limit]

            def delete(self, local_id):
                return True

        self.page._history_store = lambda: Store()
        self.page.refresh_history()
        self.assertEqual(len(self.page._history_records), 7)
        self.assertEqual(self.page.history_result_count.text(), '7 条结果')
        self.assertTrue(self.page.history_next_button.isEnabled())
        self.assertEqual(len(self.page.history_list.findChildren(_HistoryRow)), 5)
        self.page.history_next_button.click()
        self.app.processEvents()
        self.assertEqual(self._rendered_history_rows(), 2)
        self.page.history_search.setText('Prompt 6')
        self.app.processEvents()
        self.assertEqual(self._rendered_history_rows(), 1)
        row = next(widget for index in range(self.page.history_layout.count())
                   if (widget := self.page.history_layout.itemAt(index).widget()) is not None)
        row.clicked.emit(row.record)
        self.assertEqual(self.page.url_edit.text(), '')
        self.assertEqual(self.page.history_detail_hint.text(), '已打开历史视频详情')

    def _rendered_history_rows(self):
        return sum(isinstance(self.page.history_layout.itemAt(index).widget(), _HistoryRow)
                   for index in range(self.page.history_layout.count()))

    def test_narrow_layout_stacks_without_horizontal_scroll(self):
        self.page.resize(1000, 900)
        self.app.processEvents()
        self.assertEqual(self.page.body_layout.direction(), QBoxLayout.TopToBottom)
        self.assertEqual(self.page.body_scroll.horizontalScrollBar().maximum(), 0)
        self.page.resize(1440, 900)
        self.app.processEvents()
        self.assertEqual(self.page.body_layout.direction(), QBoxLayout.LeftToRight)

    def test_wide_layout_keeps_settings_column_visible_and_stable_across_scales(self):
        self.page.resize(1440, 900)
        flush_layout(self.app, self.page)
        self.assertEqual(self.page.body_layout.direction(), QBoxLayout.LeftToRight)
        self.assertGreater(self.page.left_column.width(), 0)
        self.assertEqual(self.page.left_column.width(), px(372))
        self.assertGreater(self.page.right_column.width(), self.page.left_column.width())
        self.assertLessEqual(self.page.left_column.geometry().right(), self.page.right_column.geometry().left())

        for scale in ('1.0', '1.25', '1.5', '1.75'):
            with self.subTest(scale=scale):
                apply_interface_scale(self.app, FakeScreen(), scale)
                self.page.resize(1000, 900) if scale == '1.75' else self.page.resize(px(1440), px(900))
                flush_layout(self.app, self.page)
                self.assertEqual(self.page.body_scroll.horizontalScrollBar().maximum(), 0)
                if scale == '1.75':
                    self.assertEqual(self.page.body_layout.direction(), QBoxLayout.TopToBottom)
                    self.assertGreater(self.page.left_column.width(), 0)
                    self.assertLessEqual(self.page.left_column.width(), self.page.body_content.width())


    def test_reference_uses_shared_lightbox_and_preview_is_balanced(self):
        from PyQt5.QtCore import Qt, QPoint
        from PyQt5.QtGui import QColor
        from ui.components.image_lightbox import ImageLightbox
        paths = []
        for index in range(2):
            path = self.root / f'ref-{index}.png'
            image = QPixmap(90,160)
            image.fill(QColor('#446688'))
            image.save(str(path))
            paths.append(str(path))
        self.page._accept_reference_images(paths)
        with patch.object(ImageLightbox,'show_images',return_value=object()) as preview:
            buttons = self.page.findChildren(QToolButton, 'videoReferenceImagePreview')
            QTest.mouseClick(buttons[1], Qt.LeftButton)
            preview.assert_called_once_with(self.page, paths, 1)
        self.page.preview_stack.setCurrentWidget(self.page.preview_media_page)
        if self.page.video_widget is not None:
            self.page.video_widget.hide()
        self.page.preview_done.show()
        flush_layout(self.app,self.page)
        center = self.page.preview_done.mapTo(self.page.preview_area, self.page.preview_done.rect().center())
        expected = self.page.preview_area.rect().center()
        self.assertLessEqual(abs(center.x()-expected.x()),2)
        self.assertLessEqual(abs(center.y()-expected.y()),2)
        if self.page.media_controls is not None:
            self.assertIs(self.page.media_controls.parentWidget(), self.page.preview_area.parentWidget())
        self.assertEqual(self.page.preview_done.alignment(), Qt.AlignCenter)

    def test_media_backend_is_checked_on_instance_and_video_stays_centered(self):
        from PyQt5.QtCore import Qt
        from PyQt5.QtMultimedia import QMediaPlayer
        checked = []

        class AvailablePlayer(QMediaPlayer):
            def isAvailable(player):
                checked.append(player)
                return True

        with patch('ui.pages.video_creation_page.QMediaPlayer', AvailablePlayer):
            page = VideoCreationPage(self.config)
        try:
            self.assertEqual(checked, [page.media_player])
            self.assertTrue(page.media_available)
            self.assertIsNotNone(page.video_widget)
            self.assertEqual(page.video_widget.aspectRatioMode(), Qt.KeepAspectRatio)
            page.resize(1440, 900)
            page.show()
            page.preview_stack.setCurrentWidget(page.preview_media_page)
            page.preview_done.hide()
            flush_layout(self.app, page)
            center = page.video_widget.mapTo(page.preview_area, page.video_widget.rect().center())
            expected = page.preview_area.rect().center()
            self.assertLessEqual(abs(center.x() - expected.x()), 2)
            self.assertLessEqual(abs(center.y() - expected.y()), 2)
        finally:
            page.shutdown_workers(wait_ms=2000)
            page.close()
            page.deleteLater()

    def test_unavailable_media_backend_keeps_centered_file_fallback(self):
        with patch('ui.pages.video_creation_page.QMediaPlayer', None):
            page = VideoCreationPage(self.config)
        try:
            self.assertFalse(page.media_available)
            self.assertIsNone(page.media_player)
            self.assertIsNone(page.video_widget)
            self.assertFalse(page.preview_done.isHidden())
        finally:
            page.shutdown_workers(wait_ms=2000)
            page.close()
            page.deleteLater()

    def test_decoded_frame_is_painted_centered_in_translucent_window_and_fullscreen_restores(self):
        from PyQt5.QtCore import Qt, QSize
        from PyQt5.QtGui import QColor, QImage
        from PyQt5.QtMultimedia import QAbstractVideoBuffer, QVideoFrame, QVideoSurfaceFormat
        from PyQt5.QtWidgets import QWidget
        from ui.pages.video_creation_page import _VideoPreviewWidget

        host = QWidget()
        host.setAttribute(Qt.WA_TranslucentBackground)
        view = _VideoPreviewWidget(host)
        view.setGeometry(0, 0, 240, 240)
        host.resize(240, 240)
        host.show()
        # DirectShow RGB32 frames can contain zero in the unused alpha byte.
        raw_pixels = b'\xAA\x77\x44\x00' * (80 * 160)
        image = QImage(raw_pixels, 80, 160, 80 * 4, QImage.Format_RGB32)
        surface_format = QVideoSurfaceFormat(image.size(), QVideoFrame.Format_RGB32)
        try:
            self.assertTrue(view.surface.start(surface_format))
            self.assertTrue(view.surface.present(QVideoFrame(image)))
            image.fill(QColor('#FF0000'))  # present must retain its own frame storage.
            self.app.processEvents()
            self.assertEqual(view.frameRect().size(), QSize(120, 240))
            self.assertLessEqual((view.frameRect().center() - view.rect().center()).manhattanLength(), 1)
            shot = host.grab().toImage()
            self.assertEqual(shot.pixelColor(120, 120).name(), '#4477aa')
            self.assertNotEqual(shot.pixelColor(5, 120).name(), '#4477aa')
            self.assertFalse(view.surface.present(QVideoFrame()))
            self.assertEqual(view.surface.supportedPixelFormats(QAbstractVideoBuffer.GLTextureHandle), [])
            view.showFullScreen()
            self.assertTrue(view.isFullScreen())
            QTest.keyClick(view, Qt.Key_Escape)
            self.assertFalse(view.isFullScreen())
            self.assertIs(view.parentWidget(), host)
            self.assertFalse(view.isWindow())
        finally:
            view.surface.stop()
            host.close()
            host.deleteLater()



if __name__ == '__main__':
    unittest.main()
