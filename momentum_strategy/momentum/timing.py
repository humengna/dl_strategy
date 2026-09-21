# coding: utf-8
"""
步骤 5：择时信号。

与原脚本一致：RSRS 只计算并打印，不参与决策；
实际信号由目标股动量分数「连续下降天数」决定。
"""

import logging
from typing import Optional, Sequence

from .config import StrategyConfig
from .datasource import DataSource
from .indicators import rsrs_score

log = logging.getLogger(__name__)

SIGNAL_BUY = 'BUY'
SIGNAL_SELL = 'SELL'
SIGNAL_KEEP = 'KEEP'


def count_decline_days(scores: Sequence[float], epsilon: float = 0.0) -> int:
    """
    从最新一个分数往前数，连续下降了多少天。

    epsilon 为相对容差：只有 scores[i] 比 scores[i-1] 低出 epsilon * |scores[i-1]|
    才算一次下降。默认 0.0，即原脚本的严格比较。
    """
    days = 0
    for i in range(len(scores) - 1, 0, -1):
        threshold = scores[i - 1] - abs(scores[i - 1]) * epsilon
        if scores[i] < threshold:
            days += 1
        else:
            break
    return days


def timing_signal(scores: Sequence[float], cfg: StrategyConfig) -> str:
    """
    分数序列为空 -> KEEP（维持现状）
    连续下降天数 >= decline_days_to_sell -> SELL，否则 BUY
    """
    if not scores:
        return SIGNAL_KEEP

    days = count_decline_days(scores, cfg.decline_epsilon)
    log.info('动量分数序列: %s', [round(float(s), 4) for s in scores])
    log.info('连续下降天数: %d', days)

    return SIGNAL_SELL if days >= cfg.decline_days_to_sell else SIGNAL_BUY


def rsrs_value(source: DataSource, date: str, cfg: StrategyConfig) -> Optional[float]:
    """大盘 RSRS 修正标准分（排除当前 bar）"""
    if not cfg.rsrs_enabled:
        return None

    df = source.get_one(cfg.rsrs_index, date, cfg.bars_needed_for_rsrs)
    if df is None or len(df) < cfg.rsrs_m + cfg.rsrs_n + 1:
        return None

    return rsrs_score(df['high'].values[:-1], df['low'].values[:-1],
                      n=cfg.rsrs_n, m=cfg.rsrs_m)
