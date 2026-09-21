# coding: utf-8
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

import logging
from typing import List, Optional

import numpy as np

from .config import BacktestConfig
from .datasource import DataSource
from .engine import BacktestEngine
from .indicators import limit_ratio
from .panel import Panel, momentum_score_matrix, rsrs_series
from .timing import SIGNAL_KEEP, timing_signal
from .universe import build_base_pool

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
                '  - 加 --download 让脚本补下载，或在 QMT 客户端「行情 -> 数据管理」补充日线\n'
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

        with np.errstate(invalid='ignore'):
            cap = np.where(total_value[None, :] > 0, total_value[None, :],
                           shares[None, :] * np.where(close_ok, close, np.nan))
            cap_ok = ~(np.isfinite(cap) & (cap > 0)) | (
                (cap >= self.cfg.min_market_cap) & (cap <= self.cfg.max_market_cap))

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

        j = int(np.argmax(np.where(usable, row, -np.inf)))
        target = self.panel.stocks[j]
        log.info('步骤2 - 目标: %s %s（分数 %.4f）', target,
                 self.source.get_stock_name(target), row[j])

        scores = self.series_at(i, j)
        self.score_series = {target: scores}
        log.info('步骤3 - 动量分数序列: %s', [round(float(s), 4) for s in scores])

        if not bool(self.tradable[i, j]):
            log.info('步骤4 - 目标股票被过滤（停牌或跌停）')
            return self.review(date)
        self.today_target = target
        log.info('步骤4 - 过滤通过: %s', target)

        if self.rsrs is not None and np.isfinite(self.rsrs[i]):
            log.info('RSRS修正标准分: %.4f', self.rsrs[i])
        else:
            log.info('RSRS修正标准分: 数据不足')

        signal = timing_signal(scores, self.cfg) if scores else SIGNAL_KEEP
        self._last_signal = signal
        log.info('步骤5 - 择时信号: %s', signal)

        self.adjust_position(date, target, signal)
        log.info('步骤6 - 调仓执行完毕')

        self.check_stop_loss(date)
        return self.review(date)

    def series_at(self, i: int, j: int) -> List[float]:
        """动量分数序列：分数矩阵第 j 列的一段，缺失记 0.0（与逐日版一致）"""
        length = self.cfg.score_series_len
        if i - length < 0:
            return []
        window = self.scores[i - length:i + 1, j]
        return [0.0 if not np.isfinite(v) else float(v) for v in window]
