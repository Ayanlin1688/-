"""导航项展开态点击回归：点击后不得收缩消失（QFluentWidgets 展开动画 sizeHint=0 缺陷）。

实测复现：展开态点击「历史记录/设置/工作台」→ setExpanded 动画按 sizeHint（宽 0）设置尺寸，
被点击项宽度归零从界面消失；用户侧表现为“点击后就消失了”。

选中态按段10改为蓝色实心圆角条、白色图标/文字；像素回归同时守住单一选中项，
展开/折叠和四档缩放不得叠加旧竖线或丢失选中背景。
"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import QPoint, Qt
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


class NavSelectionSingletonTests(unittest.TestCase):
    """段10：蓝色圆角条只标识当前页，白色图标/文字在两态均可读。"""

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

    def _assert_single_selection(self, route):
        item = self.window.navigationInterface.widget(route)
        inner = item.itemWidget
        self.assertTrue(item.isSelected, route)
        self.assertEqual(inner.height(), px(44))
        image = inner.grab().toImage()
        blue = QColor(materials.ecommerce_color('navigation_selected'))
        # Away from glyphs the entire row is filled, including both edges.
        for y in (px(5), image.height() - px(5)):
            for x in range(px(9), image.width() - px(9)):
                self.assertEqual(image.pixelColor(x, y), blue, (route, x, y))
        self.assertNotEqual(image.pixelColor(0, 0), blue, 'selection must have rounded corners')

        def white_pixels(left, right):
            return sum(1 for x in range(left, right)
                       for y in range(px(10), image.height() - px(10))
                       if min(image.pixelColor(x, y).getRgb()[:3]) >= 235)

        icon_left = (inner.width() - px(16)) // 2 if inner.isCompacted else px(12)
        self.assertGreater(white_pixels(icon_left, icon_left + px(16)), px(5), route)
        if not inner.isCompacted:
            self.assertGreater(white_pixels(px(38), image.width() - px(12)), px(10), route)
        selected = []
        for page in (self.window.workspace_page, self.window.image_generation_page,
                     self.window.video_creation_page, self.window.ecommerce_page,
                     self.window.history_page, self.window.settings_page):
            other = self.window.navigationInterface.widget(page.objectName())
            if other.isSelected:
                selected.append(page.objectName())
            else:
                snapshot = other.itemWidget.grab().toImage()
                self.assertNotEqual(snapshot.pixelColor(snapshot.width() // 2, px(5)), blue)
        self.assertEqual(selected, [route])

    def test_default_collapsed_rail_has_single_filled_selection(self):
        self.window.switchTo(self.window.workspace_page)
        QTest.qWait(60)
        self._assert_single_selection('workspacePage')

    def test_expanded_and_collapsed_toggles_keep_single_filled_selection(self):
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
            self._assert_single_selection('workspacePage')

    def test_ten_page_switch_rounds_never_stack_selections(self):
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
                self._assert_single_selection(route)

    def test_filled_selection_tracks_interface_scale(self):
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
                self._assert_single_selection('workspacePage')
                self.assertEqual(px(44), round(44 * float(choice)))
        self.manager.config['appearance']['ui_scale'] = '1.0'
        self.window._apply_interface_scale()
        self.window._sync_nav_pane(True)
        self.window.switchTo(self.window.workspace_page)
        QTest.qWait(80)
        self._assert_single_selection('workspacePage')

    def test_full_row_edges_click_through_at_all_scales_in_both_modes(self):
        panel = self.window.navigationInterface.panel
        pages = (self.window.workspace_page, self.window.image_generation_page,
                 self.window.video_creation_page, self.window.ecommerce_page,
                 self.window.history_page, self.window.settings_page)
        for scale in ('1.0', '1.25', '1.5', '1.75'):
            self.manager.config['appearance']['ui_scale'] = scale
            self.window._apply_interface_scale()
            for collapsed in (True, False):
                if panel.isCollapsed() != collapsed:
                    self.window._toggle_navigation()
                    QTest.qWait(350)
                self.window._sync_nav_pane(collapsed)
                for page in pages:
                    inner = self.window.navigationInterface.widget(page.objectName()).itemWidget
                    for point in (QPoint(1, inner.height() // 2),
                                  QPoint(inner.width() - 2, inner.height() // 2),
                                  QPoint(inner.width() // 2, inner.height() - 2)):
                        with self.subTest(scale=scale, collapsed=collapsed,
                                          route=page.objectName(), point=point):
                            self.window.switchTo(pages[1] if page is pages[0] else pages[0])
                            QTest.mouseClick(inner, Qt.LeftButton, pos=point)
                            self.assertIs(self.window.stackedWidget.currentWidget(), page)
                            self.assertEqual(inner.height(), px(44))
                            self.assertEqual(inner.size(), inner.parentWidget().size())


if __name__ == '__main__':
    unittest.main()
