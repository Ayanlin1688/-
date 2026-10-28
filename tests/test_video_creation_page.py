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

from PyQt5.QtWidgets import QApplication, QBoxLayout, QFrame, QToolButton, QAbstractButton, QCheckBox
from PyQt5.QtGui import QPixmap
from PyQt5.QtTest import QTest
from PyQt5.QtCore import QObject, Qt, pyqtSignal
from qfluentwidgets import PushButton, ToolButton

from core.config_manager import ConfigManager
from core.i18n import set_language
from tests.qt_scale_support import flush_layout, reset_interface_scaling
from tests.test_interface_scaling import FakeScreen
from ui.scaling import apply_interface_scale
from ui.tokens import px
from ui.pages.video_creation_page import VideoCreationPage, _HistoryRow


class _ReferencePlayerFixture(QObject):
    PlayingState, PausedState, StoppedState = 1, 2, 0
    stateChanged = pyqtSignal(int)
    durationChanged = pyqtSignal(int)
    error = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_state = self.StoppedState
        self.sources = []
        self.play_calls = 0

    def isAvailable(self):
        return True

    def state(self):
        return self.current_state

    def setMedia(self, source):
        self.sources.append(source)

    def setVideoOutput(self, surface):
        self.surface = surface

    def _change(self, state):
        self.current_state = state
        self.stateChanged.emit(state)

    def play(self):
        self.play_calls += 1
        self._change(self.PlayingState)

    def pause(self):
        self._change(self.PausedState)

    def stop(self):
        self._change(self.StoppedState)


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

    def _use_models(self, names):
        from core.model_catalog import _record_from_upstream
        self.config.model_catalog_controller.snapshot_for.return_value = {
            name: _record_from_upstream(dict(id=name, supported_endpoint_types=['openai-video'])) for name in names}
        self.page.refresh_catalog()

    def _wait_upload(self):
        deadline = time.monotonic() + 3
        while self.page._upload_worker is not None and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertIsNone(self.page._upload_worker)

    def _add_media(self, kind, name):
        path = self.root / name
        path.write_bytes(b'local reference fixture')
        self.page._media_uploaded(kind, str(path), 'https://fixture.test/' + name)
        return path, self.page._media_rows[kind][-1]

    def test_reference_audio_play_pause_end_error_remove_and_no_reupload(self):
        self.page._set_mode('reference')
        self.page.prompt_edit.setPlainText('参考素材测试')
        with patch('ui.pages.video_creation_page.QMediaPlayer', _ReferencePlayerFixture), patch.object(self.page, '_warning') as warning:
            path, row = self._add_media('audios', '中文旁白.wav')
            player = row._player
            self.assertEqual(player.sources, [])
            warning.assert_not_called()
            QTest.mouseClick(row.play_button, Qt.LeftButton)
            self.assertEqual(row.play_button.toolTip(), '暂停')
            self.assertEqual(Path(player.sources[0].canonicalUrl().toLocalFile()), path)
            QTest.mouseClick(row.play_button, Qt.LeftButton)
            self.assertEqual(row.play_button.toolTip(), '播放')
            QTest.mouseClick(row, Qt.LeftButton)
            self.assertEqual(len(player.sources), 1)
            player.stop()
            self.assertEqual(row.play_button.toolTip(), '播放')
            QTest.mouseClick(row.play_button, Qt.LeftButton)
            player.error.emit(1)
            self.assertEqual(row.play_button.toolTip(), '播放')
            self.assertTrue(player.sources[-1].isNull())
            warning.assert_called_once_with('音频播放失败，请检查文件或系统音频输出')
            QTest.mouseClick(row.play_button, Qt.LeftButton)
            self.assertEqual(Path(player.sources[-1].canonicalUrl().toLocalFile()), path)
            QTest.mouseClick(row.remove_button, Qt.LeftButton)
            self.assertEqual(self.page.reference_audios, [])
            self.assertEqual(self.page.reference_audios_label.text(), '参考音频（0/3）')
            self.assertNotIn('https://fixture.test/' + path.name, self.page._media_local_paths)
            self.assertTrue(player.sources[-1].isNull())
            self.assertTrue(self.page.generate_button.isEnabled())

    def test_reference_video_dialog_reopen_duration_and_system_fallback(self):
        with patch('ui.pages.video_creation_page.QMediaPlayer', _ReferencePlayerFixture), patch.object(self.page, '_warning') as warning:
            path, row = self._add_media('videos', '产品演示.mp4')
            row._duration_player.durationChanged.emit(125000)
            QTest.qWait(10)
            self.assertIn('02:05', row.meta_label.text())
            self.assertTrue(row._duration_player.sources[-1].isNull())
            self.assertTrue(row.toggle_playback())
            dialog = row._video_dialog
            self.assertTrue(dialog.isVisible())
            self.assertEqual(Path(dialog.player.sources[0].canonicalUrl().toLocalFile()), path)
            QTest.mouseClick(dialog.play_button, Qt.LeftButton)
            self.assertEqual(dialog.play_button.text(), '播放')
            dialog.close()
            self.assertTrue(dialog.player.sources[-1].isNull())
            self.assertIsNone(row._video_dialog)
            self.assertTrue(row.toggle_playback())
            self.assertIsNot(row._video_dialog, dialog)
            row.stop()
            warning.assert_not_called()
        with patch('ui.pages.video_creation_page.QMediaPlayer', None), patch('ui.pages.video_creation_page.QDesktopServices.openUrl', return_value=True) as open_url, patch.object(row, 'warning_callback') as warning:
            self.assertTrue(row.toggle_playback())
            self.assertEqual(Path(open_url.call_args.args[0].toLocalFile()), path)
            warning.assert_called_once_with('已使用系统默认播放器打开参考视频')
        with patch('ui.pages.video_creation_page.QMediaPlayer', None), patch('ui.pages.video_creation_page.QDesktopServices.openUrl', side_effect=OSError('fixture')), patch.object(row, 'warning_callback') as warning:
            self.assertFalse(row.toggle_playback())
            warning.assert_called_once_with('无法打开参考视频，请检查文件关联')

    def test_audio_backend_unavailable_warns_only_on_play(self):
        with patch('ui.pages.video_creation_page.QMediaPlayer', None), patch.object(self.page, '_warning') as warning:
            _path, row = self._add_media('audios', '无设备.mp3')
            warning.assert_not_called()
            self.assertFalse(row.toggle_playback())
            warning.assert_called_once_with('当前设备无法播放音频，请检查系统音频输出')

    def test_windows_media_backend_prefers_wmf_and_preserves_explicit_override(self):
        from ui.pages.video_creation_page import _prefer_windows_media_backend
        key = 'QT_MULTIMEDIA_PREFERRED_PLUGINS'
        with patch.dict(os.environ, {}, clear=False), patch('os.name', 'nt'):
            os.environ.pop(key, None)
            _prefer_windows_media_backend()
            self.assertEqual(os.environ[key], 'windowsmediafoundation,directshow')
            os.environ[key] = 'directshow'
            _prefer_windows_media_backend()
            self.assertEqual(os.environ[key], 'directshow')
        with patch.dict(os.environ, {}, clear=False), patch('os.name', 'posix'):
            os.environ.pop(key, None)
            _prefer_windows_media_backend()
            self.assertNotIn(key, os.environ)

    def test_media_rows_and_submit_reachable_all_scales_and_themes(self):
        from ui.theme import apply_ui_mode
        from PyQt5.QtCore import QPoint
        self.page._set_mode('reference')
        with patch('ui.pages.video_creation_page.QMediaPlayer', _ReferencePlayerFixture):
            self._add_media('videos', '很长的参考视频文件名用于验证省略和提示.mp4')
            self._add_media('audios', '音频旁白.wav')
        try:
            for theme in ('light', 'dark'):
                apply_ui_mode(theme)
                self.page.refresh_colors()
                for scale in ('1.0', '1.25', '1.5', '1.75'):
                    apply_interface_scale(self.app, FakeScreen(), scale)
                    self.page.resize(1024, 700)
                    flush_layout(self.app, self.page)
                    for kind in ('videos', 'audios'):
                        row = self.page._media_rows[kind][0]
                        self.assertGreaterEqual(row.height(), px(44))
                        self.assertEqual(row.name_label.toolTip(), Path(row.path).name)
                        widgets = (row.name_label, row.meta_label, row.play_button, row.remove_button)
                        for widget in widgets:
                            self.assertTrue(row.rect().contains(widget.geometry()))
                        for left, right in zip(widgets, widgets[1:]):
                            self.assertLess(left.geometry().right(), right.x())
                        self.assertGreaterEqual(row.meta_label.width(), row.meta_label.fontMetrics().horizontalAdvance(row.meta_label.text()))
                        self.page.body_scroll.ensureWidgetVisible(row)
                        self.app.processEvents()
                        point = row.mapTo(self.page.body_scroll.viewport(), row.play_button.geometry().center())
                        self.assertTrue(self.page.body_scroll.viewport().rect().contains(point))
                    self.page.body_scroll.ensureWidgetVisible(self.page.generate_button)
                    self.app.processEvents()
                    center = self.page.generate_button.mapTo(self.page.body_scroll.viewport(), self.page.generate_button.rect().center())
                    self.assertTrue(self.page.body_scroll.viewport().rect().contains(center))
        finally:
            apply_ui_mode('light')

    def test_wan_clips_reference_videos_to_real_api_limit_and_rejects_fourth(self):
        self._use_models(['video-v3', 'wan-3.0'])
        self.page._set_mode('reference')
        with patch('ui.pages.video_creation_page.QMediaPlayer', _ReferencePlayerFixture):
            entries = [self._add_media('videos', f'video-{index}.mp4') for index in range(5)]
        with patch.object(self.page, '_warning') as warning:
            self.page.model_combo.setCurrentIndex(self.page.model_combo.findData('wan-3.0'))
        warning.assert_called_once_with('已按当前模型上限裁剪参考素材')
        self.assertEqual(len(self.page._media_rows['videos']), 3)
        self.assertEqual(len(self.page._media_local_paths), 3)
        self.assertEqual(self.page.reference_videos_label.text(), '参考视频（3/3）')
        self.assertEqual(self.page.reference_videos_label.toolTip(), '当前接口最多 3 个参考视频')
        with patch.object(self.page, '_warning') as warning, patch('ui.pages.video_creation_page.ImageUploader.from_config') as uploader:
            self.assertFalse(self.page._accept_media_files([str(entries[3][0])], 'videos'))
            uploader.assert_not_called()
        warning.assert_called_once_with('参考素材超出模型上限，最多 3 个')
        self.page.duration_buttons[-1].click()
        self.assertEqual(self.page.duration_spin.value(), 30)

    def test_reference_upload_failure_blocks_submit_until_retry_or_explicit_removal(self):
        self.page._set_mode('reference')
        self.page.prompt_edit.setPlainText('不可静默忽略上传失败素材')
        worker = Mock(remaining=['fixture.mp4'], kind='videos')
        with patch.object(self.page, '_warning'):
            self.page._media_upload_failed(worker, '素材上传被拒绝，请检查文件格式与大小')
            self.page._update_form_state()
            self.assertFalse(self.page.generate_button.isEnabled())
            self.assertFalse(self.page.submit_generation())
        self.page.discard_upload_button.click()
        self.assertIsNone(self.page._retry_upload)
        self.assertTrue(self.page.generate_button.isEnabled())

    def test_model_capabilities_link_duration_presets_media_and_output_sizes(self):
        self._use_models(['MiniMax-H3', 'video-v3', 'wan-3.0'])
        for model, duration, limits, presets, resolutions in (
                ('MiniMax-H3', (4, 15), (9, 3, 3), (5, 10, 15), ('768p', '1080p', '2K', '4K')),
                ('video-v3', (4, 30), (30, 10, 10), (5, 10, 15, 30), ('720p',)),
                ('wan-3.0', (5, 30), (10, 3, 5), (5, 10, 15, 30), ('720p',))):
            self.page.model_combo.setCurrentIndex(self.page.model_combo.findData(model))
            self.assertEqual((self.page.duration_spin.minimum(), self.page.duration_spin.maximum()), duration)
            self.assertEqual(tuple(button.property('duration') for button in self.page.duration_buttons), presets)
            self.assertEqual(tuple(self.page._capabilities['max_' + kind] for kind in ('images', 'videos', 'audios')), limits)
            self.assertEqual(self.page._capabilities['resolutions'], resolutions)
            if model == 'video-v3':
                self.assertEqual(self.page.duration_spin.value(), 30)
                self.assertEqual(self.page.output_size_combo.currentText(), '720x1280')
        self.page.reference_images = ['image'] * 30
        self.page.reference_videos = ['video'] * 10
        self.page.reference_audios = ['audio'] * 10
        with patch.object(self.page, '_warning') as warning:
            self.page.model_combo.setCurrentIndex(0)
        warning.assert_called_once()
        self.assertEqual(tuple(len(getattr(self.page, 'reference_' + kind)) for kind in ('images', 'videos', 'audios')), (9, 3, 3))

    def test_unknown_model_requires_video_endpoint_and_uses_conservative_fallback(self):
        from core.model_catalog import _record_from_upstream
        self.config.model_catalog_controller.snapshot_for.return_value = {
            'future-motion': _record_from_upstream(dict(id='future-motion', kind='unknown', supported_endpoint_types=['openai-video'])),
            'unsupported-motion': _record_from_upstream(dict(id='unsupported-motion', kind='video')),
            'unknown-without-endpoint': _record_from_upstream(dict(id='unknown-without-endpoint')),
        }
        with self.assertLogs('core.video_capabilities', level='WARNING'):
            self.page.refresh_catalog()
        self.page.prompt_edit.setPlainText('Offline prompt')
        self.assertEqual(self.page.model_combo.currentData(), 'future-motion')
        self.assertTrue(self.page.generate_button.isEnabled())
        self.assertEqual(self.page._capabilities['source'], 'fallback')
        self.assertEqual(self.page._capabilities['durations'], tuple(range(4, 16)))
        self.assertEqual(self.page.model_combo.findData('unknown-without-endpoint'), -1)
        self.page.model_combo.setCurrentIndex(self.page.model_combo.findData('unsupported-motion'))
        self.assertFalse(self.page.generate_button.isEnabled())
        self.assertIn('不支持视频提交', self.page.generate_button.toolTip())
        with patch.object(self.page, '_warning') as warning:
            self.assertFalse(self.page.submit_generation())
        warning.assert_called_once()

    def test_catalog_refresh_preserves_selection_then_falls_back_when_removed(self):
        self.page.model_combo.setCurrentIndex(self.page.model_combo.findData('veo-3'))
        self.page.refresh_catalog()
        self.assertEqual(self.page.model_combo.currentData(), 'veo-3')
        self.config.model_catalog_controller.snapshot_for.return_value = {
            'video-v3': dict(id='video-v3', kind='video', available=True)}
        with patch.object(self.page, '_warning') as warning:
            self.page.refresh_catalog()
        warning.assert_called_once()
        self.assertEqual(self.page.model_combo.currentData(), 'video-v3')
        self.assertEqual(self.page.duration_spin.value(), 30)

    def test_media_upload_is_async_disables_submit_and_preserves_order(self):
        self.page.prompt_edit.setPlainText('Offline upload')
        paths = [self.root / '片段.mp4', self.root / '参考.mov']
        for path in paths:
            path.write_bytes(b'fixture media')
        started, release = threading.Event(), threading.Event()
        uploader = Mock()
        def upload(path, kind):
            started.set(); release.wait(2)
            return 'https://fixture.test/' + Path(path).name
        uploader.upload_file.side_effect = upload
        try:
            with patch('ui.pages.video_creation_page.ImageUploader.from_config', return_value=uploader):
                before = time.perf_counter()
                self.assertTrue(self.page._accept_media_files([str(path) for path in paths], 'videos'))
                self.assertLess(time.perf_counter() - before, .1)
                self.assertFalse(self.page.generate_button.isEnabled())
                self.assertFalse(self.page.model_combo.isEnabled())
                self.assertTrue(started.wait(1))
                QTest.qWait(20)
                release.set(); self._wait_upload()
        finally:
            release.set()
        self.assertEqual(self.page.reference_videos, ['https://fixture.test/' + path.name for path in paths])
        self.assertIn('参考视频（2/3）', self.page.reference_videos_label.text())
        self.assertIn(paths[0].name, self.page.reference_videos_hint.toolTip())
        self.assertTrue(self.page.generate_button.isEnabled())
        uploader.close.assert_called_once()

    def test_media_upload_failure_is_classified_and_retry_only_uploads_remaining(self):
        from core.http_client import RequestError
        paths = [self.root / 'first.mp3', self.root / 'second.mp3']
        for path in paths:
            path.write_bytes(b'fixture media')
        uploader = Mock()
        uploader.upload_file.side_effect = ['https://fixture.test/first.mp3', RequestError('redacted', 401)]
        with patch('ui.pages.video_creation_page.ImageUploader.from_config', return_value=uploader), patch.object(self.page, '_warning') as warning:
            self.page._accept_media_files([str(path) for path in paths], 'audios')
            self._wait_upload()
        self.assertEqual(self.page.reference_audios, ['https://fixture.test/first.mp3'])
        self.assertEqual(self.page.upload_status.text(), '素材上传鉴权失败，请检查上传密钥')
        self.assertEqual(self.page._retry_upload, ([str(paths[1])], 'audios'))
        warning.assert_called_once()
        uploader = Mock(); uploader.upload_file.return_value = 'https://fixture.test/second.mp3'
        with patch('ui.pages.video_creation_page.ImageUploader.from_config', return_value=uploader):
            self.page._retry_media_upload(); self._wait_upload()
        self.assertEqual(len(self.page.reference_audios), 2)
        uploader.upload_file.assert_called_once_with(str(paths[1]), kind='audios')

    def test_catalog_change_waits_for_upload_and_cancel_drops_late_result(self):
        paths = [self.root / 'first.mp4', self.root / 'second.mp4']
        for path in paths:
            path.write_bytes(b'fixture media')
        started, release = threading.Event(), threading.Event()
        uploader = Mock()
        def upload(path, kind):
            started.set(); release.wait(2)
            return 'https://fixture.test/' + Path(path).name
        uploader.upload_file.side_effect = upload
        try:
            with patch('ui.pages.video_creation_page.ImageUploader.from_config', return_value=uploader):
                self.page._accept_media_files([str(path) for path in paths], 'videos')
                self.assertTrue(started.wait(1))
                self.config.model_catalog_controller.snapshot_for.return_value = {
                    'video-v3': dict(id='video-v3', kind='video', available=True)}
                self.page.refresh_catalog()
                self.assertEqual(self.page.model_combo.currentData(), 'video-v2')
                self.page._upload_worker.cancel()
                with patch.object(self.page, '_warning'):
                    release.set(); self._wait_upload()
        finally:
            release.set()
        self.assertEqual(self.page.reference_videos, [])
        self.assertEqual(uploader.upload_file.call_count, 1)
        self.assertEqual(self.page.upload_status.text(), '参考素材上传已取消')
        self.assertEqual(self.page.model_combo.currentData(), 'video-v3')
        self.assertFalse(self.page._catalog_refresh_pending)

    def test_preview_fills_available_height_at_two_window_sizes_and_decoded_ratio(self):
        from PyQt5.QtGui import QImage
        for size in ((1200, 800), (1600, 1000)):
            self.page.resize(*size)
            self.page.ratio_combo.setCurrentText('9:16')
            flush_layout(self.app, self.page)
            if size[1] >= 1000:
                self.assertGreater(self.page.preview_area.height(), px(420))
            self.assertAlmostEqual(self.page.preview_area.height(), self.page._preview_available_height, delta=2)
            self.assertAlmostEqual(self.page.preview_area.width()/self.page.preview_area.height(), 9/16, delta=.01)
        if self.page.video_widget is not None:
            self.page.preview_stack.setCurrentIndex(2)
            self.page.video_widget._set_frame(QImage(600, 800, QImage.Format_RGB32))
            flush_layout(self.app, self.page)
            self.assertAlmostEqual(self.page.preview_area.width()/self.page.preview_area.height(), 3/4, delta=.01)

    def test_preview_1366_and_1920_is_large_centered_bounded_all_scales(self):
        from ui.theme import apply_ui_mode
        from PyQt5.QtGui import QImage
        try:
            for theme in ('light', 'dark'):
                apply_ui_mode(theme)
                self.page.refresh_colors()
                for scale in ('1.0', '1.25', '1.5', '1.75'):
                    apply_interface_scale(self.app, FakeScreen(), scale)
                    for window_size in ((1366, 768), (1920, 1080)):
                        self.page.resize(*window_size)
                        for ratio in ('9:16', '16:9', '1:1'):
                            with self.subTest(theme=theme, scale=scale, size=window_size, ratio=ratio):
                                self.page.ratio_combo.setCurrentText(ratio)
                                flush_layout(self.app, self.page)
                                area = self.page.preview_area
                                card = area.parentWidget()
                                self.assertTrue(card.rect().contains(area.geometry()))
                                self.assertAlmostEqual(area.geometry().center().x(), card.rect().center().x(), delta=2)
                                self.assertLessEqual(area.height(), px(720))
                                width, height = map(int, ratio.split(':'))
                                self.assertAlmostEqual(area.width() / area.height(), width / height, delta=.01)
                                if ratio == '9:16':
                                    self.assertGreaterEqual(area.height(), px(420))
                                    self.assertGreaterEqual(area.height() / (card.height() - px(32)), .55)
                                previous = area.size()
                                for _ in range(3):
                                    self.page._update_preview_geometry()
                                    self.app.processEvents()
                                self.assertEqual(area.size(), previous)
            if self.page.video_widget is not None:
                self.page.preview_stack.setCurrentIndex(2)
                self.page.video_widget._set_frame(QImage(720, 1280, QImage.Format_RGB32))
                flush_layout(self.app, self.page)
                self.assertAlmostEqual(self.page.preview_area.width() / self.page.preview_area.height(), 9/16, delta=.01)
        finally:
            apply_ui_mode('light')

    def test_v3_four_duration_shortcuts_fit_all_scales(self):
        self._use_models(['video-v3'])
        for scale in ('1.0', '1.25', '1.5', '1.75'):
            apply_interface_scale(self.app, FakeScreen(), scale)
            self.page.resize(px(1440), px(900))
            flush_layout(self.app, self.page)
            self.assertEqual([button.text() for button in self.page.duration_buttons], ['5秒', '10秒', '15秒', '30秒'])
            for index, button in enumerate(self.page.duration_buttons):
                self.assertGreaterEqual(button.width(), button.fontMetrics().horizontalAdvance(button.text()) + px(12))
                self.assertTrue(self.page.parameters_content.rect().contains(button.geometry()))
                row, column, row_span, column_span = self.page.duration_shortcuts.getItemPosition(index)
                self.assertEqual((row, column, row_span, column_span), (index // 2, index % 2, 1, 1))
                if column:
                    self.assertEqual(button.y(), self.page.duration_buttons[index-1].y())
                    self.assertGreaterEqual(button.x() - self.page.duration_buttons[index-1].geometry().right() - 1, px(8))
                if row:
                    self.assertEqual(button.x(), self.page.duration_buttons[index-2].x())
                    self.assertGreaterEqual(button.y() - self.page.duration_buttons[index-2].geometry().bottom() - 1, px(8))
                self.assertAlmostEqual(button.width(), self.page.duration_buttons[0].width(), delta=1)
                button.click()
                self.assertEqual(self.page.duration_spin.value(), (5, 10, 15, 30)[index])
                self.assertEqual([item.isChecked() for item in self.page.duration_buttons], [i == index for i in range(4)])

    def test_duration_selected_fill_follows_theme(self):
        from PyQt5.QtGui import QColor
        from ui.materials import ecommerce_color
        from ui.theme import apply_ui_mode
        self._use_models(['video-v3', 'wan-3.0'])
        try:
            for theme in ('light', 'dark'):
                apply_ui_mode(theme)
                self.page.refresh_colors()
                for model in ('video-v3', 'wan-3.0'):
                    self.page.model_combo.setCurrentIndex(self.page.model_combo.findData(model))
                    for scale in ('1.0', '1.25', '1.5', '1.75'):
                        apply_interface_scale(self.app, FakeScreen(), scale)
                        self.page.refresh_interface_scale()
                        flush_layout(self.app, self.page)
                        for button in self.page.duration_buttons:
                            button.click()
                            self.app.processEvents()
                            rendered = button.grab().toImage()
                            self.assertEqual(rendered.pixelColor(px(8), button.height() // 2).name(),
                                             QColor(ecommerce_color('video_creation_primary')).name())
        finally:
            apply_ui_mode('light')

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
        self.assertIn('自动保存', self.page.info_bar.text())

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
        self.assertEqual((self.page.duration_spin.minimum(), self.page.duration_spin.maximum()), (5, 15))
        self.page.duration_buttons[1].click()
        self.assertEqual(self.page.duration_spin.value(), 10)
        self.assertTrue(self.page.duration_buttons[1].isChecked())
        self.page.ratio_combo.setCurrentText('16:9')
        self.assertEqual(self.page.output_size_combo.currentText(), '854x480')
        self.page.ratio_combo.setCurrentText('1:1')
        self.assertEqual(self.page.output_size_combo.currentText(), '480x480')
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
                    self.assertEqual(self.page.body_layout.direction(), QBoxLayout.LeftToRight)
                    self.assertGreaterEqual(self.page.body_content.width(), self.page.body_scroll.viewport().width())
                    self.assertEqual(self.page.right_column.minimumWidth(), px(640))
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
                    self.assertEqual(self.page.duration_spin.value(), 5)
                    self.assertTrue(buttons[0].isChecked())

    def test_model_change_refreshes_supported_options(self):
        self.page.model_combo.setCurrentIndex(self.page.model_combo.findData('veo-3'))
        self.assertEqual([self.page.ratio_combo.itemText(i) for i in range(self.page.ratio_combo.count())], ['16:9'])
        self.assertEqual(self.page.output_size_combo.currentText(), '1920x1088')

    def test_verified_model_output_sizes_and_unknown_progress_is_indeterminate(self):
        self.assertEqual([self.page.output_size_combo.itemText(i)
                          for i in range(self.page.output_size_combo.count())],
                         ['480x854', '720x1280', '1080x1920'])
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

    def test_reference_mode_shows_model_media_limits_and_enabled_uploads(self):
        self.assertFalse(self.page.reference_panel.isVisible())
        self.page.reference_mode_button.click()
        self.app.processEvents()
        self.assertTrue(self.page.reference_panel.isVisible())
        self.assertEqual(self.page.mode, 'reference')
        self.assertTrue(self.page.reference_videos_button.isEnabled())
        self.assertTrue(self.page.reference_audios_button.isEnabled())
        self.assertIn('0/9', self.page.reference_images_label.text())
        self.page.text_mode_button.click()
        self.assertFalse(self.page.reference_panel.isVisible())

    def test_reference_image_drop_zone_tiles_remove_and_prompt_insertion(self):
        self.page.reference_mode_button.click()
        paths = []
        for index in range(10):
            path = self.root / f'reference-{index}.png'
            image = QPixmap(16, 16)
            image.fill()
            self.assertTrue(image.save(str(path)))
            paths.append(str(path))

        self.assertTrue(self.page.reference_image_drop_zone.isVisible())
        self.assertEqual(self.page.reference_image_drop_zone.label.text(), '点击或拖入参考图片')
        with patch.object(self.page, '_warning') as warning:
            self.page._accept_reference_images(paths)
        warning.assert_called_once()
        self.assertEqual(len(self.page.reference_images), 9)
        self.assertFalse(self.page.reference_image_drop_zone.isVisible())
        self.assertEqual(self.page.reference_images_grid.count(), 9)
        self.assertEqual(self.page.reference_images_label.text(), '参考图片（9/9）')

        first_tile = self.page.reference_images_grid.itemAt(0).widget()
        first_tile.findChild(QToolButton, 'videoReferenceImageInsert').click()
        self.assertIn('@参考图', self.page.prompt_edit.toPlainText())
        first_tile.findChild(QToolButton, 'videoReferenceImageRemove').click()
        self.assertEqual(self.page.reference_images, paths[1:9])
        self.assertEqual(self.page.reference_images_grid.count(), 9)
        self.assertTrue(self.page.reference_images_grid_host.findChild(QFrame, 'videoReferenceImageAddTile').isVisible())

        for _ in range(8):
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
        for size in ((1200, 800), (1600, 1000)):
            self.page.resize(*size)
            flush_layout(self.app, self.page)
            play = self.page.preview_empty_play.geometry()
            label = self.page.preview_empty.geometry()
            area = self.page.preview_empty_page.rect()
            self.assertAlmostEqual((play.top() + label.bottom()) / 2, area.center().y(), delta=2)
            self.assertAlmostEqual(play.center().x(), area.center().x(), delta=2)
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
                self.assertGreater(height, px(420) if ratio == '9:16' else px(160))
                self.assertLessEqual(height, self.page._preview_available_height)
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

    def test_no_manual_download_actions_and_local_file_can_be_opened(self):
        path = self.root / 'saved.mp4'
        path.write_bytes(b'\x00\x00\x00\x18ftypmp42fixture-video')
        self.page._current_record = {'local_id': 'saved-1', 'file_path': str(path)}
        with patch('ui.pages.video_creation_page.QDesktopServices.openUrl', return_value=True) as open_url:
            self.page._open_current_file()
        self.assertEqual(Path(open_url.call_args.args[0].toLocalFile()), path)
        self.assertFalse(any('下载' in button.text() or '下载' in button.toolTip()
                             for button in self.page.findChildren(QAbstractButton)))
        self.assertIn('自动保存', self.page.info_bar.text())
        for name in ('download_button', 'batch_download_button', 'select_downloadable_check',
                     '_download_current', '_download_record', '_batch_download'):
            self.assertFalse(hasattr(self.page, name), name)

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

    def test_history_rows_have_no_download_selection_or_actions(self):
        for status in ('failed', 'running', 'downloading', 'succeeded'):
            row = _HistoryRow({'local_id': status, 'status': status,
                               'file_path': str(self.root / 'video.mp4')}, self.page)
            self.assertEqual(row.findChildren(QCheckBox), [])
            self.assertFalse(any('下载' in b.text() or '下载' in b.toolTip()
                                 for b in row.findChildren(QAbstractButton)))
            row.deleteLater()

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

    def test_narrow_layout_keeps_columns_with_horizontal_scroll(self):
        self.page.resize(1000, 900)
        self.app.processEvents()
        self.assertEqual(self.page.body_layout.direction(), QBoxLayout.LeftToRight)
        self.assertGreater(self.page.body_scroll.horizontalScrollBar().maximum(), 0)
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
                if scale == '1.75':
                    self.assertGreater(self.page.body_scroll.horizontalScrollBar().maximum(), 0)
                    self.assertEqual(self.page.body_layout.direction(), QBoxLayout.LeftToRight)
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
