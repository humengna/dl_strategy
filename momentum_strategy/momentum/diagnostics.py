# coding: utf-8
"""
xtdata 数据自检。

逐步确认：能否 import xtquant -> 交易日历 -> 板块成分 -> 合约信息 ->
日线数据（按 count 取 / 按区间取）-> 必要时试下载一只标的再重试。
每一步都把 xtdata 的真实返回打出来，便于定位到底卡在哪一环。

    python run_backtest.py --check-data
    python run_backtest.py --check-data --download        # 自检时顺便试下载
"""

from typing import List, Optional, Sequence

DEFAULT_SAMPLES = ('000300.SH', '600000.SH', '000001.SZ')

OK = '[OK]  '
BAD = '[FAIL]'
WARN = '[WARN]'


def _p(tag: str, msg: str) -> None:
    print(f'{tag} {msg}')


def check_xtdata(samples: Sequence[str] = DEFAULT_SAMPLES,
                 sector: str = '沪深A股',
                 start_date: str = '20240101',
                 end_date: str = '20241231',
                 try_download: bool = False) -> bool:
    """返回 True 表示日线数据可用"""
    print('=' * 56)
    print('xtdata 数据自检')
    print('=' * 56)

    # 1. 导入
    try:
        from xtquant import xtdata
    except Exception as e:
        _p(BAD, f'import xtquant 失败: {e}')
        print('  xtquant 随 QMT 安装，一般在 <QMT安装目录>\\bin.x64\\Lib\\site-packages')
        print('  把该目录加入 PYTHONPATH，或直接用 QMT 自带的 python 运行')
        return False
    _p(OK, f'import xtquant 成功（{getattr(xtdata, "__file__", "?")}）')

    # 2. 交易日历
    try:
        dates = xtdata.get_trading_dates('SH', start_time='', end_time=end_date, count=-1)
        days = [xtdata.timetag_to_datetime(t, '%Y%m%d') for t in dates]
        _p(OK, f'交易日历 {len(days)} 个交易日，最后一个 {days[-1] if days else "无"}')
    except Exception as e:
        _p(BAD, f'get_trading_dates 失败: {e}（QMT 客户端可能未启动或未登录）')
        return False

    # 3. 板块
    try:
        stocks = xtdata.get_stock_list_in_sector(sector) or []
        _p(OK 
           if stocks else WARN,
           f'板块 {sector}: {len(stocks)} 只' + (f'，示例 {stocks[:3]}' if stocks else '（为空，检查板块名）'))
    except Exception as e:
        _p(BAD, f'get_stock_list_in_sector 失败: {e}')
        stocks = []

    # 4. 合约信息
    probe = list(samples)
    for stock in probe[:1]:
        try:
            detail = xtdata.get_instrument_detail(stock)
            if detail:
                _p(OK, f'{stock} 合约信息: 名称={detail.get("InstrumentName")} '
                       f'TotalValue={detail.get("TotalValue")}')
                missing = [k for k in ('TotalValue', 'TotalVolume', 'TotalVolumn', 'TotalShares')
                           if k in detail]
                _p(OK, f'  市值相关字段: {missing or "无（市值过滤会被跳过）"}')
            else:
                _p(WARN, f'{stock} 合约信息为空')
        except Exception as e:
            _p(BAD, f'get_instrument_detail 失败: {e}')

    # 5. 日线：按 count 取
    ok_count = _probe_bars(xtdata, probe, mode='count')
    # 6. 日线：按区间取
    ok_range = _probe_bars(xtdata, probe, mode='range',
                           start_date=start_date, end_date=end_date)

    if ok_count or ok_range:
        _p(OK, '日线数据可用，可以直接跑回测')
        return True

    _p(BAD, '日线数据取不到 —— 本地大概率没有下载过日线')

    if not try_download:
        print('\n处理办法（任选其一）：')
        print('  1. 回测命令加 --download，让脚本先补下载')
        print('  2. 在 QMT 客户端「行情 -> 数据管理 / 数据下载」里补充日线数据')
        print('  3. 再跑一次 --check-data --download，让自检直接试一只标的的下载')
        return False

    # 7. 试下载一只再重试
    target = probe[0]
    _p(WARN, f'尝试下载 {target} 的日线 ...')
    try:
        xtdata.download_history_data(target, period='1d',
                                     start_time=start_date, end_time=end_date)
    except Exception as e:
        _p(BAD, f'download_history_data 失败: {e}')
        return False

    if _probe_bars(xtdata, [target], mode='range',
                   start_date=start_date, end_date=end_date):
        _p(OK, f'{target} 下载后可正常读取 —— 整体补下载即可（回测加 --download）')
        return True

    _p(BAD, f'{target} 下载后仍读不到数据，请检查客户端数据权限与磁盘数据目录')
    return False


def _probe_bars(xtdata, stocks: Sequence[str], mode: str,
                start_date: str = '', end_date: str = '') -> bool:
    """分别用完整字段和核心字段探测，打印返回形状"""
    from .config import DAILY_FIELDS
    from .datasource import CORE_FIELDS, DEFAULT_DIVIDEND_TYPE

    for label, fields in (('完整字段', DAILY_FIELDS), ('核心字段', CORE_FIELDS)):
        kwargs = dict(period='1d', dividend_type=DEFAULT_DIVIDEND_TYPE, fill_data=True)
        if mode == 'count':
            kwargs.update(count=5)
            desc = f'count=5 / {label}'
        else:
            kwargs.update(start_time=start_date, end_time=end_date, count=-1)
            desc = f'{start_date}~{end_date} / {label}'

        try:
            data = xtdata.get_market_data_ex(list(fields), list(stocks), **kwargs)
        except Exception as e:
            _p(BAD, f'get_market_data_ex({desc}) 抛异常: {e}')
            continue

        shapes = {s: (0 if data.get(s) is None else len(data[s])) for s in stocks}
        got = [s for s, n in shapes.items() if n > 0]
        if got:
            sample = data[got[0]]
            _p(OK, f'get_market_data_ex({desc}) 返回 {shapes}')
            _p(OK, f'  {got[0]} 列: {list(sample.columns)}')
            _p(OK, f'  最后一行: {sample.tail(1).to_dict("records")}')
            return True
        _p(WARN, f'get_market_data_ex({desc}) 全部为空 {shapes}')

    return False
