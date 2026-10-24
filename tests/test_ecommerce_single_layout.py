"""Offline geometry and state tests for the single-creation two-column view."""
import os
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import QPoint, QRect, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QBoxLayout, QLabel, QScrollArea

from core.i18n import set_language, tr
from tests import test_ecommerce_page as page_fixture
from tests.qt_scale_support import flush_layout, reset_interface_scaling
from tests.test_interface_scaling import FakeScreen
from ui.scaling import apply_interface_scale
from ui.tokens import px


class EcommerceSingleLayoutTests(unittest.TestCase):
    """Reuse the isolated store and model clients without inheriting other tests."""

    setUpClass = page_fixture.EcommercePageTests.__dict__['setUpClass']
    make_manager = page_fixture.EcommercePageTests.make_manager
    wait_until = page_fixture.EcommercePageTests.wait_until
    fill_valid = page_fixture.EcommercePageTests.fill_valid
    seed_history = page_fixture.EcommercePageTests.seed_history

    def setUp(self):
        reset_interface_scaling(self.app)
        page_fixture.EcommercePageTests.setUp(self)
        self.page.resize(1440, 900)
        self.page.show_single_creation()
        self.page.show()
        flush_layout(self.app, self.page)

    def tearDown(self):
        try:
            page_fixture.EcommercePageTests.tearDown(self)
        finally:
            reset_interface_scaling(self.app)

    def mapped_rect(self, widget, parent=None):
        return QRect(widget.mapTo(parent or self.page, QPoint()), widget.size())

    def assert_scrolls_fit(self):
        for scroll in (self.page.body_scroll, self.page.result_scroll):
            self.assertTrue(scroll.isVisible())
            self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
            self.assertLessEqual(scroll.widget().width(), scroll.viewport().width())
            rect = self.mapped_rect(scroll)
            self.assertGreaterEqual(rect.left(), 0)
            self.assertLess(rect.right(), self.page.width())
        # The single view has two independently bounded scroll areas and no
        # third scroll container capable of introducing page-level overflow.
        self.assertEqual(set(self.page.single_view.findChildren(QScrollArea)),
                         {self.page.body_scroll, self.page.result_scroll})

    def test_wide_view_has_two_independent_columns_with_bounded_contents(self):
        self.assertEqual(self.page.body_scroll.objectName(), 'ecommerceFormScroll')
        self.assertEqual(self.page.result_scroll.objectName(), 'ecommerceResultScroll')
        self.assertIs(self.page.body_content, self.page.body_scroll.widget())
        self.assertEqual(self.page.single_columns_layout.direction(), QBoxLayout.LeftToRight)
        left, right = (self.mapped_rect(scroll) for scroll in
                       (self.page.body_scroll, self.page.result_scroll))
        self.assertLess(left.right(), right.left())
        self.assertAlmostEqual(left.width(), right.width(), delta=2)
        self.assertGreaterEqual(left.width(), px(420))
        self.assertTrue(self.page.body_content.isAncestorOf(self.page.model_card_container))
        self.assertTrue(self.page.result_scroll.widget().isAncestorOf(self.page.progress_card))
        self.assertTrue(self.page.result_scroll.widget().isAncestorOf(self.page.result_panel))
        self.assert_scrolls_fit()

    def test_narrow_view_stacks_columns_and_returns_to_side_by_side(self):
        self.page.resize(1000, 900)
        flush_layout(self.app, self.page)
        self.assertEqual(self.page.single_columns_layout.direction(), QBoxLayout.TopToBottom)
        left, right = (self.mapped_rect(scroll) for scroll in
                       (self.page.body_scroll, self.page.result_scroll))
        self.assertGreater(right.top(), left.bottom())
        self.assertAlmostEqual(left.left(), right.left(), delta=1)
        self.assertAlmostEqual(left.width(), right.width(), delta=1)
        self.assert_scrolls_fit()
        self.page.resize(1600, 900)
        flush_layout(self.app, self.page)
        self.assertEqual(self.page.single_columns_layout.direction(), QBoxLayout.LeftToRight)
        left, right = (self.mapped_rect(scroll) for scroll in
                       (self.page.body_scroll, self.page.result_scroll))
        self.assertLess(left.right(), right.left())
        self.assert_scrolls_fit()

    def test_header_copy_and_navigation_buttons_use_existing_view_actions(self):
        self.assertEqual(self.page.single_title_label.text(), '电商图制作')
        self.assertEqual(self.page.single_subtitle_label.text(),
                         '上传产品图，AI 自动生成电商轮播图与详情图')
        self.assertEqual(self.page.history_tasks_button.text(), '历史任务')
        self.assertEqual(self.page.batch_creation_button.text(), '批量制作')
        with patch.object(self.page, 'show_task_list') as history:
            self.page.history_tasks_button.click()
            history.assert_called_once_with()
        with patch.object(self.page, '_show_batch_view') as batch:
            self.page.batch_creation_button.click()
            batch.assert_called_once_with()
        header_before = self.mapped_rect(self.page.single_header)
        self.page.body_scroll.verticalScrollBar().setValue(
            self.page.body_scroll.verticalScrollBar().maximum())
        self.app.processEvents()
        self.assertEqual(self.mapped_rect(self.page.single_header), header_before)

    def test_empty_state_survives_invalid_start_and_has_future_header_host(self):
        self.assertTrue(self.page.empty_state.isVisible())
        self.assertEqual(self.page.empty_state.objectName(), 'ecommerceEmptyState')
        self.assertFalse(self.page.progress_card.isVisible())
        self.assertFalse(self.page.result_panel.isVisible())
        self.assertEqual(self.page.right_header_host.objectName(), 'ecommerceRightHeaderHost')
        self.assertEqual(self.page.right_header_host.height(), 0)
        for grid in (self.page.product_grid, self.page.style_grid):
            self.assertIsNotNone(grid.add_tile())
            self.assertTrue(grid.add_tile().isVisible())
            self.assertGreater(grid.add_tile().height(), 0)
        self.page.start_generation()
        self.assertIsNone(self.page._worker)
        self.factory.assert_not_called()
        self.notices.assert_called()
        self.assertTrue(self.page.empty_state.isVisible())
        self.assertFalse(self.page.progress_card.isVisible())

    def test_header_and_empty_guidance_retranslate_without_rebuilding_columns(self):
        original_scrolls = (self.page.body_scroll, self.page.result_scroll)
        set_language('English')
        try:
            self.page.retranslate_ui()
            flush_layout(self.app, self.page)
            self.assertEqual(self.page.single_title_label.text(), tr('电商图制作'))
            self.assertEqual(self.page.single_subtitle_label.text(),
                             tr('上传产品图，AI 自动生成电商轮播图与详情图'))
            self.assertEqual(self.page.history_tasks_button.text(), tr('历史任务'))
            self.assertEqual(self.page.batch_creation_button.text(), tr('批量制作'))
            empty_copy = [label.text() for label in self.page.empty_state.findChildren(QLabel)]
            for source in ('填写完左侧信息后点击「开始制作」', 'AI 将自动分析产品并生成图片'):
                self.assertNotEqual(tr(source), source)
                self.assertIn(tr(source), empty_copy)
            self.assertEqual((self.page.body_scroll, self.page.result_scroll), original_scrolls)
            self.assertTrue(self.page.empty_state.isVisible())
        finally:
            set_language('zh-CN')
            self.page.retranslate_ui()

    def test_history_group_shows_right_progress_results_and_clear_restores_empty(self):
        group, records = self.seed_history(states=('succeeded', 'failed'))
        self.page.show_task_list()
        self.page._view_history_group(group['group_id'])
        flush_layout(self.app, self.page)
        self.assertEqual(self.page.current_view_index(), self.page.SINGLE_VIEW)
        self.assertFalse(self.page.empty_state.isVisible())
        self.assertTrue(self.page.progress_card.isVisible())
        self.assertTrue(self.page.result_panel.isVisible())
        self.assertEqual(len(self.page.result_panel.cards), len(records))
        self.page._clear_results()
        flush_layout(self.app, self.page)
        self.assertTrue(self.page.empty_state.isVisible())
        self.assertFalse(self.page.progress_card.isVisible())
        self.assertFalse(self.page.result_panel.isVisible())

    def test_valid_offline_start_switches_to_progress_before_first_group_arrives(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.fill_valid()

        def blocked_analysis(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('离线分析桩未释放')
            return page_fixture.fake_analysis(*args, **kwargs)

        self.analysis.side_effect = blocked_analysis
        try:
            self.page.start_generation()
            self.wait_until(entered.is_set)
            self.assertTrue(self.page.busy)
            self.assertFalse(self.page.empty_state.isVisible())
            self.assertTrue(self.page.progress_card.isVisible())
            self.assertTrue(self.page.cancel_button.isEnabled())
            self.assertFalse(self.page.start_button.isEnabled())
        finally:
            release.set()
            self.wait_until(lambda: not self.page.busy)
        self.assertTrue(self.page.result_panel.isVisible())
        self.assertFalse(self.page.empty_state.isVisible())

    def test_supported_scales_keep_header_cards_and_scrolls_inside_page(self):
        for scale in ('1.0', '1.25', '1.5', '1.75'):
            apply_interface_scale(self.app, FakeScreen(), scale)
            for width in (1000, 1440):
                with self.subTest(scale=scale, width=width):
                    self.page.resize(px(width), px(900))
                    flush_layout(self.app, self.page)
                    self.assert_scrolls_fit()
                    title = self.mapped_rect(self.page.single_title_label,
                                             self.page.single_header)
                    buttons = [self.mapped_rect(button, self.page.single_header)
                               for button in (self.page.history_tasks_button,
                                              self.page.batch_creation_button)]
                    self.assertLess(title.right(), buttons[0].left())
                    self.assertLess(buttons[0].right(), buttons[1].left())
                    for button in buttons:
                        self.assertTrue(self.page.single_header.rect().contains(button))
                        self.assertEqual(button.height(), px(36))
                    for card in (self.page.product_upload_card, self.page.style_upload_card,
                                 self.page.platform_card, self.page.product_card,
                                 self.page.options_card, self.page.models_card):
                        margins = card.layout().contentsMargins()
                        self.assertEqual((margins.left(), margins.top(), margins.right(), margins.bottom()),
                                         (px(20), px(18), px(20), px(18)))
                        self.assertIn(f'border-radius: {px(12)}px', card.styleSheet())


if __name__ == '__main__':
    unittest.main()
