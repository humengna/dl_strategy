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
| 4 | 过滤停牌的候选股（不判断跌停，见下方「未来函数」） | `selector.py` |
| 5 | 择时：动量分数连续下降 ≥ 2 天 → SELL，否则 BUY / KEEP | `timing.py` |
| 6 | 调仓：按当日开盘价换仓，满仓单票 | `engine.py` |
| 7 | 止损：收盘价相对成本跌破 -15% 强制清仓 | `engine.py` |

RSRS 修正标准分（N=21, M=600, 沪深300）**只计算和打印，不参与决策**，与原脚本一致。

所有打分取数都多取 1 根 K 线并用 `[-(n+1):-1]` 切片排除当日，不含未来函数。

### 未来函数

策略在**当日开盘**下单，所以任何决策都只能用开盘时点已知的信息：
昨日及更早的行情、开盘价、停牌状态。当日的收盘价、最高价、最低价都是未知的。

- 候选股过滤只保留停牌判断，**不判断跌停**（跌停要收盘价才能确认）
- 止损用当日收盘价，这是盘中 14:50 检查的近似，且受 T+1 约束（当日买入当天卖不掉）
- 仍在用当日数据的地方：买入前的「一字涨停」判断用了当日最低价
  （`low >= limit_up`，严格说应改成 `open >= limit_up`）；股票池的市值兜底
  估算用了当日收盘价。这两处影响较小，但同属未来函数

## 性能

选股环节是纯矩阵运算：股票池过滤、动量打分、RSRS 全部一次性算成
(交易日 × 标的) 的矩阵，而不是逐日逐股票切 DataFrame 再调 `np.polyfit`。
5000 只 × 250 个交易日原本要跑 125 万次回归，现在压成几次矩阵运算。

合成数据实测（260 个交易日）：

| 股票池 | 逐日引擎 | 向量引擎 | 提速 |
|---|---|---|---|
| 50 只 | 7.1 s | 0.19 s | 38× |
| 300 只 | 30.4 s | 0.43 s | 70× |

股票数越多差距越大，全市场（5000+ 只）差距在两个数量级以上。
两个引擎的下单、止损、复盘是同一份代码，测试里逐笔比对过结果完全相同
（`tests/test_vector_engine.py`）。`--engine loop` 可切回逐日引擎做交叉验证。

一处行为差异：面板按交易日历对齐，某只标的当日没有数据即视为当日不可交易；
逐日引擎会沿用它最近一根 K 线（已退市标的会一直参与打分）。行情完整时两者一致。

## 回测结果

每次回测默认写到 `results/bt_<起止日期>_<时间戳>/`（`--out-dir` 可指定，
`--no-save` 关闭）：

| 文件 | 内容 |
|---|---|
| `equity.csv` | 逐日净值：总资产、净值、当日收益、回撤、现金、市值，以及当天的目标股、择时信号、收盘持仓 |
| `deals.csv` | 每一笔成交：日期、代码、名称、方向、价格、数量、金额、费用、已实现盈亏、下单原因 |
| `trades.csv` | 先进先出配对后的完整交易：买卖日期与价格、持有天数、费用、盈亏、收益率、卖出原因 |
| `summary.json` | 绩效指标 + 本次回测用的全部参数（便于复现） |
| `summary.txt` | 绩效指标的可读版本 |

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

账户参数在 `AccountConfig`：

| 费用 | 默认值 | 收取方式 |
|---|---|---|
| 佣金 `commission_rate` | 1e-4（万 1） | 买卖双边，单笔最低 `min_commission` 5 元 |
| 过户费 `transfer_fee_rate` | 1e-5（千分之 0.01） | 买卖双边 |
| 印花税 `stamp_tax_rate` | 5e-4（千分之 0.5） | 仅卖出 |

成交额较大时往返成本 = 万1×2 + 千分之0.01×2 + 千分之0.5 = **0.072%**；
小额下单受最低佣金影响会更高（成交额 1 万时往返 0.152%）。
四个费率都能在命令行覆盖：`--commission`、`--min-commission`、`--transfer-fee`、`--stamp-tax`。

另有一手 100 股、T+1（当日买入次日才可卖）。

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
python run_backtest.py --start 20240101 --end 20241231 --download     # 先补下载日线（带进度条）
python run_backtest.py --start 20240101 --end 20241231 --equity-csv equity.csv --deals-csv deals.csv

# 离线试跑（任何平台，不需要 QMT）
python tools/make_sample_data.py --out data/sample --days 700
python run_backtest.py --source csv --data-dir data/sample --start 20240102 --end 20240630
```

下载和预加载都带进度条，终端里原地刷新，输出重定向到文件时按 10% 一档换行打印：

```
[数据] 开始下载日线: 5224 只，20240101 ~ 20241231
[数据] 下载 [#########---------------]  40.0% 2090/5224 已用 03:12 剩余 04:47 600519.SH
```

数据自检（取不到数据时先跑这个）：

```bash
python run_backtest.py --check-data              # 逐步定位哪一环取不到数
python run_backtest.py --check-data --download   # 顺便试下载一只标的
```

常用参数：`--lookback` 回看天数、`--stop-loss` 止损线、`--decline-days` 连续下降
清仓天数、`--min-cap/--max-cap` 市值区间、`--sectors` 板块（逗号分隔）、
`--no-rsrs` 跳过 RSRS、`--no-cache` 关闭行情内存缓存、`--engine loop` 切回逐日引擎、
`--out-dir` 指定结果目录、`--no-save` 不保存结果、`-q` 只输出进度条和最终统计，
以及上面那四个费率参数。

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
│   ├── panel.py        # 向量化行情面板与滚动回归核
│   ├── vector_engine.py# 向量化回测引擎（默认）
│   ├── sample_data.py  # 合成样例行情
│   ├── diagnostics.py  # xtdata 数据自检
│   ├── progress.py     # 终端进度条
│   └── cli.py          # 命令行入口
├── tools/
│   ├── make_sample_data.py   # 生成样例行情
│   └── build_standalone.py   # 把包打包成单文件 dl_strategy_xtdata.py
├── tests/              # pytest，不依赖 QMT
└── run_backtest.py
```

数据源是个接口，策略层只依赖 `DataSource`。换数据源（Tushare、本地数据库等）
只要实现 `get_sector_stocks / get_trading_dates / get_bars / get_detail` 四个方法。

## 测试

```bash
python -m pytest
```

121 个用例，全部基于合成数据，不需要 QMT 环境。覆盖指标计算、打分排序（含
「当日 K 线不参与打分」的未来函数检查）、股票池过滤、择时信号、T+1 与费用、
调仓与止损、绩效统计，以及 xtdata 取数行为（分批、字段退回、无数据报错，
用桩 xtquant 注入，不需要 QMT）、进度条渲染、
向量化指标与 polyfit 的数值一致性、两个引擎的逐笔结果一致性、结果文件落盘。

## 与原 QMT 脚本的差异

1. **驱动方式**：`handlebar` 逐 K 线回调 → 交易日列表循环
2. **数据**：`C.get_market_data_ex` 等 → `xtdata` 同名接口，并加了一层内存缓存
3. **交易**：`passorder` / `get_trade_detail_data` → `SimAccount` 模拟撮合
4. **跌停过滤已删除**：原脚本 `filter_target` 用当日收盘价判断候选股是否跌停，
   但下单发生在当日开盘，那一刻收盘价还不存在 —— 这是未来函数。现在只保留
   开盘前就已知的停牌过滤，以及开盘价有效性检查
5. **买入数量**：预留佣金，避免满仓下单因手续费不足失败
6. **连续下降判定**：默认仍是严格比较 `<`；分数几乎相等时浮点噪声会被误判为
   下降，可用 `decline_epsilon` 设相对容差

## 排错：预加载 0 只有数据

`[数据] 预加载完成，0 只有数据` 说明 xtdata 连上了但一根日线都没读到。
按顺序排查：

1. **本地没下载过日线**（最常见）。`get_trading_dates`、`get_stock_list_in_sector`
   不需要下载就能返回，所以它们正常不代表 K 线有数据。加 `--download` 重跑，
   或在 QMT 客户端「行情 → 数据管理 / 数据下载」里补充日线
2. **QMT 客户端没启动或没登录**。xtdata 只读本机客户端的数据缓存
3. **字段不被支持**。部分版本不支持 `suspendFlag`，整批请求会直接返回空表；
   预加载会自动探测并退回核心字段，日志里会提示
4. **单次请求标的过多**。全市场 5000+ 只一次性请求容易超时或静默返回空，
   预加载按 300 只一批切分（`PRELOAD_CHUNK_SIZE`）

`python run_backtest.py --check-data` 会把每一步的真实返回打出来，包括列名和
最后一行数据，直接看卡在哪。预加载为空时回测会立即报错退出，不会空转整段区间。

## 注意事项

- 止损用当日收盘价判断，且受 T+1 约束：当日买入的股票当天无法止损，与原脚本
  依赖 `m_nCanUseVolume` 的行为一致
- 市值过滤用的是 `get_instrument_detail` 的当前值，历史回测存在轻微前视偏差
- 模拟撮合按给定价格全额成交，不含滑点和冲击成本，实盘结果会更差

## 单文件版

仓库根目录的 `dl_strategy_xtdata.py` 由本项目自动生成，方便直接丢进 QMT 目录
或拷到别的机器：

```bash
python tools/build_standalone.py            # 重新生成
python tools/build_standalone.py --check    # 校验是否与包同步（测试里会跑）
```

改逻辑请改 `momentum/` 下的模块再重新生成，不要直接编辑单文件版。
