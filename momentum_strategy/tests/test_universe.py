# coding: utf-8
from momentum.config import StrategyConfig
from momentum.sample_data import make_calendar
from momentum.universe import build_base_pool, filter_universe
from tests.conftest import make_frame


def build_source(source_factory, suspend_last=False):
    dates = make_calendar(5, start='20240102')
    stocks = ['600000.SH', '000001.SZ', '300750.SZ', '688981.SH']
    frames = {}
    for s in stocks:
        suspend = [0, 0, 0, 0, 1] if (suspend_last and s == '000001.SZ') else None
        frames[s] = make_frame(dates, [10, 11, 12, 13, 14], suspend=suspend)
    details = {
        '600000.SH': {'InstrumentName': '浦发银行', 'TotalValue': 100e8},
        '000001.SZ': {'InstrumentName': '平安银行', 'TotalValue': 100e8},
        '300750.SZ': {'InstrumentName': 'ST宁德', 'TotalValue': 100e8},     # ST
        '688981.SH': {'InstrumentName': '中芯国际', 'TotalValue': 900e8},   # 超出市值上限
    }
    sectors = {'沪深A股': stocks}
    return dates, source_factory(frames, details, sectors)


def test_build_base_pool_from_sector(source_factory):
    _, source = build_source(source_factory)
    pool = build_base_pool(source, StrategyConfig())
    assert pool == ['000001.SZ', '300750.SZ', '600000.SH', '688981.SH']


def test_build_base_pool_can_exclude_boards(source_factory):
    _, source = build_source(source_factory)
    cfg = StrategyConfig(exclude_gem=True, exclude_star=True)
    assert build_base_pool(source, cfg) == ['000001.SZ', '600000.SH']


def test_build_base_pool_empty_sector(source_factory):
    source = source_factory({}, {}, {'沪深A股': []})
    assert build_base_pool(source, StrategyConfig()) == []


def test_filter_universe_drops_st_and_oversized(source_factory):
    dates, source = build_source(source_factory)
    cfg = StrategyConfig()
    pool = filter_universe(source, build_base_pool(source, cfg), dates[-1], cfg)
    assert pool == ['000001.SZ', '600000.SH']


def test_filter_universe_drops_suspended(source_factory):
    dates, source = build_source(source_factory, suspend_last=True)
    cfg = StrategyConfig()
    pool = filter_universe(source, build_base_pool(source, cfg), dates[-1], cfg)
    assert pool == ['600000.SH']


def test_filter_universe_drops_below_min_cap(source_factory):
    dates, source = build_source(source_factory)
    cfg = StrategyConfig(min_market_cap=200e8, max_market_cap=1000e8)
    pool = filter_universe(source, build_base_pool(source, cfg), dates[-1], cfg)
    assert pool == ['688981.SH']      # 只有它市值 900 亿在区间内（300750 因 ST 被剔除）


def test_filter_universe_keeps_stock_without_market_cap(source_factory):
    dates = make_calendar(3, start='20240102')
    frames = {'600000.SH': make_frame(dates, [10, 11, 12])}
    details = {'600000.SH': {'InstrumentName': '浦发银行'}}   # 无市值字段
    source = source_factory(frames, details, {'沪深A股': ['600000.SH']})
    cfg = StrategyConfig()
    assert filter_universe(source, ['600000.SH'], dates[-1], cfg) == ['600000.SH']
