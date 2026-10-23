"""CJK fonts, single-line settings headings and bounded probe result bars."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import QAbstractAnimation, QCoreApplication, QEvent, QPoint, Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget
from qfluentwidgets import FluentIcon as FIF, LineEdit, OptionsConfigItem, OptionsValidator

from core.config_manager import ConfigManager
from ui.components import custom_widgets
from ui.pages.settings_page import (
    CustomSettingCard, JsonComboBoxSettingCard, SettingsPage, StudioSwitchSettingCard,
)
from ui.pages.ecommerce_page import EcommercePage
from ui.scaling import apply_interface_scale
from tests.qt_scale_support import flush_layout, reset_interface_scaling
from tests.test_interface_scaling import FakeScreen
from tests.test_task_manager import wait_until


class SettingsCardLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        reset_interface_scaling(self.app)
        self.original_font = QFont(self.app.font())
        self.root = QWidget()
        self.layout = QVBoxLayout(self.root)

    def tearDown(self):
        self.root.close()
        self.root.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        reset_interface_scaling(self.app)
        self.app.setFont(self.original_font)

    def cards(self):
        title = '收到响应后无内容判定为失败 · 自动决定是否重试 · 定时结束后是否保持运行'
        cards = [CustomSettingCard(title, LineEdit()), StudioSwitchSettingCard(FIF.SETTING, title)]
        item = OptionsConfigItem('fixture', 'choice', 'active', OptionsValidator(['active', 'other']))
        combo = JsonComboBoxSettingCard(item, FIF.SETTING, title, texts=['active', 'other'])
        combo._prepare_responsive_card()
        cards.append(combo)
        for card in cards:
            card.setContent('默认轮询间隔（秒） · 图床 API Key（上传 Token） · AI分析（文本）线路')
            self.layout.addWidget(card)
        return cards

    def assert_heading_fits(self, card):
        self.assertFalse(card._heading.geometry().intersects(card._responsive_controls.geometry()))
        for text_label in (card.titleLabel, card.contentLabel):
            self.assertFalse(text_label.wordWrap())
            self.assertNotIn('\n', text_label.text())
            self.assertEqual(text_label.toolTip(), card._card_label_texts[text_label])
            top = text_label.mapTo(card, QPoint()).y()
            self.assertGreaterEqual(top, card.hBoxLayout.contentsMargins().top())
            self.assertLessEqual(top + text_label.height(),
                                 card.height() - card.hBoxLayout.contentsMargins().bottom())
            self.assertGreaterEqual(text_label.height(), text_label.fontMetrics().lineSpacing())

    def test_all_card_types_fit_narrow_and_wide_layouts(self):
        cards = self.cards()
        self.root.show()
        saw_elision = False
        for width in (600, 900, 1200, 1600):
            with self.subTest(width=width):
                self.root.resize(width, 600)
                flush_layout(self.app, self.root)
                for card in cards:
                    self.assert_heading_fits(card)
                    saw_elision |= card.titleLabel.text() != card.titleLabel.toolTip()
        self.assertTrue(saw_elision)

    def test_larger_fonts_recalculate_heading_height(self):
        cards = self.cards()
        # Fluent's pixel-size QSS overrides setFont(); remove that rule here
        # to exercise actual font changes rather than an unchanged QSS font.
        for card in cards:
            card.setStyleSheet('')
        self.root.resize(600, 600)
        self.root.show()
        flush_layout(self.app, self.root)
        heights = [card.height() for card in cards]
        for card in cards:
            for text_label in (card.titleLabel, card.contentLabel):
                font = QFont(text_label.font())
                font.setPointSizeF(max(9, font.pointSizeF()) + 4)
                text_label.setFont(font)
            card.refresh_interface_scale()
        flush_layout(self.app, self.root)
        for card, before in zip(cards, heights):
            self.assertGreater(card.height(), before)
            self.assert_heading_fits(card)

    def test_fractional_interface_scales_keep_labels_inside_cards(self):
        cards = self.cards()
        self.root.show()
        for factor in (1.0, 1.25, 1.5, 1.75):
            with self.subTest(scale=factor):
                apply_interface_scale(self.app, FakeScreen(), '1.0')
                self.app._interface_scaler.apply(factor)
                self.root.resize(900, 700)
                flush_layout(self.app, self.root)
                for card in cards:
                    self.assert_heading_fits(card)

    def test_elision_retains_sources_after_resize_and_dynamic_text_changes(self):
        card = self.cards()[0]
        self.root.resize(600, 600)
        self.root.show()
        flush_layout(self.app, self.root)
        title = card.titleLabel.toolTip()
        for width in (900, 1600, 600, 1200):
            self.root.resize(width, 600)
            flush_layout(self.app, self.root)
            self.assertEqual(card.titleLabel.toolTip(), title)
        card.setTitle('AI分析（文本）线路')
        card.setContent('当前线路 · fixture')
        flush_layout(self.app, self.root)
        self.assertEqual(card.titleLabel.toolTip(), 'AI分析（文本）线路')
        card.titleLabel.setText('图床 API Key（上传 Token）')
        flush_layout(self.app, self.root)
        self.assertEqual(card.titleLabel.toolTip(), '图床 API Key（上传 Token）')
        self.assert_heading_fits(card)

    def test_empty_heading_gives_controls_full_width_and_can_be_restored(self):
        control = LineEdit()
        card = CustomSettingCard('', control, parent=self.root)
        self.layout.addWidget(card)
        self.root.show()
        for width in (600, 900, 1200, 1600):
            with self.subTest(width=width):
                self.root.resize(width, 600)
                flush_layout(self.app, self.root)
                self.assertTrue(card._heading.isHidden())
                self.assertEqual(card._heading.width(), 0)
                margins = card.hBoxLayout.contentsMargins()
                self.assertEqual(card._responsive_controls.width(), card.width() - margins.left() - margins.right())
                self.assertGreaterEqual(control.width(), card._responsive_controls.width() - 2)
        card.setTitle('AI分析（文本）线路')
        flush_layout(self.app, self.root)
        self.assertFalse(card._heading.isHidden())
        self.assertGreater(card._heading.width(), 0)


class EcommerceModelGridLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_model_rows_share_column_geometry_at_supported_scales(self):
        reset_interface_scaling(self.app)
        with tempfile.TemporaryDirectory() as directory, \
                patch('requests.sessions.Session.request', side_effect=AssertionError('禁止真实联网')) as network:
            config = ConfigManager(Path(directory) / 'config.json')
            page = EcommercePage(config)
            try:
                page.show()
                for scale in ('1.0', '1.25', '1.5', '1.75'):
                    apply_interface_scale(self.app, FakeScreen(), scale)
                    for width in (1000, 1400, 1820):
                        with self.subTest(scale=scale, width=width):
                            page.resize(width, 1000)
                            flush_layout(self.app, page)
                            for first, second in (
                                    (page.analysis_model_picker, page.image_model_picker),
                                    (page.analysis_line_combo, page.image_line_combo),
                                    (page.analysis_line_label, page.image_line_label),
                                    (page.analysis_status_dot, page.image_status_dot),
                                    (page.sync_analysis_models_button, page.sync_image_models_button)):
                                self.assertEqual(first.x(), second.x())
                                self.assertEqual(first.width(), second.width())
                            for row in ((page.analysis_model_picker, page.analysis_line_combo,
                                         page.analysis_line_label, page.analysis_status_dot,
                                         page.sync_analysis_models_button),
                                        (page.image_model_picker, page.image_line_combo,
                                         page.image_line_label, page.image_status_dot,
                                         page.sync_image_models_button)):
                                for left, right in zip(row, row[1:]):
                                    self.assertLess(left.geometry().right(), right.geometry().left())
                                self.assertLessEqual(row[-1].geometry().right(), row[-1].parentWidget().width())
                            for widget in (page.analysis_model_picker, page.analysis_line_combo,
                                           page.analysis_line_label, page.analysis_status_dot,
                                           page.sync_analysis_models_button, page.image_model_picker,
                                           page.image_line_combo, page.image_line_label,
                                           page.image_status_dot, page.sync_image_models_button):
                                mapped = widget.mapTo(page.body_scroll.viewport(), widget.rect().topLeft())
                                self.assertGreaterEqual(mapped.x(), 0)
                                self.assertLessEqual(mapped.x() + widget.width(), page.body_scroll.viewport().width())
                network.assert_not_called()
            finally:
                page.close()
                page.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                reset_interface_scaling(self.app)


class SettingsResultBarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        reset_interface_scaling(self.app)
        self.temp = tempfile.TemporaryDirectory()
        self.root = QWidget()
        self.root.resize(900, 800)
        self.logs = []
        config = ConfigManager(Path(self.temp.name) / 'config.json')
        self.page = SettingsPage(config, lambda *args: self.logs.append(args), self.root)
        self.page.stop_timers()
        layout = QVBoxLayout(self.root)
        layout.addWidget(self.page)
        self.root.show()
        flush_layout(self.app, self.root)

    def tearDown(self):
        self.root.close()
        self.root.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        reset_interface_scaling(self.app)
        self.temp.cleanup()

    def test_result_tones_wrap_complete_messages_inside_window(self):
        message = 'AI分析（文本）API：成功 · 文本调用成功（视频 0 / 文本 27 / 图片 0）'
        details = ('AI分析（文本）API：文本调用成功\n'
                   '原始诊断：fixture response with a long diagnostic explanation ' * 4
                   + '\n模型：claude-haiku-4-5')
        for tone, duration in (('success', 5000), ('warning', 7000), ('error', 9000)):
            with self.subTest(tone=tone):
                bar = self.page._notice(message, level=tone, tooltip=details)
                wait_until(lambda: bar.property('slideAni').state() == QAbstractAnimation.Stopped)
                self.assertIs(bar.parentWidget(), self.root)
                self.assertEqual(bar.duration, duration)
                self.assertLessEqual(bar.maximumWidth(), self.root.width() - 48)
                self.assertTrue(bar.contentLabel.wordWrap())
                self.assertFalse(bar.titleLabel.wordWrap())
                self.assertEqual(bar.contentLabel.text(), message)
                self.assertEqual(bar.contentLabel.toolTip(), details)
                self.assertEqual(bar.titleLabel.toolTip(), details)
                self.assertGreaterEqual(bar.x(), 16)
                self.assertLessEqual(bar.geometry().right(), self.root.width() - 16)
                label = bar.contentLabel
                self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()))
                self.assertEqual(self.logs[-1], (message, tone))
                bar.close()
                QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_result_bar_tracks_window_resize_while_details_stay_in_tooltip(self):
        message = '视频 API：失败 · 与中转站的 SSL 连接被中断'
        details = 'HTTPSConnectionPool(host=probe.invalid): SSLError: ' + 'model-not-found ' * 30
        bar = self.page._show_result_infobar('测试连接', message, 'error', tooltip=details)
        wait_until(lambda: bar.property('slideAni').state() == QAbstractAnimation.Stopped)
        self.root.resize(600, 800)
        flush_layout(self.app, self.root)
        self.assertLessEqual(bar.maximumWidth(), self.root.width() - 48)
        self.assertGreaterEqual(bar.x(), 16)
        self.assertLessEqual(bar.geometry().right(), self.root.width() - 16)
        self.assertEqual(bar.contentLabel.text(), message)
        self.assertEqual(bar.contentLabel.toolTip(), details)
        self.assertEqual(bar.titleLabel.toolTip(), details)

    def test_model_pool_host_occupies_the_card_without_a_heading_column(self):
        self.page._switch_page('pool', highlight=True)
        flush_layout(self.app, self.root)
        card = self.page.pool_category_card
        self.assertTrue(card._heading.isHidden())
        self.assertEqual(card._heading.width(), 0)
        self.assertGreater(card._responsive_controls.width(), card.width() * .85)
        for kind in ('video', 'chat', 'image'):
            self.page.pool_tabs.setCurrentItem(kind)
            flush_layout(self.app, self.root)
            self.assertAlmostEqual(self.page.pool_category_title.width(), self.page.pool_tabs.width(), delta=2)
            self.assertTrue(self.page.pool_category_title.alignment() & Qt.AlignHCenter)


class UiFontTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        reset_interface_scaling(self.app)
        self.original_font = QFont(self.app.font())

    def tearDown(self):
        reset_interface_scaling(self.app)
        self.app.setFont(self.original_font)

    def test_system_cjk_font_is_selected_idempotently(self):
        families, registered, source = custom_widgets.ensure_ui_font()
        font = QFont(self.app.font())
        self.assertTrue(any(name.lower() in family.lower() for family in families
                            for name in ('YaHei', 'JhengHei', 'SimHei', 'SimSun', 'Noto Sans CJK')))
        self.assertEqual(bool(source), registered)
        self.assertEqual(custom_widgets.ensure_ui_font(), (families, registered, source))
        self.assertEqual(self.app.font(), font)

    def test_application_font_family_chain_survives_scaling_and_second_ensure(self):
        custom_widgets.ensure_ui_font()
        font = QFont(self.app.font())
        families = font.families() if hasattr(font, 'families') else [font.family()]
        apply_interface_scale(self.app, FakeScreen(), '1.5')
        custom_widgets.ensure_ui_font()
        current = self.app.font()
        self.assertAlmostEqual(current.pointSizeF(), font.pointSizeF() * 1.5)
        self.assertEqual(current.families() if hasattr(current, 'families') else [current.family()], families)

    def test_registration_tries_next_font_when_first_file_fails(self):
        with patch.object(custom_widgets, '_font_loaded', False), \
                patch.object(custom_widgets, '_font_result', ([], False, '')), \
                patch.object(Path, 'is_file', return_value=True), \
                patch.object(custom_widgets, 'QFontDatabase') as database:
            database.addApplicationFont.side_effect = [-1, 3]
            database.applicationFontFamilies.return_value = ['Microsoft YaHei UI']
            families, registered, source = custom_widgets.ensure_ui_font()
        self.assertTrue(registered)
        self.assertEqual(families[0], 'Microsoft YaHei UI')
        self.assertEqual(Path(source).name, 'msyhbd.ttc')
        self.assertEqual(database.addApplicationFont.call_count, 2)

    def test_missing_files_fall_back_to_enumerated_cjk_font(self):
        with patch.object(custom_widgets, '_font_loaded', False), \
                patch.object(custom_widgets, '_font_result', ([], False, '')), \
                patch.object(Path, 'is_file', return_value=False), \
                patch.object(custom_widgets, 'QFontDatabase') as database:
            database.return_value.families.return_value = ['Segoe UI', 'Noto Sans CJK SC']
            families, registered, source = custom_widgets.ensure_ui_font()
        self.assertEqual(families[0], 'Noto Sans CJK SC')
        self.assertFalse(registered)
        self.assertEqual(source, '')
        database.addApplicationFont.assert_not_called()

    def test_missing_all_cjk_fonts_warns_and_keeps_application_font(self):
        before = QFont(self.app.font())
        with patch.object(custom_widgets, '_font_loaded', False), \
                patch.object(custom_widgets, '_font_result', ([], False, '')), \
                patch.object(Path, 'is_file', return_value=False), \
                patch.object(custom_widgets, 'QFontDatabase') as database:
            database.return_value.families.return_value = ['Segoe UI']
            with self.assertLogs(custom_widgets.__name__, level='WARNING'):
                self.assertEqual(custom_widgets.ensure_ui_font(), ([], False, ''))
        self.assertEqual(self.app.font(), before)


if __name__ == '__main__':
    unittest.main()
