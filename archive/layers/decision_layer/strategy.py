"""
策略配置与选择 — 交易参数和策略简档。

职责：
- 定义策略配置数据结构
- 提供默认策略和策略选择逻辑
- 将策略参数注入评分/信号流程
"""

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class StrategyConfig:
    """策略配置 — 控制评分和交易行为的所有参数。

    Args:
        name: 策略名称
        bias_threshold: 乖离率阈值（%），超过此值提示不追高
        trend_strong_bias_multiplier: 强势趋势下乖离率阈值放宽倍数
        volume_shrink_ratio: 缩量判断阈值（当日量/5日均量，低于此值=缩量）
        volume_heavy_ratio: 放量判断阈值（高于此值=放量）
        ma_support_tolerance: MA 支撑判断容忍度（%）
        macd_fast: MACD 快线周期
        macd_slow: MACD 慢线周期
        macd_signal: MACD 信号线周期
        rsi_short: 短期 RSI 周期
        rsi_mid: 中期 RSI 周期
        rsi_long: 长期 RSI 周期
        rsi_overbought: RSI 超买阈值
        rsi_oversold: RSI 超卖阈值
        buy_threshold: 强烈买入阈值
        buy_soft_threshold: 买入阈值
        hold_threshold: 持有阈值
    """

    name: str = "main"

    # 乖离率
    bias_threshold: float = 5.0
    trend_strong_bias_multiplier: float = 1.5

    # 量能
    volume_shrink_ratio: float = 0.7
    volume_heavy_ratio: float = 1.5

    # 支撑
    ma_support_tolerance: float = 0.02

    # MACD 参数（标准 12/26/9）
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    # RSI 参数
    rsi_short: int = 6
    rsi_mid: int = 12
    rsi_long: int = 24
    rsi_overbought: int = 70
    rsi_oversold: int = 30

    # 信号阈值
    buy_threshold: int = 75  # ≥75 + 多头 = 强烈买入
    buy_soft_threshold: int = 60  # ≥60 + 多头/弱势多头 = 买入
    hold_threshold: int = 45  # ≥45 = 持有
    wait_threshold: int = 30  # ≥30 = 观望


# ═══════════════════════════════════════════
# 内置策略简档
# ═══════════════════════════════════════════


def default_strategy() -> StrategyConfig:
    """默认策略 — 标准趋势交易参数。"""
    return StrategyConfig(name="main")


def conservative_strategy() -> StrategyConfig:
    """保守策略 — 更严格的买入条件。

    - 乖离率阈值更小（不追高更严格）
    - 买入所需趋势强度更高
    """
    return StrategyConfig(
        name="conservative",
        bias_threshold=3.0,
        volume_shrink_ratio=0.6,
        buy_threshold=80,
        buy_soft_threshold=70,
    )


def aggressive_strategy() -> StrategyConfig:
    """激进策略 — 更强的趋势容忍度。

    - 乖离率阈值更大
    - 允许在更强的趋势中追高
    """
    return StrategyConfig(
        name="aggressive",
        bias_threshold=7.0,
        trend_strong_bias_multiplier=2.0,
        volume_shrink_ratio=0.8,
        buy_threshold=70,
        buy_soft_threshold=55,
    )


# 策略注册表
STRATEGY_REGISTRY: Dict[str, StrategyConfig] = {
    "main": default_strategy(),
    "conservative": conservative_strategy(),
    "aggressive": aggressive_strategy(),
}


def get_strategy(name: str = "main") -> StrategyConfig:
    """按名称获取策略配置。

    如果策略不存在，返回默认策略。

    Args:
        name: 策略名称

    Returns:
        StrategyConfig 对象
    """
    return STRATEGY_REGISTRY.get(name, STRATEGY_REGISTRY["main"])


def register_strategy(config: StrategyConfig) -> None:
    """注册自定义策略。

    Args:
        config: 策略配置对象
    """
    STRATEGY_REGISTRY[config.name] = config


__all__ = [
    "StrategyConfig",
    "default_strategy",
    "conservative_strategy",
    "aggressive_strategy",
    "STRATEGY_REGISTRY",
    "get_strategy",
    "register_strategy",
]
