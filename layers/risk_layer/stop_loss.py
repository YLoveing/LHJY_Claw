"""
止损/止盈管理 — 从 simulated_trading.py 的 check_stop_loss 迁移。

保持原逻辑不变：
- 跌超止损阈值 → stop_loss
- 涨超止盈阈值 → take_profit
- 否则 → hold
"""

from typing import Optional

from .models import PositionInfo, RiskConfig, RiskEvent

# 默认配置统一从 models.RiskConfig 读取（参数对齐 simulated_trading.py）
# stop_loss_pct=-8.0, take_profit_pct=25.0
_DEFAULT_CONFIG = RiskConfig()


def check_stop_loss(
    pos: dict,
    current_price: float,
    code: str,
    report_date_str: str = "",
    config: Optional[RiskConfig] = None,
) -> tuple:
    """检查是否触发止损或止盈。

    从 simulated_trading.py 原样迁移，签名兼容。
    返回值用于与原 execute_trades 流程对接。

    Args:
        pos: 持仓字典，需包含 avg_cost
        current_price: 当前市价
        code: 股票代码（用于日志/事件）
        report_date_str: 报告日期（保留兼容性，未使用）
        config: 风控配置，None 则使用默认值
            (stop_loss_pct=-8.0, take_profit_pct=25.0 对齐 simulated_trading.py)

    Returns:
        (action, reason)
        action: "stop_loss" / "take_profit" / "hold"
        reason: 触发原因描述字符串
    """
    cfg = config or _DEFAULT_CONFIG
    avg_cost = pos["avg_cost"]
    pnl_pct = (current_price - avg_cost) / avg_cost * 100

    if pnl_pct <= cfg.stop_loss_pct:
        return (
            "stop_loss",
            f"止损触发：{pnl_pct:+.1f}%（阈值{cfg.stop_loss_pct}%）",
        )
    if pnl_pct >= cfg.take_profit_pct:
        return (
            "take_profit",
            f"止盈触发：{pnl_pct:+.1f}%（阈值{cfg.take_profit_pct}%）",
        )
    return ("hold", "")


def check_stop_loss_position(
    pos: PositionInfo,
    config: Optional[RiskConfig] = None,
) -> tuple:
    """PositionInfo 版本的止损止盈检查。

    接受 PositionInfo 数据模型，返回 RiskEvent 兼容结果。

    Args:
        pos: PositionInfo 对象
        config: 风控配置

    Returns:
        (event_type, reason) — event_type 为 "stop_loss" / "take_profit" / "hold"
    """
    # 复用 dict 版本逻辑
    return check_stop_loss(
        {
            "avg_cost": pos.avg_cost,
            "quantity": pos.quantity,
        },
        pos.current_price,
        pos.code,
        config=config,
    )


def to_risk_event(action: str, code: str, reason: str) -> Optional[RiskEvent]:
    """将 check_stop_loss 返回值转为 RiskEvent。

    Args:
        action: stop_loss / take_profit / hold
        code: 股票代码
        reason: 触发原因

    Returns:
        RiskEvent 或 None（hold 时）
    """
    if action == "hold":
        return None
    severity = "critical" if action == "stop_loss" else "warning"
    return RiskEvent(
        event_type=action,
        code=code,
        reason=reason,
        severity=severity,
    )
