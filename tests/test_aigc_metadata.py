"""AIGC 旁车标注：字段齐全、不触碰视频文件本身。"""
import json
import tempfile
import unittest
from pathlib import Path

from core.aigc import AIGC_NOTICE, write_aigc_metadata


class AigcMetadataTests(unittest.TestCase):
    def test_sidecar_written_with_notice(self):
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / '001_demo.mp4'
            video.write_bytes(b'video-bytes')
            target = write_aigc_metadata(video, model='video-v3', task_id='task-1')
            self.assertEqual(target, Path(str(video) + '.aigc.json'))
            payload = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(payload['notice'], AIGC_NOTICE)
            self.assertEqual(payload['label'], 'AIGC')
            self.assertEqual(payload['model'], 'video-v3')
            self.assertEqual(payload['task_id'], 'task-1')
            self.assertEqual(video.read_bytes(), b'video-bytes')


if __name__ == '__main__':
    unittest.main()
