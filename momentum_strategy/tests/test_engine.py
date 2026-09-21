# coding: utf-8
import pytest

from momentum.broker import SimAccount
from momentum.config import AccountConfig, BacktestConfig, StrategyConfig
from momentum.engine import BacktestEngine
from momentum.sample_data import make_calendar, make_source
from momentum.timing import SIGNAL_BUY, SIGNAL_KEEP, SIGNAL_SELL
from tests.conftest import make_frame


def make_engine(source, start, end, cash=100000.0, **strategy_kw):
    strategy_kw.setdefault('lookback_days', 5)
    strategy_kw.setdefault('rsrs_enabled', False)
    config = BacktestConfig(
        start_date=start, end_date=end,
        strategy=StrategyConfig(**strategy_kw),
        account=AccountConfig(init_cash=cash),
    )
    return BacktestEngine(source, config, SimAccount(config.account))


@pytest.fixture
def rising_source(source_factory):
    """加速上涨：动量分数逐日走高，择时信号稳定为 BUY"""
    import numpy as np

    dates = make_calendar(20, start='20240102')
    closes = list(10 * np.exp(np.cumsum(np.linspace(0.002, 0.02, 20))))
    frames = {'600000.SH': make_frame(dates, closes, opens=closes)}
    return dates, source_factory(frames)


# ---------------- 调仓 ----------------

def test_adjust_position_buys_with_buy_signal(rising_source):
    dates, source = rising_source
    engine = make_engine(source, dates[0], dates[-1])

    engine.adjust_position(dates[-1], '600000.SH', SIGNAL_BUY)

    pos = engine.account.positions['600000.SH']
    assert pos.volume > 0
    assert pos.volume % 100 == 0
    assert engine.account.cash >= 0


def test_adjust_position_keep_does_not_trade(rising_source):
    dates, source = rising_source
    engine = make_engine(source, dates[0], dates[-1])
    engine.account.buy(dates[-2], '600000.SH', 10.0, 1000)
    engine.account.settle_open()
    before = len(engine.account.deals)

    engine.adjust_position(dates[-1], '600000.SH', SIGNAL_KEEP)

    assert len(engine.account.deals) == before


def test_adjust_position_sell_clears_holdings(rising_source):
    dates, source = rising_source
    engine = make_engine(source, dates[0], dates[-1])
    engine.account.buy(dates[-2], '600000.SH', 10.0, 1000)
    engine.account.settle_open()

    engine.adjust_position(dates[-1], '600000.SH', SIGNAL_SELL)

    assert engine.account.positions == {}
    assert engine.account.deals[-1].direction == -1


def test_adjust_position_switches_target(source_factory):
    dates = make_calendar(20, start='20240102')
    old = [10.0] * 20
    new = [20.0] * 20
    source = source_factory({
        '600000.SH': make_frame(dates, old, opens=old),
        '000001.SZ': make_frame(dates, new, opens=new),
    })
    engine = make_engine(source, dates[0], dates[-1])
    engine.account.buy(dates[-2], '600000.SH', 10.0, 1000)
    engine.account.settle_open()

    engine.adjust_position(dates[-1], '000001.SZ', SIGNAL_BUY)

    assert '600000.SH' not in engine.account.positions
    assert engine.account.positions['000001.SZ'].volume > 0


def test_adjust_position_blocked_by_opening_limit_up(source_factory):
    dates = make_calendar(20, start='20240102')
    closes = [10.0] * 20
    opens = [10.0] * 19 + [11.0]        # 一字涨停：开盘=最低=涨停价
    lows = [9.9] * 19 + [11.0]
    pre = [10.0] * 20
    source = source_factory({
        '600000.SH': make_frame(dates, closes, opens=opens, lows=lows, pre_closes=pre),
    })
    engine = make_engine(source, dates[0], dates[-1])

    engine.adjust_position(dates[-1], '600000.SH', SIGNAL_BUY)

    assert engine.account.positions == {}


def test_adjust_position_skips_when_cash_insufficient(rising_source):
    dates, source = rising_source
    engine = make_engine(source, dates[0], dates[-1], cash=100.0)

    engine.adjust_position(dates[-1], '600000.SH', SIGNAL_BUY)

    assert engine.account.positions == {}


# ---------------- 止损 ----------------

def test_stop_loss_triggers_below_threshold(source_factory):
    dates = make_calendar(3, start='20240102')
    closes = [10.0, 10.0, 8.0]          # 相对成本 10 跌 20%
    source = source_factory({'600000.SH': make_frame(dates, closes, opens=closes)})
    engine = make_engine(source, dates[0], dates[-1])
    engine.account.buy(dates[0], '600000.SH', 10.0, 1000)
    engine.account.settle_open()

    engine.check_stop_loss(dates[-1])

    assert engine.account.positions == {}
    assert '硬止损' in engine.account.deals[-1].msg


def test_stop_loss_not_triggered_above_threshold(source_factory):
    dates = make_calendar(3, start='20240102')
    closes = [10.0, 10.0, 9.0]          # 只跌 10%，未到 -15%
    source = source_factory({'600000.SH': make_frame(dates, closes, opens=closes)})
    engine = make_engine(source, dates[0], dates[-1])
    engine.account.buy(dates[0], '600000.SH', 10.0, 1000)
    engine.account.settle_open()

    engine.check_stop_loss(dates[-1])

    assert engine.account.positions['600000.SH'].volume == 1000


def test_stop_loss_skips_frozen_position_same_day(source_factory):
    """当日买入 T+1 冻结，止损无法在当天卖出（与原策略一致）"""
    dates = make_calendar(3, start='20240102')
    closes = [10.0, 10.0, 5.0]
    source = source_factory({'600000.SH': make_frame(dates, closes, opens=closes)})
    engine = make_engine(source, dates[0], dates[-1])
    engine.account.buy(dates[-1], '600000.SH', 10.0, 1000)   # 未 settle_open

    engine.check_stop_loss(dates[-1])

    assert engine.account.positions['600000.SH'].volume == 1000


# ---------------- 单日与整段回测 ----------------

def test_run_day_buys_on_rising_stock(rising_source):
    dates, source = rising_source
    engine = make_engine(source, dates[0], dates[-1])
    engine.prepare()

    total = engine.run_day(dates[-1])

    assert engine.today_target == '600000.SH'
    assert engine.account.positions['600000.SH'].volume > 0
    assert total == pytest.approx(engine.account.total_asset(
        {'600000.SH': float(source.get_one('600000.SH', dates[-1], 1)['close'].iloc[-1])}))


def test_run_day_skips_when_pool_empty(source_factory):
    dates = make_calendar(20, start='20240102')
    frames = {'600000.SH': make_frame(dates, [10.0] * 20)}
    details = {'600000.SH': {'InstrumentName': 'ST测试', 'TotalValue': 100e8}}
    source = source_factory(frames, details, {'沪深A股': ['600000.SH']})
    engine = make_engine(source, dates[0], dates[-1])
    engine.prepare()

    total = engine.run_day(dates[-1])

    assert engine.account.deals == []
    assert total == pytest.approx(engine.account.init_cash)


def test_full_backtest_on_sample_data():
    source = make_source(days=80, start='20240102', index_code=None)
    all_days = source.get_trading_dates('99999999')
    start, end = all_days[30], all_days[-1]

    engine = make_engine(source, start, end, cash=200000.0)
    result = engine.run()

    expected_days = [d for d in all_days if start <= d <= end]
    assert result.dates == expected_days
    assert len(result.values) == len(expected_days)
    assert all(v > 0 for v in result.values)
    assert engine.account.cash >= 0
    assert len(engine.account.deals) > 0
    # 每天最多持有 1 只标的
    assert len(engine.account.get_positions()) <= 1


def test_backtest_rejects_empty_date_range():
    source = make_source(days=40, start='20240102', index_code=None)
    engine = make_engine(source, '20990101', '20991231')
    with pytest.raises(RuntimeError):
        engine.run()


def test_rsrs_is_computed_when_enough_history():
    from momentum.timing import rsrs_value
    source = make_source(days=700, start='20220104')
    cfg = StrategyConfig(rsrs_n=21, rsrs_m=600)
    days = source.get_trading_dates('99999999')
    value = rsrs_value(source, days[-1], cfg)
    assert value is not None


def test_rsrs_returns_none_without_enough_history():
    from momentum.timing import rsrs_value
    source = make_source(days=80, start='20240102')
    assert rsrs_value(source, '20240501', StrategyConfig()) is None
