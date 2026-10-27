"""Offline geometry and state tests for the single-creation two-column view."""
import os
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import QEvent, QObject, QPoint, QRect, Qt
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtWidgets import QBoxLayout, QLabel, QScrollArea

from core.i18n import set_language, tr
from tests import test_ecommerce_page as page_fixture
from tests.qt_scale_support import flush_layout, reset_interface_scaling
from tests.test_interface_scaling import FakeScreen
from ui.scaling import apply_interface_scale
from ui.tokens import px


class _RightColumnGeometryEvents(QObject):
    """Record viewport feedback rather than accepting transient equal sizes."""

    def __init__(self, widgets, parent):
        super().__init__(parent)
        self.events = []
        self.names = {widget: name for name, widget in widgets.items()}
        for widget in self.names:
            widget.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Resize, QEvent.Show, QEvent.Hide):
            self.events.append((self.names[watched], int(event.type()),
                                watched.size().width(), watched.size().height()))
        return False


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
        self.assertEqual({s for s in self.page.single_view.findChildren(QScrollArea)
                          if s.window() is self.page.window()},
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
        self.assertTrue(self.page.result_scroll.widget().isAncestorOf(self.page.step_bar))
        self.assertTrue(self.page.progress_card.isHidden())
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

    def test_empty_state_survives_invalid_start_and_keeps_step_bar_hidden(self):
        self.assertTrue(self.page.empty_state.isVisible())
        self.assertEqual(self.page.empty_state.objectName(), 'ecommerceEmptyState')
        self.assertFalse(self.page.progress_card.isVisible())
        self.assertFalse(self.page.result_panel.isVisible())
        self.assertEqual(self.page.right_header_host.objectName(), 'ecommerceRightHeaderHost')
        self.assertEqual(self.page.step_bar.stage, 'idle')
        self.assertFalse(self.page.step_bar.isVisible())
        self.assertFalse(self.page.analysis_view.isVisible())
        self.assertFalse(self.page.submission_view.isVisible())
        self.assertFalse(self.page.completion_bar.isVisible())
        self.assertFalse(self.page.completion_footer.isVisible())
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
        self.assertEqual(self.page._right_state, 'idle')

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

    def test_history_group_shows_finished_results_and_clear_restores_empty(self):
        group, records = self.seed_history(states=('succeeded', 'failed'))
        self.page.show_task_list()
        self.page._view_history_group(group['group_id'])
        flush_layout(self.app, self.page)
        self.assertEqual(self.page.current_view_index(), self.page.SINGLE_VIEW)
        self.assertFalse(self.page.empty_state.isVisible())
        self.assertEqual(self.page._right_state, 'finished')
        self.assertEqual(self.page.step_bar.stage, 'failed')
        self.assertTrue(self.page.step_bar.isVisible())
        self.assertGreater(self.page.right_header_host.height(), 0)
        self.assertFalse(self.page.progress_card.isVisible())
        self.assertTrue(self.page.result_panel.isVisible())
        self.assertTrue(self.page.completion_bar.isVisible())
        self.assertTrue(self.page.completion_footer.isVisible())
        self.assertEqual(len(self.page.result_panel.cards), len(records))
        self.page._clear_results()
        flush_layout(self.app, self.page)
        self.assertTrue(self.page.empty_state.isVisible())
        self.assertEqual(self.page._right_state, 'idle')
        self.assertFalse(self.page.step_bar.isVisible())
        self.assertFalse(self.page.progress_card.isVisible())
        self.assertFalse(self.page.result_panel.isVisible())
        self.assertFalse(self.page.completion_footer.isVisible())

    def test_valid_offline_start_switches_to_analysis_before_first_group_arrives(self):
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
            self.assertEqual(self.page._right_state, 'analyzing')
            self.assertEqual(self.page.step_bar.stage, 'analyzing')
            self.assertTrue(self.page.analysis_view.isVisible())
            self.assertFalse(self.page.progress_card.isVisible())
            self.assertTrue(self.page.cancel_button.isEnabled())
            self.assertFalse(self.page.start_button.isEnabled())
        finally:
            release.set()
            self.wait_until(lambda: not self.page.busy)
        self.assertTrue(self.page.result_panel.isVisible())
        self.assertFalse(self.page.empty_state.isVisible())

    def test_analyzing_group_shows_reasoning_and_collects_prompt_records(self):
        group, records = self.seed_history(status='analyzing', states=('pending', 'pending'))
        self.page._on_group(group)
        self.page._append_log('[思考中] 商品材质与卖点已确认')
        for record in records:
            self.page._on_item(record)
        flush_layout(self.app, self.page)
        self.assertEqual(self.page._right_state, 'analyzing')
        self.assertEqual(self.page.step_bar.stage, 'analyzing')
        self.assertTrue(self.page.analysis_view.isVisible())
        self.assertIn('商品材质与卖点已确认',
                      self.page.analysis_view.reasoning_edit.toPlainText())
        self.assertEqual(len(self.page._reasoning_chunks), 1)
        self.assertEqual(set(self.page._prompt_items), {('carousel', 1), ('carousel', 2)})
        for widget in (self.page.empty_state, self.page.submission_view,
                       self.page.result_panel, self.page.completion_bar,
                       self.page.completion_footer):
            self.assertFalse(widget.isVisible())

    def test_generating_group_shows_submission_grid_and_half_complete_progress(self):
        group, records = self.seed_history(status='generating',
                                          states=('succeeded', 'failed', 'running', 'pending'))
        self.page._on_group(group)
        for record in records:
            self.page._on_item(record)
        flush_layout(self.app, self.page)
        self.assertEqual(self.page._right_state, 'generating')
        self.assertEqual(self.page.step_bar.stage, 'generating')
        self.assertTrue(self.page.submission_view.isVisible())
        self.assertEqual(len(self.page.submission_view.cells), 4)
        labels = [label.text() for label in self.page.submission_view.findChildren(QLabel)]
        self.assertIn('已完成 2/4（失败 1）', labels)
        for widget in (self.page.empty_state, self.page.analysis_view,
                       self.page.result_panel, self.page.completion_bar,
                       self.page.completion_footer):
            self.assertFalse(widget.isVisible())
        with patch.object(self.page, 'cancel_generation') as cancel:
            self.page.submission_view.cancel_requested.emit()
            cancel.assert_called_once_with()
        with patch.object(self.page, '_run_regenerate') as retry:
            self.page.submission_view.retry_requested.emit(('carousel', 2))
            retry.assert_called_once_with([('carousel', 2)])

    def test_success_completion_bars_preserve_existing_actions(self):
        group, _ = self.seed_history(status='succeeded', states=('succeeded', 'succeeded'))
        self.page._view_history_group(group['group_id'])
        flush_layout(self.app, self.page)
        self.assertEqual(self.page._right_state, 'finished')
        self.assertEqual(self.page.step_bar.stage, 'finished')
        self.assertEqual(self.page.completion_title_label.text(), '全部完成（2 张）')
        self.assertTrue(self.page.result_panel.isVisible())
        self.assertTrue(self.page.completion_bar.isVisible())
        self.assertTrue(self.page.completion_footer.isVisible())
        buttons = (self.page.footer_tasks_button, self.page.footer_regenerate_button,
                   self.page.footer_new_button, self.page.footer_done_button)
        self.assertEqual([button.text() for button in buttons],
                         ['查看任务组', '重新生成', '+ 新建制作', '已完成'])
        self.assertFalse(self.page.footer_done_button.isEnabled())
        with patch.object(self.page, '_show_platform_preview') as preview:
            self.page.completion_preview_button.click()
            preview.assert_called_once_with(group['group_id'])
        with patch.object(self.page, '_run_regenerate') as regenerate:
            self.page.completion_regenerate_button.click()
            self.page.footer_regenerate_button.click()
            self.assertEqual(regenerate.call_count, 2)
        with patch.object(self.page, 'open_output') as open_output:
            self.page.completion_open_button.click()
            open_output.assert_called_once_with()
        with patch.object(self.page, 'show_task_list') as tasks:
            self.page.footer_tasks_button.click()
            tasks.assert_called_once_with()
        with patch.object(self.page, '_start_new_creation') as new:
            self.page.footer_new_button.click()
            new.assert_called_once_with()

    def test_partial_completion_marks_title_and_step_as_failed(self):
        for status in ('partial', 'failed'):
            with self.subTest(status=status):
                group, _ = self.seed_history(group_id='finished-' + status, status=status,
                                             states=('succeeded', 'failed'))
                self.page._view_history_group(group['group_id'])
                flush_layout(self.app, self.page)
                self.assertEqual(self.page._right_state, 'finished')
                self.assertEqual(self.page.step_bar.stage, 'failed')
                self.assertEqual(self.page.completion_title_label.text(),
                                 '部分完成（成功 1 / 共 2 张）')
                self.assertTrue(self.page.completion_title_label.property('hasFailures'))
                self.assertTrue(self.page.result_panel.isVisible())
                self.assertTrue(self.page.completion_footer.isVisible())

    def test_regenerate_launch_uses_generating_until_first_group_then_accepts_terminal_while_busy(self):
        group, _ = self.seed_history(status='partial', states=('succeeded', 'failed'))
        self.page._view_history_group(group['group_id'])
        worker = page_fixture.EcommerceWorker(
            self.page._make_manager, {}, self.page.cancel_event, operation='regenerate')
        try:
            with patch('ui.pages.ecommerce_page.EcommerceWorker', return_value=worker), \
                    patch.object(worker, 'start') as start:
                self.page._run_regenerate()
            start.assert_called_once_with()
            self.assertTrue(self.page.busy)
            self.assertEqual(self.page._group['status'], 'partial')
            self.assertEqual(self.page._right_state, 'generating')
            self.assertEqual(self.page.step_bar.stage, 'generating')
            self.assertTrue(self.page.submission_view.isVisible())
            self.assertFalse(self.page.completion_footer.isVisible())
            self.factory.assert_not_called()

            set_language('English')
            try:
                self.page.retranslate_ui()
                self.assertEqual(self.page.stage_label.text(), tr('图片生成中'))
                self.assertEqual(self.page._right_state, 'generating')
            finally:
                set_language('zh-CN')
                self.page.retranslate_ui()

            # A terminal callback is authoritative even before the QThread's
            # native finished signal clears busy; it must not revert to active.
            self.page._on_group(dict(group, status='partial',
                                     updated_at=group['updated_at'] + 1))
            self.assertTrue(self.page.busy)
            self.assertEqual(self.page._right_state, 'finished')
            self.assertEqual(self.page.step_bar.stage, 'failed')
            self.assertTrue(self.page.completion_footer.isVisible())
            self.assertFalse(self.page.submission_view.isVisible())
        finally:
            page_fixture._LIVE_WORKERS.discard(worker)
            self.page._thread_stopped()
            worker.deleteLater()
            self.app.processEvents()

    def test_regenerate_live_items_replace_stale_failure_totals_without_double_counting(self):
        group, records = self.seed_history(status='partial', states=('succeeded', 'failed'))
        self.page._view_history_group(group['group_id'])
        self.page._on_group(dict(group, status='generating',
                                 updated_at=group['updated_at'] + 1))
        retried = records[1]
        self.page._on_item(dict(retried, status='running',
                                updated_at=retried['updated_at'] + 1))
        self.assertEqual(self.page._group['totals']['failed'], 1)
        self.assertEqual(self.page._right_progress(self.page._right_items()), (2, 1, 0))
        labels = [label.text() for label in self.page.submission_view.findChildren(QLabel)]
        self.assertIn('已完成 1/2（失败 0）', labels)

        self.page._on_item(dict(retried, status='succeeded',
                                updated_at=retried['updated_at'] + 2))
        self.assertEqual(self.page._right_progress(self.page._right_items()), (2, 2, 0))
        labels = [label.text() for label in self.page.submission_view.findChildren(QLabel)]
        self.assertIn('已完成 2/2（失败 0）', labels)
        self.assertFalse(self.page.completion_title_label.property('hasFailures'))

    def test_same_right_state_skips_reapplying_styles_until_failure_color_changes(self):
        group, _ = self.seed_history(status='succeeded', states=('succeeded', 'succeeded'))
        self.page._view_history_group(group['group_id'])
        with patch.object(self.page, '_refresh_right_styles',
                          wraps=self.page._refresh_right_styles) as refresh:
            for _ in range(3):
                self.page._sync_right_states()
            refresh.assert_not_called()
            key = ('carousel', 2)
            self.page._items[key] = dict(self.page._items[key], status='failed')
            self.page._sync_right_states()
            self.assertTrue(self.page.completion_title_label.property('hasFailures'))
            refresh.assert_called_once_with()
            self.page._sync_right_states()
            self.assertEqual(refresh.call_count, 1)

    def test_next_group_clears_previous_grid_results_prompts_and_reasoning(self):
        previous, _ = self.seed_history(group_id='first-group', status='succeeded',
                                         states=('succeeded', 'succeeded', 'failed'))
        self.page._view_history_group(previous['group_id'])
        self.page._append_log('[思考中] 上一组的思考')
        self.assertEqual(len(self.page.result_panel.cards), 3)
        self.assertTrue(self.page._prompt_items)

        current, records = self.seed_history(group_id='next-group', status='generating',
                                             states=('running',))
        self.page._on_group(current)
        self.page._on_item(records[0])
        flush_layout(self.app, self.page)
        self.assertEqual(self.page._right_state, 'generating')
        self.assertEqual(set(self.page.submission_view.cells), {('carousel', 1)})
        self.assertEqual(set(self.page.result_panel.cards), {records[0]['item_id']})
        self.assertEqual(set(self.page._prompt_items), {('carousel', 1)})
        self.assertEqual(self.page._prompt_items['carousel', 1]['group_id'], current['group_id'])
        self.assertEqual(list(self.page._reasoning_chunks), [])
        self.assertNotIn('上一组的思考', self.page.analysis_view.reasoning_edit.toPlainText())
        self.assertEqual(self.page._right_progress(self.page._right_items()), (1, 0, 0))

    def test_prompt_menu_actions_copy_by_kind_and_all_in_order_after_language_switch(self):
        items = [
            dict(kind='detail', seq=1, title='详情说明', prompt='detail prompt'),
            dict(kind='carousel', seq=2, title='轮播次屏', prompt='carousel prompt two'),
            dict(kind='carousel', seq=1, title='轮播首屏', prompt='carousel prompt one'),
        ]
        self.page._prompt_items = {(item['kind'], item['seq']): item for item in items}
        actions = tuple(action for action, _source in self.page.prompt_actions)
        self.assertIs(self.page.completion_prompt_button.menu(), self.page.prompt_menu)
        with patch.object(self.page.prompt_menu, 'exec') as show_menu:
            QTest.mouseClick(self.page.completion_prompt_button, Qt.LeftButton)
            show_menu.assert_called_once()
        ordered = [items[2], items[1], items[0]]

        def copied_text(selected):
            return '\n\n'.join(
                tr('轮{seq} · {title}' if item['kind'] == 'carousel' else
                   '详{seq} · {title}').format(**item) + '\n' + item['prompt']
                for item in selected)

        try:
            with patch('ui.pages.ecommerce_page.InfoBar.success') as success:
                for language in ('zh-CN', 'English'):
                    set_language(language)
                    self.page.retranslate_ui()
                    self.assertEqual(tuple(action for action, _source in self.page.prompt_actions),
                                     actions)
                    for (action, source), selected in zip(
                            self.page.prompt_actions, (ordered[:2], ordered[2:], ordered)):
                        with self.subTest(language=language, action=source):
                            self.assertEqual(action.text(), tr(source))
                            action.trigger()
                            self.assertEqual(self.app.clipboard().text(), copied_text(selected))
                            self.assertEqual(success.call_args.kwargs['content'], tr('提示词已复制'))
                self.assertEqual(success.call_count, 6)
                self.factory.assert_not_called()
        finally:
            set_language('zh-CN')
            self.page.retranslate_ui()

    def test_completed_right_column_retranslates_without_losing_results(self):
        group, _ = self.seed_history(status='succeeded', states=('succeeded', 'succeeded'))
        self.page._view_history_group(group['group_id'])
        cards = dict(self.page.result_panel.cards)
        set_language('English')
        try:
            self.page.retranslate_ui()
            flush_layout(self.app, self.page)
            self.assertEqual(self.page.completion_title_label.text(),
                             tr('全部完成（{n} 张）').format(n=2))
            self.assertEqual(self.page.completion_open_button.text(),
                             tr('打开文件夹({n})').format(n=2))
            for button, source in (
                    (self.page.completion_prompt_button, '提示词'),
                    (self.page.completion_preview_button, '查看平台效果'),
                    (self.page.footer_tasks_button, '查看任务组'),
                    (self.page.footer_new_button, '+ 新建制作'),
                    (self.page.footer_done_button, '已完成')):
                self.assertNotEqual(tr(source), source)
                self.assertEqual(button.text(), tr(source))
            self.assertEqual(dict(self.page.result_panel.cards), cards)
            self.assertEqual(self.page.step_bar.stage, 'finished')
            self.assertTrue(self.page.completion_footer.isVisible())
        finally:
            set_language('zh-CN')
            self.page.retranslate_ui()

    def test_prompt_dropdown_reserves_arrow_space_in_both_languages_and_supported_scales(self):
        group, _ = self.seed_history(status='succeeded', states=('succeeded',))
        self.page._view_history_group(group['group_id'])
        button = self.page.completion_prompt_button
        try:
            for language in ('zh-CN', 'English'):
                set_language(language)
                self.page.retranslate_ui()
                for scale in ('1.0', '1.25', '1.5', '1.75'):
                    with self.subTest(language=language, scale=scale):
                        apply_interface_scale(self.app, FakeScreen(), scale)
                        flush_layout(self.app, self.page)
                        text_width = button.fontMetrics().horizontalAdvance(button.text())
                        self.assertGreaterEqual(button.width(), text_width + px(40))
        finally:
            set_language('zh-CN')
            self.page.retranslate_ui()

    def test_active_right_states_stay_bounded_at_supported_scales(self):
        groups = [self.seed_history(group_id='scaled-' + status, status=status,
                                    states=states)[0]
                  for status, states in (
                      ('analyzing', ('pending', 'pending')),
                      ('generating', ('succeeded', 'running')),
                      ('succeeded', ('succeeded', 'succeeded')))]
        for scale in ('1.0', '1.25', '1.5', '1.75'):
            apply_interface_scale(self.app, FakeScreen(), scale)
            for width in (1000, 1440):
                for group in groups:
                    with self.subTest(scale=scale, width=width, status=group['status']):
                        self.page.resize(px(width), px(900))
                        self.page._view_history_group(group['group_id'])
                        flush_layout(self.app, self.page)
                        self.assert_scrolls_fit()
                        self.assertTrue(self.page.step_bar.isVisible())
                        self.assertEqual(self.page.step_bar.height(), px(48))
                        self.assertFalse(self.page.empty_state.isVisible())
                        if group['status'] == 'succeeded':
                            buttons = (self.page.footer_tasks_button,
                                       self.page.footer_regenerate_button,
                                       self.page.footer_new_button,
                                       self.page.footer_done_button)
                            for button in buttons:
                                rect = self.mapped_rect(button, self.page.completion_footer)
                                self.assertTrue(self.page.completion_footer.rect().contains(rect))

    def test_right_scrollbar_reserved_only_for_finished_state_with_results(self):
        scroll = self.page.result_scroll
        self.assertEqual(self.page._right_state, 'idle')
        self.assertEqual(scroll.verticalScrollBarPolicy(), Qt.ScrollBarAsNeeded)

        finished, _ = self.seed_history(status='succeeded',
                                        states=('succeeded', 'succeeded'))
        self.page._view_history_group(finished['group_id'])
        flush_layout(self.app, self.page)
        self.assertTrue(self.page.result_panel.cards)
        self.assertEqual(scroll.verticalScrollBarPolicy(), Qt.ScrollBarAlwaysOn)
        self.assertTrue(scroll.verticalScrollBar().isVisible())

        # Existing result cards may remain in memory during active callbacks;
        # they must not keep a reserved scrollbar on the analysis/submission UI.
        for offset, status in enumerate(('analyzing', 'generating'), 1):
            with self.subTest(status=status):
                self.page._on_group(dict(finished, status=status,
                                         updated_at=finished['updated_at'] + offset))
                flush_layout(self.app, self.page)
                self.assertTrue(self.page.result_panel.cards)
                self.assertEqual(self.page._right_state, status)
                self.assertEqual(scroll.verticalScrollBarPolicy(), Qt.ScrollBarAsNeeded)
                self.assert_scrolls_fit()

        self.page._clear_results()
        flush_layout(self.app, self.page)
        self.assertEqual(self.page._right_state, 'idle')
        self.assertEqual(scroll.verticalScrollBarPolicy(), Qt.ScrollBarAsNeeded)
        self.assertFalse(scroll.verticalScrollBar().isVisible())

        empty_finished, _ = self.seed_history(group_id='finished-without-items',
                                              status='failed', states=())
        self.page._view_history_group(empty_finished['group_id'])
        flush_layout(self.app, self.page)
        self.assertEqual(self.page._right_state, 'finished')
        self.assertFalse(self.page.result_panel.cards)
        self.assertEqual(scroll.verticalScrollBarPolicy(), Qt.ScrollBarAsNeeded)
        self.assert_scrolls_fit()

    def _right_scroll_snapshot(self):
        scroll = self.page.result_scroll
        bar = scroll.verticalScrollBar()
        return dict(
            policy=int(scroll.verticalScrollBarPolicy()),
            scrollbar_visible=bar.isVisible(), scrollbar_width=bar.width(),
            scrollbar_geometry=bar.geometry().getRect(),
            scrollbar_range=(bar.minimum(), bar.maximum()),
            viewport_geometry=scroll.viewport().geometry().getRect(),
            content_geometry=scroll.widget().geometry().getRect(),
            result_panel_geometry=self.page.result_panel.geometry().getRect())

    def test_finished_scrollbar_geometry_converges_at_100_125_150_175_percent(self):
        self.right_scroll_stability_samples = []
        for scale in ('1.0', '1.25', '1.5', '1.75'):
            with self.subTest(scale=scale):
                self.page._clear_results()
                apply_interface_scale(self.app, FakeScreen(), scale)
                self.page.resize(px(1440), px(900))
                flush_layout(self.app, self.page)
                scroll = self.page.result_scroll
                self.assertEqual(scroll.verticalScrollBarPolicy(), Qt.ScrollBarAsNeeded)
                recorder = _RightColumnGeometryEvents(dict(
                    viewport=scroll.viewport(), content=scroll.widget(),
                    result_panel=self.page.result_panel,
                    scrollbar=scroll.verticalScrollBar()), self.page)
                ranges = QSignalSpy(scroll.verticalScrollBar().rangeChanged)
                group, _ = self.seed_history(group_id='stable-completion-' + scale,
                                             status='succeeded',
                                             states=('succeeded', 'succeeded'))
                self.page._view_history_group(group['group_id'])
                flush_layout(self.app, self.page)
                # Cover the submission grid's 100ms deferred layout and queued
                # scrollbar events before defining the settled baseline.
                QTest.qWait(120)
                flush_layout(self.app, self.page)
                self.app.processEvents()
                first = self._right_scroll_snapshot()
                transition_events, transition_ranges = len(recorder.events), len(ranges)
                self.assertEqual(len(self.page.result_panel.cards), 2)
                self.assertEqual(first['policy'], int(Qt.ScrollBarAlwaysOn))
                self.assertTrue(first['scrollbar_visible'])
                self.assertGreater(first['scrollbar_width'], 0)
                self.assert_scrolls_fit()

                for _ in range(3):
                    self.page._sync_right_states()
                    flush_layout(self.app, self.page)
                QTest.qWait(120)
                flush_layout(self.app, self.page)
                self.app.processEvents()
                second = self._right_scroll_snapshot()
                self.assertEqual(second, first)
                self.assertEqual(len(recorder.events), transition_events,
                                 recorder.events[transition_events:])
                self.assertEqual(len(ranges), transition_ranges,
                                 list(ranges)[transition_ranges:])
                self.assert_scrolls_fit()
                self.right_scroll_stability_samples.append(dict(
                    scale=float(scale), first=first, second=second,
                    transition_geometry_events=transition_events,
                    transition_range_changes=transition_ranges,
                    settled_geometry_events=len(recorder.events) - transition_events,
                    settled_range_changes=len(ranges) - transition_ranges))
                for widget in recorder.names:
                    widget.removeEventFilter(recorder)
                recorder.deleteLater()

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


    def test_model_card_alignment_equal_columns_and_language_style(self):
        self.assertEqual(self.page.models_card.x(),self.page.options_card.x())
        self.assertEqual(self.page.models_card.width(),self.page.options_card.width())
        self.assertEqual(self.page.models_card.layout().contentsMargins(),self.page.options_card.layout().contentsMargins())
        self.assertGreater(self.page.model_card_container.maximumWidth(),980)
        controls = (self.page.analysis_model_picker,self.page.image_model_picker)
        for control in controls:
            self.assertGreaterEqual(control.width(),px(260))
            self.assertEqual(control.toolTip(),control.currentText())
        self.assertLessEqual(abs(controls[0].width()-controls[1].width()),1)
        batch = self.page.batch_page
        self.assertEqual(self.page.language.height(),batch.language_combo.height())
        self.assertEqual(type(self.page.language),type(batch.language_combo))


if __name__ == '__main__':
    unittest.main()
