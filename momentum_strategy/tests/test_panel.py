# coding: utf-8
"""向量化指标核必须和逐点 polyfit 实现给出一样的数值。"""

import numpy as np
import pytest

from momentum.indicators import momentum_score, rsrs_score
from momentum.panel import (Panel, momentum_score_matrix, rolling_linreg_fixed_x,
                            rolling_linreg_xy, rsrs_series)
from momentum.sample_data import make_calendar
from tests.conftest import make_frame


@pytest.fixture
def prices():
    rng = np.random.default_rng(11)
    close = 10 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, (120, 12)), axis=0))
    close[40, 2] = np.nan          # 缺失
    close[60, 5] = 0.0             # 非正价
    close[70:80, 8] = 15.0         # 走平
    return close


def test_rolling_linreg_matches_polyfit():
    rng = np.random.default_rng(5)
    y = rng.normal(0, 1, (40, 3))
    slope, r2, ok = rolling_linreg_fixed_x(y, 6)

    for i in range(5, 40):
        for j in range(3):
            window = y[i - 5:i + 1, j]
            p_slope, p_int = np.polyfit(np.arange(6.0), window, 1)
            assert ok[i, j]
            assert slope[i, j] == pytest.approx(p_slope, rel=1e-9, abs=1e-12)


def test_momentum_matrix_matches_reference(prices):
    lookback = 5
    matrix = momentum_score_matrix(prices, lookback)

    for i in range(lookback + 1, prices.shape[0]):
        for j in range(prices.shape[1]):
            ref = momentum_score(prices[i - lookback:i, j])
            got = matrix[i, j]
            if ref is None:
                assert np.isnan(got), f'{i},{j} 应为 nan'
            else:
                assert got == pytest.approx(ref, rel=1e-9, abs=1e-12), f'{i},{j}'


def test_momentum_matrix_excludes_current_bar(prices):
    """把某一天的价格改掉，不能影响当天及更早的分数"""
    lookback = 5
    base = momentum_score_matrix(prices, lookback)

    bumped = prices.copy()
    bumped[60, 0] *= 3
    after = momentum_score_matrix(bumped, lookback)

    np.testing.assert_allclose(base[:61, 0], after[:61, 0], equal_nan=True)
    assert not np.allclose(base[61, 0], after[61, 0], equal_nan=True)


def test_flat_prices_score_zero():
    close = np.full((20, 1), 12.0)
    matrix = momentum_score_matrix(close, 5)
    assert matrix[10, 0] == 0.0


def test_matrix_nan_when_window_incomplete():
    close = np.full((20, 1), 12.0)
    close[8, 0] = np.nan
    matrix = momentum_score_matrix(close, 5)
    assert np.isnan(matrix[10, 0])       # 窗口 [5:10) 含缺失
    assert not np.isnan(matrix[15, 0])


def test_rsrs_series_matches_reference():
    rng = np.random.default_rng(13)
    n, window, m = 260, 8, 120
    low = 3800 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    high = low * (1 + np.abs(rng.normal(0, 0.005, n)))

    series = rsrs_series(high, low, window, m)
    for i in (200, 230, 259):
        ref = rsrs_score(high[:i], low[:i], n=window, m=m)
        assert series[i] == pytest.approx(ref, rel=1e-8, abs=1e-10)


def test_rsrs_series_nan_without_enough_history():
    rng = np.random.default_rng(1)
    low = 100 + np.cumsum(rng.normal(0, 1, 50))
    high = low * 1.01
    series = rsrs_series(high, low, 8, 120)
    assert np.isnan(series).all()


def test_rolling_linreg_xy_matches_polyfit():
    rng = np.random.default_rng(7)
    x = rng.normal(100, 5, 60)
    y = x * 1.02 + rng.normal(0, 0.3, 60)
    slope, r2, ok = rolling_linreg_xy(x, y, 10)

    for i in (20, 40, 59):
        p_slope, _ = np.polyfit(x[i - 9:i + 1], y[i - 9:i + 1], 1)
        assert slope[i] == pytest.approx(p_slope, rel=1e-8, abs=1e-10)


# ---------------- Panel ----------------

def test_panel_aligns_stocks_on_common_calendar(source_factory):
    dates = make_calendar(10, start='20240102')
    frames = {
        '600000.SH': make_frame(dates, [10 + i for i in range(10)]),
        '000001.SZ': make_frame(dates[3:], [20 + i for i in range(7)]),   # 晚上市
    }
    panel = Panel.from_frames(frames)

    assert panel.dates == dates
    assert panel.stocks == ['000001.SZ', '600000.SH']
    close = panel.field('close')
    assert np.isnan(close[0, 0])              # 000001.SZ 前三天没有数据
    assert close[3, 0] == 20
    assert close[0, 1] == 10


def test_panel_index_lookup(source_factory):
    dates = make_calendar(5, start='20240102')
    panel = Panel.from_frames({'600000.SH': make_frame(dates, [1, 2, 3, 4, 5])})
    assert panel.index_of(dates[2]) == 2
    assert panel.index_of('20990101') == 4       # 晚于末日 -> 最后一行
    assert panel.index_of('20000101') == -1      # 早于首日


def test_panel_from_source(source_factory):
    dates = make_calendar(8, start='20240102')
    frames = {'600000.SH': make_frame(dates, [10] * 8),
              '000001.SZ': make_frame(dates, [20] * 8)}
    source = source_factory(frames)

    panel = Panel.from_source(source, ['600000.SH', '000001.SZ'], dates[2], dates[-1])
    assert panel.dates == dates[2:]
    assert panel.shape == (6, 2)
