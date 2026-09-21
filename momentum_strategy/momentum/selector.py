# coding: utf-8
"""
步骤 2~4：动量打分排序、动量分数序列、候选股过滤。

所有取数一律多取 1 根并用 [-(n+1):-1] 切片排除当前 bar，
保证打分只用到「上一交易日及之前」的收盘价，不含未来函数。
"""

import logging
from typing import List, Optional, Sequence, Tuple

import numpy as np

from .config import StrategyConfig
from .datasource import DataSource
from .indicators import limit_ratio, momentum_score

log = logging.getLogger(__name__)


def rank_by_momentum(source: DataSource, pool: Sequence[str], date: str,
                     cfg: StrategyConfig) -> List[Tuple[str, float]]:
    """对股票池打分并按分数降序排列，返回 [(stock, score), ...]"""
    if not pool:
        return []

    quotes = source.get_bars(pool, date, cfg.bars_needed_for_rank)

    scores = {}
    for stock in pool:
        df = quotes.get(stock)
        if df is None or len(df) < cfg.lookback_days + 1:
            continue

        closes = df['close'].values
        hist = closes[-(cfg.lookback_days + 1):-1]   # 排除当前 bar
        if len(hist) < cfg.lookback_days or np.any(np.isnan(hist)):
            continue

        score = momentum_score(hist, cfg.trading_days_per_year)
        if score is not None:
            scores[stock] = score

    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def pick_target(source: DataSource, pool: Sequence[str], date: str,
                cfg: StrategyConfig) -> Optional[str]:
    """取动量分数第 1 名"""
    ranked = rank_by_momentum(source, pool, date, cfg)
    if not ranked:
        return None
    log.info('Top3: %s', [(s, round(sc, 4)) for s, sc in ranked[:3]])
    return ranked[0][0]


def momentum_series(source: DataSource, stock: str, date: str,
                    cfg: StrategyConfig) -> List[float]:
    """
    目标股的动量分数序列，从远到近共 score_series_len + 1 个值
    （原脚本取 5 个历史值再补 1 个最新值，共 6 个）。
    数据不足时返回空列表。
    """
    df = source.get_one(stock, date, cfg.bars_needed_for_series)
    if df is None or len(df) < cfg.lookback_days + 2:
        return []

    closes = df['close'].values
    n = cfg.lookback_days
    scores: List[float] = []

    for i in range(cfg.score_series_len, 0, -1):
        window = closes[-(n + 1 + i):-(i + 1)]
        if len(window) >= n and not np.any(np.isnan(window)):
            score = momentum_score(window, cfg.trading_days_per_year)
            scores.append(score if score is not None else 0.0)
        else:
            scores.append(0.0)

    latest = closes[-(n + 1):-1]
    if len(latest) >= n and not np.any(np.isnan(latest)):
        score = momentum_score(latest, cfg.trading_days_per_year)
        scores.append(score if score is not None else 0.0)
    else:
        scores.append(0.0)

    return scores


def filter_target(source: DataSource, stock: Optional[str], date: str,
                  cfg: StrategyConfig) -> Optional[str]:
    """剔除停牌、跌停的候选股；通过则原样返回代码"""
    if not stock:
        return None

    df = source.get_one(stock, date, 1)
    if df is None:
        return None

    if 'suspendFlag' in df.columns:
        try:
            if int(df['suspendFlag'].iloc[-1]) == 1:
                log.info('%s 停牌中', stock)
                return None
        except (TypeError, ValueError):
            pass

    last_close = float(df['close'].iloc[-1])
    if last_close <= 0:
        return None

    pre_close = float(df['preClose'].iloc[-1]) if 'preClose' in df.columns else last_close
    ratio = limit_ratio(stock) if cfg.dynamic_limit_down else 0.10
    limit_down = round(pre_close * (1 - ratio), 2)
    if last_close <= limit_down:
        log.info('%s 跌停，收盘:%.2f 跌停价:%.2f', stock, last_close, limit_down)
        return None

    return stock


def price_and_limits(source: DataSource, stock: str, date: str):
    """
    返回 (开盘价, 涨停价, 跌停价, 最低价)，取不到时四个值均为 0.0。
    涨跌停幅度按代码前缀区分。
    """
    df = source.get_one(stock, date, 1)
    if df is None:
        return 0.0, 0.0, 0.0, 0.0

    open_price = float(df['open'].iloc[-1])
    low_price = float(df['low'].iloc[-1]) if 'low' in df.columns else open_price
    pre_close = float(df['preClose'].iloc[-1]) if 'preClose' in df.columns else open_price
    if pre_close <= 0:
        return open_price, 0.0, 0.0, low_price

    ratio = limit_ratio(stock)
    return (open_price,
            round(pre_close * (1 + ratio), 2),
            round(pre_close * (1 - ratio), 2),
            low_price)
