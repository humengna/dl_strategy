# coding: utf-8
"""
模拟撮合账户。

xtdata 只提供行情，没有交易接口，所以原脚本里的
get_trade_detail_data / passorder 由这里替代：
  - T+1：当日买入次日才计入可用数量
  - 佣金按成交额收取，单笔有最低值；卖出额外收印花税
  - 以给定价格立即全额成交，不模拟盘口冲击和滑点
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import AccountConfig

log = logging.getLogger(__name__)

BUY = 1
SELL = -1


@dataclass
class Position:
    """持仓。open_price 对应 QMT 的 m_dOpenPrice，can_use 对应 m_nCanUseVolume"""

    stock: str
    volume: int
    open_price: float
    can_use: int = 0


@dataclass
class Deal:
    """成交记录。realized_pnl 仅卖出时有意义（已扣除本次费用）"""

    date: str
    stock: str
    direction: int
    price: float
    volume: int
    fee: float
    msg: str = ''
    realized_pnl: float = 0.0


@dataclass
class SimAccount:
    cfg: AccountConfig = field(default_factory=AccountConfig)
    cash: float = 0.0
    positions: Dict[str, Position] = field(default_factory=dict)
    deals: List[Deal] = field(default_factory=list)

    def __post_init__(self):
        if self.cash <= 0:
            self.cash = float(self.cfg.init_cash)

    # ---------- 查询 ----------

    @property
    def init_cash(self) -> float:
        return float(self.cfg.init_cash)

    def get_positions(self) -> List[Position]:
        return [p for p in self.positions.values() if p.volume > 0]

    def holdings_can_use(self) -> Dict[str, int]:
        """{股票: 可卖数量}"""
        return {s: p.can_use for s, p in self.positions.items() if p.can_use > 0}

    def settle_open(self) -> None:
        """每个交易日开盘前调用：解冻昨日买入（T+1）"""
        for pos in self.positions.values():
            pos.can_use = pos.volume

    def total_asset(self, price_map: Optional[Dict[str, float]] = None) -> float:
        price_map = price_map or {}
        market_value = 0.0
        for stock, pos in self.positions.items():
            price = price_map.get(stock) or pos.open_price
            market_value += price * pos.volume
        return self.cash + market_value

    # ---------- 费用 ----------

    def buy_fee(self, amount: float) -> float:
        return max(amount * self.cfg.commission_rate, self.cfg.min_commission)

    def sell_fee(self, amount: float) -> float:
        commission = max(amount * self.cfg.commission_rate, self.cfg.min_commission)
        return commission + amount * self.cfg.stamp_tax_rate

    def affordable_volume(self, price: float) -> int:
        """按可用资金算出能买的最大整手数量（已预留佣金）"""
        if price <= 0:
            return 0
        lot = self.cfg.lot_size
        raw = self.cash / (price * (1 + self.cfg.commission_rate))
        return int(raw / lot) * lot

    # ---------- 交易 ----------

    def buy(self, date: str, stock: str, price: float, volume: int, msg: str = '') -> bool:
        if volume <= 0 or price <= 0:
            return False

        amount = price * volume
        fee = self.buy_fee(amount)
        if amount + fee > self.cash + 1e-6:
            log.warning('资金不足，买入失败 %s 需要:%.2f 可用:%.2f', stock, amount + fee, self.cash)
            return False

        self.cash -= amount + fee
        pos = self.positions.get(stock)
        if pos is None:
            pos = Position(stock, volume, price)
            self.positions[stock] = pos
        else:
            pos.open_price = (pos.open_price * pos.volume + amount) / (pos.volume + volume)
            pos.volume += volume
        if not self.cfg.t_plus_one:
            pos.can_use = pos.volume

        self.deals.append(Deal(date, stock, BUY, price, volume, fee, msg))
        log.info('买入成交 %s 价格:%.2f 数量:%d 费用:%.2f 余额:%.2f',
                 stock, price, volume, fee, self.cash)
        return True

    def sell(self, date: str, stock: str, price: float, volume: int, msg: str = '') -> bool:
        pos = self.positions.get(stock)
        if pos is None or volume <= 0 or price <= 0:
            return False

        volume = min(volume, pos.can_use)
        if volume <= 0:
            log.info('%s 无可用数量，卖出跳过（T+1）', stock)
            return False

        amount = price * volume
        fee = self.sell_fee(amount)
        realized = (price - pos.open_price) * volume - fee

        self.cash += amount - fee
        pos.volume -= volume
        pos.can_use -= volume
        if pos.volume <= 0:
            del self.positions[stock]

        self.deals.append(Deal(date, stock, SELL, price, volume, fee, msg, realized))
        log.info('卖出成交 %s 价格:%.2f 数量:%d 费用:%.2f 盈亏:%.2f 余额:%.2f',
                 stock, price, volume, fee, realized, self.cash)
        return True

    def sell_all(self, date: str, stock: str, price: float, msg: str = '') -> bool:
        pos = self.positions.get(stock)
        if pos is None:
            return False
        return self.sell(date, stock, price, pos.can_use, msg)
