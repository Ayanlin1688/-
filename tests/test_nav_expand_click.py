"""导航项展开态点击回归：点击后不得收缩消失（QFluentWidgets 展开动画 sizeHint=0 缺陷）。

实测复现：展开态点击「历史记录/设置/工作台」→ setExpanded 动画按 sizeHint（宽 0）设置尺寸，
被点击项宽度归零从界面消失；用户侧表现为“点击后就消失了”。

另含导航指示条单例回归（需求9）：自绘 3px accent 竖条 + 原生 setIndicatorColor 同时绘制
= 展开态每个导航项左侧两根竖杆。修复后只保留原生指示条，这里用像素统计守住该不变量。
"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtGui import QColor
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from tests.qt_scale_support import reset_interface_scaling
from ui import materials
from ui.tokens import px


class NavExpandClickTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        temp = tempfile.TemporaryDirectory()
        manager = ConfigManager(Path(temp.name) / 'config.json')
        manager.load_config()
        window = MainWindow(manager, network_time=False)
        window.resize(1200, 800)
        window.show()
        QTest.qWait(400)
        return temp, window

    def test_click_in_expand_mode_keeps_item_width(self):
        temp, window = self._window()
        try:
            nav = window.navigationInterface
            if nav.panel.isCollapsed():
                window._toggle_navigation()          # 展开（240 宽文字导航）
                QTest.qWait(600)
            for route in ('historyPage', 'settingsPage', 'workspacePage'):
                item = nav.widget(route)
                inner = item.itemWidget
                inner.itemClicked.emit(True, False)  # 与真实点击同一条处理链（叶子 clickArrow 恒 False）
                QTest.qWait(400)
                self.assertGreater(item.width(), 120,
                                   f'{route} 点击后宽度异常：{item.width()}（应保持展开宽度，而非收缩消失）')
        finally:
            window.close()
            window.deleteLater()
            QTest.qWait(200)
            temp.cleanup()

    def test_expand_toggle_round_trip_keeps_widths(self):
        temp, window = self._window()
        try:
            nav = window.navigationInterface
            for _ in range(2):
                window._toggle_navigation()
                QTest.qWait(500)
                for route in ('historyPage', 'settingsPage'):
                    item = nav.widget(route)
                    inner = item.itemWidget
                    inner.itemClicked.emit(True, False)
                    QTest.qWait(250)
            for route in ('historyPage', 'settingsPage', 'workspacePage'):
                self.assertGreater(nav.widget(route).width(), 40,
                                   f'{route} 宽度异常：{nav.widget(route).width()}')
        finally:
            window.close()
            window.deleteLater()
            QTest.qWait(200)
            temp.cleanup()


class NavIndicatorSingletonTests(unittest.TestCase):
    """需求9：导航指示条只能由原生 setIndicatorColor 画一次（像素级回归）。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        reset_interface_scaling(self.app)
        self.addCleanup(reset_interface_scaling, self.app)
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = ConfigManager(Path(self.temp.name) / 'config.json')
        self.manager.load_config()
        self.window = MainWindow(self.manager, network_time=False)
        self.window.resize(1200, 800)
        self.window.show()
        QTest.qWait(300)
        # 滑动指示条动画只影响面板层叠加，关闭后选中态同步落位，便于稳定做像素统计。
        self.window.navigationInterface.setIndicatorAnimationEnabled(False)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QTest.qWait(100)

    @staticmethod
    def _is_accent(color, accent):
        return ((color.red() - accent.red()) ** 2
                + (color.green() - accent.green()) ** 2
                + (color.blue() - accent.blue()) ** 2) <= 2500

    def _accent_runs(self, inner, accent):
        image = inner.grab().toImage()
        columns = []
        for x in range(min(image.width(), 40)):
            rows = [y for y in range(image.height())
                    if self._is_accent(QColor(image.pixel(x, y)), accent)]
            if len(rows) >= 2:
                columns.append((x, min(rows), max(rows)))
        runs = []
        for x, top, bottom in columns:
            if runs and x - runs[-1][1] <= 1:
                runs[-1][1] = x
                runs[-1][2] = min(runs[-1][2], top)
                runs[-1][3] = max(runs[-1][3], bottom)
            else:
                runs.append([x, x, top, bottom])
        return runs

    def _assert_single_indicator(self, route):
        item = self.window.navigationInterface.widget(route)
        inner = item.itemWidget
        self.assertTrue(item.isSelected, f'{route} 未处于选中态，无法校验指示条')
        accent = QColor(materials.palette()['accent'])
        runs = self._accent_runs(inner, accent)
        self.assertEqual(len(runs), 1,
                         f'{route} 应只有一根原生指示条，实际 {len(runs)} 根：{runs}')
        left, right, top, bottom = runs[0]
        expected_width = max(1, px(3))
        expected_height = max(2, px(16))
        margins = inner._margins()
        self.assertLessEqual(abs(left - margins.left()), 2,
                             f'{route} 指示条未贴左内边距：left={left} margins={margins.left()}')
        self.assertLessEqual(right - left + 1, expected_width + 2,
                             f'{route} 指示条过宽：{right - left + 1}px（期望 {expected_width}px）')
        height = bottom - top + 1
        self.assertLessEqual(abs(height - expected_height), 2,
                             f'{route} 指示条高度异常：{height}px（期望 {expected_height}px）')
        center = (top + bottom) / 2.0
        self.assertLessEqual(abs(center - (inner.height() - 1) / 2.0), 2,
                             f'{route} 指示条未垂直居中：center={center} inner_height={inner.height()}')
        return runs

    def test_default_collapsed_rail_has_single_indicator(self):
        self.window.switchTo(self.window.workspace_page)
        QTest.qWait(60)
        self._assert_single_indicator('workspacePage')

    def test_expanded_and_collapsed_toggles_keep_single_indicator(self):
        self.window.switchTo(self.window.workspace_page)
        QTest.qWait(60)
        for round_index in range(10):
            expanded = round_index % 2 == 0
            if self.window.navigationInterface.panel.isCollapsed() == expanded:
                self.window._toggle_navigation()
                QTest.qWait(450)
            self.window._sync_nav_pane(self.window.navigationInterface.panel.isCollapsed())
            self.window.switchTo(self.window.workspace_page)
            QTest.qWait(40)
            self._assert_single_indicator('workspacePage')

    def test_ten_page_switch_rounds_never_stack_indicators(self):
        pages = {
            'workspacePage': self.window.workspace_page,
            'settingsPage': self.window.settings_page,
            'historyPage': self.window.history_page,
            'ecommercePage': self.window.ecommerce_page,
        }
        groups = (('workspacePage', 'settingsPage'), ('historyPage', 'ecommercePage'))
        for round_index in range(10):
            for route in groups[round_index % len(groups)]:
                self.window.switchTo(pages[route])
                QTest.qWait(30)
                self._assert_single_indicator(route)

    def test_indicator_geometry_tracks_interface_scale(self):
        panel = self.window.navigationInterface.panel
        for choice in ('1.0', '1.25', '1.5', '1.75'):
            with self.subTest(scale=choice):
                self.manager.config['appearance']['ui_scale'] = choice
                self.window._apply_interface_scale()
                if not panel.isCollapsed():
                    panel.collapse()
                    QTest.qWait(300)
                self.window._sync_nav_pane(True)
                self.window.switchTo(self.window.workspace_page)
                QTest.qWait(80)
                self._assert_single_indicator('workspacePage')
                self.assertEqual(px(3), round(3 * float(choice)))
        self.manager.config['appearance']['ui_scale'] = '1.0'
        self.window._apply_interface_scale()
        self.window._sync_nav_pane(True)
        self.window.switchTo(self.window.workspace_page)
        QTest.qWait(80)
        self._assert_single_indicator('workspacePage')


if __name__ == '__main__':
    unittest.main()
