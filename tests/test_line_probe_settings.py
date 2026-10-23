"""Settings route and upload-field probes stay offline and capability-aware."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QAbstractButton, QCheckBox

from core.config_manager import ConfigManager
from core.i18n import current_language, set_language, tr
from core.line_routing import resolve_line
from ui.main_window import MainWindow
from ui.pages.settings_page import SettingsPage
from test_task_manager import wait_until


class ProbeFormattingTests(unittest.TestCase):
    def test_multiline_details_stay_on_one_capability_line_in_both_languages(self):
        original_language = current_language()
        self.addCleanup(set_language, original_language)
        formatter = SimpleNamespace(
            _friendly_probe_detail=lambda result: result['message'],
            _dedupe_probe_lines=SettingsPage._dedupe_probe_lines,
        )
        for language in ('zh-CN', 'en-US'):
            with self.subTest(language=language):
                set_language(language)
                title = tr('视频 API')
                result = dict(ok=True, message='fixture first\nfixture first\nfixture second')
                line = SettingsPage._format_probe_line(formatter, title, result, 'video')
                self.assertEqual(line, f"{title}：{tr('成功')} · fixture first；fixture second")

    def test_duplicate_raw_details_and_capability_lines_are_deduplicated_in_both_languages(self):
        original_language = current_language()
        self.addCleanup(set_language, original_language)
        raw = 'HTTPSConnectionPool(host=probe.invalid): SSLError: handshake failed'
        for language in ('zh-CN', 'en-US'):
            with self.subTest(language=language):
                set_language(language)
                header = f"{tr('视频 API')}：{tr('失败')} · {raw}"
                self.assertEqual(SettingsPage._dedupe_probe_lines(
                    '\n'.join((header, raw, header, raw))), header)


class LineProbeSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.network = patch('requests.sessions.Session.request',
                             side_effect=AssertionError('settings probe test must remain offline'))
        self.network.start()
        self.config = ConfigManager(Path(self.temp.name) / 'config.json')
        self.config.config['api'].update(base_url='https://active.invalid/v1', api_key='fixture-active')
        self.config.config['stations'] = [dict(
            id='station-a', name='Station A', enabled=True, base_url='https://station.invalid/v1',
            api_key='fixture-station', upload_url='', upload_api_key='', capabilities=['video', 'llm'])]
        self.config.config['line_routing'].update(video='station-a', chat='active', image='station-a')
        self.config.save_config()
        self.window = MainWindow(self.config, network_time=False, model_sync=False)
        self.settings = self.window.settings_page

    def tearDown(self):
        self.window.close()
        wait_until(lambda: not self.window._background_busy())
        self.window.deleteLater()
        QTest.qWait(30)
        self.network.stop()
        self.temp.cleanup()

    def test_route_buttons_resolve_each_capability(self):
        resolutions = []
        def resolve(config, capability):
            resolutions.append(capability)
            return resolve_line(config, capability)

        result = dict(ok=True, level='success', message='fixture',
                      counts={'video': 1, 'chat': 2, 'image': 1})
        with patch('ui.pages.settings_page.resolve_line', side_effect=resolve), \
                patch('ui.pages.settings_page.probe_line', return_value=result):
            for capability in ('video', 'chat', 'image'):
                self.settings.routing_test_buttons[capability].click()
                wait_until(lambda: not self.settings.jobs.busy)
        self.assertEqual(resolutions, ['video', 'chat', 'image'])

    def test_route_probe_keeps_one_compact_line_and_full_details_in_tooltip(self):
        result = dict(
            ok=True, level='success', message='发现视频模型',
            status_code=200, counts={'video': 13, 'chat': 27, 'image': 11},
            model='claude-haiku-4-5')
        button = self.settings.routing_test_buttons['video']
        with patch('ui.pages.settings_page.probe_line', return_value=result), \
                patch.object(self.settings, '_notice') as notice:
            self.settings._test_routing_line('video', button)
            wait_until(lambda: not self.settings.jobs.busy)

        visible = notice.call_args.args[0]
        self.assertEqual(len(visible.splitlines()), 1)
        self.assertIn('视频 API：成功', visible)
        self.assertIn('视频 13 / 文本 27 / 图片 11', visible)
        self.assertNotIn('模型分类', visible)
        self.assertNotIn('不含隐藏套餐', visible)
        self.assertIn('发现视频模型', notice.call_args.kwargs['tooltip'])
        self.assertIn('claude-haiku-4-5', notice.call_args.kwargs['tooltip'])

    def test_successful_http_status_is_not_formatted_as_an_error(self):
        self.assertEqual(self.settings._friendly_probe_detail(dict(
            ok=True, status_code=200, message='文本调用成功')), '文本调用成功')

    def test_legacy_timeout_errors_are_shown_as_chinese(self):
        for raw in ('ReadTimeout: timed out', 'ConnectTimeout: connection timed out'):
            with self.subTest(raw=raw):
                message = self.settings._friendly_probe_detail(dict(ok=False, message=raw))
                self.assertEqual(message, '连接中转站超时，请检查网络或代理后重试')
                self.assertNotIn('Timeout', message)

    def test_station_probe_summary_deduplicates_details_and_hides_ssl_stack(self):
        self.settings._open_station_editor(None)
        self.settings.station_base.setText('https://station.invalid')
        self.settings.station_key.setText('fixture-station-key')
        self.settings.station_upload.setText('')
        self.settings.station_caps['video'].setChecked(True)
        self.settings.station_caps['llm'].setChecked(True)
        self.settings.station_caps['image'].setChecked(False)
        button = next(button for button in self.settings.station_editor_host.findChildren(QAbstractButton)
                      if button.text() == '测试')
        raw_error = ('HTTPSConnectionPool(host=station.invalid): SSLError: handshake failed\n'
                     'HTTPSConnectionPool(host=station.invalid): SSLError: handshake failed')
        results = {
            'video': dict(ok=False, level='error', message=raw_error,
                          counts={'video': 3, 'chat': 2, 'image': 1}),
            'llm': dict(ok=True, level='success', message='文本调用成功',
                        counts={'video': 0, 'chat': 7, 'image': 0}, model='fixture-chat-model'),
        }
        log_entries = []
        self.settings.log_callback = lambda *args: log_entries.append(args)
        with patch('ui.pages.settings_page.probe_line', side_effect=[results['video'], results['llm']]), \
                patch.object(self.settings, '_notice') as notice:
            self.settings._test_station_fields(
                self.settings.station_base, self.settings.station_key,
                self.settings.station_upload, self.settings.station_upload_key,
                self.settings.station_caps, button)
            wait_until(lambda: not self.settings.jobs.busy)

        visible = notice.call_args.args[0]
        lines = visible.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith('视频 API：失败 ·'))
        self.assertIn('视频 3 / 文本 2 / 图片 1', lines[0])
        self.assertTrue(lines[1].startswith('AI分析（文本）API：成功 ·'))
        self.assertIn('文本 7', lines[1])
        self.assertIn('fixture-chat-model', lines[1])
        self.assertNotIn('HTTPSConnectionPool', visible)
        self.assertNotIn('SSLError', visible)
        self.assertNotIn('模型分类', visible)
        self.assertNotIn('文本实际命中模型', visible)
        self.assertNotIn('不含隐藏套餐', visible)
        tooltip = notice.call_args.kwargs['tooltip']
        self.assertIn('HTTPSConnectionPool', tooltip)
        self.assertEqual(tooltip.count('HTTPSConnectionPool'), 1)
        self.assertIn('fixture-chat-model', tooltip)
        self.assertTrue(any('HTTPSConnectionPool' in entry[0] for entry in log_entries))

    def test_full_connection_probe_has_at_most_four_compact_capability_lines(self):
        ssl_stack = 'HTTPSConnectionPool(host=probe.invalid): SSLError: UNEXPECTED_EOF'
        probe_results = {
            'video': dict(ok=True, level='success', message='发现视频模型',
                          counts={'video': 13, 'chat': 27, 'image': 11}),
            'chat': dict(ok=False, level='error', message=ssl_stack, counts={'video': 0, 'chat': 1, 'image': 0}),
            'image': dict(ok=True, level='success', message='模型列表可达，未实际出图',
                          counts={'video': 0, 'chat': 0, 'image': 4}),
        }
        log_entries = []
        self.settings.log_callback = lambda *args: log_entries.append(args)
        with patch('ui.pages.settings_page.probe_line', side_effect=lambda _base, _key, cap, **_kw:
                   probe_results[cap]), \
                patch('ui.pages.settings_page.probe_upload', return_value=dict(
                    ok=True, level='warning', message='未配置上传 Token，跳过；不影响视频/文本/图片调用')), \
                patch.object(self.settings, '_notice') as notice:
            self.settings.test_connection()
            wait_until(lambda: not self.settings.jobs.busy)

        visible = notice.call_args.args[0]
        lines = visible.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith('视频 API：成功 ·'))
        self.assertIn('视频 13 / 文本 27 / 图片 11', lines[0])
        self.assertTrue(lines[1].startswith('AI分析（文本）API：失败 ·'))
        self.assertTrue(lines[2].startswith('图片 API：成功 ·'))
        self.assertTrue(lines[3].startswith('上传 API：跳过 ·'))
        self.assertNotIn('HTTPSConnectionPool', visible)
        self.assertNotIn('SSLError', visible)
        self.assertNotIn('模型分类', visible)
        self.assertNotIn('文本实际命中模型', visible)
        self.assertNotIn('不含隐藏套餐', visible)
        tooltip = notice.call_args.kwargs['tooltip']
        self.assertIn(ssl_stack, tooltip)
        self.assertIn('视频 13 / 文本 27 / 图片 11', tooltip)
        self.assertTrue(any(ssl_stack in entry[0] for entry in log_entries))

    def test_station_test_only_probes_checked_capabilities_and_keeps_llm_key(self):
        self.settings._open_station_editor(None)
        self.settings.station_name.setText('Text only')
        self.settings.station_base.setText('https://text.invalid')
        self.settings.station_key.setText('fixture-text')
        self.settings.station_caps['video'].setChecked(False)
        self.settings.station_caps['llm'].setChecked(True)
        self.settings.station_caps['image'].setChecked(False)
        button = next(button for button in self.settings.station_editor_host.findChildren(QAbstractButton)
                      if button.text() == '测试')
        result = dict(ok=True, level='success', message='fixture', counts={})
        with patch('ui.pages.settings_page.probe_line', return_value=result) as probe:
            button.click()
            wait_until(lambda: not self.settings.jobs.busy)
        probe.assert_called_once()
        self.assertEqual(probe.call_args.args[2], 'chat')
        self.assertEqual(self.settings.station_caps['llm'].text(), '文本（AI分析）')
        self.settings._save_station_editor()
        saved = next(station for station in self.config.config['stations'] if station['name'] == 'Text only')
        self.assertEqual(saved['capabilities'], ['llm'])

    def test_station_page_test_button_uses_only_checked_capabilities(self):
        self.settings._switch_page('station:station-a', highlight=True)
        _area, content = self.settings._pages['station:station-a']
        boxes = {box.text(): box for box in content.findChildren(QCheckBox)}
        boxes['视频'].setChecked(False)
        boxes['文本（AI分析）'].setChecked(True)
        boxes['生图'].setChecked(False)
        button = next(button for button in content.findChildren(QAbstractButton)
                      if button.text() == '测试')
        result = dict(ok=True, level='success', message='fixture', counts={})
        with patch('ui.pages.settings_page.probe_line', return_value=result) as probe:
            button.click()
            wait_until(lambda: not self.settings.jobs.busy)
        probe.assert_called_once()
        self.assertEqual(probe.call_args.args[2], 'chat')

    def test_upload_token_mistake_moves_to_token_field_and_invalid_url_shows_hint(self):
        with patch.object(self.settings, '_notice') as notice:
            self.settings.upload_url.setText('vup_fixture-token')
            self.assertTrue(self.settings._repair_upload_fields(self.settings.upload_url, self.settings.upload_key))
        self.assertEqual(self.settings.upload_url.text(), '')
        self.assertEqual(self.settings.upload_key.text(), 'vup_fixture-token')
        notice.assert_called_once()

        self.settings.upload_url.setText('not a URL')
        self.assertFalse(self.settings._repair_upload_fields(self.settings.upload_url, self.settings.upload_key))
        hint = self.settings._upload_url_hints[self.settings.upload_url]
        self.assertFalse(hint.isHidden())
        self.assertIn('http://', hint.text())

    def test_upload_url_hint_card_shrinks_after_url_is_corrected(self):
        url = self.settings.upload_url
        card = self.settings._upload_url_hint_cards[url]
        base_height = card.height()

        url.setText('not a URL')
        self.assertFalse(self.settings._repair_upload_fields(url, self.settings.upload_key))
        QTest.qWait(30)
        expanded_height = card.height()
        self.assertFalse(self.settings._upload_url_hints[url].isHidden())
        self.assertGreaterEqual(expanded_height, base_height)

        url.clear()
        self.assertTrue(self.settings._repair_upload_fields(url, self.settings.upload_key))
        QTest.qWait(30)
        self.assertTrue(self.settings._upload_url_hints[url].isHidden())
        self.assertLessEqual(card.height(), base_height)

    def test_upload_token_correction_handles_same_and_replaces_old_token_idempotently(self):
        url = self.settings.upload_url
        token = self.settings.upload_key
        token.setText('vup_same-fixture-token')
        url.setText('vup_same-fixture-token')
        with patch.object(self.settings, '_notice') as notice:
            self.assertTrue(self.settings._repair_upload_fields(url, token))
            self.assertEqual(url.text(), '')
            self.assertEqual(token.text(), 'vup_same-fixture-token')
            self.assertTrue(self.settings._repair_upload_fields(url, token))
            self.assertEqual(notice.call_count, 1)

        token.setText('old-upload-fixture')
        url.setText('vup_replacement-fixture-token')
        with patch.object(self.settings, '_notice') as notice:
            self.assertTrue(self.settings._repair_upload_fields(url, token))
            notice.assert_called_once()
            self.assertIn('覆盖旧值', notice.call_args.args[0])
            self.assertEqual(url.text(), '')
            self.assertEqual(token.text(), 'vup_replacement-fixture-token')
            self.assertTrue(self.settings._repair_upload_fields(url, token))
            self.assertEqual(notice.call_count, 1)

    def test_station_test_runs_with_invalid_upload_url_and_reports_each_api_result(self):
        self.settings._open_station_editor(None)
        self.settings.station_base.setText('https://station.invalid')
        self.settings.station_key.setText('fixture-station-key')
        self.settings.station_upload.setText('not a URL')
        for capability, box in self.settings.station_caps.items():
            box.setChecked(capability == 'llm')
        button = next(button for button in self.settings.station_editor_host.findChildren(QAbstractButton)
                      if button.text() == '测试')

        with patch('ui.pages.settings_page.probe_line', return_value=dict(
                ok=True, level='success', message='fixture reachable', counts={'chat': 2})) as probe, \
                patch.object(self.settings, '_notice') as notice:
            self.settings._test_station_fields(
                self.settings.station_base, self.settings.station_key,
                self.settings.station_upload, self.settings.station_upload_key,
                self.settings.station_caps, button)
            wait_until(lambda: not self.settings.jobs.busy)

        probe.assert_called_once()
        self.assertEqual(probe.call_args.args[2], 'chat')
        message = notice.call_args.args[0]
        self.assertIn('AI分析（文本）API：成功', message)
        self.assertIn('上传 URL 未配置或格式无效', message)
        self.assertTrue(message.splitlines()[-1].startswith('上传 API：跳过 ·'))
        self.assertEqual(notice.call_args.kwargs['level'], 'warning')
        self.assertTrue(button.isEnabled())

    def test_station_probe_failure_is_visible_as_error(self):
        self.settings._open_station_editor(None)
        self.settings.station_base.setText('https://station.invalid')
        self.settings.station_caps['video'].setChecked(True)
        for capability in ('llm', 'image'):
            self.settings.station_caps[capability].setChecked(False)
        button = next(button for button in self.settings.station_editor_host.findChildren(QAbstractButton)
                      if button.text() == '测试')
        with patch('ui.pages.settings_page.probe_line', return_value=dict(
                ok=False, level='error', message='fixture rejected', counts={})) as probe, \
                patch.object(self.settings, '_notice') as notice:
            self.settings._test_station_fields(
                self.settings.station_base, self.settings.station_key,
                self.settings.station_upload, self.settings.station_upload_key,
                self.settings.station_caps, button)
            wait_until(lambda: not self.settings.jobs.busy)
        probe.assert_called_once()
        self.assertIn('fixture rejected', notice.call_args.args[0])
        self.assertEqual(notice.call_args.kwargs['level'], 'error')

    def test_startup_sanitizes_current_line_and_all_station_records_once(self):
        config = ConfigManager(Path(self.temp.name) / 'dirty-config.json')
        config.config['api'].update(upload_url='vup_current-fixture', upload_api_key='old-current-fixture')
        config.config['stations'] = [
            dict(id='empty', upload_url='vup_empty-fixture', upload_api_key=''),
            dict(id='same', upload_url='vup_same-fixture', upload_api_key='vup_same-fixture'),
            dict(id='replace', upload_url='vup_new-fixture', upload_api_key='old-station-fixture'),
            dict(id='clean', upload_url='https://upload.invalid', upload_api_key='upload-fixture'),
        ]
        config.save_config()
        logs = []
        original_update = config.update
        with patch.object(config, 'update', wraps=original_update) as update:
            page = SettingsPage(config, lambda *args: logs.append(args))
            page.stop_timers()
            self.addCleanup(page.deleteLater)
            self.assertEqual(update.call_count, 2)
            self.assertEqual([call.args[0] for call in update.call_args_list], [('api',), ('stations',)])

        self.assertEqual(config.config['api']['upload_url'], '')
        self.assertEqual(config.config['api']['upload_api_key'], 'vup_current-fixture')
        stations = {row['id']: row for row in config.config['stations']}
        self.assertEqual(stations['empty']['upload_url'], '')
        self.assertEqual(stations['empty']['upload_api_key'], 'vup_empty-fixture')
        self.assertEqual(stations['same']['upload_url'], '')
        self.assertEqual(stations['same']['upload_api_key'], 'vup_same-fixture')
        self.assertEqual(stations['replace']['upload_url'], '')
        self.assertEqual(stations['replace']['upload_api_key'], 'vup_new-fixture')
        self.assertEqual(stations['clean']['upload_url'], 'https://upload.invalid')
        self.assertEqual(len(logs), 1)
        self.assertNotIn('vup_', logs[0][0])


if __name__ == '__main__':
    unittest.main()
