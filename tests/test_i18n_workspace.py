"""工作台页面与控件的中英切换：译文表完整性 + offscreen retranslate 行为。

覆盖用户需求「设置→语言切到 English 后整个界面立刻跟随」中的工作台部分：
- EN_WORKSPACE 纯函数约束：非空、占位符一致、键与源码 tr() 一字不差、不覆盖核心译文；
- offscreen：临时 ConfigManager + MainWindow，切换 English 后调用
  workspace_page.retranslate_ui()，断言关键控件为英文，切回中文可恢复，
  连续切换不重建任务表行对象（选中/滚动状态不丢）。
"""
import ast
import os
import re
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from core import i18n
from core.i18n_catalog import EN
from core.i18n_catalog_workspace import EN_WORKSPACE

ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_FILES = [
    ROOT / 'ui' / 'pages' / 'workspace_page.py',
    ROOT / 'ui' / 'widgets' / 'current_task_card.py',
    ROOT / 'ui' / 'widgets' / 'data_source_card.py',
    ROOT / 'ui' / 'widgets' / 'log_drawer.py',
    ROOT / 'ui' / 'widgets' / 'params_card.py',
    ROOT / 'ui' / 'widgets' / 'recent_completed_panel.py',
    ROOT / 'ui' / 'widgets' / 'task_monitor_bar.py',
    ROOT / 'ui' / 'widgets' / 'task_queue_panel.py',
    ROOT / 'ui' / 'widgets' / 'workspace_directory_bar.py',
    ROOT / 'ui' / 'widgets' / 'workspace_log.py',
    ROOT / 'ui' / 'widgets' / 'workspace_summary.py',
    ROOT / 'ui' / 'widgets' / 'workspace_task_table.py',
]
CJK = re.compile(r'[\u4e00-\u9fff]')
PLACEHOLDER = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)')


def workspace_tr_keys():
    """AST 扫描工作台文件里的 tr() 字面量参数（= 需要译文的界面文案集合）。"""
    keys = set()
    for path in WORKSPACE_FILES:
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, 'id', None) or getattr(node.func, 'attr', None)
            if name != 'tr' or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                keys.add(first.value)
    return keys


def ensure_workspace_catalog_merged():
    """i18n-core 在 core/i18n.py 合并 EN_WORKSPACE 之前，测试进程内先注入。"""
    target = getattr(i18n, 'EN', None)
    if not isinstance(target, dict):
        raise AssertionError('core.i18n.EN 不是字典，无法合并工作台译文')
    for source, target_text in EN_WORKSPACE.items():
        target.setdefault(source, target_text)


class WorkspaceCatalogTests(unittest.TestCase):
    def test_entries_are_nonempty_and_translated(self):
        self.assertGreaterEqual(len(EN_WORKSPACE), 150)
        for source, target in EN_WORKSPACE.items():
            self.assertIsInstance(source, str)
            self.assertTrue(source.strip(), 'empty source key')
            self.assertIsInstance(target, str)
            self.assertTrue(target.strip(), f'empty translation for {source!r}')
            self.assertIsNone(CJK.search(target), f'translation still contains Chinese: {source!r} -> {target!r}')

    def test_placeholders_are_preserved(self):
        for source, target in EN_WORKSPACE.items():
            source_names = set(PLACEHOLDER.findall(source))
            target_names = set(PLACEHOLDER.findall(target))
            self.assertEqual(source_names, target_names, f'placeholder mismatch for {source!r} -> {target!r}')

    def test_catalog_keys_match_source_tr_literals(self):
        used = workspace_tr_keys()
        # 每个译文键必须真的来自工作台源码（防止拼写漂移与僵尸词条）。
        self.assertEqual(set(EN_WORKSPACE) - used, set())
        # 工作台所有 tr() 字面量都必须被 EN 或 EN_WORKSPACE 覆盖（不允许漏译）。
        missing = used - set(EN) - set(EN_WORKSPACE)
        self.assertEqual(missing, set())

    def test_catalog_does_not_conflict_with_core_entries(self):
        for source in set(EN_WORKSPACE) & set(EN):
            self.assertEqual(EN[source], EN_WORKSPACE[source], f'conflicting translation for {source!r}')

    def test_runtime_translation_of_workspace_copy(self):
        ensure_workspace_catalog_merged()
        original = i18n.current_language()
        try:
            i18n.set_language('English')
            self.assertEqual(i18n.tr('工作台'), 'Workspace')
            self.assertEqual(i18n.tr('当前没有运行中的队列'), 'No queue is running')
            self.assertEqual(i18n.tr('有 {count} 条提交待确认，队列已阻止重复创建，请逐条核对后继续。').format(count=2),
                             '2 submission(s) await confirmation; the queue has blocked duplicate creation. '
                             'Review them one by one before continuing.')
        finally:
            i18n.set_language(original)


class WorkspaceRetranslateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        ensure_workspace_catalog_merged()

    def setUp(self):
        from PyQt5.QtTest import QTest
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        i18n.set_language('简体中文')
        self.addCleanup(i18n.set_language, '简体中文')
        self.temp = tempfile.TemporaryDirectory()
        self.manager = ConfigManager(Path(self.temp.name) / 'config.json')
        self.manager.load_config()
        self.window = MainWindow(self.manager, network_time=False)
        self.page = self.window.workspace_page
        self.addCleanup(self._teardown_window)

    def _teardown_window(self):
        from PyQt5.QtTest import QTest
        self.window.close()
        self.window.deleteLater()
        QTest.qWait(30)
        self.temp.cleanup()

    def _switch(self, language):
        i18n.set_language(language)
        self.page.retranslate_ui()

    # -- 英文：关键控件全部跟随 -----------------------------------------
    def test_switch_to_english_translates_page_and_children(self):
        self._switch('English')
        page = self.page
        expected = {
            '页面标题': (page.title_label.text(), 'Workspace'),
            '开始按钮': (page.start_button.text(), 'Start Generating'),
            '暂停按钮': (page.pause_button.text(), 'Pause'),
            '自动保存': (page.auto_save_label.text(), 'Auto-save'),
            '导航工作台': (page.navigation_tabs['workspace'].text(), 'Workspace'),
            '导航设置': (page.navigation_tabs['settings'].text(), 'Settings'),
            '关闭按钮': (page.close_button.text(), 'Close'),
            '数据源标题': (page.data_source.heading.text(), 'Data source'),
            '数据源状态': (page.data_source.status_label.text(), 'Select prompt and image folders'),
            '参数卡标题': (page.params_card.heading.text(), 'Parameters'),
            '高级参数': (page.params_card.expand_button.text(), 'Advanced parameters'),
            '参数首字段': (page.params_card._field_captions[0][0].text(), 'Model'),
            '队列标题': (page.queue_panel.heading.text(), 'Task queue'),
            '队列表头-提示词': (page.queue_panel._header_cells[1].text(), 'Prompts'),
            '队列表头-状态': (page.queue_panel._header_cells[10].text(), 'Status'),
            '队列空态': (page.queue_panel.empty_label.text(), 'No tasks'),
            '目录-提示词': (page.directory_bar.fields['prompts'].title.text(), 'Prompts'),
            '目录-保存至': (page.directory_bar.fields['output'].title.text(), 'Save to'),
            '匹配详情按钮': (page.directory_bar.match_button.text(), 'Match details'),
            '监看带标题': (page.task_monitor.live_label.text(), 'Current task'),
            '监看带跳过': (page.task_monitor.skip_button.text(), 'Skip'),
            '当前任务卡标题': (page.current_task.heading.text(), 'Current task'),
            '当前任务空态': (page.current_task.title.text(), 'No task started yet'),
            '最近完成标题': (page.recent_panel.heading.text(), 'Recently completed'),
            '最近完成空态': (page.recent_panel.empty_label.text(), 'No completed videos yet'),
            '日志标题': (page.log_drawer.heading.text(), 'Execution log'),
            '日志筛选': (page.log_drawer.filter_box._buttons['全部'].text(), 'All'),
            '日志筛选项': (page.log_drawer.filter_box.currentText(), '全部'),
            '详情弹窗标题': (page.controls_dialog.header.title_label.text(), 'Parameters & Task Details'),
            '接口状态': (page.api_status.text(), 'Not checked'),
            '进度占位': (page.product_progress_label.text(), 'Now: — · total progress: products 0/0'),
        }
        for name, (actual, wanted) in expected.items():
            self.assertEqual(actual, wanted, f'{name} 未跟随英文切换: {actual!r} != {wanted!r}')
        # 下拉选项整体切到英文（状态项与筛选全部无中文）。
        filter_items = [page.queue_panel.filter_box.itemText(i) for i in range(page.queue_panel.filter_box.count())]
        self.assertEqual(filter_items[0], 'All')
        self.assertFalse([text for text in filter_items if CJK.search(text)], filter_items)

    def test_switch_back_to_chinese_restores_default_copy(self):
        self._switch('English')
        self._switch('简体中文')
        page = self.page
        self.assertEqual(page.title_label.text(), '工作台')
        self.assertEqual(page.start_button.text(), '开始生成')
        self.assertEqual(page.auto_save_label.text(), '自动保存')
        self.assertEqual(page.queue_panel.heading.text(), '任务队列')
        self.assertEqual(page.queue_panel.filter_box.currentText(), '全部')
        self.assertEqual(page.queue_panel._header_cells[1].text(), '提示词')
        self.assertEqual(page.queue_panel.empty_label.text(), '暂无任务')
        self.assertEqual(page.log_drawer.heading.text(), '执行日志')
        self.assertEqual(page.data_source.status_label.text(), '请选择提示词与图片目录')
        self.assertEqual(page.params_card.heading.text(), '生成参数')
        self.assertEqual(page.current_task.title.text(), '尚未开始任务')
        self.assertEqual(page.task_monitor.live_label.text(), '当前任务')
        self.assertEqual(page.product_progress_label.text(), '当前：—，总进度：产品 0/0')

    # -- 连续切换：不重建表格/行对象，选中保持 --------------------------
    def test_three_consecutive_switches_keep_rows_and_selection(self):
        tasks = [
            dict(local_id='id1', prompt_path='C:/x/1.txt', prompt_name='01demo',
                 product='汽车', model='video-v3', status='waiting', images=[],
                 progress=0, requested_model='video-v3'),
            dict(local_id='id2', prompt_path='C:/x/2.txt', prompt_name='02demo',
                 product='汽车', model='video-v3', status='processing', images=[],
                 progress=42, requested_model='video-v3'),
        ]
        self.page.queue_panel.update_tasks(tasks)
        panel = self.page.queue_panel
        first_row = panel.rows[0]
        second_row = panel.rows[1]
        first_item = panel._task_items[0]
        row_count = len(panel.rows)
        panel.select_task(1)
        selected_index = panel.list.currentRow()
        for _ in range(3):
            self._switch('English')
            self.assertEqual(self.page.title_label.text(), 'Workspace')
            self.assertEqual(panel.rows[0], first_row)
            self.assertEqual(panel.rows[1], second_row)
            self.assertEqual(panel._task_items[0], first_item)
            self.assertEqual(len(panel.rows), row_count)
            self.assertEqual(panel.list.currentRow(), selected_index)
            self.assertNotIn('未匹配', second_row.status_label.text())
            self._switch('简体中文')
            self.assertEqual(self.page.title_label.text(), '工作台')
            self.assertEqual(panel.rows[0], first_row)
            self.assertEqual(len(panel.rows), row_count)
            self.assertEqual(panel.list.currentRow(), selected_index)

    # -- 动态文案（行/当前任务/监看带）跟随语言 ------------------------
    def test_dynamic_task_text_follows_language_without_new_rows(self):
        task = dict(local_id='id1', prompt_path='C:/x/1.txt', prompt_name='01demo',
                    product='汽车', model='video-v3', status='processing', images=[],
                    progress=35, requested_model='video-v3', poll_elapsed_seconds=75)
        self.page.queue_panel.update_tasks([task])
        row = self.page.queue_panel.rows[0]
        self.page.current_task.update_task(0, task)
        self.page.task_monitor.update_task(0, task)
        self.page.current_task.update_progress(35, 35, 60)
        self.page.task_monitor.update_progress(35, 35, 60)

        self._switch('English')
        self.assertEqual(row.status_label.text(), 'Generating')
        self.assertIn('Generating', self.page.current_task.title.text())
        self.assertIn('References: 0', self.page.current_task.details.text())
        self.assertEqual(self.page.current_task.concurrency_label.text(), 'Generating 0, queued 0')
        self.assertEqual(self.page.task_monitor.state_label.text(), 'Generating')
        self.assertIn('Elapsed 35 s', self.page.task_monitor.timing.text())
        self.assertIs(self.page.queue_panel.rows[0], row)

        self._switch('简体中文')
        self.assertEqual(row.status_label.text(), '生成中')
        self.assertIn('参考图 0 张', self.page.current_task.details.text())
        self.assertIs(self.page.queue_panel.rows[0], row)

    def test_recovery_bar_and_recovery_copy_translate(self):
        self.page.task_manager.tasks = [
            dict(local_id='u1', prompt_name='p', status='submission_unknown', prompt_path='C:/x/1.txt')]
        self._switch('English')
        self.page._update_recovery_bar()
        self.assertIn('submission(s) await confirmation', self.page.recovery_label.text())
        self.assertEqual(self.page.recovery_button.text(), 'Review one by one')
        self._switch('简体中文')
        self.page._update_recovery_bar()
        self.assertIn('条提交待确认', self.page.recovery_label.text())
        self.assertEqual(self.page.recovery_button.text(), '逐条核对')

    def test_startup_in_english_then_switch_to_chinese(self):
        """英文配置启动（构造期 tr 已是英文）后再切回中文也必须全量恢复。"""
        from PyQt5.QtTest import QTest
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        manager = ConfigManager(Path(temp.name) / 'config.json')
        manager.load_config()
        manager.update(('appearance', 'language'), 'English')
        manager.save_config()
        window = MainWindow(manager, network_time=False)
        self.addCleanup(lambda: (window.close(), window.deleteLater(), QTest.qWait(20)))
        page = window.workspace_page
        # 构造期即英文（不调用 retranslate）。
        self.assertEqual(page.title_label.text(), 'Workspace')
        self.assertEqual(page.directory_bar.fields['prompts'].title.text(), 'Prompts')
        self.assertEqual(page.queue_panel.filter_box.currentText(), 'All')
        # 切回中文并 retranslate：不能用“构造期英文”回放导致仍显示英文。
        i18n.set_language('简体中文')
        page.retranslate_ui()
        self.assertEqual(page.title_label.text(), '工作台')
        self.assertEqual(page.directory_bar.fields['prompts'].title.text(), '提示词')
        self.assertEqual(page.directory_bar.fields['images'].title.text(), '参考图')
        self.assertEqual(page.directory_bar.fields['output'].title.text(), '保存至')
        self.assertEqual(page.queue_panel.filter_box.currentText(), '全部')
        self.assertEqual(page.params_card._field_captions[0][0].text(), '模型')
        self.assertEqual(page.log_drawer.heading.text(), '执行日志')
        self.assertEqual(page.current_task.title.text(), '尚未开始任务')

    def test_retranslate_is_idempotent_and_keeps_object_references(self):
        panel = self.page.queue_panel
        task = dict(local_id='id1', prompt_path='C:/x/1.txt', prompt_name='01demo',
                    product='', model='video-v3', status='waiting', images=[],
                    progress=0, requested_model='video-v3')
        panel.update_tasks([task])
        first = panel.rows[0]
        for _ in range(2):
            self._switch('English')
        self.assertIs(panel.rows[0], first)
        self.assertEqual(panel.list.count(), 1)
        self.assertTrue(panel.empty_panel.isHidden())
        self.assertEqual(self.page.queue_panel.filter_box.currentText(), 'All')


if __name__ == '__main__':
    unittest.main()
