"""
execution_layer — 交易执行层

职责边界：
- 接收 decision_layer 的买卖信号 + risk_layer 的风控放行
- 执行模拟交易（当前模式）
- 费用计算（佣金、印花税、过户费）
- 交易记录持久化
- 投资组合管理（快照、统计）
- 未来扩展：对接 QMT 等真实交易通道

单向依赖：本层依赖 risk_layer，不依赖其他 layer
"""

from .models import (
    OrderRequest,
    Fill,
    TradeRecord,
    ExecutionConfig,
    PortfolioSnapshot,
)
from .fees import calc_buy_fees, calc_sell_fees
from .persistence import (
    load_state,
    save_state,
    load_trades,
    save_trades,
    load_performance,
    save_performance,
    load_signal_trace,
    save_signal_trace,
)
from .engine import SimulatedExecutionEngine
from .portfolio import (
    compute_daily_perf,
    generate_summary,
    generate_performance_card,
    verify_signal_accuracy,
    save_signal_trace_record,
)

__all__ = [
    # models
    "OrderRequest",
    "Fill",
    "TradeRecord",
    "ExecutionConfig",
    "PortfolioSnapshot",
    # fees
    "calc_buy_fees",
    "calc_sell_fees",
    # persistence
    "load_state",
    "save_state",
    "load_trades",
    "save_trades",
    "load_performance",
    "save_performance",
    "load_signal_trace",
    "save_signal_trace",
    # engine
    "SimulatedExecutionEngine",
    # portfolio
    "compute_daily_perf",
    "generate_summary",
    "generate_performance_card",
    "verify_signal_accuracy",
    "save_signal_trace_record",
]
