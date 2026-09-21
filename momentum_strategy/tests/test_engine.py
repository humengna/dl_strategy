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


# ---------------- 停牌不可交易 ----------------

def _suspended_source(source_factory, suspend_last=True, zero_volume=False):
    """最后一天停牌：K 线被前收填满（开=高=低=收=前收），成交量为 0"""
    from momentum.sample_data import make_calendar

    dates = make_calendar(30, start='20240102')
    closes = [10.0] * 29 + [10.0]
    frame = make_frame(dates, closes, opens=[10.0] * 30, pre_closes=[10.0] * 30,
                       suspend=([0] * 29 + [1]) if suspend_last else None)
    if zero_volume:
        frame.loc[frame.index[-1], 'volume'] = 0
    return dates, source_factory({'600000.SH': frame})


def test_cannot_sell_suspended_stock(source_factory):
    """停牌日 K 线是前收填充的，照着它成交等于按停牌前的价格脱手"""
    dates, source = _suspended_source(source_factory)
    engine = make_engine(source, dates[0], dates[-1])
    engine.account.buy(dates[-2], '600000.SH', 10.0, 1000)
    engine.account.settle_open()

    engine.adjust_position(dates[-1], '000001.SZ', SIGNAL_SELL)

    assert engine.account.positions['600000.SH'].volume == 1000   # 仍然持有
    assert engine.account.deals[-1].direction == 1                # 最后一笔还是当初的买入


def test_cannot_switch_out_of_suspended_stock(source_factory):
    dates, source = _suspended_source(source_factory)
    engine = make_engine(source, dates[0], dates[-1])
    engine.account.buy(dates[-2], '600000.SH', 10.0, 1000)
    engine.account.settle_open()

    engine.adjust_position(dates[-1], '000001.SZ', SIGNAL_BUY)

    assert '600000.SH' in engine.account.positions


def test_stop_loss_blocked_by_suspension(source_factory):
    """停牌时止损也执行不了，只能继续持有"""
    from momentum.sample_data import make_calendar

    dates = make_calendar(30, start='20240102')
    closes = [10.0] * 29 + [5.0]        # 相对成本 -50%，正常必然触发止损
    frame = make_frame(dates, closes, opens=[10.0] * 30, pre_closes=[10.0] * 30,
                       suspend=[0] * 29 + [1])
    source = source_factory({'600000.SH': frame})

    engine = make_engine(source, dates[0], dates[-1])
    engine.account.buy(dates[0], '600000.SH', 10.0, 1000)
    engine.account.settle_open()

    engine.check_stop_loss(dates[-1])
    assert engine.account.positions['600000.SH'].volume == 1000

    # 换成没停牌的同一天，就应该止损出去
    frame2 = make_frame(dates, closes, opens=[10.0] * 30, pre_closes=[10.0] * 30)
    engine2 = make_engine(source_factory({'600000.SH': frame2}), dates[0], dates[-1])
    engine2.account.buy(dates[0], '600000.SH', 10.0, 1000)
    engine2.account.settle_open()
    engine2.check_stop_loss(dates[-1])
    assert engine2.account.positions == {}


def test_zero_volume_counts_as_suspended(source_factory):
    """有些数据没有 suspendFlag，成交量为 0 同样说明当天不可交易"""
    dates, source = _suspended_source(source_factory, suspend_last=False, zero_volume=True)
    engine = make_engine(source, dates[0], dates[-1])
    engine.account.buy(dates[-2], '600000.SH', 10.0, 1000)
    engine.account.settle_open()

    engine.adjust_position(dates[-1], '000001.SZ', SIGNAL_SELL)
    assert engine.account.positions['600000.SH'].volume == 1000


def test_suspended_stock_sells_once_resumed(source_factory):
    """复牌当天就应该正常卖出"""
    from momentum.sample_data import make_calendar

    dates = make_calendar(31, start='20240102')
    frame = make_frame(dates, [10.0] * 30 + [9.0], opens=[10.0] * 30 + [9.0],
                       pre_closes=[10.0] * 31, suspend=[0] * 29 + [1, 0])
    source = source_factory({'600000.SH': frame})

    engine = make_engine(source, dates[0], dates[-1])
    engine.account.buy(dates[-3], '600000.SH', 10.0, 1000)
    engine.account.settle_open()

    engine.adjust_position(dates[-2], '000001.SZ', SIGNAL_SELL)     # 停牌日
    assert '600000.SH' in engine.account.positions

    engine.account.settle_open()
    engine.adjust_position(dates[-1], '000001.SZ', SIGNAL_SELL)     # 复牌日
    assert engine.account.positions == {}
    assert engine.account.deals[-1].price == pytest.approx(9.0)


def test_full_backtest_holds_through_suspension_then_sells_on_resume(source_factory):
    """
    端到端：持仓中途停牌若干天
      - 停牌期间目标股已经切走，但旧仓卖不掉，持仓和现金都不动
      - 复牌当天必须卖出
    """
    import pandas as pd

    from momentum.sample_data import make_calendar
    from momentum.vector_engine import VectorBacktestEngine

    dates = make_calendar(40, start='20240102')
    halt = dates[29:32]                       # 连停 3 天

    def build_frame(closes, suspend):
        opens = list(closes)
        pre = [closes[0]] + list(closes[:-1])
        return pd.DataFrame({
            'open': opens, 'high': [c * 1.01 for c in closes],
            'low': [c * 0.99 for c in closes], 'close': list(closes), 'preClose': pre,
            'volume': [0.0 if f else 1e6 for f in suspend],
            'suspendFlag': [float(f) for f in suspend],
        }, index=dates)

    # 加速上涨：动量分数逐日走高，信号稳定为 BUY
    # （完美指数增长会让各窗口分数数学上完全相等，信号被浮点噪声左右）
    import numpy as np
    leader = list(10 * np.exp(np.cumsum(np.linspace(0.002, 0.02, 40))))
    for i in (29, 30, 31):
        leader[i] = leader[28]                # 停牌日 K 线按前收填满
    flags = [0] * 29 + [1, 1, 1] + [0] * 8

    frames = {'600000.SH': build_frame(leader, flags),
              '000001.SZ': build_frame([20.0] * 40, [0] * 40)}
    source = source_factory(frames)

    engine = VectorBacktestEngine(
        source,
        BacktestConfig(start_date=dates[20], end_date=dates[-1],
                       strategy=StrategyConfig(lookback_days=5, rsrs_enabled=False),
                       account=AccountConfig(init_cash=200000.0)))
    result = engine.run()
    rows = {r.date: r for r in result.daily}

    # 停牌前已经持有领涨股
    assert rows[dates[28]].stock == '600000.SH'

    # 停牌期间：目标已切走，持仓和现金纹丝不动，且没有任何成交
    frozen = rows[dates[28]]
    for day in halt:
        assert rows[day].stock == '600000.SH', f'{day} 不该被卖掉'
        assert rows[day].volume == frozen.volume
        assert rows[day].cash == pytest.approx(frozen.cash)
    assert not [d for d in engine.account.deals if d.date in halt]

    # 复牌当天卖出
    resume = dates[32]
    sells = [d for d in engine.account.deals if d.date == resume and d.direction == -1]
    assert sells and sells[0].stock == '600000.SH'
    assert sells[0].price != pytest.approx(frozen.cost)    # 按复牌当天的真实价，不是停牌前的填充价


def test_suspended_holding_blocks_new_buy(source_factory):
    """停牌期间资金锁在旧仓里，不该冒出第二个持仓"""
    from momentum.sample_data import make_calendar

    dates = make_calendar(30, start='20240102')
    frame = make_frame(dates, [10.0] * 30, opens=[10.0] * 30, pre_closes=[10.0] * 30,
                       suspend=[0] * 29 + [1])
    other = make_frame(dates, [20.0] * 30, opens=[20.0] * 30, pre_closes=[20.0] * 30)
    source = source_factory({'600000.SH': frame, '000001.SZ': other})

    engine = make_engine(source, dates[0], dates[-1], cash=200000.0)
    engine.account.buy(dates[-2], '600000.SH', 10.0, 19900)      # 几乎满仓
    engine.account.settle_open()

    engine.adjust_position(dates[-1], '000001.SZ', SIGNAL_BUY)

    assert set(engine.account.positions) == {'600000.SH'}
