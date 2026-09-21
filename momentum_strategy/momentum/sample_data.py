# coding: utf-8
"""
合成样例行情，用于单元测试和没有 QMT 环境时跑通完整流程。
生成的数据结构与 CsvDataSource 期望的一致。
"""

import json
import os
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

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
    from .datasource import CsvDataSource

    frames, details, sectors = make_sample_frames(days, stocks, index_code or '', start, seed)
    return CsvDataSource.from_frames(frames, details, sectors)
