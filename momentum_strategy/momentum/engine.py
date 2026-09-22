# coding: utf-8
"""
回测引擎：把原脚本 handlebar 里的单日流程搬过来，改为按交易日列表循环驱动。

单个交易日的顺序（与原脚本一致）：
  ① 股票池 -> 动量打分取第 1 名 -> 分数序列 -> 跌停停牌过滤 -> 择时 -> 调仓（开盘价成交）
  ② 止损检查（收盘价，触发 -15% 则清仓）
  ③ 复盘打印并记录当日总资产
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .broker import SimAccount
from .config import BacktestConfig
from .datasource import DataSource
from .progress import Progress
from .selector import (blocked_by_limit_up, filter_target, is_suspended,
                       momentum_series, pick_target, pick_targets, price_and_limits)
from .timing import SIGNAL_BUY, SIGNAL_KEEP, SIGNAL_SELL, rsrs_value, timing_signal
from .universe import build_base_pool, filter_universe

log = logging.getLogger(__name__)


@dataclass
class DailyRecord:
    """每个交易日收盘后的快照，用于保存回测结果"""

    date: str
    target: str = ''          # 当日选出的目标股（多标的时为第一名）
    signal: str = ''          # 择时信号
    stock: str = ''           # 收盘持仓（多标的时为第一只）
    volume: int = 0
    cost: float = 0.0
    price: float = 0.0
    market_value: float = 0.0     # 全部持仓市值
    cash: float = 0.0
    total_asset: float = 0.0
    position_count: int = 0       # 收盘持仓只数
    holdings: str = ''            # 全部持仓，形如 '600000.SH:1000;000001.SZ:2000'


@dataclass
class BacktestResult:
    account: SimAccount
    equity_curve: List[Tuple[str, float]] = field(default_factory=list)
    signals: List[Tuple[str, str, Optional[str]]] = field(default_factory=list)  # (日期, 信号, 目标股)
    daily: List[DailyRecord] = field(default_factory=list)

    @property
    def dates(self) -> List[str]:
        return [d for d, _ in self.equity_curve]

    @property
    def values(self) -> List[float]:
        return [v for _, v in self.equity_curve]


class BacktestEngine:
    def __init__(self, source: DataSource, config: BacktestConfig,
                 account: Optional[SimAccount] = None):
        self.source = source
        self.config = config
        self.cfg = config.strategy
        self.account = account or SimAccount(config.account)
        self.base_pool: List[str] = []
        self.score_series: Dict[str, List[float]] = {}
        self.today_target: Optional[str] = None

    # ---------- 交易日历 ----------

    def trading_days(self) -> Tuple[List[str], str]:
        """返回 (回测交易日列表, 含预热的数据起点日期)"""
        all_days = self.source.get_trading_dates(self.config.end_date)
        if not all_days:
            raise RuntimeError('未取到交易日历，请检查数据源（QMT 是否已启动并下载过数据）')

        run_days = [d for d in all_days if d >= self.config.start_date]
        if not run_days:
            raise RuntimeError(f'区间 {self.config.start_date} ~ {self.config.end_date} 内没有交易日')

        first = all_days.index(run_days[0])
        warm_idx = max(0, first - self.cfg.warmup_days)
        return run_days, all_days[warm_idx]

    def _warm_start(self, extra_days: int) -> str:
        all_days = self.source.get_trading_dates(self.config.end_date)
        run_days = [d for d in all_days if d >= self.config.start_date]
        if not run_days:
            return self.config.start_date
        first = all_days.index(run_days[0])
        return all_days[max(0, first - extra_days)]

    # ---------- 主流程 ----------

    def prepare(self, download: bool = False) -> List[str]:
        self.base_pool = build_base_pool(self.source, self.cfg)
        log.info('原始股票池: %d 只', len(self.base_pool))
        if not self.base_pool:
            return []

        stock_start = self._warm_start(self.cfg.warmup_days + 5)
        index_start = self._warm_start(self.cfg.bars_needed_for_rsrs + 10)

        if download:
            self.source.download(list(self.base_pool) + [self.cfg.rsrs_index],
                                 index_start, self.config.end_date)

        self.source.preload(self.base_pool, stock_start, self.config.end_date)
        if self.cfg.rsrs_enabled:
            self.source.preload([self.cfg.rsrs_index], index_start, self.config.end_date)

        if not self.source.has_data(self.base_pool, self.config.end_date):
            raise RuntimeError(
                '数据源取不到任何日线数据，回测无法开始。\n'
                '  - 若上面提示「缺的是除权除息因子」，加 --download 补下载，\n'
                '    或先用 --dividend-type none 跑不复权\n'
                '  - 否则加 --download 补下载日线，或在 QMT 客户端「行情 -> 数据管理」补充\n'
                '  - 用 python run_backtest.py --check-data 逐步定位'
            )
        return self.base_pool

    def run(self, download: bool = False, show_progress: bool = False) -> BacktestResult:
        log.info('回测区间: %s ~ %s，初始资金: %.0f',
                 self.config.start_date, self.config.end_date, self.account.init_cash)
        log.info('板块数: %d，动量回看: %d 天，止损线: %.0f%%',
                 len(self.cfg.concept_sectors), self.cfg.lookback_days,
                 self.cfg.stop_loss_ratio * 100)

        run_days, _ = self.trading_days()
        log.info('交易日: %d 个', len(run_days))

        if not self.prepare(download=download):
            raise RuntimeError('股票池为空，请检查板块名称或数据源')

        result = BacktestResult(account=self.account)
        bar = Progress(len(run_days), prefix='[回测] 交易日', enabled=show_progress)
        # 原脚本是 bar_count < LOOKBACK_DAYS + 10 时 return，
        # 即前 warmup_days - 1 根 bar 不交易，第 warmup_days 根开始交易
        skip = (self.cfg.warmup_days - 1) if self.cfg.skip_warmup_bars else 0
        if skip:
            log.info('前 %d 个交易日为预热期，不交易', skip)

        for n, date in enumerate(run_days, 1):
            if n <= skip:
                self._last_signal = ''
                self.today_target = None
                total = self.review(date)
            else:
                total = self.run_day(date)
            result.equity_curve.append((date, total))
            result.signals.append((date, self._last_signal, self.today_target))
            result.daily.append(self.daily_record(date, total))
            bar.update(n, suffix=date)
        bar.close()
        return result

    def daily_record(self, date: str, total: float) -> DailyRecord:
        """收盘快照。策略最多持有 1 只，取第一只持仓即可"""
        record = DailyRecord(date=date, target=self.today_target or '',
                             signal=self._last_signal, cash=self.account.cash,
                             total_asset=total)
        positions = self.account.get_positions()
        record.position_count = len(positions)
        record.holdings = ';'.join(f'{p.stock}:{p.volume}' for p in positions)
        for i, pos in enumerate(positions):
            price = self._last_price_map.get(pos.stock, pos.open_price)
            record.market_value += price * pos.volume
            if i == 0:
                record.stock = pos.stock
                record.volume = pos.volume
                record.cost = pos.open_price
                record.price = price
        return record

    # ---------- 单个交易日 ----------

    _last_signal = ''
    _last_price_map: Dict[str, float] = {}

    def run_day(self, date: str) -> float:
        log.info('=' * 56)
        log.info('交易日: %s', date)

        self.account.settle_open()
        self._last_signal = ''
        self.today_target = None

        pool = filter_universe(self.source, self.base_pool, date, self.cfg)
        if not pool:
            log.info('股票池为空，跳过今日')
            return self.review(date)
        log.info('步骤1 - 股票池: %d 只', len(pool))

        picks = pick_targets(self.source, pool, date, self.cfg)
        if not picks:
            log.info('步骤2 - 未选出目标股票')
            return self.review(date)
        log.info('步骤2 - 目标: %s', [f'{p} {self.source.get_stock_name(p)}' for p in picks])

        self.score_series = {p: momentum_series(self.source, p, date, self.cfg)
                             for p in picks}
        log.info('步骤3 - 首选动量分数序列: %s',
                 [round(float(x), 4) for x in self.score_series.get(picks[0], [])])

        targets = [p for p in picks if filter_target(self.source, p, date, self.cfg)]
        if not targets:
            log.info('步骤4 - 目标股票全部被过滤')
            return self.review(date)
        self.today_target = targets[0]
        log.info('步骤4 - 过滤通过: %s', targets)

        rsrs = rsrs_value(self.source, date, self.cfg)
        log.info('RSRS修正标准分: %s', f'{rsrs:.4f}' if rsrs is not None else '数据不足')

        signal, to_sell = self.resolve_signals(date, targets)
        self._last_signal = signal
        log.info('步骤5 - 择时信号: %s%s', signal,
                 f'  待卖出: {sorted(to_sell)}' if to_sell else '')

        # 原策略口径下 SELL 意味着「清仓且当日不再买入」，保持该语义；
        # 其余情况统一走组合调仓（max_positions=1 时与原逻辑完全等价）
        if not self.cfg.timing_on_holdings and signal == SIGNAL_SELL:
            self.adjust_portfolio(date, [], to_sell=to_sell, allow_buy=False)
        else:
            keep = [t for t in targets if t not in to_sell]
            self.adjust_portfolio(date, keep, to_sell=to_sell)
        log.info('步骤6 - 调仓执行完毕')

        self.check_stop_loss(date)
        return self.review(date)

    def resolve_signals(self, date: str, targets: Sequence[str]):
        """
        返回 (用于记录的整体信号, 需要卖出的持仓集合)。

        timing_on_holdings=False 是原策略口径：只看「当天选出的候选股」的
        分数序列 —— 而候选股是当天分数最高的那只，序列几乎必然上升，
        所以 SELL 几乎不触发（实测 1627 天里只有 53 天）。
        timing_on_holdings=True 时逐只判断当前持仓，连续下降的才卖。
        """
        if not self.cfg.timing_on_holdings:
            scores = self.score_series.get(targets[0], [])
            signal = timing_signal(scores, self.cfg)
            holdings = set(self.account.holdings_can_use())
            return signal, (holdings if signal == SIGNAL_SELL else set())

        to_sell = set()
        for stock in self.account.holdings_can_use():
            series = momentum_series(self.source, stock, date, self.cfg)
            self.score_series[stock] = series
            if series and timing_signal(series, self.cfg) == SIGNAL_SELL:
                to_sell.add(stock)

        if to_sell:
            return SIGNAL_SELL, to_sell
        return (SIGNAL_BUY if targets else SIGNAL_KEEP), to_sell

    # ---------- 步骤 6：调仓 ----------

    def adjust_position(self, date: str, target: str, signal: str) -> None:
        """
        单标的调仓（原策略口径），保留为组合调仓的薄封装：
          SELL     -> 清仓，且当日不再买入
          BUY/KEEP -> 已持有目标股则持有，否则先卖旧再买新
        """
        if signal == SIGNAL_SELL:
            holdings = set(self.account.holdings_can_use())
            self.adjust_portfolio(date, [], to_sell=holdings, allow_buy=False)
            return
        self.adjust_portfolio(date, [target], to_sell=set())

    def adjust_portfolio(self, date: str, targets: Sequence[str],
                         to_sell: Optional[set] = None,
                         allow_buy: bool = True) -> None:
        """
        组合调仓：卖掉「不在目标里」或「被择时判定卖出」的持仓，
        再把目标补齐到 max_positions 只等权。

        每个仓位的额度 = 开盘时点总资产 × position_ratio / max_positions，
        额度在卖出之前算好，避免当天卖出的钱被重复计入额度。
        """
        to_sell = set(to_sell or ())
        targets = [t for t in targets if t]
        holdings = self.account.holdings_can_use()
        log.info('当前可用持仓: %s  目标: %s', holdings, list(targets))

        budget = self.slot_budget(date)

        for stock in list(holdings):
            if stock in to_sell:
                self._sell_at_open(date, stock, f'择时卖出 {stock}')
            elif stock not in targets:
                self._sell_at_open(date, stock, f'切换标的 卖出 {stock}')

        if not allow_buy:
            return

        held = set(self.account.positions)
        for target in targets:
            if target in held:
                log.info('KEEP: 继续持有 %s', target)
                continue
            self._buy_at_open(date, target, budget)

    def slot_budget(self, date: str) -> float:
        """单个仓位的资金额度（按开盘时点的总资产算）"""
        equity = self.account.cash
        for pos in self.account.get_positions():
            df = self.source.get_one(pos.stock, date, 1)
            price = float(df['open'].iloc[-1]) if df is not None else pos.open_price
            if not price > 0:
                price = pos.open_price
            equity += price * pos.volume
        return equity * self.cfg.position_ratio / self.cfg.max_positions

    def _buy_at_open(self, date: str, target: str, budget: float) -> None:
        open_price, limit_up, limit_down, low_price = price_and_limits(
            self.source, target, date)
        log.info('%s 开盘:%.2f 涨停:%.2f 跌停:%.2f 最低:%.2f',
                 target, open_price, limit_up, limit_down, low_price)

        if open_price <= 0:
            log.warning('%s 价格异常: %s', target, open_price)
            return

        if blocked_by_limit_up(self.cfg, open_price, low_price, limit_up):
            log.info('%s 涨停买不进', target)
            return

        volume = self.account.affordable_volume(open_price, budget)
        if volume < self.account.cfg.lot_size:
            log.info('资金不足买 1 手，可用:%.2f 额度:%.2f 股价:%.2f',
                     self.account.cash, budget, open_price)
            return

        self.account.buy(date, target, open_price, volume,
                         f'BUY信号 买入 {target} {volume}股')

    def _sell_at_open(self, date: str, stock: str, msg: str) -> None:
        # 停牌的票卖不掉，只能继续持有 —— 停牌日的 K 线是用前收填充的，
        # 照着它成交等于凭空按停牌前的价格脱手
        if not self.cfg.allow_sell_suspended and is_suspended(self.source, stock, date):
            log.info('%s 当日停牌，无法卖出，继续持有', stock)
            return

        df = self.source.get_one(stock, date, 1)
        if df is None:
            log.warning('无法获取 %s 价格，卖出跳过', stock)
            return
        self.account.sell_all(date, stock, float(df['open'].iloc[-1]), msg)

    # ---------- 止损 ----------

    def check_stop_loss(self, date: str) -> None:
        """用当日收盘价判断是否触发硬止损（原脚本 14:50 的盘中检查）"""
        for pos in self.account.get_positions():
            if pos.can_use <= 0 or pos.open_price <= 0:
                continue

            df = self.source.get_one(pos.stock, date, 1)
            if df is None:
                continue
            close = float(df['close'].iloc[-1])
            if close <= 0:
                continue

            if not self.cfg.allow_sell_suspended and is_suspended(self.source, pos.stock, date):
                log.info('%s 当日停牌，止损无法执行，继续持有', pos.stock)
                continue

            profit = (close - pos.open_price) / pos.open_price
            log.info('止损检查 %s %s 成本:%.2f 收盘:%.2f 盈亏:%.2f%%',
                     pos.stock, self.source.get_stock_name(pos.stock),
                     pos.open_price, close, profit * 100)

            if profit <= self.cfg.stop_loss_ratio:
                log.info('%s 触发硬止损（%.2f%% <= %.0f%%），强制清仓',
                         pos.stock, profit * 100, self.cfg.stop_loss_ratio * 100)
                self.account.sell_all(date, pos.stock, close, f'硬止损平仓 {pos.stock}')

    # ---------- 复盘 ----------

    def review(self, date: str) -> float:
        today_deals = [d for d in self.account.deals if d.date == date]
        if today_deals:
            log.info('今日成交 %d 笔:', len(today_deals))
            for deal in today_deals:
                log.info('  %s %s 价格:%.2f 数量:%d',
                         deal.stock, '买入' if deal.direction > 0 else '卖出',
                         deal.price, deal.volume)

        price_map: Dict[str, float] = {}
        for pos in self.account.get_positions():
            df = self.source.get_one(pos.stock, date, 1)
            close = float(df['close'].iloc[-1]) if df is not None else 0.0
            price_map[pos.stock] = close
            profit = (close - pos.open_price) / pos.open_price if pos.open_price > 0 else 0.0
            log.info('  持仓 %s %s 成本:%.2f 收盘:%.2f 盈亏:%.2f%% 市值:%.0f',
                     pos.stock, self.source.get_stock_name(pos.stock),
                     pos.open_price, close, profit * 100, close * pos.volume)

        self._last_price_map = price_map
        total = self.account.total_asset(price_map)
        log.info('  资金 可用:%.0f 总资产:%.0f', self.account.cash, total)
        return total
