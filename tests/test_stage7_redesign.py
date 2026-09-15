"""Stage 7 acceptance: workspace layout discipline, rounded chrome, relay stations."""
import copy
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication
from PyQt5.QtTest import QTest
from core.config_manager import ConfigManager
from ui.main_window import MainWindow
from test_task_manager import wait_until


class RedesignLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = ConfigManager(Path(self.temp.name) / 'config.json')
        self.config.load_config()
        self.window = MainWindow(self.config, network_time=False)
        self.window.resize(1600, 1020)
        self.window.show()
        QTest.qWait(120)

    def tearDown(self):
        self.window.close()
        wait_until(lambda: not self.window._background_busy())
        self.window.deleteLater(); QTest.qWait(30)
        self.temp.cleanup()

    def test_table_columns_share_one_width_source_and_chips_stay_compact(self):
        page = self.window.workspace_page
        tasks = [dict(prompt_name=f'任务{i}', model='video-v3', images=[], status='processing' if i < 2 else 'waiting')
                 for i in range(4)]
        page._tasks_updated(tasks)
        QTest.qWait(120)
        table = page.queue_panel
        table._apply_column_widths()
        QTest.qWait(60)
        header = [table._header_grid.columnMinimumWidth(i) for i in range(12)]
        for row in table.rows:
            self.assertEqual([row.grid.columnMinimumWidth(i) for i in range(12)], header,
                             '表头与数据行必须使用同一列宽解析结果')
        self.assertLessEqual(header[1], 340, '提示词列不得无限拉伸产生空白死区')
        row = table.rows[0]
        self.assertLessEqual(row.status_label.width(), 112)
        self.assertFalse(row.status_label.geometry().intersects(row.more_button.geometry()),
                         '状态胶囊与行尾操作按钮不得重叠')
        self.assertGreaterEqual(row.more_button.width(), 24)

    def test_rounded_window_frame_and_navigation_spacing(self):
        self.assertTrue(self.window.testAttribute(Qt.WA_TranslucentBackground))
        margin = self.window._window_margins_px()
        self.assertEqual(margin, 8)
        self.assertEqual(self.window.hBoxLayout.contentsMargins().left(), 8)
        self.assertEqual(self.window.titleBar.x(), 8)
        self.assertEqual(len(self.window.workspace_page.queue_panel.rows), 0)
        item = self.window.navigationInterface.widget(self.window.workspace_page.objectName())
        self.assertGreaterEqual(item.height(), 40)


class RelayStationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = ConfigManager(Path(self.temp.name) / 'config.json')
        self.config.load_config()
        self.config.config['api'].update(base_url='https://relay.alpha.example/v1', api_key='***',
                                         upload_url='https://relay.alpha.example/upload')
        self.config.save_config()
        self.window = MainWindow(self.config, network_time=False)
        self.window.show()
        QTest.qWait(100)
        self.settings = self.window.settings_page

    def tearDown(self):
        self.window.close()
        wait_until(lambda: not self.window._background_busy())
        self.window.deleteLater(); QTest.qWait(30)
        self.temp.cleanup()

    def test_stations_named_categorized_and_switchable_with_persistence(self):
        self.assertEqual(self.settings._stations(), [])
        self.settings._capture_current_station()
        stations = self.settings._stations()
        self.assertEqual(len(stations), 1)
        self.assertEqual(stations[0]['base_url'], 'https://relay.alpha.example/v1')
        self.assertEqual(self.config.config['stations_active'], stations[0]['id'])
        self.assertEqual(len(self.settings.station_rows), 1)

        self.settings._open_station_editor(None)
        self.settings.station_name.setText('创作线')
        self.settings.station_base.setText('https://relay.beta.example/v1')
        self.settings.station_key.setText('sk-beta')
        self.settings.station_caps['video'].setChecked(False)
        self.settings.station_caps['llm'].setChecked(True)
        self.settings._save_station_editor()
        stations = self.settings._stations()
        self.assertEqual(len(stations), 2)
        beta = stations[1]
        self.assertEqual(beta['name'], '创作线')
        self.assertEqual(beta['capabilities'], ['llm'])

        self.settings._set_current_station(beta['id'])
        self.assertEqual(self.config.config['stations_active'], beta['id'])
        self.assertEqual(self.config.config['api']['base_url'], 'https://relay.beta.example/v1')
        self.assertEqual(self.settings.base_url.text(), 'https://relay.beta.example/v1')

        self.settings.base_url.setText('https://relay.beta2.example/v1')
        QTest.qWait(30)
        self.assertEqual(self.settings._stations()[1]['base_url'], 'https://relay.beta2.example/v1')

        restored = ConfigManager(self.config.path).load_config()
        self.assertEqual(len(restored['stations']), 2)
        self.assertEqual(restored['stations'][1]['name'], '创作线')

        self.settings._delete_station(beta['id'])
        self.assertEqual(len(self.settings._stations()), 1)

    def test_settings_rail_navigates_all_categories(self):
        names = list(self.settings._rail_buttons.keys())
        self.assertGreaterEqual(len(names), 8)
        for name, button in self.settings._rail_buttons.items():
            button.click()
            QTest.qWait(20)
            self.assertTrue(button.property('railActive'), f'{name} 分类应获得激活态')


if __name__ == '__main__':
    unittest.main()
