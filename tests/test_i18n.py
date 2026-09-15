"""多语言：tr 回退与切换、目录完整性、语言规范化与启动语言应用时序。"""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from core import i18n
from core.i18n_catalog import EN


class I18nTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language('简体中文')

    def test_translation_switch_and_fallback(self):
        i18n.set_language('简体中文')
        self.assertEqual(i18n.tr('工作台'), '工作台')
        i18n.set_language('English')
        self.assertEqual(i18n.tr('工作台'), 'Workspace')
        self.assertEqual(i18n.tr('未收录的字符串'), '未收录的字符串')

    def test_language_normalization(self):
        self.assertEqual(i18n.set_language('English'), 'en-US')
        self.assertEqual(i18n.set_language('en'), 'en-US')
        self.assertEqual(i18n.set_language('简体中文'), 'zh-CN')
        self.assertEqual(i18n.set_language(''), 'zh-CN')
        self.assertEqual(i18n.current_language(), 'zh-CN')

    def test_catalog_entries_are_nonempty_strings(self):
        self.assertGreater(len(EN), 20)
        for source, target in EN.items():
            self.assertIsInstance(source, str)
            self.assertTrue(source.strip())
            self.assertIsInstance(target, str)
            self.assertTrue(target.strip())

    def test_workspace_surface_batch_entries(self):
        i18n.set_language('English')
        expected = {
            '开始生成': 'Start Generating',
            '暂停': 'Pause',
            '继续': 'Resume',
            '自动保存': 'Auto-save',
            '产品': 'Products',
            '任务': 'Tasks',
            '未检测': 'Not checked',
            '未配置': 'Not configured',
            '检测中': 'Checking...',
            '已连接': 'Connected',
            '离线缓存': 'Offline cache',
            '未选择': 'Not selected',
            '选择目录': 'Choose folder',
            '提示词': 'Prompts',
            '参考图': 'Images',
            '保存至': 'Save to',
            '匹配详情': 'Match details',
            '已匹配': 'matched',
            '未匹配': 'unmatched',
            '数据源': 'Data source',
            '尚未选择目录': 'No folder selected',
            '选择': 'Choose',
            '关闭': 'Close',
            '生成参数与任务详情': 'Parameters & Task Details',
        }
        for source, target in expected.items():
            self.assertEqual(i18n.tr(source), target, source)

    def test_history_and_task_table_batch_entries(self):
        i18n.set_language('English')
        expected = {
            '任务队列': 'Task queue',
            '生成参数': 'Parameters',
            '个任务': 'tasks',
            '重置模型识别': 'Reset model detection',
            '取消全部': 'Cancel all',
            '暂无任务': 'No tasks',
            '更多操作': 'More actions',
            '重试': 'Retry',
            '跳过': 'Skip',
            '查看日志': 'View logs',
            '打开文件夹': 'Open folder',
            '任务详情 / 确认提交': 'Task details / Confirm',
            '强制使用模型': 'Force model',
            '恢复自动识别': 'Restore auto detection',
            '未分组': 'Ungrouped',
            '全部': 'All',
            '全部状态': 'All statuses',
            '序号': 'No.',
            '任务名': 'Task name',
            '模型': 'Model',
            '状态': 'Status',
            '提交时间': 'Submitted',
            '完成时间': 'Finished',
            '文件大小': 'Size',
            '操作': 'Actions',
            '打开输出文件夹': 'Open output folder',
            '打开文件': 'Open file',
            '等待中': 'Waiting',
            '生成中': 'Generating',
            '已完成': 'Completed',
            '失败': 'Failed',
            '提交待确认': 'Unconfirmed',
            '比例': 'Ratio',
            '分辨率': 'Resolution',
            '时长': 'Duration',
            '进度': 'Progress',
            '用时/剩余': 'Elapsed / Left',
        }
        for source, target in expected.items():
            self.assertEqual(i18n.tr(source), target, source)


class StartupLanguageTests(unittest.TestCase):
    """启动时语言必须先于界面构建生效：否则英文模式重启后部分界面仍显示中文。"""

    def tearDown(self):
        i18n.set_language('简体中文')

    def test_configured_language_applies_before_ui_construction(self):
        import tempfile
        from pathlib import Path
        from PyQt5.QtTest import QTest
        from PyQt5.QtWidgets import QApplication
        from core.config_manager import ConfigManager

        app = QApplication.instance() or QApplication([])
        temp = tempfile.TemporaryDirectory()
        try:
            manager = ConfigManager(Path(temp.name) / 'config.json')
            manager.load_config()
            manager.update(('appearance', 'language'), 'English')
            manager.save_config()

            from ui.main_window import MainWindow
            window = MainWindow(manager, network_time=False)
            try:
                # 不进入事件循环：构造期 tr() 就必须解析为配置语言。
                self.assertEqual([item.text() for item in window._nav_items], ['Workspace', 'History', 'Settings'])
                self.assertTrue(window.settings_page.license_status_label.text().startswith('License status'))
            finally:
                window.close()
                window.deleteLater()
                QTest.qWait(120)
        finally:
            temp.cleanup()


if __name__ == '__main__':
    unittest.main()
