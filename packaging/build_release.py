"""一键发布构建：PyInstaller 免安装打包 → 便携 zip →（可选）Inno Setup 安装器。

用法（在仓库根目录）：
    python packaging/build_release.py              # 打包 + 便携 zip + 版本清单
    python packaging/build_release.py --no-build   # 跳过 PyInstaller（复用现有 dist）
    python packaging/build_release.py --installer  # 额外构建安装器（需已安装 Inno Setup）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.version import APP_NAME, APP_VERSION  # noqa: E402

DIST = ROOT / 'packaging' / 'dist'
BUILD = ROOT / 'packaging' / 'build'
EXE_NAME = 'YanlinMatrix'


def _write_version_info(target: Path) -> Path:
    parts = (APP_VERSION.split('.') + ['0', '0', '0', '0'])[:4]
    values = ', '.join(parts)
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({values}), prodvers=({values}), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404b0', [
        StringStruct('CompanyName', 'Yanlin'),
        StringStruct('FileDescription', '{APP_NAME}'),
        StringStruct('FileVersion', '{APP_VERSION}'),
        StringStruct('InternalName', '{EXE_NAME}'),
        StringStruct('OriginalFilename', '{EXE_NAME}.exe'),
        StringStruct('ProductName', '{APP_NAME}'),
        StringStruct('ProductVersion', '{APP_VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)"""
    target.write_text(text, encoding='utf-8')
    return target


def build_bundle() -> Path:
    import PyInstaller.__main__ as pyi
    BUILD.mkdir(parents=True, exist_ok=True)
    version_file = _write_version_info(BUILD / 'version_info.txt')
    icon = ROOT / 'assets' / 'app.ico'
    args = [
        str(ROOT / 'main.py'),
        '--noconfirm', '--clean', '--onedir', '--windowed',
        '--name', EXE_NAME,
        '--distpath', str(DIST), '--workpath', str(BUILD / 'work'),
        '--specpath', str(BUILD),
        '--collect-all', 'qfluentwidgets',
        '--version-file', str(version_file),
    ]
    if icon.is_file():
        args += ['--icon', str(icon)]
    assets = ROOT / 'assets'
    if assets.is_dir():
        args += ['--add-data', f'{assets}{os.pathsep}assets']
    print('[build] PyInstaller 打包中（首次约 1–3 分钟）…')
    pyi.run(args)
    bundle = DIST / EXE_NAME
    if not (bundle / f'{EXE_NAME}.exe').is_file():
        raise SystemExit('构建失败：未找到 ' + str(bundle / f'{EXE_NAME}.exe'))
    return bundle


def make_zip(bundle: Path) -> Path:
    target = DIST / f'{EXE_NAME}-v{APP_VERSION}-win64-portable.zip'
    if target.exists():
        target.unlink()
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob('*')):
            if path.is_file():
                archive.write(path, path.relative_to(bundle.parent))
    return target


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def make_manifest(zip_path: Path) -> Path:
    manifest = {
        'app': EXE_NAME,
        'name': APP_NAME,
        'version': APP_VERSION,
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'assets': [{
            'file': zip_path.name,
            'size': zip_path.stat().st_size,
            'sha256': sha256(zip_path),
            'download_url': '',
        }],
    }
    target = DIST / 'update-manifest.json'
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return target


def run_installer() -> None:
    candidates = [
        Path(r'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'),
        Path(r'C:\Program Files\Inno Setup 6\ISCC.exe'),
    ]
    iscc = next((path for path in candidates if path.is_file()), None)
    if iscc is None:
        print('[installer] 未找到 Inno Setup（ISCC.exe）；安装后重跑 --installer 即可构建安装器。')
        return
    print('[installer] Inno Setup 构建安装器中…')
    subprocess.check_call([str(iscc), f'/DAppVersion={APP_VERSION}', str(ROOT / 'packaging' / 'installer.iss')])


def maybe_sign(paths) -> None:
    """可选代码签名：配置环境变量 YANLIN_SIGN_COMMAND（含 {file} 占位）后自动签名。

    示例（signtool）：
      YANLIN_SIGN_COMMAND=signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /f cert.pfx {file}
    """
    template = os.environ.get('YANLIN_SIGN_COMMAND', '').strip()
    if not template:
        print('[sign] 未配置 YANLIN_SIGN_COMMAND，跳过代码签名（正式发布前请配置签名证书）')
        return
    for path in paths:
        path = Path(path)
        if not path.exists():
            continue
        subprocess.check_call(template.replace('{file}', str(path)), shell=True)
        print(f'[sign] 已签名：{path}')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-build', action='store_true', help='跳过 PyInstaller，直接复用现有 dist 产物')
    parser.add_argument('--installer', action='store_true', help='构建 Inno Setup 安装器（需本机已安装）')
    args = parser.parse_args()
    DIST.mkdir(parents=True, exist_ok=True)
    bundle = DIST / EXE_NAME
    if not args.no_build:
        bundle = build_bundle()
    if not bundle.is_dir():
        raise SystemExit('没有可用产物；先执行完整构建')
    zip_path = make_zip(bundle)
    manifest = make_manifest(zip_path)
    print(f'[ok] 便携包：{zip_path}')
    print(f'[ok] 版本清单：{manifest}')
    maybe_sign([bundle / f'{EXE_NAME}.exe', zip_path])
    if args.installer:
        run_installer()
        maybe_sign([DIST / f'YanlinMatrix-Setup-{APP_VERSION}.exe'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
