# coding: utf-8
"""
A股动量择时策略 [优化版单文件]

在原策略基础上按实测诊断做了五项调整（默认全部开启）：
  1. 仓位系数 0.35     —— 凯利最优 k*=μ/σ²≈0.56，取更保守值
  2. 5 只等权分散       —— 组合方差 σ²(1/N+(1-1/N)ρ)，显著降波动
  3. 择时盯当前持仓     —— 原逻辑判断候选股，SELL 几乎不触发
  4. 动量回看 29 天     —— 原脚本注释里的值，5 天是波动的主要来源
  5. 涨停按开盘价拦截   —— 原用当日最低价，是最后一处未来函数

诊断依据（2020-2026 满仓单票实测）：算术日均 +0.3756%/天、日波动 8.20%/天，
波动损耗 σ²/2 = 0.3363%/天，吃掉算术收益的 90%，几何日均只剩 +0.0394%。

!! 本文件由 momentum_strategy/tools/build_standalone.py 自动生成，请勿直接修改 !!
   改动请提交到 momentum_strategy/momentum/ 下的模块，再重新生成。

运行
----
  python dl_strategy_opt.py --start 20200101 --end 20260918 --cash 1000000 -q
  python dl_strategy_opt.py --check-data
  加 --max-positions 1 --position-ratio 1 可退回原策略口径做对照
  结果默认保存到 results/bt_<起止日期>_..._opt_.../
"""

import argparse
import bisect
import json
import logging
import math
import numpy as np
import os
import pandas as pd
import sys
import time
import unicodedata
from collections import deque
from dataclasses import asdict
from dataclasses import asdict, dataclass
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, Optional
from typing import Callable, List, Optional, Sequence, Tuple
from typing import Dict, Iterable, List, Optional, Sequence
from typing import Dict, List, Optional
from typing import Dict, List, Optional, Sequence
from typing import Dict, List, Optional, Sequence, Tuple
from typing import List, Optional
from typing import List, Optional, Sequence
from typing import List, Optional, Sequence, Tuple
from typing import List, Sequence
from typing import Optional, Sequence
from typing import Optional, Sequence, Tuple
from typing import Optional, TextIO
from typing import Tuple

# ======================================================================
# progress.py
# ======================================================================

"""
终端进度条。

终端（tty）下用 \\r 原地刷新，重定向到文件时按百分比档位换行打印，
这样日志文件里不会出现成千上万行刷屏。
"""



def format_duration(seconds: float) -> str:
    """把秒数格式化成 mm:ss 或 h:mm:ss"""
    if seconds is None or seconds != seconds or seconds in (float('inf'), float('-inf')) or seconds < 0:
        return '--:--'
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f'{hours}:{minutes:02d}:{secs:02d}'
    return f'{minutes:02d}:{secs:02d}'


class Progress(object):
    """
    进度条。

        bar = Progress(total=5224, prefix='下载日线')
        for i, item in enumerate(items, 1):
            ...
            bar.update(i, suffix=item)
        bar.close()
    """

    BAR_WIDTH = 24

    def __init__(self, total: int, prefix: str = '', enabled: bool = True,
                 stream: Optional[TextIO] = None, min_interval: float = 0.2,
                 step_percent: int = 10):
        self.total = max(int(total or 0), 0)
        self.prefix = prefix
        self.stream = stream if stream is not None else sys.stdout
        self.enabled = bool(enabled) and self.total > 0
        self.min_interval = min_interval          # tty 下两次刷新的最小间隔
        self.step_percent = max(int(step_percent), 1)   # 非 tty 下每多少百分点打一行

        self.start_time = time.time()
        self.tty = bool(getattr(self.stream, 'isatty', lambda: False)())
        self._done = 0
        self._last_render = 0.0
        self._last_bucket = -1
        self._max_len = 0
        self._rendered_done = -1
        self._closed = False

    # ---------- 对外 ----------

    def update(self, done: int, suffix: str = '', force: bool = False) -> None:
        self._done = max(int(done), 0)
        if not self.enabled or self._closed:
            return
        if not force and not self._should_render():
            return
        self._write(self._render(suffix))

    def advance(self, step: int = 1, suffix: str = '') -> None:
        self.update(self._done + step, suffix)

    def close(self, suffix: str = '') -> None:
        if self._closed:
            return
        # 非 tty 下最后一行已经打过就不再重复
        if self.enabled and (self.tty or self._rendered_done != self._done):
            self._write(self._render(suffix), final=True)
        self._closed = True

    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time

    # ---------- 内部 ----------

    def _should_render(self) -> bool:
        now = time.time()
        if self._done >= self.total:
            return True
        if self.tty:
            if now - self._last_render < self.min_interval:
                return False
            self._last_render = now
            return True

        bucket = int(self._percent() // self.step_percent)
        if bucket <= self._last_bucket:
            return False
        self._last_bucket = bucket
        self._last_render = now
        return True

    def _percent(self) -> float:
        if not self.total:
            return 100.0
        return min(100.0 * self._done / self.total, 100.0)

    def _eta(self) -> float:
        if self._done <= 0 or self._done >= self.total:
            return 0.0
        return self.elapsed / self._done * (self.total - self._done)

    def _render(self, suffix: str) -> str:
        pct = self._percent()
        filled = int(self.BAR_WIDTH * pct / 100)
        bar = '#' * filled + '-' * (self.BAR_WIDTH - filled)

        parts = []
        if self.prefix:
            parts.append(self.prefix)
        parts.append(f'[{bar}]')
        parts.append(f'{pct:5.1f}%')
        parts.append(f'{self._done}/{self.total}')
        parts.append(f'已用 {format_duration(self.elapsed)}')
        if self._done < self.total:
            parts.append(f'剩余 {format_duration(self._eta())}')
        if suffix:
            parts.append(str(suffix))
        return ' '.join(parts)

    def _write(self, line: str, final: bool = False) -> None:
        self._rendered_done = self._done
        self._max_len = max(self._max_len, len(line))
        if self.tty:
            self.stream.write('\r' + line.ljust(self._max_len))
            if final:
                self.stream.write('\n')
        else:
            self.stream.write(line + '\n')
        try:
            self.stream.flush()
        except Exception:
            pass


# ======================================================================
# config.py
# ======================================================================

"""策略参数。数值全部取自原 QMT 回测脚本 dl_strategy.py，未做调整。"""


# ------------------------------------------------------------
# 概念板块列表（原脚本中的完整列表）
# ------------------------------------------------------------
CONCEPT_SECTORS_FULL: Tuple[str, ...] = (
    '锂电池', '芯片', '人工智能', '光伏', '军工', '新能源车', '储能',
    '5G', '半导体', '国产软件', '云计算', '大数据', '物联网', '机器人',
    '氢能源', '风能', '核电', '特高压', '充电桩', '智能电网', '工业互联网',
    '数字货币', '区块链', '元宇宙', 'VR', '消费电子', '汽车电子', '无人驾驶',
    '高端装备', '新材料', '稀土永磁', '石墨烯', '碳纤维', '降解塑料',
    '医美', '创新药', '生物疫苗', '基因测序', '医疗器械', '中药',
    '白酒', '食品饮料', '免税', '电商', '网红经济', '在线教育',
    '卫星导航', '大飞机', '军民融合', '一带一路', '雄安新区', '海南自贸',
    '碳中和', '环保', '固废处理', '污水处理', '垃圾分类',
    '网络安全', '信创', '东数西算', '量子科技', '脑机接口',
)

# 原脚本最后一行把板块覆盖成全市场：CONCEPT_SECTORS = ['沪深a股']
# xtdata 中板块名为 '沪深A股'（大写 A），这里沿用同一含义
CONCEPT_SECTORS_DEFAULT: Tuple[str, ...] = ('沪深A股',)

# 日线字段
DAILY_FIELDS = ('open', 'high', 'low', 'close', 'preClose', 'volume', 'suspendFlag')


@dataclass
class StrategyConfig:
    """选股 / 择时 / 风控参数"""

    # 股票池
    concept_sectors: Tuple[str, ...] = CONCEPT_SECTORS_DEFAULT
    filter_market_cap: bool = True         # 是否启用市值过滤
    min_market_cap: float = 30e8           # 市值下限
    max_market_cap: float = 500e8          # 市值上限
    exclude_st: bool = True
    # 原脚本中被注释掉的创业板 / 科创板剔除开关
    exclude_gem: bool = False              # 300 / 301
    exclude_star: bool = False             # 688

    # 动量打分
    lookback_days: int = 5                 # 原脚本 LOOKBACK_DAYS = 5（注释里另有 29）
    trading_days_per_year: int = 244

    # 动量分数序列长度（原脚本固定取 5，再补最新 1 个，共 6 个）
    score_series_len: int = 5

    # ---- 组合构建（默认等同原策略：满仓单票）----
    # 同时持有的标的数，等权分配。1 = 原策略的满仓单票
    max_positions: int = 1
    # 仓位系数：投入资金 = 总资产 × 该系数，其余留现金。
    # 满仓(1.0) 对这个策略是严重过度下注 —— 实测 μ=0.38%/天、σ=8.2%/天，
    # 凯利最优 k*=μ/σ²≈0.56，且左侧比右侧安全，建议 0.3~0.4
    position_ratio: float = 1.0

    # 择时信号的判断对象。False = 原策略：判断「当天新选出的候选股」，
    # 而候选股是当天分数最高的那只，序列几乎必然上升，导致 SELL 几乎不触发；
    # True = 判断「当前持仓股」，连续下降才真正成为止盈/止损机制
    timing_on_holdings: bool = False

    # 一字涨停拦截用哪个价。'low' = 原脚本：当日最低价（未来函数，
    # 实际只拦住全天封板，放过了开盘涨停、盘中打开的票）；
    # 'open' = 开盘价，开盘时点已知
    limit_up_block_field: str = 'low'

    # 择时：动量分数连续下降达到该天数则清仓
    decline_days_to_sell: int = 2
    # 判定「下降」的最小幅度。0.0 = 与原脚本一致的严格比较；
    # 分数几乎相等时（例如价格走势非常接近完美指数增长），
    # 严格比较会把 1e-13 级别的浮点噪声当成下降，可设一个相对容差规避。
    decline_epsilon: float = 0.0

    # RSRS（仅打印，不参与决策，与原脚本一致）
    rsrs_n: int = 21
    rsrs_m: int = 600
    rsrs_index: str = '000300.SH'
    rsrs_enabled: bool = True

    # 候选股跌停过滤。
    # 注意这是未来函数：下单在当日开盘，而跌停要用当日收盘价才能确认。
    # 默认关闭；置 True 可复现原脚本的口径，用于对比两种假设下的差别。
    filter_limit_down: bool = False
    # 跌停幅度：0 表示按代码前缀区分（主板 10%、创业板/科创板 20%）；
    # >0 表示固定比例，原脚本恒用 0.10，会把跌 10% 的创业板票误判为跌停
    limit_down_ratio: float = 0.0

    # 停牌的票是否允许卖出。原脚本卖出端不查停牌，会按前收填充价成交；
    # 置 True 复刻该行为
    allow_sell_suspended: bool = False

    # 是否跳过回测区间开头的 warmup_days 个交易日不交易
    # （原脚本 bar_count < LOOKBACK_DAYS + 10 时直接 return）
    skip_warmup_bars: bool = False

    # 风控
    stop_loss_ratio: float = -0.15         # 固定硬止损线

    # 预热：原脚本 bar_count < LOOKBACK_DAYS + 10 时不交易
    warmup_days: int = 0                   # 0 表示按 lookback_days + 10 自动计算

    def __post_init__(self):
        if self.warmup_days <= 0:
            self.warmup_days = self.lookback_days + 10
        if self.max_positions < 1:
            raise ValueError('max_positions 至少为 1')
        if not 0 < self.position_ratio <= 1:
            raise ValueError('position_ratio 必须在 (0, 1] 之间')
        if self.limit_up_block_field not in ('low', 'open'):
            raise ValueError("limit_up_block_field 只能是 'low' 或 'open'")

    @property
    def bars_needed_for_rank(self) -> int:
        """排序打分需要的 K 线根数（多取 1 根用于排除当前 bar）"""
        return self.lookback_days + 2

    @property
    def bars_needed_for_series(self) -> int:
        return self.lookback_days + self.score_series_len + 2

    @property
    def bars_needed_for_rsrs(self) -> int:
        return self.rsrs_m + self.rsrs_n + 2


@dataclass
class AccountConfig:
    """模拟账户参数"""

    init_cash: float = 200000.0

    # 交易费用（A 股现行规则）
    commission_rate: float = 1e-4          # 佣金万 1，买卖双边
    min_commission: float = 5.0            # 佣金单笔最低 5 元
    transfer_fee_rate: float = 1e-5        # 过户费千分之 0.01，买卖双边
    stamp_tax_rate: float = 5e-4           # 印花税千分之 0.5，仅卖出

    lot_size: int = 100                    # 一手股数
    t_plus_one: bool = True                # 当日买入次日才可卖
    # 买入数量是否预留手续费。原脚本直接 int(可用资金/价格/100)*100，
    # 不留费用，置 False 复刻该行为
    reserve_fee_on_buy: bool = True

    @property
    def buy_cost_rate(self) -> float:
        """买入的比例费用合计（不含最低佣金）"""
        return self.commission_rate + self.transfer_fee_rate

    @property
    def sell_cost_rate(self) -> float:
        """卖出的比例费用合计（不含最低佣金）"""
        return self.commission_rate + self.transfer_fee_rate + self.stamp_tax_rate


@dataclass
class BacktestConfig:
    """回测运行参数"""

    start_date: str = '20240101'
    end_date: str = '20241231'
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    account: AccountConfig = field(default_factory=AccountConfig)


# 优化版预设：基于 2020-2026 实测数据的诊断（波动损耗 σ²/2 吃掉算术收益的 90%）
#   - 降仓位：凯利最优 k*≈0.56，取更保守的 0.35（左侧比右侧安全）
#   - 分散持仓：组合方差 σ²(1/N + (1-1/N)ρ)，N=5 时显著降波动
#   - 择时盯持仓：原逻辑判断候选股，SELL 只占 3%，形同虚设
#   - 回看 29 天：原脚本注释里的值，5 天是追一周爆发、波动的主要来源
#   - 一字涨停按开盘价拦截：去掉最后一处未来函数
OPTIMIZED_PRESET = {
    'max_positions': 5,
    'position_ratio': 0.35,
    'timing_on_holdings': True,
    'limit_up_block_field': 'open',
    'lookback_days': 29,
}


# ======================================================================
# indicators.py
# ======================================================================

"""指标计算：一元线性回归、对数价格动量分数、RSRS 修正标准分。"""




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


# ======================================================================
# panel.py
# ======================================================================

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


# ======================================================================
# broker.py
# ======================================================================

"""
模拟撮合账户。

xtdata 只提供行情，没有交易接口，所以原脚本里的
get_trade_detail_data / passorder 由这里替代：
  - T+1：当日买入次日才计入可用数量
  - 佣金按成交额收取，单笔有最低值；卖出额外收印花税
  - 以给定价格立即全额成交，不模拟盘口冲击和滑点
"""



log = logging.getLogger(__name__)

BUY = 1
SELL = -1


@dataclass
class Position:
    """持仓。open_price 对应 QMT 的 m_dOpenPrice，can_use 对应 m_nCanUseVolume"""

    stock: str
    volume: int
    open_price: float
    can_use: int = 0


@dataclass
class Deal:
    """成交记录。realized_pnl 仅卖出时有意义（已扣除本次费用）"""

    date: str
    stock: str
    direction: int
    price: float
    volume: int
    fee: float
    msg: str = ''
    realized_pnl: float = 0.0


@dataclass
class SimAccount:
    cfg: AccountConfig = field(default_factory=AccountConfig)
    cash: float = 0.0
    positions: Dict[str, Position] = field(default_factory=dict)
    deals: List[Deal] = field(default_factory=list)

    def __post_init__(self):
        if self.cash <= 0:
            self.cash = float(self.cfg.init_cash)

    # ---------- 查询 ----------

    @property
    def init_cash(self) -> float:
        return float(self.cfg.init_cash)

    def get_positions(self) -> List[Position]:
        return [p for p in self.positions.values() if p.volume > 0]

    def holdings_can_use(self) -> Dict[str, int]:
        """{股票: 可卖数量}"""
        return {s: p.can_use for s, p in self.positions.items() if p.can_use > 0}

    def settle_open(self) -> None:
        """每个交易日开盘前调用：解冻昨日买入（T+1）"""
        for pos in self.positions.values():
            pos.can_use = pos.volume

    def total_asset(self, price_map: Optional[Dict[str, float]] = None) -> float:
        price_map = price_map or {}
        market_value = 0.0
        for stock, pos in self.positions.items():
            price = price_map.get(stock) or pos.open_price
            market_value += price * pos.volume
        return self.cash + market_value

    # ---------- 费用 ----------

    def commission(self, amount: float) -> float:
        """佣金：按成交额比例收取，单笔不足最低值时按最低值"""
        return max(amount * self.cfg.commission_rate, self.cfg.min_commission)

    def buy_fee(self, amount: float) -> float:
        """买入：佣金 + 过户费"""
        return self.commission(amount) + amount * self.cfg.transfer_fee_rate

    def sell_fee(self, amount: float) -> float:
        """卖出：佣金 + 过户费 + 印花税"""
        return (self.commission(amount)
                + amount * self.cfg.transfer_fee_rate
                + amount * self.cfg.stamp_tax_rate)

    def affordable_volume(self, price: float, budget: Optional[float] = None) -> int:
        """
        按可用资金算出能买的最大整手数量。budget 给定时再受该预算限制
        （多标的等权时用来给每个仓位分配额度）。

        先用比例费用估一个上界，再逐手回退到「成交额 + 实际费用 <= 可用资金」，
        这样最低佣金（小额下单时费用远高于比例值）也能被正确预留。
        """
        if price <= 0:
            return 0

        limit = self.cash if budget is None else min(self.cash, max(budget, 0.0))
        lot = self.cfg.lot_size
        if not self.cfg.reserve_fee_on_buy:
            # 原脚本口径：不预留费用，直接按可用资金整除
            return int(limit / price / lot) * lot

        raw = limit / (price * (1 + self.cfg.buy_cost_rate))
        volume = int(raw / lot) * lot

        while volume >= lot:
            amount = price * volume
            if amount + self.buy_fee(amount) <= limit + 1e-6:
                return volume
            volume -= lot
        return 0

    # ---------- 交易 ----------

    def buy(self, date: str, stock: str, price: float, volume: int, msg: str = '') -> bool:
        if volume <= 0 or price <= 0:
            return False

        amount = price * volume
        fee = self.buy_fee(amount)
        if amount + fee > self.cash + 1e-6:
            log.warning('资金不足，买入失败 %s 需要:%.2f 可用:%.2f', stock, amount + fee, self.cash)
            return False

        self.cash -= amount + fee
        pos = self.positions.get(stock)
        if pos is None:
            pos = Position(stock, volume, price)
            self.positions[stock] = pos
        else:
            pos.open_price = (pos.open_price * pos.volume + amount) / (pos.volume + volume)
            pos.volume += volume
        if not self.cfg.t_plus_one:
            pos.can_use = pos.volume

        self.deals.append(Deal(date, stock, BUY, price, volume, fee, msg))
        log.info('买入成交 %s 价格:%.2f 数量:%d 费用:%.2f 余额:%.2f',
                 stock, price, volume, fee, self.cash)
        return True

    def sell(self, date: str, stock: str, price: float, volume: int, msg: str = '') -> bool:
        pos = self.positions.get(stock)
        if pos is None or volume <= 0 or price <= 0:
            return False

        volume = min(volume, pos.can_use)
        if volume <= 0:
            log.info('%s 无可用数量，卖出跳过（T+1）', stock)
            return False

        amount = price * volume
        fee = self.sell_fee(amount)
        realized = (price - pos.open_price) * volume - fee

        self.cash += amount - fee
        pos.volume -= volume
        pos.can_use -= volume
        if pos.volume <= 0:
            del self.positions[stock]

        self.deals.append(Deal(date, stock, SELL, price, volume, fee, msg, realized))
        log.info('卖出成交 %s 价格:%.2f 数量:%d 费用:%.2f 盈亏:%.2f 余额:%.2f',
                 stock, price, volume, fee, realized, self.cash)
        return True

    def sell_all(self, date: str, stock: str, price: float, msg: str = '') -> bool:
        pos = self.positions.get(stock)
        if pos is None:
            return False
        return self.sell(date, stock, price, pos.can_use, msg)


# ======================================================================
# datasource.py
# ======================================================================

"""
行情数据源。

DataSource 定义策略需要的最小接口，策略层只依赖这个接口：
  - XtDataSource  : 生产环境，走 xtquant.xtdata（需要本机 QMT / 投研端在线）
  - CsvDataSource : 离线环境，从 csv 目录读数据，用于单元测试和无 QMT 时试跑

约定：
  - 日期一律用 'YYYYMMDD' 字符串
  - get_bars 返回 {stock: DataFrame}，DataFrame 按日期升序，index 为日期字符串
  - 查询区间是「截至 end_date（含）的最后 count 根」
"""




# 单次 get_market_data_ex 的标的数量上限。一次性请求几千只容易超时或静默返回空表。
PRELOAD_CHUNK_SIZE = 300

# 所有 QMT 版本都支持的字段，作为 suspendFlag 不可用时的退路
CORE_FIELDS = ('open', 'high', 'low', 'close', 'preClose', 'volume')

# 复权方式。默认后复权：
#   - 动量打分对对数价格做回归，除权跳空落在窗口内会被当成真实下跌，
#     一次 2% 的现金分红经 244 天年化放大后能让分数只剩原来的 23%
#   - 后复权的复权因子只由当日之前的除权事件决定，且不会被之后的分红改写，
#     不含未来函数（前复权以最新日为锚，历史价格会被未来的分红改写）
DIVIDEND_TYPES = ('none', 'front', 'back', 'front_ratio', 'back_ratio')
DEFAULT_DIVIDEND_TYPE = 'back'

NO_DATA_HINT = """
[数据] xtdata 没有返回任何日线数据，请按以下顺序排查：
  1. QMT / 投研端客户端是否已启动并登录（xtdata 只读本机客户端的数据缓存）
  2. 本地是否下载过日线 —— 加 --download 重跑，或在客户端
     「行情 -> 数据管理 / 数据下载」里补充日线数据
  3. 运行 python run_backtest.py --check-data 逐步定位到底哪一步取不到数
"""

# 复权因子是独立的一份数据（除权除息），不随日线一起下载。
# 缺它时 get_market_data_ex(dividend_type='back'/'front') 会直接返回空表，
# 表象和"没下载日线"一模一样，所以要单独识别并给出正确的处理办法。
DIVIDEND_PERIOD = 'divid_factors'

MISSING_DIVIDEND_HINT = """
[数据] 日线数据是有的，但按「{mode}」复权取不到 —— 缺的是除权除息因子。

  复权因子（{period}）是独立于日线的一份数据，不随日线一起下载，
  所以昨天下过日线、今天换成复权口径依然会取不到。

  处理办法（任选其一）：
    1. 加 --download 重跑，脚本会连同除权除息因子一起补下载
    2. 在 QMT 客户端「行情 -> 数据管理」里补充除权除息数据
    3. 先用 --dividend-type none 跑不复权（注意：除权跳空会被当成真实下跌，
       分红股的动量分数会被严重压低）
"""


class DataSource(object):
    """数据源接口"""

    def get_sector_stocks(self, sector: str) -> List[str]:
        raise NotImplementedError

    def get_trading_dates(self, end_date: str) -> List[str]:
        """返回截至 end_date 的全部交易日（升序）"""
        raise NotImplementedError

    def get_bars(self, stocks: Sequence[str], end_date: str, count: int,
                 fields: Optional[Sequence[str]] = None) -> Dict[str, pd.DataFrame]:
        raise NotImplementedError

    def get_detail(self, stock: str) -> Optional[dict]:
        raise NotImplementedError

    def preload(self, stocks: Sequence[str], start_date: str, end_date: str) -> None:
        """可选：批量预加载到内存"""
        return None

    def download(self, stocks: Sequence[str], start_date: str, end_date: str) -> None:
        """可选：补下载本地数据"""
        return None

    # ---------- 基于 get_bars / get_detail 的通用便捷方法 ----------

    def get_one(self, stock: str, end_date: str, count: int,
                fields: Optional[Sequence[str]] = None) -> Optional[pd.DataFrame]:
        df = self.get_bars([stock], end_date, count, fields).get(stock)
        if df is None or len(df) == 0:
            return None
        return df

    def has_data(self, stocks: Sequence[str], end_date: str, sample: int = 20) -> bool:
        """抽样检查数据源在 end_date 之前是否有日线数据"""
        probe = list(stocks)[:sample]
        if not probe:
            return False
        data = self.get_bars(probe, end_date, 1)
        return any(df is not None and len(df) > 0 for df in data.values())

    def get_stock_name(self, stock: str) -> str:
        detail = self.get_detail(stock)
        if not detail:
            return ''
        return detail.get('InstrumentName', '') or ''

    def get_market_cap(self, stock: str, last_close: float = 0.0) -> float:
        """
        总市值。优先取 TotalValue；缺失时用总股本 × 最新收盘价估算。
        不同 QMT 版本总股本字段名不一致，依次尝试。
        """
        detail = self.get_detail(stock)
        if not detail:
            return 0.0

        total_value = detail.get('TotalValue', 0) or 0
        if total_value > 0:
            return float(total_value)

        for key in ('TotalVolume', 'TotalVolumn', 'TotalShares'):
            shares = detail.get(key, 0) or 0
            if shares > 0 and last_close > 0:
                return float(shares) * float(last_close)
        return 0.0


def _normalize(df: pd.DataFrame, end_date: str, count: int) -> pd.DataFrame:
    """index 统一成日期字符串，按 end_date 截断并取最后 count 根"""
    out = df.copy()
    out.index = [str(i)[:8] for i in out.index]
    out = out.loc[out.index <= end_date]
    if count and count > 0:
        out = out.tail(count)
    return out


class XtDataSource(DataSource):
    """
    xtquant.xtdata 数据源。

    use_cache=True 时 preload 会把整个回测区间的日线一次性读进内存，
    之后按日期切片，避免逐个交易日对全市场重复调用 get_market_data_ex。
    缓存未命中的标的会自动回落到实时查询。
    """

    def __init__(self, use_cache: bool = True, market: str = 'SH',
                 dividend_type: str = DEFAULT_DIVIDEND_TYPE):
        from xtquant import xtdata  # 延迟导入：没有 QMT 的机器也能 import 本模块

        if dividend_type not in DIVIDEND_TYPES:
            raise ValueError(f'不支持的复权方式 {dividend_type}，可选 {DIVIDEND_TYPES}')

        self._xtdata = xtdata
        self.use_cache = use_cache
        self.market = market
        self.dividend_type = dividend_type
        self.fields = tuple(DAILY_FIELDS)
        self._cache: Dict[str, pd.DataFrame] = {}
        self._detail_cache: Dict[str, Optional[dict]] = {}

    # ---------- 板块 / 日历 ----------

    def get_sector_stocks(self, sector: str) -> List[str]:
        try:
            return list(self._xtdata.get_stock_list_in_sector(sector) or [])
        except Exception as e:
            print(f'[数据] 板块 {sector} 获取失败: {e}')
            return []

    def get_trading_dates(self, end_date: str) -> List[str]:
        timetags = self._xtdata.get_trading_dates(self.market, start_time='',
                                                  end_time=end_date, count=-1)
        days = [self._xtdata.timetag_to_datetime(t, '%Y%m%d') for t in timetags]
        return [d for d in days if d <= end_date]

    # ---------- 行情 ----------

    def _raw_fetch(self, fields, stocks, start_date='', end_date='', count=-1):
        """直接调用 get_market_data_ex，异常时返回空 dict"""
        try:
            data = self._xtdata.get_market_data_ex(
                list(fields), list(stocks),
                period='1d',
                start_time=start_date,
                end_time=end_date,
                count=count,
                dividend_type=self.dividend_type,
                fill_data=True,
            )
        except Exception as e:
            print(f'[数据] get_market_data_ex 调用失败: {e}')
            return {}
        return data or {}

    @staticmethod
    def _any_rows(data) -> bool:
        return any(df is not None and len(df) > 0 for df in data.values())

    def probe_dividend(self, sample_stocks, start_date='', end_date='') -> bool:
        """
        当前复权方式取不到数、但不复权能取到时，说明缺的是除权除息因子。
        返回 True 表示确认是复权因子缺失（已打印提示）。
        """
        if self.dividend_type == 'none':
            return False

        sample = list(sample_stocks)[:3]
        saved, self.dividend_type = self.dividend_type, 'none'
        try:
            has_raw = self._any_rows(self._raw_fetch(CORE_FIELDS, sample, start_date, end_date))
        finally:
            self.dividend_type = saved

        if has_raw:
            print(MISSING_DIVIDEND_HINT.format(mode=self.dividend_type,
                                               period=DIVIDEND_PERIOD))
            return True
        return False

    def resolve_fields(self, sample_stocks, start_date='', end_date='') -> bool:
        """
        用少量标的探测可用字段。

        某些 QMT 版本不支持 suspendFlag，整批请求会直接返回空表，
        这里探测失败就退回核心字段，仍然为空则判定为本地无数据。
        """
        sample = list(sample_stocks)[:3]
        if not sample:
            return False

        for fields in (self.fields, CORE_FIELDS):
            if self._any_rows(self._raw_fetch(fields, sample, start_date, end_date)):
                if tuple(fields) != tuple(self.fields):
                    print(f'[数据] 字段 {sorted(set(self.fields) - set(fields))} 不可用，改用核心字段')
                    self.fields = tuple(fields)
                return True
        return False

    def preload(self, stocks, start_date, end_date, show_progress: bool = True):
        if not self.use_cache or not stocks:
            return

        stocks = list(dict.fromkeys(stocks))
        print(f'[数据] 预加载 {len(stocks)} 只标的 {start_date} ~ {end_date}'
              f'（复权: {self.dividend_type}）...')

        if not self.resolve_fields(stocks, start_date, end_date):
            if not self.probe_dividend(stocks, start_date, end_date):
                print(NO_DATA_HINT)
            return

        loaded = 0
        processed = 0
        total_chunks = (len(stocks) + PRELOAD_CHUNK_SIZE - 1) // PRELOAD_CHUNK_SIZE
        bar = Progress(len(stocks), prefix='[数据] 预加载',
                       enabled=show_progress and total_chunks > 1)

        for idx in range(total_chunks):
            chunk = stocks[idx * PRELOAD_CHUNK_SIZE:(idx + 1) * PRELOAD_CHUNK_SIZE]
            data = self._raw_fetch(self.fields, chunk, start_date, end_date)
            for stock in chunk:
                df = data.get(stock)
                if df is None or len(df) == 0:
                    continue
                df = df.copy()
                df.index = [str(i)[:8] for i in df.index]
                self._cache[stock] = df
                loaded += 1
            processed += len(chunk)
            bar.update(processed, suffix=f'已加载 {loaded} 只')
        bar.close()

        print(f'[数据] 预加载完成，{loaded} 只有数据')
        if loaded == 0:
            print(NO_DATA_HINT)

    def get_bars(self, stocks, end_date, count, fields=None):
        if isinstance(stocks, str):
            stocks = [stocks]
        fields = list(fields or self.fields)

        result: Dict[str, pd.DataFrame] = {}
        missing: List[str] = []

        if self.use_cache:
            for stock in stocks:
                cached = self._cache.get(stock)
                if cached is None:
                    missing.append(stock)
                    continue
                sub = _normalize(cached, end_date, count)
                if len(sub) > 0:
                    result[stock] = sub
            if not missing:
                return result
        else:
            missing = list(stocks)

        data = self._raw_fetch(fields, missing, end_date=end_date, count=count)

        for stock in missing:
            df = data.get(stock)
            if df is None or len(df) == 0:
                continue
            result[stock] = _normalize(df, end_date, count)
        return result

    # ---------- 合约信息 ----------

    def get_detail(self, stock):
        if stock in self._detail_cache:
            return self._detail_cache[stock]
        try:
            detail = self._xtdata.get_instrument_detail(stock)
        except Exception:
            detail = None
        self._detail_cache[stock] = detail
        return detail

    def download(self, stocks, start_date, end_date, show_progress: bool = True):
        """
        补下载本地数据：日线 + 除权除息因子。

        复权价要靠除权除息因子算，这份数据不随日线一起下载，
        只下日线的话换成复权口径依然取不到数。
        """
        stocks = list(dict.fromkeys(stocks))
        if not stocks:
            return

        self._download_period(stocks, '1d', '日线', start_date, end_date, show_progress)
        if self.dividend_type != 'none':
            self.download_dividend_factors(stocks, start_date, end_date, show_progress)

    def download_dividend_factors(self, stocks: Sequence[str], start_date: str,
                                  end_date: str, show_progress: bool = True) -> None:
        """补下载除权除息因子（复权价的来源）"""
        stocks = list(dict.fromkeys(stocks))
        if not stocks:
            return
        self._download_period(stocks, DIVIDEND_PERIOD, '除权除息因子',
                              start_date, end_date, show_progress)

    def _download_period(self, stocks: Sequence[str], period: str, label: str,
                         start_date: str, end_date: str, show_progress: bool = True):
        """下载某个周期的数据：优先批量+进度回调，不支持则逐级降级"""
        print(f'[数据] 开始下载{label}: {len(stocks)} 只，{start_date} ~ {end_date}')
        bar = Progress(len(stocks), prefix=f'[数据] {label}', enabled=show_progress)

        def _callback(data):
            """xtdata 回调，data 形如 {'finished': n, 'total': m, 'stockcode': '600000.SH'}"""
            try:
                total = int(data.get('total') or 0)
                finished = int(data.get('finished') or 0)
            except (AttributeError, TypeError, ValueError):
                return
            if total > 0:
                bar.total = total
            bar.update(finished, suffix=str(data.get('stockcode') or ''))

        batch = getattr(self._xtdata, 'download_history_data2', None)
        if batch is not None:
            try:
                batch(stocks, period=period, start_time=start_date,
                      end_time=end_date, callback=_callback)
                bar.close()
                print(f'[数据] {label}下载完成')
                return
            except TypeError:
                # 该版本的 download_history_data2 不接受 callback
                try:
                    batch(stocks, period=period, start_time=start_date, end_time=end_date)
                    bar.update(len(stocks))
                    bar.close()
                    print(f'[数据] {label}下载完成（该版本不支持进度回调）')
                    return
                except Exception as e:
                    print(f'[数据] {label}批量下载失败，改为逐只下载: {e}')
            except Exception as e:
                print(f'[数据] {label}批量下载失败，改为逐只下载: {e}')

        failed = []
        for i, stock in enumerate(stocks, 1):
            try:
                self._xtdata.download_history_data(stock, period=period,
                                                   start_time=start_date, end_time=end_date)
            except Exception as e:
                failed.append((stock, str(e)))
            bar.update(i, suffix=stock)
        bar.close()

        if failed:
            print(f'[数据] {label}下载完成，{len(failed)} 只失败，例如 {failed[:3]}')
        else:
            print(f'[数据] {label}下载完成')


class CsvDataSource(DataSource):
    """
    离线 csv 数据源，目录结构：

        data_dir/
          bars/600000.SH.csv        # 列：date,open,high,low,close,preClose,volume,suspendFlag
          instruments.json          # {"600000.SH": {"InstrumentName": "浦发银行", "TotalValue": 3.2e10}}
          sectors.json              # {"沪深A股": ["600000.SH", ...]}

    用于单元测试，以及没有 QMT 环境时用自备数据跑通流程。
    """

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._cache: Dict[str, pd.DataFrame] = {}
        self._details: Dict[str, dict] = {}
        self._sectors: Dict[str, List[str]] = {}
        self._load()

    def _load(self):
        bars_dir = os.path.join(self.data_dir, 'bars')
        if os.path.isdir(bars_dir):
            for name in sorted(os.listdir(bars_dir)):
                if not name.endswith('.csv'):
                    continue
                stock = name[:-4]
                df = pd.read_csv(os.path.join(bars_dir, name), dtype={'date': str})
                df = df.set_index('date').sort_index()
                df.index = [str(i)[:8] for i in df.index]
                self._cache[stock] = df

        detail_path = os.path.join(self.data_dir, 'instruments.json')
        if os.path.isfile(detail_path):
            with open(detail_path, encoding='utf-8') as f:
                self._details = json.load(f)

        sector_path = os.path.join(self.data_dir, 'sectors.json')
        if os.path.isfile(sector_path):
            with open(sector_path, encoding='utf-8') as f:
                self._sectors = json.load(f)

    # ---------- 接口实现 ----------

    def get_sector_stocks(self, sector):
        return list(self._sectors.get(sector, []))

    def get_trading_dates(self, end_date):
        days = set()
        for df in self._cache.values():
            days.update(df.index)
        return sorted(d for d in days if d <= end_date)

    def get_bars(self, stocks, end_date, count, fields=None):
        if isinstance(stocks, str):
            stocks = [stocks]
        result = {}
        for stock in stocks:
            df = self._cache.get(stock)
            if df is None:
                continue
            sub = _normalize(df, end_date, count)
            if len(sub) > 0:
                result[stock] = sub
        return result

    def get_detail(self, stock):
        return self._details.get(stock)

    # ---------- 供测试构造数据 ----------

    @classmethod
    def from_frames(cls, frames: Dict[str, pd.DataFrame],
                    details: Optional[Dict[str, dict]] = None,
                    sectors: Optional[Dict[str, Iterable[str]]] = None) -> 'CsvDataSource':
        """直接用内存中的 DataFrame 构造数据源，不读磁盘"""
        obj = cls.__new__(cls)
        obj.data_dir = ''
        obj._cache = {}
        for stock, df in frames.items():
            d = df.copy()
            d.index = [str(i)[:8] for i in d.index]
            obj._cache[stock] = d.sort_index()
        obj._details = dict(details or {})
        obj._sectors = {k: list(v) for k, v in (sectors or {}).items()}
        return obj


# ======================================================================
# universe.py
# ======================================================================

"""步骤 1：股票池构建与过滤（停牌 / ST / 市值）。"""



log = logging.getLogger(__name__)


def build_base_pool(source: DataSource, cfg: StrategyConfig) -> List[str]:
    """
    从概念板块取原始股票池。板块成分不随日期变化，整个回测只需取一次。
    """
    pool = set()
    for sector in cfg.concept_sectors:
        stocks = source.get_sector_stocks(sector)
        if not stocks:
            log.warning('板块 %s 未取到成分股', sector)
            continue
        pool.update(stocks)

    if not pool:
        log.warning('概念板块未获取到股票')
        return []

    result = []
    for stock in sorted(pool):
        code = stock.split('.')[0]
        if not code[:1].isdigit():          # 剔除指数 / 基金等非股票代码
            continue
        if cfg.exclude_gem and code.startswith(('300', '301')):
            continue
        if cfg.exclude_star and code.startswith('688'):
            continue
        result.append(stock)
    return result


def filter_universe(source: DataSource, base_pool: Sequence[str],
                    date: str, cfg: StrategyConfig) -> List[str]:
    """
    用截至 date（含）的数据过滤股票池：
      - 停牌（suspendFlag == 1）
      - ST
      - 市值不在 [min_market_cap, max_market_cap] 区间（filter_market_cap=False 可关闭）
    取不到市值的标的不因此被剔除（与原脚本一致）。
    """
    if not base_pool:
        return []

    quotes = source.get_bars(base_pool, date, 2)

    result = []
    for stock in base_pool:
        df = quotes.get(stock)
        if df is None or len(df) < 1:
            continue

        if 'suspendFlag' in df.columns:
            try:
                if int(df['suspendFlag'].iloc[-1]) == 1:
                    continue
            except (TypeError, ValueError):
                pass

        if 'volume' in df.columns:
            try:
                if float(df['volume'].iloc[-1]) <= 0:      # 停牌日成交量为 0
                    continue
            except (TypeError, ValueError):
                pass

        last_close = float(df['close'].iloc[-1]) if 'close' in df.columns else 0.0
        if last_close <= 0:
            continue

        detail = source.get_detail(stock)
        if not detail:
            continue

        if cfg.exclude_st:
            name = (detail.get('InstrumentName', '') or '').upper()
            if 'ST' in name:
                continue

        if cfg.filter_market_cap:
            market_cap = source.get_market_cap(stock, last_close)
            if market_cap > 0 and not (cfg.min_market_cap <= market_cap <= cfg.max_market_cap):
                continue

        result.append(stock)

    return result


# ======================================================================
# selector.py
# ======================================================================

"""
步骤 2~4：动量打分排序、动量分数序列、候选股过滤。

所有取数一律多取 1 根并用 [-(n+1):-1] 切片排除当前 bar，
保证打分只用到「上一交易日及之前」的收盘价，不含未来函数。
"""




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
    picks = pick_targets(source, pool, date, cfg, 1)
    return picks[0] if picks else None


def pick_targets(source: DataSource, pool: Sequence[str], date: str,
                 cfg: StrategyConfig, count: Optional[int] = None) -> List[str]:
    """取动量分数前 count 名（默认 cfg.max_positions）"""
    count = cfg.max_positions if count is None else count
    ranked = rank_by_momentum(source, pool, date, cfg)
    if not ranked:
        return []
    log.info('Top%d: %s', min(3, len(ranked)),
             [(s, round(sc, 4)) for s, sc in ranked[:3]])
    return [s for s, _ in ranked[:max(count, 1)]]


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


def is_suspended(source: DataSource, stock: str, date: str) -> bool:
    """
    当日是否停牌 / 不可交易。

    停牌日 xtdata 在 fill_data=True 下会用前收把 K 线填满（开=高=低=收=前收、
    成交量为 0），光看价格分辨不出来，必须靠 suspendFlag 或成交量判断。
    取不到数据同样按不可交易处理。
    """
    df = source.get_one(stock, date, 1)
    if df is None or len(df) == 0:
        return True

    if 'suspendFlag' in df.columns:
        try:
            if int(df['suspendFlag'].iloc[-1]) == 1:
                return True
        except (TypeError, ValueError):
            pass

    if 'volume' in df.columns:
        try:
            if float(df['volume'].iloc[-1]) <= 0:
                return True
        except (TypeError, ValueError):
            pass

    return False


def filter_target(source: DataSource, stock: Optional[str], date: str,
                  cfg: StrategyConfig) -> Optional[str]:
    """
    剔除停牌的候选股；通过则原样返回代码。

    跌停过滤由 cfg.filter_limit_down 控制，默认关闭：下单发生在当日开盘，
    而跌停要用当日收盘价才能确认，开盘时它还不存在，拿它过滤属于未来函数。
    打开后可复现原脚本的口径。停牌是开盘前就已知的，始终过滤。
    """
    if not stock:
        return None

    if is_suspended(source, stock, date):
        log.info('%s 当日停牌', stock)
        return None

    df = source.get_one(stock, date, 1)
    if df is None:
        return None

    # 数据有效性用开盘价判断：它既是成交价，也是开盘时点就已知的值
    open_price = float(df['open'].iloc[-1]) if 'open' in df.columns else 0.0
    if not open_price > 0:
        log.info('%s 开盘价异常，跳过', stock)
        return None

    if cfg.filter_limit_down:
        last_close = float(df['close'].iloc[-1]) if 'close' in df.columns else 0.0
        pre_close = float(df['preClose'].iloc[-1]) if 'preClose' in df.columns else last_close
        ratio = cfg.limit_down_ratio if cfg.limit_down_ratio > 0 else limit_ratio(stock)
        limit_down = round(pre_close * (1 - ratio), 2)
        if last_close > 0 and last_close <= limit_down:
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


def blocked_by_limit_up(cfg: StrategyConfig, open_price: float,
                        low_price: float, limit_up: float) -> bool:
    """
    是否因涨停买不进。

    cfg.limit_up_block_field='low' 是原脚本口径：用当日最低价判断，
    因 low <= open 恒成立，它实际只拦住「全天封板」，放过了
    「开盘涨停、盘中打开」的票 —— 而那些票开盘同样买不到，属于未来函数。
    'open' 用开盘价判断，是开盘时点就已知的信息。
    """
    if limit_up <= 0:
        return False
    price = low_price if cfg.limit_up_block_field == 'low' else open_price
    return price >= limit_up


# ======================================================================
# timing.py
# ======================================================================

"""
步骤 5：择时信号。

与原脚本一致：RSRS 只计算并打印，不参与决策；
实际信号由目标股动量分数「连续下降天数」决定。
"""



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


# ======================================================================
# engine.py
# ======================================================================

"""
回测引擎：把原脚本 handlebar 里的单日流程搬过来，改为按交易日列表循环驱动。

单个交易日的顺序（与原脚本一致）：
  ① 股票池 -> 动量打分取第 1 名 -> 分数序列 -> 跌停停牌过滤 -> 择时 -> 调仓（开盘价成交）
  ② 止损检查（收盘价，触发 -15% 则清仓）
  ③ 复盘打印并记录当日总资产
"""



log = logging.getLogger(__name__)


@dataclass
class DailyRecord:
    """每个交易日收盘后的快照，用于保存回测结果"""

    date: str
    target: str = ''          # 当日选出的目标股（多标的时为第一名）
    signal: str = ''          # 择时信号
    stock: str = ''           # 收盘持仓（多标的时为第一只）
    volume: int = 0
    cost: float = 0.0
    price: float = 0.0
    market_value: float = 0.0     # 全部持仓市值
    cash: float = 0.0
    total_asset: float = 0.0
    position_count: int = 0       # 收盘持仓只数
    holdings: str = ''            # 全部持仓，形如 '600000.SH:1000;000001.SZ:2000'


@dataclass
class BacktestResult:
    account: SimAccount
    equity_curve: List[Tuple[str, float]] = field(default_factory=list)
    signals: List[Tuple[str, str, Optional[str]]] = field(default_factory=list)  # (日期, 信号, 目标股)
    daily: List[DailyRecord] = field(default_factory=list)

    @property
    def dates(self) -> List[str]:
        return [d for d, _ in self.equity_curve]

    @property
    def values(self) -> List[float]:
        return [v for _, v in self.equity_curve]


class BacktestEngine:
    def __init__(self, source: DataSource, config: BacktestConfig,
                 account: Optional[SimAccount] = None):
        self.source = source
        self.config = config
        self.cfg = config.strategy
        self.account = account or SimAccount(config.account)
        self.base_pool: List[str] = []
        self.score_series: Dict[str, List[float]] = {}
        self.today_target: Optional[str] = None

    # ---------- 交易日历 ----------

    def trading_days(self) -> Tuple[List[str], str]:
        """返回 (回测交易日列表, 含预热的数据起点日期)"""
        all_days = self.source.get_trading_dates(self.config.end_date)
        if not all_days:
            raise RuntimeError('未取到交易日历，请检查数据源（QMT 是否已启动并下载过数据）')

        run_days = [d for d in all_days if d >= self.config.start_date]
        if not run_days:
            raise RuntimeError(f'区间 {self.config.start_date} ~ {self.config.end_date} 内没有交易日')

        first = all_days.index(run_days[0])
        warm_idx = max(0, first - self.cfg.warmup_days)
        return run_days, all_days[warm_idx]

    def _warm_start(self, extra_days: int) -> str:
        all_days = self.source.get_trading_dates(self.config.end_date)
        run_days = [d for d in all_days if d >= self.config.start_date]
        if not run_days:
            return self.config.start_date
        first = all_days.index(run_days[0])
        return all_days[max(0, first - extra_days)]

    # ---------- 主流程 ----------

    def prepare(self, download: bool = False) -> List[str]:
        self.base_pool = build_base_pool(self.source, self.cfg)
        log.info('原始股票池: %d 只', len(self.base_pool))
        if not self.base_pool:
            return []

        stock_start = self._warm_start(self.cfg.warmup_days + 5)
        index_start = self._warm_start(self.cfg.bars_needed_for_rsrs + 10)

        if download:
            self.source.download(list(self.base_pool) + [self.cfg.rsrs_index],
                                 index_start, self.config.end_date)

        self.source.preload(self.base_pool, stock_start, self.config.end_date)
        if self.cfg.rsrs_enabled:
            self.source.preload([self.cfg.rsrs_index], index_start, self.config.end_date)

        if not self.source.has_data(self.base_pool, self.config.end_date):
            raise RuntimeError(
                '数据源取不到任何日线数据，回测无法开始。\n'
                '  - 若上面提示「缺的是除权除息因子」，加 --download 补下载，\n'
                '    或先用 --dividend-type none 跑不复权\n'
                '  - 否则加 --download 补下载日线，或在 QMT 客户端「行情 -> 数据管理」补充\n'
                '  - 用 python run_backtest.py --check-data 逐步定位'
            )
        return self.base_pool

    def run(self, download: bool = False, show_progress: bool = False) -> BacktestResult:
        log.info('回测区间: %s ~ %s，初始资金: %.0f',
                 self.config.start_date, self.config.end_date, self.account.init_cash)
        log.info('板块数: %d，动量回看: %d 天，止损线: %.0f%%',
                 len(self.cfg.concept_sectors), self.cfg.lookback_days,
                 self.cfg.stop_loss_ratio * 100)

        run_days, _ = self.trading_days()
        log.info('交易日: %d 个', len(run_days))

        if not self.prepare(download=download):
            raise RuntimeError('股票池为空，请检查板块名称或数据源')

        result = BacktestResult(account=self.account)
        bar = Progress(len(run_days), prefix='[回测] 交易日', enabled=show_progress)
        # 原脚本是 bar_count < LOOKBACK_DAYS + 10 时 return，
        # 即前 warmup_days - 1 根 bar 不交易，第 warmup_days 根开始交易
        skip = (self.cfg.warmup_days - 1) if self.cfg.skip_warmup_bars else 0
        if skip:
            log.info('前 %d 个交易日为预热期，不交易', skip)

        for n, date in enumerate(run_days, 1):
            if n <= skip:
                self._last_signal = ''
                self.today_target = None
                total = self.review(date)
            else:
                total = self.run_day(date)
            result.equity_curve.append((date, total))
            result.signals.append((date, self._last_signal, self.today_target))
            result.daily.append(self.daily_record(date, total))
            bar.update(n, suffix=date)
        bar.close()
        return result

    def daily_record(self, date: str, total: float) -> DailyRecord:
        """收盘快照。策略最多持有 1 只，取第一只持仓即可"""
        record = DailyRecord(date=date, target=self.today_target or '',
                             signal=self._last_signal, cash=self.account.cash,
                             total_asset=total)
        positions = self.account.get_positions()
        record.position_count = len(positions)
        record.holdings = ';'.join(f'{p.stock}:{p.volume}' for p in positions)
        for i, pos in enumerate(positions):
            price = self._last_price_map.get(pos.stock, pos.open_price)
            record.market_value += price * pos.volume
            if i == 0:
                record.stock = pos.stock
                record.volume = pos.volume
                record.cost = pos.open_price
                record.price = price
        return record

    # ---------- 单个交易日 ----------

    _last_signal = ''
    _last_price_map: Dict[str, float] = {}

    def run_day(self, date: str) -> float:
        log.info('=' * 56)
        log.info('交易日: %s', date)

        self.account.settle_open()
        self._last_signal = ''
        self.today_target = None

        pool = filter_universe(self.source, self.base_pool, date, self.cfg)
        if not pool:
            log.info('股票池为空，跳过今日')
            return self.review(date)
        log.info('步骤1 - 股票池: %d 只', len(pool))

        picks = pick_targets(self.source, pool, date, self.cfg)
        if not picks:
            log.info('步骤2 - 未选出目标股票')
            return self.review(date)
        log.info('步骤2 - 目标: %s', [f'{p} {self.source.get_stock_name(p)}' for p in picks])

        self.score_series = {p: momentum_series(self.source, p, date, self.cfg)
                             for p in picks}
        log.info('步骤3 - 首选动量分数序列: %s',
                 [round(float(x), 4) for x in self.score_series.get(picks[0], [])])

        targets = [p for p in picks if filter_target(self.source, p, date, self.cfg)]
        if not targets:
            log.info('步骤4 - 目标股票全部被过滤')
            return self.review(date)
        self.today_target = targets[0]
        log.info('步骤4 - 过滤通过: %s', targets)

        rsrs = rsrs_value(self.source, date, self.cfg)
        log.info('RSRS修正标准分: %s', f'{rsrs:.4f}' if rsrs is not None else '数据不足')

        signal, to_sell = self.resolve_signals(date, targets)
        self._last_signal = signal
        log.info('步骤5 - 择时信号: %s%s', signal,
                 f'  待卖出: {sorted(to_sell)}' if to_sell else '')

        # 原策略口径下 SELL 意味着「清仓且当日不再买入」，保持该语义；
        # 其余情况统一走组合调仓（max_positions=1 时与原逻辑完全等价）
        if not self.cfg.timing_on_holdings and signal == SIGNAL_SELL:
            self.adjust_portfolio(date, [], to_sell=to_sell, allow_buy=False)
        else:
            keep = [t for t in targets if t not in to_sell]
            self.adjust_portfolio(date, keep, to_sell=to_sell)
        log.info('步骤6 - 调仓执行完毕')

        self.check_stop_loss(date)
        return self.review(date)

    def resolve_signals(self, date: str, targets: Sequence[str]):
        """
        返回 (用于记录的整体信号, 需要卖出的持仓集合)。

        timing_on_holdings=False 是原策略口径：只看「当天选出的候选股」的
        分数序列 —— 而候选股是当天分数最高的那只，序列几乎必然上升，
        所以 SELL 几乎不触发（实测 1627 天里只有 53 天）。
        timing_on_holdings=True 时逐只判断当前持仓，连续下降的才卖。
        """
        if not self.cfg.timing_on_holdings:
            scores = self.score_series.get(targets[0], [])
            signal = timing_signal(scores, self.cfg)
            holdings = set(self.account.holdings_can_use())
            return signal, (holdings if signal == SIGNAL_SELL else set())

        to_sell = set()
        for stock in self.account.holdings_can_use():
            series = momentum_series(self.source, stock, date, self.cfg)
            self.score_series[stock] = series
            if series and timing_signal(series, self.cfg) == SIGNAL_SELL:
                to_sell.add(stock)

        if to_sell:
            return SIGNAL_SELL, to_sell
        return (SIGNAL_BUY if targets else SIGNAL_KEEP), to_sell

    # ---------- 步骤 6：调仓 ----------

    def adjust_position(self, date: str, target: str, signal: str) -> None:
        """
        单标的调仓（原策略口径），保留为组合调仓的薄封装：
          SELL     -> 清仓，且当日不再买入
          BUY/KEEP -> 已持有目标股则持有，否则先卖旧再买新
        """
        if signal == SIGNAL_SELL:
            holdings = set(self.account.holdings_can_use())
            self.adjust_portfolio(date, [], to_sell=holdings, allow_buy=False)
            return
        self.adjust_portfolio(date, [target], to_sell=set())

    def adjust_portfolio(self, date: str, targets: Sequence[str],
                         to_sell: Optional[set] = None,
                         allow_buy: bool = True) -> None:
        """
        组合调仓：卖掉「不在目标里」或「被择时判定卖出」的持仓，
        再把目标补齐到 max_positions 只等权。

        每个仓位的额度 = 开盘时点总资产 × position_ratio / max_positions，
        额度在卖出之前算好，避免当天卖出的钱被重复计入额度。
        """
        to_sell = set(to_sell or ())
        targets = [t for t in targets if t]
        holdings = self.account.holdings_can_use()
        log.info('当前可用持仓: %s  目标: %s', holdings, list(targets))

        budget = self.slot_budget(date)

        for stock in list(holdings):
            if stock in to_sell:
                self._sell_at_open(date, stock, f'择时卖出 {stock}')
            elif stock not in targets:
                self._sell_at_open(date, stock, f'切换标的 卖出 {stock}')

        if not allow_buy:
            return

        held = set(self.account.positions)
        for target in targets:
            if target in held:
                log.info('KEEP: 继续持有 %s', target)
                continue
            self._buy_at_open(date, target, budget)

    def slot_budget(self, date: str) -> float:
        """单个仓位的资金额度（按开盘时点的总资产算）"""
        equity = self.account.cash
        for pos in self.account.get_positions():
            df = self.source.get_one(pos.stock, date, 1)
            price = float(df['open'].iloc[-1]) if df is not None else pos.open_price
            if not price > 0:
                price = pos.open_price
            equity += price * pos.volume
        return equity * self.cfg.position_ratio / self.cfg.max_positions

    def _buy_at_open(self, date: str, target: str, budget: float) -> None:
        open_price, limit_up, limit_down, low_price = price_and_limits(
            self.source, target, date)
        log.info('%s 开盘:%.2f 涨停:%.2f 跌停:%.2f 最低:%.2f',
                 target, open_price, limit_up, limit_down, low_price)

        if open_price <= 0:
            log.warning('%s 价格异常: %s', target, open_price)
            return

        if blocked_by_limit_up(self.cfg, open_price, low_price, limit_up):
            log.info('%s 涨停买不进', target)
            return

        volume = self.account.affordable_volume(open_price, budget)
        if volume < self.account.cfg.lot_size:
            log.info('资金不足买 1 手，可用:%.2f 额度:%.2f 股价:%.2f',
                     self.account.cash, budget, open_price)
            return

        self.account.buy(date, target, open_price, volume,
                         f'BUY信号 买入 {target} {volume}股')

    def _sell_at_open(self, date: str, stock: str, msg: str) -> None:
        # 停牌的票卖不掉，只能继续持有 —— 停牌日的 K 线是用前收填充的，
        # 照着它成交等于凭空按停牌前的价格脱手
        if not self.cfg.allow_sell_suspended and is_suspended(self.source, stock, date):
            log.info('%s 当日停牌，无法卖出，继续持有', stock)
            return

        df = self.source.get_one(stock, date, 1)
        if df is None:
            log.warning('无法获取 %s 价格，卖出跳过', stock)
            return
        self.account.sell_all(date, stock, float(df['open'].iloc[-1]), msg)

    # ---------- 止损 ----------

    def check_stop_loss(self, date: str) -> None:
        """用当日收盘价判断是否触发硬止损（原脚本 14:50 的盘中检查）"""
        for pos in self.account.get_positions():
            if pos.can_use <= 0 or pos.open_price <= 0:
                continue

            df = self.source.get_one(pos.stock, date, 1)
            if df is None:
                continue
            close = float(df['close'].iloc[-1])
            if close <= 0:
                continue

            if not self.cfg.allow_sell_suspended and is_suspended(self.source, pos.stock, date):
                log.info('%s 当日停牌，止损无法执行，继续持有', pos.stock)
                continue

            profit = (close - pos.open_price) / pos.open_price
            log.info('止损检查 %s %s 成本:%.2f 收盘:%.2f 盈亏:%.2f%%',
                     pos.stock, self.source.get_stock_name(pos.stock),
                     pos.open_price, close, profit * 100)

            if profit <= self.cfg.stop_loss_ratio:
                log.info('%s 触发硬止损（%.2f%% <= %.0f%%），强制清仓',
                         pos.stock, profit * 100, self.cfg.stop_loss_ratio * 100)
                self.account.sell_all(date, pos.stock, close, f'硬止损平仓 {pos.stock}')

    # ---------- 复盘 ----------

    def review(self, date: str) -> float:
        today_deals = [d for d in self.account.deals if d.date == date]
        if today_deals:
            log.info('今日成交 %d 笔:', len(today_deals))
            for deal in today_deals:
                log.info('  %s %s 价格:%.2f 数量:%d',
                         deal.stock, '买入' if deal.direction > 0 else '卖出',
                         deal.price, deal.volume)

        price_map: Dict[str, float] = {}
        for pos in self.account.get_positions():
            df = self.source.get_one(pos.stock, date, 1)
            close = float(df['close'].iloc[-1]) if df is not None else 0.0
            price_map[pos.stock] = close
            profit = (close - pos.open_price) / pos.open_price if pos.open_price > 0 else 0.0
            log.info('  持仓 %s %s 成本:%.2f 收盘:%.2f 盈亏:%.2f%% 市值:%.0f',
                     pos.stock, self.source.get_stock_name(pos.stock),
                     pos.open_price, close, profit * 100, close * pos.volume)

        self._last_price_map = price_map
        total = self.account.total_asset(price_map)
        log.info('  资金 可用:%.0f 总资产:%.0f', self.account.cash, total)
        return total


# ======================================================================
# vector_engine.py
# ======================================================================

"""
向量化回测引擎。

和逐日版（BacktestEngine）相比，选股环节全部换成矩阵运算：
  - 股票池过滤：停牌 / 收盘价 / ST / 市值 一次性算成 (交易日 × 标的) 的布尔矩阵
  - 动量打分：整块滚动回归，125 万次 polyfit -> 几次矩阵运算
  - 动量分数序列：直接切分数矩阵的一列
  - RSRS：整段历史滚动一次，不再每天重算 600 次回归

下单、止损、复盘完全继承逐日版，保证两个引擎的交易逻辑只有一份实现。

与逐日版的一处行为差异：面板按交易日历对齐，某只标的当日没有数据即视为
当日不可交易；逐日版会沿用它最近一根 K 线（已退市标的会一直参与打分）。
行情完整时两者结果一致。
"""




log = logging.getLogger(__name__)


class PanelDataSource(DataSource):
    """
    把面板包装成 DataSource，供继承来的下单 / 止损 / 复盘代码取价。
    面板里没有的标的（例如指数）回落到底层数据源。
    """

    def __init__(self, panel: Panel, base: DataSource):
        self.panel = panel
        self.base = base
        close = panel.field('close')
        # 每只标的的有效行号，取价时用 searchsorted 定位，避免逐次扫描
        self._valid_rows = [np.flatnonzero(np.isfinite(close[:, j]))
                            for j in range(close.shape[1])] if close is not None else []

    # --- 透传 ---
    def get_sector_stocks(self, sector):
        return self.base.get_sector_stocks(sector)

    def get_trading_dates(self, end_date):
        return self.base.get_trading_dates(end_date)

    def get_detail(self, stock):
        return self.base.get_detail(stock)

    def preload(self, stocks, start_date, end_date):
        return None

    def download(self, stocks, start_date, end_date):
        return self.base.download(stocks, start_date, end_date)

    # --- 取价 ---
    def get_bars(self, stocks, end_date, count, fields=None):
        import pandas as pd

        if isinstance(stocks, str):
            stocks = [stocks]

        panel = self.panel
        i = panel.index_of(end_date)
        result = {}
        outside = []

        for stock in stocks:
            j = panel.stock_pos.get(stock)
            if j is None:
                outside.append(stock)
                continue
            if i < 0:
                continue

            rows = self._valid_rows[j]
            end = int(np.searchsorted(rows, i, side='right'))
            if end <= 0:
                continue
            start = 0 if count is None or count <= 0 else max(0, end - count)
            take = rows[start:end]
            if len(take) == 0:
                continue

            use = list(fields) if fields else list(panel.arrays)
            data = {f: panel.arrays[f][take, j] for f in use if panel.has(f)}
            result[stock] = pd.DataFrame(data, index=[panel.dates[r] for r in take])

        if outside:
            result.update(self.base.get_bars(outside, end_date, count, fields))
        return result


class VectorBacktestEngine(BacktestEngine):
    def __init__(self, source: DataSource, config: BacktestConfig, account=None):
        super().__init__(source, config, account)
        self.panel: Optional[Panel] = None
        self.scores: Optional[np.ndarray] = None
        self.pool_mask: Optional[np.ndarray] = None
        self.tradable: Optional[np.ndarray] = None
        self.rsrs: Optional[np.ndarray] = None
        self._base_source = source

    # ---------- 预处理 ----------

    def prepare(self, download: bool = False) -> List[str]:
        self.base_pool = build_base_pool(self._base_source, self.cfg)
        log.info('原始股票池: %d 只', len(self.base_pool))
        if not self.base_pool:
            return []

        stock_start = self._warm_start(self.cfg.warmup_days + 5)
        index_start = self._warm_start(self.cfg.bars_needed_for_rsrs + 10)

        if download:
            self._base_source.download(list(self.base_pool) + [self.cfg.rsrs_index],
                                       index_start, self.config.end_date)

        self._base_source.preload(self.base_pool, stock_start, self.config.end_date)
        if self.cfg.rsrs_enabled:
            self._base_source.preload([self.cfg.rsrs_index], index_start, self.config.end_date)

        log.info('构建行情面板 ...')
        self.panel = Panel.from_source(self._base_source, self.base_pool,
                                       stock_start, self.config.end_date)
        if not len(self.panel) or not self.panel.stocks:
            raise RuntimeError(
                '数据源取不到任何日线数据，回测无法开始。\n'
                '  - 若上面提示「缺的是除权除息因子」，加 --download 补下载，\n'
                '    或先用 --dividend-type none 跑不复权\n'
                '  - 否则加 --download 补下载日线，或在 QMT 客户端「行情 -> 数据管理」补充\n'
                '  - 用 python run_backtest.py --check-data 逐步定位'
            )
        log.info('面板规模: %d 个交易日 × %d 只标的', *self.panel.shape)

        self.source = PanelDataSource(self.panel, self._base_source)
        self._build_matrices(index_start)
        return self.base_pool

    def _build_matrices(self, index_start: str) -> None:
        panel = self.panel
        close = panel.field('close')
        n, m = panel.shape

        # --- 动量分数矩阵 ---
        self.scores = momentum_score_matrix(close, self.cfg.lookback_days,
                                            self.cfg.trading_days_per_year)

        # --- 静态过滤：合约信息不变，只算一次 ---
        static_ok = np.ones(m, dtype=bool)
        total_value = np.zeros(m)
        shares = np.zeros(m)
        for j, stock in enumerate(panel.stocks):
            detail = self._base_source.get_detail(stock)
            if not detail:
                static_ok[j] = False
                continue
            if self.cfg.exclude_st and 'ST' in (detail.get('InstrumentName', '') or '').upper():
                static_ok[j] = False
                continue
            value = detail.get('TotalValue', 0) or 0
            if value > 0:
                total_value[j] = float(value)
            else:
                for key in ('TotalVolume', 'TotalVolumn', 'TotalShares'):
                    got = detail.get(key, 0) or 0
                    if got > 0:
                        shares[j] = float(got)
                        break

        # --- 逐日过滤 ---
        close_ok = np.isfinite(close) & (close > 0)

        suspend = panel.field('suspendFlag')
        if suspend is None:
            susp_ok = np.ones((n, m), dtype=bool)
        else:
            susp_ok = ~(suspend == 1)

        volume = panel.field('volume')
        if volume is not None:                       # 停牌日成交量为 0
            susp_ok = susp_ok & ~(np.isfinite(volume) & (volume <= 0))

        if self.cfg.filter_market_cap:
            with np.errstate(invalid='ignore'):
                cap = np.where(total_value[None, :] > 0, total_value[None, :],
                               shares[None, :] * np.where(close_ok, close, np.nan))
                cap_ok = ~(np.isfinite(cap) & (cap > 0)) | (
                    (cap >= self.cfg.min_market_cap) & (cap <= self.cfg.max_market_cap))
        else:
            cap_ok = np.ones((n, m), dtype=bool)

        self.pool_mask = close_ok & susp_ok & cap_ok & static_ok[None, :]

        # --- 候选股可交易性：停牌 + 开盘价有效 ---
        open_arr = panel.field('open')
        if open_arr is None:
            open_ok = np.isfinite(close)
        else:
            open_ok = np.isfinite(open_arr) & (open_arr > 0)
        self.tradable = np.isfinite(close) & susp_ok & open_ok

        # 跌停过滤（未来函数，默认关闭，与逐日版的 filter_limit_down 对应）
        if self.cfg.filter_limit_down:
            if self.cfg.limit_down_ratio > 0:
                ratios = np.full(len(panel.stocks), self.cfg.limit_down_ratio)
            else:
                ratios = np.array([limit_ratio(s) for s in panel.stocks])
            pre_close = panel.field('preClose')
            if pre_close is None:
                pre_close = close
            with np.errstate(invalid='ignore'):
                limit_down = np.round(pre_close * (1 - ratios[None, :]), 2)
                not_limit_down = ~((close > 0) & (close <= limit_down))
            self.tradable = self.tradable & not_limit_down

        # --- RSRS ---
        self.rsrs = None
        if self.cfg.rsrs_enabled:
            index_df = self._base_source.get_one(self.cfg.rsrs_index,
                                                 self.config.end_date, 0)
            if index_df is not None and len(index_df) >= self.cfg.rsrs_m + self.cfg.rsrs_n:
                values = rsrs_series(index_df['high'].to_numpy(dtype=float),
                                     index_df['low'].to_numpy(dtype=float),
                                     self.cfg.rsrs_n, self.cfg.rsrs_m)
                dates = [str(d)[:8] for d in index_df.index]
                lookup = dict(zip(dates, values))
                self.rsrs = np.array([lookup.get(d, np.nan) for d in panel.dates])

    # ---------- 单个交易日 ----------

    def run_day(self, date: str) -> float:
        log.info('=' * 56)
        log.info('交易日: %s', date)

        self.account.settle_open()
        self._last_signal = ''
        self.today_target = None

        i = self.panel.date_pos.get(date)
        if i is None:
            log.info('非交易日或面板中无此日期，跳过')
            return self.review(date)
        self._row = i

        candidates = self.pool_mask[i]
        if not candidates.any():
            log.info('股票池为空，跳过今日')
            return self.review(date)
        log.info('步骤1 - 股票池: %d 只', int(candidates.sum()))

        row = self.scores[i]
        usable = candidates & np.isfinite(row)
        if not usable.any():
            log.info('步骤2 - 未选出目标股票')
            return self.review(date)

        picks = self.top_indices(i, usable, self.cfg.max_positions)
        names = [self.panel.stocks[j] for j in picks]
        log.info('步骤2 - 目标: %s',
                 [f'{n}（{row[j]:.4f}）' for n, j in zip(names, picks)])

        self.score_series = {n: self.series_at(i, j) for n, j in zip(names, picks)}
        log.info('步骤3 - 首选动量分数序列: %s',
                 [round(float(x), 4) for x in self.score_series.get(names[0], [])])

        targets = [n for n, j in zip(names, picks) if bool(self.tradable[i, j])]
        if not targets:
            log.info('步骤4 - 目标股票全部被过滤（停牌或跌停）')
            return self.review(date)
        self.today_target = targets[0]
        log.info('步骤4 - 过滤通过: %s', targets)

        if self.rsrs is not None and np.isfinite(self.rsrs[i]):
            log.info('RSRS修正标准分: %.4f', self.rsrs[i])
        else:
            log.info('RSRS修正标准分: 数据不足')

        signal, to_sell = self.resolve_signals(date, targets)
        self._last_signal = signal
        log.info('步骤5 - 择时信号: %s%s', signal,
                 f'  待卖出: {sorted(to_sell)}' if to_sell else '')

        # 原策略口径下 SELL 意味着「清仓且当日不再买入」，保持该语义；
        # 其余情况统一走组合调仓（max_positions=1 时与原逻辑完全等价）
        if not self.cfg.timing_on_holdings and signal == SIGNAL_SELL:
            self.adjust_portfolio(date, [], to_sell=to_sell, allow_buy=False)
        else:
            keep = [t for t in targets if t not in to_sell]
            self.adjust_portfolio(date, keep, to_sell=to_sell)
        log.info('步骤6 - 调仓执行完毕')

        self.check_stop_loss(date)
        return self.review(date)

    def top_indices(self, i: int, usable: np.ndarray, count: int) -> List[int]:
        """取当日分数最高的 count 个列号（按分数降序）"""
        row = np.where(usable, self.scores[i], -np.inf)
        count = min(max(count, 1), int(usable.sum()))
        if count == 1:
            return [int(np.argmax(row))]
        idx = np.argpartition(-row, count - 1)[:count]
        return [int(j) for j in idx[np.argsort(-row[idx])]]

    def holding_series(self, date: str, stock: str) -> List[float]:
        """持仓股的动量分数序列：在面板里就切矩阵列，否则回落到逐只计算"""
        j = self.panel.stock_pos.get(stock)
        i = getattr(self, '_row', None)
        if j is None or i is None:
            return momentum_series(self.source, stock, date, self.cfg)
        return self.series_at(i, j)

    def resolve_signals(self, date: str, targets):
        if not self.cfg.timing_on_holdings:
            return super().resolve_signals(date, targets)

        to_sell = set()
        for stock in self.account.holdings_can_use():
            series = self.holding_series(date, stock)
            self.score_series[stock] = series
            if series and timing_signal(series, self.cfg) == SIGNAL_SELL:
                to_sell.add(stock)
        if to_sell:
            return SIGNAL_SELL, to_sell
        return (SIGNAL_BUY if targets else SIGNAL_KEEP), to_sell

    def series_at(self, i: int, j: int) -> List[float]:
        """动量分数序列：分数矩阵第 j 列的一段，缺失记 0.0（与逐日版一致）"""
        length = self.cfg.score_series_len
        if i - length < 0:
            return []
        window = self.scores[i - length:i + 1, j]
        return [0.0 if not np.isfinite(v) else float(v) for v in window]


# ======================================================================
# report.py
# ======================================================================

"""回测绩效统计与输出。"""





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
             'cash', 'market_value', 'position_count', 'holdings',
             'stock', 'volume', 'cost', 'price', 'target', 'signal']]
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


# ============================================================
# 终端表格对齐
# ============================================================

def display_width(text: str) -> int:
    """中日韩字符在终端里占两列，按显示宽度算才能对齐"""
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in str(text))


def pad(text: str, width: int, left: bool = False) -> str:
    space = ' ' * max(0, width - display_width(text))
    return (text + space) if left else (space + text)


# ======================================================================
# scoreboard.py
# ======================================================================

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


# ======================================================================
# score_returns.py
# ======================================================================

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




log = logging.getLogger(__name__)

DETAIL_COLUMNS = ['date', 'rank', 'stock', 'name', 'score',
                  'buy_date', 'buy_open', 'sell_date', 'sell_open',
                  'ret', 'ret_net', 'note']

TRADED = ''
NOTE_NO_BUY = '买入日停牌'
NOTE_NO_SELL = '卖出日之后无行情'
NOTE_NO_BAR = '无行情数据'
NOTE_LIMIT_UP = '买入日开盘涨停'


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
                              fields=('open', 'close', 'preClose', 'volume', 'suspendFlag'))
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


def limit_up_ratio(stock: str, name: str = '') -> float:
    """
    涨停幅度。创业板/科创板 20%（ST 也是 20%），北交所 30%，
    主板 ST/*ST 5%，其余 10%。indicators.limit_ratio 只按代码前缀分，
    这里多用榜单里的名称补上 ST 一档。
    """
    code = stock.split('.')[0]
    if code.startswith(('43', '83', '87', '88', '92')):
        return 0.30
    if code.startswith(('300', '301', '688')):
        return 0.20
    if 'ST' in (name or '').upper().replace(' ', ''):
        return 0.05
    return limit_ratio(stock)


def is_limit_up_open(open_price: float, pre_close: float, ratio: float,
                     tolerance: float = 0.003) -> bool:
    """
    开盘价是否已经涨停。

    用涨幅比例判断而不是 round(前收×(1+幅度), 2)：后复权价不是真实报价，
    对它做两位小数取整没有意义。tolerance 吸收复权与取整带来的零点几个点误差
    —— 代价是涨 9.7%~10% 没封板的票也会被当成买不进，属于偏保守。
    """
    if not (np.isfinite(open_price) and np.isfinite(pre_close)) or pre_close <= 0:
        return False
    return open_price / pre_close - 1.0 >= ratio - tolerance


def _prev_close(pre_closes, closes, i: int, col: int) -> float:
    """前收盘价：优先取 preClose 字段，缺失时回落到上一行的收盘价"""
    if pre_closes is not None:
        value = pre_closes[i, col]
        if np.isfinite(value) and value > 0:
            return float(value)
    if closes is not None and i > 0:
        value = closes[i - 1, col]
        if np.isfinite(value) and value > 0:
            return float(value)
    return float('nan')


def score_returns(board: pd.DataFrame, panel: Panel, delay: int = 1, hold: int = 1,
                  account: Optional[AccountConfig] = None, max_sell_delay: int = 20,
                  skip_limit_up: bool = False, limit_tolerance: float = 0.003
                  ) -> pd.DataFrame:
    """
    逐条信号算收益，返回明细表。

    delay          : 信号日之后第几个交易日开盘买入（1 = 次日）
    hold           : 买入后持有几个交易日，在那天开盘卖出（1 = 第二天）
    skip_limit_up  : 买入日开盘已涨停的剔除（挂单买不进），这笔记为持币
    """
    account = account or AccountConfig()
    buy_rate = account.buy_cost_rate
    sell_rate = account.sell_cost_rate

    opens = panel.field('open')
    closes = panel.field('close')
    pre_closes = panel.field('preClose')
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

        if skip_limit_up:
            prev = _prev_close(pre_closes, closes, b, col)
            ratio = limit_up_ratio(row.stock, getattr(row, 'name', ''))
            if is_limit_up_open(opens[b, col], prev, ratio, limit_tolerance):
                records.append(dict(base, buy_date=dates[b], buy_open=float(opens[b, col]),
                                    note=NOTE_LIMIT_UP))
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

def curve_stats(series: Sequence[float], trades: Optional[Sequence[float]] = None,
                trading_days_per_year: int = 244) -> Dict[str, float]:
    """
    series: 逐信号日的收益，买不进/卖不掉的那天记 0（持币），用来算净值曲线
    trades: 实际成交的那些笔，用来算日均 / 中位数 / 胜率 / 最好最差
            —— 不传就等同于 series 里的非零项
    """
    values = np.asarray([0.0 if not np.isfinite(r) else float(r) for r in series], dtype=float)
    if len(values) == 0:
        return {}

    done = np.asarray([float(r) for r in (trades if trades is not None else series)
                       if np.isfinite(r)], dtype=float)

    equity = np.cumprod(1.0 + values)
    peak = np.maximum.accumulate(equity)
    drawdown = equity / peak - 1.0
    total = float(equity[-1] - 1.0)

    years = len(values) / float(trading_days_per_year)
    annual = float(equity[-1] ** (1.0 / years) - 1.0) if years > 0 and equity[-1] > 0 else float('nan')

    std = float(values.std(ddof=1)) if len(values) > 1 else 0.0
    sharpe = (float(values.mean()) / std * math.sqrt(trading_days_per_year)) if std > 0 else float('nan')

    stats = {'days': len(values), 'traded': len(done), 'total': total, 'annual': annual,
             'max_drawdown': float(drawdown.min()), 'std': std, 'sharpe': sharpe}
    if len(done):
        stats.update({'mean': float(done.mean()), 'median': float(np.median(done)),
                      'win_rate': float((done > 0).mean()),
                      'best': float(done.max()), 'worst': float(done.min())})
    else:
        stats.update({'mean': 0.0, 'median': 0.0, 'win_rate': 0.0,
                      'best': 0.0, 'worst': 0.0})
    return stats


def rank_stats(detail: pd.DataFrame, column: str = 'ret',
               trading_days_per_year: int = 244) -> List[Tuple[str, Dict[str, float]]]:
    """
    按名次分组统计，最后再加一行 TOP-N 等权组合。

    买不进的那天不是「跳过这天」而是「当天持币」，所以净值曲线里记 0，
    只有日均 / 胜率这些逐笔指标才排除掉它。
    """
    out = []
    ranks = sorted(detail['rank'].unique())
    for rank in ranks:
        rows = detail[detail['rank'] == rank].sort_values('date')
        values = rows[column].to_numpy(dtype=float)
        stats = curve_stats(values, values[np.isfinite(values)], trading_days_per_year)
        if stats:
            stats['signals'] = int(len(rows))
            out.append((f'第 {rank} 名', stats))

    if len(ranks) > 1:
        daily = _portfolio_daily(detail, column, len(ranks))
        done = detail[column].to_numpy(dtype=float)
        stats = curve_stats(daily.to_numpy(dtype=float), done[np.isfinite(done)],
                            trading_days_per_year)
        if stats:
            stats['signals'] = int(len(detail))
            out.append((f'TOP{int(detail["rank"].max())} 等权', stats))
    return out


def _portfolio_daily(detail: pd.DataFrame, column: str, slots: int) -> pd.Series:
    """
    等权组合的逐日收益：每个名次一个仓位，买不进的那个仓位当天空着（记 0），
    所以是「当天成交的收益之和 ÷ 名次数」，不是「成交那几只的平均」。
    """
    return detail.groupby('date')[column].sum(min_count=0).sort_index() / float(slots)


STAT_COLUMNS = (('口径', 12, True), ('笔数', 7, False), ('日均', 9, False),
                ('中位数', 9, False), ('胜率', 8, False), ('累计', 11, False),
                ('年化', 11, False), ('最大回撤', 10, False), ('夏普', 7, False))


def format_stats(rows: Sequence[Tuple[str, Dict[str, float]]], title: str) -> str:
    header = '  ' + ' '.join(pad(name, width, left) for name, width, left in STAT_COLUMNS)
    rule = '=' * display_width(header)
    lines = [rule, title, rule, header]
    for label, st in rows:
        values = [label, f"{st['traded']}/{st['days']}", f"{st['mean']:.3%}",
                  f"{st['median']:.3%}", f"{st['win_rate']:.1%}", f"{st['total']:.2%}",
                  f"{st['annual']:.2%}", f"{st['max_drawdown']:.2%}", f"{st['sharpe']:.2f}"]
        lines.append('  ' + ' '.join(pad(v, w, left)
                                     for v, (_, w, left) in zip(values, STAT_COLUMNS)))
    lines.append(rule)
    return '\n'.join(lines)


def format_notes(detail: pd.DataFrame) -> str:
    counts = detail['note'].value_counts()
    total = len(detail)
    done = int(counts.get(TRADED, 0))
    lines = [f'  信号 {total} 条，成交 {done} 条（{done / total:.1%}），其余当天持币']
    for note, count in counts.items():
        if note != TRADED:
            lines.append(f'    {note}: {count} 条（{count / total:.1%}）')
    if detail['ret'].notna().any():
        best = detail.loc[detail['ret'].idxmax()]
        worst = detail.loc[detail['ret'].idxmin()]
        lines.append(f"  单笔最好: {best['date']} 第{best['rank']}名 {best['stock']} "
                     f"{best['name']} {best['ret']:+.2%}")
        lines.append(f"  单笔最差: {worst['date']} 第{worst['rank']}名 {worst['stock']} "
                     f"{worst['name']} {worst['ret']:+.2%}")
    return '\n'.join(lines)


def portfolio_equity(detail: pd.DataFrame, column: str = 'ret') -> pd.DataFrame:
    """TOP-N 等权组合的逐日收益与净值（买不进的仓位当天记 0）"""
    slots = int(detail['rank'].nunique())
    daily = _portfolio_daily(detail, column, slots)
    values = np.nan_to_num(daily.to_numpy(dtype=float), nan=0.0)
    return pd.DataFrame({'date': daily.index, 'ret': values,
                         'equity': np.cumprod(1.0 + values)})


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
                      trading_days_per_year: int = 244, max_sell_delay: int = 20,
                      skip_limit_up: bool = False, limit_tolerance: float = 0.003
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
    detail = score_returns(board, panel, delay, hold, account, max_sell_delay,
                           skip_limit_up, limit_tolerance)

    entry = '次日' if delay == 1 else f'信号日之后第 {delay} 个交易日'
    exit_ = '第二天' if hold == 1 else f'持有 {hold} 个交易日后'
    picked = '第 %s 名' % '/'.join(str(r) for r in sorted(board['rank'].unique()))
    title = f'[{entry}开盘买入 / {exit_}开盘卖出]  {picked}  {start} ~ {last}'
    if skip_limit_up:
        title += '  剔除买入日开盘涨停'

    print(format_stats(rank_stats(detail, 'ret', trading_days_per_year), title + '  不计费用'))
    print(format_notes(detail))
    print()
    print(format_stats(rank_stats(detail, 'ret_net', trading_days_per_year),
                       title + '  扣佣金/过户费/印花税'))
    if skip_limit_up:
        print('  「笔数」是 成交/信号日；买不进的那天按持币记 0，不是从曲线里抹掉')
        print(f'  涨停判定：开盘涨幅 >= 涨停幅度 - {limit_tolerance:.1%}'
              '（主板 10%、创业板/科创板 20%、主板 ST 5%、北交所 30%）')
        print('  仍未建模：一字跌停卖不掉、滑点与冲击成本')
    else:
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


# ======================================================================
# sample_data.py
# ======================================================================

"""
合成样例行情，用于单元测试和没有 QMT 环境时跑通完整流程。
生成的数据结构与 CsvDataSource 期望的一致。
"""



DEFAULT_STOCKS = (
    '600000.SH', '600519.SH', '000001.SZ', '300750.SZ',
    '688981.SH', '000002.SZ', '002415.SZ', '601318.SH',
)


def make_calendar(days: int, start: str = '20220104') -> List[str]:
    """用工作日近似交易日历"""
    return pd.bdate_range(start=pd.Timestamp(start), periods=days).strftime('%Y%m%d').tolist()


def make_bars(dates: Sequence[str], base_price: float = 20.0,
              drift: float = 0.0005, vol: float = 0.02,
              seed: int = 0) -> pd.DataFrame:
    """按几何布朗运动生成一只标的的日线"""
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, len(dates))
    close = base_price * np.exp(np.cumsum(rets))
    pre_close = np.concatenate([[base_price], close[:-1]])
    open_ = pre_close * (1 + rng.normal(0, 0.004, len(dates)))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.005, len(dates))))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.005, len(dates))))

    return pd.DataFrame({
        'open': open_, 'high': high, 'low': low, 'close': close,
        'preClose': pre_close,
        'volume': rng.integers(1e5, 1e6, len(dates)).astype(float),
        'suspendFlag': np.zeros(len(dates)),
    }, index=list(dates))


def make_sample_frames(days: int = 700, stocks: Sequence[str] = DEFAULT_STOCKS,
                       index_code: str = '000300.SH',
                       start: str = '20220104', seed: int = 7):
    """返回 (frames, details, sectors)，可直接喂给 CsvDataSource.from_frames"""
    dates = make_calendar(days, start)

    frames: Dict[str, pd.DataFrame] = {}
    details: Dict[str, dict] = {}
    for i, stock in enumerate(stocks):
        frames[stock] = make_bars(dates, base_price=10 + i * 4,
                                  drift=0.0002 + i * 0.0002, seed=seed + i)
        details[stock] = {
            'InstrumentName': f'样例{stock[:6]}',
            'TotalValue': float(100e8 + i * 20e8),
        }

    if index_code:
        frames[index_code] = make_bars(dates, base_price=3800, drift=0.0001,
                                       vol=0.01, seed=seed + 99)
        details[index_code] = {'InstrumentName': '沪深300', 'TotalValue': 0.0}

    sectors = {'沪深A股': list(stocks)}
    return frames, details, sectors


def write_sample_dir(data_dir: str, days: int = 700,
                     stocks: Sequence[str] = DEFAULT_STOCKS,
                     index_code: str = '000300.SH',
                     start: str = '20220104', seed: int = 7) -> str:
    """把样例数据写成 CsvDataSource 目录结构，返回目录路径"""
    frames, details, sectors = make_sample_frames(days, stocks, index_code, start, seed)

    bars_dir = os.path.join(data_dir, 'bars')
    os.makedirs(bars_dir, exist_ok=True)
    for stock, df in frames.items():
        out = df.copy()
        out.index.name = 'date'
        out.to_csv(os.path.join(bars_dir, f'{stock}.csv'), encoding='utf-8')

    with open(os.path.join(data_dir, 'instruments.json'), 'w', encoding='utf-8') as f:
        json.dump(details, f, ensure_ascii=False, indent=2)
    with open(os.path.join(data_dir, 'sectors.json'), 'w', encoding='utf-8') as f:
        json.dump(sectors, f, ensure_ascii=False, indent=2)

    return data_dir


def make_source(days: int = 700, stocks: Sequence[str] = DEFAULT_STOCKS,
                index_code: Optional[str] = '000300.SH',
                start: str = '20220104', seed: int = 7):
    """直接构造一个内存 CsvDataSource"""

    frames, details, sectors = make_sample_frames(days, stocks, index_code or '', start, seed)
    return CsvDataSource.from_frames(frames, details, sectors)


# ======================================================================
# diagnostics.py
# ======================================================================

"""
xtdata 数据自检。

逐步确认：能否 import xtquant -> 交易日历 -> 板块成分 -> 合约信息 ->
日线数据（按 count 取 / 按区间取）-> 必要时试下载一只标的再重试。
每一步都把 xtdata 的真实返回打出来，便于定位到底卡在哪一环。

    python run_backtest.py --check-data
    python run_backtest.py --check-data --download        # 自检时顺便试下载
"""


DEFAULT_SAMPLES = ('000300.SH', '600000.SH', '000001.SZ')

OK = '[OK]  '
BAD = '[FAIL]'
WARN = '[WARN]'


def _p(tag: str, msg: str) -> None:
    print(f'{tag} {msg}')


def check_xtdata(samples: Sequence[str] = DEFAULT_SAMPLES,
                 sector: str = '沪深A股',
                 start_date: str = '20240101',
                 end_date: str = '20241231',
                 try_download: bool = False) -> bool:
    """返回 True 表示日线数据可用"""
    print('=' * 56)
    print('xtdata 数据自检')
    print('=' * 56)

    # 1. 导入
    try:
        from xtquant import xtdata
    except Exception as e:
        _p(BAD, f'import xtquant 失败: {e}')
        print('  xtquant 随 QMT 安装，一般在 <QMT安装目录>\\bin.x64\\Lib\\site-packages')
        print('  把该目录加入 PYTHONPATH，或直接用 QMT 自带的 python 运行')
        return False
    _p(OK, f'import xtquant 成功（{getattr(xtdata, "__file__", "?")}）')

    # 2. 交易日历
    try:
        dates = xtdata.get_trading_dates('SH', start_time='', end_time=end_date, count=-1)
        days = [xtdata.timetag_to_datetime(t, '%Y%m%d') for t in dates]
        _p(OK, f'交易日历 {len(days)} 个交易日，最后一个 {days[-1] if days else "无"}')
    except Exception as e:
        _p(BAD, f'get_trading_dates 失败: {e}（QMT 客户端可能未启动或未登录）')
        return False

    # 3. 板块
    try:
        stocks = xtdata.get_stock_list_in_sector(sector) or []
        _p(OK 
           if stocks else WARN,
           f'板块 {sector}: {len(stocks)} 只' + (f'，示例 {stocks[:3]}' if stocks else '（为空，检查板块名）'))
    except Exception as e:
        _p(BAD, f'get_stock_list_in_sector 失败: {e}')
        stocks = []

    # 4. 合约信息
    probe = list(samples)
    for stock in probe[:1]:
        try:
            detail = xtdata.get_instrument_detail(stock)
            if detail:
                _p(OK, f'{stock} 合约信息: 名称={detail.get("InstrumentName")} '
                       f'TotalValue={detail.get("TotalValue")}')
                missing = [k for k in ('TotalValue', 'TotalVolume', 'TotalVolumn', 'TotalShares')
                           if k in detail]
                _p(OK, f'  市值相关字段: {missing or "无（市值过滤会被跳过）"}')
            else:
                _p(WARN, f'{stock} 合约信息为空')
        except Exception as e:
            _p(BAD, f'get_instrument_detail 失败: {e}')

    # 5. 日线：按 count 取
    ok_count = _probe_bars(xtdata, probe, mode='count')
    # 6. 日线：按区间取
    ok_range = _probe_bars(xtdata, probe, mode='range',
                           start_date=start_date, end_date=end_date)

    if ok_count or ok_range:
        _p(OK, '日线数据可用，可以直接跑回测')
        return True

    # 7. 复权因子缺失时，复权价取不到但不复权能取到 —— 表象和「没下载日线」一样
    if DEFAULT_DIVIDEND_TYPE != 'none':
        if _probe_bars(xtdata, probe, mode='range', start_date=start_date,
                       end_date=end_date, dividend_type='none'):
            _p(BAD, f'日线有数据，但按「{DEFAULT_DIVIDEND_TYPE}」复权取不到 '
                    f'—— 缺的是除权除息因子（{DIVIDEND_PERIOD}）')
            print('  复权因子是独立于日线的一份数据，不随日线一起下载。')
            print('  处理办法：')
            print('    1. 回测命令加 --download，会连同除权除息因子一起补下载')
            print('    2. 在 QMT 客户端「行情 -> 数据管理」里补充除权除息数据')
            print('    3. 先用 --dividend-type none 跑不复权（除权跳空会被当成真实下跌）')
            return False

    _p(BAD, '日线数据取不到 —— 本地大概率没有下载过日线')

    if not try_download:
        print('\n处理办法（任选其一）：')
        print('  1. 回测命令加 --download，让脚本先补下载（含除权除息因子）')
        print('  2. 在 QMT 客户端「行情 -> 数据管理 / 数据下载」里补充日线数据')
        print('  3. 再跑一次 --check-data --download，让自检直接试一只标的的下载')
        return False

    # 7. 试下载一只再重试
    target = probe[0]
    _p(WARN, f'尝试下载 {target} 的日线 ...')
    try:
        xtdata.download_history_data(target, period='1d',
                                     start_time=start_date, end_time=end_date)
    except Exception as e:
        _p(BAD, f'download_history_data 失败: {e}')
        return False

    if _probe_bars(xtdata, [target], mode='range',
                   start_date=start_date, end_date=end_date):
        _p(OK, f'{target} 下载后可正常读取 —— 整体补下载即可（回测加 --download）')
        return True

    _p(BAD, f'{target} 下载后仍读不到数据，请检查客户端数据权限与磁盘数据目录')
    return False


def _probe_bars(xtdata, stocks: Sequence[str], mode: str,
                start_date: str = '', end_date: str = '') -> bool:
    """分别用完整字段和核心字段探测，打印返回形状"""

    for label, fields in (('完整字段', DAILY_FIELDS), ('核心字段', CORE_FIELDS)):
        kwargs = dict(period='1d', fill_data=True,
                      dividend_type=dividend_type or DEFAULT_DIVIDEND_TYPE)
        if mode == 'count':
            kwargs.update(count=5)
            desc = f"count=5 / {label} / 复权 {kwargs['dividend_type']}"
        else:
            kwargs.update(start_time=start_date, end_time=end_date, count=-1)
            desc = (f'{start_date}~{end_date} / {label}'
                    f" / 复权 {kwargs['dividend_type']}")

        try:
            data = xtdata.get_market_data_ex(list(fields), list(stocks), **kwargs)
        except Exception as e:
            _p(BAD, f'get_market_data_ex({desc}) 抛异常: {e}')
            continue

        shapes = {s: (0 if data.get(s) is None else len(data[s])) for s in stocks}
        got = [s for s, n in shapes.items() if n > 0]
        if got:
            sample = data[got[0]]
            _p(OK, f'get_market_data_ex({desc}) 返回 {shapes}')
            _p(OK, f'  {got[0]} 列: {list(sample.columns)}')
            _p(OK, f'  最后一行: {sample.tail(1).to_dict("records")}')
            return True
        _p(WARN, f'get_market_data_ex({desc}) 全部为空 {shapes}')

    return False


# ======================================================================
# cli.py
# ======================================================================

"""命令行入口。"""




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
    p.add_argument('--dividend-type', choices=list(DIVIDEND_TYPES), default=DEFAULT_DIVIDEND_TYPE,
                   help='复权方式，默认 back（后复权）。不复权会把除权跳空当成真实下跌，'
                        '严重压低分红股的动量分数；none 仅用于和旧结果对照')
    p.add_argument('--download', action='store_true', help='回测前先补下载本地日线')
    p.add_argument('--check-data', action='store_true',
                   help='只做 xtdata 数据自检并退出（配合 --download 会试下载一只标的）')

    p.add_argument('--sectors', default='', help='板块名，逗号分隔，默认 ' + ','.join(CONCEPT_SECTORS_DEFAULT))
    p.add_argument('--lookback', type=int, nargs='+', default=[StrategyConfig.lookback_days],
                   metavar='N', help='动量回看天数，可给多个依次回测，例如 --lookback 15 29')
    p.add_argument('--stop-loss', type=float, default=StrategyConfig.stop_loss_ratio, help='止损线，如 -0.15')
    p.add_argument('--decline-days', type=int, default=StrategyConfig.decline_days_to_sell,
                   help='动量分数连续下降几天清仓')
    p.add_argument('--min-cap', type=float, default=StrategyConfig.min_market_cap, help='市值下限')
    p.add_argument('--max-cap', type=float, default=StrategyConfig.max_market_cap, help='市值上限')
    p.add_argument('--no-cap-filter', action='store_true',
                   help='关闭市值过滤，股票池取整个板块（对照 QMT 原脚本市值过滤失效时的口径）')
    p.add_argument('--no-rsrs', action='store_true', help='跳过 RSRS 计算（默认只打印不参与决策）')
    p.add_argument('--optimized', action='store_true',
                   help='优化版预设：%d 只等权 + 仓位 %.0f%%%% + 择时盯持仓 + 回看 %d 天 '
                        '+ 涨停按开盘价拦截' % (
                            OPTIMIZED_PRESET['max_positions'],
                            OPTIMIZED_PRESET['position_ratio'] * 100,
                            OPTIMIZED_PRESET['lookback_days']))
    p.add_argument('--max-positions', type=int, default=StrategyConfig.max_positions,
                   help='同时持有的标的数，等权分配，默认 1（满仓单票）')
    p.add_argument('--position-ratio', type=float, default=StrategyConfig.position_ratio,
                   help='仓位系数，投入资金 = 总资产 × 该系数，默认 1.0（满仓）。'
                        '实测凯利最优约 0.56，建议取更保守的 0.3~0.4')
    p.add_argument('--timing-on-holdings', action='store_true',
                   help='择时判断当前持仓而非候选股。原逻辑判断候选股，SELL 几乎不触发')
    p.add_argument('--limit-up-check', choices=['low', 'open'],
                   default=StrategyConfig.limit_up_block_field,
                   help="涨停拦截用哪个价：low=原脚本（当日最低价，未来函数）、open=开盘价")
    p.add_argument('--replicate-qmt', action='store_true',
                   help='完全复刻原 QMT 脚本的行为（含它的已知缺陷），'
                        '会覆盖下面这些开关：市值过滤关闭、不复权、跌停过滤按固定 10%%、'
                        '停牌也能卖出、买入不预留费用、开头 15 个交易日不交易')
    p.add_argument('--limit-down-ratio', type=float, default=StrategyConfig.limit_down_ratio,
                   help='跌停幅度，0=按代码前缀区分 10%%/20%%（默认），0.10=原脚本的固定 10%%')
    p.add_argument('--allow-sell-suspended', action='store_true',
                   help='允许卖出停牌股（按前收填充价成交），复刻原脚本卖出端不查停牌的行为')
    p.add_argument('--no-fee-reserve', action='store_true',
                   help='买入数量不预留手续费，复刻原脚本 int(可用资金/价格/100)*100')
    p.add_argument('--skip-warmup', action='store_true',
                   help='回测区间开头 warmup 个交易日不交易，复刻原脚本 bar_count 判断')
    p.add_argument('--filter-limit-down', action='store_true',
                   help='过滤当日跌停的候选股。这是未来函数（下单在开盘，跌停要收盘才知道），'
                        '默认不过滤，打开用于复现原脚本口径')

    p.add_argument('--commission', type=float, default=AccountConfig.commission_rate,
                   help='佣金费率，双边，默认 1e-4（万 1）')
    p.add_argument('--min-commission', type=float, default=AccountConfig.min_commission,
                   help='单笔最低佣金，默认 5 元')
    p.add_argument('--transfer-fee', type=float, default=AccountConfig.transfer_fee_rate,
                   help='过户费费率，双边，默认 1e-5（千分之 0.01）')
    p.add_argument('--stamp-tax', type=float, default=AccountConfig.stamp_tax_rate,
                   help='印花税费率，仅卖出，默认 5e-4（千分之 0.5）')

    p.add_argument('--top-scores', type=int, nargs='?', const=DEFAULT_TOP_N, default=0,
                   metavar='N',
                   help='只打印每日动量分数前 N 名（不带数字时为 %d）然后退出：'
                        '不做回测、不套任何过滤模块（市值/ST/停牌/涨跌停一律不看）'
                        % DEFAULT_TOP_N)
    p.add_argument('--scores-csv', default='',
                   help='排行榜输出路径，默认 results/scores_<起止日期>_lb<N>_top<M>.csv')
    p.add_argument('--scores-days', type=int, default=0,
                   help='终端最多打印最后多少个交易日的排行，0=全部打印（csv 始终是全量）')

    p.add_argument('--eval-scores', default='', metavar='CSV',
                   help='读入排行榜 csv，算「次日开盘买入、第二天开盘卖出」的收益并退出。'
                        '买卖都取开盘价，逐日首尾相接，可直接连乘成净值')
    p.add_argument('--entry-delay', type=int, default=1, metavar='N',
                   help='--eval-scores：信号日之后第 N 个交易日开盘买入，默认 1（次日）')
    p.add_argument('--hold-days', type=int, default=1, metavar='N',
                   help='--eval-scores：买入后持有 N 个交易日，在那天开盘卖出，默认 1')
    p.add_argument('--skip-limit-up', action='store_true',
                   help='--eval-scores：剔除买入日开盘就涨停的信号（挂单买不进），'
                        '该日按持币计入曲线')
    p.add_argument('--limit-tolerance', type=float, default=0.003, metavar='X',
                   help='--eval-scores：涨停判定的容差，默认 0.003（10%% 的票按 9.7%% 算封板）')
    p.add_argument('--eval-ranks', type=int, nargs='+', default=[], metavar='K',
                   help='--eval-scores：只评估这些名次，默认全部')
    p.add_argument('--eval-out', default='',
                   help='--eval-scores：逐笔明细输出路径，默认榜单同目录 <榜单名>_returns.csv')

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
        if not args.data_dir:
            raise SystemExit('--source csv 需要同时指定 --data-dir')
        return CsvDataSource(args.data_dir)

    return XtDataSource(use_cache=not args.no_cache, dividend_type=args.dividend_type)


QMT_PRESET = {
    'no_cap_filter': True,        # 原脚本市值过滤因 NameError 被吞而失效，池子=全市场
    'dividend_type': 'none',      # 原脚本 dividend_type='none'
    'filter_limit_down': True,    # 原脚本按当日收盘价判断跌停
    'limit_down_ratio': 0.10,     # 且恒用固定 10%，不区分创业板/科创板
    'no_fee_reserve': True,       # 原脚本买入量不预留手续费
    'skip_warmup': True,          # 原脚本前 LOOKBACK+10 根 bar 不交易
}

# 原脚本卖出端不查停牌，会按前收填充价把停牌股脱手 —— 这一条实盘根本做不到，
# 默认不复刻；确实要完全对齐原脚本时另加 --allow-sell-suspended
QMT_NOT_REPLICATED = 'allow_sell_suspended'


def apply_qmt_preset(args) -> None:
    """把命令行参数整体切到原 QMT 脚本的口径（含它的已知缺陷）"""
    for key, value in QMT_PRESET.items():
        setattr(args, key, value)
    print('[复刻模式] 已切换到原 QMT 脚本口径：')
    print('  市值过滤关闭 / 不复权 / 跌停按固定 10% 过滤')
    print('  / 买入不预留费用 / 开头预热期不交易')
    print('  注意：这些是为了对齐原脚本而保留的缺陷，结果会偏乐观，不要用来评估策略本身')
    if not getattr(args, QMT_NOT_REPLICATED, False):
        print('  唯一没有复刻的一项：停牌股仍然卖不掉，要等复牌当天才卖')
        print('  （原脚本会按前收填充价脱手，实盘做不到；加 --allow-sell-suspended 可对齐）')


def apply_optimized_preset(args) -> None:
    """切到优化版口径（基于实测诊断：波动损耗吃掉算术收益的 90%）"""
    args.max_positions = OPTIMIZED_PRESET['max_positions']
    args.position_ratio = OPTIMIZED_PRESET['position_ratio']
    args.timing_on_holdings = OPTIMIZED_PRESET['timing_on_holdings']
    args.limit_up_check = OPTIMIZED_PRESET['limit_up_block_field']
    if args.lookback == [StrategyConfig.lookback_days]:      # 用户没显式指定才覆盖
        args.lookback = [OPTIMIZED_PRESET['lookback_days']]

    print('[优化模式] 已切换到优化口径：')
    print('  %d 只等权 / 仓位 %.0f%% / 择时盯持仓 / 回看 %s 天 / 涨停按开盘价拦截'
          % (args.max_positions, args.position_ratio * 100, args.lookback))
    print('  依据：实测 μ=+0.38%/天、σ=8.2%/天，波动损耗 σ²/2 吃掉算术收益的 90%，')
    print('        凯利最优仓位 k*≈0.56，这里取更保守的 %.2f' % args.position_ratio)


def run_label(args, cfg: StrategyConfig) -> str:
    """
    回测结果目录名：起止日期 + 回看天数，非默认的关键参数再追加短标签，
    这样不同参数的结果放在一起也能一眼区分。
    例：bt_20240101_20241231_lb29_dd1_ld
    """
    default = StrategyConfig()
    parts = [f'bt_{args.start}_{args.end}', f'lb{cfg.lookback_days}']

    if cfg.decline_days_to_sell != default.decline_days_to_sell:
        parts.append(f'dd{cfg.decline_days_to_sell}')
    if cfg.stop_loss_ratio != default.stop_loss_ratio:
        parts.append('sl%g' % round(abs(cfg.stop_loss_ratio) * 100, 4))
    if getattr(args, 'optimized', False):
        parts.append('opt')
    if cfg.max_positions != default.max_positions:
        parts.append(f'n{cfg.max_positions}')
    if cfg.position_ratio != default.position_ratio:
        parts.append('pos%g' % round(cfg.position_ratio * 100, 4))
    if cfg.timing_on_holdings:
        parts.append('hold')
    if cfg.limit_up_block_field != default.limit_up_block_field:
        parts.append('up' + cfg.limit_up_block_field)
    if getattr(args, 'replicate_qmt', False):
        parts.append('qmt')
    if not cfg.filter_market_cap:
        parts.append('nocap')
    if cfg.filter_limit_down:
        parts.append('ld')
    if not cfg.rsrs_enabled:
        parts.append('norsrs')
    dividend = getattr(args, 'dividend_type', DEFAULT_DIVIDEND_TYPE)
    if dividend != DEFAULT_DIVIDEND_TYPE:
        parts.append(f'div{dividend}')
    return '_'.join(parts)


def suffix_path(path: str, tag: str) -> str:
    """给单独指定的输出文件加参数后缀，避免多次回测互相覆盖"""
    if not path or not tag:
        return path
    base, ext = os.path.splitext(path)
    return f'{base}_{tag}{ext}'


COMPARE_COLUMNS = (('回看天数', 10, True), ('期末资产', 13, False), ('总收益', 10, False),
                   ('年化', 10, False), ('最大回撤', 10, False), ('夏普', 7, False),
                   ('交易笔数', 9, False), ('卖出胜率', 9, False))


def compare_table(rows) -> str:
    """多组参数跑完后的横向对比"""
    header = '  ' + ' '.join(pad(name, width, left) for name, width, left in COMPARE_COLUMNS)
    lines = ['', '=' * display_width(header), '[参数对比]', '=' * display_width(header), header]

    for label, perf in rows:
        if perf is None:
            lines.append('  ' + pad(label, COMPARE_COLUMNS[0][1], True) + ' 无有效交易日')
            continue
        values = [label, f'{perf.final_asset:,.0f}', f'{perf.total_return:.2%}',
                  f'{perf.annual_return:.2%}', f'{perf.max_drawdown:.2%}',
                  f'{perf.sharpe:.2f}', str(perf.buy_count + perf.sell_count),
                  f'{perf.win_rate:.1%}']
        lines.append('  ' + ' '.join(pad(v, w, left)
                                     for v, (_, w, left) in zip(values, COMPARE_COLUMNS)))
    return '\n'.join(lines)


def scoreboard_path(args, lookback: int, multi: bool) -> str:
    """排行榜 csv 路径。--no-save 时只打印不落盘。"""
    if args.no_save:
        return ''
    if args.scores_csv:
        return suffix_path(args.scores_csv, f'lb{lookback}' if multi else '')
    name = f'scores_{args.start}_{args.end}_lb{lookback}_top{args.top_scores}.csv'
    return os.path.join(args.out_dir or 'results', name)


def run_scoreboards(args, sectors, lookbacks) -> int:
    """--top-scores：只算分数排行，不进回测"""
    multi = len(lookbacks) > 1
    for n, lookback in enumerate(lookbacks, 1):
        if multi:
            print('\n' + '=' * 78)
            print(f'[排行榜 {n}/{len(lookbacks)}] 回看 {lookback} 天')
            print('=' * 78)

        config = BacktestConfig(
            start_date=args.start,
            end_date=args.end,
            strategy=StrategyConfig(concept_sectors=sectors, lookback_days=lookback),
        )
        run_scoreboard(
            build_source(args), config,
            top_n=args.top_scores,
            out_path=scoreboard_path(args, lookback, multi),
            download=args.download and n == 1,
            max_print_days=args.scores_days,
        )
    return 0


def eval_out_path(args) -> str:
    """逐笔明细默认落在榜单旁边，文件名带上买卖口径，不同参数不互相覆盖"""
    if args.no_save:
        return ''
    if args.eval_out:
        return args.eval_out
    base, ext = os.path.splitext(args.eval_scores)
    tag = f'd{args.entry_delay}h{args.hold_days}'
    if args.eval_ranks:
        tag += 'r' + ''.join(str(r) for r in sorted(args.eval_ranks))
    if args.skip_limit_up:
        tag += '_noup'
    return f'{base}_returns_{tag}{ext or ".csv"}'


def run_score_eval(args) -> int:
    """--eval-scores：按榜单算「次日开盘买入、第二天开盘卖出」的收益"""
    run_score_returns(
        build_source(args), args.eval_scores,
        delay=args.entry_delay,
        hold=args.hold_days,
        account=AccountConfig(
            commission_rate=args.commission,
            min_commission=args.min_commission,
            transfer_fee_rate=args.transfer_fee,
            stamp_tax_rate=args.stamp_tax,
        ),
        ranks=args.eval_ranks,
        out_path=eval_out_path(args),
        download=args.download,
        skip_limit_up=args.skip_limit_up,
        limit_tolerance=args.limit_tolerance,
    )
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(not args.quiet)

    if args.check_data:
        ok = check_xtdata(start_date=args.start, end_date=args.end,
                          try_download=args.download)
        return 0 if ok else 1

    if args.optimized and args.replicate_qmt:
        raise SystemExit('--optimized 与 --replicate-qmt 互斥，两者口径相反')
    if args.optimized:
        apply_optimized_preset(args)
    if args.replicate_qmt:
        apply_qmt_preset(args)

    sectors = tuple(s.strip() for s in args.sectors.split(',') if s.strip()) \
        or CONCEPT_SECTORS_DEFAULT

    lookbacks = list(dict.fromkeys(args.lookback))

    if args.eval_scores:
        return run_score_eval(args)

    if args.top_scores:
        return run_scoreboards(args, sectors, lookbacks)

    multi = len(lookbacks) > 1
    engine_cls = VectorBacktestEngine if args.engine == 'fast' else BacktestEngine
    stamp = datetime.now().strftime('%H%M%S')
    summary = []

    for n, lookback in enumerate(lookbacks, 1):
        config = BacktestConfig(
            start_date=args.start,
            end_date=args.end,
            strategy=StrategyConfig(
                concept_sectors=sectors,
                lookback_days=lookback,
                stop_loss_ratio=args.stop_loss,
                decline_days_to_sell=args.decline_days,
                filter_market_cap=not args.no_cap_filter,
                min_market_cap=args.min_cap,
                max_market_cap=args.max_cap,
                rsrs_enabled=not args.no_rsrs,
                filter_limit_down=args.filter_limit_down,
                limit_down_ratio=args.limit_down_ratio,
                allow_sell_suspended=args.allow_sell_suspended,
                skip_warmup_bars=args.skip_warmup,
                max_positions=args.max_positions,
                position_ratio=args.position_ratio,
                timing_on_holdings=args.timing_on_holdings,
                limit_up_block_field=args.limit_up_check,
            ),
            account=AccountConfig(
                init_cash=args.cash,
                commission_rate=args.commission,
                min_commission=args.min_commission,
                transfer_fee_rate=args.transfer_fee,
                stamp_tax_rate=args.stamp_tax,
                reserve_fee_on_buy=not args.no_fee_reserve,
            ),
        )
        label = run_label(args, config.strategy)

        if multi:
            print('\n' + '=' * 78)
            print(f'[回测 {n}/{len(lookbacks)}] 回看 {lookback} 天')
            print('=' * 78)

        engine = engine_cls(build_source(args), config)
        started = time.time()
        # 只有第一次需要补下载，后续几次数据已经在本地
        result = engine.run(download=args.download and n == 1, show_progress=args.quiet)
        elapsed = time.time() - started

        perf = evaluate(result, config.strategy.trading_days_per_year)
        print(format_report(perf))
        print(f'  回看天数  : {lookback}')
        print(f'  回测耗时  : {elapsed:.1f} 秒（{args.engine} 引擎）')

        if not args.no_save:
            if args.out_dir:
                out_dir = os.path.join(args.out_dir, label) if multi else args.out_dir
            else:
                out_dir = os.path.join('results', f'{label}_{stamp}')
            save_results(
                result, out_dir, perf,
                name_lookup=engine.source.get_stock_name,
                extra={
                    'label': label,
                    'start_date': args.start,
                    'end_date': args.end,
                    'init_cash': args.cash,
                    'engine': args.engine,
                    'dividend_type': args.dividend_type,
                    'replicate_qmt': args.replicate_qmt,
                    'optimized': args.optimized,
                    'elapsed_seconds': round(elapsed, 2),
                    'sectors': list(sectors),
                    'strategy': asdict(config.strategy),
                    'account': asdict(config.account),
                },
            )

        tag = f'lb{lookback}' if multi else ''
        save_csv(result, suffix_path(args.equity_csv, tag), suffix_path(args.deals_csv, tag))
        summary.append((f'{lookback} 天', perf))

    if multi:
        print(compare_table(summary))

    return 0


if __name__ == '__main__':
    raise SystemExit(main(['--optimized'] + sys.argv[1:]))
