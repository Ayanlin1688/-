"""扫描分发目录和便携包，阻止本地数据进入客户发布包。"""
from __future__ import annotations

import argparse
import hashlib
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
CERTIFI_PEM = '_internal/certifi/cacert.pem'
PRIVATE_DIRECTORIES = {
    '.cluster', 'logs', 'screenshots', 'screenshot', 'diagnostics',
    'diagnostic', 'artifacts', 'crash-dumps', '截图', '诊断',
}


def forbidden_reason(relative_path: str) -> str | None:
    path = PurePosixPath(relative_path.replace('\\', '/'))
    parts = tuple(part.casefold() for part in path.parts)
    if path.is_absolute() or '..' in parts or any(':' in part for part in parts):
        return '不安全的归档路径'
    if not parts:
        return None
    if PRIVATE_DIRECTORIES.intersection(parts):
        return '本地数据目录'
    name = parts[-1]
    if name.startswith(('config.json', 'models_cache.json', 'license-ledger.csv')):
        return '本地配置或许可台账'
    if '.sqlite3' in name:
        return '本地数据库或附属文件'
    suffix = path.suffix.casefold()
    if suffix in {'.key', '.pem'}:
        if path.as_posix().casefold() == CERTIFI_PEM:
            return None
        return '密钥或证书文件'
    if suffix in {'.mp4', '.mov', '.avi', '.log'}:
        return '生成视频或执行日志'
    if any(marker in name for marker in ('screenshot', 'screen_capture', 'screen-capture',
                                         'codex-clipboard', '截图')):
        return '截图或截图归档'
    if (suffix in {'.zip', '.7z', '.rar', '.tar', '.gz', '.json', '.txt', '.dmp'}
            and any(marker in name for marker in ('diagnostic', '诊断', 'crash', 'debug-report'))):
        return '诊断产物'
    return None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def check_release(bundle: Path, zip_path: Path) -> dict:
    bundle = bundle.resolve()
    zip_path = zip_path.resolve()
    if not bundle.is_dir():
        raise ValueError(f'分发目录不存在：{bundle}')
    if not zip_path.is_file():
        raise ValueError(f'便携包不存在：{zip_path}')
    hits = []
    bundle_files = set()
    zip_files = set()
    whitelist_count = 0
    for path in sorted(bundle.rglob('*')):
        relative = path.relative_to(bundle).as_posix()
        if path.is_file():
            bundle_files.add(relative)
            whitelist_count += relative.casefold() == CERTIFI_PEM
        reason = forbidden_reason(relative)
        if path.is_symlink():
            reason = '分发目录不得包含符号链接'
        if reason:
            hits.append(f'目录/{relative}：{reason}')
    with zipfile.ZipFile(zip_path) as archive:
        names = set()
        zip_count = 0
        for member in archive.infolist():
            normalized = member.filename.replace('\\', '/')
            prefix = bundle.name + '/'
            if normalized == prefix and member.is_dir():
                continue
            if not normalized.startswith(prefix):
                hits.append(f'ZIP/{member.filename}：文件不在分发根目录内')
                relative = normalized
            else:
                relative = normalized[len(prefix):]
            reason = forbidden_reason(relative)
            if stat.S_ISLNK(member.external_attr >> 16):
                reason = '便携包不得包含符号链接'
            if reason:
                hits.append(f'ZIP/{member.filename}：{reason}')
            if not member.is_dir():
                zip_count += 1
                folded = relative.casefold()
                if folded in names:
                    hits.append(f'ZIP/{member.filename}：重复文件名')
                names.add(folded)
                zip_files.add(relative)
                whitelist_count += relative.casefold() == CERTIFI_PEM
    if not bundle_files:
        hits.append('分发目录为空')
    for relative in sorted(bundle_files - zip_files):
        hits.append(f'目录/{relative}：ZIP 缺少该文件')
    for relative in sorted(zip_files - bundle_files):
        hits.append(f'ZIP/{relative}：分发目录不存在该文件')
    return {
        'bundle_count': len(bundle_files), 'zip_count': zip_count,
        'hits': hits, 'whitelist_count': whitelist_count,
        'zip_path': str(zip_path), 'sha256': sha256(zip_path),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, default=ROOT / 'packaging' / 'dist' / 'YanlinMatrix')
    parser.add_argument('--zip', dest='zip_path', type=Path, help='最终便携 ZIP 的路径')
    args = parser.parse_args(argv)
    if args.zip_path is None:
        sys.path.insert(0, str(ROOT))
        from core.version import APP_VERSION
        args.zip_path = ROOT / 'packaging' / 'dist' / f'YanlinMatrix-v{APP_VERSION}-win64-portable.zip'
    try:
        result = check_release(args.bundle, args.zip_path)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print(f'[clean] 校验失败：{error}', file=sys.stderr)
        return 1
    print(f'[clean] 文件总数：目录 {result["bundle_count"]}，ZIP {result["zip_count"]}')
    print(f'[clean] certifi 白名单：{result["whitelist_count"]} 项（目录与 ZIP 分别计数）')
    print(f'[clean] 命中清单：{len(result["hits"])} 项')
    for hit in result['hits']:
        print(f'  {hit}')
    print(f'[clean] 最终 ZIP：{result["zip_path"]}')
    print(f'[clean] SHA256：{result["sha256"]}')
    if result['hits']:
        print('[clean] 不得分发：洁净度检查未通过', file=sys.stderr)
        return 1
    print('[clean] 通过：没有发现禁止分发的本地数据文件')
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
