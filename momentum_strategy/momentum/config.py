# coding: utf-8
"""策略参数。数值全部取自原 QMT 回测脚本 dl_strategy.py，未做调整。"""

from dataclasses import dataclass, field
from typing import Tuple

# ------------------------------------------------------------
# 概念板块列表（原脚本中的完整列表）
# ------------------------------------------------------------
CONCEPT_SECTORS_FULL: Tuple[str, ...] = (
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
)

# 原脚本最后一行把板块覆盖成全市场：CONCEPT_SECTORS = ['沪深a股']
# xtdata 中板块名为 '沪深A股'（大写 A），这里沿用同一含义
CONCEPT_SECTORS_DEFAULT: Tuple[str, ...] = ('沪深A股',)

# 日线字段
DAILY_FIELDS = ('open', 'high', 'low', 'close', 'preClose', 'volume', 'suspendFlag')


@dataclass
class StrategyConfig:
    """选股 / 择时 / 风控参数"""

    # 股票池
    concept_sectors: Tuple[str, ...] = CONCEPT_SECTORS_DEFAULT
    min_market_cap: float = 30e8           # 市值下限
    max_market_cap: float = 500e8          # 市值上限
    exclude_st: bool = True
    # 原脚本中被注释掉的创业板 / 科创板剔除开关
    exclude_gem: bool = False              # 300 / 301
    exclude_star: bool = False             # 688

    # 动量打分
    lookback_days: int = 5                 # 原脚本 LOOKBACK_DAYS = 5（注释里另有 29）
    trading_days_per_year: int = 244

    # 动量分数序列长度（原脚本固定取 5，再补最新 1 个，共 6 个）
    score_series_len: int = 5

    # 择时：动量分数连续下降达到该天数则清仓
    decline_days_to_sell: int = 2
    # 判定「下降」的最小幅度。0.0 = 与原脚本一致的严格比较；
    # 分数几乎相等时（例如价格走势非常接近完美指数增长），
    # 严格比较会把 1e-13 级别的浮点噪声当成下降，可设一个相对容差规避。
    decline_epsilon: float = 0.0

    # RSRS（仅打印，不参与决策，与原脚本一致）
    rsrs_n: int = 21
    rsrs_m: int = 600
    rsrs_index: str = '000300.SH'
    rsrs_enabled: bool = True

    # 候选股跌停过滤。
    # 注意这是未来函数：下单在当日开盘，而跌停要用当日收盘价才能确认。
    # 默认关闭；置 True 可复现原脚本的口径，用于对比两种假设下的差别。
    filter_limit_down: bool = False

    # 风控
    stop_loss_ratio: float = -0.15         # 固定硬止损线

    # 预热：原脚本 bar_count < LOOKBACK_DAYS + 10 时不交易
    warmup_days: int = 0                   # 0 表示按 lookback_days + 10 自动计算

    def __post_init__(self):
        if self.warmup_days <= 0:
            self.warmup_days = self.lookback_days + 10

    @property
    def bars_needed_for_rank(self) -> int:
        """排序打分需要的 K 线根数（多取 1 根用于排除当前 bar）"""
        return self.lookback_days + 2

    @property
    def bars_needed_for_series(self) -> int:
        return self.lookback_days + self.score_series_len + 2

    @property
    def bars_needed_for_rsrs(self) -> int:
        return self.rsrs_m + self.rsrs_n + 2


@dataclass
class AccountConfig:
    """模拟账户参数"""

    init_cash: float = 200000.0

    # 交易费用（A 股现行规则）
    commission_rate: float = 1e-4          # 佣金万 1，买卖双边
    min_commission: float = 5.0            # 佣金单笔最低 5 元
    transfer_fee_rate: float = 1e-5        # 过户费千分之 0.01，买卖双边
    stamp_tax_rate: float = 5e-4           # 印花税千分之 0.5，仅卖出

    lot_size: int = 100                    # 一手股数
    t_plus_one: bool = True                # 当日买入次日才可卖

    @property
    def buy_cost_rate(self) -> float:
        """买入的比例费用合计（不含最低佣金）"""
        return self.commission_rate + self.transfer_fee_rate

    @property
    def sell_cost_rate(self) -> float:
        """卖出的比例费用合计（不含最低佣金）"""
        return self.commission_rate + self.transfer_fee_rate + self.stamp_tax_rate


@dataclass
class BacktestConfig:
    """回测运行参数"""

    start_date: str = '20240101'
    end_date: str = '20241231'
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    account: AccountConfig = field(default_factory=AccountConfig)
