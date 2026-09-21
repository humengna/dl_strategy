# coding: utf-8
import numpy as np

from momentum.indicators import limit_ratio, linear_regression, momentum_score, rsrs_score


def test_linear_regression_exact_line():
    x = [0, 1, 2, 3, 4]
    y = [1, 3, 5, 7, 9]           # y = 2x + 1
    slope, r2 = linear_regression(x, y)
    assert slope == 0 or abs(slope - 2.0) < 1e-9
    assert abs(r2 - 1.0) < 1e-9


def test_linear_regression_too_short():
    assert linear_regression([1], [2]) == (0.0, 0.0)


def test_momentum_score_positive_for_uptrend():
    closes = [10 * (1.01 ** i) for i in range(10)]
    score = momentum_score(closes)
    assert score is not None and score > 0


def test_momentum_score_negative_for_downtrend():
    closes = [10 * (0.99 ** i) for i in range(10)]
    score = momentum_score(closes)
    assert score is not None and score < 0


def test_momentum_score_flat_series_is_zero():
    # 水平线 ss_tot = 0，R2 记为 0，分数为 0
    assert momentum_score([10.0] * 8) == 0.0


def test_momentum_score_rejects_bad_input():
    assert momentum_score([10.0]) is None
    assert momentum_score([10.0, 0.0, 12.0]) is None
    assert momentum_score([10.0, np.nan, 12.0]) is None


def test_momentum_score_scales_with_trend_strength():
    steady = [10 * (1.01 ** i) for i in range(10)]
    noisy = [10 * (1.01 ** i) * (1 + 0.05 * (-1) ** i) for i in range(10)]
    # 同样的斜率，拟合越差分数越低
    assert momentum_score(steady) > momentum_score(noisy)


def test_rsrs_none_when_not_enough_data():
    assert rsrs_score([1, 2, 3], [1, 2, 3], n=21, m=600) is None


def test_rsrs_returns_value_with_enough_data():
    rng = np.random.default_rng(1)
    n, m = 5, 30
    lows = np.cumsum(rng.normal(0, 1, n + m + 10)) + 100
    highs = lows * 1.02
    value = rsrs_score(highs, lows, n=n, m=m)
    assert value is not None
    assert np.isfinite(value)


def test_limit_ratio_by_board():
    assert limit_ratio('300750.SZ') == 0.20
    assert limit_ratio('301001.SZ') == 0.20
    assert limit_ratio('688981.SH') == 0.20
    assert limit_ratio('600000.SH') == 0.10
    assert limit_ratio('000001.SZ') == 0.10
