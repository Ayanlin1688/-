"""Real Fluent selectors and asynchronous catalog refresh with local HTTP fixtures."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QCoreApplication, QEvent, Qt
from core.config_manager import ConfigManager
from core.model_catalog import builtin_models
from test_model_catalog import CatalogServer
from test_task_manager import wait_until


class ModelUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._drain_deferred_deletes()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self._drain_deferred_deletes)
        self.config = ConfigManager(Path(self.temp.name) / 'config.json')

    @staticmethod
    def _drain_deferred_deletes():
        # Finish queued widget destruction before the next window refreshes
        # Fluent's shared theme registry. Cleanups run after deleteLater calls.
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_selector_keeps_wire_id_while_showing_model_metadata(self):
        from ui.components.model_selector import ModelComboBox
        combo = ModelComboBox()
        self.addCleanup(combo.deleteLater)
        records = builtin_models()
        records['image-only'] = dict(id='image-only', kind='image', description='Image model', resolutions=[], pricing_text='')
        records['video-v2']['description'] = 'Supported video model'
        records['video-v2']['pricing_text'] = '$0.20/second'
        combo.set_models(records, 'video-v2')
        self.assertEqual(combo.currentText(), 'video-v2')
        self.assertEqual(combo.currentData(), 'video-v2')
        self.assertIn('720p', combo.itemText(combo.currentIndex()))
        self.assertIn('$0.20/second', combo.itemText(combo.currentIndex()))
        self.assertIn('Supported video model', combo.toolTip())
        self.assertFalse(combo.items[combo.findData('image-only')].isEnabled)
        combo.setCurrentText('MiniMax-H3')
        self.assertEqual(combo.currentData(), 'MiniMax-H3')

    def test_selector_uses_only_unambiguous_provider_rename_alias(self):
        from ui.components.model_selector import ModelComboBox
        combo = ModelComboBox()
        self.addCleanup(combo.deleteLater)
        record = dict(builtin_models()['video-v2'], id='video-v2-special', aliases=['video-v2-old'])
        combo.set_models({'video-v2-special': record}, 'video-v2-old', keep_missing=True)
        self.assertEqual(combo.currentData(), 'video-v2-special')
        self.assertEqual(combo.count(), 1)
        combo.set_models({'video-v2-special': record}, 'unrelated-missing', keep_missing=True)
        self.assertEqual(combo.currentData(), 'unrelated-missing')
        self.assertFalse(combo.items[combo.currentIndex()].isEnabled)

    def test_model_pick_combo_groups_filters_and_keeps_custom_ids(self):
        from ui.components.model_selector import ModelPickComboBox

        combo = ModelPickComboBox()
        self.addCleanup(combo.deleteLater)
        records = {
            'misc-chat': dict(id='misc-chat', kind='chat', available=True),
            'qwen-max': dict(id='qwen-max', kind='chat', available=True),
            'grok-4.7': dict(id='grok-4.7', kind='chat', available=True),
            'deepseek-chat': dict(id='deepseek-chat', kind='chat', available=True),
            'gemini-3.8-flash': dict(id='gemini-3.8-flash', kind='chat', available=True),
            'claude-sonnet-4-6': dict(id='claude-sonnet-4-6', kind='chat', available=True),
            'gpt-6-sol': dict(id='gpt-6-sol', kind='chat', available=True),
            'gpt-6-sol-pack': dict(id='gpt-6-sol-pack', kind='chat', available=False),
            'gpt-image-2': dict(id='gpt-image-2', kind='image', available=True),
            'video-v3': dict(id='video-v3', kind='video', available=True),
        }
        combo.set_models(records, 'deepseek-chat', kind='chat')

        separators = [combo.itemText(index) for index in range(combo.count())
                      if str(combo.itemData(index) or '').startswith('__separator__:')]
        self.assertEqual(separators, [
            '── GPT ──', '── CLAUDE ──', '── GEMINI ──', '── DEEPSEEK ──',
            '── GROK ──', '── QWEN ──', '── 其他 ──',
        ])
        for index in range(combo.count()):
            if str(combo.itemData(index) or '').startswith('__separator__:'):
                self.assertFalse(combo.items[index].isEnabled)
        self.assertEqual(combo.findData('gpt-6-sol-pack'), -1)
        self.assertEqual(combo.findData('gpt-image-2'), -1)
        self.assertEqual(combo.findData('video-v3'), -1)
        self.assertEqual(combo.value(), 'deepseek-chat')
        from PyQt5.QtWidgets import QAction
        model_index = combo.findData('gpt-6-sol')
        menu = combo._createComboMenu()
        action = QAction(combo.itemText(model_index), menu)
        menu.addAction(action)
        self.assertEqual(action.toolTip(), 'gpt-6-sol')
        menu.deleteLater()
        combo.set_value('  user/custom-model  ')
        self.assertEqual(combo.value(), 'user/custom-model')

        combo.set_models(records, 'gpt-image-2', kind='image')
        self.assertEqual(combo.value(), 'gpt-image-2')
        self.assertEqual(combo.findData('deepseek-chat'), -1)

    def test_model_pick_combo_has_stable_size_and_elides_without_losing_value(self):
        from ui.components.model_selector import ModelPickComboBox

        combo = ModelPickComboBox()
        self.addCleanup(combo.deleteLater)
        combo.set_models({'jt-image-2.5-sunburst': dict(
            id='jt-image-2.5-sunburst', kind='image', available=True)},
            'jt-image-2.5-sunburst', kind='image')
        self.assertEqual(combo.sizeHint().width(), 260)
        self.assertEqual(combo.sizeHint().height(), 33)
        self.assertGreaterEqual(combo.minimumSizeHint().width(), 140)
        self.assertEqual(combo.minimumSizeHint().height(), 33)
        combo.show()
        combo.resize(140, 33)
        self.app.processEvents()
        self.assertEqual(combo.value(), 'jt-image-2.5-sunburst')
        self.assertEqual(combo.toolTip(), 'jt-image-2.5-sunburst')
        combo.setFocus()
        self.app.processEvents()
        self.assertEqual(combo.text(), 'jt-image-2.5-sunburst')

    def test_model_pick_signals_and_value_always_use_wire_ids(self):
        from ui.components.model_selector import ModelPickComboBox

        combo = ModelPickComboBox()
        self.addCleanup(combo.deleteLater)
        combo.set_models({
            'gpt-x': dict(id='gpt-x', kind='chat', available=True, display_name='GPT X'),
            'deepseek-v4-pro': dict(id='deepseek-v4-pro', kind='chat', available=True,
                                    display_name='DeepSeek-V4-Pro'),
        }, 'gpt-x', kind='chat')
        emitted = []
        activated = []
        combo.currentTextChanged.connect(emitted.append)
        combo.textActivated.connect(activated.append)
        combo.setCurrentIndex(combo.findData('deepseek-v4-pro'))
        self.assertEqual(combo.value(), 'deepseek-v4-pro')
        self.assertTrue(emitted)
        self.assertTrue(all(' · ' not in value for value in emitted))
        combo._onItemClicked(combo.findData('gpt-x'))
        self.assertEqual(activated[-1], 'gpt-x')
        self.assertNotIn(' · ', activated[-1])

        combo.setText('my-custom-id')
        self.assertEqual(combo.value(), 'my-custom-id')
        combo.setText('gpt-x · GPT X')
        self.assertEqual(combo.value(), 'gpt-x')

        separator = next(index for index in range(combo.count())
                         if str(combo.itemData(index) or '').startswith('__separator__:'))
        combo.setCurrentIndex(separator)
        self.assertNotIn('__separator__:', combo.value())
        combo.set_value(combo.itemData(separator))
        self.assertEqual(combo.value(), '')
        combo.setText('__separator__:OTHER')
        self.assertEqual(combo.value(), '')
        self.assertFalse(any('__separator__:' in value for value in emitted))

    def test_model_pick_menu_selection_immediately_renders_an_elided_wire_id(self):
        from PyQt5.QtCore import Qt
        from ui.components.model_selector import ModelPickComboBox

        model_id = 'custom-model-with-a-very-long-wire-identifier'
        combo = ModelPickComboBox()
        self.addCleanup(combo.deleteLater)
        combo.resize(160, 33)
        combo.set_models({
            model_id: dict(id=model_id, kind='chat', available=True,
                           display_name='Long Model Display Name'),
        }, '', kind='chat')
        combo._onItemClicked(combo.findData(model_id))
        self.assertEqual(combo.value(), model_id)
        self.assertEqual(combo.text(), combo.fontMetrics().elidedText(
            model_id, Qt.ElideRight, max(30, combo.width() - 42)))
        self.assertEqual(combo.toolTip(), model_id)

    def test_pool_selection_persists_the_id_during_synchronous_elided_rendering(self):
        from ui.pages.settings_page import SettingsPage

        model_id = 'chat-model-with-a-very-long-wire-identifier'
        records = builtin_models()
        records[model_id] = dict(id=model_id, kind='chat', available=True,
                                 display_name='Long Chat Display Name')
        self.config.config['chat_model_pool']['models'] = [
            dict(name='deepseek-chat', enabled=True, status='健康')]
        with patch('ui.pages.settings_page.catalog_snapshot', return_value=records):
            page = SettingsPage(self.config, lambda *_: None)
            self.addCleanup(page.deleteLater)
            page.stop_timers()
            _, check, combo, state = page.pool_rows['chat'][0]
            combo.resize(160, 33)
            combo.setCurrentIndex(combo.findData(model_id))
            self.assertEqual(combo.value(), model_id)
            self.assertEqual(self.config.config['chat_model_pool']['models'][0]['name'], model_id)
            page.refresh_pool_state()
            self.assertTrue(check.isEnabled())
            self.assertEqual(state.text(), '健康')

    def test_pool_category_is_centered_and_chat_image_rows_do_not_overlap(self):
        from ui.pages.settings_page import SettingsPage

        self.config.config['chat_model_pool']['models'] = [
            dict(name='deepseek-chat', enabled=True, status='健康')]
        self.config.config['image_model_pool']['models'] = [
            dict(name='gpt-image-2.5-sunburst', enabled=True, status='健康')]
        page = SettingsPage(self.config, lambda *_: None)
        self.addCleanup(page.deleteLater)
        page.stop_timers()
        page.resize(1024, 760)
        page.show()
        page._switch_page('pool', highlight=True)
        self.app.processEvents()

        self.assertTrue(page.pool_category_card.iconLabel.isHidden())
        self.assertTrue(page.pool_category_title.alignment() & Qt.AlignHCenter)
        self.assertEqual(page.pool_category_card.titleLabel.text(), '')
        for kind in ('chat', 'image'):
            row, check, combo, _state = page.pool_rows[kind][0]
            row.resize(420, 64)
            row.layout().setGeometry(row.rect())
            self.app.processEvents()
            self.assertGreaterEqual(combo.minimumSizeHint().width(), 140)
            self.assertGreaterEqual(combo.geometry().left() - check.geometry().right(), 8)

    def test_empty_catalog_disables_submission_options_without_crashing(self):
        from core.api_client import ApiClient
        from ui.main_window import MainWindow
        with CatalogServer() as server:
            window = MainWindow(self.config, network_time=False)
            try:
                window.model_catalog.catalog.api_key = 'fixture-key'
                client = ApiClient(server.base, 'fixture-key')
                try:
                    window.model_catalog.catalog.refresh(client, persist=False)
                finally:
                    client.close()
                window.model_catalog.changed.emit({})
                card = window.workspace_page.params_card
                self.assertIsNone(card.model.currentData())
                self.assertFalse(card.duration.isEnabled())
                self.assertIn('没有可用时长', card.duration.toolTip())
                self.assertEqual(card.ratio.count(), 0)
                self.assertEqual(card.resolution.count(), 0)
            finally:
                window.close()
                wait_until(lambda: not window._background_busy() and not window._close_timer.isActive())
                window.deleteLater()

    def test_local_sync_refreshes_catalog_and_persists_no_runtime_config(self):
        from ui.model_catalog_controller import ModelCatalogController, runtime_config
        with CatalogServer() as server:
            server.routes['/v1/models'] = (200, {'data': [
                {'id': 'MiniMax-H3', 'kind': 'video'},
                {'id': 'video-v3-480p', 'kind': 'video'},
            ]})
            self.config.config['api'].update(base_url=server.base, api_key='fixture-key')
            logs = []
            controller = ModelCatalogController(self.config, lambda *args: logs.append(args), auto_sync=False)
            try:
                controller.refresh()
                wait_until(lambda: not controller.jobs.busy)
                self.assertEqual(set(controller.snapshot()), {'MiniMax-H3', 'video-v3-480p'})
                self.assertTrue(controller.catalog.fetched_at)
                self.assertIn('_model_catalog', runtime_config(self.config))
                self.config.save_config()
                document = json.loads(self.config.path.read_text(encoding='utf-8'))
                self.assertNotIn('_model_catalog', document)
                server.routes['/v1/models'] = (503, {'error': 'offline fixture'})
                controller.refresh()
                wait_until(lambda: not controller.jobs.busy)
                self.assertEqual(set(controller.snapshot()), {'MiniMax-H3', 'video-v3-480p'})
                self.assertTrue(any('使用离线缓存' in text for text, _ in logs))
            finally:
                controller.shutdown()
                wait_until(lambda: not controller.jobs.busy)
                controller.deleteLater()

    def test_workspace_and_settings_share_dynamic_capabilities(self):
        from ui.main_window import MainWindow
        window = MainWindow(self.config, network_time=False)
        window.show()
        try:
            card = window.workspace_page.params_card
            card.model.setCurrentText('MiniMax-H3')
            self.assertEqual([card.resolution.itemText(i) for i in range(card.resolution.count())], ['768p', '1080p', '2K', '4K'])
            card.model.setCurrentText('video-v2')
            self.assertEqual([card.resolution.itemText(i) for i in range(card.resolution.count())], ['480p', '720p', '1080p'])
            self.assertEqual(card.resolution.currentText(), '720p')
            self.assertTrue(card.resolution.isEnabled())
            self.assertEqual(self.config.config['workspace']['model'], 'video-v2')
            self.assertTrue(window.settings_page.auto_detect.isChecked())
            window.settings_page.auto_detect.setChecked(False)
            self.assertFalse(self.config.config['prompt_detection']['enabled'])
        finally:
            window.close()
            wait_until(lambda: not window._background_busy() and not window._close_timer.isActive())
            window.deleteLater()

    def test_late_response_from_old_credentials_never_overwrites_cache(self):
        from ui.model_catalog_controller import ModelCatalogController
        with CatalogServer() as old, CatalogServer() as new:
            entered, release = threading.Event(), threading.Event()
            def delayed():
                entered.set()
                release.wait(5)
                return {'data': [{'id': 'video-v2'}]}
            old.routes['/v1/models'] = (200, delayed)
            new.routes['/v1/models'] = (200, {'data': [{'id': 'video-v3'}]})
            self.config.config['api'].update(base_url=old.base, api_key='old-fixture-key')
            controller = ModelCatalogController(self.config, lambda *_: None, auto_sync=False)
            try:
                controller.refresh()
                wait_until(entered.is_set)
                self.config.config['api'].update(base_url=new.base, api_key='new-fixture-key')
                controller.credentials_changed()
                controller.refresh()
                wait_until(lambda: not controller.syncing)
                cache = controller.catalog.cache_path
                expected = cache.read_bytes()
                release.set()
                wait_until(lambda: not controller.jobs.busy)
                self.assertEqual(set(controller.snapshot()), {'video-v3'})
                self.assertEqual(cache.read_bytes(), expected)
                self.assertEqual(json.loads(expected)['base_url'], new.base + '/v1')
            finally:
                release.set()
                controller.shutdown()
                wait_until(lambda: not controller.jobs.busy)
                controller.deleteLater()

    def test_sync_adds_new_model_and_disables_delisted_pool_entry(self):
        from ui.main_window import MainWindow
        with CatalogServer() as server:
            self.config.config['api'].update(base_url=server.base, api_key='fixture-key')
            self.config.config['model_pool']['models'] = [dict(name='video-v2', enabled=True, status='健康')]
            self.config.save_config()
            server.routes['/v1/models'] = (200, {'data': [{'id': 'video-v3-480p', 'description': '新渠道说明'}]})
            window = MainWindow(self.config, network_time=False)
            try:
                window.settings_page.sync_models_button.click()
                wait_until(lambda: not window.model_catalog.jobs.busy)
                combo = window.workspace_page.params_card.model
                self.assertEqual(combo.currentData(), 'video-v3-480p')
                self.assertEqual(combo.count(), 1)
                _, check, pool_combo, label = window.settings_page.model_rows[0]
                self.assertEqual(pool_combo.currentData(), 'video-v2')
                self.assertFalse(check.isEnabled())
                self.assertIn('已下架', label.text())
                self.assertIn('480p', combo.itemText(0))
                self.assertIn('新渠道说明', combo.toolTip())
            finally:
                window.close()
                wait_until(lambda: not window._background_busy() and not window._close_timer.isActive())
                window.deleteLater()

    def _make_catalog_controller(self, auto_sync=False):
        from ui.model_catalog_controller import ModelCatalogController
        controller = ModelCatalogController(self.config, lambda *_: None, auto_sync=auto_sync)
        self.addCleanup(controller.shutdown)
        return controller

    def test_catalog_snapshots_merge_with_video_priority_and_isolate_routes(self):
        from test_task_manager import wait_until
        with CatalogServer() as video, CatalogServer() as chat:
            video.routes['/v1/models'] = (200, {'data': [
                {'id': 'shared-chat-id', 'kind': 'chat', 'name': 'video route copy'},
                {'id': 'video-v3'},
            ]})
            chat.routes['/v1/models'] = (200, {'data': [
                {'id': 'shared-chat-id', 'kind': 'chat', 'name': 'chat route copy'},
                {'id': 'chat-only', 'kind': 'chat'},
            ]})
            self.config.config['api'].update(base_url=video.base, api_key='video-fixture-key')
            self.config.config['stations'] = [dict(
                id='chat-route', name='Chat route', enabled=True,
                base_url=chat.base, api_key='chat-fixture-key')]
            self.config.config['line_routing'].update(video='active', chat='chat-route', image='active')
            controller = self._make_catalog_controller()
            controller.refresh()
            wait_until(lambda: not controller.jobs.busy)

            merged = controller.snapshot()
            chat_only = controller.snapshot_for('chat')
            video_only = controller.snapshot_for('video')
            self.assertIn('video-v3', merged)
            self.assertIn('chat-only', merged)
            self.assertEqual(merged['shared-chat-id']['display_name'], 'video route copy')
            self.assertEqual(chat_only['shared-chat-id']['display_name'], 'chat route copy')
            self.assertEqual(set(chat_only), {'shared-chat-id', 'chat-only'})
            self.assertIn('video-v3', video_only)
            self.assertNotIn('chat-only', video_only)

    def test_catalog_refresh_keeps_successful_route_when_another_route_fails(self):
        from test_task_manager import wait_until
        with CatalogServer() as video, CatalogServer() as chat:
            video.routes['/v1/models'] = (200, {'data': [{'id': 'video-route-model'}]})
            chat.routes['/v1/models'] = (503, {'error': 'offline fixture'})
            self.config.config['api'].update(base_url=video.base, api_key='video-fixture-key')
            self.config.config['stations'] = [dict(
                id='chat-route', name='Chat route', enabled=True,
                base_url=chat.base, api_key='chat-fixture-key')]
            self.config.config['line_routing']['chat'] = 'chat-route'
            controller = self._make_catalog_controller()
            messages = []
            controller.sync_finished.connect(lambda *args: messages.append(args))
            controller.refresh()
            wait_until(lambda: not controller.jobs.busy)
            self.assertFalse(controller.syncing)
            self.assertIn('video-route-model', controller.snapshot())
            self.assertTrue(any('1 条线路失败' in row[2] for row in messages))

    def test_reload_lines_rebuilds_identity_set_and_syncs_new_route(self):
        from test_task_manager import wait_until
        with CatalogServer() as video, CatalogServer() as chat:
            video.routes['/v1/models'] = (200, {'data': [{'id': 'video-model'}]})
            chat.routes['/v1/models'] = (200, {'data': [{'id': 'chat-model', 'kind': 'chat'}]})
            self.config.config['api'].update(base_url=video.base, api_key='video-fixture-key')
            self.config.config['line_routing'].update(video='active', chat='active', image='active')
            from ui.model_catalog_controller import ModelCatalogController
            controller = ModelCatalogController(self.config, lambda *_: None, auto_sync=True)
            self.addCleanup(controller.shutdown)
            controller.start()
            wait_until(lambda: not controller.jobs.busy)
            self.assertEqual(set(controller.snapshot_for('chat')), {'video-model'})

            self.config.config['stations'] = [dict(
                id='chat-route', name='Chat route', enabled=True,
                base_url=chat.base, api_key='chat-fixture-key')]
            self.config.config['line_routing']['chat'] = 'chat-route'
            controller.reload_lines()
            wait_until(lambda: not controller.jobs.busy)
            self.assertEqual(set(controller.snapshot_for('chat')), {'chat-model'})
            self.assertIn('video-model', controller.snapshot())

    def test_catalog_refresh_during_batch_is_applied_after_completion(self):
        from core.api_client import ApiClient
        from test_pool_execution import PoolServer
        from ui.main_window import MainWindow
        root = Path(self.temp.name).resolve()
        prompts = root/'prompts'
        prompts.mkdir()
        (prompts/'1.txt').write_text('简单描述桌上的毯子。', encoding='utf-8')
        self.config.config['paths'].update(prompts=str(prompts), output=str(root/'out'))
        self.config.config['task_strategy']['unmatched_prompt'] = '仍提交文生视频'
        self.config.config['workspace'].update(model='video-v3', resolution='720p', poll_interval=3)
        with PoolServer() as server, CatalogServer() as metadata:
            self.config.config['api'].update(base_url=server.base, api_key='fixture-key')
            self.config.save_config()
            server.processing_seconds = .4
            metadata.routes['/models'] = (200, {'data': [{'id': 'video-v3-480p'}]})
            window = MainWindow(self.config, network_time=False)
            workspace = window.workspace_page
            self.config.config['workspace']['poll_interval'] = .01
            try:
                wait_until(lambda: len(workspace.queue_panel._tasks) == 1)
                with patch.object(workspace, '_choose_unmatched_policy', return_value='仍提交文生视频'):
                    workspace.start_button.click()
                    wait_until(lambda: len(server.jobs) == 1)
                client = ApiClient(metadata.base, 'fixture-key')
                try:
                    controller = window.model_catalog
                    controller.catalog.refresh(client, persist=False)
                    controller.changed.emit(controller.snapshot())
                finally:
                    client.close()
                self.assertEqual(workspace.params_card.model.currentData(), 'video-v3')
                wait_until(lambda: not workspace.task_manager.is_running)
                self.assertEqual(workspace.params_card.model.currentData(), 'video-v3-480p')
                self.assertEqual(workspace.params_card.resolution.currentText(), '480p')
                self.assertEqual(workspace.queue_panel._tasks[0]['status'], 'completed')
                self.assertEqual(workspace.task_manager.tasks[0]['model'], 'video-v3')
            finally:
                workspace.task_manager.cancel_all()
                window.close()
                wait_until(lambda: not window._background_busy() and not window._close_timer.isActive())
                window.deleteLater()

    def test_scanned_model_labels_context_override_and_dialog_reset(self):
        from ui.main_window import MainWindow
        from ui.components.match_dialog import MatchDialog
        root = Path(self.temp.name).resolve()
        prompts = root / 'prompts'
        prompts.mkdir()
        texts = ['subject_definitions: product\n[Shot 1] close view', '镜头1：展示@图1。', '自然光照亮桌上的毯子。']
        for index, text in enumerate(texts, 1):
            (prompts / f'{index}.txt').write_text(text, encoding='utf-8')
        self.config.config['paths'].update(prompts=str(prompts), output=str(root/'output'))
        self.config.save_config()
        window = MainWindow(self.config, network_time=False)
        window.show()
        workspace = window.workspace_page
        try:
            wait_until(lambda: len(workspace.queue_panel._tasks) == 3)
            self.assertEqual([task['requested_model'] for task in workspace.queue_panel._tasks], ['MiniMax-H3', 'video-v2', 'video-v3'])
            item = workspace.queue_panel.task_item(0)
            self.assertIn('[H3]', workspace.queue_panel.list.itemWidget(item).model_label.text())
            menu = workspace.queue_panel.model_menu_for(0)
            action = next(action for action in menu.actions() if action.data() == 'video-v3')
            action.trigger()
            wait_until(lambda: workspace.queue_panel._tasks[0]['model_source'] == 'manual')
            self.assertEqual(self.config.config['model_overrides'][str(prompts/'1.txt')], 'video-v3')
            dialog = MatchDialog(window, workspace.append_log, self.config, workspace.data_source.matches,
                                 workspace.data_source.automatic_matches)
            try:
                self.assertIn('MiniMax-H3', dialog.detected_model_label.text())
                dialog.model_combo.setCurrentText('video-v2')
                self.assertEqual(self.config.config['model_overrides'][str(prompts/'1.txt')], 'video-v3')
                dialog._save()
                self.assertEqual(self.config.config['model_overrides'][str(prompts/'1.txt')], 'video-v2')
                self.config.config['prompt_detection']['enabled'] = False
                dialog._show_details(0)
                self.assertFalse(dialog.model_combo.isEnabled())
                self.assertIn('设置中开启自动识别', dialog.model_combo.toolTip())
                dialog.reset_models()
                dialog._save()
                self.assertEqual(self.config.config['model_overrides'], {})
            finally:
                dialog.close()
            menu.deleteLater()
        finally:
            window.close()
            wait_until(lambda: not window._background_busy() and not window._close_timer.isActive())
            window.deleteLater()
