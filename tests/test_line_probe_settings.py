"""Settings route and upload-field probes stay offline and capability-aware."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QAbstractButton, QCheckBox

from core.config_manager import ConfigManager
from core.line_routing import resolve_line
from ui.main_window import MainWindow
from ui.pages.settings_page import SettingsPage
from test_task_manager import wait_until


class LineProbeSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.network = patch('requests.sessions.Session.request',
                             side_effect=AssertionError('settings probe test must remain offline'))
        self.network.start()
        self.config = ConfigManager(Path(self.temp.name) / 'config.json')
        self.config.config['api'].update(base_url='https://active.invalid/v1', api_key='fixture-active')
        self.config.config['stations'] = [dict(
            id='station-a', name='Station A', enabled=True, base_url='https://station.invalid/v1',
            api_key='fixture-station', upload_url='', upload_api_key='', capabilities=['video', 'llm'])]
        self.config.config['line_routing'].update(video='station-a', chat='active', image='station-a')
        self.config.save_config()
        self.window = MainWindow(self.config, network_time=False, model_sync=False)
        self.settings = self.window.settings_page

    def tearDown(self):
        self.window.close()
        wait_until(lambda: not self.window._background_busy())
        self.window.deleteLater()
        QTest.qWait(30)
        self.network.stop()
        self.temp.cleanup()

    def test_route_buttons_resolve_each_capability(self):
        resolutions = []
        def resolve(config, capability):
            resolutions.append(capability)
            return resolve_line(config, capability)

        result = dict(ok=True, level='success', message='fixture',
                      counts={'video': 1, 'chat': 2, 'image': 1})
        with patch('ui.pages.settings_page.resolve_line', side_effect=resolve), \
                patch('ui.pages.settings_page.probe_line', return_value=result):
            for capability in ('video', 'chat', 'image'):
                self.settings.routing_test_buttons[capability].click()
                wait_until(lambda: not self.settings.jobs.busy)
        self.assertEqual(resolutions, ['video', 'chat', 'image'])

    def test_station_test_only_probes_checked_capabilities_and_keeps_llm_key(self):
        self.settings._open_station_editor(None)
        self.settings.station_name.setText('Text only')
        self.settings.station_base.setText('https://text.invalid')
        self.settings.station_key.setText('fixture-text')
        self.settings.station_caps['video'].setChecked(False)
        self.settings.station_caps['llm'].setChecked(True)
        self.settings.station_caps['image'].setChecked(False)
        button = next(button for button in self.settings.station_editor_host.findChildren(QAbstractButton)
                      if button.text() == '测试')
        result = dict(ok=True, level='success', message='fixture', counts={})
        with patch('ui.pages.settings_page.probe_line', return_value=result) as probe:
            button.click()
            wait_until(lambda: not self.settings.jobs.busy)
        probe.assert_called_once()
        self.assertEqual(probe.call_args.args[2], 'chat')
        self.assertEqual(self.settings.station_caps['llm'].text(), '文本（AI分析）')
        self.settings._save_station_editor()
        saved = next(station for station in self.config.config['stations'] if station['name'] == 'Text only')
        self.assertEqual(saved['capabilities'], ['llm'])

    def test_station_page_test_button_uses_only_checked_capabilities(self):
        self.settings._switch_page('station:station-a', highlight=True)
        _area, content = self.settings._pages['station:station-a']
        boxes = {box.text(): box for box in content.findChildren(QCheckBox)}
        boxes['视频'].setChecked(False)
        boxes['文本（AI分析）'].setChecked(True)
        boxes['生图'].setChecked(False)
        button = next(button for button in content.findChildren(QAbstractButton)
                      if button.text() == '测试')
        result = dict(ok=True, level='success', message='fixture', counts={})
        with patch('ui.pages.settings_page.probe_line', return_value=result) as probe:
            button.click()
            wait_until(lambda: not self.settings.jobs.busy)
        probe.assert_called_once()
        self.assertEqual(probe.call_args.args[2], 'chat')

    def test_upload_token_mistake_moves_to_token_field_and_invalid_url_shows_hint(self):
        with patch.object(self.settings, '_notice') as notice:
            self.settings.upload_url.setText('vup_fixture-token')
            self.assertTrue(self.settings._repair_upload_fields(self.settings.upload_url, self.settings.upload_key))
        self.assertEqual(self.settings.upload_url.text(), '')
        self.assertEqual(self.settings.upload_key.text(), 'vup_fixture-token')
        notice.assert_called_once()

        self.settings.upload_url.setText('not a URL')
        self.assertFalse(self.settings._repair_upload_fields(self.settings.upload_url, self.settings.upload_key))
        hint = self.settings._upload_url_hints[self.settings.upload_url]
        self.assertFalse(hint.isHidden())
        self.assertIn('http://', hint.text())

    def test_upload_token_correction_handles_same_and_replaces_old_token_idempotently(self):
        url = self.settings.upload_url
        token = self.settings.upload_key
        token.setText('vup_same-fixture-token')
        url.setText('vup_same-fixture-token')
        with patch.object(self.settings, '_notice') as notice:
            self.assertTrue(self.settings._repair_upload_fields(url, token))
            self.assertEqual(url.text(), '')
            self.assertEqual(token.text(), 'vup_same-fixture-token')
            self.assertTrue(self.settings._repair_upload_fields(url, token))
            self.assertEqual(notice.call_count, 1)

        token.setText('old-upload-fixture')
        url.setText('vup_replacement-fixture-token')
        with patch.object(self.settings, '_notice') as notice:
            self.assertTrue(self.settings._repair_upload_fields(url, token))
            notice.assert_called_once()
            self.assertIn('覆盖旧值', notice.call_args.args[0])
            self.assertEqual(url.text(), '')
            self.assertEqual(token.text(), 'vup_replacement-fixture-token')
            self.assertTrue(self.settings._repair_upload_fields(url, token))
            self.assertEqual(notice.call_count, 1)

    def test_station_test_runs_with_invalid_upload_url_and_reports_each_api_result(self):
        self.settings._open_station_editor(None)
        self.settings.station_base.setText('https://station.invalid')
        self.settings.station_key.setText('fixture-station-key')
        self.settings.station_upload.setText('not a URL')
        for capability, box in self.settings.station_caps.items():
            box.setChecked(capability == 'llm')
        button = next(button for button in self.settings.station_editor_host.findChildren(QAbstractButton)
                      if button.text() == '测试')

        with patch('ui.pages.settings_page.probe_line', return_value=dict(
                ok=True, level='success', message='fixture reachable', counts={'chat': 2})) as probe, \
                patch.object(self.settings, '_notice') as notice:
            self.settings._test_station_fields(
                self.settings.station_base, self.settings.station_key,
                self.settings.station_upload, self.settings.station_upload_key,
                self.settings.station_caps, button)
            wait_until(lambda: not self.settings.jobs.busy)

        probe.assert_called_once()
        self.assertEqual(probe.call_args.args[2], 'chat')
        message = notice.call_args.args[0]
        self.assertIn('AI分析（文本）API：成功', message)
        self.assertIn('上传 URL 未配置或格式无效', message)
        self.assertEqual(notice.call_args.kwargs['level'], 'warning')
        self.assertTrue(button.isEnabled())

    def test_station_probe_failure_is_visible_as_error(self):
        self.settings._open_station_editor(None)
        self.settings.station_base.setText('https://station.invalid')
        self.settings.station_caps['video'].setChecked(True)
        for capability in ('llm', 'image'):
            self.settings.station_caps[capability].setChecked(False)
        button = next(button for button in self.settings.station_editor_host.findChildren(QAbstractButton)
                      if button.text() == '测试')
        with patch('ui.pages.settings_page.probe_line', return_value=dict(
                ok=False, level='error', message='fixture rejected', counts={})) as probe, \
                patch.object(self.settings, '_notice') as notice:
            self.settings._test_station_fields(
                self.settings.station_base, self.settings.station_key,
                self.settings.station_upload, self.settings.station_upload_key,
                self.settings.station_caps, button)
            wait_until(lambda: not self.settings.jobs.busy)
        probe.assert_called_once()
        self.assertIn('fixture rejected', notice.call_args.args[0])
        self.assertEqual(notice.call_args.kwargs['level'], 'error')

    def test_startup_sanitizes_current_line_and_all_station_records_once(self):
        config = ConfigManager(Path(self.temp.name) / 'dirty-config.json')
        config.config['api'].update(upload_url='vup_current-fixture', upload_api_key='old-current-fixture')
        config.config['stations'] = [
            dict(id='empty', upload_url='vup_empty-fixture', upload_api_key=''),
            dict(id='same', upload_url='vup_same-fixture', upload_api_key='vup_same-fixture'),
            dict(id='replace', upload_url='vup_new-fixture', upload_api_key='old-station-fixture'),
            dict(id='clean', upload_url='https://upload.invalid', upload_api_key='upload-fixture'),
        ]
        config.save_config()
        logs = []
        original_update = config.update
        with patch.object(config, 'update', wraps=original_update) as update:
            page = SettingsPage(config, lambda *args: logs.append(args))
            page.stop_timers()
            self.addCleanup(page.deleteLater)
            self.assertEqual(update.call_count, 2)
            self.assertEqual([call.args[0] for call in update.call_args_list], [('api',), ('stations',)])

        self.assertEqual(config.config['api']['upload_url'], '')
        self.assertEqual(config.config['api']['upload_api_key'], 'vup_current-fixture')
        stations = {row['id']: row for row in config.config['stations']}
        self.assertEqual(stations['empty']['upload_url'], '')
        self.assertEqual(stations['empty']['upload_api_key'], 'vup_empty-fixture')
        self.assertEqual(stations['same']['upload_url'], '')
        self.assertEqual(stations['same']['upload_api_key'], 'vup_same-fixture')
        self.assertEqual(stations['replace']['upload_url'], '')
        self.assertEqual(stations['replace']['upload_api_key'], 'vup_new-fixture')
        self.assertEqual(stations['clean']['upload_url'], 'https://upload.invalid')
        self.assertEqual(len(logs), 1)
        self.assertNotIn('vup_', logs[0][0])


if __name__ == '__main__':
    unittest.main()
