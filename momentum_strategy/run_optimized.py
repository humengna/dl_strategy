# coding: utf-8
"""
优化版回测入口 —— 等同于 run_backtest.py --optimized，命令行其余参数照常可用。

优化项及依据（实测 2020-2026，满仓单票）：
    算术日均 μ = +0.3756%/天，日波动 σ = 8.20%/天
    波动损耗 σ²/2 = 0.3363%/天，吃掉算术收益的 90%
    -> 几何日均只剩 +0.0394%，7 年净值 1.90，最大回撤 -96%

  1. 仓位系数 0.35    凯利最优 k*=μ/σ²≈0.56，左侧比右侧安全故取更保守值
  2. 5 只等权        组合方差 σ²(1/N+(1-1/N)ρ)，分散后波动大幅下降
  3. 择时盯持仓       原逻辑判断候选股，1627 天里 SELL 只占 53 天，形同虚设
  4. 回看 29 天       原脚本注释里的值；5 天是追一周爆发，是波动的主要来源
  5. 涨停按开盘价拦截  原用当日最低价，实际只拦全天封板，是最后一处未来函数

    python run_optimized.py --start 20200101 --end 20260918 --cash 1000000 -q

想逐项验证，用 run_backtest.py 单独开关：
    --max-positions / --position-ratio / --timing-on-holdings
    --lookback / --limit-up-check
"""

import sys

from momentum.cli import main

if __name__ == '__main__':
    sys.exit(main(['--optimized'] + sys.argv[1:]))
