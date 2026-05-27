"""
凯利公式仓位计算 — 从 simulated_trading.py 的 _kelly_fraction / _compute_kelly_amount 迁移。

保持原逻辑不变：
- 基于评分映射胜率 p
- 半凯利（f_kelly * 0.5），保守策略
- 支持情绪系数调节

使用方式：
    amount = compute_kelly_amount(score=85, cash=50000, sentiment_factor=1.0)
    # → 返回建议买入金额
"""

from typing import Optional

from .models import RiskConfig

# 默认风控参数（与 simulated_trading.py 原值对齐）
_DEFAULT_CONFIG = RiskConfig()


def kelly_fraction(
    score: int,
    kelly_b: Optional[float] = None,
) -> float:
    """计算凯利仓位比例 f* = (b*p - q) / b。

    从 simulated_trading.py 的 _kelly_fraction 原样迁移。

    score 70 → p≈0.65, 75→0.70, 85→0.78, 95→0.85
    返回半凯利（保守策略）。

    Args:
        score: 评分 (0-100)
        kelly_b: 赔率 b = 止盈/止损。None 则使用默认值 25/15

    Returns:
        半凯利比例（0.0 ~ 0.45）
    """
    if kelly_b is None:
        kelly_b = _DEFAULT_CONFIG.kelly_b()

    # score → 胜率 p（与原 _kelly_fraction 一致）
    if score >= 100:
        p = 0.90
    elif score >= 90:
        p = 0.85
    elif score >= 80:
        p = 0.78
    elif score >= 70:
        p = 0.70
    else:
        p = 0.50

    q = 1 - p
    f_kelly = max(0.0, (kelly_b * p - q) / kelly_b)
    return f_kelly * 0.5  # 半凯利


def compute_kelly_amount(
    score: int,
    cash: float,
    sentiment_factor: float = 1.0,
    config: Optional[RiskConfig] = None,
) -> float:
    """计算凯利建议买入金额。

    从 simulated_trading.py 的 _compute_kelly_amount 原样迁移。

    凯利仓位 = 现金 × 凯利比例 × 情绪系数 × 分散系数(0.25)
    结果钳位到 [min_trade, max_trade] 范围内。

    Args:
        score: 评分 (0-100)
        cash: 当前可用现金
        sentiment_factor: 情绪仓位调节系数（默认 1.0）
        config: 风控配置（用于凯利参数），None 则使用默认值

    Returns:
        建议买入金额（float）
    """
    cfg = config or _DEFAULT_CONFIG
    f = kelly_fraction(score, kelly_b=cfg.kelly_b())
    amount = cash * f * cfg.kelly_diversification * sentiment_factor

    # 钳位到合理范围（与原 _compute_kelly_amount 一致）
    min_trade = 10_000
    max_trade = min(cash * cfg.max_single_position_pct, 200_000)
    return max(min_trade, min(max_trade, amount))
