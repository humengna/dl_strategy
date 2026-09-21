# coding: utf-8
"""
生成样例行情数据，便于在没有 QMT 的机器上试跑：

    python tools/make_sample_data.py --out data/sample --days 700
    python run_backtest.py --source csv --data-dir data/sample --start 20240102 --end 20240331
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from momentum.sample_data import write_sample_dir  # noqa: E402


def main():
    p = argparse.ArgumentParser(description='生成样例行情数据')
    p.add_argument('--out', default='data/sample', help='输出目录')
    p.add_argument('--days', type=int, default=700, help='生成多少个交易日')
    p.add_argument('--start', default='20220104', help='起始日期')
    p.add_argument('--seed', type=int, default=7, help='随机种子')
    args = p.parse_args()

    path = write_sample_dir(args.out, days=args.days, start=args.start, seed=args.seed)
    print(f'样例数据已生成: {os.path.abspath(path)}')


if __name__ == '__main__':
    main()
