# coding: utf-8
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from momentum.datasource import CsvDataSource  # noqa: E402
from momentum.sample_data import make_calendar  # noqa: E402


def make_frame(dates, closes, opens=None, highs=None, lows=None,
               pre_closes=None, suspend=None) -> pd.DataFrame:
    """用给定收盘价序列构造一张日线表，其余字段按需补齐"""
    opens = opens if opens is not None else list(closes)
    highs = highs if highs is not None else [max(o, c) * 1.01 for o, c in zip(opens, closes)]
    lows = lows if lows is not None else [min(o, c) * 0.99 for o, c in zip(opens, closes)]
    if pre_closes is None:
        pre_closes = [closes[0]] + list(closes[:-1])
    suspend = suspend if suspend is not None else [0] * len(closes)

    return pd.DataFrame({
        'open': opens, 'high': highs, 'low': lows, 'close': list(closes),
        'preClose': pre_closes, 'volume': [1e6] * len(closes),
        'suspendFlag': suspend,
    }, index=list(dates))


@pytest.fixture
def calendar():
    return make_calendar(60, start='20240102')


@pytest.fixture
def source_factory():
    def _make(frames, details=None, sectors=None):
        details = details or {s: {'InstrumentName': f'测试{s[:6]}', 'TotalValue': 100e8}
                              for s in frames}
        sectors = sectors or {'沪深A股': list(frames)}
        return CsvDataSource.from_frames(frames, details, sectors)
    return _make
