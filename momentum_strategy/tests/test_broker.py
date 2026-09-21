# coding: utf-8
import pytest

from momentum.broker import SimAccount
from momentum.config import AccountConfig


def make_account(cash=100000.0, **kw):
    return SimAccount(AccountConfig(init_cash=cash, **kw))


def test_buy_deducts_cash_and_fee():
    acc = make_account()
    assert acc.buy('20240102', '600000.SH', 10.0, 1000)
    # 成交额 10000：佣金 max(10000*1e-4, 5) = 5，过户费 10000*1e-5 = 0.1
    assert abs(acc.cash - (100000 - 10000 - 5 - 0.1)) < 1e-6
    assert acc.positions['600000.SH'].volume == 1000


def test_buy_fee_is_commission_plus_transfer_fee():
    acc = make_account(cash=1000000.0)
    amount = 500000.0
    # 佣金 500000*1e-4 = 50 > 最低 5；过户费 500000*1e-5 = 5
    assert acc.buy_fee(amount) == pytest.approx(50 + 5)


def test_commission_falls_back_to_minimum():
    acc = make_account()
    # 成交额 10000 的佣金按比例只有 1 元，低于最低 5 元
    assert acc.commission(10000.0) == pytest.approx(5.0)
    assert acc.commission(60000.0) == pytest.approx(6.0)


def test_sell_fee_includes_stamp_tax_and_transfer_fee():
    acc = make_account(cash=1000000.0)
    amount = 500000.0
    # 佣金 50 + 过户费 5 + 印花税 500000*5e-4 = 250
    assert acc.sell_fee(amount) == pytest.approx(50 + 5 + 250)


def test_round_trip_cost_rate():
    """大额成交时往返成本 ≈ 万1×2 + 千分之0.01×2 + 千分之0.5 = 0.072%"""
    acc = make_account(cash=10000000.0)
    amount = 1000000.0
    total = acc.buy_fee(amount) + acc.sell_fee(amount)
    assert total / amount == pytest.approx(2 * 1e-4 + 2 * 1e-5 + 5e-4)


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
    expected_fee = max(amount * 1e-4, 5.0) + amount * 1e-5 + amount * 5e-4
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


def test_affordable_volume_reserves_minimum_commission():
    """小额下单时最低佣金 5 元远高于比例费用，必须预留出来"""
    for cash in (1000.0, 2000.0, 5050.0, 10000.0, 12345.6):
        for price in (3.0, 10.0, 47.5):
            acc = make_account(cash=cash)
            vol = acc.affordable_volume(price)
            if vol > 0:
                assert acc.buy('20240102', '600000.SH', price, vol), (cash, price, vol)
                assert acc.cash >= -1e-6
                # 再多买一手就该买不起了
                acc2 = make_account(cash=cash)
                amount = price * (vol + 100)
                assert amount + acc2.buy_fee(amount) > cash + 1e-6


def test_affordable_volume_zero_when_cash_cannot_cover_one_lot_with_fee():
    # 100 股 × 10 元 = 1000 元，但加上 5 元最低佣金就超了
    acc = make_account(cash=1004.0)
    assert acc.affordable_volume(10.0) == 0


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
