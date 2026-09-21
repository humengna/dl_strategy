# coding: utf-8
"""向量化引擎必须和逐日引擎给出完全相同的回测结果。"""

import numpy as np
import pytest

from momentum.config import AccountConfig, BacktestConfig, StrategyConfig
from momentum.engine import BacktestEngine
from momentum.sample_data import make_source
from momentum.vector_engine import PanelDataSource, VectorBacktestEngine


def build(cls, source, start, end, cash=200000.0, **strategy_kw):
    strategy_kw.setdefault('rsrs_enabled', False)
    config = BacktestConfig(start_date=start, end_date=end,
                            strategy=StrategyConfig(**strategy_kw),
                            account=AccountConfig(init_cash=cash))
    return cls(source, config)


def run_both(days=200, skip=40, n_stocks=6, **strategy_kw):
    stocks = [f'{600000 + i}.SH' for i in range(n_stocks)]
    results = []
    for cls in (BacktestEngine, VectorBacktestEngine):
        source = make_source(days=days, index_code=None, stocks=stocks)
        all_days = source.get_trading_dates('99999999')
        engine = build(cls, source, all_days[skip], all_days[-1], **strategy_kw)
        results.append(engine.run())
    return results


def deal_key(deals):
    return [(d.date, d.stock, d.direction, round(d.price, 9), d.volume) for d in deals]


def test_equity_curve_identical_to_loop_engine():
    loop, fast = run_both()
    assert loop.dates == fast.dates
    np.testing.assert_allclose(fast.values, loop.values, rtol=0, atol=0)


def test_deals_identical_to_loop_engine():
    loop, fast = run_both()
    assert deal_key(fast.account.deals) == deal_key(loop.account.deals)
    assert len(fast.account.deals) > 0


def test_signals_identical_to_loop_engine():
    loop, fast = run_both()
    assert fast.signals == loop.signals


def test_identical_with_larger_universe():
    loop, fast = run_both(days=150, skip=30, n_stocks=25)
    np.testing.assert_allclose(fast.values, loop.values, rtol=0, atol=0)
    assert deal_key(fast.account.deals) == deal_key(loop.account.deals)


def test_identical_with_longer_lookback():
    loop, fast = run_both(days=200, skip=60, lookback_days=20)
    np.testing.assert_allclose(fast.values, loop.values, rtol=0, atol=0)
    assert deal_key(fast.account.deals) == deal_key(loop.account.deals)


def test_identical_with_tighter_stop_loss():
    loop, fast = run_both(stop_loss_ratio=-0.03)
    np.testing.assert_allclose(fast.values, loop.values, rtol=0, atol=0)
    assert deal_key(fast.account.deals) == deal_key(loop.account.deals)


def test_vector_engine_is_faster():
    import time

    stocks = [f'{600000 + i}.SH' for i in range(30)]
    elapsed = {}
    for cls in (BacktestEngine, VectorBacktestEngine):
        source = make_source(days=150, index_code=None, stocks=stocks)
        all_days = source.get_trading_dates('99999999')
        engine = build(cls, source, all_days[30], all_days[-1])
        started = time.time()
        engine.run()
        elapsed[cls.__name__] = time.time() - started

    assert elapsed['VectorBacktestEngine'] * 3 < elapsed['BacktestEngine']


def test_daily_records_cover_every_trading_day():
    _, fast = run_both()
    assert len(fast.daily) == len(fast.dates)
    assert [d.date for d in fast.daily] == fast.dates
    assert all(d.total_asset > 0 for d in fast.daily)
    # 有持仓的那天要记下持仓明细
    held = [d for d in fast.daily if d.stock]
    assert held and all(d.volume > 0 and d.market_value > 0 for d in held)


def test_rsrs_computed_by_vector_engine():
    source = make_source(days=700)
    all_days = source.get_trading_dates('99999999')
    engine = build(VectorBacktestEngine, source, all_days[-30], all_days[-1],
                   rsrs_enabled=True, rsrs_n=21, rsrs_m=600)
    engine.run()

    assert engine.rsrs is not None
    assert np.isfinite(engine.rsrs[-1])


def test_vector_engine_fails_fast_without_data(source_factory):
    source = source_factory({}, {}, {'沪深A股': []})
    engine = build(VectorBacktestEngine, source, '20240102', '20240131')
    with pytest.raises(RuntimeError):
        engine.run()


# ---------------- PanelDataSource ----------------

def test_panel_source_serves_last_bars():
    source = make_source(days=60, index_code=None,
                         stocks=['600000.SH', '000001.SZ'])
    all_days = source.get_trading_dates('99999999')
    engine = build(VectorBacktestEngine, source, all_days[30], all_days[-1])
    engine.prepare()

    panel_source = engine.source
    assert isinstance(panel_source, PanelDataSource)

    date = all_days[40]
    df = panel_source.get_one('600000.SH', date, 3)
    assert list(df.index) == all_days[38:41]

    expected = source.get_one('600000.SH', date, 3)
    np.testing.assert_allclose(df['close'].to_numpy(), expected['close'].to_numpy())


def test_panel_source_falls_back_for_unknown_stock():
    source = make_source(days=60, stocks=['600000.SH'])      # 指数不在股票池里
    all_days = source.get_trading_dates('99999999')
    engine = build(VectorBacktestEngine, source, all_days[30], all_days[-1])
    engine.prepare()

    df = engine.source.get_one('000300.SH', all_days[40], 5)
    assert df is not None and len(df) == 5
