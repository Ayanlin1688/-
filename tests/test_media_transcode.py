"""Upload-only reference video compression coverage."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from core import media_transcode


class _FinishedProcess:
    def __init__(self, returncode=0):
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def communicate(self):
        return b'', b''


class MediaTranscodeTests(unittest.TestCase):
    def test_ffmpeg_prefers_imageio_then_falls_back_to_path(self):
        imageio = Mock()
        imageio.get_ffmpeg_exe.return_value = r'C:\tools\imageio-ffmpeg.exe'
        with patch.dict('sys.modules', {'imageio_ffmpeg': imageio}), \
                patch.object(media_transcode.Path, 'is_file', return_value=True), \
                patch.object(media_transcode.shutil, 'which') as which:
            self.assertEqual(media_transcode.ffmpeg_binary(), r'C:\tools\imageio-ffmpeg.exe')
            which.assert_not_called()

        with patch.dict('sys.modules', {'imageio_ffmpeg': None}), \
                patch.object(media_transcode.shutil, 'which', return_value='ffmpeg.exe') as which:
            self.assertEqual(media_transcode.ffmpeg_binary(), 'ffmpeg.exe')
            which.assert_called_once_with('ffmpeg')

    def test_command_uses_h264_aac_bounded_scale_and_cleans_temp_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / '原视频.mp4'
            source.write_bytes(b'keep original bytes')
            command_seen = []

            def spawn(command, **_kwargs):
                command_seen.extend(command)
                Path(command[-1]).write_bytes(b'compressed-fixture')
                return _FinishedProcess()

            with patch.object(media_transcode, 'ffmpeg_binary', return_value='ffmpeg.exe'), \
                    patch.object(media_transcode.subprocess, 'Popen', side_effect=spawn):
                compressed, original_size, compressed_size = media_transcode.compress_reference_video(source)

            self.assertEqual(original_size, len(b'keep original bytes'))
            self.assertEqual(compressed_size, len(b'compressed-fixture'))
            self.assertEqual(source.read_bytes(), b'keep original bytes')
            self.assertTrue(Path(compressed).is_file())
            command = ' '.join(command_seen)
            for option in ('scale=', 'libx264', 'veryfast', '23', '4M', '8M', 'aac',
                           '96k', '+faststart'):
                self.assertIn(option, command)
            self.assertTrue(media_transcode.cleanup_transcoded_video(compressed))
            self.assertFalse(Path(compressed).exists())

    def test_missing_ffmpeg_and_failed_compress_are_chinese_and_clean_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.mp4'
            source.write_bytes(b'media')
            with patch.object(media_transcode, 'ffmpeg_binary', return_value=None):
                with self.assertRaisesRegex(RuntimeError, '本机无法压缩'):
                    media_transcode.compress_reference_video(source)

            output_paths = []

            def spawn(command, **_kwargs):
                output = Path(command[-1])
                output_paths.append(output)
                output.write_bytes(b'partial')
                return _FinishedProcess(returncode=1)

            with patch.object(media_transcode, 'ffmpeg_binary', return_value='ffmpeg.exe'), \
                    patch.object(media_transcode.subprocess, 'Popen', side_effect=spawn):
                with self.assertRaisesRegex(RuntimeError, '参考视频压缩失败'):
                    media_transcode.compress_reference_video(source)
            self.assertEqual(len(output_paths), 1)
            self.assertFalse(output_paths[0].exists())

    def test_cleanup_refuses_paths_outside_owned_temp_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'do-not-delete.mp4'
            source.write_bytes(b'original')
            self.assertFalse(media_transcode.cleanup_transcoded_video(source))
            self.assertTrue(source.exists())


if __name__ == '__main__':
    unittest.main()
