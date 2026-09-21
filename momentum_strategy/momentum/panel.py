# coding: utf-8
"""
向量化行情面板与滚动指标核。

逐日、逐股票地切 DataFrame 再调 np.polyfit 是回测最大的开销：
5000 只 × 250 个交易日 = 125 万次回归。这里把行情拉平成
(交易日 × 标的) 的 numpy 矩阵，再用滚动求和一次性算出所有格子的
动量分数，把 125 万次回归压缩成几次矩阵运算。

关键恒等式（一元线性回归）：
    slope = (L*Sxy - Sx*Sy) / (L*Sxx - Sx^2)
    R^2   = (L*Sxy - Sx*Sy)^2 / ((L*Sxx - Sx^2) * (L*Sy2 - Sy^2))
其中 S* 都是窗口内的和，可以用 cumsum 在 O(n) 内滚动求出。
slope 和 R^2 对 x、y 各自平移不变，所以计算前先减去列均值，
避免大数相减的精度损失（和 polyfit 的结果对齐到 1e-9 以内）。
"""

import bisect
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .config import DAILY_FIELDS


# ============================================================
# 滚动求和 / 滚动回归
# ============================================================

def _rolling_sum(a: np.ndarray, window: int) -> np.ndarray:
    """第 i 行 = a[i-window+1 : i+1] 的和，前 window-1 行为 nan。a 不得含 nan。"""
    out = np.full(a.shape, np.nan)
    n = a.shape[0]
    if n < window or window < 1:
        return out

    cum = np.cumsum(a, axis=0)
    head = np.zeros((1,) + a.shape[1:], dtype=float)
    out[window - 1:] = cum[window - 1:] - np.concatenate([head, cum[:n - window]], axis=0)
    return out


def rolling_linreg_fixed_x(y: np.ndarray, window: int
                           ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    对每个滚动窗口做 y ~ x 回归，x 固定为 0..window-1。

    返回 (slope, r2, ok)，形状与 y 相同；第 i 行对应窗口 y[i-window+1 : i+1]。
    ok 表示该窗口内所有值都有效（非 nan）。
    """
    y = np.asarray(y, dtype=float)
    squeeze = y.ndim == 1
    if squeeze:
        y = y.reshape(-1, 1)

    n, m = y.shape
    nan = np.full((n, m), np.nan)
    if n < window or window < 2:
        return nan, nan.copy(), np.zeros((n, m), dtype=bool)

    valid = np.isfinite(y)
    # 平移不改变 slope / R2，减去列均值以降低大数相减的精度损失
    with np.errstate(invalid='ignore'):
        center = np.nanmean(np.where(valid, y, np.nan), axis=0)
    center = np.where(np.isfinite(center), center, 0.0)
    yy = np.where(valid, y - center, 0.0)

    count = _rolling_sum(valid.astype(float), window)
    sum_y = _rolling_sum(yy, window)
    sum_y2 = _rolling_sum(yy * yy, window)

    # Sxy = Σ k * y_k，k 为窗口内位置；window 很小，直接累加 window 次向量化位移
    acc = np.zeros((n - window + 1, m))
    for k in range(window):
        acc += k * yy[k:n - window + 1 + k]
    sum_xy = np.full((n, m), np.nan)
    sum_xy[window - 1:] = acc

    length = float(window)
    sum_x = length * (length - 1) / 2.0
    sum_xx = (length - 1) * length * (2 * length - 1) / 6.0
    den_x = length * sum_xx - sum_x * sum_x

    with np.errstate(invalid='ignore', divide='ignore'):
        num = length * sum_xy - sum_x * sum_y
        den_y = length * sum_y2 - sum_y * sum_y
        slope = num / den_x
        r2 = np.where(den_y > 0, num * num / (den_x * den_y), 0.0)

    ok = count >= window - 1e-9
    slope = np.where(ok, slope, np.nan)
    r2 = np.where(ok, r2, np.nan)

    if squeeze:
        return slope.ravel(), r2.ravel(), ok.ravel()
    return slope, r2, ok


def rolling_linreg_xy(x: np.ndarray, y: np.ndarray, window: int
                      ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """x、y 都随窗口变化的滚动回归（RSRS 用最低价回归最高价）。"""
    x = np.asarray(x, dtype=float).reshape(-1, 1)
    y = np.asarray(y, dtype=float).reshape(-1, 1)

    n = x.shape[0]
    nan = np.full(n, np.nan)
    if n < window or window < 2:
        return nan, nan.copy(), np.zeros(n, dtype=bool)

    valid = np.isfinite(x) & np.isfinite(y)
    with np.errstate(invalid='ignore'):
        cx = np.nanmean(np.where(valid, x, np.nan))
        cy = np.nanmean(np.where(valid, y, np.nan))
    cx = cx if np.isfinite(cx) else 0.0
    cy = cy if np.isfinite(cy) else 0.0

    xx = np.where(valid, x - cx, 0.0)
    yy = np.where(valid, y - cy, 0.0)

    count = _rolling_sum(valid.astype(float), window)
    sum_x = _rolling_sum(xx, window)
    sum_y = _rolling_sum(yy, window)
    sum_xx = _rolling_sum(xx * xx, window)
    sum_yy = _rolling_sum(yy * yy, window)
    sum_xy = _rolling_sum(xx * yy, window)

    length = float(window)
    with np.errstate(invalid='ignore', divide='ignore'):
        num = length * sum_xy - sum_x * sum_y
        den_x = length * sum_xx - sum_x * sum_x
        den_y = length * sum_yy - sum_y * sum_y
        slope = np.where(den_x > 0, num / den_x, np.nan)
        r2 = np.where((den_x > 0) & (den_y > 0), num * num / (den_x * den_y), 0.0)

    ok = (count >= window - 1e-9)
    slope = np.where(ok, slope, np.nan)
    r2 = np.where(ok, r2, np.nan)
    return slope.ravel(), r2.ravel(), ok.ravel()


# ============================================================
# 策略指标矩阵
# ============================================================

def momentum_score_matrix(close: np.ndarray, lookback: int,
                          trading_days_per_year: int = 244) -> np.ndarray:
    """
    动量分数矩阵。out[i, j] = 用 close[i-lookback : i, j]（不含第 i 行）算出的分数，
    与逐日调用 momentum_score(收盘价窗口) 等价。

    窗口内有非正价或缺失 -> nan（排名时剔除）；价格完全走平 -> 0.0。
    """
    close = np.asarray(close, dtype=float)
    with np.errstate(invalid='ignore', divide='ignore'):
        logp = np.log(np.where(close > 0, close, np.nan))

    slope, r2, ok = rolling_linreg_fixed_x(logp, lookback)
    with np.errstate(over='ignore', invalid='ignore'):
        score = (np.exp(slope * trading_days_per_year) - 1.0) * np.abs(r2)
    score = np.where(ok, score, np.nan)
    score = np.where(ok & (r2 <= 0), 0.0, score)

    out = np.full(score.shape, np.nan)
    out[1:] = score[:-1]          # 排除当日：第 i 行用到 i-1 结束的窗口
    return out


def rsrs_series(high: np.ndarray, low: np.ndarray, n: int, m: int) -> np.ndarray:
    """
    RSRS 修正标准分序列。out[i] = 截至 i-1（不含当日）的 RSRS 值，
    与逐日调用 rsrs_score(high[:i], low[:i]) 等价。
    """
    beta, r2, ok = rolling_linreg_xy(low, high, n)

    valid = np.isfinite(beta)
    beta0 = np.where(valid, beta, 0.0).reshape(-1, 1)
    count = _rolling_sum(valid.astype(float).reshape(-1, 1), m).ravel()
    sum_b = _rolling_sum(beta0, m).ravel()
    sum_b2 = _rolling_sum(beta0 * beta0, m).ravel()

    with np.errstate(invalid='ignore', divide='ignore'):
        mean = sum_b / m
        var = sum_b2 / m - mean * mean
        std = np.sqrt(np.where(var > 0, var, np.nan))
        z = np.where(std > 0, (beta - mean) / std, 0.0)
        value = z * r2

    enough = np.isfinite(count) & (count >= m - 1e-9) & ok
    value = np.where(enough, value, np.nan)

    out = np.full(len(value), np.nan)
    out[1:] = value[:-1]          # 排除当日
    return out


# ============================================================
# 行情面板
# ============================================================

class Panel(object):
    """(交易日 × 标的) 的行情矩阵集合"""

    def __init__(self, dates: Sequence[str], stocks: Sequence[str],
                 arrays: Dict[str, np.ndarray]):
        self.dates = list(dates)
        self.stocks = list(stocks)
        self.arrays = arrays
        self.date_pos = {d: i for i, d in enumerate(self.dates)}
        self.stock_pos = {s: j for j, s in enumerate(self.stocks)}

    # ---------- 访问 ----------

    def __len__(self) -> int:
        return len(self.dates)

    @property
    def shape(self) -> Tuple[int, int]:
        return len(self.dates), len(self.stocks)

    def has(self, field: str) -> bool:
        return field in self.arrays

    def field(self, name: str) -> Optional[np.ndarray]:
        return self.arrays.get(name)

    def row(self, field: str, i: int) -> Optional[np.ndarray]:
        arr = self.arrays.get(field)
        return None if arr is None else arr[i]

    def index_of(self, date: str) -> int:
        """date 所在行；不是交易日时取其之前最近的一行，早于起点返回 -1"""
        pos = self.date_pos.get(date)
        if pos is not None:
            return pos
        i = bisect.bisect_right(self.dates, date) - 1
        return i

    def value(self, field: str, i: int, j: int) -> float:
        arr = self.arrays.get(field)
        if arr is None or i < 0 or j < 0:
            return float('nan')
        return float(arr[i, j])

    # ---------- 构造 ----------

    @classmethod
    def from_frames(cls, frames: Dict[str, pd.DataFrame],
                    fields: Sequence[str] = DAILY_FIELDS,
                    start_date: str = '', end_date: str = '') -> 'Panel':
        stocks = [s for s in frames if frames[s] is not None and len(frames[s]) > 0]
        stocks.sort()

        all_dates = set()
        for stock in stocks:
            all_dates.update(str(d)[:8] for d in frames[stock].index)
        dates = sorted(d for d in all_dates
                       if (not start_date or d >= start_date) and (not end_date or d <= end_date))

        n, m = len(dates), len(stocks)
        pos = {d: i for i, d in enumerate(dates)}
        available = set()
        for stock in stocks:
            available.update(frames[stock].columns)
        use_fields = [f for f in fields if f in available]

        arrays = {f: np.full((n, m), np.nan) for f in use_fields}
        for j, stock in enumerate(stocks):
            df = frames[stock]
            idx = [str(d)[:8] for d in df.index]
            rows = np.array([pos.get(d, -1) for d in idx])
            keep = rows >= 0
            if not keep.any():
                continue
            rows = rows[keep]
            for f in use_fields:
                if f not in df.columns:
                    continue
                values = pd.to_numeric(df[f], errors='coerce').to_numpy(dtype=float)
                arrays[f][rows, j] = values[keep]

        return cls(dates, stocks, arrays)

    @classmethod
    def from_source(cls, source, stocks: Sequence[str], start_date: str, end_date: str,
                    fields: Sequence[str] = DAILY_FIELDS) -> 'Panel':
        frames = source.get_bars(list(stocks), end_date, 0, fields)
        return cls.from_frames(frames, fields, start_date, end_date)
