# coding: utf-8
import pytest

from momentum.config import StrategyConfig
from momentum.indicators import momentum_score
from momentum.sample_data import make_calendar
from momentum.selector import (filter_target, momentum_series, pick_target,
                               price_and_limits, rank_by_momentum)
from tests.conftest import make_frame


@pytest.fixture
def cfg():
    return StrategyConfig(lookback_days=5)


def test_rank_picks_strongest_trend(source_factory, cfg):
    dates = make_calendar(12, start='20240102')
    frames = {
        '600000.SH': make_frame(dates, [10 * (1.03 ** i) for i in range(12)]),   # 最强
        '000001.SZ': make_frame(dates, [10 * (1.01 ** i) for i in range(12)]),
        '000002.SZ': make_frame(dates, [10 * (0.98 ** i) for i in range(12)]),   # 下跌
    }
    source = source_factory(frames)

    ranked = rank_by_momentum(source, list(frames), dates[-1], cfg)
    assert [s for s, _ in ranked] == ['600000.SH', '000001.SZ', '000002.SZ']
    assert pick_target(source, list(frames), dates[-1], cfg) == '600000.SH'


def test_rank_excludes_current_bar(source_factory, cfg):
    """当日 K 线不得参与打分：最后一根改成天量暴涨，排序分数必须不变"""
    dates = make_calendar(12, start='20240102')
    closes = [10 * (1.01 ** i) for i in range(12)]

    normal = source_factory({'600000.SH': make_frame(dates, closes)})
    spiked_closes = list(closes[:-1]) + [closes[-1] * 3]
    spiked = source_factory({'600000.SH': make_frame(dates, spiked_closes)})

    score_normal = rank_by_momentum(normal, ['600000.SH'], dates[-1], cfg)[0][1]
    score_spiked = rank_by_momentum(spiked, ['600000.SH'], dates[-1], cfg)[0][1]
    assert score_normal == pytest.approx(score_spiked)

    # 并且等于「排除当前 bar 的前 5 根收盘价」直接算出来的分数
    expected = momentum_score(closes[-6:-1], cfg.trading_days_per_year)
    assert score_normal == pytest.approx(expected)


def test_rank_skips_stocks_with_insufficient_history(source_factory, cfg):
    dates = make_calendar(12, start='20240102')
    short_dates = dates[-3:]
    frames = {
        '600000.SH': make_frame(dates, [10 * (1.01 ** i) for i in range(12)]),
        '000001.SZ': make_frame(short_dates, [10, 11, 12]),
    }
    ranked = rank_by_momentum(source_factory(frames), list(frames), dates[-1], cfg)
    assert [s for s, _ in ranked] == ['600000.SH']


def test_momentum_series_length_and_last_value(source_factory, cfg):
    dates = make_calendar(20, start='20240102')
    closes = [10 * (1.01 ** i) for i in range(20)]
    source = source_factory({'600000.SH': make_frame(dates, closes)})

    scores = momentum_series(source, '600000.SH', dates[-1], cfg)
    assert len(scores) == cfg.score_series_len + 1
    # 最后一个值 = 排除当前 bar 的最近 5 根收盘价打分
    assert scores[-1] == pytest.approx(momentum_score(closes[-6:-1], cfg.trading_days_per_year))


def test_momentum_series_empty_when_history_short(source_factory, cfg):
    dates = make_calendar(4, start='20240102')
    source = source_factory({'600000.SH': make_frame(dates, [10, 11, 12, 13])})
    assert momentum_series(source, '600000.SH', dates[-1], cfg) == []


def test_filter_target_rejects_suspended(source_factory, cfg):
    dates = make_calendar(3, start='20240102')
    frame = make_frame(dates, [10, 10, 10], suspend=[0, 0, 1])
    source = source_factory({'600000.SH': frame})
    assert filter_target(source, '600000.SH', dates[-1], cfg) is None


def test_filter_target_keeps_stock_that_closes_at_limit_down(source_factory, cfg):
    """
    跌停要当日收盘价才能确认，而下单在开盘。
    收盘跌停的票在开盘那一刻并不可知，不能拿来过滤。
    """
    dates = make_calendar(2, start='20240102')
    frame = make_frame(dates, [10.0, 9.0], opens=[10.0, 9.9], pre_closes=[10.0, 10.0])
    source = source_factory({'600000.SH': frame})
    assert filter_target(source, '600000.SH', dates[-1], cfg) == '600000.SH'


def test_filter_target_ignores_todays_close(source_factory, cfg):
    """当日收盘价怎么变都不该影响开盘时的过滤结果（未来函数守卫）"""
    dates = make_calendar(2, start='20240102')

    def build(last_close):
        frame = make_frame(dates, [10.0, last_close], opens=[10.0, 10.0],
                           pre_closes=[10.0, 10.0])
        return source_factory({'600000.SH': frame})

    results = {filter_target(build(c), '600000.SH', dates[-1], cfg)
               for c in (5.0, 9.0, 10.0, 11.0, 20.0)}
    assert results == {'600000.SH'}


def test_filter_target_rejects_invalid_open(source_factory, cfg):
    dates = make_calendar(2, start='20240102')
    frame = make_frame(dates, [10.0, 10.0], opens=[10.0, 0.0], pre_closes=[10.0, 10.0])
    source = source_factory({'600000.SH': frame})
    assert filter_target(source, '600000.SH', dates[-1], cfg) is None


def test_filter_target_none_for_unknown_stock(source_factory, cfg):
    source = source_factory({})
    assert filter_target(source, '600000.SH', '20240110', cfg) is None
    assert filter_target(source, None, '20240110', cfg) is None


def test_price_and_limits(source_factory):
    dates = make_calendar(2, start='20240102')
    frame = make_frame(dates, [10.0, 10.5], opens=[10.0, 10.2],
                       lows=[9.8, 10.1], pre_closes=[10.0, 10.0])
    source = source_factory({'600000.SH': frame})

    open_price, up, down, low = price_and_limits(source, '600000.SH', dates[-1])
    assert open_price == pytest.approx(10.2)
    assert up == pytest.approx(11.0)      # 主板 +10%
    assert down == pytest.approx(9.0)
    assert low == pytest.approx(10.1)


def test_price_and_limits_missing_stock(source_factory):
    assert price_and_limits(source_factory({}), '600000.SH', '20240110') == (0.0, 0.0, 0.0, 0.0)
