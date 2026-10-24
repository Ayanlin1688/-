"""模型同步 InfoBar 单例回归（需求6）：不堆叠、1.5s 合并、不吞其它提示。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt5 import sip
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from core.config_manager import ConfigManager
from tests.test_task_manager import wait_until
from ui.pages.settings_page import SettingsPage


class _InfoBarFactoryFixture:
    """同时接管 success / warning 工厂，记录创建顺序与调用参数。"""

    def __init__(self, test):
        self.test = test
        self.bars = []
        self.calls = []

    def __call__(self, *args, **kwargs):
        bar = Mock(name=f'infobar-{len(self.bars)}')
        bar.closedSignal = Mock()
        self.bars.append(bar)
        self.calls.append((args, kwargs))
        return bar

    def __enter__(self):
        self.success = patch('ui.pages.settings_page.InfoBar.success', side_effect=self)
        self.warning = patch('ui.pages.settings_page.InfoBar.warning', side_effect=self)
        self.success_mock = self.success.start()
        self.warning_mock = self.warning.start()
        self.test.addCleanup(self.success.stop)
        self.test.addCleanup(self.warning.stop)
        return self

    def __exit__(self, *exc_info):
        return False


class ModelSyncInfoBarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from tests.qt_scale_support import reset_interface_scaling
        reset_interface_scaling(self.app)
        self.addCleanup(reset_interface_scaling, self.app)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = ConfigManager(Path(self.temp.name) / 'config.json')
        self.page = SettingsPage(self.config, lambda *_: None)
        self.page.resize(1100, 800)
        self.page.show()
        QTest.qWait(20)
        self.factory = _InfoBarFactoryFixture(self)
        self.factory.__enter__()

    def tearDown(self):
        self.page.stop_timers()
        self.page.close()
        self.page.deleteLater()
        QTest.qWait(20)

    def test_seven_rapid_sync_events_leave_single_latest_infobar(self):
        for index in range(7):
            self.page.model_sync_finished(True, 47 + index, f'已同步：第 {index} 次')
        self.assertEqual(self.factory.bars, [], '防抖窗口内不得弹任何 InfoBar')
        self.assertTrue(self.page._model_sync_timer.isActive())
        wait_until(lambda: len(self.factory.bars) == 1)
        self.assertEqual(len(self.factory.bars), 1)
        self.assertEqual(len(self.factory.calls), 1)
        title, message = self.factory.calls[0][0][:2]
        self.assertEqual(title, '模型同步')
        self.assertEqual(message, '已同步：第 6 次')
        self.assertEqual(self.factory.success_mock.call_count, 1)
        self.factory.warning_mock.assert_not_called()
        self.assertIs(self.page._model_sync_infobar, self.factory.bars[0])

    def test_triggers_within_debounce_window_merge_into_latest(self):
        self.page.model_sync_finished(True, 47, '旧消息')
        QTest.qWait(700)
        self.page.model_sync_finished(True, 51, '合并后的最新消息')
        self.assertEqual(self.factory.bars, [])
        self.assertGreaterEqual(self.page._model_sync_timer.remainingTime(), 1400)
        wait_until(lambda: len(self.factory.bars) == 1)
        self.assertEqual(len(self.factory.bars), 1)
        self.assertEqual(self.factory.calls[0][0][1], '合并后的最新消息')

    def test_previous_bar_is_closed_before_the_replacement(self):
        self.page.model_sync_finished(True, 47, '第一次同步')
        wait_until(lambda: len(self.factory.bars) == 1)
        old = self.factory.bars[0]
        self.assertIs(self.page._model_sync_infobar, old)
        self.page.model_sync_finished(True, 51, '第二次同步')
        self.assertEqual(old.close.call_count, 1, '新同步必须先关闭旧 InfoBar')
        self.assertIsNone(self.page._model_sync_infobar)
        # 旧条晚到的关闭信号不能清掉新条引用。
        late_handler = old.closedSignal.connect.call_args[0][0]
        late_handler()
        wait_until(lambda: len(self.factory.bars) == 2)
        self.assertIs(self.page._model_sync_infobar, self.factory.bars[1])
        self.assertEqual(self.factory.calls[1][0][1], '第二次同步')

    def test_failure_uses_warning_and_still_keeps_a_single_bar(self):
        self.page.model_sync_finished(False, 0, '1 条线路失败已用离线缓存')
        wait_until(lambda: len(self.factory.bars) == 1)
        self.factory.warning_mock.assert_called_once()
        self.factory.success_mock.assert_not_called()
        self.page.model_sync_finished(True, 51, '已同步：kkone 47 个、DeepSeek 4 个')
        wait_until(lambda: len(self.factory.bars) == 2)
        self.assertEqual(self.factory.bars[0].close.call_count, 1)
        self.factory.success_mock.assert_called_once()
        self.assertEqual(len(self.factory.bars), 2)
        self.assertIs(self.page._model_sync_infobar, self.factory.bars[1])

    def test_non_sync_infobar_is_not_closed_or_replaced(self):
        other = self.page._show_result_infobar('测试连接', '其它能力的提示', 'success')
        self.assertTrue(other.isVisible())
        self.page.model_sync_finished(True, 51, '已同步：kkone 47 个')
        wait_until(lambda: len(self.factory.bars) == 1)
        self.page.model_sync_finished(True, 52, '已同步：kkone 47 个、DeepSeek 4 个')
        wait_until(lambda: len(self.factory.bars) == 2)
        self.assertFalse(sip.isdeleted(other))
        self.assertTrue(other.isVisible(), '模型同步提示不得吞掉其它 InfoBar')
        self.assertIsNot(self.page._model_sync_infobar, other)
        other.close()

    def test_stop_timers_cancels_pending_and_closes_visible_sync_bar(self):
        self.page.model_sync_finished(True, 47, '待展示')
        self.assertTrue(self.page._model_sync_timer.isActive())
        self.page.stop_timers()
        self.assertFalse(self.page._model_sync_timer.isActive())
        self.assertIsNone(self.page._model_sync_pending)
        self.assertEqual(self.factory.bars, [])
        wait_until(lambda: self.page._model_sync_timer.isActive() is False)
        self.page.model_sync_finished(True, 51, '已展示')
        wait_until(lambda: len(self.factory.bars) == 1)
        bar = self.factory.bars[0]
        self.page.stop_timers()
        self.assertEqual(bar.close.call_count, 1)
        self.assertIsNone(self.page._model_sync_infobar)


class ModelSyncInfoBarWiringTests(unittest.TestCase):
    """手动刷新（按钮）与自动同步（控制器信号）都走同一条单例槽。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_manual_and_automatic_refresh_share_one_infobar(self):
        from ui.main_window import MainWindow

        with tempfile.TemporaryDirectory() as folder:
            config = ConfigManager(Path(folder) / 'config.json')
            factory = _InfoBarFactoryFixture(self)
            with factory:
                window = MainWindow(config, network_time=False)
                try:
                    window.show()
                    QTest.qWait(60)
                    # 手动刷新：未配置 API Key 时控制器同步返回失败信号（离线，无网络）。
                    window.settings_page.sync_models_button.click()
                    self.assertTrue(window.settings_page._model_sync_timer.isActive())
                    # 自动同步：控制器完成时发出同一条 sync_finished 信号。
                    window.model_catalog.sync_finished.emit(
                        True, 51, '已同步：kkone 47 个、DeepSeek 4 个')
                    self.assertEqual(factory.bars, [], '1.5s 内连续触发必须合并为一条')
                    wait_until(lambda: len(factory.bars) == 1)
                    self.assertEqual(len(factory.bars), 1)
                    self.assertEqual(factory.calls[0][0][1],
                                     '已同步：kkone 47 个、DeepSeek 4 个')
                    factory.warning_mock.assert_not_called()
                finally:
                    window.close()
                    wait_until(lambda: not window._background_busy()
                               and not window._close_timer.isActive())
                    window.deleteLater()
                    QTest.qWait(20)


if __name__ == '__main__':
    unittest.main()
