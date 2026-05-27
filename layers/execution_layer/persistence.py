"""
状态持久化 — 从 simulated_trading.py 的 load/save 函数迁移。

管理 state.json / trades.json / performance.json / signal_trace.json 的读写。
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("execution_layer")

# ── 默认路径（与原 simulated_trading.py 一致）──
_BASE_DIR = Path("/opt/daily_stock_analysis")
_DEFAULT_DATA_DIR = _BASE_DIR / "simulated_trading"


# ============================================================
# 账户状态（state.json）
# ============================================================

INITIAL_CAPITAL = 30_000


def _migrate_state(state: dict) -> dict:
    """确保状态字典包含所有必需字段（v2 迁移兼容）。

    从 simulated_trading.py 的 _migrate_state 原样迁移。
    """
    defaults = {
        "cash": INITIAL_CAPITAL,
        "positions": {},
        "total_pnl": 0.0,
        "total_fee": 0.0,
        "total_market_value": 0.0,
        "total_equity": INITIAL_CAPITAL,
        "total_return": 0.0,
        "total_return_pct": 0.0,
        "peak_equity": INITIAL_CAPITAL,
        "max_drawdown_pct": 0.0,
        "last_update": "",
    }
    for k, v in defaults.items():
        state.setdefault(k, v)
    return state


def load_state(data_dir: Optional[Path] = None) -> dict:
    """从 state.json 加载账户状态。

    从 simulated_trading.py 的 load_state 原样迁移。

    Args:
        data_dir: 数据目录，None 则使用默认路径

    Returns:
        状态字典（确保所有必需字段存在）
    """
    d = data_dir or _DEFAULT_DATA_DIR
    state_file = d / "state.json"
    if state_file.exists():
        with open(state_file) as f:
            return _migrate_state(json.load(f))
    return _migrate_state({})


def save_state(state: dict, data_dir: Optional[Path] = None) -> None:
    """保存账户状态到 state.json。

    从 simulated_trading.py 的 save_state 原样迁移。
    """
    d = data_dir or _DEFAULT_DATA_DIR
    d.mkdir(parents=True, exist_ok=True)
    state_file = d / "state.json"
    with open(state_file, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ============================================================
# 交易记录（trades.json）
# ============================================================

def load_trades(data_dir: Optional[Path] = None) -> List[dict]:
    """从 trades.json 加载交易记录。

    从 simulated_trading.py 的 load_trades 原样迁移。
    """
    d = data_dir or _DEFAULT_DATA_DIR
    trades_file = d / "trades.json"
    if trades_file.exists():
        with open(trades_file) as f:
            return json.load(f)
    return []


def save_trades(trades: List[dict], data_dir: Optional[Path] = None) -> None:
    """保存交易记录到 trades.json。"""
    d = data_dir or _DEFAULT_DATA_DIR
    d.mkdir(parents=True, exist_ok=True)
    trades_file = d / "trades.json"
    with open(trades_file, "w") as f:
        json.dump(trades, f, ensure_ascii=False, indent=2)


# ============================================================
# 绩效数据（performance.json）
# ============================================================

def load_performance(data_dir: Optional[Path] = None) -> dict:
    """从 performance.json 加载绩效数据。

    从 simulated_trading.py 的 load_performance 原样迁移。
    """
    d = data_dir or _DEFAULT_DATA_DIR
    perf_file = d / "performance.json"
    if perf_file.exists():
        with open(perf_file) as f:
            return json.load(f)
    return {"daily": [], "weekly": [], "summary": {}}


def save_performance(perf: dict, data_dir: Optional[Path] = None) -> None:
    """保存绩效数据到 performance.json。"""
    d = data_dir or _DEFAULT_DATA_DIR
    d.mkdir(parents=True, exist_ok=True)
    perf_file = d / "performance.json"
    with open(perf_file, "w") as f:
        json.dump(perf, f, ensure_ascii=False, indent=2)


# ============================================================
# 信号追踪（signal_trace.json）
# ============================================================

def load_signal_trace(data_dir: Optional[Path] = None) -> dict:
    """从 signal_trace.json 加载信号追踪记录。

    从 simulated_trading.py 的 load_signal_trace 原样迁移。
    """
    d = data_dir or _DEFAULT_DATA_DIR
    trace_file = d / "signal_trace.json"
    if trace_file.exists():
        with open(trace_file) as f:
            return json.load(f)
    return {"records": []}


def save_signal_trace(trace: dict, data_dir: Optional[Path] = None) -> None:
    """保存信号追踪记录到 signal_trace.json。

    从 simulated_trading.py 的 save_signal_trace 原样迁移。
    """
    d = data_dir or _DEFAULT_DATA_DIR
    d.mkdir(parents=True, exist_ok=True)
    trace_file = d / "signal_trace.json"
    with open(trace_file, "w") as f:
        json.dump(trace, f, ensure_ascii=False, indent=2, default=str)
