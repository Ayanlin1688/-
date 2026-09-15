"""Exercise user-visible state transitions against isolated config files."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt, QTimer, QPoint, QModelIndex
from PyQt5.QtGui import QImage, QColor
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QFileDialog
from qfluentwidgets import ImageLabel, CaptionLabel, SwitchSettingCard, ComboBoxSettingCard, FluentWindow
from core.config_manager import ConfigManager
from core.matcher import StoryboardMatcher
from ui.main_window import MainWindow
from ui.components.match_dialog import MatchDialog
from test_task_manager import wait_until


class InteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config.json"
        self.window = MainWindow(ConfigManager(self.path), network_time=False)
        self.window.show()
        QTest.qWait(40)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        QTest.qWait(30)
        self.temp.cleanup()

    def test_navigation_and_setting_card_types(self):
        self.assertIsInstance(self.window, FluentWindow)
        for page in (self.window.history_page, self.window.settings_page, self.window.workspace_page):
            nav = self.window.navigationInterface.widget(page.objectName())
            QTest.mouseClick(nav.itemWidget, Qt.LeftButton, pos=QPoint(20, 18))
            QTest.qWait(350)
            self.assertIs(self.window.stackedWidget.currentWidget(), page)
            self.assertTrue(page.isVisible())
        settings = self.window.settings_page
        self.assertGreaterEqual(len(settings.findChildren(SwitchSettingCard)), 4)
        self.assertGreaterEqual(len(settings.findChildren(ComboBoxSettingCard)), 7)
        for group in settings.groups:
            settings.scroll.ensureWidgetVisible(group)
            self.app.processEvents()
            self.assertGreater(group.height(), 40)

    def test_round_trip_and_deleted_model_can_be_followed_by_more_edits(self):
        settings = self.window.settings_page
        settings.base_url.setText("https://example.invalid/v1")
        settings.api_key.setText("test-only-secret")
        settings.strategy.setCurrentText("随机")
        settings.cooldown.setValue(63)
        settings.auto_retry.switchButton.setChecked(False)
        settings.open_folder.switchButton.setChecked(True)
        settings.default_ratio.setCurrentText("9:16")
        settings.naming.setText("{提示词名}_demo.mp4")
        settings.language.setCurrentText("English")
        settings.add_model.click()
        removed = settings.model_rows[0][0]
        settings._remove_model_row(removed)
        QTest.qWait(30)
        settings.model_rows[0][2].setCurrentText("video-v2-fast")
        settings.model_rows[0][1].setChecked(False)
        params = self.window.workspace_page.params_card
        params.duration.setValue(12)
        params.audio.setChecked(False)
        params.seed.setText("12345")
        self.window.close()
        reopened = MainWindow(ConfigManager(self.path), network_time=False)
        try:
            self.assertEqual(reopened.settings_page.base_url.text(), "https://example.invalid/v1")
            self.assertEqual(reopened.settings_page.api_key.text(), "test-only-secret")
            self.assertEqual(reopened.settings_page.strategy.currentText(), "随机")
            self.assertEqual(reopened.settings_page.cooldown.value(), 63)
            self.assertFalse(reopened.settings_page.auto_retry.isChecked())
            self.assertTrue(reopened.settings_page.open_folder.isChecked())
            self.assertEqual(reopened.settings_page.default_ratio.currentText(), "9:16")
            self.assertEqual(reopened.settings_page.naming.text(), "{提示词名}_demo.mp4")
            self.assertEqual(reopened.settings_page.language.currentText(), "English")
            self.assertEqual(len(reopened.settings_page.model_rows), 3)
            self.assertFalse(reopened.settings_page.model_rows[0][1].isChecked())
            self.assertEqual(reopened.workspace_page.params_card.duration.value(), 12)
            self.assertFalse(reopened.workspace_page.params_card.audio.isChecked())
            self.assertEqual(reopened.workspace_page.params_card.seed.text(), "12345")
        finally:
            reopened.close()
            reopened.deleteLater()

    def test_animation_reversal_and_logs_filter_export(self):
        workspace = self.window.workspace_page
        workspace.open_controls(); QTest.qWait(60)
        params = workspace.params_card
        params.toggle_advanced()
        QTest.qWait(60)
        params.toggle_advanced()
        wait_until(lambda: params.advanced.isVisible(), timeout=5000)
        self.assertTrue(params.advanced.isVisible())
        self.assertGreaterEqual(params.seed.height(), 24)
        workspace.controls_dialog.close()
        logs = workspace.log_drawer
        logs.toggle()
        wait_until(lambda: logs.browser.isHidden(), timeout=5000)
        self.assertTrue(logs.browser.isHidden())
        logs.toggle()
        wait_until(lambda: logs.browser.isVisible(), timeout=5000)
        self.assertTrue(logs.browser.isVisible())
        logs.clear()
        workspace.start_button.click()
        logs.append_log("<bad> & literal", "error")
        logs.filter_box.setCurrentText("ERROR")
        self.assertIn("<bad> & literal", logs.browser.toPlainText())
        self.assertNotIn("开始生成", logs.browser.toPlainText())
        out = Path(self.temp.name) / "execution.log"
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(out), "")):
            logs.export()
        self.assertIn("开始生成", out.read_text(encoding="utf-8"))
        logs.clear()
        self.assertEqual(logs.browser.toPlainText(), "")

    def test_real_image_binding_and_local_save(self):
        path = Path(self.temp.name) / "真实图片.png"
        image = QImage(120, 90, QImage.Format_RGB32)
        image.fill(QColor("#22c55e"))
        self.assertTrue(image.save(str(path)))
        prompt = str((Path(self.temp.name) / '提示词.txt').resolve())
        dialog = MatchDialog(self.window, config_manager=self.window.config_manager,
                             matches=[dict(prompt_path=prompt, prompt_name='提示词', images=[])])
        dialog.show()
        try:
            with patch.object(QFileDialog, "getOpenFileNames", return_value=([str(path)], "")):
                dialog._add()
            item = dialog.picture_list.item(0)
            thumb = dialog.picture_list.itemWidget(item).findChild(ImageLabel)
            self.assertEqual(thumb.pixmap().size().width(), 80)
            self.assertEqual(thumb.pixmap().toImage().pixelColor(40, 40).name(), "#22c55e")
            dialog._save()
            manager = ConfigManager(self.path)
            manager.load_config()
            self.assertEqual(manager.config['match_overrides'][prompt][-1], str(path.resolve()))
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_about_closes_and_path_selection_saves(self):
        QTimer.singleShot(100, lambda: self.app.activeModalWidget().accept())
        self.window.show_about()
        source = self.window.workspace_page.data_source
        with patch.object(QFileDialog, "getExistingDirectory", return_value=self.temp.name):
            source._choose("images", source.fields["images"])
        self.assertEqual(self.window.config_manager.config['paths']['images'], str(Path(self.temp.name).resolve()))

    def test_automatic_three_image_dialog_reorders_and_persists_picture_mapping(self):
        root = Path(self.temp.name).resolve()
        prompt = root / '玫瑰毯子1.txt'; prompt.write_text('<Picture 1> <Picture 3>', encoding='utf-8')
        paths = [str(root / f'1 ({i}).jpg') for i in (1, 2, 3)]
        for path in paths:
            image = QImage(90, 100, QImage.Format_RGB32); image.fill(QColor('green')); image.save(path)
        matches = StoryboardMatcher().match_files([prompt], paths)
        dialog = MatchDialog(self.window, config_manager=self.window.config_manager, matches=matches)
        dialog.show()
        try:
            self.assertEqual(dialog.picture_list.count(), 3)
            self.assertTrue(any('已绑定3张' in label.text() for label in dialog.findChildren(CaptionLabel)))
            model = dialog.picture_list.model()
            self.assertTrue(model.moveRows(QModelIndex(), 2, 1, QModelIndex(), 0))
            self.app.processEvents()
            expected = [paths[2], paths[0], paths[1]]
            for index, path in enumerate(expected):
                item = dialog.picture_list.item(index)
                self.assertEqual(item.data(Qt.UserRole), path)
                host = dialog.picture_list.itemWidget(item)
                text = '\n'.join(label.text() for label in host.findChildren(CaptionLabel))
                self.assertIn(f'Picture {index+1}', text)
                self.assertIn(Path(path).name, text)
            dialog._save()
            config = ConfigManager(self.path).load_config()
            rebound = StoryboardMatcher.from_config(config).match_files([prompt], paths)[0]
            self.assertEqual(rebound['images'], expected)
        finally:
            dialog.close(); dialog.deleteLater()

    def test_workspace_cards_and_parameter_dialog_preserve_error_state(self):
        workspace = self.window.workspace_page
        for width in (1400, 1100):
            self.window.resize(width, 900)
            QTest.qWait(50)
            cards = workspace.summary.cards
            self.assertLessEqual(max(c.width() for c in cards)-min(c.width() for c in cards), 1)
            self.assertTrue(all(c.geometry().right() < cards[i+1].x() for i, c in enumerate(cards[:-1])))
        workspace.open_controls(); QTest.qWait(60)
        workspace.center_scroll.ensureWidgetVisible(workspace.recent_panel)
        self.assertTrue(workspace.recent_panel.empty_state.isVisible())
        self.assertFalse(workspace.recent_panel.list.isVisible())
        tasks = [dict(prompt_name='上传失败', status='failed', model='video-v3', images=[], task_id=''),
                 dict(prompt_name='跳过任务', status='skipped', model='video-v3', images=[], task_id='')]
        workspace.queue_panel.update_tasks(tasks)
        self.assertEqual(workspace.queue_panel.list.item(0).data(Qt.UserRole), '失败')
        self.assertEqual(workspace.queue_panel.list.item(1).data(Qt.UserRole), '已跳过')
        workspace.current_task.update_task(0, tasks[0])
        self.assertTrue(workspace.current_task.progress.isError())
        self.assertIn('待提交', workspace.current_task.details.text())
        self.assertIn('失败', workspace.current_task.title.text())
        self.assertEqual(workspace.current_task.progress.darkBackgroundColor.name(), '#f56c6c')
