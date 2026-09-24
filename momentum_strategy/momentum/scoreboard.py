# coding: utf-8
"""
每日动量分数排行榜。

回测只会告诉你最后买了哪只票，中间「谁排第几」被股票池过滤、停牌/涨跌停拦截、
择时信号一层层削掉了。这里把打分环节单独拆出来看：

    原始股票池 -> 行情面板 -> 动量分数矩阵 -> 每日取前 N 名

中间不套任何过滤模块 —— 不看市值、不看 ST、不看停牌、不看涨跌停，
也不判断能不能买卖，纯粹是信号本身长什么样。

与回测口径一致的地方：
  - 打分算法、回看天数、年化天数共用 momentum_score_matrix，数值完全相同
  - 同样排除当日：第 i 天的分数只用到第 i-1 天为止的收盘价，没有未来函数
"""

import logging
import os
from typing import Callable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .config import BacktestConfig
from .datasource import DataSource
from .panel import Panel, momentum_score_matrix
from .report import display_width, pad
from .universe import build_base_pool

log = logging.getLogger(__name__)

DEFAULT_TOP_N = 5

# (日期, [(代码, 分数), ...])
DayPicks = Tuple[str, List[Tuple[str, float]]]

NO_PANEL_HINT = (
    '数据源取不到任何日线数据，排行榜无法计算。\n'
    '  - 加 --download 补下载日线，或在 QMT 客户端「行情 -> 数据管理」补充\n'
    '  - 用 python run_backtest.py --check-data 逐步定位'
)


# ============================================================
# 计算
# ============================================================

def build_score_matrix(source: DataSource, config: BacktestConfig,
                       download: bool = False) -> Tuple[Panel, np.ndarray]:
    """取原始股票池 -> 面板 -> 动量分数矩阵，返回 (panel, scores)"""
    cfg = config.strategy

    pool = build_base_pool(source, cfg)
    log.info('原始股票池: %d 只（未经任何过滤）', len(pool))
    if not pool:
        raise RuntimeError('概念板块未取到任何股票')

    # 打分要用回看窗口内的收盘价，起点往前多留几天。
    # 这里刻意和 BacktestEngine._warm_start 取同一个提前量：面板范围一致，
    # 分数矩阵才会和回测里的逐格相同（回归前减列均值，范围不同会差到 1e-12）。
    all_days = source.get_trading_dates(config.end_date)
    run_days = [d for d in all_days if d >= config.start_date]
    if all_days and run_days:
        first = all_days.index(run_days[0])
        start = all_days[max(0, first - (cfg.warmup_days + 5))]
    else:
        start = config.start_date

    if download:
        source.download(pool, start, config.end_date)
    source.preload(pool, start, config.end_date)

    log.info('构建行情面板 ...')
    panel = Panel.from_source(source, pool, start, config.end_date)
    if not len(panel) or not panel.stocks:
        raise RuntimeError(NO_PANEL_HINT)
    log.info('面板规模: %d 个交易日 × %d 只标的', *panel.shape)

    scores = momentum_score_matrix(panel.field('close'), cfg.lookback_days,
                                   cfg.trading_days_per_year)
    return panel, scores


def daily_top_scores(panel: Panel, scores: np.ndarray, start_date: str = '',
                     top_n: int = DEFAULT_TOP_N) -> List[DayPicks]:
    """
    逐日取分数最高的 top_n 只。分数为 nan（窗口内数据不足或有非正价）的不参与排名，
    这是「算不出来」而不是「被过滤掉」。
    """
    out: List[DayPicks] = []
    for i, date in enumerate(panel.dates):
        if start_date and date < start_date:
            continue

        row = scores[i]
        usable = np.flatnonzero(np.isfinite(row))
        if len(usable) == 0:
            out.append((date, []))
            continue

        count = min(max(int(top_n), 1), len(usable))
        idx = usable[np.argpartition(-row[usable], count - 1)[:count]]
        idx = idx[np.argsort(-row[idx], kind='stable')]
        out.append((date, [(panel.stocks[j], float(row[j])) for j in idx]))
    return out


# ============================================================
# 输出
# ============================================================

def format_score(value: float) -> str:
    """分数是 (exp(slope×244)-1)×R²，量级能跨十几个数量级，太大太小都转科学计数"""
    if not np.isfinite(value):
        return 'nan'
    if value != 0 and (abs(value) >= 1e6 or abs(value) < 1e-4):
        return '%.4e' % value
    return '%.4f' % value


SCORE_COLUMNS = (('日期', 10, True), ('排名', 6, False), ('代码', 11, True),
                 ('名称', 12, True), ('动量分数', 14, False))


def format_scoreboard(rows: Sequence[DayPicks], top_n: int, lookback: int,
                      name_lookup: Optional[Callable[[str], str]] = None,
                      max_days: int = 0) -> str:
    """终端表格。同一天只在第一行显示日期，视觉上自然分组。"""
    header = '  ' + ' '.join(pad(name, width, left) for name, width, left in SCORE_COLUMNS)
    rule = '=' * display_width(header)
    title = f'[每日动量分数 TOP{top_n}]  回看 {lookback} 天 · 不套任何过滤模块'
    lines = [rule, title,
             '  股票池=板块全部成分股，不看市值/ST/停牌/涨跌停',
             '  分数只用到上一交易日为止的收盘价，不含未来函数',
             rule, header]

    shown = rows if max_days <= 0 else rows[-max_days:]
    for date, picks in shown:
        if not picks:
            lines.append('  ' + pad(date, SCORE_COLUMNS[0][1], True) + ' 无有效分数')
            continue
        for rank, (stock, score) in enumerate(picks, 1):
            name = name_lookup(stock) if name_lookup else ''
            values = [date if rank == 1 else '', str(rank), stock, name or '-',
                      format_score(score)]
            lines.append('  ' + ' '.join(pad(v, w, left)
                                         for v, (_, w, left) in zip(values, SCORE_COLUMNS)))

    if max_days > 0 and len(rows) > max_days:
        lines.insert(4, f'  （共 {len(rows)} 个交易日，这里只显示最后 {max_days} 天，完整结果见 csv）')
    lines.append(rule)
    return '\n'.join(lines)


def scoreboard_dataframe(rows: Sequence[DayPicks],
                         name_lookup: Optional[Callable[[str], str]] = None
                         ) -> pd.DataFrame:
    records = []
    for date, picks in rows:
        for rank, (stock, score) in enumerate(picks, 1):
            records.append({
                'date': date,
                'rank': rank,
                'stock': stock,
                'name': (name_lookup(stock) if name_lookup else '') or '',
                'score': score,
            })
    return pd.DataFrame(records, columns=['date', 'rank', 'stock', 'name', 'score'])


def save_scoreboard(rows: Sequence[DayPicks], path: str,
                    name_lookup: Optional[Callable[[str], str]] = None) -> str:
    if not path:
        return ''
    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    scoreboard_dataframe(rows, name_lookup).to_csv(path, index=False, encoding='utf-8-sig')
    print(f'[输出] 每日动量分数排行: {path}')
    return path


# ============================================================
# 一次跑完
# ============================================================

def run_scoreboard(source: DataSource, config: BacktestConfig,
                   top_n: int = DEFAULT_TOP_N, out_path: str = '',
                   download: bool = False, max_print_days: int = 0
                   ) -> List[DayPicks]:
    panel, scores = build_score_matrix(source, config, download=download)
    rows = daily_top_scores(panel, scores, config.start_date, top_n)

    name_lookup = getattr(source, 'get_stock_name', None)
    print(format_scoreboard(rows, top_n, config.strategy.lookback_days,
                            name_lookup, max_print_days))
    save_scoreboard(rows, out_path, name_lookup)
    return rows
