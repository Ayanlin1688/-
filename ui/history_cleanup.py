"""Local history cleanup with bounded output paths and recoverable file deletion."""
import ctypes
import os
from pathlib import Path
import tempfile
import time

from core.disk_guard import cleanup_disk
from core.i18n import tr


DELETABLE_STATUSES = {'submission_unknown', 'failed', 'skipped', 'cancelled', 'completed'}


def output_file(record, output_root):
    value = record.get('result_path')
    if not value:
        return None
    path = Path(value).absolute()
    roots = [Path(root).resolve() for root in (output_root, record.get('output_dir')) if root]
    if path.is_symlink() or not any(path.resolve().is_relative_to(root) for root in roots):
        raise ValueError(tr('输出文件超出记录的输出目录，未删除：') + str(path))
    if path.exists() and not path.is_file():
        raise ValueError(tr('输出路径不是文件，未删除：') + str(path))
    return path


def output_summary(records, output_root):
    files = {}
    for record in records:
        path = output_file(record, output_root)
        if path is not None and path.is_file():
            files[str(path)] = path.stat().st_size
    return {'file_count': len(files), 'size_bytes': sum(files.values())}


def recycle_file(path):
    """Recycle on local Windows volumes; never silently agree to permanent deletion."""
    if os.name != 'nt':
        raise OSError(tr('当前平台回收站不可用'))
    path = Path(path).absolute()
    drive = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(path.anchor))
    if drive != 3 or str(path).startswith('\\\\'):
        raise OSError(tr('该输出目录不支持本地回收站'))

    class FileOperation(ctypes.Structure):
        _fields_ = [('hwnd', ctypes.c_void_p), ('wFunc', ctypes.c_uint),
                    ('pFrom', ctypes.c_wchar_p), ('pTo', ctypes.c_wchar_p),
                    ('fFlags', ctypes.c_ushort), ('fAnyOperationsAborted', ctypes.c_int),
                    ('hNameMappings', ctypes.c_void_p), ('lpszProgressTitle', ctypes.c_wchar_p)]

    source = ctypes.create_unicode_buffer(str(path) + '\0\0')
    operation = FileOperation()
    operation.wFunc = 3
    operation.pFrom = ctypes.cast(source, ctypes.c_wchar_p)
    operation.fFlags = 0x0040 | 0x0010 | 0x4000 | 0x0400
    function = ctypes.windll.shell32.SHFileOperationW
    function.argtypes = [ctypes.POINTER(FileOperation)]
    function.restype = ctypes.c_int
    result = function(ctypes.byref(operation))
    if result or operation.fAnyOperationsAborted or path.exists():
        raise OSError(tr('回收站删除未完成（返回码 {code}）').format(code=result))


def remove_empty_product_folders(path, output_root):
    if not output_root:
        return
    root = Path(output_root).resolve()
    folder = Path(path).resolve().parent
    while folder != root and folder.resolve().is_relative_to(root):
        if folder.is_symlink() or getattr(folder, 'is_junction', lambda: False)():
            return
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def cleanup_local_cache(paths, log, temporary_root=None):
    removed = 0
    freed = 0
    seen = set()
    for value in (paths.get('output'), paths.get('images')):
        if not value or str(Path(value).resolve()) in seen:
            continue
        seen.add(str(Path(value).resolve()))
        report = cleanup_disk({'output': value}, log, retention_days=1)
        removed += report['removed']
        freed += report['freed_bytes']
    root = Path(temporary_root or tempfile.gettempdir())
    cutoff = time.time() - 86400
    for path in root.glob('storyboard-reference-*'):
        try:
            if path.is_symlink() or not path.is_file() or path.suffix.lower() != '.jpg':
                continue
            stat = path.stat()
            if stat.st_mtime >= cutoff:
                continue
            path.unlink()
            removed += 1
            freed += stat.st_size
        except OSError:
            continue
    return {'removed': removed, 'freed_bytes': freed}
