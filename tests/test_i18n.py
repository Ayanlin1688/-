"""多语言：tr 回退与切换、目录完整性、语言规范化与启动语言应用时序。"""
import gc
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from core import i18n
from core.i18n_catalog import EN as EN_BASE


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
        self.assertGreater(len(EN_BASE), 20)
        for source, target in EN_BASE.items():
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

    def test_logs_task_card_and_dialogs_batch_entries(self):
        i18n.set_language('English')
        expected = {
            '执行日志': 'Execution log',
            '本次运行': 'This session',
            '条': 'entries',
            '导出': 'Export',
            '清空': 'Clear',
            '高级参数': 'Advanced parameters',
            '时长（秒）': 'Duration (s)',
            '留空则随机': 'Leave empty for random',
            '生成音频': 'Generate audio',
            '轮询间隔（秒）': 'Poll interval (s)',
            '最大重试次数': 'Max retries',
            '随机种子': 'Random seed',
            '轮询': 'Round robin',
            '随机': 'Random',
            '优先级': 'Priority',
            '秒': 's',
            '当前任务': 'Current task',
            '尚未开始任务': 'No task started yet',
            '已用': 'Elapsed',
            '预计剩余': 'ETA',
            '跳过当前': 'Skip current',
            '重新下载': 'Redownload',
            '处理待确认': 'Resolve unconfirmed',
            '最近完成': 'Recently completed',
            '参考图查看器': 'Reference viewer',
            '适应窗口': 'Fit window',
            '确认提交结果': 'Confirm submission result',
            '保持待确认': 'Keep unconfirmed',
            '保存确认结果': 'Save confirmation',
            '暂不处理': 'Not now',
            '计费未提供': 'Pricing unavailable',
            '非视频模型': 'Not a video model',
            '协议待确认': 'Protocol unverified',
            '已下架': 'Delisted',
            '暂无可提交模型': 'No submittable model',
            '关闭（Esc）': 'Close (Esc)',
        }
        for source, target in expected.items():
            self.assertEqual(i18n.tr(source), target, source)


class LanguageBusTests(unittest.TestCase):
    """语言总线：变化才发射、弱引用登记、合并词表容错。"""

    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        i18n.set_language('简体中文')
        self.received = []
        self._slot = self.received.append
        i18n.language_bus().language_changed.connect(self._slot)

    def tearDown(self):
        try:
            i18n.language_bus().language_changed.disconnect(self._slot)
        except (TypeError, RuntimeError):
            pass
        i18n.set_language('简体中文')

    def test_emits_once_per_actual_change(self):
        self.assertEqual(i18n.set_language('简体中文'), 'zh-CN')
        self.assertEqual(self.received, [])
        self.assertEqual(i18n.set_language('English'), 'en-US')
        self.assertEqual(self.received, ['en-US'])
        # 同语言重复设置不得重复发射。
        i18n.set_language('English')
        i18n.set_language('en')
        self.assertEqual(self.received, ['en-US'])
        i18n.set_language('简体中文')
        self.assertEqual(self.received, ['en-US', 'zh-CN'])

    def test_register_retranslate_calls_widget_and_ignores_missing_method(self):
        from PyQt5.QtWidgets import QWidget

        calls = []

        class _Translatable(QWidget):
            def retranslate_ui(self):
                calls.append(i18n.current_language())

        widget = _Translatable()
        self.assertTrue(i18n.register_retranslate(widget))
        plain = QWidget()
        self.assertFalse(i18n.register_retranslate(plain))
        i18n.set_language('English')
        self.assertEqual(calls, ['en-US'])
        widget.deleteLater()
        plain.deleteLater()

    def test_register_retranslate_survives_destroyed_widget(self):
        from PyQt5.QtWidgets import QWidget

        calls = []

        class _Translatable(QWidget):
            def retranslate_ui(self):
                calls.append(1)

        widget = _Translatable()
        self.assertTrue(i18n.register_retranslate(widget))
        i18n.set_language('English')
        before = len(calls)
        del widget
        gc.collect()
        # 控件销毁后切语言不得抛异常，也不得再回调。
        i18n.set_language('简体中文')
        i18n.set_language('English')
        self.assertEqual(len(calls), before)

    def test_retranslate_error_in_one_widget_does_not_block_others(self):
        from PyQt5.QtWidgets import QWidget

        calls = []

        class _Broken(QWidget):
            def retranslate_ui(self):
                raise RuntimeError('boom')

        class _Translatable(QWidget):
            def retranslate_ui(self):
                calls.append(1)

        broken, widget = _Broken(), _Translatable()
        i18n.register_retranslate(broken)
        i18n.register_retranslate(widget)
        i18n.set_language('English')
        self.assertEqual(calls, [1])
        broken.deleteLater()
        widget.deleteLater()

    def test_merged_catalog_is_a_copy_with_extensions_tolerant(self):
        # 可选扩展模块尚未接入时也必须能 import；合并结果是副本，可被扩展覆盖。
        self.assertIsInstance(i18n.EN, dict)
        self.assertIsNot(i18n.EN, EN_BASE)
        self.assertEqual(i18n.EN['工作台'], 'Workspace')
        self.assertGreaterEqual(len(i18n.EN), len(EN_BASE))

    def test_translate_text_templates_roundtrip(self):
        i18n.set_language('English')
        self.assertEqual(i18n.translate_text('共 3 条记录 · 存储占用 1.20 GB'),
                         'Total 3 records · Storage 1.20 GB')
        self.assertEqual(i18n.translate_text('冷却中 5秒'), 'Cooling down 5s')
        self.assertEqual(i18n.tr('中转站'), 'Relay stations')
        i18n.set_language('简体中文')
        self.assertEqual(i18n.translate_text('Total 3 records · Storage 1.20 GB'),
                         '共 3 条记录 · 存储占用 1.20 GB')
        self.assertEqual(i18n.translate_text('Relay stations'), '中转站')
        # 未收录的文本原样返回，不得损坏用户数据。
        self.assertEqual(i18n.translate_text('C:\\Users\\demo\\a.mp4'), 'C:\\Users\\demo\\a.mp4')


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
                self.assertEqual([item.text() for item in window._nav_items],
                                 ['Workspace', 'Image generation', 'Video creation', 'Ecommerce Images', 'History', 'Settings'])
                self.assertTrue(window.settings_page.license_status_label.text().startswith('License status'))
                # 设置页 / 历史页（本次新增的全量重翻）启动即英文。
                self.assertEqual(window.settings_page.sync_models_button.text(), 'Sync upstream models')
                self.assertEqual(window.history_page.filter_box.itemText(0), 'All statuses')
                self.assertEqual(window.history_page.table.horizontalHeaderItem(1).text(), 'No.')
            finally:
                window.close()
                window.deleteLater()
                QTest.qWait(120)
        finally:
            temp.cleanup()


if __name__ == '__main__':
    unittest.main()
