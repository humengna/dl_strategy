# coding: utf-8
"""
排行榜信号的持有收益：次日开盘买入，持有 N 个交易日后开盘卖出。

排行榜（scoreboard.py）只回答「当天谁的动量分数最高」，不回答「照着买赚不赚」。
这里把榜单和行情对起来算实际收益：

    信号日 D  ->  第 D+delay 个交易日开盘买入  ->  再持有 hold 个交易日，开盘卖出

默认 delay=1、hold=1，就是「次日开盘买入，第二天开盘卖出」。
注意 D 当天的分数本来就只用到 D-1 为止的收盘价，再延后一天买入，
等于把信号又推迟了一个交易日 —— 比回测里 D 日开盘就买要保守一档。

买卖都取开盘价，同一名次逐日首尾相接（今天卖出和明天买入是同一个开盘时点），
所以把每日收益连乘就是一条可以直接看的净值曲线，中间没有重叠持仓。

没有建模的部分（会让下面的收益偏乐观）：
  - 一字涨停买不进、一字跌停卖不掉
  - 冲击成本与滑点
  - 停牌只做了「买不进就作废、卖不掉就顺延」
"""

import bisect
import logging
import math
import os
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .config import AccountConfig, DAILY_FIELDS
from .datasource import DataSource
from .panel import Panel
from .report import display_width, pad

log = logging.getLogger(__name__)

DETAIL_COLUMNS = ['date', 'rank', 'stock', 'name', 'score',
                  'buy_date', 'buy_open', 'sell_date', 'sell_open',
                  'ret', 'ret_net', 'note']

TRADED = ''
NOTE_NO_BUY = '买入日停牌'
NOTE_NO_SELL = '卖出日之后无行情'
NOTE_NO_BAR = '无行情数据'


# ============================================================
# 读入榜单
# ============================================================

def load_scoreboard(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={'date': str}, encoding='utf-8-sig')
    missing = {'date', 'rank', 'stock'} - set(df.columns)
    if missing:
        raise SystemExit(f'{path} 缺少必需的列: {sorted(missing)}')
    if 'name' not in df.columns:
        df['name'] = ''
    if 'score' not in df.columns:
        df['score'] = np.nan
    df['date'] = df['date'].str.strip().str[:8]
    df['rank'] = df['rank'].astype(int)
    return df.sort_values(['date', 'rank']).reset_index(drop=True)


# ============================================================
# 取开盘价并逐条算收益
# ============================================================

def build_open_panel(source: DataSource, stocks: Sequence[str],
                     start_date: str, end_date: str,
                     download: bool = False) -> Panel:
    stocks = sorted(set(stocks))
    if download:
        source.download(stocks, start_date, end_date)
    source.preload(stocks, start_date, end_date)

    panel = Panel.from_source(source, stocks, start_date, end_date,
                              fields=('open', 'close', 'volume', 'suspendFlag'))
    if not len(panel) or not panel.stocks:
        raise SystemExit('取不到任何日线数据，无法计算收益。'
                         '加 --download 补下载，或用 --check-data 定位')
    log.info('行情面板: %d 个交易日 × %d 只标的', *panel.shape)
    return panel


def _next_pos(dates: Sequence[str], date: str, step: int) -> int:
    """date 之后第 step 个交易日在面板里的行号，越界返回 -1"""
    i = bisect.bisect_left(dates, date)
    if i >= len(dates) or dates[i] != date:
        i -= 1                       # 信号日本身不在面板里（例如停牌），从它之前那天起算
    j = i + step
    return j if 0 <= j < len(dates) else -1


def _first_valid(opens: np.ndarray, j: int, col: int, limit: int) -> int:
    """从第 j 行起往后找第一个有开盘价的行（卖出遇停牌顺延），最多找 limit 行"""
    n = opens.shape[0]
    for k in range(j, min(n, j + limit + 1)):
        value = opens[k, col]
        if np.isfinite(value) and value > 0:
            return k
    return -1


def score_returns(board: pd.DataFrame, panel: Panel, delay: int = 1, hold: int = 1,
                  account: Optional[AccountConfig] = None,
                  max_sell_delay: int = 20) -> pd.DataFrame:
    """
    逐条信号算收益，返回明细表。

    delay: 信号日之后第几个交易日开盘买入（1 = 次日）
    hold : 买入后持有几个交易日，在那天开盘卖出（1 = 第二天）
    """
    account = account or AccountConfig()
    buy_rate = account.buy_cost_rate
    sell_rate = account.sell_cost_rate

    opens = panel.field('open')
    dates = panel.dates
    records = []

    for row in board.itertuples(index=False):
        base = {'date': row.date, 'rank': int(row.rank), 'stock': row.stock,
                'name': getattr(row, 'name', ''), 'score': getattr(row, 'score', np.nan),
                'buy_date': '', 'buy_open': np.nan, 'sell_date': '', 'sell_open': np.nan,
                'ret': np.nan, 'ret_net': np.nan, 'note': TRADED}

        col = panel.stock_pos.get(row.stock)
        if col is None:
            records.append(dict(base, note=NOTE_NO_BAR))
            continue

        b = _next_pos(dates, row.date, delay)
        if b < 0 or not (np.isfinite(opens[b, col]) and opens[b, col] > 0):
            records.append(dict(base, note=NOTE_NO_BUY))       # 买入日停牌，这笔作废
            continue

        s = _first_valid(opens, b + hold, col, max_sell_delay)   # 卖出遇停牌顺延
        if s < 0:
            records.append(dict(base, buy_date=dates[b], buy_open=float(opens[b, col]),
                                note=NOTE_NO_SELL))
            continue

        buy, sell = float(opens[b, col]), float(opens[s, col])
        ret = sell / buy - 1.0
        net = sell * (1 - sell_rate) / (buy * (1 + buy_rate)) - 1.0
        records.append(dict(base, buy_date=dates[b], buy_open=buy,
                            sell_date=dates[s], sell_open=sell,
                            ret=ret, ret_net=net))

    return pd.DataFrame(records, columns=DETAIL_COLUMNS)


# ============================================================
# 统计
# ============================================================

def curve_stats(returns: Sequence[float], trading_days_per_year: int = 244) -> Dict[str, float]:
    """一条逐日收益序列的累计 / 年化 / 回撤 / 夏普"""
    values = np.array([r for r in returns if np.isfinite(r)], dtype=float)
    if len(values) == 0:
        return {}

    equity = np.cumprod(1.0 + values)
    peak = np.maximum.accumulate(equity)
    drawdown = equity / peak - 1.0
    total = float(equity[-1] - 1.0)

    years = len(values) / float(trading_days_per_year)
    if years > 0 and equity[-1] > 0:
        annual = float(equity[-1] ** (1.0 / years) - 1.0)
    else:
        annual = float('nan')

    mean, std = float(values.mean()), float(values.std(ddof=1)) if len(values) > 1 else 0.0
    sharpe = (mean / std * math.sqrt(trading_days_per_year)) if std > 0 else float('nan')

    return {'days': len(values), 'total': total, 'annual': annual,
            'max_drawdown': float(drawdown.min()), 'mean': mean, 'std': std,
            'sharpe': sharpe, 'win_rate': float((values > 0).mean()),
            'best': float(values.max()), 'worst': float(values.min()),
            'median': float(np.median(values))}


def rank_stats(detail: pd.DataFrame, column: str = 'ret',
               trading_days_per_year: int = 244) -> List[Tuple[str, Dict[str, float]]]:
    """按名次分组统计，最后再加一行 TOP-N 等权组合"""
    out = []
    for rank in sorted(detail['rank'].unique()):
        rows = detail[detail['rank'] == rank].sort_values('date')
        stats = curve_stats(rows[column].to_numpy(dtype=float), trading_days_per_year)
        if stats:
            stats['signals'] = int(len(rows))
            stats['traded'] = int(rows[column].notna().sum())
            out.append((f'第 {rank} 名', stats))

    daily = detail.groupby('date')[column].mean()         # 每日等权持有当天上榜的几只
    stats = curve_stats(daily.to_numpy(dtype=float), trading_days_per_year)
    if stats:
        stats['signals'] = int(len(detail))
        stats['traded'] = int(detail[column].notna().sum())
        out.append((f'TOP{detail["rank"].max()} 等权', stats))
    return out


STAT_COLUMNS = (('口径', 12, True), ('笔数', 7, False), ('日均', 9, False),
                ('中位数', 9, False), ('胜率', 8, False), ('累计', 11, False),
                ('年化', 11, False), ('最大回撤', 10, False), ('夏普', 7, False))


def format_stats(rows: Sequence[Tuple[str, Dict[str, float]]], title: str) -> str:
    header = '  ' + ' '.join(pad(name, width, left) for name, width, left in STAT_COLUMNS)
    rule = '=' * display_width(header)
    lines = [rule, title, rule, header]
    for label, s in rows:
        values = [label, str(s.get('traded', s['days'])), f"{s['mean']:.3%}",
                  f"{s['median']:.3%}", f"{s['win_rate']:.1%}", f"{s['total']:.2%}",
                  f"{s['annual']:.2%}", f"{s['max_drawdown']:.2%}", f"{s['sharpe']:.2f}"]
        lines.append('  ' + ' '.join(pad(v, w, left)
                                     for v, (_, w, left) in zip(values, STAT_COLUMNS)))
    lines.append(rule)
    return '\n'.join(lines)


def format_notes(detail: pd.DataFrame) -> str:
    counts = detail['note'].value_counts()
    total = len(detail)
    done = int(counts.get(TRADED, 0))
    lines = [f'  信号 {total} 条，成交 {done} 条（{done / total:.1%}）']
    for note, count in counts.items():
        if note != TRADED:
            lines.append(f'    {note}: {count} 条')
    best = detail.loc[detail['ret'].idxmax()] if detail['ret'].notna().any() else None
    worst = detail.loc[detail['ret'].idxmin()] if detail['ret'].notna().any() else None
    if best is not None:
        lines.append(f"  单笔最好: {best['date']} 第{best['rank']}名 {best['stock']} "
                     f"{best['name']} {best['ret']:+.2%}")
        lines.append(f"  单笔最差: {worst['date']} 第{worst['rank']}名 {worst['stock']} "
                     f"{worst['name']} {worst['ret']:+.2%}")
    return '\n'.join(lines)


def portfolio_equity(detail: pd.DataFrame, column: str = 'ret') -> pd.DataFrame:
    """TOP-N 等权组合的逐日收益与净值"""
    daily = detail.groupby('date')[column].mean().sort_index()
    daily = daily[np.isfinite(daily.to_numpy(dtype=float))]
    return pd.DataFrame({'date': daily.index, 'ret': daily.to_numpy(),
                         'equity': np.cumprod(1.0 + daily.to_numpy())})


# ============================================================
# 一次跑完
# ============================================================

def pad_end_date(last_signal: str, delay: int, hold: int, max_sell_delay: int) -> str:
    """最后几条信号要用到信号日之后的行情，取数窗口往后多留一截（不超过今天）"""
    from datetime import datetime, timedelta

    need = delay + hold + max_sell_delay
    end = datetime.strptime(last_signal, '%Y%m%d') + timedelta(days=int(need * 1.8) + 10)
    return min(end.strftime('%Y%m%d'), datetime.now().strftime('%Y%m%d'))


def run_score_returns(source: DataSource, board_path: str, delay: int = 1, hold: int = 1,
                      account: Optional[AccountConfig] = None, ranks: Sequence[int] = (),
                      out_path: str = '', download: bool = False, end_date: str = '',
                      trading_days_per_year: int = 244, max_sell_delay: int = 20
                      ) -> pd.DataFrame:
    board = load_scoreboard(board_path)
    if ranks:
        board = board[board['rank'].isin(list(ranks))].reset_index(drop=True)
    if board.empty:
        raise SystemExit('榜单里没有符合条件的记录')

    start, last = board['date'].min(), board['date'].max()
    end = end_date or pad_end_date(last, delay, hold, max_sell_delay)
    log.info('榜单: %s ~ %s，%d 条信号，%d 只标的',
             start, last, len(board), board['stock'].nunique())

    panel = build_open_panel(source, board['stock'].unique(), start, end, download)
    detail = score_returns(board, panel, delay, hold, account, max_sell_delay)

    entry = '次日' if delay == 1 else f'信号日之后第 {delay} 个交易日'
    exit_ = '第二天' if hold == 1 else f'持有 {hold} 个交易日后'
    title = f'[{entry}开盘买入 / {exit_}开盘卖出]  {start} ~ {last}'

    print(format_stats(rank_stats(detail, 'ret', trading_days_per_year), title + '  不计费用'))
    print(format_notes(detail))
    print()
    print(format_stats(rank_stats(detail, 'ret_net', trading_days_per_year),
                       title + '  扣佣金/过户费/印花税'))
    print('  未建模：一字涨停买不进、一字跌停卖不掉、滑点与冲击成本')

    if out_path:
        folder = os.path.dirname(os.path.abspath(out_path))
        if folder:
            os.makedirs(folder, exist_ok=True)
        detail.to_csv(out_path, index=False, encoding='utf-8-sig')
        print(f'[输出] 逐笔明细: {out_path}')

        base, ext = os.path.splitext(out_path)
        curve = f'{base}_equity{ext}'
        portfolio_equity(detail).to_csv(curve, index=False, encoding='utf-8-sig')
        print(f'[输出] 等权组合净值: {curve}')

    return detail
