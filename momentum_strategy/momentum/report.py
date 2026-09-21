# coding: utf-8
"""回测绩效统计与输出。"""

import math
from dataclasses import asdict, dataclass
from typing import Optional

import numpy as np
import pandas as pd

from .broker import SELL
from .engine import BacktestResult


@dataclass
class Performance:
    start_date: str
    end_date: str
    trading_days: int
    init_cash: float
    final_asset: float
    total_return: float
    annual_return: float
    max_drawdown: float
    max_drawdown_date: str
    sharpe: float
    buy_count: int
    sell_count: int
    win_rate: float
    total_fee: float


def evaluate(result: BacktestResult, trading_days_per_year: int = 244) -> Optional[Performance]:
    if not result.equity_curve:
        return None

    dates = result.dates
    values = np.asarray(result.values, dtype=float)
    account = result.account
    init_cash = account.init_cash

    total_return = values[-1] / init_cash - 1
    years = len(values) / trading_days_per_year
    if years > 0 and values[-1] > 0:
        annual_return = (values[-1] / init_cash) ** (1 / years) - 1
    else:
        annual_return = 0.0

    peak = np.maximum.accumulate(values)
    drawdown = values / peak - 1
    max_dd = float(drawdown.min())
    max_dd_date = dates[int(drawdown.argmin())]

    if len(values) > 1:
        rets = np.diff(values) / values[:-1]
        sharpe = float(rets.mean() / rets.std() * math.sqrt(trading_days_per_year)) \
            if rets.std() > 0 else 0.0
    else:
        sharpe = 0.0

    sells = [d for d in account.deals if d.direction == SELL]
    buys = [d for d in account.deals if d.direction != SELL]
    wins = [d for d in sells if d.realized_pnl > 0]
    win_rate = len(wins) / len(sells) if sells else 0.0

    return Performance(
        start_date=dates[0],
        end_date=dates[-1],
        trading_days=len(values),
        init_cash=init_cash,
        final_asset=float(values[-1]),
        total_return=float(total_return),
        annual_return=float(annual_return),
        max_drawdown=max_dd,
        max_drawdown_date=max_dd_date,
        sharpe=sharpe,
        buy_count=len(buys),
        sell_count=len(sells),
        win_rate=win_rate,
        total_fee=float(sum(d.fee for d in account.deals)),
    )


def format_report(perf: Optional[Performance]) -> str:
    if perf is None:
        return '[回测结果] 无有效交易日'

    lines = [
        '=' * 56,
        '[回测结果]',
        '=' * 56,
        f'  区间      : {perf.start_date} ~ {perf.end_date}（{perf.trading_days} 个交易日）',
        f'  初始资金  : {perf.init_cash:,.0f}',
        f'  期末资产  : {perf.final_asset:,.0f}',
        f'  总收益率  : {perf.total_return:.2%}',
        f'  年化收益  : {perf.annual_return:.2%}',
        f'  最大回撤  : {perf.max_drawdown:.2%}（{perf.max_drawdown_date}）',
        f'  夏普比率  : {perf.sharpe:.2f}',
        f'  成交笔数  : 买入 {perf.buy_count} / 卖出 {perf.sell_count}',
        f'  卖出胜率  : {perf.win_rate:.2%}',
        f'  累计费用  : {perf.total_fee:,.0f}',
    ]
    return '\n'.join(lines)


def equity_dataframe(result: BacktestResult) -> pd.DataFrame:
    df = pd.DataFrame(result.equity_curve, columns=['date', 'total_asset'])
    df['nav'] = df['total_asset'] / result.account.init_cash
    return df


def deals_dataframe(result: BacktestResult) -> pd.DataFrame:
    rows = [asdict(d) for d in result.account.deals]
    if not rows:
        return pd.DataFrame(columns=['date', 'stock', 'direction', 'price',
                                     'volume', 'fee', 'msg', 'realized_pnl'])
    df = pd.DataFrame(rows)
    df['direction'] = df['direction'].map({1: '买入', -1: '卖出'})
    return df


def save_csv(result: BacktestResult, equity_path: str = '', deals_path: str = '') -> None:
    if equity_path:
        equity_dataframe(result).to_csv(equity_path, index=False, encoding='utf-8-sig')
        print(f'[输出] 净值曲线: {equity_path}')
    if deals_path:
        deals_dataframe(result).to_csv(deals_path, index=False, encoding='utf-8-sig')
        print(f'[输出] 成交明细: {deals_path}')
