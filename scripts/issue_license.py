"""对外签发并验证 YL2 离线激活码。

私钥只在本地读取，不打印私钥内容；激活码台账位于被 Git 忽略的
``.cluster/licensing`` 目录。
"""
from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import os
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PRIVATE_KEY = ROOT / '.cluster' / 'licensing' / 'yanlin-license-private-2026-10.pem'
LEDGER_PATH = ROOT / '.cluster' / 'licensing' / 'license-ledger.csv'
LEDGER_FIELDS = (
    'issued_at', 'customer', 'edition', 'expires', 'code', 'operator', 'pubkey_sha256')

sys.path.insert(0, str(ROOT))

from core.licensing import _VERIFY_PUBLIC_PEM, make_key_asymmetric, parse_key


def _configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is not None:
            reconfigure(encoding='utf-8')


def _repo_relative(path: Path) -> str | None:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return None


def _check_git_safety(path: Path, label: str) -> None:
    """拒绝已跟踪或未被忽略的仓库内敏感文件。"""
    relative = _repo_relative(path)
    if relative is None:
        return  # 仓库外的私钥不可能被本仓库跟踪。
    try:
        ignored = subprocess.run(
            ['git', 'check-ignore', '-q', '--no-index', '--', relative],
            cwd=ROOT, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tracked = subprocess.run(
            ['git', 'ls-files', '--error-unmatch', '--', relative],
            cwd=ROOT, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        raise RuntimeError(f'无法执行 Git 安全检查：{error}') from error
    if ignored.returncode != 0:
        raise RuntimeError(f'安全检查失败：{label}未被 .gitignore 忽略：{relative}')
    if tracked.returncode == 0:
        raise RuntimeError(f'安全检查失败：{label}已被 Git 跟踪：{relative}')


def _resolve_private_key(value: str | None) -> Path:
    raw = value or os.environ.get('YANLIN_LICENSE_KEY') or str(DEFAULT_PRIVATE_KEY)
    return Path(os.path.expandvars(raw)).expanduser()


def _customer(args: argparse.Namespace) -> str:
    if args.customer is not None:
        value = args.customer
    else:
        try:
            value = args.customer_file.read_text(encoding='utf-8')
        except (OSError, UnicodeError) as error:
            raise ValueError(f'无法读取客户名文件：{error}') from error
    value = value.strip()
    if not value:
        raise ValueError('客户名不能为空')
    if '\r' in value or '\n' in value:
        raise ValueError('客户名不能包含换行')
    return value


def _expires(value: str) -> str:
    value = (value or '').strip()
    if not value:
        return ''
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('到期日必须为 YYYY-MM-DD，留空表示永久')
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError('到期日不是有效日期') from error
    return value


def _public_key_der(public_key) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)


def _load_signing_key(path: Path):
    from cryptography.hazmat.primitives import serialization

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ValueError(f'无法读取私钥文件：{path}（{error}）') from error
    try:
        private_key = serialization.load_pem_private_key(raw, password=None)
    except (TypeError, ValueError, OSError) as error:
        raise ValueError(f'私钥文件无法解析：{path}') from error
    if getattr(private_key, 'key_size', None) != 3072:
        raise ValueError('私钥必须是 3072 位 RSA')
    return private_key, raw


def _embedded_public_fingerprint() -> tuple[bytes, str]:
    from cryptography.hazmat.primitives import serialization

    public_key = serialization.load_pem_public_key(_VERIFY_PUBLIC_PEM.encode('utf-8'))
    der = _public_key_der(public_key)
    return der, hashlib.sha256(der).hexdigest()


def _assert_key_pair(private_key, embedded_der: bytes) -> None:
    derived_der = _public_key_der(private_key.public_key())
    if derived_der != embedded_der:
        raise ValueError('私钥与 core/licensing.py 当前内嵌公钥不匹配')


def _append_ledger(customer: str, edition: str, expires: str, code: str, fingerprint: str) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_file = not LEDGER_PATH.exists() or LEDGER_PATH.stat().st_size == 0
    row = {
        'issued_at': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
        'customer': customer,
        'edition': edition,
        'expires': expires,
        'code': code,
        'operator': getpass.getuser(),
        'pubkey_sha256': fingerprint,
    }
    try:
        with LEDGER_PATH.open('a', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=LEDGER_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)
    except OSError as error:
        raise RuntimeError(f'无法写入发码台账：{LEDGER_PATH}（{error}）') from error


def _verify(code: str) -> int:
    try:
        parsed = parse_key(code)
    except ValueError as error:
        print('customer=<unknown>')
        print('edition=<unknown>')
        print('expires=<unknown>')
        print('valid=否')
        print('reason=' + str(error))
        return 1
    print('customer=' + parsed['customer'])
    print('edition=' + parsed['edition'])
    print('expires=' + (parsed['expires'] or '<permanent>'))
    print('valid=是')
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='签发或离线验证 YL2 激活码')
    parser.add_argument('--customer', help='客户名称')
    parser.add_argument('--customer-file', type=Path, help='UTF-8 客户名文件')
    parser.add_argument('--edition', default='pro', help='版本，默认 pro')
    parser.add_argument('--expires', default='', help='到期日 YYYY-MM-DD；留空表示永久')
    parser.add_argument('--private-key', type=str, help='RSA 私钥路径；默认读取环境变量或本地私钥')
    parser.add_argument('--verify', metavar='YL2_CODE', help='离线验证一个 YL2 激活码')
    return parser


def main() -> int:
    _configure_output()
    parser = _parser()
    args = parser.parse_args()
    if args.customer is not None and args.customer_file is not None:
        parser.error('--customer 与 --customer-file 只能二选一')
    if args.verify is None and args.customer is None and args.customer_file is None:
        parser.error('签发时必须提供 --customer 或 --customer-file')
    if args.verify is not None and (args.customer is not None or args.customer_file is not None):
        parser.error('--verify 不能与客户名参数同时使用')

    private_path = _resolve_private_key(args.private_key)
    try:
        _check_git_safety(private_path, '私钥文件')
        _check_git_safety(LEDGER_PATH, '发码台账')
        if args.verify is not None:
            return _verify(args.verify)

        customer = _customer(args)
        edition = args.edition.strip()
        if not edition or '\r' in edition or '\n' in edition:
            raise ValueError('版本不能为空或包含换行')
        expires = _expires(args.expires)
        private_key, private_pem = _load_signing_key(private_path)
        embedded_der, fingerprint = _embedded_public_fingerprint()
        _assert_key_pair(private_key, embedded_der)
        code = make_key_asymmetric(customer, edition, expires, private_pem)
        parse_key(code)
        _append_ledger(customer, edition, expires, code, fingerprint)
        print(code)
        return 0
    except (RuntimeError, ValueError) as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
