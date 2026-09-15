"""Unattended disk hygiene: purge stale temp files and warn before the disk fills."""
import shutil
import time
from pathlib import Path

TEMP_SUFFIXES = {'.part', '.tmp', '.download', '.crdownload', '.partial'}
MEGABYTE = 1048576
GIGABYTE = 1073741824


def _purge(root, cutoff):
    """Remove well-known partial/download leftovers older than the cutoff."""
    removed = 0
    freed = 0
    try:
        candidates = [path for path in Path(root).rglob('*') if path.suffix.lower() in TEMP_SUFFIXES]
    except OSError:
        return removed, freed
    for path in candidates:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
            if stat.st_mtime >= cutoff:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
            freed += stat.st_size
        except OSError:
            continue
    return removed, freed


def cleanup_disk(paths, log, retention_days=7, min_free_gb=2.0, now=None):
    """Delete stale temp files under media directories and report free space.

    Only well-known temp suffixes older than the retention window are removed;
    completed videos and reference images are never touched.
    """
    now = now or time.time()
    cutoff = now - max(1, int(retention_days)) * 86400
    removed = 0
    freed = 0
    dirs = [Path(root) for root in (paths.get('output'), paths.get('images')) if root and Path(root).is_dir()]
    for root in dirs:
        part_removed, part_freed = _purge(root, cutoff)
        removed += part_removed
        freed += part_freed
    if removed:
        log(f'磁盘清理：已删除 {removed} 个过期临时文件（释放 {freed / MEGABYTE:.1f}MB）', 'info')
    if dirs:
        try:
            usage = shutil.disk_usage(str(dirs[0]))
        except OSError:
            usage = None
        if usage is not None:
            free_gb = usage.free / GIGABYTE
            threshold = max(0.1, float(min_free_gb))
            if free_gb < threshold / 4:
                log(f'磁盘空间严重不足：{dirs[0]} 剩余 {free_gb:.1f}GB；请尽快清理，否则新任务可能无法写入', 'error')
            elif free_gb < threshold:
                log(f'磁盘空间预警：{dirs[0]} 剩余 {free_gb:.1f}GB（阈值 {threshold:g}GB）；已自动清理过期缓存', 'warning')
    return {'removed': removed, 'freed_bytes': freed, 'roots': [str(root) for root in dirs]}
