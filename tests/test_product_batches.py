"""Product-folder scanning, execution, and UI behavior."""

import copy
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from core.config_manager import DEFAULT_CONFIG, ConfigManager
from core.matcher import StoryboardMatcher
from core.task_manager import TaskManager, resolve_output_directory
from test_http_clients import LocalServer
from test_task_manager import wait_until
from ui.components.match_dialog import MatchDialog
from ui.main_window import MainWindow
from ui.pages.history_page import HistoryPage
from ui.widgets.data_source_card import DataSourceCard
from ui.widgets.task_queue_panel import TaskQueuePanel


class ProductMatcherTests(unittest.TestCase):
    def test_flat_prompts_keep_recursive_images_and_manual_external_bindings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prompts = root / 'prompts'; prompts.mkdir()
            images = root / 'images'; (images / 'nested').mkdir(parents=True)
            prompt = prompts / 'scene1.txt'; prompt.write_text('prompt', encoding='utf-8')
            image = images / 'nested' / 'scene1.jpg'; image.touch()
            paths = dict(prompts=str(prompts), images=str(images))
            self.assertEqual(StoryboardMatcher().scan_and_match(paths)[0]['images'], [str(image.resolve())])
            external = root / 'manual.jpg'; external.touch()
            matcher = StoryboardMatcher(overrides={str(prompt.resolve()): [str(external.resolve())]})
            self.assertEqual(matcher.scan_and_match(paths)[0]['images'], [str(external.resolve())])
            self.assertEqual(StoryboardMatcher(recursive=False).scan_and_match(paths)[0]['images'], [])
    @staticmethod
    def make_directory_link(link, target):
        try:
            link.symlink_to(target, target_is_directory=True)
            return True
        except OSError:
            if os.name != "nt":
                return False
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                check=False,
            )
            return result.returncode == 0

    def test_empty_prompt_path_returns_empty_list_without_scanning_cwd(self):
        matcher = StoryboardMatcher()
        self.assertEqual(matcher.scan_and_match({"prompts": "", "images": ""}), [])
        self.assertEqual(matcher.products, [])

    def test_product_folders_are_natural_and_never_cross_bind_identical_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prompts, images = root / "prompts", root / "images"
            for base in (prompts, images):
                (base / "产品10").mkdir(parents=True)
                (base / "产品2").mkdir()
            for product in ("产品2", "产品10"):
                (prompts / product / "分镜1.txt").write_text("prompt", encoding="utf-8")
                for number in (10, 2, 1):
                    (images / product / f"1({number}).jpg").touch()

            matcher = StoryboardMatcher()
            tasks = matcher.scan_and_match({"prompts": str(prompts), "images": str(images)})

            self.assertEqual([task["product"] for task in tasks], ["产品2", "产品10"])
            self.assertEqual([task["product_index"] for task in tasks], [1, 2])
            self.assertEqual([task["product_total"] for task in tasks], [2, 2])
            self.assertEqual([Path(path).name for path in tasks[0]["images"]],
                             ["1(1).jpg", "1(2).jpg", "1(10).jpg"])
            self.assertTrue(all(Path(path).parent.name == task["product"]
                                for task in tasks for path in task["images"]))
            self.assertEqual(matcher.products, ["产品2", "产品10"])
            self.assertEqual(matcher.warnings, [])

    def test_recursive_prompts_with_same_name_sort_by_natural_relative_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            product = root / "prompts" / "产品1"
            images = root / "images" / "产品1"
            for folder in ("场景10", "场景2"):
                (product / folder).mkdir(parents=True)
                (product / folder / "镜头1.txt").write_text(folder, encoding="utf-8")
            images.mkdir(parents=True)

            tasks = StoryboardMatcher().scan_and_match(
                {"prompts": str(product.parent), "images": str(images.parent)}
            )

            self.assertEqual([Path(task["prompt_path"]).parent.name for task in tasks], ["场景2", "场景10"])

    def test_missing_product_images_skip_without_fallback_and_cross_override_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prompts, images = root / "prompts", root / "images"
            (prompts / "缺图产品").mkdir(parents=True)
            (prompts / "有图产品").mkdir()
            (images / "有图产品").mkdir(parents=True)
            for name in ("分镜1.txt", "分镜2.txt"):
                (prompts / "缺图产品" / name).write_text("prompt", encoding="utf-8")
            foreign_prompt = prompts / "有图产品" / "分镜1.txt"
            foreign_prompt.write_text("prompt", encoding="utf-8")
            foreign_image = images / "有图产品" / "分镜1.jpg"
            foreign_image.touch()
            missing_prompt = str((prompts / "缺图产品" / "分镜1.txt").resolve())

            matcher = StoryboardMatcher(overrides={missing_prompt: [str(foreign_image.resolve())]})
            tasks = matcher.scan_and_match({"prompts": str(prompts), "images": str(images)})

            missing = [task for task in tasks if task["product"] == "缺图产品"]
            self.assertEqual([task["images"] for task in missing], [[], []])
            self.assertTrue(all(task["skip_reason"] for task in missing))
            self.assertEqual(sum("缺图产品" in warning for warning in matcher.warnings), 1)
            self.assertTrue(any("产品目录外" in warning for warning in matcher.warnings))
            with_images = next(task for task in tasks if task["product"] == "有图产品")
            self.assertEqual(Path(with_images["images"][0]).parent.name, "有图产品")

    def test_product_image_directory_link_cannot_redirect_to_another_product(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            product_prompt = root / "prompts" / "产品1"
            product_prompt.mkdir(parents=True)
            (product_prompt / "分镜1.txt").write_text("prompt", encoding="utf-8")
            foreign_images = root / "images" / "产品2"
            foreign_images.mkdir(parents=True)
            (foreign_images / "分镜1.jpg").touch()
            linked_product = root / "images" / "产品1"
            if not self.make_directory_link(linked_product, foreign_images):
                self.skipTest("directory links are unavailable")

            matcher = StoryboardMatcher()
            tasks = matcher.scan_and_match(
                {"prompts": str(product_prompt.parent), "images": str(foreign_images.parent)}
            )

            self.assertEqual(tasks[0]["images"], [])
            self.assertTrue(tasks[0]["skip_reason"])
            self.assertTrue(any("链接" in warning for warning in matcher.warnings))

    def test_manual_order_is_kept_inside_product_and_flat_root_stays_legacy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prompts, images = root / "prompts", root / "images"
            prompts.mkdir(); images.mkdir()
            prompt = prompts / "分镜1.txt"; prompt.write_text("prompt", encoding="utf-8")
            image_paths = []
            for name in ("1(1).jpg", "1(2).jpg", "1(3).jpg"):
                path = images / name; path.touch(); image_paths.append(str(path.resolve()))
            matcher = StoryboardMatcher(overrides={str(prompt.resolve()): image_paths[::-1]})

            tasks = matcher.scan_and_match({"prompts": str(prompts), "images": str(images)})

            self.assertEqual(tasks[0]["images"], image_paths[::-1])
            self.assertEqual(tasks[0]["product"], "")
            self.assertEqual(tasks[0]["output_subdir"], "")
            self.assertEqual(tasks[0]["product_task_index"], 1)
            self.assertEqual((tasks[0]["product_index"], tasks[0]["product_total"]), (1, 1))
            self.assertEqual(matcher.products, [])

    def test_mixed_root_prompts_form_ungrouped_batch_without_scanning_product_images(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prompts, images = root / "prompts", root / "images"
            (prompts / "产品1").mkdir(parents=True)
            (images / "产品1").mkdir(parents=True)
            (prompts / "根分镜.txt").write_text("root", encoding="utf-8")
            (prompts / "产品1" / "根分镜.txt").write_text("product", encoding="utf-8")
            (images / "产品1" / "根分镜.jpg").touch()

            tasks = StoryboardMatcher().scan_and_match({"prompts": str(prompts), "images": str(images)})

            self.assertEqual([(task["product"], task["matched"]) for task in tasks],
                             [("", False), ("产品1", True)])
            self.assertEqual([task["product_index"] for task in tasks], [1, 2])
            self.assertEqual([task["product_total"] for task in tasks], [2, 2])

    def test_output_resolution_rejects_parent_and_existing_link_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "output"; root.mkdir()
            outside = Path(temp) / "outside"; outside.mkdir()
            self.assertEqual(resolve_output_directory(root, "产品1"), str((root / "产品1").resolve()))
            with self.assertRaises(ValueError):
                resolve_output_directory(root, "..")
            link = root / "linked"
            if not self.make_directory_link(link, outside):
                self.skipTest("symlink and junction creation are unavailable")
            with self.assertRaises(ValueError):
                resolve_output_directory(root, "linked")


class ProductWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_worker_finishes_each_product_and_uses_product_local_output_numbers(self):
        with tempfile.TemporaryDirectory() as temp, LocalServer() as server:
            root = Path(temp)
            prompts, images, output = root / "prompts", root / "images", root / "output"
            for base in (prompts, images):
                (base / "产品2").mkdir(parents=True)
                (base / "产品10").mkdir()
            for product, names in (("产品2", ("分镜2", "分镜1")), ("产品10", ("分镜1",))):
                for name in names:
                    (prompts / product / f"{name}.txt").write_text("prompt", encoding="utf-8")
                    (images / product / f"{name}.png").write_bytes(b"good-image")
            config = copy.deepcopy(DEFAULT_CONFIG)
            config["paths"] = {"prompts": str(prompts), "images": str(images), "output": str(output)}
            config["api"].update(base_url=server.base, api_key="local-test", upload_url=server.base + "/upload")
            config["workspace"]["poll_interval"] = 0.02
            manager = TaskManager()
            try:
                manager.start_tasks(config)
                wait_until(lambda: not manager.is_running)
                self.assertEqual([(task["product"], task["prompt_name"]) for task in manager.tasks],
                                 [("产品2", "分镜1"), ("产品2", "分镜2"), ("产品10", "分镜1")])
                self.assertEqual([task["status"] for task in manager.tasks], ["completed"] * 3)
                self.assertEqual([Path(task["result_path"]).parent.name for task in manager.tasks],
                                 ["产品2", "产品2", "产品10"])
                self.assertEqual([Path(task["result_path"]).name for task in manager.tasks],
                                 ["001_分镜1.mp4", "002_分镜2.mp4", "001_分镜1.mp4"])
            finally:
                manager.cancel_all()
                wait_until(lambda: not manager.is_running)

    def test_missing_product_folder_is_always_skipped_before_submission(self):
        with tempfile.TemporaryDirectory() as temp, LocalServer() as server:
            root = Path(temp)
            prompt = root / "prompts" / "缺图产品"; prompt.mkdir(parents=True)
            (prompt / "分镜1.txt").write_text("prompt", encoding="utf-8")
            images = root / "images"; images.mkdir()
            config = copy.deepcopy(DEFAULT_CONFIG)
            config["paths"] = {"prompts": str(prompt.parent), "images": str(images), "output": str(root / "output")}
            config["api"].update(base_url=server.base, api_key="local-test", upload_url=server.base + "/upload")
            config["task_strategy"]["unmatched_prompt"] = "暂停任务"
            manager = TaskManager()
            try:
                manager.start_tasks(config)
                wait_until(lambda: not manager.is_running)
                self.assertEqual(manager.tasks[0]["status"], "skipped")
                self.assertTrue(manager.tasks[0]["skip_reason"])
                self.assertEqual(server.calls, [])
            finally:
                manager.cancel_all()
                wait_until(lambda: not manager.is_running)


class ProductUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = ConfigManager(Path(self.temp.name) / "config.json")
        self.manager.load_config()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def tasks():
        return [
            dict(product="产品1", prompt_name="分镜1", status="completed", product_task_index=1,
                 product_task_total=2, product_index=1, product_total=2, images=["one.jpg"], model="video-v3", task_id="one"),
            dict(product="产品1", prompt_name="分镜2", status="skipped", product_task_index=2,
                 product_task_total=2, product_index=1, product_total=2, images=[], model="video-v3", task_id=""),
            dict(product="产品2", prompt_name="分镜1", status="processing", product_task_index=1,
                 product_task_total=1, product_index=2, product_total=2, images=["two.jpg"], model="video-v3", task_id="two"),
        ]

    def test_queue_groups_title_selection_collapse_persistence_and_product_stats(self):
        panel = TaskQueuePanel()
        try:
            tasks = self.tasks()
            panel.update_tasks(tasks)
            self.assertEqual(panel.list.count(), 5)
            first_task_item = panel.task_item(0)
            self.assertEqual(panel.list.itemWidget(first_task_item).title.text(), "产品1 / 分镜1")
            panel.list.setCurrentItem(panel.list.item(0))
            panel.toggle_group("产品1")
            self.assertTrue(first_task_item.isHidden())
            panel.update_tasks(tasks)
            self.assertTrue(panel.task_item(0).isHidden())
            panel.select_task(2)
            self.assertEqual(panel.list.currentItem().data(Qt.UserRole + 1), 2)
            self.assertIn("产品2", panel.current_product_label.text())
            self.assertEqual(panel.product_progress_label.text(), "已完成产品 1/2")
            panel.update_tasks([dict(prompt_name="旧任务", product="", status="completed")])
            self.assertEqual(panel.product_progress_label.text(), "已完成产品 1/1")
            self.assertIn("未分组", panel.current_product_label.text())
        finally:
            panel.deleteLater()

    def test_data_source_history_and_match_dialog_show_products_without_mapping_regression(self):
        source = DataSourceCard(self.manager, lambda *_: None)
        tasks = self.tasks()
        source.set_matches(tasks, tasks)
        self.assertIn("已识别 2 个产品，共 3 个提示词", source.status_label.text())

        history = HistoryPage(lambda *_: None, self.manager)
        records = [dict(tasks[0], model="video-v3", created_at="", finished_at="", result_path="", task_id="")]
        history.update_history(records)
        headers = [history.table.horizontalHeaderItem(i).text() for i in range(history.table.columnCount())]
        self.assertEqual(headers[:5], ["序号", "产品", "任务名", "模型", "状态"])
        self.assertEqual(history.table.item(0, 1).text(), "产品1")
        history.filter_box.setCurrentText("已完成")
        self.assertFalse(history.table.isRowHidden(0))
        history.filter_box.setCurrentText("失败")
        self.assertTrue(history.table.isRowHidden(0))

        matches = [dict(prompt_path=str(Path(self.temp.name) / "产品1" / "分镜1.txt"),
                        prompt_name="分镜1", product="产品1", images=[])]
        dialog = MatchDialog(config_manager=self.manager, matches=matches)
        try:
            self.assertEqual(dialog.prompt_list.item(0).text(), "产品1 / 分镜1.txt")
        finally:
            dialog.close(); dialog.deleteLater(); history.deleteLater(); source.deleteLater()

    def test_workspace_product_progress_uses_terminal_task_count_and_task_row_mapping(self):
        window = MainWindow(self.manager, network_time=False)
        try:
            workspace = window.workspace_page
            tasks = self.tasks()
            workspace._tasks_updated(tasks)
            workspace._current_changed(2, tasks[2])
            self.assertEqual(workspace.product_progress_label.text(),
                             "当前：产品2 (1/1)，总进度：产品 2/2")
            self.assertEqual(workspace.product_progress.value(), 2)
            self.assertEqual(workspace.product_progress.maximum(), 3)
            self.assertEqual(workspace.queue_panel.list.currentItem().data(Qt.UserRole + 1), 2)
        finally:
            window.close(); window.deleteLater(); QTest.qWait(20)

    def test_history_redownload_after_interrupted_poll_uses_product_local_index(self):
        with LocalServer() as server:
            self.manager.config['api'].update(base_url=server.base, api_key='local-test')
            self.manager.save_config()
            window = MainWindow(self.manager, network_time=False)
            try:
                server.polls['already-submitted'] = 1
                output = Path(self.temp.name) / 'output' / '产品2'
                task = dict(local_id='restore-product', task_id='already-submitted', model='MiniMax-H3',
                            prompt_name='分镜1', product='产品2', product_task_index=1, sequence=3,
                            output_dir=str(output), status='failed', error='poll interrupted')
                from core.submission_ledger import account_scope
                task['api_scope'] = account_scope(self.manager.config)
                window.workspace_page.redownload(task)
                wait_until(lambda: not window.workspace_page.jobs.busy)
                saved = self.manager.config['history'][0]
                self.assertEqual(saved['filename'], '001_分镜1.mp4')
                self.assertTrue((output / '001_分镜1.mp4').is_file())
                self.assertEqual(server.calls, [])
            finally:
                window.close(); window.deleteLater(); QTest.qWait(20)


if __name__ == "__main__":
    unittest.main()
