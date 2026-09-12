"""Real Fluent selectors and asynchronous catalog refresh with local HTTP fixtures."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import json
from pathlib import Path
import tempfile
import threading
import unittest
from PyQt5.QtWidgets import QApplication
from core.config_manager import ConfigManager
from core.model_catalog import builtin_models
from test_model_catalog import CatalogServer
from test_task_manager import wait_until


class ModelUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = ConfigManager(Path(self.temp.name) / 'config.json')

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
                self.assertEqual(card.ratio.count(), 0)
                self.assertEqual(card.resolution.count(), 0)
            finally:
                window.close()
                wait_until(lambda: not window._background_busy())
                window.deleteLater()

    def test_local_sync_refreshes_catalog_and_persists_no_runtime_config(self):
        from ui.model_catalog_controller import ModelCatalogController, runtime_config
        with CatalogServer() as server:
            server.routes['/models'] = (200, {'data': [
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
                server.routes['/models'] = (503, {'error': 'offline fixture'})
                controller.refresh()
                wait_until(lambda: not controller.jobs.busy)
                self.assertEqual(set(controller.snapshot()), {'MiniMax-H3', 'video-v3-480p'})
                self.assertTrue(any('使用离线模型缓存' in text for text, _ in logs))
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
            self.assertEqual([card.resolution.itemText(i) for i in range(card.resolution.count())], ['1080p', '2K', '4K'])
            card.model.setCurrentText('video-v2')
            self.assertEqual(card.resolution.currentText(), '720p')
            self.assertFalse(card.resolution.isEnabled())
            self.assertEqual(self.config.config['workspace']['model'], 'video-v2')
            self.assertTrue(window.settings_page.auto_detect.isChecked())
            window.settings_page.auto_detect.setChecked(False)
            self.assertFalse(self.config.config['prompt_detection']['enabled'])
        finally:
            window.close()
            wait_until(lambda: not window._background_busy())
            window.deleteLater()

    def test_late_response_from_old_credentials_never_overwrites_cache(self):
        from ui.model_catalog_controller import ModelCatalogController
        with CatalogServer() as old, CatalogServer() as new:
            entered, release = threading.Event(), threading.Event()
            def delayed():
                entered.set()
                release.wait(5)
                return {'data': [{'id': 'video-v2'}]}
            old.routes['/models'] = (200, delayed)
            new.routes['/models'] = (200, {'data': [{'id': 'video-v3'}]})
            self.config.config['api'].update(base_url=old.base, api_key='old-fixture-key')
            controller = ModelCatalogController(self.config, lambda *_: None, auto_sync=False)
            try:
                controller.refresh()
                wait_until(entered.is_set)
                self.config.config['api'].update(base_url=new.base, api_key='new-fixture-key')
                controller.credentials_changed()
                controller.refresh()
                wait_until(lambda: not controller.syncing)
                cache = self.config.path.parent/'models_cache.json'
                expected = cache.read_bytes()
                release.set()
                wait_until(lambda: not controller.jobs.busy)
                self.assertEqual(set(controller.snapshot()), {'video-v3'})
                self.assertEqual(cache.read_bytes(), expected)
                self.assertEqual(json.loads(expected)['base_url'], new.base)
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
            server.routes['/models'] = (200, {'data': [{'id': 'video-v3-480p', 'description': '新渠道说明'}]})
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
                wait_until(lambda: not window._background_busy())
                window.deleteLater()

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
                wait_until(lambda: not window._background_busy())
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
                dialog.reset_models()
                dialog._save()
                self.assertEqual(self.config.config['model_overrides'], {})
            finally:
                dialog.close()
            menu.deleteLater()
        finally:
            window.close()
            wait_until(lambda: not window._background_busy())
            window.deleteLater()
