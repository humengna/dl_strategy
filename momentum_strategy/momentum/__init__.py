# coding: utf-8
"""动量择时策略：概念池 + 对数线性回归动量打分 + RSRS + 连续下降择时 + 硬止损。"""

from .broker import Deal, Position, SimAccount
from .config import AccountConfig, BacktestConfig, StrategyConfig
from .datasource import CsvDataSource, DataSource, XtDataSource
from .engine import BacktestEngine, BacktestResult
from .indicators import limit_ratio, linear_regression, momentum_score, rsrs_score
from .report import Performance, evaluate, format_report
from .selector import filter_target, momentum_series, pick_target, rank_by_momentum
from .timing import SIGNAL_BUY, SIGNAL_KEEP, SIGNAL_SELL, count_decline_days, timing_signal
from .universe import build_base_pool, filter_universe

__version__ = '0.1.0'

__all__ = [
    'AccountConfig', 'BacktestConfig', 'StrategyConfig',
    'DataSource', 'XtDataSource', 'CsvDataSource',
    'SimAccount', 'Position', 'Deal',
    'BacktestEngine', 'BacktestResult',
    'linear_regression', 'momentum_score', 'rsrs_score', 'limit_ratio',
    'build_base_pool', 'filter_universe',
    'rank_by_momentum', 'pick_target', 'momentum_series', 'filter_target',
    'timing_signal', 'count_decline_days', 'SIGNAL_BUY', 'SIGNAL_SELL', 'SIGNAL_KEEP',
    'evaluate', 'format_report', 'Performance',
]
