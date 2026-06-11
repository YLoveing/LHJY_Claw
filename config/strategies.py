"""
策略配置 — 单一事实来源
====================
所有多因子策略权重在此定义，回测和生产均从此导入。
这是保证 回测代码 = 生产代码 的关键约束。

打工马设计的三套策略:

A_稳健反转: 动量反转 + 大回撤抄底，偏保守
B_全面反转: 全方位反转（动量+RSI+波动率+回撤），偏激进  ← 最优
C_保守质量: 趋势质量型，低波动+均线多头，偏防御
"""

STRATEGIES = {
    "A_稳健反转": {
        "mom_3m": -0.50,
        "mom_1m": -0.40,
        "volatility_60d": -0.90,
        "max_dd_60d": -2.20,
        "price_ma60": 0.80,
        "volume_trend": 0.40,
        "close_low_20": 0.15,
    },
    "B_全面反转": {
        "mom_6m": -0.20,
        "mom_3m": -0.20,
        "mom_1m": -0.10,
        "rsi_14": -0.10,
        "volatility_60d": -0.10,
        "max_dd_60d": -0.10,
        "price_ma60": 0.08,
        "volume_ratio": -0.05,
        "volume_trend": -0.05,
        "close_low_20": 0.02,
    },
    "C_保守质量": {
        "volatility_60d": -0.25,
        "max_dd_60d": -0.15,
        "mom_3m": 0.15,
        "price_ma60": 0.15,
        "price_ma20": 0.10,
        "short_long_ma": 0.10,
        "volume_trend": 0.05,
        "close_low_20": 0.05,
    },
}

# 最优参数（Walk-forward 4折验证）
BEST_PARAMS = {
    "B_全面反转": {
        "top_n": 20,
        "rebalance_days": 42,  # ~2个月
        "slippage": 0.0015,
    },
}
