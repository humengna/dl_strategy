# coding: utf-8
"""回测绩效统计与输出。"""

import json
import math
import os
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable, Dict, Optional

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
    """
    净值曲线。有每日快照时一并带上当日目标股、信号和收盘持仓，
    没有快照（例如只手工拼了 equity_curve）时退化为最简三列。
    """
    if not result.daily:
        df = pd.DataFrame(result.equity_curve, columns=['date', 'total_asset'])
        df['nav'] = df['total_asset'] / result.account.init_cash
        return df

    rows = [asdict(d) for d in result.daily]
    df = pd.DataFrame(rows)
    df['nav'] = df['total_asset'] / result.account.init_cash
    df['daily_return'] = df['total_asset'].pct_change().fillna(0.0)
    df['drawdown'] = df['total_asset'] / df['total_asset'].cummax() - 1

    df = df[['date', 'total_asset', 'nav', 'daily_return', 'drawdown',
             'cash', 'market_value', 'stock', 'volume', 'cost', 'price',
             'target', 'signal']]
    return _round(df, {'total_asset': 2, 'nav': 6, 'daily_return': 6, 'drawdown': 6,
                       'cash': 2, 'market_value': 2, 'cost': 4, 'price': 4})


def deals_dataframe(result: BacktestResult,
                    name_lookup: Optional[Callable[[str], str]] = None) -> pd.DataFrame:
    columns = ['date', 'stock', 'name', 'direction', 'price', 'volume',
               'amount', 'fee', 'realized_pnl', 'msg']
    rows = [asdict(d) for d in result.account.deals]
    if not rows:
        return pd.DataFrame(columns=columns)

    df = pd.DataFrame(rows)
    df['name'] = [name_lookup(s) if name_lookup else '' for s in df['stock']]
    df['amount'] = df['price'] * df['volume']
    df['direction'] = df['direction'].map({1: '买入', -1: '卖出'})
    return _round(df[columns], {'price': 4, 'amount': 2, 'fee': 2, 'realized_pnl': 2})


def trades_dataframe(result: BacktestResult,
                     name_lookup: Optional[Callable[[str], str]] = None) -> pd.DataFrame:
    """
    按先进先出把买卖配成一笔笔完整交易（开仓 -> 平仓），费用按成交量摊分。
    尚未平仓的持仓不计入。
    """
    columns = ['stock', 'name', 'buy_date', 'buy_price', 'sell_date', 'sell_price',
               'volume', 'hold_days', 'fee', 'pnl', 'return_pct', 'sell_reason']
    lots: Dict[str, deque] = {}
    rows = []

    for deal in result.account.deals:
        if deal.direction != SELL:
            lots.setdefault(deal.stock, deque()).append(
                {'date': deal.date, 'price': deal.price,
                 'left': deal.volume, 'fee_per_share': deal.fee / max(deal.volume, 1)})
            continue

        remaining = deal.volume
        sell_fee_per_share = deal.fee / max(deal.volume, 1)
        queue = lots.get(deal.stock)
        while remaining > 0 and queue:
            lot = queue[0]
            matched = min(remaining, lot['left'])
            lot['left'] -= matched
            remaining -= matched
            if lot['left'] <= 0:
                queue.popleft()

            fee = (lot['fee_per_share'] + sell_fee_per_share) * matched
            pnl = (deal.price - lot['price']) * matched - fee
            cost = lot['price'] * matched
            rows.append({
                'stock': deal.stock,
                'name': name_lookup(deal.stock) if name_lookup else '',
                'buy_date': lot['date'],
                'buy_price': lot['price'],
                'sell_date': deal.date,
                'sell_price': deal.price,
                'volume': matched,
                'hold_days': _days_between(lot['date'], deal.date),
                'fee': fee,
                'pnl': pnl,
                'return_pct': pnl / cost if cost > 0 else 0.0,
                'sell_reason': deal.msg,
            })

    return _round(pd.DataFrame(rows, columns=columns),
                  {'buy_price': 4, 'sell_price': 4, 'fee': 2, 'pnl': 2, 'return_pct': 6})


def _round(df: pd.DataFrame, digits: Dict[str, int]) -> pd.DataFrame:
    """按列四舍五入，便于直接看 csv"""
    out = df.copy()
    for col, nd in digits.items():
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors='coerce').round(nd)
    return out


def _days_between(start: str, end: str) -> int:
    try:
        return (datetime.strptime(end, '%Y%m%d') - datetime.strptime(start, '%Y%m%d')).days
    except (ValueError, TypeError):
        return 0


def save_results(result: BacktestResult, out_dir: str,
                 perf: Optional[Performance] = None,
                 name_lookup: Optional[Callable[[str], str]] = None,
                 extra: Optional[dict] = None) -> Dict[str, str]:
    """
    把回测结果写到一个目录：
      equity.csv   逐日净值 + 当日目标股 / 信号 / 持仓
      deals.csv    每一笔成交
      trades.csv   先进先出配对后的完整交易（含持有天数与收益率）
      summary.json 绩效指标（机器读）
      summary.txt  绩效指标（人读）
    """
    os.makedirs(out_dir, exist_ok=True)
    paths = {}

    equity_path = os.path.join(out_dir, 'equity.csv')
    equity_dataframe(result).to_csv(equity_path, index=False, encoding='utf-8-sig')
    paths['equity'] = equity_path

    deals_path = os.path.join(out_dir, 'deals.csv')
    deals_dataframe(result, name_lookup).to_csv(deals_path, index=False, encoding='utf-8-sig')
    paths['deals'] = deals_path

    trades_path = os.path.join(out_dir, 'trades.csv')
    trades_dataframe(result, name_lookup).to_csv(trades_path, index=False, encoding='utf-8-sig')
    paths['trades'] = trades_path

    payload = {'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    if extra:
        payload.update(extra)
    if perf is not None:
        payload['performance'] = asdict(perf)

    json_path = os.path.join(out_dir, 'summary.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    paths['summary_json'] = json_path

    txt_path = os.path.join(out_dir, 'summary.txt')
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write(format_report(perf) + '\n')
    paths['summary_txt'] = txt_path

    print(f'[输出] 回测结果已保存到 {os.path.abspath(out_dir)}')
    for name in ('equity.csv', 'deals.csv', 'trades.csv', 'summary.json', 'summary.txt'):
        print(f'         {name}')
    return paths


def save_csv(result: BacktestResult, equity_path: str = '', deals_path: str = '') -> None:
    if equity_path:
        equity_dataframe(result).to_csv(equity_path, index=False, encoding='utf-8-sig')
        print(f'[输出] 净值曲线: {equity_path}')
    if deals_path:
        deals_dataframe(result).to_csv(deals_path, index=False, encoding='utf-8-sig')
        print(f'[输出] 成交明细: {deals_path}')
