"""语言切换全量应用：导航 / 设置页 / 历史页 / 对话框随设置即时切换。

覆盖用户需求第 8 条：切到 English 后整个界面立即跟随，切回中文恢复，
重复切换稳定且 retranslate_ui 幂等。
"""
import gc
import os
import re
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication, QEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QComboBox, QLineEdit, QWidget

from core import i18n
from core.config_manager import ConfigManager
from tests.qt_scale_support import reset_interface_scaling

CJK = re.compile(r'[\u4e00-\u9fff]')

# 用户数据 / 配置模板块：不是界面文案，切换语言时应保持原样。
ALLOWED_RESIDUAL = (
    '{序号}_{提示词名}.mp4',
)


def has_cjk(text):
    return isinstance(text, str) and bool(CJK.search(text))


def visible_chinese(root, allowed=ALLOWED_RESIDUAL):
    """收集控件树中残留的中文界面文案（不含用户数据输入框内容）。"""
    hits = []
    for widget in [root] + root.findChildren(QWidget):
        if isinstance(widget, QLineEdit):
            continue
        for attribute in ('text', 'title', 'windowTitle'):
            getter = getattr(widget, attribute, None)
            if not callable(getter):
                continue
            try:
                value = getter()
            except Exception:
                continue
            if has_cjk(value) and value not in allowed:
                hits.append((type(widget).__name__, attribute, value))
        if hasattr(widget, 'itemText') and hasattr(widget, 'count'):
            try:
                for index in range(widget.count()):
                    value = widget.itemText(index)
                    if has_cjk(value) and value not in allowed:
                        hits.append((type(widget).__name__, 'item', value))
            except Exception:
                pass
    return hits


class LanguageSwitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        reset_interface_scaling(self.app)
        self._collect_deleted_widgets()
        i18n.set_language('简体中文')
        self.temp = tempfile.TemporaryDirectory()
        self.manager = ConfigManager(Path(self.temp.name) / 'config.json')
        self.manager.load_config()
        from ui.main_window import MainWindow
        self.window = MainWindow(self.manager, network_time=False)
        self.settings = self.window.settings_page
        self.history = self.window.history_page

    def tearDown(self):
        i18n.set_language('简体中文')
        try:
            self.window.close()
            self.window.deleteLater()
        finally:
            self._collect_deleted_widgets()
            reset_interface_scaling(self.app)
            self._collect_deleted_widgets()
            self.temp.cleanup()

    def _collect_deleted_widgets(self):
        """Clear previous tests' Qt deletions and Python cycles before counting."""
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        gc.collect()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def _registered_window_ids(self):
        # Keep identities only: retaining the widgets would mask weak-reference
        # removals and invalidate the language-bus lifetime assertion.
        return {id(widget) for widget in i18n._REGISTERED
                if isinstance(widget, QWidget) and not sip.isdeleted(widget)
                and (widget is self.window or self.window.isAncestorOf(widget))}

    # ------------------------------------------------------------------
    def _language_combo(self):
        return self.settings.language

    def _switch(self, value):
        combo = self._language_combo()
        index = next((i for i in range(combo.count()) if combo.itemData(i) == value), -1)
        self.assertGreaterEqual(index, 0, f'language option {value!r} missing')
        combo.setCurrentIndex(index)
        QTest.qWait(80)
        self.assertEqual(i18n.current_language(), 'en-US' if value == 'English' else 'zh-CN')

    def _current_page_titles(self):
        from qfluentwidgets import TitleLabel
        key = self.settings._current_page_key()
        content = self.settings._pages[key][1]
        return [label.text() for label in content.findChildren(TitleLabel)]

    # ------------------------------------------------------------------
    def test_switch_to_english_applies_everywhere(self):
        self._switch('English')
        # 导航 / 关于入口
        self.assertEqual([item.text() for item in self.window._nav_items],
                         ['Workspace', 'Image generation', 'Video creation', 'Ecommerce Image Maker', 'History', 'Settings'])
        self.assertEqual(self.window._about_item.text(), 'About')
        # 设置页：页面标题、卡片、按钮、状态文案
        self.assertIn('Relay stations', self._current_page_titles())
        self.assertEqual(self.settings.sync_models_button.text(), 'Sync upstream models')
        self.assertTrue(self.settings.license_status_label.text().startswith('License status'))
        self.assertFalse(has_cjk(self.settings.license_status_label.text()))
        self.assertEqual(self.settings.pool_tabs.items['video'].text(), 'Video models')
        self.assertEqual(self.settings.last_model_sync.text(), 'Last sync: Not synced yet')
        self.assertEqual([self.settings.language.itemText(i) for i in range(self.settings.language.count())],
                         ['Simplified Chinese', 'English'])
        # 历史页：标题 / 筛选 / 表头
        self.assertEqual(self.history.filter_box.itemText(0), 'All statuses')
        self.assertEqual(self.history.type_filter_box.itemText(1), 'Video')
        self.assertEqual(self.history.table.horizontalHeaderItem(1).text(), 'No.')
        self.assertEqual(self.history.table.horizontalHeaderItem(6).text(), 'Status')
        self.assertFalse(has_cjk(self.history.footer_right.text()))
        # 设置页与历史页（非用户数据部分）不得残留中文
        self.assertEqual(visible_chinese(self.settings), [])
        self.assertEqual(visible_chinese(self.history), [])

    def test_switch_back_restores_chinese(self):
        self._switch('English')
        self._switch('简体中文')
        self.assertEqual([item.text() for item in self.window._nav_items],
                         ['工作台', '图片生成', '视频制作', '电商图制作', '任务历史', '设置'])
        self.assertIn('中转站', self._current_page_titles())
        self.assertEqual(self.settings.sync_models_button.text(), '同步上游模型')
        self.assertTrue(self.settings.license_status_label.text().startswith('许可状态'))
        self.assertEqual(self.history.filter_box.itemText(0), '全部状态')
        self.assertEqual(self.history.table.horizontalHeaderItem(1).text(), '序号')

    def test_video_page_links_navigate_to_workspace_and_settings(self):
        page = self.window.video_creation_page
        page.navigate_workspace.emit()
        self.assertIs(self.window.stackedWidget.currentWidget(), self.window.workspace_page)
        page.open_settings_requested.emit()
        self.assertIs(self.window.stackedWidget.currentWidget(), self.window.settings_page)

    def test_repeated_switch_is_stable_and_retranslate_idempotent(self):
        self._collect_deleted_widgets()
        baseline_nav = [item.text() for item in self.window._nav_items]
        registry = i18n.registered_retranslate_count()
        window_registry = self._registered_window_ids()
        self.assertTrue(window_registry)
        for _ in range(3):
            self._switch('English')
            self._switch('简体中文')
        self.assertEqual([item.text() for item in self.window._nav_items], baseline_nav)
        # 语言总线登记不随切换次数堆叠（同控件弱引用集合去重）。
        self.assertEqual(i18n.registered_retranslate_count(), registry)
        self.assertEqual(self._registered_window_ids(), window_registry)

        # retranslate_ui 反复调用必须幂等。
        self._switch('English')
        first = ([item.text() for item in self.window._nav_items],
                 self.settings.sync_models_button.text(),
                 self.settings.last_model_sync.text(),
                 self.history.table.horizontalHeaderItem(6).text(),
                 self._current_page_titles())
        self.window.retranslate_ui()
        self.settings.retranslate_ui()
        self.history.retranslate_ui()
        self.window.retranslate_ui()
        second = ([item.text() for item in self.window._nav_items],
                  self.settings.sync_models_button.text(),
                  self.settings.last_model_sync.text(),
                  self.history.table.horizontalHeaderItem(6).text(),
                  self._current_page_titles())
        self.assertEqual(first, second)

    def test_open_dialog_follows_language_switch(self):
        from ui.components.history_dialog import DeleteHistoryDialog
        dialog = DeleteHistoryDialog([dict(status='completed')],
                                     {'file_count': 1, 'size_bytes': 1048576})
        try:
            self.assertEqual(dialog.windowTitle(), '批量删除历史')
            self._switch('English')
            self.assertEqual(dialog.windowTitle(), 'Bulk delete history')
            self.assertTrue(dialog.yesButton.text() == 'Delete')
            self.assertFalse(has_cjk(dialog.hint_label.text()))
            self._switch('简体中文')
            self.assertEqual(dialog.windowTitle(), '批量删除历史')
        finally:
            dialog.close()
            dialog.deleteLater()
            QTest.qWait(40)


if __name__ == '__main__':
    unittest.main()
