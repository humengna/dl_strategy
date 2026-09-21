# coding: utf-8
from momentum.config import StrategyConfig
from momentum.timing import SIGNAL_BUY, SIGNAL_KEEP, SIGNAL_SELL, count_decline_days, timing_signal


def test_count_decline_days():
    assert count_decline_days([1, 2, 3, 4]) == 0
    assert count_decline_days([1, 2, 3, 2]) == 1
    assert count_decline_days([1, 5, 3, 2]) == 2
    assert count_decline_days([5, 4, 3, 2]) == 3
    assert count_decline_days([]) == 0


def test_timing_signal_sell_after_two_declines():
    cfg = StrategyConfig()
    assert timing_signal([1, 5, 3, 2], cfg) == SIGNAL_SELL


def test_timing_signal_buy_with_single_decline():
    cfg = StrategyConfig()
    assert timing_signal([1, 2, 3, 2], cfg) == SIGNAL_BUY


def test_timing_signal_keep_when_no_scores():
    assert timing_signal([], StrategyConfig()) == SIGNAL_KEEP


def test_decline_threshold_is_configurable():
    cfg = StrategyConfig(decline_days_to_sell=3)
    assert timing_signal([1, 5, 3, 2], cfg) == SIGNAL_BUY
    assert timing_signal([9, 5, 3, 2], cfg) == SIGNAL_SELL


def test_decline_epsilon_ignores_float_noise():
    """分数在浮点噪声级别上相等时，容差可避免被误判为下降"""
    scores = [10.0, 10.0 - 1e-13, 10.0 - 2e-13]
    assert count_decline_days(scores) == 2                      # 严格比较：算下降
    assert count_decline_days(scores, epsilon=1e-9) == 0        # 带容差：不算

    cfg = StrategyConfig(decline_epsilon=1e-9)
    assert timing_signal(scores, cfg) == SIGNAL_BUY
    assert timing_signal(scores, StrategyConfig()) == SIGNAL_SELL


def test_decline_epsilon_still_catches_real_declines():
    cfg = StrategyConfig(decline_epsilon=1e-9)
    assert timing_signal([9.0, 5.0, 3.0], cfg) == SIGNAL_SELL
