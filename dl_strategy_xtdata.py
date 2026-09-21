# coding: utf-8
"""
A股股票策略 [xtdata 版]：热门概念池 + 对数线性回归动量打分 + RSRS修正标准分大盘择时
                        + 动量分数连续下降个股择时 + 固定 -15% 硬止损

与 QMT 内置回测版（handlebar + passorder）的主要区别
--------------------------------------------------
  1. 数据层全部改用 xtquant.xtdata，不再依赖策略上下文对象 C
       C.get_market_data_ex        -> xtdata.get_market_data_ex
       C.get_stock_list_in_sector  -> xtdata.get_stock_list_in_sector
       C.get_instrument_detail     -> xtdata.get_instrument_detail
       C.get_stock_name            -> xtdata.get_instrument_detail()['InstrumentName']
  2. 驱动方式：不再用 handlebar 逐 K 线回调，改为按 xtdata.get_trading_dates
     取到的交易日列表自行循环（一个交易日 = 原来的一根日 K）
  3. 交易层：xtdata 只有行情没有交易，passorder / get_trade_detail_data
     由本文件内的 SimAccount 模拟撮合账户替代（含 T+1、手续费、印花税）
  4. 为避免每个交易日对全市场重复取数，默认一次性把回测区间内的行情读进内存缓存
     （BacktestData），再按日期切片；可用 use_cache=False 退回逐日直接查询
  5. 脚本可直接 python 运行，需要本机已启动 迅投QMT/投研端 并已下载好日线数据

运行方式
--------
  python dl_strategy_xtdata.py --start 20240101 --end 20241231 --cash 200000
  首次运行本地缺数据时加 --download 先补下载（耗时较长）
"""

import argparse
import math
from datetime import datetime

import numpy as np
import pandas as pd

from xtquant import xtdata

# ============================================================
# 策略参数（与原版保持一致）
# ============================================================

STRATEGY_NAME = '动量择时策略'

# 概念板块列表
CONCEPT_SECTORS = [
    '锂电池', '芯片', '人工智能', '光伏', '军工', '新能源车', '储能',
    '5G', '半导体', '国产软件', '云计算', '大数据', '物联网', '机器人',
    '氢能源', '风能', '核电', '特高压', '充电桩', '智能电网', '工业互联网',
    '数字货币', '区块链', '元宇宙', 'VR', '消费电子', '汽车电子', '无人驾驶',
    '高端装备', '新材料', '稀土永磁', '石墨烯', '碳纤维', '降解塑料',
    '医美', '创新药', '生物疫苗', '基因测序', '医疗器械', '中药',
    '白酒', '食品饮料', '免税', '电商', '网红经济', '在线教育',
    '卫星导航', '大飞机', '军民融合', '一带一路', '雄安新区', '海南自贸',
    '碳中和', '环保', '固废处理', '污水处理', '垃圾分类',
    '网络安全', '信创', '东数西算', '量子科技', '脑机接口',
]
CONCEPT_SECTORS = ['沪深A股']

# 过滤条件
MIN_MARKET_CAP = 30e8
MAX_MARKET_CAP = 500e8

# 动量打分参数
LOOKBACK_DAYS = 5          # 29
TRADING_DAYS_PER_YEAR = 244

# RSRS 参数
RSRS_N = 21
RSRS_M = 600
RSRS_INDEX = '000300.SH'

# 止损线
STOP_LOSS_RATIO = -0.15

# 预热天数：前若干个交易日数据不足，不参与交易（对应原版 bar_count < LOOKBACK_DAYS + 10）
WARMUP_DAYS = LOOKBACK_DAYS + 10

# 行情字段
DAILY_FIELDS = ['open', 'high', 'low', 'close', 'preClose', 'volume', 'suspendFlag']

# 交易成本
COMMISSION_RATE = 2.5e-4   # 佣金万 2.5
MIN_COMMISSION = 5.0       # 单笔最低 5 元
STAMP_TAX_RATE = 5e-4      # 卖出印花税万 5


# ============================================================
# 模拟账户：替代 get_trade_detail_data / passorder
# ============================================================

class Position(object):
    """持仓。open_price 对应原版 m_dOpenPrice（成本价），can_use 对应 m_nCanUseVolume（T+1 可卖量）"""

    def __init__(self, stock, volume, price):
        self.stock = stock
        self.volume = volume
        self.can_use = 0          # 当日买入不可卖，次日开盘结算
        self.open_price = price


class Deal(object):
    def __init__(self, date, stock, direction, price, volume, fee, msg):
        self.date = date
        self.stock = stock
        self.direction = direction    # 1 买入 / -1 卖出
        self.price = price
        self.volume = volume
        self.fee = fee
        self.msg = msg


class SimAccount(object):
    """
    极简模拟股票账户：
      - T+1：当日买入次日才计入 can_use
      - 按成交额收佣金（最低 5 元），卖出额外收印花税
      - 以传入价格立即全额成交（不模拟盘口冲击）
    """

    def __init__(self, init_cash):
        self.init_cash = float(init_cash)
        self.cash = float(init_cash)
        self.positions = {}      # stock -> Position
        self.deals = []          # 全部成交
        self.equity_curve = []   # [(date, total_asset)]

    # ---------- 查询（对应 get_trade_detail_data） ----------

    def get_positions(self):
        return [p for p in self.positions.values() if p.volume > 0]

    def get_holdings_can_use(self):
        """返回 {stock: 可用数量}，对应原版遍历 m_nCanUseVolume 的结果"""
        return {s: p.can_use for s, p in self.positions.items() if p.can_use > 0}

    def settle_open(self):
        """每个交易日开盘前调用：解冻昨日买入的股票（T+1）"""
        for pos in self.positions.values():
            pos.can_use = pos.volume

    # ---------- 交易（对应 passorder） ----------

    def buy(self, date, stock, price, volume, msg=''):
        if volume <= 0 or price <= 0:
            return False
        amount = price * volume
        fee = max(amount * COMMISSION_RATE, MIN_COMMISSION)
        if amount + fee > self.cash + 1e-6:
            print(f'[账户] 资金不足，买入失败 {stock} 需要:{amount + fee:.2f} 可用:{self.cash:.2f}')
            return False

        self.cash -= amount + fee
        pos = self.positions.get(stock)
        if pos is None:
            self.positions[stock] = Position(stock, volume, price)
        else:
            total_cost = pos.open_price * pos.volume + amount
            pos.volume += volume
            pos.open_price = total_cost / pos.volume
        self.deals.append(Deal(date, stock, 1, price, volume, fee, msg))
        print(f'[账户] 买入成交 {stock} 价格:{price:.2f} 数量:{volume} 费用:{fee:.2f} 余额:{self.cash:.2f}')
        return True

    def sell(self, date, stock, price, volume, msg=''):
        pos = self.positions.get(stock)
        if pos is None or volume <= 0 or price <= 0:
            return False
        volume = min(volume, pos.can_use)
        if volume <= 0:
            print(f'[账户] {stock} 无可用数量，卖出跳过（T+1）')
            return False

        amount = price * volume
        fee = max(amount * COMMISSION_RATE, MIN_COMMISSION) + amount * STAMP_TAX_RATE
        self.cash += amount - fee
        pos.volume -= volume
        pos.can_use -= volume
        if pos.volume <= 0:
            del self.positions[stock]
        self.deals.append(Deal(date, stock, -1, price, volume, fee, msg))
        print(f'[账户] 卖出成交 {stock} 价格:{price:.2f} 数量:{volume} 费用:{fee:.2f} 余额:{self.cash:.2f}')
        return True

    # ---------- 估值 ----------

    def total_asset(self, price_map):
        mv = 0.0
        for stock, pos in self.positions.items():
            px = price_map.get(stock, pos.open_price)
            if px and px > 0:
                mv += px * pos.volume
            else:
                mv += pos.open_price * pos.volume
        return self.cash + mv


# ============================================================
# 数据层：xtdata 读取 + 内存缓存
# ============================================================

class BacktestData(object):
    """
    统一封装 xtdata 行情读取。

    use_cache=True 时，一次性把 [warmup_start, end] 区间的日线读进内存，
    之后按 end_date/count 切片，避免逐日对全市场反复调用 get_market_data_ex。
    """

    def __init__(self, use_cache=True):
        self.use_cache = use_cache
        self._cache = {}            # stock -> DataFrame(index=日期字符串)
        self._detail_cache = {}     # stock -> instrument detail dict
        self._name_cache = {}

    # ---------- 缓存预加载 ----------

    def preload(self, stocks, start_date, end_date, fields=None):
        if not self.use_cache or not stocks:
            return
        fields = fields or DAILY_FIELDS
        stocks = list(dict.fromkeys(stocks))
        print(f'[数据] 预加载行情: {len(stocks)} 只, {start_date} ~ {end_date} ...')
        data = xtdata.get_market_data_ex(
            fields, stocks,
            period='1d',
            start_time=start_date,
            end_time=end_date,
            dividend_type='none',
            fill_data=True,
        )
        loaded = 0
        for stock in stocks:
            df = data.get(stock)
            if df is None or len(df) == 0:
                continue
            df = df.copy()
            df.index = [str(i) for i in df.index]
            self._cache[stock] = df
            loaded += 1
        print(f'[数据] 预加载完成: {loaded} 只有数据')

    # ---------- 行情查询（对应 C.get_market_data_ex） ----------

    def get_bars(self, stocks, end_date, count, fields=None):
        """
        返回 {stock: DataFrame}，DataFrame 为截至 end_date（含）的最后 count 根日线。
        无数据的标的不出现在返回值中。
        """
        fields = fields or DAILY_FIELDS
        if isinstance(stocks, str):
            stocks = [stocks]

        result = {}
        missing = []
        if self.use_cache:
            for stock in stocks:
                df = self._cache.get(stock)
                if df is None:
                    missing.append(stock)
                    continue
                sub = df.loc[df.index <= end_date]
                if count and count > 0:
                    sub = sub.tail(count)
                if len(sub) > 0:
                    result[stock] = sub
            if not missing:
                return result
        else:
            missing = list(stocks)

        # 缓存未命中（或关闭缓存）时直接查 xtdata
        try:
            data = xtdata.get_market_data_ex(
                fields, missing,
                period='1d',
                end_time=end_date,
                count=count,
                dividend_type='none',
                fill_data=True,
            )
        except Exception as e:
            print(f'[数据] get_market_data_ex 失败: {e}')
            return result

        for stock in missing:
            df = data.get(stock)
            if df is None or len(df) == 0:
                continue
            df = df.copy()
            df.index = [str(i) for i in df.index]
            result[stock] = df
        return result

    def get_one(self, stock, end_date, count, fields=None):
        """单只标的的便捷查询，返回 DataFrame 或 None"""
        data = self.get_bars([stock], end_date, count, fields)
        df = data.get(stock)
        if df is None or len(df) == 0:
            return None
        return df

    # ---------- 合约信息（对应 C.get_instrument_detail / get_stock_name） ----------

    def get_detail(self, stock):
        if stock in self._detail_cache:
            return self._detail_cache[stock]
        try:
            detail = xtdata.get_instrument_detail(stock)
        except Exception:
            detail = None
        self._detail_cache[stock] = detail
        return detail

    def get_stock_name(self, stock):
        if stock in self._name_cache:
            return self._name_cache[stock]
        detail = self.get_detail(stock)
        name = ''
        if detail:
            name = detail.get('InstrumentName', '') or ''
        self._name_cache[stock] = name
        return name


# ============================================================
# 通用工具
# ============================================================

def get_limit_ratio(stock):
    """
    根据代码前缀确定涨跌停幅度：
      创业板(300/301)、科创板(688) -> 20%
      主板(60/00) -> 10%
    """
    code = stock.split('.')[0]
    if code.startswith(('300', '301', '688')):
        return 0.20
    return 0.10


def get_price_and_limits(stock, data, bar_date):
    """
    获取某股票当日开盘价、涨停价、跌停价、最低价。
    涨跌停比例按代码前缀动态确定。
    拿不到时统一返回 (0.0, 0.0, 0.0, 0.0)。
    """
    df = data.get_one(stock, bar_date, 1)
    if df is None:
        return 0.0, 0.0, 0.0, 0.0

    open_price = float(df['open'].iloc[-1])
    low_price = float(df['low'].iloc[-1])
    pre_close = float(df['preClose'].iloc[-1]) if 'preClose' in df.columns else open_price
    if pre_close <= 0:
        return open_price, 0.0, 0.0, low_price

    ratio = get_limit_ratio(stock)
    limit_up = round(pre_close * (1 + ratio), 2)
    limit_down = round(pre_close * (1 - ratio), 2)
    return open_price, limit_up, limit_down, low_price


def linear_regression(x, y):
    """numpy.polyfit 一元线性回归，返回 (slope, r_squared)"""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if len(x) < 2:
        return 0.0, 0.0

    slope, intercept = np.polyfit(x, y, 1)
    y_pred = slope * x + intercept
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return float(slope), float(r2)


def calc_momentum_score(close_prices):
    """
    动量分数 = 年化收益率 * |R2|
    对对数价格做线性回归
    """
    close_prices = np.asarray(close_prices, dtype=float)
    if len(close_prices) < 2 or np.any(close_prices <= 0):
        return None

    log_prices = np.log(close_prices)
    X = np.arange(len(log_prices), dtype=float)

    try:
        slope, r2 = linear_regression(X, log_prices)
    except Exception:
        return None

    if r2 <= 0:
        return 0.0

    annual_return = np.exp(slope * TRADING_DAYS_PER_YEAR) - 1
    return float(annual_return * abs(r2))


# ============================================================
# 步骤1：构建股票池
# ============================================================

def get_stock_pool(data, bar_date, base_pool):
    """
    用 bar_date（含）之前的数据过滤股票池，避免未来函数。
    过滤：停牌、ST、市值区间
    """
    if not base_pool:
        print('[get_stock_pool] 概念板块未获取到股票')
        return []

    quotes = data.get_bars(base_pool, bar_date, 2)

    result = []
    for stock in base_pool:
        df = quotes.get(stock)
        if df is None or len(df) < 1:
            continue

        # 停牌过滤：suspendFlag == 1
        if 'suspendFlag' in df.columns:
            try:
                if int(df['suspendFlag'].iloc[-1]) == 1:
                    continue
            except (TypeError, ValueError):
                pass

        last_close = float(df['close'].iloc[-1]) if 'close' in df.columns else 0.0
        if last_close <= 0:
            continue

        # ST 过滤 + 市值过滤
        detail = data.get_detail(stock)
        if not detail:
            continue

        stock_name = detail.get('InstrumentName', '') or ''
        if 'ST' in stock_name.upper():
            continue

        total_value = detail.get('TotalValue', 0) or 0
        if total_value <= 0:
            # 不同版本字段名不一致，依次尝试总股本字段
            total_shares = 0
            for key in ('TotalVolume', 'TotalVolumn', 'TotalShares'):
                total_shares = detail.get(key, 0) or 0
                if total_shares > 0:
                    break
            if total_shares > 0:
                total_value = total_shares * last_close
        if total_value > 0 and (total_value < MIN_MARKET_CAP or total_value > MAX_MARKET_CAP):
            continue

        result.append(stock)

    return result


def build_base_pool():
    """从概念板块取原始股票池（只取一次，后续每日再做行情过滤）"""
    pool_set = set()
    for sector_name in CONCEPT_SECTORS:
        try:
            stocks = xtdata.get_stock_list_in_sector(sector_name)
        except Exception as e:
            print(f'[build_base_pool] 板块 {sector_name} 获取失败: {e}')
            continue
        if not stocks:
            print(f'[build_base_pool] 板块 {sector_name} 为空')
            continue
        pool_set.update(stocks)

    # 代码前缀过滤（与原版一致，默认不剔除创业板/科创板）
    pool_list = []
    for stock in sorted(pool_set):
        code = stock.split('.')[0]
        # if code.startswith('300') or code.startswith('301'):
        #     continue
        # if code.startswith('688'):
        #     continue
        if not code[:1].isdigit():
            continue
        pool_list.append(stock)
    return pool_list


# ============================================================
# 步骤2：动量打分选股
# ============================================================

def get_rank(pool, data, bar_date):
    """
    动量打分：只用 bar_date 之前（不含当日）的收盘价，取第 1 名
    """
    if not pool:
        return None

    quotes = data.get_bars(pool, bar_date, LOOKBACK_DAYS + 2)

    scores = {}
    for stock in pool:
        df = quotes.get(stock)
        if df is None or len(df) < LOOKBACK_DAYS + 1:
            continue

        close_prices = df['close'].values
        # 排除当前 bar（最后 1 根），使用其前 LOOKBACK_DAYS 根
        hist = close_prices[-(LOOKBACK_DAYS + 1):-1]
        if len(hist) < LOOKBACK_DAYS or np.any(np.isnan(hist)):
            continue

        score = calc_momentum_score(hist)
        if score is not None:
            scores[stock] = score

    if not scores:
        return None

    sorted_stocks = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    print(f'[get_rank] Top3: {[(s, round(float(sc), 4)) for s, sc in sorted_stocks[:3]]}')
    return sorted_stocks[0][0]


# ============================================================
# 步骤3：计算近 5 日动量分数序列
# ============================================================

def rank_stock_change(stock, data, bar_date):
    """
    计算目标股票近 5 日（+ 最新 1 日）动量分数序列，全部排除当前 bar
    """
    result = {}

    df = data.get_one(stock, bar_date, LOOKBACK_DAYS + 5 + 2)
    if df is None or len(df) < LOOKBACK_DAYS + 2:
        return result

    close_prices = df['close'].values
    scores = []

    # 从远到近：d-5 ... d-1
    for i in range(5, 0, -1):
        window = close_prices[-(LOOKBACK_DAYS + 1 + i):-(i + 1)]
        if len(window) >= LOOKBACK_DAYS and not np.any(np.isnan(window)):
            score = calc_momentum_score(window)
            scores.append(score if score is not None else 0.0)
        else:
            scores.append(0.0)

    # 最新一天（d0，仍排除当前 bar）
    latest = close_prices[-(LOOKBACK_DAYS + 1):-1]
    if len(latest) >= LOOKBACK_DAYS and not np.any(np.isnan(latest)):
        score = calc_momentum_score(latest)
        scores.append(score if score is not None else 0.0)
    else:
        scores.append(0.0)

    result[stock] = scores
    return result


# ============================================================
# 步骤4：过滤候选股
# ============================================================

def filter_target(stock, data, bar_date):
    """剔除停牌、跌停（跌停幅度按代码前缀区分 10% / 20%）"""
    if stock is None:
        return None

    df = data.get_one(stock, bar_date, 1)
    if df is None:
        return None

    if 'suspendFlag' in df.columns:
        try:
            if int(df['suspendFlag'].iloc[-1]) == 1:
                print(f'[filter_target] {stock} 停牌中')
                return None
        except (TypeError, ValueError):
            pass

    last_close = float(df['close'].iloc[-1])
    pre_close = float(df['preClose'].iloc[-1]) if 'preClose' in df.columns else last_close
    if last_close <= 0:
        return None

    limit_down = round(pre_close * (1 - get_limit_ratio(stock)), 2)
    if last_close <= limit_down:
        print(f'[filter_target] {stock} 跌停 收盘:{last_close} 跌停价:{limit_down}')
        return None

    return stock


# ============================================================
# 步骤5：综合择时信号
# ============================================================

def get_timing_signal(stock, data, bar_date, stock_df):
    """
    RSRS 仅记录，不介入决策
    实际信号 = 动量分数是否连续下降 >= 2 天
    """
    rsrs = calc_rsrs(data, bar_date)
    if rsrs is not None:
        print(f'[择时] RSRS修正标准分: {rsrs:.4f}')
    else:
        print('[择时] RSRS 数据不足')

    scores = stock_df.get(stock, [])
    if len(scores) < 1:
        return 'KEEP'

    sig = 0
    for i in range(len(scores) - 1, 0, -1):
        if scores[i] < scores[i - 1]:
            sig += 1
        else:
            break

    print(f'[择时] 动量分数序列: {[round(float(s), 4) for s in scores]}')
    print(f'[择时] 连续下降天数: {sig}')

    return 'SELL' if sig >= 2 else 'BUY'


def calc_rsrs(data, bar_date):
    """RSRS：N=21 根高低点回归取斜率，M=600 根算 zscore，再乘 R2"""
    df = data.get_one(RSRS_INDEX, bar_date, RSRS_M + RSRS_N + 2)
    if df is None or len(df) < RSRS_M + RSRS_N + 1:
        return None

    # 排除当前 bar
    highs = df['high'].values[:-1]
    lows = df['low'].values[:-1]

    betas = []
    r2_list = []
    for i in range(RSRS_N - 1, len(highs)):
        h = highs[i - RSRS_N + 1:i + 1]
        l = lows[i - RSRS_N + 1:i + 1]
        if len(h) < RSRS_N or np.any(np.isnan(h)) or np.any(np.isnan(l)):
            continue
        try:
            slope, r2 = linear_regression(l, h)
        except Exception:
            continue
        betas.append(slope)
        r2_list.append(r2)

    if len(betas) < RSRS_M:
        return None

    recent_betas = betas[-RSRS_M:]
    mean_beta = np.mean(recent_betas)
    std_beta = np.std(recent_betas)
    if std_beta == 0:
        return 0.0

    zscore = (recent_betas[-1] - mean_beta) / std_beta
    recent_r2 = r2_list[-1] if r2_list else 0
    return zscore * recent_r2


# ============================================================
# 步骤6：执行调仓
# ============================================================

def adjust_position(stock, signal, data, bar_date, account):
    """
    SELL     -> 清仓
    BUY/KEEP -> 持仓已是目标股则持有，否则先卖旧再买新
    成交价统一取当日开盘价（对应原版 passorder 指定价单）
    """
    current_holdings = account.get_holdings_can_use()
    print(f'[调仓] 当前持仓(可用): {current_holdings}')

    if signal == 'SELL':
        for s, vol in list(current_holdings.items()):
            px_df = data.get_one(s, bar_date, 1)
            if px_df is None:
                print(f'[调仓] 无法获取 {s} 价格，卖出跳过')
                continue
            price = float(px_df['open'].iloc[-1])
            msg = f'SELL信号 清仓 {s}'
            print(f'[调仓] {msg}')
            account.sell(bar_date, s, price, vol, msg)
        return

    # BUY / KEEP：已持有目标股则继续持有
    if stock in current_holdings and current_holdings[stock] > 0:
        print(f'[调仓] KEEP: 继续持有 {stock}')
        return

    # 换仓：先卖旧
    for s, vol in list(current_holdings.items()):
        if s == stock:
            continue
        px_df = data.get_one(s, bar_date, 1)
        if px_df is None:
            print(f'[调仓] 无法获取 {s} 价格，卖出跳过')
            continue
        price = float(px_df['open'].iloc[-1])
        msg = f'切换标的 卖出 {s}'
        print(f'[调仓] {msg}')
        account.sell(bar_date, s, price, vol, msg)

    # 再买新
    current_price, limit_up, limit_down, low_price = get_price_and_limits(stock, data, bar_date)
    print(f'[调仓] {stock} 开盘:{current_price:.2f} 涨停:{limit_up:.2f} 跌停:{limit_down:.2f} 最低:{low_price:.2f}')

    if current_price <= 0:
        print(f'[调仓] {stock} 价格异常: {current_price}')
        return

    if limit_up > 0 and low_price >= limit_up:
        print('[调仓] 开盘一字涨停，无法买入')
        return

    available_cash = account.cash
    # 预留手续费，避免因佣金导致资金不足
    buy_vol = int(available_cash / (current_price * (1 + COMMISSION_RATE)) / 100) * 100
    if buy_vol < 100:
        print(f'[调仓] 资金不足买 1 手，可用:{available_cash:.2f} 股价:{current_price}')
        return

    msg = f'BUY信号 买入 {stock} {buy_vol}股'
    print(f'[调仓] {msg}')
    account.buy(bar_date, stock, current_price, buy_vol, msg)


# ============================================================
# 止损检查（原 14:50 check_lose，回测用当日收盘价）
# ============================================================

def check_lose(data, bar_date, account):
    for pos in account.get_positions():
        stock = pos.stock
        cost_price = pos.open_price
        volume = pos.can_use
        if volume <= 0 or cost_price <= 0:
            continue

        df = data.get_one(stock, bar_date, 1)
        if df is None:
            continue
        current_price = float(df['close'].iloc[-1])
        if current_price <= 0:
            continue

        profit_ratio = (current_price - cost_price) / cost_price
        print(f'[止损检查] {stock} {data.get_stock_name(stock)} '
              f'成本:{cost_price:.2f} 收盘:{current_price:.2f} 盈亏:{profit_ratio:.2%}')

        if profit_ratio <= STOP_LOSS_RATIO:
            print(f'[止损检查] {stock} 触发硬止损！盈亏 {profit_ratio:.2%} <= {STOP_LOSS_RATIO:.0%}，强制清仓')
            account.sell(bar_date, stock, current_price, volume, f'硬止损平仓 {stock}')


# ============================================================
# 复盘打印（原 15:05 print_trade_info）
# ============================================================

def print_trade_info(data, bar_date, account):
    today_deals = [d for d in account.deals if d.date == bar_date]
    if today_deals:
        print(f'--- 今日成交 ({len(today_deals)} 笔) ---')
        for deal in today_deals:
            print(f'  {deal.stock} {"买入" if deal.direction == 1 else "卖出"} '
                  f'价格:{deal.price:.2f} 数量:{deal.volume}')

    price_map = {}
    for pos in account.get_positions():
        df = data.get_one(pos.stock, bar_date, 1)
        current_price = float(df['close'].iloc[-1]) if df is not None else 0.0
        price_map[pos.stock] = current_price
        market_value = current_price * pos.volume
        profit_ratio = (current_price - pos.open_price) / pos.open_price if pos.open_price > 0 else 0
        print(f'  持仓: {pos.stock} {data.get_stock_name(pos.stock)} '
              f'成本:{pos.open_price:.2f} 收盘:{current_price:.2f} '
              f'盈亏:{profit_ratio:.2%} 市值:{market_value:.0f}')

    total = account.total_asset(price_map)
    print(f'  资金: 可用={account.cash:.0f} 总资产={total:.0f}')
    return total


# ============================================================
# 单个交易日流程（对应原版 handlebar）
# ============================================================

def run_one_day(data, bar_date, account, base_pool, state):
    print('\n' + '=' * 60)
    print(f'[回测] 交易日: {bar_date}')
    print('=' * 60)

    account.settle_open()   # T+1 解冻

    # 步骤1：构建股票池
    pool = get_stock_pool(data, bar_date, base_pool)
    if not pool:
        print('[回测] 股票池为空，跳过今日')
        return print_trade_info(data, bar_date, account)
    print(f'[回测] 步骤1 - 股票池: {len(pool)} 只')

    # 步骤2：动量打分选股，取第 1 名
    target_stock = get_rank(pool, data, bar_date)
    if target_stock is None:
        print('[回测] 步骤2 - 未选出目标股票')
        return print_trade_info(data, bar_date, account)
    print(f'[回测] 步骤2 - 目标: {target_stock} {data.get_stock_name(target_stock)}')

    # 步骤3：近 5 日动量分数序列
    state['stock_df'] = rank_stock_change(target_stock, data, bar_date)
    scores = state['stock_df'].get(target_stock, [])
    print(f'[回测] 步骤3 - 近5日动量分数: {[round(float(s), 4) for s in scores]}')

    # 步骤4：过滤候选股
    target_stock = filter_target(target_stock, data, bar_date)
    if target_stock is None:
        print('[回测] 步骤4 - 目标股票被过滤')
        return print_trade_info(data, bar_date, account)
    state['today_target'] = target_stock
    print(f'[回测] 步骤4 - 过滤通过: {target_stock}')

    # 步骤5：择时信号
    signal = get_timing_signal(target_stock, data, bar_date, state['stock_df'])
    print(f'[回测] 步骤5 - 择时信号: {signal}')

    # 步骤6：调仓
    adjust_position(target_stock, signal, data, bar_date, account)
    print('[回测] 步骤6 - 调仓执行完毕')

    # 止损检查（收盘价）
    check_lose(data, bar_date, account)

    # 复盘
    return print_trade_info(data, bar_date, account)


# ============================================================
# 回测主流程
# ============================================================

def get_trading_days(start_date, end_date, extra_before=0):
    """
    返回 (回测交易日列表, 含预热的起始日期)
    extra_before：在 start_date 之前额外向前取多少个交易日作为预热
    """
    timetags = xtdata.get_trading_dates('SH', start_time='', end_time=end_date, count=-1)
    all_days = [xtdata.timetag_to_datetime(t, '%Y%m%d') for t in timetags]
    all_days = [d for d in all_days if d <= end_date]
    if not all_days:
        raise RuntimeError('未取到交易日历，请检查 QMT 客户端是否已启动并下载过数据')

    run_days = [d for d in all_days if d >= start_date]
    if not run_days:
        raise RuntimeError(f'区间 {start_date} ~ {end_date} 内没有交易日')

    first_idx = all_days.index(run_days[0])
    warm_idx = max(0, first_idx - extra_before)
    return run_days, all_days[warm_idx]


def download_data(stocks, start_date, end_date):
    print(f'[下载] 开始补下载日线数据: {len(stocks)} 只 ...')
    try:
        xtdata.download_history_data2(stocks, period='1d',
                                      start_time=start_date, end_time=end_date)
    except AttributeError:
        for i, stock in enumerate(stocks, 1):
            xtdata.download_history_data(stock, period='1d',
                                         start_time=start_date, end_time=end_date)
            if i % 200 == 0:
                print(f'[下载] {i}/{len(stocks)}')
    print('[下载] 完成')


def report(account, equity_curve):
    print('\n' + '=' * 60)
    print('[回测结果]')
    print('=' * 60)
    if not equity_curve:
        print('无有效交易日')
        return

    dates = [d for d, _ in equity_curve]
    values = np.array([v for _, v in equity_curve], dtype=float)

    total_return = values[-1] / account.init_cash - 1
    days = len(values)
    years = days / TRADING_DAYS_PER_YEAR
    annual = (values[-1] / account.init_cash) ** (1 / years) - 1 if years > 0 and values[-1] > 0 else 0.0

    peak = np.maximum.accumulate(values)
    drawdown = values / peak - 1
    max_dd = drawdown.min() if len(drawdown) else 0.0
    max_dd_date = dates[int(drawdown.argmin())] if len(drawdown) else ''

    rets = np.diff(values) / values[:-1] if len(values) > 1 else np.array([])
    if len(rets) > 1 and rets.std() > 0:
        sharpe = rets.mean() / rets.std() * math.sqrt(TRADING_DAYS_PER_YEAR)
    else:
        sharpe = 0.0

    buy_cnt = len([d for d in account.deals if d.direction == 1])
    sell_cnt = len([d for d in account.deals if d.direction == -1])
    total_fee = sum(d.fee for d in account.deals)

    print(f'  区间: {dates[0]} ~ {dates[-1]}  共 {days} 个交易日')
    print(f'  初始资金: {account.init_cash:,.0f}')
    print(f'  期末资产: {values[-1]:,.0f}')
    print(f'  总收益率: {total_return:.2%}')
    print(f'  年化收益: {annual:.2%}')
    print(f'  最大回撤: {max_dd:.2%} (于 {max_dd_date})')
    print(f'  夏普比率: {sharpe:.2f}')
    print(f'  成交笔数: 买入 {buy_cnt} / 卖出 {sell_cnt}，总费用 {total_fee:,.0f}')


def run_backtest(start_date, end_date, init_cash=200000.0,
                 use_cache=True, do_download=False, save_csv=''):
    print('[动量择时策略-xtdata版] 初始化 ...')
    print(f'  回测区间: {start_date} ~ {end_date}, 初始资金: {init_cash:,.0f}')
    print(f'  概念板块数: {len(CONCEPT_SECTORS)}')
    print(f'  动量回看: {LOOKBACK_DAYS}天, 止损线: {STOP_LOSS_RATIO:.0%}')

    # 交易日历（额外向前取足够多的预热日，供 RSRS 使用）
    warm_need = max(WARMUP_DAYS, RSRS_M + RSRS_N + 10)
    run_days, warm_start = get_trading_days(start_date, end_date, extra_before=warm_need)
    print(f'  交易日: {len(run_days)} 个, 数据预热起点: {warm_start}')

    base_pool = build_base_pool()
    print(f'  原始股票池: {len(base_pool)} 只')
    if not base_pool:
        print('股票池为空，请检查板块名称或 QMT 数据')
        return None

    if do_download:
        download_data(base_pool + [RSRS_INDEX], warm_start, end_date)

    data = BacktestData(use_cache=use_cache)
    # 个股只需 LOOKBACK 级别的预热；指数需要 RSRS 的长历史，单独加载
    stock_warm_start = get_trading_days(start_date, end_date, extra_before=WARMUP_DAYS + 5)[1]
    data.preload(base_pool, stock_warm_start, end_date)
    data.preload([RSRS_INDEX], warm_start, end_date)

    account = SimAccount(init_cash)
    state = {'stock_df': {}, 'today_target': None}

    for bar_date in run_days:
        total = run_one_day(data, bar_date, account, base_pool, state)
        account.equity_curve.append((bar_date, total))

    report(account, account.equity_curve)

    if save_csv:
        df = pd.DataFrame(account.equity_curve, columns=['date', 'total_asset'])
        df.to_csv(save_csv, index=False, encoding='utf-8-sig')
        print(f'[回测] 净值曲线已保存: {save_csv}')

    return account


def main():
    parser = argparse.ArgumentParser(description='动量择时策略 xtdata 回测')
    parser.add_argument('--start', default='20240101', help='回测开始日期 YYYYMMDD')
    parser.add_argument('--end', default=datetime.now().strftime('%Y%m%d'), help='回测结束日期 YYYYMMDD')
    parser.add_argument('--cash', type=float, default=200000.0, help='初始资金')
    parser.add_argument('--no-cache', action='store_true', help='关闭行情内存缓存，逐日直接查询 xtdata')
    parser.add_argument('--download', action='store_true', help='回测前先补下载本地日线数据')
    parser.add_argument('--save-csv', default='', help='净值曲线输出 csv 路径')
    args = parser.parse_args()

    run_backtest(args.start, args.end, args.cash,
                 use_cache=not args.no_cache,
                 do_download=args.download,
                 save_csv=args.save_csv)


if __name__ == '__main__':
    main()
