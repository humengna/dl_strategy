# coding: utf-8
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
