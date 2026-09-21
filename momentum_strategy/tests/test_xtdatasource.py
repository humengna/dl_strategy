# coding: utf-8
"""XtDataSource 的取数行为测试：用桩 xtquant 注入，不需要 QMT。"""

import sys
import types

import pandas as pd
import pytest

from momentum.config import DAILY_FIELDS
from momentum.datasource import CORE_FIELDS, PRELOAD_CHUNK_SIZE


class FakeXtdata:
    """
    可配置的 xtdata 桩：
      - supports_suspend_flag=False 时，请求含 suspendFlag 会返回空表（复现部分 QMT 版本的行为）
      - has_local_data=False 时，任何请求都返回空表（复现本地没下载数据）
      - max_batch 限制单次请求的标的数，超出则整批返回空（复现大批量静默失败）
    """

    def __init__(self, stocks, days=30, supports_suspend_flag=True,
                 has_local_data=True, max_batch=None,
                 batch_download=False, batch_callback=True):
        self.stocks = list(stocks)
        self.dates = pd.bdate_range('20240102', periods=days).strftime('%Y%m%d').tolist()
        self.supports_suspend_flag = supports_suspend_flag
        self.has_local_data = has_local_data
        self.max_batch = max_batch
        self.calls = []
        self.downloaded = []
        if batch_download:
            self.download_history_data2 = (self._batch_with_callback if batch_callback
                                           else self._batch_without_callback)

    # --- 被 XtDataSource 调用的接口 ---

    def get_stock_list_in_sector(self, sector):
        return list(self.stocks)

    def get_trading_dates(self, market, start_time='', end_time='', count=-1):
        return [int(pd.Timestamp(d).timestamp() * 1000) for d in self.dates]

    def timetag_to_datetime(self, t, fmt):
        return pd.Timestamp(t, unit='ms').strftime(fmt)

    def get_instrument_detail(self, stock):
        return {'InstrumentName': f'测试{stock[:6]}', 'TotalValue': 100e8}

    def get_market_data_ex(self, field_list, stock_list, period='1d', start_time='',
                           end_time='', count=-1, dividend_type='none', fill_data=True):
        self.calls.append({'fields': tuple(field_list), 'n_stocks': len(stock_list),
                           'start': start_time, 'end': end_time, 'count': count})

        empty = {s: pd.DataFrame() for s in stock_list}
        if not self.has_local_data:
            return empty
        if self.max_batch is not None and len(stock_list) > self.max_batch:
            return empty
        if 'suspendFlag' in field_list and not self.supports_suspend_flag:
            return empty

        out = {}
        for stock in stock_list:
            if stock not in self.stocks:
                out[stock] = pd.DataFrame()
                continue
            dates = [d for d in self.dates if (not end_time or d <= end_time)]
            if start_time:
                dates = [d for d in dates if d >= start_time]
            if count and count > 0:
                dates = dates[-count:]
            cols = {f: [10.0] * len(dates) for f in field_list}
            out[stock] = pd.DataFrame(cols, index=dates)
        return out

    def download_history_data(self, stock, period='1d', start_time='', end_time=''):
        self.downloaded.append(stock)

    def _batch_with_callback(self, stock_list, period='1d', start_time='',
                             end_time='', callback=None, incrementally=None):
        total = len(stock_list)
        for i, stock in enumerate(stock_list, 1):
            self.downloaded.append(stock)
            if callback is not None:
                callback({'finished': i, 'total': total, 'stockcode': stock, 'message': ''})

    def _batch_without_callback(self, stock_list, period='1d', start_time='', end_time=''):
        # 旧版本签名里没有 callback，传了就抛 TypeError
        self.downloaded.extend(stock_list)


@pytest.fixture
def make_xt(monkeypatch):
    """安装桩 xtquant 并返回 (XtDataSource 工厂, 桩对象)"""
    def _make(**kwargs):
        stocks = kwargs.pop('stocks', ['600000.SH', '000001.SZ'])
        fake = FakeXtdata(stocks, **kwargs)
        module = types.ModuleType('xtquant')
        module.xtdata = fake
        monkeypatch.setitem(sys.modules, 'xtquant', module)
        monkeypatch.setitem(sys.modules, 'xtquant.xtdata', fake)

        from momentum.datasource import XtDataSource
        return XtDataSource(), fake
    return _make


def test_preload_loads_all_stocks(make_xt):
    source, fake = make_xt(stocks=['600000.SH', '000001.SZ'])
    source.preload(['600000.SH', '000001.SZ'], '20240102', '20240229')

    assert set(source._cache) == {'600000.SH', '000001.SZ'}
    assert source.has_data(['600000.SH'], '20240229')


def test_preload_splits_into_chunks(make_xt):
    stocks = [f'{600000 + i}.SH' for i in range(PRELOAD_CHUNK_SIZE * 2 + 10)]
    source, fake = make_xt(stocks=stocks, max_batch=PRELOAD_CHUNK_SIZE)

    source.preload(stocks, '20240102', '20240229')

    # 每批不超过上限，且全部标的都拿到了数据
    batch_calls = [c for c in fake.calls if c['n_stocks'] > 3]
    assert batch_calls and all(c['n_stocks'] <= PRELOAD_CHUNK_SIZE for c in batch_calls)
    assert len(source._cache) == len(stocks)


def test_falls_back_to_core_fields_when_suspend_flag_unsupported(make_xt):
    source, fake = make_xt(supports_suspend_flag=False)
    assert 'suspendFlag' in source.fields

    source.preload(['600000.SH'], '20240102', '20240229')

    assert source.fields == CORE_FIELDS
    assert len(source._cache) == 1
    df = source.get_one('600000.SH', '20240229', 1)
    assert 'suspendFlag' not in df.columns


def test_keeps_full_fields_when_supported(make_xt):
    source, fake = make_xt(supports_suspend_flag=True)
    source.preload(['600000.SH'], '20240102', '20240229')
    assert source.fields == tuple(DAILY_FIELDS)


def test_preload_reports_no_data(make_xt, capsys):
    source, fake = make_xt(has_local_data=False)
    source.preload(['600000.SH', '000001.SZ'], '20240102', '20240229')

    assert source._cache == {}
    out = capsys.readouterr().out
    assert '没有返回任何日线数据' in out
    assert '--download' in out
    assert not source.has_data(['600000.SH'], '20240229')


def test_backtest_fails_fast_without_data(make_xt):
    from momentum.config import AccountConfig, BacktestConfig, StrategyConfig
    from momentum.engine import BacktestEngine

    source, fake = make_xt(has_local_data=False)
    config = BacktestConfig(
        start_date=fake.dates[10], end_date=fake.dates[-1],
        strategy=StrategyConfig(rsrs_enabled=False),
        account=AccountConfig(init_cash=100000.0),
    )
    engine = BacktestEngine(source, config)

    with pytest.raises(RuntimeError, match='取不到任何日线数据'):
        engine.run()


def test_get_bars_falls_back_to_live_query_on_cache_miss(make_xt):
    source, fake = make_xt(stocks=['600000.SH', '000001.SZ'])
    source.preload(['600000.SH'], '20240102', '20240229')
    fake.calls.clear()

    data = source.get_bars(['600000.SH', '000001.SZ'], '20240229', 3)

    assert set(data) == {'600000.SH', '000001.SZ'}
    # 只为未命中缓存的那只发起请求
    assert len(fake.calls) == 1
    assert fake.calls[0]['n_stocks'] == 1


def test_download_falls_back_to_single_stock_api(make_xt):
    source, fake = make_xt(stocks=['600000.SH', '000001.SZ'])
    # 桩没有 download_history_data2，应退回逐只下载
    source.download(['600000.SH', '000001.SZ'], '20240102', '20240229')
    assert fake.downloaded == ['600000.SH', '000001.SZ']


# ---------------- 下载进度 ----------------

def test_download_reports_progress_via_callback(make_xt, capsys):
    stocks = [f'{600000 + i}.SH' for i in range(20)]
    source, fake = make_xt(stocks=stocks, batch_download=True)

    source.download(stocks, '20240102', '20240229')

    out = capsys.readouterr().out
    assert '开始下载日线: 20 只' in out
    assert '[数据] 下载' in out
    assert '100.0%' in out
    assert '20/20' in out
    assert '下载完成' in out
    assert fake.downloaded == stocks


def test_download_falls_back_when_callback_unsupported(make_xt, capsys):
    stocks = ['600000.SH', '000001.SZ']
    source, fake = make_xt(stocks=stocks, batch_download=True, batch_callback=False)

    source.download(stocks, '20240102', '20240229')

    out = capsys.readouterr().out
    assert '不支持进度回调' in out
    assert fake.downloaded == stocks


def test_download_progress_on_per_stock_fallback(make_xt, capsys):
    stocks = [f'{600000 + i}.SH' for i in range(12)]
    source, fake = make_xt(stocks=stocks)          # 桩没有 download_history_data2

    source.download(stocks, '20240102', '20240229')

    out = capsys.readouterr().out
    assert '100.0%' in out and '12/12' in out
    assert fake.downloaded == stocks


def test_download_survives_single_stock_failure(make_xt, capsys):
    stocks = ['600000.SH', 'BAD.SH', '000001.SZ']
    source, fake = make_xt(stocks=stocks)

    original = fake.download_history_data

    def flaky(stock, **kwargs):
        if stock == 'BAD.SH':
            raise RuntimeError('no such stock')
        original(stock, **kwargs)

    fake.download_history_data = flaky
    source.download(stocks, '20240102', '20240229')

    out = capsys.readouterr().out
    assert '1 只失败' in out
    assert fake.downloaded == ['600000.SH', '000001.SZ']


def test_download_progress_can_be_disabled(make_xt, capsys):
    stocks = [f'{600000 + i}.SH' for i in range(5)]
    source, fake = make_xt(stocks=stocks)

    source.download(stocks, '20240102', '20240229', show_progress=False)

    out = capsys.readouterr().out
    assert '%' not in out
    assert '下载完成' in out


def test_preload_shows_progress_for_large_pool(make_xt, capsys):
    stocks = [f'{600000 + i}.SH' for i in range(PRELOAD_CHUNK_SIZE + 50)]
    source, fake = make_xt(stocks=stocks)

    source.preload(stocks, '20240102', '20240229')

    out = capsys.readouterr().out
    assert '[数据] 预加载' in out
    assert '100.0%' in out
    assert f'{len(stocks)}/{len(stocks)}' in out
