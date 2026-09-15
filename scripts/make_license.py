"""厂商工具：生成离线激活码。

用法：
    python -X utf8 scripts/make_license.py --customer 客户名 [--edition pro] [--expires 2027-12-31]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.licensing import make_key


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--customer', required=True, help='客户名称（写入激活码）')
    parser.add_argument('--edition', default='pro', help='版本（默认 pro）')
    parser.add_argument('--expires', default='', help='到期日 YYYY-MM-DD；留空表示永久')
    args = parser.parse_args()
    print(make_key(args.customer, args.edition, args.expires))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
