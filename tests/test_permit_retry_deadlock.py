import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from core.config_manager import DEFAULT_CONFIG
from core.image_uploader import ImageUploader
from core.task_manager import TaskWorker
from test_http_clients import LocalServer


class PermitRetryTests(unittest.TestCase):
    def test_sibling_wait_does_not_hold_permit_needed_by_owner_retry(self):
        with tempfile.TemporaryDirectory() as directory, LocalServer() as server:
            root = Path(directory)
            prompts = root / 'prompts'; prompts.mkdir()
            images = root / 'images'; images.mkdir()
            for n in (1, 2):
                (prompts / f'{n}.txt').write_text('Identical source with different references', encoding='utf-8')
                (images / f'{n}.png').write_bytes(f'good-image-{n}'.encode())
            config = copy.deepcopy(DEFAULT_CONFIG)
            config['paths'].update(prompts=str(prompts), images=str(images), output=str(root / 'output'))
            config['api'].update(base_url=server.base, api_key='fixture', upload_url=server.base + '/upload')
            config['prompt_detection']['enabled'] = False
            config['workspace'].update(poll_interval=.01)
            config['task_strategy'].update(max_concurrency=2, auto_retry=True, retry_interval=.25, max_retries=1)
            upload = ImageUploader.upload_images
            failed = threading.Event()
            def once(uploader, paths, *args, **kwargs):
                if not failed.is_set():
                    failed.set()
                    raise RuntimeError('temporary upload outage')
                return upload(uploader, paths, *args, **kwargs)
            worker = TaskWorker(config, [])
            watchdog = threading.Timer(4, lambda: worker.control.set('cancelled', True))
            watchdog.start()
            try:
                with patch.object(ImageUploader, 'upload_images', once):
                    worker.run()
            finally:
                watchdog.cancel()
            self.assertEqual([t['status'] for t in worker.tasks], ['completed', 'completed'])
            self.assertEqual(sum(path == '/videos' for path, *_ in server.calls), 2)
