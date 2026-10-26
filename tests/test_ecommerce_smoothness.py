"""Offline task list performance, font fallback and scaling smoke tests."""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PyQt5.QtCore import QPoint, QThreadPool, Qt
from PyQt5.QtWidgets import QApplication

from core.i18n import set_language
from tests.qt_scale_support import flush_layout, reset_interface_scaling
from tests.test_interface_scaling import FakeScreen
from ui import materials
from ui.components.ecommerce_task_list import EcommerceTaskList
from ui.main_window import MainWindow
from ui.scaling import apply_interface_scale
from ui.tokens import px


class EcommerceSmoothnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        set_language('zh-CN')
        self.addCleanup(set_language, 'zh-CN')
        reset_interface_scaling(self.app)
        self.addCleanup(reset_interface_scaling, self.app)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.panel = EcommerceTaskList(lambda status: status)
        self.panel.resize(1400, 900)
        self.addCleanup(self.dispose_panel)

    def dispose_panel(self):
        self.panel.close()
        self.panel.deleteLater()
        QThreadPool.globalInstance().waitForDone(2000)
        self.app.processEvents()

    def groups(self, count=50):
        return [dict(
            group_id=f'smooth-{index}', updated_at=1000 + index,
            status='succeeded', product_name='离线任务',
            totals={'succeeded': 12, 'total': 12},
            platforms=[{'name': '离线平台'}],
            config=dict(product_info='耐热陶瓷，便携易清洁。' * 30,
                        carousel_count=5, detail_count=7),
            manifest={'items': [dict(kind='carousel' if item < 5 else 'detail',
                                     seq=item + 1, status='succeeded',
                                     file=str(self.root / f'{index}-{item}.png'))
                                for item in range(12)]},
            thumbs=[str(self.root / f'{index}-{item}.png') for item in range(3)],
        ) for index in range(count)]

    def test_task_card_two_line_text_and_actions_stay_separate_at_four_scales(self):
        self.panel.update_groups(self.groups(2))
        self.panel.show()
        screen = FakeScreen()
        for scale in ('1.0', '1.25', '1.5', '1.75'):
            with self.subTest(scale=scale):
                apply_interface_scale(self.app, screen, scale)
                self.panel.resize(px(1400), px(900))
                flush_layout(self.app, self.panel)
                cards = list(self.panel.cards.values())
                self.assertEqual(len({card.height() for card in cards}), 1)
                for card in cards:
                    label = card.desc_label
                    self.assertEqual(label.textFormat(), Qt.PlainText)
                    self.assertTrue(label.wordWrap())
                    self.assertLessEqual(label.height(), label.fontMetrics().lineSpacing() * 2 + px(2))
                    self.assertEqual(label.font().family(), materials.ui_font_family())
                    desc_right = label.mapTo(card, QPoint(label.width(), 0)).x()
                    action_left = card.view_button.mapTo(card, QPoint()).x()
                    self.assertLess(desc_right, action_left)
                    before = card.geometry()
                    self.panel.update_groups(self.groups(2))
                    flush_layout(self.app, self.panel)
                    self.assertEqual(card.geometry(), before)

    def test_history_page_delete_queues_task_list_update(self):
        store = Mock()
        window = SimpleNamespace(config_manager=Mock(), history_page=Mock(), ecommerce_page=Mock())
        window.config_manager.ecommerce_store.return_value = store
        MainWindow._delete_ecommerce_history_groups(window, ['one', 'two'])
        self.assertEqual([call.args[0] for call in store.delete_group.call_args_list], ['one', 'two'])
        window.history_page.refresh_records.assert_called_once_with()
        window.ecommerce_page._queue_history_refresh.assert_called_once_with()
        window.ecommerce_page._refresh_history.assert_not_called()

    def test_late_task_cards_and_chips_keep_scale_baselines(self):
        groups = self.groups(3)
        self.panel.update_groups(groups)
        self.panel.show()

        def measurements(card):
            margins = card.layout().contentsMargins()
            return (
                card.desc_label.font().pixelSize(),
                card.desc_label.maximumHeight(),
                card.thumb_labels[0].size(),
                card.view_button.height(),
                (margins.left(), margins.top(), margins.right(), margins.bottom()),
                card.layout().spacing(), card.height(),
            )

        def assert_scaled(card):
            self.assertEqual(card.desc_label.font().pixelSize(), px(12))
            self.assertGreaterEqual(card.thumb_labels[0].width(), px(88))
            self.assertGreaterEqual(card.view_button.height(), px(30))
            self.assertEqual(card.desc_label.maximumHeight(),
                             card.desc_label.fontMetrics().lineSpacing() * 2 + px(2))
            self.assertTrue(all(chip.font().pixelSize() == px(11)
                                for chip in card.platform_chips))

        for scale in ('1.0', '1.25', '1.5', '1.75'):
            with self.subTest(scale=scale):
                self.panel.set_tab('all')
                apply_interface_scale(self.app, FakeScreen(), scale)
                self.panel.resize(px(1400), px(900))
                flush_layout(self.app, self.panel)
                card = next(iter(self.panel.cards.values()))
                assert_scaled(card)
                expected = measurements(card)

                self.panel.set_tab('done')
                flush_layout(self.app, self.panel)
                card = next(iter(self.panel.cards.values()))
                assert_scaled(card)
                self.assertEqual(measurements(card), expected)

                groups[0]['updated_at'] += 10
                groups[0]['platforms'] = [{'name': '离线平台'}, {'name': f'新增平台 {scale}'}]
                self.panel.update_groups(groups)
                flush_layout(self.app, self.panel)
                self.assertIs(next(iter(self.panel.cards.values())), card)
                self.assertEqual(len(card.platform_chips), 2)
                assert_scaled(card)
                self.assertEqual(measurements(card), expected)


class UiFontFamilyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_ui_font_preference_order_is_cached(self):
        for available, expected in (
            (['Segoe UI', 'Microsoft YaHei', 'Microsoft YaHei UI'], 'Microsoft YaHei UI'),
            (['Segoe UI', 'Microsoft YaHei'], 'Microsoft YaHei'),
            (['Segoe UI'], 'Segoe UI'),
            ([], self.app.font().family()),
        ):
            with self.subTest(fonts=available), patch.object(materials, '_UI_FONT_FAMILY', None), \
                    patch.object(materials, 'QFontDatabase') as database:
                database.return_value.families.return_value = available
                self.assertEqual(materials.ui_font_family(), expected)
                self.assertEqual(materials.ui_font_family(), expected)
                database.return_value.families.assert_called_once_with()

    def test_ui_font_database_failure_uses_system_font(self):
        with patch.object(materials, '_UI_FONT_FAMILY', None), \
                patch.object(materials, 'QFontDatabase', side_effect=RuntimeError('font fixture')):
            self.assertEqual(materials.ui_font_family(), self.app.font().family())


if __name__ == '__main__':
    unittest.main()
