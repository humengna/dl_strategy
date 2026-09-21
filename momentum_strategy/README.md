# momentum-strategy

A股动量择时策略。逻辑与参数取自 QMT 回测脚本 `dl_strategy.py`，重构为分层的
独立 Python 项目：行情走 `xtquant.xtdata`，交易由内置模拟账户撮合，可直接
`python run_backtest.py` 运行。

## 策略逻辑

每个交易日按以下顺序执行（与原脚本 `handlebar` 一致）：

| 步骤 | 内容 | 模块 |
|---|---|---|
| 1 | 概念板块取池，过滤停牌 / ST / 市值区间 | `universe.py` |
| 2 | 对数收盘价线性回归打分（年化收益 × \|R²\|），取第 1 名 | `selector.py` |
| 3 | 计算目标股近 5 日动量分数序列 | `selector.py` |
| 4 | 过滤跌停、停牌的候选股 | `selector.py` |
| 5 | 择时：动量分数连续下降 ≥ 2 天 → SELL，否则 BUY / KEEP | `timing.py` |
| 6 | 调仓：按当日开盘价换仓，满仓单票 | `engine.py` |
| 7 | 止损：收盘价相对成本跌破 -15% 强制清仓 | `engine.py` |

RSRS 修正标准分（N=21, M=600, 沪深300）**只计算和打印，不参与决策**，与原脚本一致。

所有打分取数都多取 1 根 K 线并用 `[-(n+1):-1]` 切片排除当日，不含未来函数。

## 参数

全部集中在 `momentum/config.py`，数值与原脚本一一对应：

| 参数 | 默认值 | 原脚本 |
|---|---|---|
| `concept_sectors` | `('沪深A股',)` | `CONCEPT_SECTORS=['沪深a股']`（完整概念列表保留为 `CONCEPT_SECTORS_FULL`） |
| `min_market_cap` / `max_market_cap` | 30亿 / 500亿 | `MIN_MARKET_CAP` / `MAX_MARKET_CAP` |
| `lookback_days` | 5 | `LOOKBACK_DAYS = 5` |
| `trading_days_per_year` | 244 | `TRADING_DAYS_PER_YEAR` |
| `rsrs_n` / `rsrs_m` | 21 / 600 | `RSRS_N` / `RSRS_M` |
| `stop_loss_ratio` | -0.15 | `STOP_LOSS_RATIO` |
| `decline_days_to_sell` | 2 | `sig >= 2` |
| `warmup_days` | 15 | `bar_count < LOOKBACK_DAYS + 10` |

账户参数在 `AccountConfig`：初始资金、佣金万 2.5（单笔最低 5 元）、印花税万 5、
一手 100 股、T+1。

## 安装

```bash
pip install -r requirements.txt        # numpy / pandas
pip install -r requirements-dev.txt    # 加 pytest
```

`xtquant` 随迅投 QMT / 投研端安装，不在 PyPI 上；只有用 `--source xtdata`
（默认）时才需要，且需要本机 QMT 客户端已启动登录。

## 运行

```bash
# 真实数据（Windows + QMT）
python run_backtest.py --start 20240101 --end 20241231 --cash 200000
python run_backtest.py --start 20240101 --end 20241231 --download     # 先补下载日线
python run_backtest.py --start 20240101 --end 20241231 --equity-csv equity.csv --deals-csv deals.csv

# 离线试跑（任何平台，不需要 QMT）
python tools/make_sample_data.py --out data/sample --days 700
python run_backtest.py --source csv --data-dir data/sample --start 20240102 --end 20240630
```

常用参数：`--lookback` 回看天数、`--stop-loss` 止损线、`--decline-days` 连续下降
清仓天数、`--min-cap/--max-cap` 市值区间、`--sectors` 板块（逗号分隔）、
`--no-rsrs` 跳过 RSRS、`--no-cache` 关闭行情内存缓存、`-q` 只输出最终统计。

## 项目结构

```
momentum_strategy/
├── momentum/
│   ├── config.py       # 全部参数（StrategyConfig / AccountConfig / BacktestConfig）
│   ├── indicators.py   # 线性回归、动量分数、RSRS、涨跌停幅度
│   ├── datasource.py   # DataSource 接口 + XtDataSource（xtdata）+ CsvDataSource（离线）
│   ├── universe.py     # 步骤 1：股票池
│   ├── selector.py     # 步骤 2~4：打分、分数序列、候选过滤
│   ├── timing.py       # 步骤 5：择时信号、RSRS
│   ├── broker.py       # 模拟账户：T+1、佣金、印花税、成交流水
│   ├── engine.py       # 回测引擎：交易日循环、调仓、止损、复盘
│   ├── report.py       # 绩效统计与 csv 输出
│   ├── sample_data.py  # 合成样例行情
│   └── cli.py          # 命令行入口
├── tools/make_sample_data.py
├── tests/              # pytest，不依赖 QMT
└── run_backtest.py
```

数据源是个接口，策略层只依赖 `DataSource`。换数据源（Tushare、本地数据库等）
只要实现 `get_sector_stocks / get_trading_dates / get_bars / get_detail` 四个方法。

## 测试

```bash
python -m pytest
```

67 个用例，全部基于合成数据，不需要 QMT 环境。覆盖指标计算、打分排序（含
「当日 K 线不参与打分」的未来函数检查）、股票池过滤、择时信号、T+1 与费用、
调仓与止损、绩效统计。

## 与原 QMT 脚本的差异

1. **驱动方式**：`handlebar` 逐 K 线回调 → 交易日列表循环
2. **数据**：`C.get_market_data_ex` 等 → `xtdata` 同名接口，并加了一层内存缓存
3. **交易**：`passorder` / `get_trade_detail_data` → `SimAccount` 模拟撮合
4. **跌停判断**：原脚本 `filter_target` 固定按 10% 算跌停价，创业板/科创板会误杀；
   这里默认按代码前缀区分 10%/20%，`dynamic_limit_down=False` 可回到原行为
5. **买入数量**：预留佣金，避免满仓下单因手续费不足失败
6. **连续下降判定**：默认仍是严格比较 `<`；分数几乎相等时浮点噪声会被误判为
   下降，可用 `decline_epsilon` 设相对容差

## 注意事项

- 止损用当日收盘价判断，且受 T+1 约束：当日买入的股票当天无法止损，与原脚本
  依赖 `m_nCanUseVolume` 的行为一致
- 市值过滤用的是 `get_instrument_detail` 的当前值，历史回测存在轻微前视偏差
- 模拟撮合按给定价格全额成交，不含滑点和冲击成本，实盘结果会更差
