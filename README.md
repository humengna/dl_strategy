# dl_strategy

动量择时 A 股策略的 **xtdata 版**（`dl_strategy_xtdata.py`）。

原版跑在 QMT 客户端内置回测里（`init` / `handlebar` / `passorder`），本版改成
可以直接 `python` 运行的独立脚本：行情走 `xtquant.xtdata`，交易由脚本内的
`SimAccount` 模拟撮合。

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
