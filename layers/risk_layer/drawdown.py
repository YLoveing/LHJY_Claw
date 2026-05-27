"""
账户回撤检查 — 从 simulated_trading.py 的 check_account_drawdown 迁移。

保持原逻辑不变：
- 如果当前权益超过峰值，更新峰值
- 计算当前回撤百分比
- 回撤在限制范围内（-5% >= -20% → True）才允许交易
"""

from typing import Optional, Tuple

from .models import AccountState, DrawdownState, RiskConfig

_DEFAULT_CONFIG = RiskConfig()


def check_account_drawdown(
    state: dict,
    config: Optional[RiskConfig] = None,
) -> Tuple[bool, float]:
    """检查账户总回撤是否超过限制。

    从 simulated_trading.py 原样迁移。
    签名兼容旧状态字典，便于渐进迁移。

    Args:
        state: 账户状态字典，需包含 key: cash, positions, peak_equity
            positions 为 {code: {quantity, current_price, ...}}
        config: 风控配置，None 则使用默认阈值 -20%

    Returns:
        (can_trade, drawdown_pct)
        can_trade: True 表示回撤在限制范围内，允许交易
        drawdown_pct: 当前回撤百分比（如 -8.5 表示回撤 8.5%）
    """
    cfg = config or _DEFAULT_CONFIG
    current_equity = state["cash"] + sum(
        p["quantity"] * p["current_price"]
        for p in state["positions"].values()
    )
    peak = state.get("peak_equity", 0)

    if current_equity > peak:
        state["peak_equity"] = current_equity
        peak = current_equity

    if peak == 0:
        return True, 0.0

    drawdown_pct = (current_equity - peak) / peak * 100
    can_trade = drawdown_pct >= cfg.max_drawdown_pct
    return can_trade, drawdown_pct


def check_account_drawdown_state(
    state: AccountState,
    config: Optional[RiskConfig] = None,
) -> DrawdownState:
    """AccountState 版本的账户回撤检查。

    Args:
        state: AccountState 数据模型
        config: 风控配置

    Returns:
        DrawdownState 包含完整回撤信息
    """
    cfg = config or _DEFAULT_CONFIG
    current_equity = state.total_equity
    peak = state.peak_equity

    if current_equity > peak:
        peak = current_equity

    if peak == 0:
        dd_pct = 0.0
        max_dd = 0.0
        is_hibernating = False
    else:
        dd_pct = (current_equity - peak) / peak * 100
        max_dd = min(dd_pct, state.drawdown_pct)
        is_hibernating = dd_pct < cfg.max_drawdown_pct

    recovery_needed = abs(dd_pct) if dd_pct < 0 else 0.0
    return DrawdownState(
        peak_equity=peak,
        current_drawdown_pct=round(dd_pct, 2),
        max_drawdown_pct=round(max_dd, 2),
        is_hibernating=is_hibernating,
        recovery_needed_pct=round(recovery_needed, 2),
    )
