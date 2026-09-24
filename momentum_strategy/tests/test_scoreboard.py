# coding: utf-8
"""每日动量分数排行榜：打分口径与回测一致，但不套任何过滤模块。"""

import os

import numpy as np
import pandas as pd
import pytest

from momentum.config import AccountConfig, BacktestConfig, StrategyConfig
from momentum.datasource import CsvDataSource
from momentum.panel import momentum_score_matrix
from momentum.report import display_width
from momentum.sample_data import make_calendar, make_source
from momentum.scoreboard import (build_score_matrix, daily_top_scores,
                                 format_score, format_scoreboard,
                                 run_scoreboard, save_scoreboard,
                                 scoreboard_dataframe)
from momentum.vector_engine import VectorBacktestEngine

DAYS = 60
RATES = {'600001.SH': 0.006, '600002.SH': 0.004, '600003.SH': 0.002,
         '600004.SH': -0.001}


def const_frame(dates, rate):
    """恒定对数斜率的价格序列：R²=1，分数恰好是 exp(rate×244)-1，便于精确断言"""
    close = 10.0 * np.exp(np.cumsum(np.full(len(dates), rate)))
    pre_close = np.concatenate([[10.0], close[:-1]])
    return pd.DataFrame({
        'open': pre_close, 'high': np.maximum(pre_close, close),
        'low': np.minimum(pre_close, close), 'close': close,
        'preClose': pre_close, 'volume': np.full(len(dates), 1e6),
        'suspendFlag': np.zeros(len(dates)),
    }, index=list(dates))


def make_const_source(details=None):
    dates = make_calendar(DAYS, '20240102')
    frames = {s: const_frame(dates, r) for s, r in RATES.items()}
    detail = {s: {'InstrumentName': f'样例{s[:6]}', 'TotalValue': 100e8} for s in RATES}
    if details:
        for stock, extra in details.items():
            detail[stock].update(extra)
    return CsvDataSource.from_frames(frames, detail, {'沪深A股': list(RATES)}), dates


def make_config(start, end, lookback=5):
    return BacktestConfig(start_date=start, end_date=end,
                          strategy=StrategyConfig(lookback_days=lookback,
                                                  rsrs_enabled=False),
                          account=AccountConfig())


def run(source, dates, top_n=5, lookback=5, start_idx=30):
    config = make_config(dates[start_idx], dates[-1], lookback)
    panel, scores = build_score_matrix(source, config)
    return daily_top_scores(panel, scores, config.start_date, top_n), panel, scores


# ---------------- 排名本身 ----------------

def test_returns_one_row_per_trading_day():
    source, dates = make_const_source()
    rows, _, _ = run(source, dates)
    assert [d for d, _ in rows] == dates[30:]


def test_每日最多取_top_n_只():
    source, dates = make_const_source()
    for top_n in (1, 3, 5):
        rows, _, _ = run(source, dates, top_n=top_n)
        assert all(len(picks) == min(top_n, len(RATES)) for _, picks in rows)


def test_取不满_n_只时不报错():
    source, dates = make_const_source()
    rows, _, _ = run(source, dates, top_n=10)
    assert all(len(picks) == len(RATES) for _, picks in rows)


def test_按分数降序排列():
    source, dates = make_const_source()
    rows, _, _ = run(source, dates)
    for _, picks in rows:
        values = [score for _, score in picks]
        assert values == sorted(values, reverse=True)


def test_排序与斜率一致():
    """恒定斜率下，排名就是斜率从大到小"""
    source, dates = make_const_source()
    rows, _, _ = run(source, dates)
    expect = [s for s, _ in sorted(RATES.items(), key=lambda kv: -kv[1])]
    assert all([s for s, _ in picks] == expect for _, picks in rows)


def test_分数等于闭式解():
    source, dates = make_const_source()
    rows, _, _ = run(source, dates)
    _, picks = rows[0]
    for stock, score in picks:
        assert score == pytest.approx(np.exp(RATES[stock] * 244) - 1.0, rel=1e-9)


# ---------------- 不套过滤模块 ----------------

def test_st_停牌_小市值_依然上榜():
    """引擎会把这只票挡在池子外，排行榜必须照样给出它的分数"""
    source, dates = make_const_source(details={
        '600001.SH': {'InstrumentName': 'ST样例', 'TotalValue': 5e8},
    })
    frame = source._cache['600001.SH']
    frame.loc[frame.index[-5:], 'suspendFlag'] = 1.0
    frame.loc[frame.index[-5:], 'volume'] = 0.0

    rows, _, _ = run(source, dates)
    assert all(picks[0][0] == '600001.SH' for _, picks in rows)      # 始终排第 1


def test_引擎池子确实会剔除这只票():
    """对照组：同样的数据走回测，600001.SH 被 ST + 市值 + 停牌过滤掉"""
    source, dates = make_const_source(details={
        '600001.SH': {'InstrumentName': 'ST样例', 'TotalValue': 5e8},
    })
    engine = VectorBacktestEngine(source, make_config(dates[30], dates[-1]))
    engine.prepare()
    j = engine.panel.stock_pos['600001.SH']
    assert not engine.pool_mask[:, j].any()


def test_不看涨跌停():
    """一字跌停（收盘=前收×0.9）的票照样计分上榜"""
    source, dates = make_const_source()
    frame = source._cache['600001.SH']
    last = frame.index[-1]
    frame.loc[last, 'close'] = frame.loc[frame.index[-2], 'close'] * 0.9
    frame.loc[last, 'preClose'] = frame.loc[frame.index[-2], 'close']

    rows, _, _ = run(source, dates)
    assert '600001.SH' in [s for s, _ in rows[-1][1]]


# ---------------- 与回测口径的一致性 ----------------

def test_分数与向量引擎逐格相同():
    """排行榜和回测共用同一套打分，面板提前量也对齐，结果必须逐格完全相同"""
    source = make_source(days=200, index_code=None,
                         stocks=[f'{600000 + i}.SH' for i in range(6)])
    all_days = source.get_trading_dates('99999999')
    config = make_config(all_days[40], all_days[-1])

    engine = VectorBacktestEngine(source, config)
    engine.prepare()
    panel, scores = build_score_matrix(source, config)

    for date in all_days[40:]:
        i, k = panel.date_pos[date], engine.panel.date_pos[date]
        for stock in panel.stocks:
            a = scores[i, panel.stock_pos[stock]]
            b = engine.scores[k, engine.panel.stock_pos[stock]]
            assert (np.isnan(a) and np.isnan(b)) or a == b


def test_没有未来函数():
    """把 date 之后的数据全部删掉，当天的榜单不变"""
    source, dates = make_const_source()
    cut = dates[45]

    full, _, _ = run(source, dates)
    full_day = dict(full)[cut]

    for stock in RATES:
        source._cache[stock] = source._cache[stock].loc[:cut]
    config = make_config(dates[30], cut)
    panel, scores = build_score_matrix(source, config)
    trimmed = dict(daily_top_scores(panel, scores, config.start_date, 5))[cut]

    assert [s for s, _ in trimmed] == [s for s, _ in full_day]
    for (_, a), (_, b) in zip(trimmed, full_day):
        assert a == pytest.approx(b, rel=1e-9)


def test_分数排除当日():
    """第 i 天的榜单只用到 i-1 为止的收盘价"""
    source, dates = make_const_source()
    _, panel, scores = run(source, dates)
    close = panel.field('close')
    direct = momentum_score_matrix(close, 5, 244)
    i = panel.date_pos[dates[40]]
    np.testing.assert_allclose(scores[i], direct[i], rtol=0, atol=0)
    assert np.isnan(scores[0]).all()


# ---------------- 输出 ----------------

def test_dataframe_列与行数():
    source, dates = make_const_source()
    rows, _, _ = run(source, dates, top_n=3)
    df = scoreboard_dataframe(rows, source.get_stock_name)
    assert list(df.columns) == ['date', 'rank', 'stock', 'name', 'score']
    assert len(df) == len(rows) * 3
    assert set(df['rank']) == {1, 2, 3}
    assert df['name'].iloc[0] != ''


def test_保存_csv(tmp_path):
    source, dates = make_const_source()
    rows, _, _ = run(source, dates)
    path = save_scoreboard(rows, str(tmp_path / 'scores.csv'), source.get_stock_name)
    back = pd.read_csv(path, dtype={'date': str})
    assert len(back) == len(rows) * len(RATES)
    assert back['date'].iloc[0] == dates[30]


def test_format_score_跨量级():
    assert format_score(1.5) == '1.5000'
    assert 'e+' in format_score(1.2e10)
    assert 'e-' in format_score(3.0e-9)
    assert format_score(0.0) == '0.0000'
    assert format_score(float('nan')) == 'nan'


def test_表格按显示宽度对齐():
    source, dates = make_const_source()
    rows, _, _ = run(source, dates, top_n=3)
    lines = format_scoreboard(rows, 3, 5, source.get_stock_name).splitlines()
    body = [x for x in lines if x.startswith('  ') and '.SH' in x]
    assert len({display_width(x) for x in body}) == 1


def test_只打印最后几天(capsys):
    source, dates = make_const_source()
    rows, _, _ = run(source, dates, top_n=2)
    text = format_scoreboard(rows, 2, 5, None, max_days=3)
    printed = {line.split()[0] for line in text.splitlines() if line.startswith('  2024')}
    assert printed == set(dates[-3:])
    assert str(len(rows)) in text


def test_run_scoreboard_端到端(tmp_path, capsys):
    source, dates = make_const_source()
    config = make_config(dates[30], dates[-1])
    out = str(tmp_path / 'scores.csv')
    rows = run_scoreboard(source, config, top_n=5, out_path=out)
    assert os.path.exists(out)
    assert len(rows) == len(dates) - 30
    assert 'TOP5' in capsys.readouterr().out
