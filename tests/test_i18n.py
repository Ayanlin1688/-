"""多语言骨架：tr 回退与切换、目录完整性与语言规范化。"""
import unittest

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


if __name__ == '__main__':
    unittest.main()
