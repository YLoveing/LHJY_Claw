"""
风控数据模型 — 定义风控层内部和对外暴露的数据结构。

所有模型均为 dataclass，支持序列化（通过 .to_dict() 或 asdict）。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class PositionInfo:
    """单只持仓信息 — 风控视角。

    Args:
        code: 股票代码
        quantity: 持有股数
        avg_cost: 持仓均价
        current_price: 当前市价
        invested: 总投资金额
        entry_score: 买入时评分
    """

    code: str
    quantity: int
    avg_cost: float
    current_price: float
    invested: float
    entry_score: int = 0

    @property
    def market_value(self) -> float:
        """当前持仓市值。"""
        return self.quantity * self.current_price

    @property
    def pnl(self) -> float:
        """浮动盈亏。"""
        return (self.current_price - self.avg_cost) * self.quantity

    @property
    def pnl_pct(self) -> float:
        """浮动盈亏百分比。"""
        if self.avg_cost == 0:
            return 0.0
        return (self.current_price - self.avg_cost) / self.avg_cost * 100


@dataclass
class AccountState:
    """账户总状态 — 风控视角。

    Args:
        cash: 当前现金
        positions: 持仓字典 {code: PositionInfo}
        total_equity: 总权益（现金 + 持仓市值）
        peak_equity: 历史峰值权益
        total_pnl: 累计已实现盈亏
        total_fee: 累计费用
    """

    cash: float
    positions: Dict[str, PositionInfo]
    total_equity: float
    peak_equity: float
    total_pnl: float = 0.0
    total_fee: float = 0.0

    @property
    def market_value(self) -> float:
        """持仓总市值。"""
        return sum(p.market_value for p in self.positions.values())

    @property
    def position_count(self) -> int:
        """持仓数量。"""
        return len(self.positions)

    @property
    def drawdown_pct(self) -> float:
        """当前回撤百分比（相对于历史峰值）。"""
        if self.peak_equity <= 0:
            return 0.0
        return (self.total_equity - self.peak_equity) / self.peak_equity * 100

    def to_dict(self) -> dict:
        return {
            "cash": self.cash,
            "positions": {
                k: {
                    "code": v.code,
                    "quantity": v.quantity,
                    "avg_cost": v.avg_cost,
                    "current_price": v.current_price,
                    "invested": v.invested,
                    "entry_score": v.entry_score,
                }
                for k, v in self.positions.items()
            },
            "total_equity": self.total_equity,
            "peak_equity": self.peak_equity,
            "total_pnl": self.total_pnl,
            "total_fee": self.total_fee,
        }


@dataclass
class DrawdownState:
    """回撤状态跟踪。"""

    peak_equity: float
    current_drawdown_pct: float
    max_drawdown_pct: float
    is_hibernating: bool = False  # 是否因回撤暂停交易
    recovery_needed_pct: float = 0.0  # 恢复回撤需要的涨幅


@dataclass
class RiskConfig:
    """风控配置参数。

    Args:
        stop_loss_pct: 单只止损阈值（%），-15 表示跌15%触发止损
        take_profit_pct: 单只止盈阈值（%），25 表示涨25%触发减半仓
        max_drawdown_pct: 账户最大回撤限制（%），-30 表示回撤超30%暂停交易
        max_positions: 最大持仓数
        kelly_diversification: 凯利分散系数
        max_single_position_pct: 单只仓位占比上限
        sentiment_adjustment_path: 情绪调节文件路径（可选）
    """

    # 止损/止盈参数对齐 simulated_trading.py 的 HARD_STOP_PCT / TAKE_PROFIT_PCT
    stop_loss_pct: float = -8.0
    take_profit_pct: float = 25.0
    # 费率参数（A股真实费率，与 fees.py DEFAULT_* 常量对齐）
    commission_rate: float = 0.00025  # 佣金万2.5（买卖均收，最低5元）
    stamp_tax_rate: float = 0.0005  # 印花税万5（仅卖出时收）
    min_commission: float = 5.0  # 佣金最低收费5元
    max_drawdown_pct: float = -30.0  # 对齐 simulated_trading.py 的 ACCOUNT_DRAWDOWN_LIMIT
    max_positions: int = 8
    kelly_diversification: float = 0.25
    max_single_position_pct: float = 0.25
    sentiment_adjustment_path: str = ""

    def kelly_b(self) -> float:
        """凯利赔率 b = 止盈 / abs(止损) = 25.0 / 8.0 = 3.125。"""
        if self.stop_loss_pct == 0:
            return 3.125
        return abs(self.take_profit_pct / self.stop_loss_pct)


@dataclass
class RiskEvent:
    """一次风控事件记录。

    Args:
        event_type: stop_loss / take_profit / drawdown_alert / rebalance / buy_blocked
        code: 相关股票代码
        reason: 触发原因描述
        severity: info / warning / critical
    """

    event_type: str
    code: str
    reason: str
    severity: str = "info"


@dataclass
class RiskDecision:
    """风控决策结果。

    Args:
        allowed: 是否允许操作
        max_buy_amount: 最大可买入金额
        events: 本次决策触发的风控事件列表
        adjusted_max_positions: 情绪调节后的持仓上限
    """

    allowed: bool
    max_buy_amount: float
    events: List[RiskEvent] = field(default_factory=list)
    adjusted_max_positions: int = 8
