"""
risk_layer — 风控层

职责边界：
- 止损/止盈检查（单只股票）
- 账户总回撤监控
- 仓位管理（凯利公式）
- 风险平价再平衡
- 风控规则可配置（动态阈值）
- 风控事件记录和告警

单向依赖：下层（execution_layer 依赖本层，本层不依赖任何 layer）
"""

from .models import (
    PositionInfo,
    AccountState,
    RiskConfig,
    RiskEvent,
    RiskDecision,
    DrawdownState,
)
from .kelly import kelly_fraction, compute_kelly_amount
from .stop_loss import check_stop_loss
from .drawdown import check_account_drawdown
from .rebalance import try_rebalance
from .manager import RiskManager

__all__ = [
    # models
    "PositionInfo",
    "AccountState",
    "RiskConfig",
    "RiskEvent",
    "RiskDecision",
    "DrawdownState",
    # kelly
    "kelly_fraction",
    "compute_kelly_amount",
    # stop loss
    "check_stop_loss",
    # drawdown
    "check_account_drawdown",
    # rebalance
    "try_rebalance",
    # manager
    "RiskManager",
]
