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
| 4 | 过滤停牌的候选股（跌停过滤可选，见下方「未来函数」） | `selector.py` |
| 5 | 择时：动量分数连续下降 ≥ 2 天 → SELL，否则 BUY / KEEP | `timing.py` |
| 6 | 调仓：按当日开盘价换仓，满仓单票 | `engine.py` |
| 7 | 止损：收盘价相对成本跌破 -15% 强制清仓 | `engine.py` |

RSRS 修正标准分（N=21, M=600, 沪深300）**只计算和打印，不参与决策**，与原脚本一致。

所有打分取数都多取 1 根 K 线并用 `[-(n+1):-1]` 切片排除当日，不含未来函数。

### 未来函数

策略在**当日开盘**下单，所以任何决策都只能用开盘时点已知的信息：
昨日及更早的行情、开盘价、停牌状态。当日的收盘价、最高价、最低价都是未知的。

- 候选股过滤默认只保留停牌判断，**不判断跌停**（跌停要收盘价才能确认）。
  `--filter-limit-down` / `filter_limit_down=True` 可打开跌停过滤，复现原脚本口径，
  跌停幅度按代码前缀区分 10%/20%
- 止损用当日收盘价，这是盘中 14:50 检查的近似，且受 T+1 约束（当日买入当天卖不掉）
- **停牌的票卖不掉**：停牌日 xtdata 会用前收把 K 线填满（开=高=低=收=前收、
  成交量为 0），照着它成交等于按停牌前的价格脱手。换仓、清仓、止损三条卖出路径
  都会先查停牌，停牌就继续持有，复牌当天再卖
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

每次回测默认写到 `results/<标签>_<时间戳>/`（`--out-dir` 可指定，`--no-save` 关闭）。
标签由起止日期、回看天数和非默认的关键参数拼成，不同参数的结果放一起也能一眼区分：

```
bt_20240101_20241231_lb29              回看 29 天，其余都是默认值
bt_20240101_20241231_lb15_dd1_ld       回看 15 天、连降 1 天清仓、打开跌停过滤
```

短标签含义：`lb` 回看天数、`dd` 连续下降清仓天数、`sl` 止损百分比、
`nocap` 关闭市值过滤、`ld` 打开跌停过滤、`norsrs` 跳过 RSRS、`div<方式>` 非默认复权。
目录里包含：

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
| `filter_market_cap` | True | 原脚本恒为 True，但因 bug 实际失效（见下） |
| `min_market_cap` / `max_market_cap` | 30亿 / 500亿 | `MIN_MARKET_CAP` / `MAX_MARKET_CAP` |
| `lookback_days` | 5 | `LOOKBACK_DAYS = 5`（原注释里另有 29） |
| `trading_days_per_year` | 244 | `TRADING_DAYS_PER_YEAR` |
| `rsrs_n` / `rsrs_m` | 21 / 600 | `RSRS_N` / `RSRS_M` |
| `stop_loss_ratio` | -0.15 | `STOP_LOSS_RATIO` |
| `filter_limit_down` | False | 原脚本恒为 True（未来函数，故默认关闭） |
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
python run_backtest.py --start 20240101 --end 20241231 --lookback 15 29   # 一次跑多组回看天数
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

`--lookback` 可以给多个值，依次回测并在最后打印横向对比表，各自的结果写进
以回看天数命名的独立目录：

```
  回看天数        期末资产     总收益       年化   最大回撤    夏普  交易笔数  卖出胜率
  5 天             249,595     24.80%     35.02%    -16.92%    1.29       145     47.2%
  15 天            162,416    -18.79%    -24.59%    -29.58%   -0.88        71     40.0%
  29 天            276,436     38.22%     55.08%     -7.42%    1.98        34     58.8%
```

常用参数：`--lookback` 回看天数（可多值）、`--stop-loss` 止损线、`--decline-days` 连续下降
清仓天数、`--min-cap/--max-cap` 市值区间、`--sectors` 板块（逗号分隔）、
`--no-rsrs` 跳过 RSRS、`--no-cap-filter` 关闭市值过滤、`--dividend-type` 复权方式、
`--no-cache` 关闭行情内存缓存、
`--engine loop` 切回逐日引擎、
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

183 个用例，全部基于合成数据，不需要 QMT 环境。覆盖指标计算、打分排序（含
「当日 K 线不参与打分」的未来函数检查）、股票池过滤、择时信号、T+1 与费用、
调仓与止损、绩效统计，以及 xtdata 取数行为（分批、字段退回、无数据报错，
用桩 xtquant 注入，不需要 QMT）、进度条渲染、
向量化指标与 polyfit 的数值一致性、两个引擎的逐笔结果一致性、结果文件落盘，
以及命令行的结果目录命名与多组参数回测。

## 复权

行情默认取**后复权**（`--dividend-type back`）。这不是可选的口径问题，不复权会算错：

动量分数是对数价格回归的斜率，再用 `exp(斜率 × 244)` 年化。除权跳空落在打分
窗口里时，不复权序列会把它当成真实下跌——而年化指数会把这个跳空指数级放大：

| 窗口内除权 | 复权（真实）分数 | 不复权分数 |
|---|---|---|
| 2% 现金分红 | 1002.9 | 222 ~ 362 |
| 5% 现金分红 | 1002.9 | 15 ~ 60 |
| 10送2 | 1002.9 | **-0.04 ~ -0.41** |
| 10送10 | 1002.9 | **-0.39 ~ -0.69** |

一次 2% 的分红就能让分数只剩 23%，送股直接把分数打成负数。由于选股是在全市场
取第一名，被压低的票会掉出榜首——实测里这正是主板股（分红频繁）被系统性漏掉、
7 月（除权高峰）选股一致率崩到 29% 的原因。

**后复权不含未来函数**：某日的复权因子只由该日之前的除权事件决定，且不会被
之后的分红改写。前复权以最新日为锚，历史价格会被未来的分红改写，严格说含未来
信息——不过对这个打分公式没有影响（两者在任一时点只差一个每股常数因子，对数
回归的斜率不变），只会通过绝对价位轻微影响手数计算。

一个已知的副作用：后复权会放大绝对价位（例如 99 元 → 224 元），一手的金额随之
变大，买入时的整手取整会更粗糙。收益率不受影响（整条序列等比缩放），但小账户
下可买的标的会变少。`--dividend-type none` 可以切回不复权与旧结果对照。

## 关于市值过滤

原脚本的市值过滤**实际上是失效的**：

```python
total_value = detail.get('TotalValue', 0)
if total_value <= 0:
    total_shares = detail.get('TotalShares', 0)
    if total_shares > 0 and last_close > 0:   # last_close 未定义 -> NameError
        ...
```

`get_instrument_detail` 在 QMT 回测环境里返回的 `TotalValue` 是 0，于是走进兜底分支
撞上未定义的 `last_close`，异常被外层 `except: pass` 吞掉，整只票直接放行。
实测 QMT 跑出来的股票池是 4959~5009 只，约等于全市场。

本项目修掉了这个 bug（`TotalValue` 缺失时依次尝试 `TotalVolume` / `TotalVolumn` /
`TotalShares` × 收盘价），所以市值过滤是真在工作的。想复现原脚本那种「全市场」
口径做对照，用 `--no-cap-filter`：

```bash
python run_backtest.py --start 20260105 --end 20260918 --no-cap-filter --cash 1000000
```

注意两种口径是**两个不同的策略**：一个在 30~500 亿的池子里选，一个在全市场选。

## 复刻原 QMT 脚本

本项目在改写过程中修掉了原脚本的若干缺陷，所以默认口径和原脚本跑出来的结果不同。
`--replicate-qmt` 可以一键切回原脚本的口径（**连同它的缺陷一起**），用于对照：

```bash
python run_backtest.py --start 20240101 --end 20241231 --replicate-qmt
```

| 项目 | 原 QMT 脚本 | 本项目默认 | 复刻开关 |
|---|---|---|---|
| 市值过滤 | 兜底分支引用未定义的 `last_close`，异常被吞 → 池子=全市场 | 过滤生效 | `--no-cap-filter` |
| 复权 | `dividend_type='none'` 不复权 | 后复权 | `--dividend-type none` |
| 跌停过滤 | 用当日收盘价、恒定 10% | 关闭（未来函数） | `--filter-limit-down --limit-down-ratio 0.10` |
| 停牌卖出 | 卖出端不查停牌，按前收填充价成交 | 停牌不能卖，复牌当天才卖 | `--allow-sell-suspended`（**不在预设内**） |
| 买入数量 | `int(可用资金/价格/100)*100`，不留费用 | 预留费用并逐手回退 | `--no-fee-reserve` |
| 预热 | `bar_count < LOOKBACK+10` 时 return | 有数据即交易 | `--skip-warmup` |

每个开关也能单独使用，混搭出中间口径。结果目录会带 `qmt` 标签，`summary.json`
里记录 `replicate_qmt: true` 与每一项的具体取值。

**停牌卖出这一条不在预设里**：原脚本按前收填充价把停牌股脱手，实盘根本做不到，
复刻模式下仍然是「停牌卖不掉、复牌当天才卖」。确实要逐笔对齐原脚本时再加
`--allow-sell-suspended`。

**复刻口径的结果偏乐观**，不要用它评估策略本身：停牌能按停牌前的价格脱手、
跌停过滤用到了当日收盘价，这两项都是实盘做不到的。

## 与原 QMT 脚本的差异

1. **驱动方式**：`handlebar` 逐 K 线回调 → 交易日列表循环
2. **数据**：`C.get_market_data_ex` 等 → `xtdata` 同名接口，并加了一层内存缓存
3. **交易**：`passorder` / `get_trade_detail_data` → `SimAccount` 模拟撮合
4. **跌停过滤改为可选且默认关闭**：原脚本 `filter_target` 用当日收盘价判断候选股
   是否跌停，但下单发生在当日开盘，那一刻收盘价还不存在 —— 这是未来函数。
   默认只做开盘前已知的停牌过滤和开盘价有效性检查；加 `--filter-limit-down`
   可切回原口径做对比
5. **买入数量**：预留佣金，避免满仓下单因手续费不足失败
6. **连续下降判定**：默认仍是严格比较 `<`；分数几乎相等时浮点噪声会被误判为
   下降，可用 `decline_epsilon` 设相对容差

## 排错：昨天能跑，今天取不到数

xtdata 启动时会打印一行数据路径：

```
数据路径：D:\...\data\datadir
```

**这一行要和昨天能跑通时的路径一致。** xtdata 连不上正在运行的 QMT / 投研端时，
会退回自己的本地目录；那个目录往往是空的，于是全部标的都返回空表，
表象和「没下载过数据」完全一样。先确认：

1. QMT / 投研端**当前**是否已启动并登录（重启电脑后最容易漏这一步）
2. 打印出来的数据路径，是否指向 QMT 的数据区，而不是项目里某个空目录
3. `python run_backtest.py --check-data` 会逐步打印每一环的真实返回

## 排错：复权价取不到

日线取得到、但加上 `--dividend-type back` 就返回空 —— 缺的是**除权除息因子**
（`divid_factors`）。它是独立于日线的一份数据，不随日线一起下载。

预加载会自动分辨这两种情况并打印对应提示。补数据：

```bash
python run_backtest.py --start ... --end ... --download   # 日线 + 除权除息因子一起下
```

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
