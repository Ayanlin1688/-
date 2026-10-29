"""Temporary, upload-only transcoding for large reference videos."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path


TARGET_REFERENCE_BYTES = 40 * 1024 * 1024
MAX_REFERENCE_BYTES = 48 * 1024 * 1024
MAX_LONG_SIDE = 1280
_TEMP_PREFIX = 'bendihua-reference-video-'


class TranscodeCancelled(Exception):
    """Raised when a page closes while an ffmpeg job is running."""


def ffmpeg_binary():
    """Return the bundled imageio-ffmpeg executable or the PATH fallback."""
    try:
        import imageio_ffmpeg
        executable = imageio_ffmpeg.get_ffmpeg_exe()
        if executable and Path(executable).is_file():
            return str(executable)
    except (ImportError, OSError, RuntimeError):
        pass
    return shutil.which('ffmpeg')


def _scale_filter():
    # Keep the long side at or below 1280 and use even dimensions for H.264.
    width = "if(gte(iw,ih),trunc(min(iw,1280)/2)*2,-2)"
    height = "if(gte(ih,iw),trunc(min(ih,1280)/2)*2,-2)"
    return f"scale='{width}':'{height}'"


def compress_reference_video(src, target_bytes=TARGET_REFERENCE_BYTES, cancel_event=None):
    """Create a temporary H.264/AAC MP4 without modifying the source file.

    Returns ``(temporary_path, original_size, compressed_size)``. The caller
    owns the returned file and should remove it after its upload finishes.
    """
    source = Path(src)
    if not source.is_file():
        raise ValueError('参考视频文件不存在，请重新选择')
    original_size = source.stat().st_size
    if original_size <= 0:
        raise ValueError('参考视频文件为空，请重新选择')
    executable = ffmpeg_binary()
    if not executable:
        raise RuntimeError('本机无法压缩，请换≤48MB视频或安装 ffmpeg')

    handle = tempfile.NamedTemporaryFile(prefix=_TEMP_PREFIX, suffix='.mp4', delete=False)
    output = Path(handle.name)
    handle.close()
    command = [
        executable, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
        '-i', str(source), '-map', '0:v:0', '-map', '0:a?', '-vf', _scale_filter(),
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
        '-maxrate', '4M', '-bufsize', '8M', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '96k', '-movflags', '+faststart', str(output),
    ]
    process = None
    try:
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   creationflags=flags)
        started = time.monotonic()
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                process.terminate()
                process.wait(timeout=5)
                raise TranscodeCancelled()
            if time.monotonic() - started > 60 * 60:
                process.terminate()
                process.wait(timeout=5)
                raise RuntimeError('参考视频压缩耗时过长，请换一个较短的视频')
            time.sleep(0.1)
        stderr = process.communicate()[1] or b''
        if process.returncode != 0:
            raise RuntimeError('参考视频压缩失败，请检查文件或安装 ffmpeg')
        if not output.is_file() or output.stat().st_size <= 0:
            raise RuntimeError('参考视频压缩失败，未生成有效文件')
        compressed_size = output.stat().st_size
        if compressed_size > int(target_bytes) or compressed_size >= MAX_REFERENCE_BYTES:
            raise RuntimeError('参考视频压缩后仍超过目标大小，请换更短的视频')
        return str(output), original_size, compressed_size
    except TranscodeCancelled:
        raise
    except Exception as error:
        if isinstance(error, (ValueError, RuntimeError)):
            raise
        raise RuntimeError('参考视频压缩失败，请检查文件或安装 ffmpeg') from None
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except Exception:
                process.kill()
        if output.exists() and not _valid_transcode_output(output, process, target_bytes):
            cleanup_transcoded_video(output)


def _valid_transcode_output(path, process, target_bytes):
    try:
        return (process is not None and process.returncode == 0 and path.is_file()
                and 0 < path.stat().st_size <= int(target_bytes)
                and path.stat().st_size < MAX_REFERENCE_BYTES)
    except (OSError, TypeError, ValueError):
        return False


def cleanup_transcoded_video(path):
    """Delete only files created by this module, tolerating already-removed paths."""
    if not path:
        return False
    candidate = Path(path)
    if not candidate.name.startswith(_TEMP_PREFIX):
        return False
    try:
        candidate.unlink(missing_ok=True)
        return True
    except OSError:
        return False
