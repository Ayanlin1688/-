"""Station changes must update the active connection and its model catalog."""
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import LineEdit

from core.config_manager import ConfigManager
from core.line_routing import resolve_line
from core.model_catalog import builtin_models
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
            dict(id='alpha', name='Alpha', enabled=True,
                 base_url='https://alpha.example/v1', api_key='fixture-alpha'),
            dict(id='beta', name='Beta', base_url='https://beta.example/v1', api_key='fixture-beta',
                 upload_url='https://beta.example/upload', upload_api_key='fixture-upload', enabled=True),
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

    def test_capability_route_selection_persists_without_changing_active_api(self):
        api_before = dict(self.config.config['api'])
        combo = self.settings.routing_combos['chat']
        combo.setCurrentIndex(combo.findData('beta'))

        self.assertEqual(self.config.config['line_routing']['chat'], 'beta')
        self.assertEqual(self.config.config['api'], api_before)
        self.assertEqual(resolve_line(self.config.config, 'chat')['base_url'], 'https://beta.example/v1')
        self.assertEqual(resolve_line(self.config.config, 'chat')['api_key'], 'fixture-beta')

        self.config.config['stations'][1]['enabled'] = False
        self.assertEqual(resolve_line(self.config.config, 'chat')['base_url'], api_before['base_url'])
        self.config.config['stations'][1]['enabled'] = True
        self.config.config['stations'] = self.config.config['stations'][:1]
        self.assertEqual(resolve_line(self.config.config, 'chat')['base_url'], api_before['base_url'])

    def test_base_url_is_normalized_when_station_is_saved(self):
        self._save_active_station_base_url('https://updated.example/')
        self.assertEqual(self.config.config['api']['base_url'], 'https://updated.example/v1')
        self.assertEqual(self.config.config['stations'][0]['base_url'], 'https://updated.example/v1')

    def test_official_deepseek_template_is_idempotent_and_text_only(self):
        self.settings._add_official_deepseek_template()
        stations = self.config.config['stations']
        deepseek = [station for station in stations
                    if (urlsplit(station.get('base_url', '')).hostname or '').lower() == 'api.deepseek.com']
        self.assertEqual(len(deepseek), 1)
        station = deepseek[0]
        self.assertEqual(station['name'], '官方 DeepSeek')
        self.assertEqual(station['base_url'], 'https://api.deepseek.com/v1')
        self.assertEqual(station['capabilities'], ['llm'])
        self.assertTrue(station['enabled'])
        self.assertEqual(station['api_key'], '')
        station_id = station['id']
        self.settings._add_official_deepseek_template()
        deepseek_again = [item for item in self.config.config['stations']
                          if (urlsplit(item.get('base_url', '')).hostname or '').lower() == 'api.deepseek.com']
        self.assertEqual([item['id'] for item in deepseek_again], [station_id])
        self.assertEqual(self.settings._current_page_key(), f'station:{station_id}')

    def test_chat_route_refreshes_ecommerce_picker_with_deepseek_catalog(self):
        deepseek = dict(id='deepseek', name='官方 DeepSeek', enabled=True,
                        base_url='https://api.deepseek.com/v1', api_key='fixture-deepseek',
                        capabilities=['llm'])
        self.config.config['stations'].append(deepseek)
        self.settings._render_stations()
        controller = self.window.model_catalog
        self.settings._refresh_routing_options()
        combo = self.settings.routing_combos['chat']
        alpha_identity = ('https://alpha.example/v1', 'fixture-alpha')
        alpha_catalog = controller._catalogs[alpha_identity]
        records = builtin_models()
        alpha_catalog._models = {'gemini-3.8-flash': records['gemini-3.8-flash']}
        combo.setCurrentIndex(combo.findData('deepseek'))
        identity = (deepseek['base_url'], deepseek['api_key'])
        catalog = controller._catalogs[identity]
        deepseek_records = {name: records[name] for name in (
            'deepseek-flash', 'deepseek-v4-pro', 'deepseek-chat', 'deepseek-reasoner')}
        catalog._models = deepseek_records
        controller.changed.emit(controller.snapshot())
        page = self.window.ecommerce_page
        self.assertEqual(page.analysis_line_label.text(), '生效：api.deepseek.com')
        values = [page.analysis_model_picker.itemData(index)
                  for index in range(page.analysis_model_picker.count())
                  if not str(page.analysis_model_picker.itemData(index) or '').startswith('__separator__:')]
        self.assertEqual(values, list(deepseek_records))
        self.assertEqual(controller._route_identities['chat'], identity)
        combo.setCurrentIndex(combo.findData('active'))
        self.assertEqual(page.analysis_line_label.text(), '生效：alpha.example')
        restored = [page.analysis_model_picker.itemData(index)
                    for index in range(page.analysis_model_picker.count())
                    if not str(page.analysis_model_picker.itemData(index) or '').startswith('__separator__:')]
        self.assertEqual(restored, ['gemini-3.8-flash'])

    def _save_active_station_base_url(self, url):
        self.settings._save_station_page(
            'alpha', self.field('Alpha normalized'), self.field(url),
            self.field('fixture-alpha-updated'), self.field(''), self.field(''), {})


if __name__ == '__main__':
    unittest.main()
