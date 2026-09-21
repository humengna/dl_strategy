# coding: utf-8
from momentum.broker import SimAccount
from momentum.config import AccountConfig


def make_account(cash=100000.0, **kw):
    return SimAccount(AccountConfig(init_cash=cash, **kw))


def test_buy_deducts_cash_and_fee():
    acc = make_account()
    assert acc.buy('20240102', '600000.SH', 10.0, 1000)
    # 成交额 10000，佣金 max(10000*2.5e-4, 5) = 5
    assert abs(acc.cash - (100000 - 10000 - 5)) < 1e-6
    assert acc.positions['600000.SH'].volume == 1000


def test_buy_is_frozen_until_next_day():
    acc = make_account()
    acc.buy('20240102', '600000.SH', 10.0, 1000)
    assert acc.holdings_can_use() == {}          # T+1，当日不可卖
    assert not acc.sell('20240102', '600000.SH', 11.0, 1000)

    acc.settle_open()
    assert acc.holdings_can_use() == {'600000.SH': 1000}
    assert acc.sell('20240103', '600000.SH', 11.0, 1000)


def test_t0_account_can_sell_same_day():
    acc = make_account(t_plus_one=False)
    acc.buy('20240102', '600000.SH', 10.0, 1000)
    assert acc.holdings_can_use() == {'600000.SH': 1000}


def test_sell_applies_stamp_tax_and_records_pnl():
    acc = make_account()
    acc.buy('20240102', '600000.SH', 10.0, 1000)
    acc.settle_open()
    acc.sell('20240103', '600000.SH', 11.0, 1000)

    deal = acc.deals[-1]
    amount = 11.0 * 1000
    expected_fee = max(amount * 2.5e-4, 5.0) + amount * 5e-4
    assert abs(deal.fee - expected_fee) < 1e-9
    assert abs(deal.realized_pnl - ((11.0 - 10.0) * 1000 - expected_fee)) < 1e-9
    assert '600000.SH' not in acc.positions


def test_buy_rejected_when_cash_insufficient():
    acc = make_account(cash=1000.0)
    assert not acc.buy('20240102', '600000.SH', 10.0, 1000)
    assert acc.cash == 1000.0
    assert not acc.positions


def test_affordable_volume_is_whole_lots_and_leaves_room_for_fee():
    acc = make_account(cash=10000.0)
    vol = acc.affordable_volume(10.0)
    assert vol % 100 == 0
    assert acc.buy('20240102', '600000.SH', 10.0, vol)
    assert acc.cash >= 0


def test_average_cost_on_add():
    acc = make_account()
    acc.buy('20240102', '600000.SH', 10.0, 1000)
    acc.buy('20240102', '600000.SH', 12.0, 1000)
    assert abs(acc.positions['600000.SH'].open_price - 11.0) < 1e-9
    assert acc.positions['600000.SH'].volume == 2000


def test_total_asset_uses_price_map():
    acc = make_account()
    acc.buy('20240102', '600000.SH', 10.0, 1000)
    assert abs(acc.total_asset({'600000.SH': 12.0}) - (acc.cash + 12000)) < 1e-6
    # 没给价格时回落到成本价
    assert abs(acc.total_asset() - (acc.cash + 10000)) < 1e-6


def test_sell_all_closes_position():
    acc = make_account()
    acc.buy('20240102', '600000.SH', 10.0, 1000)
    acc.settle_open()
    assert acc.sell_all('20240103', '600000.SH', 9.0)
    assert not acc.positions
