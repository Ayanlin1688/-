"""Model tests are explicit, asynchronous and isolated from external networks."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication, QEvent, QThread
from PyQt5.QtWidgets import QApplication

from core.config_manager import ConfigManager
from core.i18n import set_language, tr
from tests.test_task_manager import wait_until
from ui.pages.ecommerce_page import EcommercePage, _LIVE_WORKERS


class EcommerceModelControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        set_language('zh-CN')
        self.temp = tempfile.TemporaryDirectory()
        self.config = ConfigManager(Path(self.temp.name) / 'config.json')
        self.config.config['api'].update(base_url='https://offline.invalid/v1', api_key='offline-key')
        self.network_patch = patch('requests.sessions.Session.request', side_effect=AssertionError('禁止真实联网'))
        self.network = self.network_patch.start()
        self.notice_patch = patch('ui.pages.ecommerce_page.InfoBar.error')
        self.notice = self.notice_patch.start()
        self.probe_patch = patch('ui.pages.ecommerce_page.probe_line')
        self.probe = self.probe_patch.start()
        self.probe.return_value = dict(ok=True, message='文本调用成功')
        self.config.model_catalog_controller = Mock()
        self.config.model_catalog_controller.snapshot_for.return_value = {
            'deepseek-v4-pro': dict(kind='chat', available=True),
            'gpt-image-2': dict(kind='image', available=True),
        }
        self.page = EcommercePage(self.config)
        self.page.analysis_model_picker.set_value('deepseek-v4-pro')
        self.page.image_model_picker.set_value('gpt-image-2')
        self.gates = []

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        wait_until(lambda: not self.page._model_probe_workers)
        self.page.close()
        if not sip.isdeleted(self.page):
            self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.network.assert_not_called()
        self.probe_patch.stop()
        self.notice_patch.stop()
        self.network_patch.stop()
        self.temp.cleanup()

    def block_probe(self, result):
        started, release = threading.Event(), threading.Event()
        self.gates.append(release)
        calls = []

        def probe(*args, **kwargs):
            calls.append((QThread.currentThread(), args, kwargs))
            started.set()
            if not release.wait(5):
                raise AssertionError('测试探测线程未及时释放')
            return result

        self.probe.side_effect = probe
        return started, release, calls

    def finish_probe(self):
        wait_until(lambda: not self.page._model_probe_workers)

    def test_initial_state_is_gray_and_does_not_probe_until_clicked(self):
        self.probe.assert_not_called()
        for dot in (self.page.analysis_status_dot, self.page.image_status_dot):
            self.assertEqual(dot._color.name(), '#92929b')
            self.assertEqual(dot.toolTip(), tr('未测试'))

    def test_chat_test_runs_off_thread_uses_selected_id_and_reenables_button(self):
        started, release, calls = self.block_probe(dict(ok=True, message='文本调用成功'))
        self.page.sync_analysis_models_button.click()
        wait_until(started.is_set)
        self.assertFalse(self.page.sync_analysis_models_button.isEnabled())
        self.page.sync_analysis_models_button.click()
        self.assertEqual(len(calls), 1)
        release.set()
        self.finish_probe()
        self.assertNotEqual(calls[0][0], self.app.thread())
        self.assertEqual(calls[0][1][2], 'chat')
        self.assertEqual(calls[0][2]['preferred_model'], 'deepseek-v4-pro')
        self.assertIsInstance(calls[0][2]['models'], list)
        self.assertTrue(self.page.sync_analysis_models_button.isEnabled())
        self.assertEqual(self.page.analysis_status_dot._color.name(), '#67c23a')
        self.assertEqual(self.page.analysis_status_dot.toolTip(), '模型测试通过：文本调用成功')
        self.config.model_catalog_controller.refresh.assert_called_once_with()
        self.notice.assert_not_called()

    def test_image_test_failure_is_red_and_notifies_once_without_generation(self):
        self.probe.return_value = dict(ok=False, message='模型列表中未找到该图片模型，请同步模型或更换')
        self.page.sync_image_models_button.click()
        self.finish_probe()
        self.assertEqual(self.probe.call_args.args[2], 'image')
        self.assertEqual(self.probe.call_args.kwargs['preferred_model'], 'gpt-image-2')
        self.assertEqual(self.page.image_status_dot._color.name(), '#f56c6c')
        self.assertIn('未找到该图片模型', self.page.image_status_dot.toolTip())
        self.assertTrue(self.page.sync_image_models_button.isEnabled())
        self.notice.assert_called_once()
        self.assertEqual(self.notice.call_args.kwargs['duration'], 5000)

    def test_unconfigured_line_fails_without_probe_and_restores_button(self):
        self.config.config['api'].update(base_url='', api_key='')
        self.page.refresh_effective_lines()
        self.page.sync_analysis_models_button.click()
        self.finish_probe()
        self.probe.assert_not_called()
        self.assertEqual(self.page.analysis_status_dot._color.name(), '#f56c6c')
        self.assertEqual(self.page.analysis_status_dot.toolTip(), tr('未配置该能力线路'))
        self.assertTrue(self.page.sync_analysis_models_button.isEnabled())

    def test_model_route_and_credential_changes_reset_only_affected_status(self):
        self.page.sync_analysis_models_button.click()
        self.finish_probe()
        self.page.sync_image_models_button.click()
        self.finish_probe()
        self.page.analysis_model_picker.set_value('another-chat-model')
        self.assertEqual(self.page.analysis_status_dot._color.name(), '#92929b')
        self.assertEqual(self.page.image_status_dot._color.name(), '#67c23a')
        self.config.config['stations'] = [dict(id='image-station', name='图片线路', enabled=True,
                                             base_url='https://other.invalid/v1', api_key='station-fixture')]
        self.page.refresh_line_routes()
        self.page.image_line_combo.setCurrentIndex(1)
        self.assertEqual(self.page.image_status_dot._color.name(), '#92929b')
        self.page.sync_analysis_models_button.click()
        self.finish_probe()
        self.config.config['api']['api_key'] = 'new-offline-key'
        self.page.refresh_effective_lines()
        self.assertEqual(self.page.analysis_status_dot._color.name(), '#92929b')
        self.assertEqual(self.page.analysis_status_dot.toolTip(), tr('未测试'))

    def test_result_from_previous_selection_cannot_turn_new_selection_green(self):
        started, release, _calls = self.block_probe(dict(ok=True, message='旧模型测试成功'))
        self.page.sync_analysis_models_button.click()
        wait_until(started.is_set)
        self.page.analysis_model_picker.set_value('new-chat-model')
        release.set()
        self.finish_probe()
        self.assertEqual(self.page.analysis_status_dot._color.name(), '#92929b')
        self.assertEqual(self.page.analysis_status_dot.toolTip(), tr('未测试'))
        self.assertTrue(self.page.sync_analysis_models_button.isEnabled())

    def test_unexpected_probe_exception_is_chinese_and_never_exposes_stack(self):
        self.probe.side_effect = RuntimeError('HTTPSConnectionPool SSLError UNEXPECTED_EOF')
        self.page.sync_analysis_models_button.click()
        self.finish_probe()
        message = self.page.analysis_status_dot.toolTip()
        self.assertIn('SSL 连接被中断', message)
        self.assertNotIn('HTTPSConnectionPool', message)
        self.assertNotIn('SSLError', message)
        self.assertTrue(self.page.sync_analysis_models_button.isEnabled())

    def test_probe_finishing_during_generation_keeps_button_disabled_until_idle(self):
        started, release, _calls = self.block_probe(dict(ok=True, message='文本调用成功'))
        self.page.sync_analysis_models_button.click()
        wait_until(started.is_set)
        self.page._worker = object()
        try:
            release.set()
            self.finish_probe()
            self.assertFalse(self.page.sync_analysis_models_button.isEnabled())
            self.page._test_selected_model('image')
            self.assertEqual(self.probe.call_count, 1)
        finally:
            self.page._thread_stopped()
        self.assertTrue(self.page.sync_analysis_models_button.isEnabled())
        self.assertTrue(self.page.sync_image_models_button.isEnabled())

    def test_close_suppresses_late_failure_and_reopening_restores_button(self):
        started, release, _calls = self.block_probe(dict(ok=False, message='旧测试失败'))
        self.page.show()
        self.page.sync_analysis_models_button.click()
        wait_until(started.is_set)
        self.page.close()
        release.set()
        self.finish_probe()
        self.notice.assert_not_called()
        self.page.show()
        self.assertTrue(self.page.sync_analysis_models_button.isEnabled())

    def test_worker_remains_alive_when_page_is_deleted_during_probe(self):
        started, release, _calls = self.block_probe(dict(ok=True, message='文本调用成功'))
        self.page.sync_analysis_models_button.click()
        wait_until(started.is_set)
        worker = self.page._model_probe_workers['chat']
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertTrue(sip.isdeleted(self.page))
        self.assertIn(worker, _LIVE_WORKERS)
        release.set()
        wait_until(lambda: worker not in _LIVE_WORKERS)
        # The Qt object no longer exists; teardown must not call widget methods.
        self.page = EcommercePage(self.config)


if __name__ == '__main__':
    unittest.main()
