# coding: utf-8
"""指标计算：一元线性回归、对数价格动量分数、RSRS 修正标准分。"""

from typing import Optional, Sequence, Tuple

import numpy as np


def linear_regression(x: Sequence[float], y: Sequence[float]) -> Tuple[float, float]:
    """numpy.polyfit 一元线性回归，返回 (slope, r_squared)"""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if len(x) < 2 or len(x) != len(y):
        return 0.0, 0.0

    slope, intercept = np.polyfit(x, y, 1)
    y_pred = slope * x + intercept
    ss_res = float(np.sum((y - y_pred) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return float(slope), float(r2)


def momentum_score(close_prices: Sequence[float],
                   trading_days_per_year: int = 244) -> Optional[float]:
    """
    动量分数 = 年化收益率 * |R2|
    对对数收盘价做线性回归，斜率年化后乘以拟合优度。
    数据不足或含非正数价格时返回 None；R2 <= 0 时返回 0.0（与原脚本一致）。
    """
    prices = np.asarray(close_prices, dtype=float)
    if len(prices) < 2 or np.any(np.isnan(prices)) or np.any(prices <= 0):
        return None

    log_prices = np.log(prices)
    x = np.arange(len(log_prices), dtype=float)

    try:
        slope, r2 = linear_regression(x, log_prices)
    except Exception:
        return None

    if r2 <= 0:
        return 0.0

    annual_return = np.exp(slope * trading_days_per_year) - 1
    return float(annual_return * abs(r2))


def rsrs_score(highs: Sequence[float], lows: Sequence[float],
               n: int = 21, m: int = 600) -> Optional[float]:
    """
    RSRS 修正标准分：
      1. 每 n 根 K 线用最低价回归最高价，取斜率 beta 与 R2
      2. 最近 m 个 beta 求 zscore
      3. zscore * 最新 R2
    数据不足返回 None。
    """
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    if len(highs) < n + m or len(highs) != len(lows):
        return None

    betas = []
    r2_list = []
    for i in range(n - 1, len(highs)):
        h = highs[i - n + 1:i + 1]
        l = lows[i - n + 1:i + 1]
        if len(h) < n or np.any(np.isnan(h)) or np.any(np.isnan(l)):
            continue
        try:
            slope, r2 = linear_regression(l, h)
        except Exception:
            continue
        betas.append(slope)
        r2_list.append(r2)

    if len(betas) < m:
        return None

    recent = np.asarray(betas[-m:], dtype=float)
    std = float(np.std(recent))
    if std == 0:
        return 0.0

    zscore = (recent[-1] - float(np.mean(recent))) / std
    recent_r2 = r2_list[-1] if r2_list else 0.0
    return float(zscore * recent_r2)


def limit_ratio(stock: str) -> float:
    """
    涨跌停幅度：创业板(300/301)、科创板(688) 为 20%，主板为 10%
    """
    code = stock.split('.')[0]
    if code.startswith(('300', '301', '688')):
        return 0.20
    return 0.10
