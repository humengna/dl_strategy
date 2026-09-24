# coding: utf-8
"""命令行：输出目录命名、多组回看天数、对比表。"""

import json
import os

import pytest

from momentum.cli import (compare_table, display_width, main, pad, run_label,
                          suffix_path)
from momentum.config import StrategyConfig
from momentum.report import Performance
from momentum.sample_data import write_sample_dir


class Args:
    start = '20240101'
    end = '20241231'
    dividend_type = 'back'


# ---------------- 目录命名 ----------------

def test_run_label_includes_lookback():
    label = run_label(Args(), StrategyConfig(lookback_days=29))
    assert label == 'bt_20240101_20241231_lb29'


def test_run_label_differs_per_lookback():
    labels = {run_label(Args(), StrategyConfig(lookback_days=n)) for n in (5, 15, 29)}
    assert len(labels) == 3
    assert all('_lb' in x for x in labels)


def test_run_label_tags_only_non_default_params():
    cfg = StrategyConfig(lookback_days=15, decline_days_to_sell=1,
                         stop_loss_ratio=-0.1, filter_market_cap=False,
                         filter_limit_down=True, rsrs_enabled=False)
    assert run_label(Args(), cfg) == 'bt_20240101_20241231_lb15_dd1_sl10_nocap_ld_norsrs'


def test_run_label_tags_non_default_dividend_type():
    args = Args()
    args.dividend_type = 'none'
    assert run_label(args, StrategyConfig()) == 'bt_20240101_20241231_lb5_divnone'


def test_run_label_omits_default_dividend_type():
    assert 'div' not in run_label(Args(), StrategyConfig())


def test_run_label_tags_disabled_cap_filter():
    cfg = StrategyConfig(filter_market_cap=False)
    assert run_label(Args(), cfg) == 'bt_20240101_20241231_lb5_nocap'


def test_run_label_default_params_stay_short():
    assert run_label(Args(), StrategyConfig()) == 'bt_20240101_20241231_lb5'


def test_suffix_path():
    assert suffix_path('equity.csv', 'lb29') == 'equity_lb29.csv'
    assert suffix_path('out/equity.csv', 'lb5') == 'out/equity_lb5.csv'
    assert suffix_path('', 'lb5') == ''
    assert suffix_path('equity.csv', '') == 'equity.csv'


# ---------------- 对比表 ----------------

def make_perf(**kw):
    base = dict(start_date='20240101', end_date='20241231', trading_days=200,
                init_cash=200000.0, final_asset=250000.0, total_return=0.25,
                annual_return=0.3, max_drawdown=-0.1, max_drawdown_date='20240601',
                sharpe=1.5, buy_count=10, sell_count=10, win_rate=0.6, total_fee=500.0)
    base.update(kw)
    return Performance(**base)


def test_display_width_counts_cjk_as_two():
    assert display_width('abc') == 3
    assert display_width('回看天数') == 8
    assert display_width('5 天') == 4


def test_pad_aligns_by_display_width():
    assert display_width(pad('回看', 10)) == 10
    assert display_width(pad('abc', 10, left=True)) == 10


def test_compare_table_rows_align():
    rows = [('5 天', make_perf()), ('29 天', make_perf(final_asset=310000.0))]
    lines = [l for l in compare_table(rows).split('\n') if l.startswith('  ')]
    assert len({display_width(l) for l in lines}) == 1      # 表头与数据行等宽
    assert '5 天' in lines[1] and '29 天' in lines[2]


def test_compare_table_handles_empty_result():
    text = compare_table([('5 天', None)])
    assert '无有效交易日' in text


# ---------------- 端到端 ----------------

@pytest.fixture(scope='module')
def sample_dir(tmp_path_factory):
    path = tmp_path_factory.mktemp('sample')
    write_sample_dir(str(path), days=160, start='20240102', index_code='')
    return str(path)


def test_multi_lookback_writes_separate_dirs(sample_dir, tmp_path, capsys):
    out = tmp_path / 'runs'
    code = main(['--source', 'csv', '--data-dir', sample_dir,
                 '--start', '20240401', '--end', '20240731',
                 '--lookback', '15', '29', '--no-rsrs', '-q',
                 '--out-dir', str(out)])
    assert code == 0

    dirs = sorted(os.listdir(out))
    assert dirs == ['bt_20240401_20240731_lb15_norsrs',
                    'bt_20240401_20240731_lb29_norsrs']

    for name in dirs:
        for f in ('equity.csv', 'deals.csv', 'trades.csv', 'summary.json'):
            assert os.path.isfile(os.path.join(out, name, f))

    # summary.json 里记着各自的回看天数
    lookbacks = []
    for name in dirs:
        with open(os.path.join(out, name, 'summary.json'), encoding='utf-8') as f:
            payload = json.load(f)
        lookbacks.append(payload['strategy']['lookback_days'])
        assert payload['label'] == name
    assert lookbacks == [15, 29]

    assert '[参数对比]' in capsys.readouterr().out


def test_single_lookback_keeps_plain_out_dir(sample_dir, tmp_path):
    out = tmp_path / 'one'
    assert main(['--source', 'csv', '--data-dir', sample_dir,
                 '--start', '20240401', '--end', '20240731',
                 '--lookback', '15', '--no-rsrs', '-q',
                 '--out-dir', str(out)]) == 0
    assert os.path.isfile(out / 'equity.csv')       # 单次回测不再套一层子目录


def test_multi_lookback_suffixes_explicit_csv(sample_dir, tmp_path):
    equity = tmp_path / 'eq.csv'
    assert main(['--source', 'csv', '--data-dir', sample_dir,
                 '--start', '20240401', '--end', '20240731',
                 '--lookback', '15', '29', '--no-rsrs', '-q', '--no-save',
                 '--equity-csv', str(equity)]) == 0
    assert os.path.isfile(tmp_path / 'eq_lb15.csv')
    assert os.path.isfile(tmp_path / 'eq_lb29.csv')
    assert not os.path.isfile(equity)


def test_duplicate_lookbacks_run_once(sample_dir, tmp_path, capsys):
    out = tmp_path / 'dup'
    assert main(['--source', 'csv', '--data-dir', sample_dir,
                 '--start', '20240401', '--end', '20240731',
                 '--lookback', '15', '15', '--no-rsrs', '-q',
                 '--out-dir', str(out)]) == 0
    assert os.path.isfile(out / 'equity.csv')       # 去重后只剩一组，不套子目录


def test_no_cap_filter_flag_reaches_config(sample_dir, tmp_path):
    out = tmp_path / 'nocap'
    assert main(['--source', 'csv', '--data-dir', sample_dir,
                 '--start', '20240401', '--end', '20240731',
                 '--no-rsrs', '--no-cap-filter', '-q',
                 '--out-dir', str(out)]) == 0

    with open(out / 'summary.json', encoding='utf-8') as f:
        payload = json.load(f)
    assert payload['strategy']['filter_market_cap'] is False
    assert payload['label'].endswith('_nocap_norsrs')


def test_dividend_type_recorded_in_summary(sample_dir, tmp_path):
    out = tmp_path / 'div'
    assert main(['--source', 'csv', '--data-dir', sample_dir,
                 '--start', '20240401', '--end', '20240731',
                 '--no-rsrs', '-q', '--out-dir', str(out)]) == 0

    with open(out / 'summary.json', encoding='utf-8') as f:
        assert json.load(f)['dividend_type'] == 'back'


# ---------------- 每日动量分数排行榜 ----------------

def test_top_scores_默认取前五(tmp_path):
    from momentum.cli import build_parser
    assert build_parser().parse_args([]).top_scores == 0
    assert build_parser().parse_args(['--top-scores']).top_scores == 5
    assert build_parser().parse_args(['--top-scores', '10']).top_scores == 10


def test_top_scores_输出榜单且不跑回测(tmp_path, capsys):
    data_dir = write_sample_dir(str(tmp_path / 'data'), days=120, start='20240102')
    out = tmp_path / 'out'
    assert main(['--source', 'csv', '--data-dir', data_dir,
                 '--start', '20240401', '--end', '20240430',
                 '--top-scores', '3', '--out-dir', str(out), '-q']) == 0

    text = capsys.readouterr().out
    assert 'TOP3' in text
    assert '总收益' not in text                      # 没有跑回测

    files = os.listdir(out)
    assert files == ['scores_20240401_20240430_lb5_top3.csv']
    rows = open(out / files[0], encoding='utf-8-sig').read().splitlines()
    assert rows[0] == 'date,rank,stock,name,score'
    assert len(rows) > 1


def test_top_scores_多组回看天数各出一份(tmp_path):
    data_dir = write_sample_dir(str(tmp_path / 'data'), days=160, start='20240102')
    out = tmp_path / 'out'
    main(['--source', 'csv', '--data-dir', data_dir,
          '--start', '20240601', '--end', '20240630', '--top-scores',
          '--lookback', '5', '29', '--out-dir', str(out), '-q'])
    assert sorted(os.listdir(out)) == ['scores_20240601_20240630_lb29_top5.csv',
                                       'scores_20240601_20240630_lb5_top5.csv']


def test_top_scores_可以不落盘(tmp_path, capsys):
    data_dir = write_sample_dir(str(tmp_path / 'data'), days=120, start='20240102')
    out = tmp_path / 'out'
    main(['--source', 'csv', '--data-dir', data_dir,
          '--start', '20240401', '--end', '20240410',
          '--top-scores', '--out-dir', str(out), '--no-save', '-q'])
    assert not out.exists()
    assert 'TOP5' in capsys.readouterr().out


# ---------------- 榜单收益评估 ----------------

def test_eval_scores_算收益并落两份文件(tmp_path, capsys):
    data_dir = write_sample_dir(str(tmp_path / 'data'), days=300, start='20230103')
    out = tmp_path / 'out'
    main(['--source', 'csv', '--data-dir', data_dir,
          '--start', '20240102', '--end', '20240331',
          '--top-scores', '3', '--out-dir', str(out), '-q'])
    board = str(out / os.listdir(out)[0])

    assert main(['--source', 'csv', '--data-dir', data_dir,
                 '--eval-scores', board, '-q']) == 0

    text = capsys.readouterr().out
    assert '次日开盘买入 / 第二天开盘卖出' in text
    assert 'TOP3 等权' in text

    base = board[:-4]
    detail = base + '_returns_d1h1.csv'
    assert os.path.exists(detail) and os.path.exists(base + '_returns_d1h1_equity.csv')
    head = open(detail, encoding='utf-8-sig').readline().strip()
    assert head == 'date,rank,stock,name,score,buy_date,buy_open,sell_date,sell_open,ret,ret_net,note'


def test_eval_scores_买卖口径写进文件名(tmp_path):
    from momentum.cli import build_parser, eval_out_path
    args = build_parser().parse_args(['--eval-scores', 'a/b/scores.csv',
                                      '--entry-delay', '2', '--hold-days', '5'])
    assert eval_out_path(args) == os.path.join('a', 'b', 'scores_returns_d2h5.csv')


def test_eval_scores_只看第一名(tmp_path, capsys):
    data_dir = write_sample_dir(str(tmp_path / 'data'), days=300, start='20230103')
    out = tmp_path / 'out'
    main(['--source', 'csv', '--data-dir', data_dir,
          '--start', '20240102', '--end', '20240331',
          '--top-scores', '3', '--out-dir', str(out), '-q'])
    board = str(out / [f for f in os.listdir(out) if f.startswith('scores_')][0])

    main(['--source', 'csv', '--data-dir', data_dir, '--eval-scores', board,
          '--eval-ranks', '1', '--no-save', '-q'])
    text = capsys.readouterr().out
    assert '第 1 名' in text and '第 2 名' not in text
