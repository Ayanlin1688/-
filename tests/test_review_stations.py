"""Station changes must update the active connection and its model catalog."""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import LineEdit

from core.config_manager import ConfigManager
from ui.main_window import MainWindow
from test_task_manager import wait_until


class StationConnectionRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = ConfigManager(Path(self.temp.name) / 'config.json')
        self.config.config['api'].update(base_url='https://alpha.example/v1', api_key='fixture-alpha')
        self.config.config['stations'] = [
            dict(id='alpha', name='Alpha', base_url='https://alpha.example/v1', api_key='fixture-alpha'),
            dict(id='beta', name='Beta', base_url='https://beta.example/v1', api_key='fixture-beta',
                 upload_url='https://beta.example/upload', upload_api_key='fixture-upload'),
        ]
        self.config.config['stations_active'] = 'alpha'
        self.config.save_config()
        self.window = MainWindow(self.config, network_time=False, model_sync=False)
        QTest.qWait(50)
        self.settings = self.window.settings_page

    def tearDown(self):
        self.window.close()
        wait_until(lambda: not self.window._background_busy())
        self.window.deleteLater()
        QTest.qWait(30)
        self.temp.cleanup()

    @staticmethod
    def field(text):
        edit = LineEdit()
        edit.setText(text)
        return edit

    def test_switch_station_rebinds_catalog_to_new_account(self):
        catalog = self.window.model_catalog
        old_catalog = catalog.catalog
        self.settings._set_current_station('beta')
        self.assertEqual(self.config.config['api']['base_url'], 'https://beta.example/v1')
        self.assertEqual(catalog.catalog.base_url, 'https://beta.example/v1')
        self.assertEqual(catalog.catalog.api_key, 'fixture-beta')
        self.assertIsNot(catalog.catalog, old_catalog)

    def test_save_active_station_updates_actual_runtime_connection(self):
        self.settings._save_station_page(
            'alpha', self.field('Alpha revised'), self.field('https://updated.example/v1'),
            self.field('fixture-updated'), self.field('https://updated.example/upload'),
            self.field('fixture-upload-updated'), {})
        api = self.config.config['api']
        self.assertEqual(api['base_url'], 'https://updated.example/v1')
        self.assertEqual(api['api_key'], 'fixture-updated')
        self.assertEqual(api['upload_url'], 'https://updated.example/upload')
        self.assertEqual(api['upload_api_key'], 'fixture-upload-updated')
        self.assertEqual(self.settings.base_url.text(), 'https://updated.example/v1')
        self.assertEqual(self.window.model_catalog.catalog.base_url, 'https://updated.example/v1')
        restored = ConfigManager(self.config.path).load_config()
        self.assertEqual(restored['api']['base_url'], 'https://updated.example/v1')

    def test_save_inactive_station_keeps_current_runtime_connection(self):
        self.settings._save_station_page(
            'beta', self.field('Beta revised'), self.field('https://updated.example/v1'),
            self.field('fixture-updated'), self.field(''), self.field(''), {})
        self.assertEqual(self.config.config['api']['base_url'], 'https://alpha.example/v1')
        self.assertEqual(self.window.model_catalog.catalog.base_url, 'https://alpha.example/v1')
        self.assertEqual(self.config.config['stations'][1]['base_url'], 'https://updated.example/v1')


if __name__ == '__main__':
    unittest.main()
