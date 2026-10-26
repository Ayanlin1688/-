"""Offline keyboard, popup lifetime and selection contracts for both forms."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtWidgets import QApplication, QVBoxLayout, QPushButton, QWidget

from core.i18n import set_language
from ui.components.platform_multi_select import PlatformMultiSelect
from ui.theme import apply_ui_mode
from ui import tokens


class PlatformMultiSelectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        set_language('zh-CN')
        tokens.set_interface_scale(1)
        self.host = QWidget()
        layout = QVBoxLayout(self.host)
        self.picker = PlatformMultiSelect()
        self.outside = QPushButton('outside')
        layout.addWidget(self.picker)
        layout.addWidget(self.outside)
        self.host.resize(600, 250)
        self.host.show()
        self.app.processEvents()

    def tearDown(self):
        self.picker.popup.hide()
        self.host.close()
        self.host.deleteLater()
        self.app.processEvents()
        tokens.set_interface_scale(1)
        set_language('zh-CN')
        apply_ui_mode('light')

    def test_selection_order_chips_limit_and_clear(self):
        changed, limit = QSignalSpy(self.picker.changed), QSignalSpy(self.picker.limit_reached)
        ids = list(self.picker.records)[:6]
        self.picker.set_platforms(ids[:5])
        self.app.processEvents()
        self.picker.chips[ids[5]].click()
        self.assertEqual(self.picker.selected_platforms(), ids[:5])
        self.assertEqual(len(limit), 1)
        self.assertFalse(self.picker.chips[ids[5]].property('checked'))
        for pid, label in zip(ids, self.picker.selected_chips):
            self.assertFalse(label.isHidden())
            self.assertEqual(label.toolTip(), self.picker.records[pid]['name'])
            self.assertGreater(label.width(), 0)
            self.assertTrue(label.text())
        for left, right in zip(self.picker.selected_chips, self.picker.selected_chips[1:]):
            self.assertLess(left.geometry().right(), right.geometry().left())
        self.assertEqual(self.picker.count_label.text(), '已选 5/5 个')
        self.picker.clear_button.click()
        self.assertEqual(self.picker.selected_platforms(), [])
        self.assertEqual(list(changed[-1]), [[]])
        self.assertTrue(all(label.isHidden() for label in self.picker.selected_chips))
        self.picker.setMaximumCount(2)
        self.picker.set_platforms(ids)
        self.assertEqual(self.picker.selected_platforms(), ids[:2])

    def test_popup_reused_keyboard_escape_and_outside_click(self):
        popup = self.picker.popup
        chips = list(self.picker.chips.values())
        self.picker.button.setFocus()
        QTest.keyClick(self.picker.button, Qt.Key_Space)
        self.assertTrue(popup.isVisible())
        chip = chips[0]
        chip.setFocus()
        QTest.keyClick(chip, Qt.Key_Space)
        self.assertTrue(chip.isChecked())
        QTest.keyClick(chip, Qt.Key_Tab)
        self.assertIsNot(self.app.focusWidget(), chip)
        QTest.keyClick(popup, Qt.Key_Escape)
        self.assertFalse(popup.isVisible())
        self.picker.toggle_popup()
        self.assertIs(popup, self.picker.popup)
        self.assertEqual(chips, list(self.picker.chips.values()))
        QTest.mouseClick(self.outside, Qt.LeftButton)
        self.app.processEvents()
        self.assertFalse(popup.isVisible())

    def test_themes_languages_and_four_scales_keep_selection(self):
        self.picker.set_platforms(['taobao', 'amazon'])
        for scale in (1, 1.25, 1.5, 1.75):
            tokens.set_interface_scale(scale)
            self.picker.refresh_interface_scale()
            self.assertEqual(self.picker.button.height(), tokens.px(40))
            for mode in ('light', 'dark'):
                apply_ui_mode(mode)
                self.assertEqual(self.picker.selected_platforms(), ['taobao', 'amazon'])
                self.assertTrue(self.picker.chips['taobao'].property('checked'))
        set_language('en-US')
        self.assertEqual(self.picker.placeholder.text(), 'Select platforms')
        self.assertEqual(self.picker.count_label.text(), 'Selected 2/5')


if __name__ == '__main__':
    unittest.main()
