# coding: utf-8
"""命令行入口。"""

import argparse
import logging
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime

from .config import (CONCEPT_SECTORS_DEFAULT, AccountConfig, BacktestConfig,
                     StrategyConfig)
from .engine import BacktestEngine
from .report import evaluate, format_report, save_csv, save_results
from .vector_engine import VectorBacktestEngine


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
    p.add_argument('--check-data', action='store_true',
                   help='只做 xtdata 数据自检并退出（配合 --download 会试下载一只标的）')

    p.add_argument('--sectors', default='', help='板块名，逗号分隔，默认 ' + ','.join(CONCEPT_SECTORS_DEFAULT))
    p.add_argument('--lookback', type=int, default=StrategyConfig.lookback_days, help='动量回看天数')
    p.add_argument('--stop-loss', type=float, default=StrategyConfig.stop_loss_ratio, help='止损线，如 -0.15')
    p.add_argument('--decline-days', type=int, default=StrategyConfig.decline_days_to_sell,
                   help='动量分数连续下降几天清仓')
    p.add_argument('--min-cap', type=float, default=StrategyConfig.min_market_cap, help='市值下限')
    p.add_argument('--max-cap', type=float, default=StrategyConfig.max_market_cap, help='市值上限')
    p.add_argument('--no-rsrs', action='store_true', help='跳过 RSRS 计算（默认只打印不参与决策）')

    p.add_argument('--commission', type=float, default=AccountConfig.commission_rate,
                   help='佣金费率，双边，默认 1e-4（万 1）')
    p.add_argument('--min-commission', type=float, default=AccountConfig.min_commission,
                   help='单笔最低佣金，默认 5 元')
    p.add_argument('--transfer-fee', type=float, default=AccountConfig.transfer_fee_rate,
                   help='过户费费率，双边，默认 1e-5（千分之 0.01）')
    p.add_argument('--stamp-tax', type=float, default=AccountConfig.stamp_tax_rate,
                   help='印花税费率，仅卖出，默认 5e-4（千分之 0.5）')

    p.add_argument('--engine', choices=['fast', 'loop'], default='fast',
                   help='fast=向量化引擎（默认）；loop=逐日引擎，慢很多，用于交叉验证')
    p.add_argument('--out-dir', default='',
                   help='回测结果输出目录，默认 results/bt_<起止日期>_<时间戳>')
    p.add_argument('--no-save', action='store_true', help='不保存回测结果')
    p.add_argument('--equity-csv', default='', help='额外单独输出净值曲线到指定路径')
    p.add_argument('--deals-csv', default='', help='额外单独输出成交明细到指定路径')
    p.add_argument('-q', '--quiet', action='store_true', help='只输出最终统计')
    return p


def build_source(args):
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

    if args.check_data:
        from .diagnostics import check_xtdata
        ok = check_xtdata(start_date=args.start, end_date=args.end,
                          try_download=args.download)
        return 0 if ok else 1

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
        account=AccountConfig(
            init_cash=args.cash,
            commission_rate=args.commission,
            min_commission=args.min_commission,
            transfer_fee_rate=args.transfer_fee,
            stamp_tax_rate=args.stamp_tax,
        ),
    )

    engine_cls = VectorBacktestEngine if args.engine == 'fast' else BacktestEngine
    engine = engine_cls(build_source(args), config)

    started = time.time()
    result = engine.run(download=args.download, show_progress=args.quiet)
    elapsed = time.time() - started

    perf = evaluate(result, config.strategy.trading_days_per_year)
    print(format_report(perf))
    print(f'  回测耗时  : {elapsed:.1f} 秒（{args.engine} 引擎）')

    if not args.no_save:
        out_dir = args.out_dir or os.path.join(
            'results', f'bt_{args.start}_{args.end}_{datetime.now().strftime("%H%M%S")}')
        save_results(
            result, out_dir, perf,
            name_lookup=engine.source.get_stock_name,
            extra={
                'start_date': args.start,
                'end_date': args.end,
                'init_cash': args.cash,
                'engine': args.engine,
                'elapsed_seconds': round(elapsed, 2),
                'sectors': list(sectors),
                'strategy': asdict(config.strategy),
                'account': asdict(config.account),
            },
        )

    save_csv(result, args.equity_csv, args.deals_csv)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
