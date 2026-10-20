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


if __name__ == '__main__':
    unittest.main()
