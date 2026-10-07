"""Click business buttons with isolated config and blocked external boundaries."""
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5.QtCore import QPoint, Qt, QTimer
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QAbstractButton, QLabel
from qfluentwidgets import LineEdit

from core.config_manager import ConfigManager
from ui.main_window import MainWindow
from ui.pages.workspace_page import WorkspacePage
try:
    from .test_task_manager import wait_until
except ImportError:
    from test_task_manager import wait_until


class ButtonAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = ExitStack()
        self.network = self.patches.enter_context(patch('requests.sessions.Session.request',
            side_effect=AssertionError('button audit must not access the network')))
        self.history_handlers = {name: self.patches.enter_context(patch.object(WorkspacePage, name, autospec=True))
                                 for name in ('redownload', 'resolve_submission', 'regenerate')}
        self.config = ConfigManager(Path(self.temp.name) / 'config.json')
        self.config.config['api'].update(base_url='https://buttons.invalid/v1', api_key='fixture-only')
        self.config.config['stations'] = [dict(id='alpha', name='Alpha', base_url='https://alpha.invalid/v1',
            api_key='fixture-alpha', upload_url='', upload_api_key='', capabilities=['video'])]
        self.config.config['stations_active'] = ''
        self.config.config['updates']['check_on_start'] = False
        self.config.save_config()
        self.window = MainWindow(self.config, network_time=False, model_sync=False)
        self.window.show()
        self.settings = self.window.settings_page
        QTest.qWait(60)

    def tearDown(self):
        try:
            self.window.close()
            wait_until(lambda: not self.window._background_busy())
            self.window.deleteLater()
            QTest.qWait(50)
            self.network.assert_not_called()
        finally:
            self.patches.close()
            self.temp.cleanup()

    def button(self, widget, text=None, tooltip=None):
        buttons = [button for button in widget.findChildren(QAbstractButton)
                   if (text is None or button.text() == text)
                   and (tooltip is None or button.toolTip() == tooltip)]
        self.assertEqual(len(buttons), 1, [(b.text(), b.toolTip()) for b in buttons])
        return buttons[0]

    def test_all_settings_navigation_and_editor_cancel(self):
        for key, button in list(self.settings._page_buttons.items()):
            button.click()
            self.assertEqual(self.settings._current_page_key(), key)
        self.settings.add_station_button.click()
        self.assertEqual(self.settings._current_page_key(), 'station-new')
        self.button(self.settings.station_editor_host, text='取消').click()
        self.assertEqual(self.settings._current_page_key(), 'stations')

    def test_station_add_capture_edit_save_activate_and_remove_buttons(self):
        settings = self.settings
        settings.add_station_button.click()
        settings.station_name.setText('New station')
        settings.station_base.setText('https://new.invalid/v1')
        settings.station_key.setText('fixture-new')
        self.button(settings.station_editor_host, text='保存').click()
        created = next(row for row in self.config.config['stations'] if row['name'] == 'New station')
        self.assertEqual(created['base_url'], 'https://new.invalid/v1')
        # Each summary row has its own captured station ID.
        self.button(settings.station_rows[0][0], text='编辑').click()
        self.assertEqual(settings._current_page_key(), 'station:alpha')
        page = settings._pages['station:alpha'][1]
        fields = page.findChildren(LineEdit)
        fields[0].setText('Alpha revised')
        self.button(page, text='保存修改').click()
        self.assertEqual(self.config.config['stations'][0]['name'], 'Alpha revised')
        self.button(settings.station_rows[0][0], text='设为当前').click()
        self.assertEqual(self.config.config['stations_active'], 'alpha')
        self.assertEqual(self.window.model_catalog.catalog.api_key, 'fixture-alpha')
        page = settings._pages[f'station:{created["id"]}'][1]
        self.button(page, text='设为当前').click()
        self.assertEqual(self.config.config['stations_active'], created['id'])
        page = settings._pages[f'station:{created["id"]}'][1]
        self.button(page, text='删除该中转站').click()
        self.assertEqual([row['id'] for row in self.config.config['stations']], ['alpha'])
        self.button(settings.station_rows[0][0], tooltip='删除该中转站').click()
        self.assertEqual(self.config.config['stations'], [])
        settings.capture_station_button.click()
        self.assertEqual(len(self.config.config['stations']), 1)
        self.assertEqual(self.config.config['stations'][0]['api_key'], 'fixture-new')
        restored = ConfigManager(self.config.path).load_config()
        self.assertEqual(restored['stations'], self.config.config['stations'])

    def test_secret_reveal_and_model_add_remove(self):
        for field in (self.settings.api_key, self.settings.upload_key):
            button = self.button(field.parentWidget(), tooltip='显示或隐藏密钥')
            self.assertEqual(field.echoMode(), LineEdit.Password)
            button.click()
            self.assertEqual(field.echoMode(), LineEdit.Normal)
            button.click()
            self.assertEqual(field.echoMode(), LineEdit.Password)
        before = len(self.settings.model_rows)
        self.settings.add_model.click()
        self.assertEqual(len(self.settings.model_rows), before + 1)
        row = self.settings.model_rows[-1][0]
        self.button(row, tooltip='删除模型').click()
        self.assertEqual(len(self.settings.model_rows), before)
        self.assertEqual(len(self.config.config['model_pool']['models']), before)

    def test_connection_button_runs_check_and_restores_itself(self):
        result = dict(ok=True, video=dict(ok=True, message='local-fixture'),
                      upload=dict(ok=True, message='local-fixture'))
        with patch('core.api_client.ApiClient.test_connection', return_value=result) as check:
            self.settings.test_button.click()
            wait_until(lambda: not self.settings.jobs.busy)
            check.assert_called_once()
        self.assertEqual(self.settings.last_connection_result, result)
        self.assertTrue(self.settings.test_button.isEnabled())
        self.assertEqual(self.settings.test_button.text(), '测试连接')

    def test_sync_model_button_is_connected_across_main_window(self):
        with patch('core.api_client.ApiClient.fetch_models', return_value=[{'id': 'MiniMax-H3'}]) as fetch:
            self.settings.sync_models_button.click()
            wait_until(lambda: not self.window.model_catalog.jobs.busy)
            fetch.assert_called_once()
        self.assertEqual(self.window.model_catalog.catalog.source, 'upstream')
        self.assertIn('MiniMax-H3', self.window.model_catalog.snapshot())

    def test_git_sync_button_calls_guarded_workflow_without_running_git(self):
        with patch('core.repository_sync.RepositorySync.sync', return_value={'commit': 'fixture-commit'}) as sync:
            self.settings.sync_button.click()
            wait_until(lambda: not self.settings.jobs.busy)
            sync.assert_called_once()
        self.assertTrue(self.settings.sync_button.isEnabled())
        self.assertEqual(self.settings.sync_button.text(), '同步到GitHub')

    def test_license_button_opens_dialog_and_cancel_preserves_configuration(self):
        before = dict(self.config.config['license'])
        with patch('qfluentwidgets.Dialog.exec', return_value=0) as dialog:
            self.button(self.settings._pages['license'][1], text='激活').click()
            dialog.assert_called_once()
        self.assertEqual(self.config.config['license'], before)

    def test_license_dialog_accepts_fixture_key_and_persists(self):
        from core.licensing import make_key, license_status
        key = make_key('Button audit fixture')
        seen = []
        def accept():
            dialog = self.app.activeModalWidget()
            seen.append(dialog)
            if dialog is not None:
                dialog.findChild(LineEdit).setText(key)
                dialog.yesButton.click()
        QTimer.singleShot(100, accept)
        # A guard closes unexpected modal layouts instead of hanging the suite.
        guard = QTimer(self.window)
        guard.setSingleShot(True)
        guard.timeout.connect(lambda: self.app.activeModalWidget().reject()
                              if self.app.activeModalWidget() else None)
        guard.start(1500)
        try:
            self.button(self.settings._pages['license'][1], text='激活').click()
        finally:
            guard.stop()
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0])
        self.assertEqual(self.config.config['license']['key'], key)
        restored = ConfigManager(self.config.path).load_config()
        self.assertEqual(license_status(restored)['state'], 'active')

    def test_history_actions_reach_correct_business_handler_and_os_boundary(self):
        history = self.window.history_page
        task = dict(local_id='history-one', prompt_name='one', model='MiniMax-H3', status='completed',
                    task_id='remote-one', result_path=str(Path(self.temp.name) / 'one.mp4'),
                    output_dir=self.temp.name)
        history.update_history([task])
        row = history.table.cellWidget(0, 8)
        with patch('ui.pages.history_page.open_local') as open_local:
            self.button(row, tooltip='打开文件').click()
            open_local.assert_called_once_with(task['result_path'], history.log_callback)
            open_local.reset_mock()
            history.table.selectRow(0)
            history.open_button.click()
            open_local.assert_called_once_with(task['output_dir'], history.log_callback, folder=True)
        self.button(row, tooltip='重新查询并下载（不创建新任务）').click()
        self.history_handlers['redownload'].assert_called_once_with(self.window.workspace_page, task)
        self.button(row, tooltip='重新生成（需确认，会创建新任务）').click()
        self.history_handlers['regenerate'].assert_called_once_with(self.window.workspace_page, task)
        unknown = dict(task, status='submission_unknown', task_id='', result_path='')
        history.update_history([unknown])
        row = history.table.cellWidget(0, 8)
        self.assertFalse(self.button(row, tooltip='打开文件（暂无本地文件，任务完成下载后可打开）').isEnabled())
        self.assertFalse(self.button(row, tooltip='当前没有可查询的远端 task_id').isEnabled())
        self.button(row, tooltip='处理待确认提交').click()
        self.history_handlers['resolve_submission'].assert_called_once_with(self.window.workspace_page, unknown)
        history.update_history([])
        self.window.switchTo(history)
        history.empty_action.click()
        self.assertIs(self.window.stackedWidget.currentWidget(), self.window.workspace_page)

    def test_main_navigation_and_maximize_buttons(self):
        for page in (self.window.settings_page, self.window.history_page, self.window.workspace_page):
            item = self.window.navigationInterface.widget(page.objectName()).itemWidget
            QTest.mouseClick(item, Qt.LeftButton, pos=QPoint(20, 18))
            self.assertIs(self.window.stackedWidget.currentWidget(), page)
        menu = self.window.navigationInterface.panel.menuButton
        menu.click()
        QTest.qWait(350)
        self.assertTrue(self.config.config['appearance']['nav_expanded'])
        menu.click()
        QTest.qWait(350)
        self.assertFalse(self.config.config['appearance']['nav_expanded'])
        with patch.object(self.window, '_release_titlebar_mouse'):
            self.window.titleBar.maxBtn.click()
            QTest.qWait(180)
            self.assertTrue(self.window.isFullScreen())
            self.window.titleBar.maxBtn.click()
            QTest.qWait(200)
            self.assertFalse(self.window.isFullScreen())
            self.assertFalse(self.window.isMaximized())

    def test_brand_avatar_minimize_and_close_buttons(self):
        self.window.switchTo(self.window.settings_page)
        QTest.mouseClick(self.window.brand_logo, Qt.LeftButton, pos=QPoint(25, 20))
        self.assertIs(self.window.stackedWidget.currentWidget(), self.window.workspace_page)
        about_badges = []
        def inspect_about(dialog):
            about_badges.append(dialog.textLayout.itemAt(0).widget())
            return 0
        with patch('qfluentwidgets.Dialog.exec_', new=inspect_about):
            QTest.mouseClick(self.window.user_avatar, Qt.LeftButton, pos=QPoint(25, 20))
        self.assertEqual(len(about_badges), 1)
        badge = about_badges[0]
        self.assertIsInstance(badge, QLabel)
        self.assertIsNotNone(badge.pixmap())
        self.assertFalse(badge.pixmap().isNull())
        self.assertEqual(badge.pixmap().width(), 56)
        self.window.titleBar.minBtn.click()
        self.assertTrue(self.window.isMinimized())
        self.window.showNormal()
        self.window.titleBar.closeBtn.click()
        self.assertFalse(self.window.isVisible())


if __name__ == '__main__':
    unittest.main()
