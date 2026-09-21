# dl_strategy

动量择时 A 股策略，两种形态：

| 目录 / 文件 | 说明 |
|---|---|
| [`momentum_strategy/`](momentum_strategy/) | **推荐**。分层的独立项目：配置 / 指标 / 数据源 / 选股 / 择时 / 撮合 / 引擎 / 统计分离，带 159 个单元测试，可用合成数据离线跑通 |
| `dl_strategy_xtdata.py` | 单文件版，**由 `momentum_strategy/tools/build_standalone.py` 自动生成**，适合直接丢进 QMT 目录。改逻辑请改包里的模块再重新生成 |

两者逻辑和参数一致，都源自 QMT 回测脚本 `dl_strategy.py`。

## 单文件版（`dl_strategy_xtdata.py`）

原版跑在 QMT 客户端内置回测里（`init` / `handlebar` / `passorder`），本版改成
可以直接 `python` 运行的独立脚本：行情走 `xtquant.xtdata`，交易由脚本内的
`SimAccount` 模拟撮合。文件由包自动生成，功能与项目版完全一致（含向量化引擎和
结果保存），测试里会比对两者输出逐字节相同。

## 环境

- Windows + 迅投 QMT / 投研端已启动并登录（xtdata 需要连本地行情服务）
- `pip install numpy pandas`，`xtquant` 用 QMT 安装目录自带的版本

## 运行

```bash
python dl_strategy_xtdata.py --start 20240101 --end 20241231 --cash 200000
python dl_strategy_xtdata.py --start 20240101 --end 20241231 --download   # 先补下载本地日线
python dl_strategy_xtdata.py --start 20240101 --end 20241231 --save-csv equity.csv
python dl_strategy_xtdata.py --start 20240101 --end 20241231 --no-cache   # 关闭内存缓存
```

## 策略流程（每个交易日）

1. 概念板块取池 → 停牌 / ST / 市值(30亿~500亿) 过滤
2. 对数价格线性回归动量打分（年化收益 × |R²|），取第 1 名
3. 计算目标股近 5 日动量分数序列
4. 跌停 / 停牌过滤
5. 择时：动量分数连续下降 ≥ 2 天 → SELL，否则 BUY/KEEP（RSRS 仅打印不参与决策）
6. 调仓（按当日开盘价成交），收盘价检查 -15% 硬止损

## 项目版（`momentum_strategy/`）

```bash
cd momentum_strategy
pip install -r requirements-dev.txt
python -m pytest                                      # 159 个用例，不需要 QMT

# 离线试跑（任何平台）
python tools/make_sample_data.py --out data/sample --days 700
python run_backtest.py --source csv --data-dir data/sample --start 20240102 --end 20240630

# 真实数据（Windows + QMT）
python run_backtest.py --start 20240101 --end 20241231 --cash 200000
```

详见 [momentum_strategy/README.md](momentum_strategy/README.md)。
