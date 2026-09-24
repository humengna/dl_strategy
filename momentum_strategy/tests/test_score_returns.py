# coding: utf-8
"""排行榜信号的持有收益：次日开盘买入，第二天开盘卖出。"""

import os

import numpy as np
import pandas as pd
import pytest

from momentum.config import AccountConfig
from momentum.datasource import CsvDataSource
from momentum.panel import Panel
from momentum.sample_data import make_calendar
from momentum.score_returns import (NOTE_NO_BAR, NOTE_NO_BUY, NOTE_NO_SELL, TRADED, curve_stats,
                                    portfolio_equity, load_scoreboard,
                                    pad_end_date, rank_stats, score_returns)

DAYS = 20
DATES = make_calendar(DAYS, '20240102')


def frame(opens):
    """开盘价直接给定，其余字段不影响收益计算"""
    opens = np.asarray(opens, dtype=float)
    return pd.DataFrame({
        'open': opens, 'close': opens, 'high': opens, 'low': opens,
        'preClose': opens, 'volume': np.where(np.isfinite(opens), 1e6, 0.0),
        'suspendFlag': np.where(np.isfinite(opens), 0.0, 1.0),
    }, index=list(DATES))


def make_panel(frames):
    source = CsvDataSource.from_frames(
        frames, {s: {'InstrumentName': s} for s in frames}, {'沪深A股': list(frames)})
    return Panel.from_source(source, list(frames), DATES[0], DATES[-1],
                             fields=('open', 'close', 'volume', 'suspendFlag'))


def board(rows):
    return pd.DataFrame([{'date': d, 'rank': r, 'stock': s, 'name': s, 'score': 1.0}
                         for d, r, s in rows])


# ---------------- 取价口径 ----------------

def test_收益等于_次日开盘到第二天开盘():
    opens = np.arange(10.0, 10.0 + DAYS)          # 10, 11, 12, ...
    panel = make_panel({'600001.SH': frame(opens)})
    detail = score_returns(board([(DATES[0], 1, '600001.SH')]), panel)

    row = detail.iloc[0]
    assert row['buy_date'] == DATES[1] and row['buy_open'] == 11.0
    assert row['sell_date'] == DATES[2] and row['sell_open'] == 12.0
    assert row['ret'] == pytest.approx(12.0 / 11.0 - 1.0)
    assert row['note'] == TRADED


def test_信号日当天不参与成交():
    """信号日 D 的开盘价既不是买价也不是卖价"""
    opens = np.arange(10.0, 10.0 + DAYS)
    panel = make_panel({'600001.SH': frame(opens)})
    detail = score_returns(board([(DATES[5], 1, '600001.SH')]), panel)
    assert detail.iloc[0]['buy_open'] != opens[5]
    assert detail.iloc[0]['buy_open'] == opens[6]


def test_entry_delay_与_hold_days():
    opens = np.arange(10.0, 10.0 + DAYS)
    panel = make_panel({'600001.SH': frame(opens)})

    same_day = score_returns(board([(DATES[0], 1, '600001.SH')]), panel, delay=0)
    assert same_day.iloc[0]['buy_date'] == DATES[0]

    holding = score_returns(board([(DATES[0], 1, '600001.SH')]), panel, delay=1, hold=3)
    assert holding.iloc[0]['buy_date'] == DATES[1]
    assert holding.iloc[0]['sell_date'] == DATES[4]
    assert holding.iloc[0]['ret'] == pytest.approx(14.0 / 11.0 - 1.0)


def test_逐日首尾相接():
    """今天卖出和明天买入是同一个开盘时点，连乘才成立"""
    opens = np.arange(10.0, 10.0 + DAYS)
    panel = make_panel({'600001.SH': frame(opens)})
    rows = [(DATES[i], 1, '600001.SH') for i in range(5)]
    detail = score_returns(board(rows), panel)
    assert list(detail['sell_date'])[:-1] == list(detail['buy_date'])[1:]


def test_连乘等于首尾开盘价之比():
    opens = np.arange(10.0, 10.0 + DAYS)
    panel = make_panel({'600001.SH': frame(opens)})
    rows = [(DATES[i], 1, '600001.SH') for i in range(8)]
    detail = score_returns(board(rows), panel)
    compound = np.prod(1.0 + detail['ret'].to_numpy())
    assert compound == pytest.approx(opens[9] / opens[1])


# ---------------- 费用 ----------------

def test_扣费后收益低于不扣费():
    opens = np.arange(10.0, 10.0 + DAYS)
    panel = make_panel({'600001.SH': frame(opens)})
    detail = score_returns(board([(DATES[0], 1, '600001.SH')]), panel)
    assert detail.iloc[0]['ret_net'] < detail.iloc[0]['ret']


def test_费用等于买卖两端费率():
    opens = np.full(DAYS, 10.0)                   # 平价进出，收益全部来自费用
    panel = make_panel({'600001.SH': frame(opens)})
    account = AccountConfig()
    detail = score_returns(board([(DATES[0], 1, '600001.SH')]), panel, account=account)

    expect = (1 - account.sell_cost_rate) / (1 + account.buy_cost_rate) - 1.0
    assert detail.iloc[0]['ret'] == pytest.approx(0.0)
    assert detail.iloc[0]['ret_net'] == pytest.approx(expect)


def test_零费率时两列相同():
    opens = np.arange(10.0, 10.0 + DAYS)
    panel = make_panel({'600001.SH': frame(opens)})
    free = AccountConfig(commission_rate=0, transfer_fee_rate=0, stamp_tax_rate=0)
    detail = score_returns(board([(DATES[0], 1, '600001.SH')]), panel, account=free)
    assert detail.iloc[0]['ret_net'] == pytest.approx(detail.iloc[0]['ret'])


# ---------------- 停牌与缺数 ----------------

def test_买入日停牌则这笔作废():
    opens = np.arange(10.0, 10.0 + DAYS)
    opens[1] = np.nan
    panel = make_panel({'600001.SH': frame(opens)})
    detail = score_returns(board([(DATES[0], 1, '600001.SH')]), panel)
    assert detail.iloc[0]['note'] == NOTE_NO_BUY
    assert np.isnan(detail.iloc[0]['ret'])


def test_卖出日停牌则顺延到复牌():
    opens = np.arange(10.0, 10.0 + DAYS)
    opens[2] = opens[3] = np.nan                  # 卖出日和次日都停牌
    panel = make_panel({'600001.SH': frame(opens)})
    detail = score_returns(board([(DATES[0], 1, '600001.SH')]), panel)
    assert detail.iloc[0]['sell_date'] == DATES[4]
    assert detail.iloc[0]['ret'] == pytest.approx(opens[4] / opens[1] - 1.0)


def test_一直停到最后卖不掉():
    opens = np.arange(10.0, 10.0 + DAYS).astype(float)
    opens[2:] = np.nan
    panel = make_panel({'600001.SH': frame(opens)})
    detail = score_returns(board([(DATES[0], 1, '600001.SH')]), panel)
    assert detail.iloc[0]['note'] == NOTE_NO_SELL
    assert np.isnan(detail.iloc[0]['ret'])


def test_面板里没有这只票():
    panel = make_panel({'600001.SH': frame(np.arange(10.0, 10.0 + DAYS))})
    detail = score_returns(board([(DATES[0], 1, '999999.SZ')]), panel)
    assert detail.iloc[0]['note'] == NOTE_NO_BAR


def test_末尾信号取不到后续行情():
    opens = np.arange(10.0, 10.0 + DAYS)
    panel = make_panel({'600001.SH': frame(opens)})
    detail = score_returns(board([(DATES[-1], 1, '600001.SH')]), panel)
    assert np.isnan(detail.iloc[0]['ret'])


# ---------------- 统计 ----------------

def test_curve_stats_累计为连乘():
    stats = curve_stats([0.1, -0.05, 0.02])
    assert stats['total'] == pytest.approx(1.1 * 0.95 * 1.02 - 1.0)
    assert stats['days'] == 3
    assert stats['win_rate'] == pytest.approx(2 / 3)
    assert stats['best'] == pytest.approx(0.1)
    assert stats['worst'] == pytest.approx(-0.05)


def test_curve_stats_回撤为负():
    stats = curve_stats([0.2, -0.5, 0.1])
    # 净值 1.2 -> 0.6 -> 0.66，峰值 1.2，最深一格是第二天的 -50%
    assert stats['max_drawdown'] == pytest.approx(0.6 / 1.2 - 1.0)


def test_curve_stats_空序列():
    assert curve_stats([]) == {}
    assert curve_stats([np.nan, np.nan]) == {}


def test_rank_stats_每个名次一行加一行组合():
    opens = np.arange(10.0, 10.0 + DAYS)
    panel = make_panel({f'60000{i}.SH': frame(opens * (1 + 0.01 * i)) for i in range(1, 4)})
    rows = [(DATES[d], r, f'60000{r}.SH') for d in range(5) for r in (1, 2, 3)]
    detail = score_returns(board(rows), panel)

    stats = rank_stats(detail)
    assert [label for label, _ in stats] == ['第 1 名', '第 2 名', '第 3 名', 'TOP3 等权']
    assert all(s['days'] == 5 for _, s in stats)


def test_组合收益是当日各名次的等权平均():
    panel = make_panel({'600001.SH': frame(np.arange(10.0, 10.0 + DAYS)),
                        '600002.SH': frame(np.full(DAYS, 20.0))})
    rows = [(DATES[0], 1, '600001.SH'), (DATES[0], 2, '600002.SH')]
    detail = score_returns(board(rows), panel)
    curve = portfolio_equity(detail)
    assert curve['ret'].iloc[0] == pytest.approx(detail['ret'].mean())


def test_equity_dataframe_净值为连乘():
    opens = np.arange(10.0, 10.0 + DAYS)
    panel = make_panel({'600001.SH': frame(opens)})
    rows = [(DATES[i], 1, '600001.SH') for i in range(5)]
    curve = portfolio_equity(score_returns(board(rows), panel))
    assert list(curve.columns) == ['date', 'ret', 'equity']
    assert curve['equity'].iloc[-1] == pytest.approx(np.prod(1 + curve['ret'].to_numpy()))


# ---------------- 读入榜单 ----------------

def test_load_scoreboard_读得了_utf8_bom(tmp_path):
    path = tmp_path / 'scores.csv'
    path.write_text('date,rank,stock,name,score\n20240102,1,600001.SH,清源股份,1.5\n',
                    encoding='utf-8-sig')
    df = load_scoreboard(str(path))
    assert df['date'].iloc[0] == '20240102'
    assert df['rank'].iloc[0] == 1
    assert df['name'].iloc[0] == '清源股份'


def test_load_scoreboard_缺列时报错(tmp_path):
    path = tmp_path / 'bad.csv'
    path.write_text('date,stock\n20240102,600001.SH\n', encoding='utf-8')
    with pytest.raises(SystemExit):
        load_scoreboard(str(path))


def test_load_scoreboard_容忍缺少名称与分数(tmp_path):
    path = tmp_path / 'thin.csv'
    path.write_text('date,rank,stock\n20240102,1,600001.SH\n', encoding='utf-8')
    df = load_scoreboard(str(path))
    assert df['name'].iloc[0] == '' and np.isnan(df['score'].iloc[0])


def test_pad_end_date_不超过今天():
    from datetime import datetime
    today = datetime.now().strftime('%Y%m%d')
    assert pad_end_date('20240102', 1, 1, 20) > '20240102'
    assert pad_end_date(today, 1, 1, 20) == today
