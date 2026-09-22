# coding: utf-8
"""优化项：多持仓等权、仓位系数、择时盯持仓、涨停按开盘价拦截。"""

import json
import os

import numpy as np
import pytest

from momentum.broker import SimAccount
from momentum.cli import apply_optimized_preset, build_parser, main, run_label
from momentum.config import OPTIMIZED_PRESET, AccountConfig, BacktestConfig, StrategyConfig
from momentum.engine import BacktestEngine
from momentum.sample_data import make_calendar, make_source, write_sample_dir
from momentum.selector import blocked_by_limit_up, pick_targets
from momentum.timing import SIGNAL_BUY, SIGNAL_SELL
from momentum.vector_engine import VectorBacktestEngine
from tests.conftest import make_frame


def build(cls, source, start, end, cash=1_000_000.0, **kw):
    kw.setdefault('rsrs_enabled', False)
    return cls(source, BacktestConfig(start_date=start, end_date=end,
                                      strategy=StrategyConfig(**kw),
                                      account=AccountConfig(init_cash=cash)))


# ---------------- 配置校验 ----------------

def test_config_rejects_bad_values():
    for kw in ({'max_positions': 0}, {'position_ratio': 0},
               {'position_ratio': 1.5}, {'limit_up_block_field': 'high'}):
        with pytest.raises(ValueError):
            StrategyConfig(**kw)


def test_defaults_stay_原策略():
    cfg = StrategyConfig()
    assert cfg.max_positions == 1
    assert cfg.position_ratio == 1.0
    assert cfg.timing_on_holdings is False
    assert cfg.limit_up_block_field == 'low'


# ---------------- 涨停拦截 ----------------

def test_limit_up_block_field():
    legacy = StrategyConfig(limit_up_block_field='low')
    fixed = StrategyConfig(limit_up_block_field='open')

    # 一字板：两种口径都拦
    assert blocked_by_limit_up(legacy, 11.0, 11.0, 11.0) is True
    assert blocked_by_limit_up(fixed, 11.0, 11.0, 11.0) is True
    # 开盘涨停、盘中打开：原口径放行（未来函数），修正后拦截
    assert blocked_by_limit_up(legacy, 11.0, 10.2, 11.0) is False
    assert blocked_by_limit_up(fixed, 11.0, 10.2, 11.0) is True
    # 高开未涨停：都放行
    assert blocked_by_limit_up(legacy, 10.8, 10.5, 11.0) is False
    assert blocked_by_limit_up(fixed, 10.8, 10.5, 11.0) is False


def test_limit_up_open_check_blocks_buy(source_factory):
    dates = make_calendar(30, start='20240102')
    opens = [10.0] * 29 + [11.0]        # 末日开盘涨停但盘中打开
    lows = [9.9] * 29 + [10.2]
    frame = make_frame(dates, [10.0] * 30, opens=opens, lows=lows,
                       pre_closes=[10.0] * 30)
    source = source_factory({'600000.SH': frame})

    for field, expect_bought in (('low', True), ('open', False)):
        engine = build(BacktestEngine, source, dates[0], dates[-1],
                       limit_up_block_field=field)
        engine.adjust_position(dates[-1], '600000.SH', SIGNAL_BUY)
        assert bool(engine.account.positions) is expect_bought


# ---------------- 仓位系数 ----------------

def test_position_ratio_caps_exposure(source_factory):
    dates = make_calendar(30, start='20240102')
    frame = make_frame(dates, [10.0] * 30, opens=[10.0] * 30, pre_closes=[10.0] * 30)
    source = source_factory({'600000.SH': frame})

    for ratio in (0.25, 0.5, 1.0):
        engine = build(BacktestEngine, source, dates[0], dates[-1],
                       cash=1_000_000.0, position_ratio=ratio)
        engine.adjust_position(dates[-1], '600000.SH', SIGNAL_BUY)
        pos = engine.account.positions['600000.SH']
        invested = pos.volume * pos.open_price
        assert invested <= 1_000_000.0 * ratio + 1e-6
        assert invested > 1_000_000.0 * ratio * 0.99          # 只差整手取整


def test_slot_budget_uses_open_equity(source_factory):
    dates = make_calendar(30, start='20240102')
    frame = make_frame(dates, [10.0] * 30, opens=[12.0] * 30, pre_closes=[10.0] * 30)
    source = source_factory({'600000.SH': frame})

    engine = build(BacktestEngine, source, dates[0], dates[-1], cash=100_000.0,
                   max_positions=2, position_ratio=0.5)
    engine.account.buy(dates[-2], '600000.SH', 10.0, 1000)    # 市值按开盘 12 算
    engine.account.settle_open()

    equity = engine.account.cash + 1000 * 12.0
    assert engine.slot_budget(dates[-1]) == pytest.approx(equity * 0.5 / 2)


# ---------------- 多持仓等权 ----------------

def test_multi_position_equal_weight(source_factory):
    dates = make_calendar(30, start='20240102')
    frames = {}
    for i, code in enumerate(['600000.SH', '000001.SZ', '000002.SZ']):
        px = [10.0 * (i + 1)] * 30
        frames[code] = make_frame(dates, px, opens=px, pre_closes=px)
    source = source_factory(frames)

    engine = build(BacktestEngine, source, dates[0], dates[-1], cash=1_000_000.0,
                   max_positions=3, position_ratio=0.9)
    engine.adjust_portfolio(dates[-1], list(frames))

    assert len(engine.account.positions) == 3
    slot = 1_000_000.0 * 0.9 / 3
    for pos in engine.account.get_positions():
        assert pos.volume * pos.open_price <= slot + 1e-6
    assert engine.account.cash > 1_000_000.0 * 0.05          # 剩下的钱留现金


def test_portfolio_sells_only_what_left_targets(source_factory):
    dates = make_calendar(30, start='20240102')
    frames = {c: make_frame(dates, [10.0] * 30, opens=[10.0] * 30,
                            pre_closes=[10.0] * 30)
              for c in ('600000.SH', '000001.SZ', '000002.SZ')}
    source = source_factory(frames)

    engine = build(BacktestEngine, source, dates[0], dates[-1], cash=1_000_000.0,
                   max_positions=2, position_ratio=1.0)
    engine.account.buy(dates[-2], '600000.SH', 10.0, 1000)
    engine.account.buy(dates[-2], '000001.SZ', 10.0, 1000)
    engine.account.settle_open()

    engine.adjust_portfolio(dates[-1], ['600000.SH', '000002.SZ'])

    assert '600000.SH' in engine.account.positions       # 仍在目标里 -> 保留
    assert '000001.SZ' not in engine.account.positions   # 掉出目标 -> 卖出
    assert '000002.SZ' in engine.account.positions       # 新进目标 -> 买入


def test_pick_targets_returns_top_n(source_factory):
    dates = make_calendar(12, start='20240102')
    frames = {
        '600000.SH': make_frame(dates, [10 * (1.05 ** i) for i in range(12)]),
        '000001.SZ': make_frame(dates, [10 * (1.03 ** i) for i in range(12)]),
        '000002.SZ': make_frame(dates, [10 * (1.01 ** i) for i in range(12)]),
        '600004.SH': make_frame(dates, [10 * (0.98 ** i) for i in range(12)]),
    }
    source = source_factory(frames)
    cfg = StrategyConfig(lookback_days=5, max_positions=3)
    assert pick_targets(source, list(frames), dates[-1], cfg) == \
        ['600000.SH', '000001.SZ', '000002.SZ']


# ---------------- 择时盯持仓 ----------------

def test_timing_on_holdings_sells_declining_position(source_factory):
    """持仓分数连降 -> 卖出；原口径下判断候选股，不会卖"""
    dates = make_calendar(40, start='20240102')
    # 持仓股：先加速上涨再转头下跌，最近两天分数连降
    falling = list(10 * np.exp(np.cumsum(
        np.concatenate([np.linspace(0.002, 0.02, 30), np.linspace(-0.01, -0.08, 10)]))))
    rising = list(20 * np.exp(np.cumsum(np.linspace(0.002, 0.02, 40))))
    frames = {'600000.SH': make_frame(dates, falling, opens=falling),
              '000001.SZ': make_frame(dates, rising, opens=rising)}
    source = source_factory(frames)

    for on_holdings, expect_sold in ((False, False), (True, True)):
        engine = build(BacktestEngine, source, dates[0], dates[-1],
                       timing_on_holdings=on_holdings)
        engine.account.buy(dates[-2], '600000.SH', falling[-2], 100)
        engine.account.settle_open()
        signal, to_sell = engine.resolve_signals(dates[-1], ['000001.SZ'])
        assert ('600000.SH' in to_sell) is expect_sold
        if expect_sold:
            assert signal == SIGNAL_SELL


# ---------------- 两个引擎一致 ----------------

def run_both(**kw):
    out = []
    for cls in (BacktestEngine, VectorBacktestEngine):
        source = make_source(days=180, index_code=None,
                             stocks=[f'{600000 + i}.SH' for i in range(12)])
        days = source.get_trading_dates('99999999')
        out.append(build(cls, source, days[40], days[-1], **kw).run())
    return out


def deal_key(deals):
    return [(d.date, d.stock, d.direction, round(d.price, 9), d.volume) for d in deals]


def test_engines_identical_multi_position():
    loop, fast = run_both(max_positions=5, position_ratio=0.35, lookback_days=10)
    assert loop.equity_curve == fast.equity_curve
    assert deal_key(loop.account.deals) == deal_key(fast.account.deals)
    assert len(fast.account.deals) > 0


def test_engines_identical_under_optimized_preset():
    loop, fast = run_both(max_positions=OPTIMIZED_PRESET['max_positions'],
                          position_ratio=OPTIMIZED_PRESET['position_ratio'],
                          timing_on_holdings=True, limit_up_block_field='open',
                          lookback_days=OPTIMIZED_PRESET['lookback_days'])
    assert loop.equity_curve == fast.equity_curve
    assert deal_key(loop.account.deals) == deal_key(fast.account.deals)


def test_multi_position_actually_holds_several():
    _, fast = run_both(max_positions=5, position_ratio=0.9, lookback_days=10)
    assert max(d.position_count for d in fast.daily) >= 3
    assert any(';' in d.holdings for d in fast.daily)


def test_multi_position_lowers_volatility():
    """分散之后日波动必须下降 —— 这是整个优化方案的核心机制"""
    single, _ = run_both(max_positions=1, position_ratio=1.0, lookback_days=10)
    multi, _ = run_both(max_positions=5, position_ratio=1.0, lookback_days=10)

    def vol(res):
        v = np.array(res.values, dtype=float)
        return np.diff(v).std() / v[:-1].mean()

    assert vol(multi) < vol(single)


# ---------------- 预设与命令行 ----------------

def test_optimized_preset_sets_everything():
    args = build_parser().parse_args(['--optimized'])
    apply_optimized_preset(args)
    assert args.max_positions == OPTIMIZED_PRESET['max_positions']
    assert args.position_ratio == OPTIMIZED_PRESET['position_ratio']
    assert args.timing_on_holdings is True
    assert args.limit_up_check == 'open'
    assert args.lookback == [OPTIMIZED_PRESET['lookback_days']]


def test_optimized_preset_respects_explicit_lookback():
    args = build_parser().parse_args(['--optimized', '--lookback', '15', '29'])
    apply_optimized_preset(args)
    assert args.lookback == [15, 29]


def test_optimized_and_replicate_are_exclusive(tmp_path):
    with pytest.raises(SystemExit):
        main(['--optimized', '--replicate-qmt', '--start', '20240101',
              '--end', '20241231', '--no-save'])


def test_optimized_label():
    args = build_parser().parse_args(['--optimized', '--start', '20240101',
                                      '--end', '20241231'])
    apply_optimized_preset(args)
    cfg = StrategyConfig(lookback_days=29, max_positions=5, position_ratio=0.35,
                         timing_on_holdings=True, limit_up_block_field='open')
    label = run_label(args, cfg)
    for tag in ('lb29', 'opt', 'n5', 'pos35', 'hold', 'upopen'):
        assert tag in label


@pytest.fixture(scope='module')
def sample_dir(tmp_path_factory):
    path = tmp_path_factory.mktemp('opt_sample')
    write_sample_dir(str(path), days=200, start='20240102', index_code='')
    return str(path)


def test_optimized_end_to_end(sample_dir, tmp_path):
    out = tmp_path / 'opt'
    assert main(['--source', 'csv', '--data-dir', sample_dir,
                 '--start', '20240601', '--end', '20240930',
                 '--optimized', '--no-rsrs', '-q', '--out-dir', str(out)]) == 0

    with open(out / 'summary.json', encoding='utf-8') as f:
        payload = json.load(f)
    assert payload['optimized'] is True
    assert payload['strategy']['max_positions'] == 5
    assert payload['strategy']['position_ratio'] == 0.35
    assert payload['strategy']['timing_on_holdings'] is True
    assert payload['strategy']['limit_up_block_field'] == 'open'
    assert payload['strategy']['lookback_days'] == 29

    import pandas as pd
    equity = pd.read_csv(out / 'equity.csv')
    for col in ('position_count', 'holdings'):
        assert col in equity.columns
    assert equity['position_count'].max() >= 2
