"""厂商工具：生成离线激活码。

用法：
    # 正式发码（YL2 非对称签名，推荐）：
    python -X utf8 scripts/make_license.py --customer 客户名 --private-key 私钥.pem [--edition pro] [--expires 2027-12-31]
    # 演示/兼容（YL1 共享密钥，仅测试用）：
    python -X utf8 scripts/make_license.py --customer 客户名 [--legacy]

私钥保管：仓库外单独保存（例如加密移动盘/密钥库）；绝不提交到 Git。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.licensing import make_key, make_key_asymmetric


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--customer', required=True, help='客户名称（写入激活码）')
    parser.add_argument('--edition', default='pro', help='版本（默认 pro）')
    parser.add_argument('--expires', default='', help='到期日 YYYY-MM-DD；留空表示永久')
    parser.add_argument('--private-key', type=Path, help='RSA 私钥（PKCS#8 PEM）；提供时生成 YL2 非对称签名激活码')
    parser.add_argument('--legacy', action='store_true', help='显式使用 YL1 共享密钥演示格式（不推荐）')
    args = parser.parse_args()
    if args.private_key and not args.legacy:
        try:
            key_text = args.private_key.read_bytes()
        except OSError as error:
            print(f'无法读取私钥：{error}', file=sys.stderr)
            return 2
        print(make_key_asymmetric(args.customer, args.edition, args.expires, key_text))
        return 0
    print('未提供 --private-key：输出 YL1 演示格式（共享密钥，仅测试用）。正式发码请使用 --private-key。', file=sys.stderr)
    print(make_key(args.customer, args.edition, args.expires))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
