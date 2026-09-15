"""Visual motion must not move hit targets permanently or keep hidden pages busy."""
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import QEvent, Qt, QPoint
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QWidget
from qfluentwidgets import TransparentToolButton
from core.config_manager import ConfigManager
from ui.main_window import MainWindow
from test_task_manager import wait_until


class VisualMotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = ConfigManager(Path(self.temp.name) / 'config.json')
        self.window = MainWindow(self.config, network_time=False)
        self.window.show(); QTest.qWait(100)

    def tearDown(self):
        self.window.close(); wait_until(lambda: not self.window._background_busy())
        self.window.deleteLater(); QTest.qWait(30)
        self.temp.cleanup()

    def test_button_hover_and_press_return_to_layout_without_duplicate_click(self):
        self.window.workspace_page.open_controls(); QTest.qWait(60)
        button = self.window.workspace_page.params_card.expand_button
        start = button.geometry()
        QApplication.sendEvent(button, QEvent(QEvent.Enter)); QTest.qWait(230)
        self.assertGreater(button.width(), start.width())
        hits = []; button.clicked.connect(lambda: hits.append(1))
        QTest.mousePress(button, Qt.LeftButton); QTest.qWait(220)
        self.assertLess(button.width(), start.width())
        QTest.mouseRelease(button, Qt.LeftButton)
        QApplication.sendEvent(button, QEvent(QEvent.Leave)); QTest.qWait(250)
        self.assertEqual(hits, [1])
        self.assertEqual(button.geometry(), start)

    def test_card_hover_reversal_never_accumulates_position_drift(self):
        self.window.workspace_page.open_controls(); QTest.qWait(60)
        from ui.components.custom_widgets import StudioCard
        card = self.window.workspace_page.data_source.findChild(StudioCard)
        # A real Windows cursor may already have entered the newly shown card.
        # Measure from its resting state, not a partially completed hover.
        QApplication.sendEvent(card, QEvent(QEvent.Leave)); QTest.qWait(230)
        start = card.pos()
        for _ in range(3):
            QApplication.sendEvent(card, QEvent(QEvent.Enter)); QTest.qWait(230)
            self.assertEqual(card.y(), start.y()-2)
            QApplication.sendEvent(card, QEvent(QEvent.Leave)); QTest.qWait(230)
            self.assertEqual(card.pos(), start)

    def test_shimmer_moves_and_stops_when_hidden_or_failed(self):
        page = self.window.workspace_page
        page._tasks_updated([dict(prompt_name='进度示例', status='processing', model='video-v3', images=[], progress=55)])
        bar = page.queue_panel.rows[0].progress
        bar.setValue(55); QTest.qWait(250)
        first = bar.grab().toImage()
        # The band deliberately leaves the filled segment each cycle. Compare
        # across a full period so two samples in that quiet gap don't flake.
        wait_until(lambda: first != bar.grab().toImage(), timeout=2200)
        self.window.switchTo(self.window.settings_page); QTest.qWait(400)
        self.assertFalse(bar.property('shimmerRunning'))
        self.window.switchTo(self.window.workspace_page); QTest.qWait(400)
        self.window.workspace_page.queue_panel.list.scrollToTop()
        QTest.qWait(100)
        self.assertTrue(bar.property('shimmerRunning'))
        bar.setError(True); QTest.qWait(100)
        self.assertFalse(bar.property('shimmerRunning'))

    def test_rapid_navigation_finishes_on_latest_page_and_releases_overlay(self):
        self.window.switchTo(self.window.history_page); QTest.qWait(60)
        self.assertTrue(getattr(self.window, 'page_transition', None))
        self.window.switchTo(self.window.settings_page); QTest.qWait(60)
        self.window.switchTo(self.window.workspace_page); QTest.qWait(400)
        self.assertIs(self.window.stackedWidget.currentWidget(), self.window.workspace_page)
        self.assertFalse(self.window.page_transition.isVisible())
        self.assertTrue(self.window.workspace_page.start_button.isEnabled())

    def test_combo_menu_selection_and_switch_keep_persisting_once(self):
        settings = self.window.settings_page
        self.window.switchTo(settings); QTest.qWait(350)
        settings.scroll.ensureWidgetVisible(settings.strategy); QTest.qWait(60)
        combo = settings.strategy
        activated = []; combo.activated.connect(activated.append)
        QTest.mouseClick(combo, Qt.LeftButton); QTest.qWait(250)
        menu = combo.dropMenu
        self.assertIsNotNone(menu)
        self.assertTrue(menu.isVisible())
        item = menu.view.item(1)
        QTest.mouseClick(menu.view.viewport(), Qt.LeftButton, pos=menu.view.visualItemRect(item).center())
        QTest.qWait(250)
        self.assertEqual(activated, [1])
        self.assertEqual(self.config.config['model_pool']['strategy'], '随机')
        switch = settings.failover.switchButton
        settings.scroll.ensureWidgetVisible(switch); QTest.qWait(230)
        before = switch.isChecked()
        changes = []; switch.checkedChanged.connect(changes.append)
        old_x = switch.indicator.sliderX
        QTest.mouseClick(switch.indicator, Qt.LeftButton); QTest.qWait(65)
        middle_x = switch.indicator.sliderX
        self.assertGreater(middle_x, 5)
        self.assertLess(middle_x, 25)
        self.assertNotEqual(middle_x, old_x)
        QTest.qWait(200)
        self.assertEqual(changes, [not before])
        self.assertEqual(self.config.config['model_pool']['auto_failover'], not before)

    def test_status_rebuild_and_minimize_stop_visual_clock_then_resume(self):
        from ui.motion import StatusDot
        queue = self.window.workspace_page.queue_panel
        task = dict(prompt_name='动效测试', status='processing', model='video-v3', images=[])
        for _ in range(4):
            queue.update_tasks([task]); QTest.qWait(50)
        dot = queue.list.itemWidget(queue.task_item(0)).findChild(StatusDot)
        first = dot.grab().toImage(); QTest.qWait(330)
        self.assertTrue(dot.property('pulseRunning'))
        self.assertNotEqual(first, dot.grab().toImage())
        self.window.showMinimized(); QTest.qWait(120)
        self.assertFalse(dot.property('pulseRunning'))
        self.assertFalse(self.window._visual_clock.timer.isActive())
        self.window.showNormal(); QTest.qWait(200)
        self.assertTrue(dot.property('pulseRunning'))
        self.assertEqual(dot.color.name(), '#3b82f6')

    def test_dynamic_model_row_button_has_motion_and_remains_clickable(self):
        settings = self.window.settings_page
        self.window.switchTo(settings); QTest.qWait(350)
        count = len(settings.model_rows)
        settings.add_model.click(); QTest.qWait(150)
        row = settings.model_rows[-1][0]
        button = row.findChild(TransparentToolButton)
        self.assertIsNotNone(getattr(button, '_studio_motion', None))
        settings.scroll.ensureWidgetVisible(row); QTest.qWait(100)
        QApplication.sendEvent(button, QEvent(QEvent.Enter)); QTest.qWait(230)
        QTest.mouseClick(button, Qt.LeftButton); QTest.qWait(250)
        self.assertEqual(len(settings.model_rows), count)
        self.assertEqual(len(self.config.config['model_pool']['models']), count)

    def test_breathing_dot_resumes_after_scrolling_back_into_view(self):
        from ui.motion import StatusDot
        queue = self.window.workspace_page.queue_panel
        queue.update_tasks([dict(prompt_name=f'任务{i}', model='video-v3', images=[],
            status='processing' if i == 0 else 'waiting') for i in range(40)])
        QTest.qWait(200)
        dot = queue.list.itemWidget(queue.task_item(0)).findChild(StatusDot)
        self.assertTrue(dot.property('pulseRunning'))
        scrollbar = queue.list.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum()); QTest.qWait(200)
        self.assertFalse(dot.property('pulseRunning'))
        self.assertFalse(self.window._visual_clock.timer.isActive())
        scrollbar.setValue(0); QTest.qWait(200)
        self.assertTrue(dot.property('pulseRunning'))

    def test_crossfade_really_fades_out_old_transparent_page_content(self):
        workspace = self.window.workspace_page
        old_marker = QWidget(workspace)
        old_marker.setStyleSheet('background:#ff0000;')
        old_marker.setGeometry(500, 60, 30, 30); old_marker.show(); old_marker.raise_()
        new_marker = QWidget(self.window.history_page)
        new_marker.setStyleSheet('background:#00ff00;')
        new_marker.setGeometry(550, 60, 30, 30); new_marker.show(); new_marker.raise_()
        QTest.qWait(50)
        self.window.switchTo(self.window.history_page)
        fade = self.window.page_transition
        fade.animation.stop()
        old_point = old_marker.mapTo(workspace, QPoint(15, 15))
        new_point = new_marker.mapTo(self.window.history_page, QPoint(15, 15))
        samples = []
        for blend in (0., .5, 1.):
            fade.blend = blend
            image = fade.grab().toImage()
            samples.append((image.pixelColor(old_point), image.pixelColor(new_point)))
        self.assertGreater(samples[0][0].red(), samples[1][0].red()+80)
        self.assertGreater(samples[1][0].red(), samples[2][0].red()+80)
        self.assertLess(samples[0][1].green()+80, samples[1][1].green())
        self.assertLess(samples[1][1].green()+80, samples[2][1].green())
        fade.finish()

    def test_long_press_near_original_edge_still_clicks_once(self):
        self.window.workspace_page.open_controls(); QTest.qWait(60)
        button = self.window.workspace_page.params_card.expand_button
        global_press = button.mapToGlobal(QPoint(1, button.height()//2))
        QApplication.sendEvent(button, QEvent(QEvent.Enter)); QTest.qWait(230)
        hits = []; button.clicked.connect(lambda: hits.append(1))
        QTest.mousePress(button, Qt.LeftButton, pos=button.mapFromGlobal(global_press)); QTest.qWait(230)
        QTest.mouseRelease(button, Qt.LeftButton, pos=button.mapFromGlobal(global_press)); QTest.qWait(250)
        self.assertEqual(hits, [1])

    def test_router_back_retargets_an_active_transition(self):
        from qfluentwidgets import qrouter
        self.window.switchTo(self.window.history_page); QTest.qWait(350)
        self.window.switchTo(self.window.settings_page); QTest.qWait(60)
        qrouter.pop()
        self.assertIs(self.window.stackedWidget.currentWidget(), self.window.history_page)
        self.assertTrue(self.window.page_transition.isVisible())
        wait_until(lambda: not self.window.page_transition.isVisible(), timeout=5000)
        self.assertFalse(self.window.page_transition.isVisible())

    def test_match_dialog_has_opaque_dark_material(self):
        from ui.components.match_dialog import MatchDialog
        dialog = MatchDialog(self.window, config_manager=self.config)
        try:
            dialog.show(); QTest.qWait(100)
            image = dialog.grab().toImage()
            background = image.pixelColor(10, image.height()//2)
            self.assertEqual(background.alpha(), 255)
            self.assertLess(background.lightness(), 30)
        finally:
            dialog.close(); dialog.deleteLater(); QTest.qWait(30)
