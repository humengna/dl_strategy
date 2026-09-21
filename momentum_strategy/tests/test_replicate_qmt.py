# coding: utf-8
"""复刻原 QMT 脚本的行为开关（含它的已知缺陷）。"""

import json
import os

import pytest

from momentum.broker import SimAccount
from momentum.cli import QMT_PRESET, apply_qmt_preset, build_parser, main, run_label
from momentum.config import AccountConfig, BacktestConfig, StrategyConfig
from momentum.engine import BacktestEngine
from momentum.sample_data import make_calendar, make_source, write_sample_dir
from momentum.selector import filter_target
from momentum.timing import SIGNAL_BUY, SIGNAL_SELL
from momentum.vector_engine import VectorBacktestEngine
from tests.conftest import make_frame


def build(cls, source, start, end, cash=200000.0, account_kw=None, **strategy_kw):
    strategy_kw.setdefault('rsrs_enabled', False)
    config = BacktestConfig(start_date=start, end_date=end,
                            strategy=StrategyConfig(**strategy_kw),
                            account=AccountConfig(init_cash=cash, **(account_kw or {})))
    return cls(source, config)


# ---------------- 固定 10% 跌停 ----------------

def test_fixed_limit_down_ratio_hits_gem_stock(source_factory):
    """原脚本恒用 10%，会把跌 10% 的创业板票误判成跌停"""
    dates = make_calendar(2, start='20240102')
    frame = make_frame(dates, [10.0, 9.0], opens=[10.0, 9.9], pre_closes=[10.0, 10.0])
    source = source_factory({'300750.SZ': frame})

    board = StrategyConfig(filter_limit_down=True)                      # 创业板按 20%
    legacy = StrategyConfig(filter_limit_down=True, limit_down_ratio=0.10)

    assert filter_target(source, '300750.SZ', dates[-1], board) == '300750.SZ'
    assert filter_target(source, '300750.SZ', dates[-1], legacy) is None


def test_fixed_ratio_applies_in_vector_engine(source_factory):
    dates = make_calendar(30, start='20240102')
    frame = make_frame(dates, [10.0] * 29 + [9.0], opens=[10.0] * 30,
                       pre_closes=[10.0] * 30)
    source = source_factory({'300750.SZ': frame})

    def tradable(**kw):
        engine = build(VectorBacktestEngine, source, dates[20], dates[-1], **kw)
        engine.prepare()
        i = engine.panel.date_pos[dates[-1]]
        return bool(engine.tradable[i, engine.panel.stock_pos['300750.SZ']])

    assert tradable(filter_limit_down=True) is True                     # 按板块 20%
    assert tradable(filter_limit_down=True, limit_down_ratio=0.10) is False


# ---------------- 停牌可卖 ----------------

def test_allow_sell_suspended(source_factory):
    dates = make_calendar(30, start='20240102')
    frame = make_frame(dates, [10.0] * 30, opens=[10.0] * 30, pre_closes=[10.0] * 30,
                       suspend=[0] * 29 + [1])
    source = source_factory({'600000.SH': frame})

    for allow, expect_sold in ((False, False), (True, True)):
        engine = build(BacktestEngine, source, dates[0], dates[-1],
                       allow_sell_suspended=allow)
        engine.account.buy(dates[-2], '600000.SH', 10.0, 1000)
        engine.account.settle_open()
        engine.adjust_position(dates[-1], '000001.SZ', SIGNAL_SELL)
        assert (engine.account.positions == {}) is expect_sold


def test_allow_sell_suspended_also_frees_stop_loss(source_factory):
    dates = make_calendar(30, start='20240102')
    frame = make_frame(dates, [10.0] * 29 + [5.0], opens=[10.0] * 30,
                       pre_closes=[10.0] * 30, suspend=[0] * 29 + [1])
    source = source_factory({'600000.SH': frame})

    engine = build(BacktestEngine, source, dates[0], dates[-1], allow_sell_suspended=True)
    engine.account.buy(dates[0], '600000.SH', 10.0, 1000)
    engine.account.settle_open()
    engine.check_stop_loss(dates[-1])
    assert engine.account.positions == {}


# ---------------- 买入不预留费用 ----------------

def test_no_fee_reserve_matches_original_formula():
    cash, price = 100000.0, 33.0
    reserved = SimAccount(AccountConfig(init_cash=cash))
    legacy = SimAccount(AccountConfig(init_cash=cash, reserve_fee_on_buy=False))

    assert legacy.affordable_volume(price) == int(cash / price / 100) * 100
    assert reserved.affordable_volume(price) <= legacy.affordable_volume(price)


def test_no_fee_reserve_can_overdraw_like_original():
    """原公式不留费用，满仓时会因手续费买不进 —— 这正是要复刻的行为"""
    acc = SimAccount(AccountConfig(init_cash=1000.0, reserve_fee_on_buy=False))
    vol = acc.affordable_volume(10.0)
    assert vol == 100
    assert acc.buy('20240102', '600000.SH', 10.0, vol) is False     # 1000 + 5 > 1000


# ---------------- 预热期不交易 ----------------

def test_skip_warmup_bars_delays_first_trade():
    source = make_source(days=120, index_code=None,
                         stocks=['600000.SH', '000001.SZ', '000002.SZ'])
    days = source.get_trading_dates('99999999')

    plain = build(VectorBacktestEngine, source, days[40], days[-1]).run()
    skipped = build(VectorBacktestEngine, source, days[40], days[-1],
                    skip_warmup_bars=True).run()

    # 原脚本 bar_count < warmup 时 return -> 前 warmup-1 根不交易
    skip = StrategyConfig().warmup_days - 1
    first_tradable = days[40 + skip]

    # 不跳过时第一天就交易；跳过时预热期内一笔都没有
    assert plain.account.deals[0].date == days[40]
    assert any(d.date < first_tradable for d in plain.account.deals)
    assert skipped.account.deals, '预热期之后仍应有交易'
    assert all(d.date >= first_tradable for d in skipped.account.deals)

    # 预热期内账户不动，也不产生信号
    warm_rows = skipped.daily[:skip]
    assert [d.date for d in warm_rows] == days[40:40 + skip]
    assert all(d.total_asset == skipped.account.init_cash for d in warm_rows)
    assert all(d.signal == '' and d.stock == '' for d in warm_rows)


def test_engines_identical_under_qmt_preset():
    """复刻口径下两个引擎仍必须逐笔一致"""
    kw = dict(filter_market_cap=False, filter_limit_down=True, limit_down_ratio=0.10,
              allow_sell_suspended=True, skip_warmup_bars=True, rsrs_enabled=False)
    out = []
    for cls in (BacktestEngine, VectorBacktestEngine):
        source = make_source(days=150, index_code=None,
                             stocks=[f'{600000 + i}.SH' for i in range(8)])
        days = source.get_trading_dates('99999999')
        out.append(build(cls, source, days[30], days[-1],
                         account_kw={'reserve_fee_on_buy': False}, **kw).run())
    loop, fast = out
    assert loop.equity_curve == fast.equity_curve
    assert [(d.date, d.stock, d.direction, round(d.price, 9), d.volume)
            for d in fast.account.deals] == \
           [(d.date, d.stock, d.direction, round(d.price, 9), d.volume)
            for d in loop.account.deals]


# ---------------- 预设 ----------------

def test_preset_flips_every_switch():
    args = build_parser().parse_args(['--replicate-qmt'])
    apply_qmt_preset(args)
    assert args.no_cap_filter is True
    assert args.dividend_type == 'none'
    assert args.filter_limit_down is True
    assert args.limit_down_ratio == 0.10
    assert args.no_fee_reserve is True
    assert args.skip_warmup is True
    assert set(QMT_PRESET) == {'no_cap_filter', 'dividend_type', 'filter_limit_down',
                               'limit_down_ratio', 'no_fee_reserve', 'skip_warmup'}


def test_preset_does_not_replicate_selling_suspended():
    """停牌股脱手实盘做不到，复刻预设里不带这一条"""
    args = build_parser().parse_args(['--replicate-qmt'])
    apply_qmt_preset(args)
    assert args.allow_sell_suspended is False
    assert 'allow_sell_suspended' not in QMT_PRESET

    # 显式加上仍然可以对齐原脚本
    args = build_parser().parse_args(['--replicate-qmt', '--allow-sell-suspended'])
    apply_qmt_preset(args)
    assert args.allow_sell_suspended is True


def test_preset_tags_output_dir():
    args = build_parser().parse_args(['--replicate-qmt', '--start', '20240101',
                                      '--end', '20241231'])
    apply_qmt_preset(args)
    cfg = StrategyConfig(filter_market_cap=False, filter_limit_down=True,
                         limit_down_ratio=0.10)
    label = run_label(args, cfg)
    assert 'qmt' in label and 'nocap' in label and 'divnone' in label


@pytest.fixture(scope='module')
def sample_dir(tmp_path_factory):
    path = tmp_path_factory.mktemp('qmt_sample')
    write_sample_dir(str(path), days=160, start='20240102', index_code='')
    return str(path)


def test_preset_end_to_end(sample_dir, tmp_path):
    out = tmp_path / 'qmt'
    assert main(['--source', 'csv', '--data-dir', sample_dir,
                 '--start', '20240401', '--end', '20240731',
                 '--replicate-qmt', '--no-rsrs', '-q', '--out-dir', str(out)]) == 0

    with open(out / 'summary.json', encoding='utf-8') as f:
        payload = json.load(f)
    assert payload['replicate_qmt'] is True
    assert payload['dividend_type'] == 'none'
    assert payload['strategy']['filter_market_cap'] is False
    assert payload['strategy']['filter_limit_down'] is True
    assert payload['strategy']['limit_down_ratio'] == 0.10
    assert payload['strategy']['allow_sell_suspended'] is False
    assert payload['strategy']['skip_warmup_bars'] is True
    assert payload['account']['reserve_fee_on_buy'] is False
    assert os.path.isfile(out / 'trades.csv')
