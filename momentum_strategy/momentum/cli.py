# coding: utf-8
"""命令行入口。"""

import argparse
import logging
import sys
from datetime import datetime

from .config import (CONCEPT_SECTORS_DEFAULT, AccountConfig, BacktestConfig,
                     StrategyConfig)
from .engine import BacktestEngine
from .report import evaluate, format_report, save_csv


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format='%(message)s',
        stream=sys.stdout,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='momentum', description='动量择时策略回测')
    p.add_argument('--start', default='20240101', help='回测开始日期 YYYYMMDD')
    p.add_argument('--end', default=datetime.now().strftime('%Y%m%d'), help='回测结束日期 YYYYMMDD')
    p.add_argument('--cash', type=float, default=200000.0, help='初始资金')

    p.add_argument('--source', choices=['xtdata', 'csv'], default='xtdata', help='数据源')
    p.add_argument('--data-dir', default='', help='csv 数据源目录（--source csv 时必填）')
    p.add_argument('--no-cache', action='store_true', help='xtdata 数据源关闭内存缓存')
    p.add_argument('--download', action='store_true', help='回测前先补下载本地日线')

    p.add_argument('--sectors', default='', help='板块名，逗号分隔，默认 ' + ','.join(CONCEPT_SECTORS_DEFAULT))
    p.add_argument('--lookback', type=int, default=StrategyConfig.lookback_days, help='动量回看天数')
    p.add_argument('--stop-loss', type=float, default=StrategyConfig.stop_loss_ratio, help='止损线，如 -0.15')
    p.add_argument('--decline-days', type=int, default=StrategyConfig.decline_days_to_sell,
                   help='动量分数连续下降几天清仓')
    p.add_argument('--min-cap', type=float, default=StrategyConfig.min_market_cap, help='市值下限')
    p.add_argument('--max-cap', type=float, default=StrategyConfig.max_market_cap, help='市值上限')
    p.add_argument('--no-rsrs', action='store_true', help='跳过 RSRS 计算（默认只打印不参与决策）')

    p.add_argument('--equity-csv', default='', help='净值曲线输出路径')
    p.add_argument('--deals-csv', default='', help='成交明细输出路径')
    p.add_argument('-q', '--quiet', action='store_true', help='只输出最终统计')
    return p


def make_source(args):
    if args.source == 'csv':
        from .datasource import CsvDataSource
        if not args.data_dir:
            raise SystemExit('--source csv 需要同时指定 --data-dir')
        return CsvDataSource(args.data_dir)

    from .datasource import XtDataSource
    return XtDataSource(use_cache=not args.no_cache)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(not args.quiet)

    sectors = tuple(s.strip() for s in args.sectors.split(',') if s.strip()) \
        or CONCEPT_SECTORS_DEFAULT

    config = BacktestConfig(
        start_date=args.start,
        end_date=args.end,
        strategy=StrategyConfig(
            concept_sectors=sectors,
            lookback_days=args.lookback,
            stop_loss_ratio=args.stop_loss,
            decline_days_to_sell=args.decline_days,
            min_market_cap=args.min_cap,
            max_market_cap=args.max_cap,
            rsrs_enabled=not args.no_rsrs,
        ),
        account=AccountConfig(init_cash=args.cash),
    )

    engine = BacktestEngine(make_source(args), config)
    result = engine.run(download=args.download)

    perf = evaluate(result, config.strategy.trading_days_per_year)
    print(format_report(perf))
    save_csv(result, args.equity_csv, args.deals_csv)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
