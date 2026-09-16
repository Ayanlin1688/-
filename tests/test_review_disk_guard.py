"""Cleanup must respect material directories and Windows directory junctions."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.config_manager import DEFAULT_CONFIG
from core.disk_guard import cleanup_disk
from core.task_manager import TaskWorker


class ReviewDiskGuardTests(unittest.TestCase):
    def stale_file(self, path):
        path.write_bytes(b'user-owned material')
        old = time.time() - 10 * 86400
        os.utime(path, (old, old))
        return path

    def test_reference_directory_is_not_a_download_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / 'images'; images.mkdir()
            output = root / 'output'; output.mkdir()
            reference = self.stale_file(images / 'editor-project.tmp')
            partial = self.stale_file(output / 'download.part')
            cleanup_disk({'images': str(images), 'output': str(output)}, lambda *_: None)
            self.assertTrue(reference.exists())
            self.assertFalse(partial.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows junction regression')
    def test_output_junction_cannot_delete_outside_selected_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'output'; output.mkdir()
            external = root / 'external'; external.mkdir()
            reference = self.stale_file(external / 'keep.tmp')
            junction = output / 'linked'
            subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(junction), str(external)],
                           check=True, capture_output=True)
            try:
                cleanup_disk({'output': str(output)}, lambda *_: None)
                self.assertTrue(reference.exists())
            finally:
                junction.rmdir()

    def test_zero_retention_disables_purge_but_checks_free_space(self):
        with tempfile.TemporaryDirectory() as directory:
            partial = self.stale_file(Path(directory) / 'keep.part')
            with patch('core.disk_guard.shutil.disk_usage', return_value=SimpleNamespace(free=10)) as usage:
                cleanup_disk({'output': directory}, lambda *_: None, retention_days=0)
            self.assertTrue(partial.exists())
            usage.assert_called_once()

    def test_worker_preserves_zero_retention_setting(self):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config['task_strategy']['disk_cleanup_days'] = 0
        with patch('core.task_manager.cleanup_disk') as cleanup:
            TaskWorker(config, [])._cleanup_disk()
        self.assertEqual(cleanup.call_args.kwargs['retention_days'], 0)
