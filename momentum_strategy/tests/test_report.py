# coding: utf-8
import json
import os

import pandas as pd
import pytest

from momentum.broker import SimAccount
from momentum.config import AccountConfig
from momentum.engine import BacktestResult
from momentum.report import deals_dataframe, equity_dataframe, evaluate, format_report


def make_result(values, init_cash=100000.0):
    account = SimAccount(AccountConfig(init_cash=init_cash))
    curve = [(f'2024010{i + 1}', v) for i, v in enumerate(values)]
    return BacktestResult(account=account, equity_curve=curve)


def test_evaluate_none_for_empty_curve():
    assert evaluate(make_result([])) is None
    assert '无有效交易日' in format_report(None)


def test_total_return_and_final_asset():
    perf = evaluate(make_result([100000, 105000, 110000]))
    assert perf.final_asset == 110000
    assert perf.total_return == pytest.approx(0.10)
    assert perf.trading_days == 3


def test_max_drawdown():
    perf = evaluate(make_result([100000, 120000, 90000, 100000]))
    assert perf.max_drawdown == pytest.approx(-0.25)     # 12 万 -> 9 万
    assert perf.max_drawdown_date == '20240103'


def test_no_drawdown_for_monotonic_curve():
    perf = evaluate(make_result([100000, 101000, 102000]))
    assert perf.max_drawdown == pytest.approx(0.0)


def test_win_rate_counts_profitable_sells():
    result = make_result([100000, 101000])
    acc = result.account
    acc.buy('20240101', '600000.SH', 10.0, 1000)
    acc.settle_open()
    acc.sell('20240102', '600000.SH', 12.0, 1000)        # 赚
    acc.buy('20240102', '000001.SZ', 10.0, 1000)
    acc.settle_open()
    acc.sell('20240103', '000001.SZ', 8.0, 1000)         # 亏

    perf = evaluate(result)
    assert perf.buy_count == 2
    assert perf.sell_count == 2
    assert perf.win_rate == pytest.approx(0.5)
    assert perf.total_fee > 0


def test_format_report_contains_key_metrics():
    text = format_report(evaluate(make_result([100000, 110000])))
    for key in ('总收益率', '年化收益', '最大回撤', '夏普比率', '卖出胜率'):
        assert key in text


def test_equity_dataframe_has_nav():
    df = equity_dataframe(make_result([100000, 110000]))
    assert list(df.columns) == ['date', 'total_asset', 'nav']
    assert df['nav'].iloc[-1] == pytest.approx(1.1)


def test_deals_dataframe_empty_and_filled():
    result = make_result([100000])
    assert deals_dataframe(result).empty

    result.account.buy('20240101', '600000.SH', 10.0, 1000)
    df = deals_dataframe(result)
    assert df['direction'].iloc[0] == '买入'
    assert df['stock'].iloc[0] == '600000.SH'


# ---------------- 结果保存 ----------------

def test_trades_dataframe_pairs_buy_and_sell():
    from momentum.report import trades_dataframe

    result = make_result([100000, 101000])
    acc = result.account
    acc.buy('20240101', '600000.SH', 10.0, 1000, '买入')
    acc.settle_open()
    acc.sell('20240105', '600000.SH', 12.0, 1000, '止盈卖出')

    df = trades_dataframe(result, name_lookup=lambda s: '测试股')
    assert len(df) == 1
    row = df.iloc[0]
    assert row['stock'] == '600000.SH'
    assert row['name'] == '测试股'
    assert row['buy_date'] == '20240101' and row['sell_date'] == '20240105'
    assert row['volume'] == 1000
    assert row['hold_days'] == 4
    assert row['pnl'] == pytest.approx(2000 - row['fee'], abs=0.01)
    assert row['return_pct'] == pytest.approx(row['pnl'] / 10000, abs=1e-6)
    assert row['sell_reason'] == '止盈卖出'


def test_trades_dataframe_fifo_across_lots():
    from momentum.report import trades_dataframe

    result = make_result([100000])
    acc = result.account
    acc.buy('20240101', '600000.SH', 10.0, 1000)
    acc.buy('20240102', '600000.SH', 12.0, 1000)
    acc.settle_open()
    acc.sell('20240103', '600000.SH', 13.0, 1500)

    df = trades_dataframe(result)
    # 先进先出：先平掉 10 元那 1000 股，再平 12 元那 500 股
    assert list(df['volume']) == [1000, 500]
    assert list(df['buy_price']) == [10.0, 12.0]


def test_trades_dataframe_ignores_open_position():
    from momentum.report import trades_dataframe

    result = make_result([100000])
    result.account.buy('20240101', '600000.SH', 10.0, 1000)
    assert trades_dataframe(result).empty


def test_save_results_writes_all_files(tmp_path):
    from momentum.report import save_results

    result = make_result([100000, 105000])
    acc = result.account
    acc.buy('20240101', '600000.SH', 10.0, 1000)
    acc.settle_open()
    acc.sell('20240102', '600000.SH', 12.0, 1000)

    out = tmp_path / 'bt'
    paths = save_results(result, str(out), evaluate(result),
                         name_lookup=lambda s: '测试股',
                         extra={'engine': 'fast'})

    for key in ('equity', 'deals', 'trades', 'summary_json', 'summary_txt'):
        assert os.path.isfile(paths[key])

    deals = pd.read_csv(paths['deals'])
    assert list(deals['direction']) == ['买入', '卖出']
    assert deals['name'].iloc[0] == '测试股'
    assert deals['amount'].iloc[0] == pytest.approx(10000)

    trades = pd.read_csv(paths['trades'])
    assert len(trades) == 1

    with open(paths['summary_json'], encoding='utf-8') as f:
        payload = json.load(f)
    assert payload['engine'] == 'fast'
    assert payload['performance']['total_return'] == pytest.approx(0.05)

    assert '总收益率' in open(paths['summary_txt'], encoding='utf-8').read()


def test_saved_equity_has_daily_detail(tmp_path):
    """引擎跑出来的结果要带上每日目标股、信号和持仓"""
    from momentum.config import AccountConfig, BacktestConfig, StrategyConfig
    from momentum.report import save_results
    from momentum.sample_data import make_source
    from momentum.vector_engine import VectorBacktestEngine

    source = make_source(days=120, index_code=None,
                         stocks=['600000.SH', '000001.SZ', '000002.SZ'])
    days = source.get_trading_dates('99999999')
    config = BacktestConfig(start_date=days[30], end_date=days[-1],
                            strategy=StrategyConfig(rsrs_enabled=False),
                            account=AccountConfig(init_cash=200000.0))
    engine = VectorBacktestEngine(source, config)
    result = engine.run()

    paths = save_results(result, str(tmp_path / 'out'), evaluate(result))
    equity = pd.read_csv(paths['equity'])

    assert len(equity) == len(result.dates)
    for col in ('date', 'total_asset', 'nav', 'daily_return', 'drawdown',
                'cash', 'market_value', 'stock', 'target', 'signal'):
        assert col in equity.columns
    assert equity['signal'].isin(['BUY', 'SELL', 'KEEP', '']).all()
    assert equity['nav'].iloc[0] > 0
