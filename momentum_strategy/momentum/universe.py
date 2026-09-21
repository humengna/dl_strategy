# coding: utf-8
"""步骤 1：股票池构建与过滤（停牌 / ST / 市值）。"""

import logging
from typing import List, Sequence

from .config import StrategyConfig
from .datasource import DataSource

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
