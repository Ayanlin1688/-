"""分析卡折叠、文本有界预览、自动滚动和语言切换回归。"""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import QPoint, QRect, Qt
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtGui import QTextCursor
from PyQt5.QtWidgets import QApplication

from core.i18n import set_language, tr
from tests.qt_scale_support import flush_layout, reset_interface_scaling
from ui import materials, tokens
from ui.components.ecommerce_analysis_view import (
    EcommerceAnalysisView, MAX_PROMPT_LENGTH, MAX_PROMPT_TEXT, MAX_REASONING_CHUNKS,
)


class EcommerceAnalysisViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        reset_interface_scaling(self.app)
        set_language('zh-CN')
        self.addCleanup(set_language, 'zh-CN')
        self.addCleanup(reset_interface_scaling, self.app)
        self.view = EcommerceAnalysisView()
        self.view.resize(540, 500)
        self.view.show()
        flush_layout(self.app, self.view)
        self.addCleanup(self.dispose_view)

    def dispose_view(self):
        self.view.close()
        self.view.deleteLater()
        self.app.processEvents()

    def test_default_empty_copy_and_expand_collapse_preserve_text(self):
        self.assertTrue(self.view.is_reasoning_expanded)
        self.assertFalse(self.view.is_prompt_expanded)
        self.assertTrue(self.view.reasoning_placeholder.isVisible())
        self.assertEqual(self.view.reasoning_placeholder.text(), '等待模型返回思考内容…')
        self.assertEqual(self.view.prompt_edit.toPlainText(), '分析完成后显示生成提示词')
        QTest.mouseClick(self.view.prompt_toggle, Qt.LeftButton)
        self.assertTrue(self.view.is_prompt_expanded)
        self.assertTrue(self.view.prompt_edit.isVisible())
        self.view.append_reasoning('商品信息核对')
        QTest.mouseClick(self.view.reasoning_toggle, Qt.LeftButton)
        self.assertFalse(self.view.reasoning_body.isVisible())
        QTest.mouseClick(self.view.reasoning_toggle, Qt.LeftButton)
        self.assertTrue(self.view.reasoning_body.isVisible())
        self.assertEqual(self.view.reasoning_edit.toPlainText(), '商品信息核对')
        self.assertTrue(self.view.reasoning_edit.isReadOnly())
        self.assertTrue(self.view.prompt_edit.isReadOnly())

    def test_reasoning_append_and_batch_set_are_bounded_and_scroll_to_bottom(self):
        self.view.set_reasoning_chunks(['第%d条思考内容' % index for index in range(300)])
        self.view.append_reasoning('最终片段')
        flush_layout(self.app, self.view)
        text = self.view.reasoning_edit.toPlainText()
        self.assertIn('最终片段', text)
        self.assertNotIn('第0条思考内容', text)
        self.assertEqual(len(self.view._reasoning_chunks), MAX_REASONING_CHUNKS)
        self.assertFalse(self.view.reasoning_placeholder.isVisible())
        bar = self.view.reasoning_edit.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        self.assertEqual(bar.value(), bar.maximum())

    def test_prompt_items_sort_carousel_before_detail_and_truncate_large_text(self):
        self.view.set_prompt_items([
            {'kind': 'detail', 'seq': 2, 'title': '详情二', 'prompt': 'detail second'},
            {'kind': 'carousel', 'seq': 2, 'title': '轮播二', 'prompt': 'b' * (MAX_PROMPT_LENGTH + 500)},
            {'kind': 'detail', 'seq': 1, 'title': '详情一', 'prompt': 'detail first'},
            {'kind': 'carousel', 'seq': 1, 'title': '轮播一', 'prompt': 'carousel first'},
        ])
        text = self.view.prompt_edit.toPlainText()
        markers = ['轮1 · 轮播一', '轮2 · 轮播二', '详1 · 详情一', '详2 · 详情二']
        self.assertEqual([text.index(marker) for marker in markers],
                         sorted(text.index(marker) for marker in markers))
        self.assertIn('b' * MAX_PROMPT_LENGTH + '…', text)
        self.assertNotIn('b' * (MAX_PROMPT_LENGTH + 1), text)
        self.assertTrue(self.view.prompt_edit.property('hasPromptItems'))
        self.view.set_prompt_items([{'kind': 'detail', 'seq': index, 'prompt': 'z' * MAX_PROMPT_LENGTH}
                                    for index in range(20)])
        self.assertLessEqual(len(self.view.prompt_edit.toPlainText()), MAX_PROMPT_TEXT + 1)

    def test_analysis_states_update_status_colors_elapsed_error_and_retry(self):
        retry = QSignalSpy(self.view.retry_requested)
        palette = {
            'analyzing': materials.ecommerce_color('analysis_active'),
            'finished': materials.ecommerce_color('analysis_finished'),
            'failed': materials.ecommerce_color('analysis_failed'),
        }

        self.view.set_analysis_state('analyzing', 12, 'fixture-chat')
        self.assertIn('12', self.view.status_detail.text())
        self.assertTrue(self.view.status_label.text().startswith('思考中…'))
        self.assertIn('fixture-chat', self.view.status_detail.text())
        self.assertIn(palette['analyzing'], self.view.status_label.styleSheet())
        self.view.set_analysis_state('finished', 13, 'fixture-chat')
        self.assertIn('13', self.view.status_label.text())
        self.assertIn(palette['finished'], self.view.status_label.styleSheet())

        self.view.set_analysis_state('failed', 14, 'fixture-chat', '分析超时，请切换模型')
        self.assertEqual(self.view.status_label.text(), '分析失败')
        self.assertTrue(self.view.retry_button.isVisible())
        self.assertIn('分析超时', self.view.status_error.text())
        self.assertIn(palette['failed'], self.view.status_label.styleSheet())
        QTest.mouseClick(self.view.retry_button, Qt.LeftButton)
        self.assertEqual(len(retry), 1)

        self.view.set_analysis_state('cancelled', 15, 'fixture-chat')
        self.assertEqual(self.view.status_label.text(), '分析已取消')
        self.assertFalse(self.view.retry_button.isVisible())

    def test_raw_output_is_collapsed_until_requested_and_stream_updates_in_place(self):
        self.view.append_raw_output('{"partial":')
        self.view.append_raw_output('"thinking"}')
        self.assertFalse(self.view.raw_edit.isVisible())
        self.assertEqual(self.view._raw_output, '{"partial":"thinking"}')
        QTest.mouseClick(self.view.raw_toggle, Qt.LeftButton)
        self.assertTrue(self.view.raw_edit.isVisible())
        self.assertEqual(self.view.raw_edit.toPlainText(), self.view._raw_output)
        self.view.append_raw_output(' more')
        self.assertEqual(self.view.raw_edit.toPlainText(), '{"partial":"thinking"} more')

    def test_long_prompt_can_expand_after_default_truncation(self):
        prompt = '提示词内容' * (MAX_PROMPT_LENGTH // 5 + 5)
        self.view.set_prompt_items([{'kind': 'carousel', 'seq': 1, 'prompt': prompt}])
        self.assertTrue(self.view.prompt_expand_button.isVisible())
        self.assertNotIn(prompt, self.view.prompt_edit.toPlainText())
        QTest.mouseClick(self.view.prompt_expand_button, Qt.LeftButton)
        self.assertIn(prompt, self.view.prompt_edit.toPlainText())
        self.assertEqual(self.view.prompt_expand_button.text(), '收起完整提示词')

    def test_clear_resets_text_and_expansion_for_next_creation(self):
        self.view.append_reasoning('上一任务')
        self.view.set_prompt_items([{'kind': 'carousel', 'seq': 1, 'prompt': 'previous'}])
        QTest.mouseClick(self.view.prompt_toggle, Qt.LeftButton)
        self.view.clear()
        self.assertEqual(self.view.reasoning_edit.toPlainText(), '')
        self.assertEqual(self.view.prompt_edit.toPlainText(), '分析完成后显示生成提示词')
        self.assertTrue(self.view.is_reasoning_expanded)
        self.assertFalse(self.view.is_prompt_expanded)
        self.assertTrue(self.view.reasoning_placeholder.isVisible())

    def test_status_only_item_updates_preserve_prompt_scroll_and_selection(self):
        items = [dict(kind='carousel', seq=index, title='商品',
                      prompt=('Line %d\n' % index) * 20, status='pending')
                 for index in range(1, 21)]
        self.view.set_prompt_items(items)
        QTest.mouseClick(self.view.prompt_toggle, Qt.LeftButton)
        flush_layout(self.app, self.view)
        cursor = self.view.prompt_edit.textCursor()
        cursor.setPosition(5)
        cursor.setPosition(25, QTextCursor.KeepAnchor)
        self.view.prompt_edit.setTextCursor(cursor)
        bar = self.view.prompt_edit.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(bar.maximum() // 2)
        before = (bar.value(), cursor.position(), cursor.anchor())
        edit = self.view.prompt_edit
        with patch.object(edit, 'setPlainText', wraps=edit.setPlainText) as replace_text:
            self.view.set_prompt_items([dict(item, status='running') for item in items])
            self.assertEqual(replace_text.call_count, 0)
        after_cursor = edit.textCursor()
        self.assertEqual((bar.value(), after_cursor.position(), after_cursor.anchor()), before)

    def test_language_bus_retranslates_placeholders_and_prompt_badges(self):
        set_language('en-US')
        self.app.processEvents()
        self.assertEqual(self.view.reasoning_title.text(), 'Reasoning')
        self.assertEqual(self.view.prompt_title.text(), 'Prompts')
        self.assertEqual(self.view.reasoning_placeholder.text(), tr('等待模型返回思考内容…'))
        self.assertNotEqual(self.view.prompt_edit.toPlainText(), '分析完成后显示生成提示词')
        self.view.set_prompt_items([{'kind': 'carousel', 'seq': 1, 'title': 'Cup', 'prompt': 'Product'}])
        self.assertIn(tr('轮{seq} · {title}').format(seq=1, title='Cup'),
                      self.view.prompt_edit.toPlainText())

    def test_supported_scales_keep_padding_and_editor_bounds_idempotent(self):
        for scale in (1.0, 1.25, 1.5, 1.75):
            tokens.set_interface_scale(scale)
            self.view.refresh_interface_scale()
            self.view.refresh_interface_scale()
            self.assertEqual(self.view.layout().contentsMargins().left(), tokens.px(20))
            self.assertLessEqual(self.view.reasoning_edit.maximumHeight(), tokens.px(180))
            self.assertLessEqual(self.view.prompt_edit.maximumHeight(), tokens.px(220))
            self.assertEqual(self.view.prompt_title.font().pixelSize(), tokens.px(13))

    def test_analysis_sections_stay_compact_at_supported_scales(self):
        self.view.set_analysis_state('analyzing', 18, 'fixture-chat')
        self.view.set_reasoning_chunks(['正在核对商品材质和卖点。', '分析内容继续流式返回。'])
        self.view.set_prompt_items([dict(kind='carousel', seq=1, title='主图',
                                         prompt='保留商品外观与材质细节。')])
        self.view.prompt_toggle.click()
        for scale in (1.0, 1.25, 1.5, 1.75):
            tokens.set_interface_scale(scale)
            self.view.refresh_interface_scale()
            self.view.resize(tokens.px(540), tokens.px(500))
            flush_layout(self.app, self.view)
            def rect_in_view(widget):
                return QRect(widget.mapTo(self.view, QPoint(0, 0)), widget.size())

            status = rect_in_view(self.view.status_card)
            reasoning_title = rect_in_view(self.view.reasoning_title)
            reasoning_edit = rect_in_view(self.view.reasoning_edit)
            prompt_title = rect_in_view(self.view.prompt_title)
            prompt_edit = rect_in_view(self.view.prompt_edit)
            raw_title = rect_in_view(self.view.raw_title)
            self.assertTrue(self.view.reasoning_edit.isVisible())
            self.assertTrue(self.view.prompt_edit.isVisible())
            self.assertLessEqual(reasoning_title.top() - status.bottom() - 1, tokens.px(12))
            self.assertLessEqual(prompt_title.top() - reasoning_edit.bottom() - 1,
                                 tokens.px(12))
            self.assertLessEqual(raw_title.top() - prompt_edit.bottom() - 1, tokens.px(12))
            self.assertLessEqual(self.view.reasoning_edit.height(), tokens.px(180))
            self.assertLessEqual(self.view.reasoning_body.height(), tokens.px(180))
            self.assertLessEqual(self.view.prompt_edit.height(), tokens.px(220))


if __name__ == '__main__':
    unittest.main()
