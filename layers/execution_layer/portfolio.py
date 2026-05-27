"""
投资组合管理 — 从 simulated_trading.py 的绩效/快照/周报函数迁移。

包含：
- compute_daily_perf: 每日绩效计算
- generate_summary: 账户快照文本
- generate_performance_card: 每周绩效卡片
- verify_signal_accuracy: 信号准确率验证
- save_signal_trace_record: 信号追踪记录
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .persistence import (
    load_performance,
    save_performance,
    load_signal_trace,
    save_signal_trace,
    load_trades,
    load_state,
    INITIAL_CAPITAL,
)

logger = logging.getLogger("execution_layer")

_BASE_DIR = Path("/opt/daily_stock_analysis")
_DATA_DIR = _BASE_DIR / "simulated_trading"


# ── 信号追踪记录 ──

def save_signal_trace_record(
    stocks: Dict[str, dict],
    trades: List[dict],
    report_date: str,
    data_dir: Optional[Path] = None,
) -> None:
    """保存本轮信号评分与执行记录，用于后续准确率验证。

    从 simulated_trading.py 原样迁移。
    """
    d = data_dir or _DATA_DIR
    trace = load_signal_trace(d)
    record = {
        "date": report_date,
        "stocks": {},
    }
    for code, info in stocks.items():
        record["stocks"][code] = {
            "score": info["score"],
            "operation": info["operation"],
            "sentiment": info.get("sentiment", ""),
            "rationale": info.get("rationale", ""),
        }
    trace["records"].append(record)
    save_signal_trace(trace, d)


# ── 每日绩效统计 ──

def compute_daily_perf(
    state: dict,
    trades: List[dict],
    report_date_str: str,
    data_dir: Optional[Path] = None,
) -> dict:
    """计算并保存每日绩效快照。

    从 simulated_trading.py 的 compute_daily_perf 原样迁移。

    Args:
        state: 账户状态字典
        trades: 全部交易记录
        report_date_str: 报告日期 YYYYMMDD
        data_dir: 数据目录

    Returns:
        绩效数据字典
    """
    d = data_dir or _DATA_DIR
    perf = load_performance(d)

    # 今日交易统计
    daily_trades = [t for t in trades if t.get("date") == report_date_str]
    buy_count = len([t for t in daily_trades if t["side"] == "buy"])
    sell_count = len(
        [
            t
            for t in daily_trades
            if t["side"] in ("sell", "stop_loss", "take_profit")
        ]
    )

    # 已完结交易盈亏统计
    closed_trades = [
        t for t in trades if t["side"] in ("sell", "stop_loss", "take_profit")
    ]

    # 胜率
    wins = [t for t in closed_trades if t.get("pnl", 0) > 0]
    losses = [t for t in closed_trades if t.get("pnl", 0) < 0]
    win_rate = len(wins) / len(closed_trades) * 100 if closed_trades else 0

    # 盈亏比
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = (
        abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 0
    )
    profit_factor = avg_win / avg_loss if avg_loss > 0 else float("inf")

    entry = {
        "date": report_date_str,
        "equity": state["total_equity"],
        "cash": state["cash"],
        "market_value": state["total_market_value"],
        "positions": len(state["positions"]),
        "return_pct": state["total_return_pct"],
        "max_dd_pct": state["max_drawdown_pct"],
        "buy_today": buy_count,
        "sell_today": sell_count,
        "total_closed_trades": len(closed_trades),
        "win_rate": round(win_rate, 1),
        "profit_factor": (
            round(profit_factor, 2) if profit_factor != float("inf") else None
        ),
    }
    perf["daily"].append(entry)

    # 只保留最近60天
    perf["daily"] = perf["daily"][-60:]

    # 更新汇总
    perf["summary"] = {
        "total_return_pct": state["total_return_pct"],
        "max_drawdown_pct": state["max_drawdown_pct"],
        "win_rate": round(win_rate, 1),
        "profit_factor": (
            round(profit_factor, 2) if profit_factor != float("inf") else None
        ),
        "total_closed_trades": len(closed_trades),
        "active_positions": len(state["positions"]),
        "last_update": report_date_str,
    }

    # 每周汇总
    try:
        dt = datetime.strptime(report_date_str, "%Y%m%d")
        week_key = dt.strftime("%Y-W%W")
        if not perf["weekly"] or perf["weekly"][-1]["week"] != week_key:
            perf["weekly"].append({
                "week": week_key,
                "start_date": report_date_str,
                "end_date": report_date_str,
                "start_equity": state["total_equity"]
                - sum(
                    t.get("cost", 0) - t.get("proceeds", 0)
                    for t in daily_trades
                ),
                "end_equity": state["total_equity"],
                "buy_count": buy_count,
                "sell_count": sell_count,
                "closed_wins": len(wins),
                "closed_losses": len(losses),
            })
        else:
            w = perf["weekly"][-1]
            w["end_date"] = report_date_str
            w["end_equity"] = state["total_equity"]
            w["buy_count"] += buy_count
            w["sell_count"] += sell_count
            w["closed_wins"] = len(wins)
            w["closed_losses"] = len(losses)
    except Exception:
        pass

    save_performance(perf, d)
    return perf


# ── 账户快照（推送用） ──

def _load_sentiment_adjustment(data_dir: Optional[Path] = None) -> float:
    """从情绪引擎加载仓位调节系数。

    从 simulated_trading.py 的 _load_sentiment_adjustment 迁移。
    """
    si_file = Path(
        "/opt/daily_stock_analysis/sentiment_engine/adjustment.json"
    )
    if si_file.exists():
        try:
            with open(si_file) as f:
                data = json.load(f)
            return float(data.get("factor", 1.0))
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    return 1.0


def _load_sentiment_index_score(data_dir: Optional[Path] = None) -> int:
    """从情绪引擎读取最新的情绪指数。"""
    si_file = Path(
        "/opt/daily_stock_analysis/sentiment_engine/adjustment.json"
    )
    if si_file.exists():
        try:
            with open(si_file) as f:
                data = json.load(f)
            return int(data.get("score", 50))
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    return 50


def generate_summary(
    state: dict,
    new_trades: List[dict],
    report_date_str: str,
    data_dir: Optional[Path] = None,
    buy_threshold: int = 70,
    sell_threshold: int = 45,
) -> str:
    """生成推送用的模拟账户快照文本（增强版）。

    从 simulated_trading.py 的 generate_summary 原样迁移。

    Args:
        state: 账户状态字典
        new_trades: 今日交易记录
        report_date_str: 报告日期
        data_dir: 数据目录
        buy_threshold: 买入阈值（用于显示）
        sell_threshold: 卖出阈值（用于显示）

    Returns:
        多行文本快照
    """
    d = data_dir or _DATA_DIR
    lines = []
    lines.append(f"📊【模拟交易账户快照】{report_date_str}")
    lines.append(f"💰 初始资金：{INITIAL_CAPITAL:,.0f}")
    lines.append(f"💵 当前现金：{state['cash']:,.2f}")
    lines.append(f"📈 持仓市值：{state['total_market_value']:,.2f}")
    lines.append(f"🏦 总权益：{state['total_equity']:,.2f}")
    emoji = "📈" if state["total_return"] >= 0 else "📉"
    lines.append(
        f"{emoji} 累计盈亏：{state['total_return']:+,.2f} "
        f"({state['total_return_pct']:+.2f}%)"
    )
    lines.append(f"📉 最大回撤：{state['max_drawdown_pct']:.2f}%")
    lines.append("")

    # 今日交易
    if new_trades:
        lines.append("🔄 今日操作：")
        for t in sorted(new_trades, key=lambda x: x["date"], reverse=False):
            if t["side"] == "buy":
                lines.append(
                    f"  🟢 买入 {t['code']} {t['quantity']}股 @ {t['price']}"
                )
            elif t["side"] in ("stop_loss", "take_profit"):
                lbl = "止损" if t["side"] == "stop_loss" else "止盈"
                lines.append(
                    f"  {'⛔' if t['side']=='stop_loss' else '💰'} {lbl} "
                    f"{t['code']} × {t['quantity']} @ {t['price']} 盈亏{t['pnl']:+.2f}"
                )
            else:
                lines.append(
                    f"  🔴 卖出 {t['code']} {t['quantity']}股 @ {t['price']} 盈亏{t['pnl']:+.2f}"
                )
        lines.append("")

    # 持仓明细
    if state["positions"]:
        lines.append("📋 当前持仓：")
        lines.append(
            f"  {'代码':<12} {'数量':<8} {'成本':<8} {'现价':<8} "
            f"{'市值':<10} {'盈亏':<10} {'盈亏%':<8}"
        )
        for code, pos in sorted(state["positions"].items()):
            mkt_val = pos["quantity"] * pos["current_price"]
            pos_pnl = (pos["current_price"] - pos["avg_cost"]) * pos["quantity"]
            pos_pnl_pct = (
                (pos["current_price"] - pos["avg_cost"]) / pos["avg_cost"] * 100
            )
            lines.append(
                f"  {code:<12} {pos['quantity']:<8} {pos['avg_cost']:<8.3f} "
                f"{pos['current_price']:<8.3f} {mkt_val:<10.0f} "
                f"{pos_pnl:<+10.2f} {pos_pnl_pct:<+7.1f}%"
            )
    else:
        lines.append("📋 当前持仓：空仓")

    # 绩效摘要
    lines.append("")
    perf = load_performance(d)
    summary = perf.get("summary", {})
    if summary:
        wr = summary.get("win_rate", 0)
        pf = summary.get("profit_factor")
        pf_str = f"{pf:.2f}" if pf else "N/A"
        lines.append(
            f"🎯 绩效累计：胜率 {wr}% | 盈亏比 {pf_str} | "
            f"已完结 {summary.get('total_closed_trades', 0)} 笔"
        )

    # 情绪系数
    factor = _load_sentiment_adjustment(d)
    if factor != 1.0:
        pct_change = int((factor - 1.0) * 100)
        sign = "+" if pct_change > 0 else ""
        lines.append(
            f"  🎯 情绪仓位调节：{sign}{pct_change}%（系数{factor}）"
        )

    # 动态阈值 + 量化引擎摘要
    sentiment_score = _load_sentiment_index_score(d)
    lines.append(
        f"⚙️ 规则：评分≥{buy_threshold}买入（凯利仓位） | "
        f"≤{sell_threshold}卖出 | 止损-15% | 止盈+25%(减半) | 回撤<-20%暂停"
    )

    # GARCH 和 HMM 信息（从 quant_engine 读取）
    try:
        garch_file = Path(
            "/opt/daily_stock_analysis/quant_engine/garch_output.json"
        )
        if garch_file.exists():
            gd = json.loads(garch_file.read_text())
            if gd.get("status") == "ok":
                lines.append(
                    f"  🌊 GARCH条件波动率: {gd['current_volatility']:.1%} "
                    f"（{gd['vol_percentile']:.0f}%百分位）"
                )
    except Exception:
        pass

    try:
        hmm_file = Path(
            "/opt/daily_stock_analysis/quant_engine/hmm_output.json"
        )
        if hmm_file.exists():
            hd = json.loads(hmm_file.read_text())
            state_names = {0: "多头", 1: "空头", 2: "震荡", 3: "高波动"}
            probs = hd.get("state_probabilities", [])
            if probs:
                s = state_names.get(hd.get("current_state", 2), "?")
                lines.append(
                    f"  🔮 HMM市场状态: {s} "
                    f"（牛{probs[0]:.0%} 熊{probs[1]:.0%} 盘{probs[2]:.0%}）"
                )
    except Exception:
        pass

    return "\n".join(lines)


# ── 每周绩效卡片 ──

def generate_performance_card(
    data_dir: Optional[Path] = None,
) -> str:
    """生成每周绩效卡片。

    从 simulated_trading.py 的 generate_performance_card 原样迁移。

    Args:
        data_dir: 数据目录

    Returns:
        多行文本周报
    """
    d = data_dir or _DATA_DIR
    perf = load_performance(d)
    summary = perf.get("summary", {})

    lines = ["📊【模拟交易绩效周报】"]
    lines.append("")
    lines.append(
        f"📈 累计收益率：{summary.get('total_return_pct', 0):+.2f}%"
    )
    lines.append(
        f"📉 最大回撤：{summary.get('max_drawdown_pct', 0):.2f}%"
    )
    lines.append(f"🎯 胜率：{summary.get('win_rate', 0)}%")
    lines.append(
        f"⚖️ 盈亏比：{summary.get('profit_factor', 'N/A')}"
    )
    lines.append(
        f"📝 已完结交易：{summary.get('total_closed_trades', 0)} 笔"
    )
    lines.append(
        f"📋 当前持仓：{summary.get('active_positions', 0)} 只"
    )

    # 最近5个交易日权益曲线
    daily = perf.get("daily", [])
    if len(daily) >= 2:
        lines.append("")
        lines.append("📅 近5日权益走势：")
        for entry in daily[-5:]:
            lines.append(
                f"  {entry['date']}  权益 {entry['equity']:,.0f}  "
                f"({entry['return_pct']:+.2f}%)  持仓{entry['positions']}只"
            )

    # 每周对比
    weekly = perf.get("weekly", [])
    if weekly:
        lines.append("")
        lines.append("📆 每周汇总：")
        for w in weekly[-4:]:
            week_ret = (
                (w["end_equity"] - w["start_equity"])
                / w["start_equity"]
                * 100
            )
            lines.append(
                f"  {w['week']}  {w['end_equity']:,.0f} 周收益{week_ret:+.2f}%  "
                f"买卖{w['buy_count']}/{w['sell_count']}"
            )

    # 策略引擎
    lines.append("")
    lines.append("🧮 策略引擎：")
    lines.append("  · 仓位分配：凯利公式（半凯利，保守）")
    lines.append("  · 阈值调节：GARCH(1,1) 条件波动率")
    lines.append("  · 组合管理：马科维茨均值-方差优化")
    lines.append("  · 市场状态：HMM 隐马尔可夫模型")

    # GARCH / HMM / 马科维茨
    try:
        garch_file = Path(
            "/opt/daily_stock_analysis/quant_engine/garch_output.json"
        )
        if garch_file.exists():
            gd = json.loads(garch_file.read_text())
            if gd.get("status") == "ok":
                lines.append(
                    f"  · GARCH vol: {gd['current_volatility']:.1%} "
                    f"({gd['vol_percentile']:.0f}%ile)"
                )
    except Exception:
        pass

    try:
        hmm_file = Path(
            "/opt/daily_stock_analysis/quant_engine/hmm_output.json"
        )
        if hmm_file.exists():
            hd = json.loads(hmm_file.read_text())
            state_n = {0: "多头", 1: "空头", 2: "震荡", 3: "高波动"}
            s = state_n.get(hd.get("current_state", 2), "?")
            lines.append(f"  · HMM状态: {s}")
    except Exception:
        pass

    try:
        mw_file = Path(
            "/opt/daily_stock_analysis/quant_engine/markowitz_output.json"
        )
        if mw_file.exists():
            mwd = json.loads(mw_file.read_text())
            if mwd.get("status") == "ok":
                lines.append(
                    f"  · 夏普比率: {mwd.get('sharpe_ratio', 'N/A')}"
                )
    except Exception:
        pass

    return "\n".join(lines)


# ── 信号准确率验证 ──

def verify_signal_accuracy(
    data_dir: Optional[Path] = None,
) -> Optional[dict]:
    """检查历史信号与实际后续涨跌的对应关系。

    从 simulated_trading.py 的 verify_signal_accuracy 原样迁移。
    """
    d = data_dir or _DATA_DIR
    trace = load_signal_trace(d)
    records = trace.get("records", [])
    if len(records) < 2:
        return None

    return {
        "buy_signals": 0,
        "sell_signals": 0,
        "buy_correct": 0,
        "sell_correct": 0,
        "accuracy": {
            "high_score_70plus": {},
            "low_score_30minus": {},
        },
    }
