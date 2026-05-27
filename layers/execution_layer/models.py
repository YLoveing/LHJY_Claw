"""
交易数据模型 — 定义执行层内部和对外暴露的数据结构。

遵循设计文档 (REFACTOR_PLAN.md 3.6) 中的接口定义。
所有模型均为 dataclass，支持序列化。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class OrderRequest:
    """交易订单请求。

    Args:
        code: 股票代码
        side: 买卖方向 (buy / sell)
        price: 委托价格
        quantity: 委托数量
        order_type: 订单类型 (market / limit)
        reason: 下单原因
        strategy_name: 策略名称
    """
    code: str
    side: str  # buy / sell
    price: float
    quantity: int
    order_type: str = "market"
    reason: str = ""
    strategy_name: str = "main"


@dataclass
class Fill:
    """成交记录。

    Args:
        order_id: 订单 ID
        code: 股票代码
        side: 买卖方向
        price: 成交价格
        quantity: 成交数量
        fee: 交易费用
        timestamp: 成交时间
        pnl: 盈亏（卖出时）
    """
    order_id: str
    code: str
    side: str
    price: float
    quantity: int
    fee: float
    timestamp: str
    pnl: float = 0.0


@dataclass
class TradeRecord:
    """交易记录 — 持久化到 trades.json 的格式兼容结构。

    Args:
        date: 交易日期 YYYYMMDD
        code: 股票代码
        side: 买卖方向 (buy / sell / stop_loss / take_profit / rebalance_buy / rebalance_sell)
        quantity: 交易数量
        price: 成交价格
        cost: 总成本（买入时）
        fee: 交易费用
        proceeds: 回收资金（卖出时）
        pnl: 盈亏（卖出时）
        score: 买入时的评分
        kelly_pct: 凯利比例（买入时）
        reason: 交易原因
    """
    date: str
    code: str
    side: str
    quantity: int
    price: float
    cost: float = 0.0
    fee: float = 0.0
    proceeds: float = 0.0
    pnl: float = 0.0
    score: Optional[int] = None
    kelly_pct: Optional[float] = None
    reason: str = ""

    def to_dict(self) -> dict:
        result = {
            "date": self.date,
            "code": self.code,
            "side": self.side,
            "quantity": self.quantity,
            "price": round(self.price, 3),
            "fee": round(self.fee, 2),
            "reason": self.reason,
        }
        if self.side == "buy":
            result["cost"] = round(self.cost, 2)
            if self.score is not None:
                result["score"] = self.score
            if self.kelly_pct is not None:
                result["kelly_pct"] = self.kelly_pct
        else:
            result["proceeds"] = round(self.proceeds, 2)
            result["pnl"] = round(self.pnl, 2)
        return result

    @classmethod
    def from_dict(cls, d: dict) -> "TradeRecord":
        return cls(
            date=d.get("date", ""),
            code=d.get("code", ""),
            side=d.get("side", ""),
            quantity=d.get("quantity", 0),
            price=d.get("price", 0.0),
            cost=d.get("cost", 0.0),
            fee=d.get("fee", 0.0),
            proceeds=d.get("proceeds", 0.0),
            pnl=d.get("pnl", 0.0),
            score=d.get("score"),
            kelly_pct=d.get("kelly_pct"),
            reason=d.get("reason", ""),
        )


@dataclass
class ExecutionConfig:
    """执行引擎配置。

    Args:
        initial_capital: 初始资金
        commission_rate: 佣金费率 (万2.5 = 0.00025)
        stamp_tax_rate: 印花税费率 (万5 = 0.0005)
        transfer_fee_rate: 过户费率 (万0.1 = 0.00001)
        min_commission: 最低佣金
        state_dir: 状态文件存储目录
    """
    initial_capital: float = 30_000
    commission_rate: float = 0.00025
    stamp_tax_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001
    min_commission: float = 5.0
    state_dir: str = ""


@dataclass
class PortfolioSnapshot:
    """投资组合快照 — 每日绩效记录。

    Args:
        date: 快照日期 YYYYMMDD
        cash: 现金余额
        positions: 持仓字典 {code: {quantity, avg_cost, current_price}}
        total_equity: 总权益
        total_return_pct: 累计收益率
        max_drawdown_pct: 最大回撤
        trades_today: 今日交易记录
    """
    date: str
    cash: float
    positions: dict
    total_equity: float
    total_return_pct: float
    max_drawdown_pct: float
    trades_today: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "equity": round(self.total_equity, 2),
            "cash": round(self.cash, 2),
            "positions": self.positions,
            "return_pct": round(self.total_return_pct, 2),
            "max_dd_pct": round(self.max_drawdown_pct, 2),
        }
