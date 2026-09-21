# coding: utf-8
"""
行情数据源。

DataSource 定义策略需要的最小接口，策略层只依赖这个接口：
  - XtDataSource  : 生产环境，走 xtquant.xtdata（需要本机 QMT / 投研端在线）
  - CsvDataSource : 离线环境，从 csv 目录读数据，用于单元测试和无 QMT 时试跑

约定：
  - 日期一律用 'YYYYMMDD' 字符串
  - get_bars 返回 {stock: DataFrame}，DataFrame 按日期升序，index 为日期字符串
  - 查询区间是「截至 end_date（含）的最后 count 根」
"""

import json
import os
from typing import Dict, Iterable, List, Optional, Sequence

import pandas as pd

from .config import DAILY_FIELDS
from .progress import Progress

# 单次 get_market_data_ex 的标的数量上限。一次性请求几千只容易超时或静默返回空表。
PRELOAD_CHUNK_SIZE = 300

# 所有 QMT 版本都支持的字段，作为 suspendFlag 不可用时的退路
CORE_FIELDS = ('open', 'high', 'low', 'close', 'preClose', 'volume')

NO_DATA_HINT = """
[数据] xtdata 没有返回任何日线数据，请按以下顺序排查：
  1. QMT / 投研端客户端是否已启动并登录（xtdata 只读本机客户端的数据缓存）
  2. 本地是否下载过日线 —— 加 --download 重跑，或在客户端
     「行情 -> 数据管理 / 数据下载」里补充日线数据
  3. 运行 python run_backtest.py --check-data 逐步定位到底哪一步取不到数
"""


class DataSource(object):
    """数据源接口"""

    def get_sector_stocks(self, sector: str) -> List[str]:
        raise NotImplementedError

    def get_trading_dates(self, end_date: str) -> List[str]:
        """返回截至 end_date 的全部交易日（升序）"""
        raise NotImplementedError

    def get_bars(self, stocks: Sequence[str], end_date: str, count: int,
                 fields: Optional[Sequence[str]] = None) -> Dict[str, pd.DataFrame]:
        raise NotImplementedError

    def get_detail(self, stock: str) -> Optional[dict]:
        raise NotImplementedError

    def preload(self, stocks: Sequence[str], start_date: str, end_date: str) -> None:
        """可选：批量预加载到内存"""
        return None

    def download(self, stocks: Sequence[str], start_date: str, end_date: str) -> None:
        """可选：补下载本地数据"""
        return None

    # ---------- 基于 get_bars / get_detail 的通用便捷方法 ----------

    def get_one(self, stock: str, end_date: str, count: int,
                fields: Optional[Sequence[str]] = None) -> Optional[pd.DataFrame]:
        df = self.get_bars([stock], end_date, count, fields).get(stock)
        if df is None or len(df) == 0:
            return None
        return df

    def has_data(self, stocks: Sequence[str], end_date: str, sample: int = 20) -> bool:
        """抽样检查数据源在 end_date 之前是否有日线数据"""
        probe = list(stocks)[:sample]
        if not probe:
            return False
        data = self.get_bars(probe, end_date, 1)
        return any(df is not None and len(df) > 0 for df in data.values())

    def get_stock_name(self, stock: str) -> str:
        detail = self.get_detail(stock)
        if not detail:
            return ''
        return detail.get('InstrumentName', '') or ''

    def get_market_cap(self, stock: str, last_close: float = 0.0) -> float:
        """
        总市值。优先取 TotalValue；缺失时用总股本 × 最新收盘价估算。
        不同 QMT 版本总股本字段名不一致，依次尝试。
        """
        detail = self.get_detail(stock)
        if not detail:
            return 0.0

        total_value = detail.get('TotalValue', 0) or 0
        if total_value > 0:
            return float(total_value)

        for key in ('TotalVolume', 'TotalVolumn', 'TotalShares'):
            shares = detail.get(key, 0) or 0
            if shares > 0 and last_close > 0:
                return float(shares) * float(last_close)
        return 0.0


def _normalize(df: pd.DataFrame, end_date: str, count: int) -> pd.DataFrame:
    """index 统一成日期字符串，按 end_date 截断并取最后 count 根"""
    out = df.copy()
    out.index = [str(i)[:8] for i in out.index]
    out = out.loc[out.index <= end_date]
    if count and count > 0:
        out = out.tail(count)
    return out


class XtDataSource(DataSource):
    """
    xtquant.xtdata 数据源。

    use_cache=True 时 preload 会把整个回测区间的日线一次性读进内存，
    之后按日期切片，避免逐个交易日对全市场重复调用 get_market_data_ex。
    缓存未命中的标的会自动回落到实时查询。
    """

    def __init__(self, use_cache: bool = True, market: str = 'SH'):
        from xtquant import xtdata  # 延迟导入：没有 QMT 的机器也能 import 本模块

        self._xtdata = xtdata
        self.use_cache = use_cache
        self.market = market
        self.fields = tuple(DAILY_FIELDS)
        self._cache: Dict[str, pd.DataFrame] = {}
        self._detail_cache: Dict[str, Optional[dict]] = {}

    # ---------- 板块 / 日历 ----------

    def get_sector_stocks(self, sector: str) -> List[str]:
        try:
            return list(self._xtdata.get_stock_list_in_sector(sector) or [])
        except Exception as e:
            print(f'[数据] 板块 {sector} 获取失败: {e}')
            return []

    def get_trading_dates(self, end_date: str) -> List[str]:
        timetags = self._xtdata.get_trading_dates(self.market, start_time='',
                                                  end_time=end_date, count=-1)
        days = [self._xtdata.timetag_to_datetime(t, '%Y%m%d') for t in timetags]
        return [d for d in days if d <= end_date]

    # ---------- 行情 ----------

    def _raw_fetch(self, fields, stocks, start_date='', end_date='', count=-1):
        """直接调用 get_market_data_ex，异常时返回空 dict"""
        try:
            data = self._xtdata.get_market_data_ex(
                list(fields), list(stocks),
                period='1d',
                start_time=start_date,
                end_time=end_date,
                count=count,
                dividend_type='none',
                fill_data=True,
            )
        except Exception as e:
            print(f'[数据] get_market_data_ex 调用失败: {e}')
            return {}
        return data or {}

    @staticmethod
    def _any_rows(data) -> bool:
        return any(df is not None and len(df) > 0 for df in data.values())

    def resolve_fields(self, sample_stocks, start_date='', end_date='') -> bool:
        """
        用少量标的探测可用字段。

        某些 QMT 版本不支持 suspendFlag，整批请求会直接返回空表，
        这里探测失败就退回核心字段，仍然为空则判定为本地无数据。
        """
        sample = list(sample_stocks)[:3]
        if not sample:
            return False

        for fields in (self.fields, CORE_FIELDS):
            if self._any_rows(self._raw_fetch(fields, sample, start_date, end_date)):
                if tuple(fields) != tuple(self.fields):
                    print(f'[数据] 字段 {sorted(set(self.fields) - set(fields))} 不可用，改用核心字段')
                    self.fields = tuple(fields)
                return True
        return False

    def preload(self, stocks, start_date, end_date, show_progress: bool = True):
        if not self.use_cache or not stocks:
            return

        stocks = list(dict.fromkeys(stocks))
        print(f'[数据] 预加载 {len(stocks)} 只标的 {start_date} ~ {end_date} ...')

        if not self.resolve_fields(stocks, start_date, end_date):
            print(NO_DATA_HINT)
            return

        loaded = 0
        processed = 0
        total_chunks = (len(stocks) + PRELOAD_CHUNK_SIZE - 1) // PRELOAD_CHUNK_SIZE
        bar = Progress(len(stocks), prefix='[数据] 预加载',
                       enabled=show_progress and total_chunks > 1)

        for idx in range(total_chunks):
            chunk = stocks[idx * PRELOAD_CHUNK_SIZE:(idx + 1) * PRELOAD_CHUNK_SIZE]
            data = self._raw_fetch(self.fields, chunk, start_date, end_date)
            for stock in chunk:
                df = data.get(stock)
                if df is None or len(df) == 0:
                    continue
                df = df.copy()
                df.index = [str(i)[:8] for i in df.index]
                self._cache[stock] = df
                loaded += 1
            processed += len(chunk)
            bar.update(processed, suffix=f'已加载 {loaded} 只')
        bar.close()

        print(f'[数据] 预加载完成，{loaded} 只有数据')
        if loaded == 0:
            print(NO_DATA_HINT)

    def get_bars(self, stocks, end_date, count, fields=None):
        if isinstance(stocks, str):
            stocks = [stocks]
        fields = list(fields or self.fields)

        result: Dict[str, pd.DataFrame] = {}
        missing: List[str] = []

        if self.use_cache:
            for stock in stocks:
                cached = self._cache.get(stock)
                if cached is None:
                    missing.append(stock)
                    continue
                sub = _normalize(cached, end_date, count)
                if len(sub) > 0:
                    result[stock] = sub
            if not missing:
                return result
        else:
            missing = list(stocks)

        data = self._raw_fetch(fields, missing, end_date=end_date, count=count)

        for stock in missing:
            df = data.get(stock)
            if df is None or len(df) == 0:
                continue
            result[stock] = _normalize(df, end_date, count)
        return result

    # ---------- 合约信息 ----------

    def get_detail(self, stock):
        if stock in self._detail_cache:
            return self._detail_cache[stock]
        try:
            detail = self._xtdata.get_instrument_detail(stock)
        except Exception:
            detail = None
        self._detail_cache[stock] = detail
        return detail

    def download(self, stocks, start_date, end_date, show_progress: bool = True):
        """
        补下载本地日线，带进度显示。

        优先用 download_history_data2 + callback（xtdata 会实时回调下载进度）；
        该接口或 callback 参数不可用时，退回逐只下载并自己统计进度。
        """
        stocks = list(dict.fromkeys(stocks))
        if not stocks:
            return

        print(f'[数据] 开始下载日线: {len(stocks)} 只，{start_date} ~ {end_date}')
        bar = Progress(len(stocks), prefix='[数据] 下载', enabled=show_progress)

        def _callback(data):
            """xtdata 回调，data 形如 {'finished': n, 'total': m, 'stockcode': '600000.SH'}"""
            try:
                total = int(data.get('total') or 0)
                finished = int(data.get('finished') or 0)
            except (AttributeError, TypeError, ValueError):
                return
            if total > 0:
                bar.total = total
            bar.update(finished, suffix=str(data.get('stockcode') or ''))

        batch = getattr(self._xtdata, 'download_history_data2', None)
        if batch is not None:
            try:
                batch(stocks, period='1d', start_time=start_date,
                      end_time=end_date, callback=_callback)
                bar.close()
                print('[数据] 下载完成')
                return
            except TypeError:
                # 该版本的 download_history_data2 不接受 callback
                try:
                    batch(stocks, period='1d', start_time=start_date, end_time=end_date)
                    bar.update(len(stocks))
                    bar.close()
                    print('[数据] 下载完成（该版本不支持进度回调）')
                    return
                except Exception as e:
                    print(f'[数据] 批量下载失败，改为逐只下载: {e}')
            except Exception as e:
                print(f'[数据] 批量下载失败，改为逐只下载: {e}')

        failed = []
        for i, stock in enumerate(stocks, 1):
            try:
                self._xtdata.download_history_data(stock, period='1d',
                                                   start_time=start_date, end_time=end_date)
            except Exception as e:
                failed.append((stock, str(e)))
            bar.update(i, suffix=stock)
        bar.close()

        if failed:
            print(f'[数据] 下载完成，{len(failed)} 只失败，例如 {failed[:3]}')
        else:
            print('[数据] 下载完成')


class CsvDataSource(DataSource):
    """
    离线 csv 数据源，目录结构：

        data_dir/
          bars/600000.SH.csv        # 列：date,open,high,low,close,preClose,volume,suspendFlag
          instruments.json          # {"600000.SH": {"InstrumentName": "浦发银行", "TotalValue": 3.2e10}}
          sectors.json              # {"沪深A股": ["600000.SH", ...]}

    用于单元测试，以及没有 QMT 环境时用自备数据跑通流程。
    """

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._cache: Dict[str, pd.DataFrame] = {}
        self._details: Dict[str, dict] = {}
        self._sectors: Dict[str, List[str]] = {}
        self._load()

    def _load(self):
        bars_dir = os.path.join(self.data_dir, 'bars')
        if os.path.isdir(bars_dir):
            for name in sorted(os.listdir(bars_dir)):
                if not name.endswith('.csv'):
                    continue
                stock = name[:-4]
                df = pd.read_csv(os.path.join(bars_dir, name), dtype={'date': str})
                df = df.set_index('date').sort_index()
                df.index = [str(i)[:8] for i in df.index]
                self._cache[stock] = df

        detail_path = os.path.join(self.data_dir, 'instruments.json')
        if os.path.isfile(detail_path):
            with open(detail_path, encoding='utf-8') as f:
                self._details = json.load(f)

        sector_path = os.path.join(self.data_dir, 'sectors.json')
        if os.path.isfile(sector_path):
            with open(sector_path, encoding='utf-8') as f:
                self._sectors = json.load(f)

    # ---------- 接口实现 ----------

    def get_sector_stocks(self, sector):
        return list(self._sectors.get(sector, []))

    def get_trading_dates(self, end_date):
        days = set()
        for df in self._cache.values():
            days.update(df.index)
        return sorted(d for d in days if d <= end_date)

    def get_bars(self, stocks, end_date, count, fields=None):
        if isinstance(stocks, str):
            stocks = [stocks]
        result = {}
        for stock in stocks:
            df = self._cache.get(stock)
            if df is None:
                continue
            sub = _normalize(df, end_date, count)
            if len(sub) > 0:
                result[stock] = sub
        return result

    def get_detail(self, stock):
        return self._details.get(stock)

    # ---------- 供测试构造数据 ----------

    @classmethod
    def from_frames(cls, frames: Dict[str, pd.DataFrame],
                    details: Optional[Dict[str, dict]] = None,
                    sectors: Optional[Dict[str, Iterable[str]]] = None) -> 'CsvDataSource':
        """直接用内存中的 DataFrame 构造数据源，不读磁盘"""
        obj = cls.__new__(cls)
        obj.data_dir = ''
        obj._cache = {}
        for stock, df in frames.items():
            d = df.copy()
            d.index = [str(i)[:8] for i in d.index]
            obj._cache[stock] = d.sort_index()
        obj._details = dict(details or {})
        obj._sectors = {k: list(v) for k, v in (sectors or {}).items()}
        return obj
