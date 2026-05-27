"""
风险平价再平衡 — 从 simulated_trading.py 的 _try_rebalance 迁移。

保持原逻辑不变：
- 仅周五执行
- 优先级：马科维茨最优权重 → 风险平价等风险贡献（回退）
- 持仓少于 2 只则跳过
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .models import PositionInfo, AccountState, RiskConfig

logger = logging.getLogger("simulated_trading")

_DEFAULT_CONFIG = RiskConfig()

# 默认量化引擎输出路径（与 simulated_trading.py 一致）
MARKOWITZ_PATH = Path(
    "/opt/daily_stock_analysis/quant_engine/markowitz_output.json"
)


def try_rebalance(
    state: dict,
    trades: List[dict],
    new_trades: List[dict],
    report_date_str: str,
    markowitz_path: Optional[Path] = None,
) -> None:
    """周五收盘后执行马科维茨/风险平价再平衡。

    从 simulated_trading.py 的 _try_rebalance 原样迁移。

    优先级：
    1. 马科维茨均值-方差最优权重 (从 quant_engine 读取)
    2. 风险平价等风险贡献 (回退方案)

    Args:
        state: 账户状态字典（会被原地修改）
        trades: 全部交易记录列表（会被追加）
        new_trades: 今日交易记录列表（会被追加）
        report_date_str: 报告日期 YYYYMMDD
        markowitz_path: 马科维茨输出文件路径

    Note:
        该函数直接修改 state / trades / new_trades，与旧版 _try_rebalance 行为一致。
    """
    try:
        today = datetime.strptime(report_date_str, "%Y%m%d")
        if today.weekday() != 4:  # 仅周五
            return
    except ValueError:
        return

    if len(state["positions"]) < 2:
        return

    mw_path = markowitz_path or MARKOWITZ_PATH

    # ── 尝试读取马科维茨最优权重 ──
    target_weights = None
    try:
        if mw_path.exists():
            mw_data = json.loads(mw_path.read_text())
            if mw_data.get("status") == "ok" and mw_data.get("weights"):
                target_weights = mw_data["weights"]
                sr = mw_data.get("sharpe_ratio", 0)
                logger.info(
                    f"[再平衡] 马科维茨最优权重 (夏普{sr:.3f}): {target_weights}"
                )
    except Exception:
        pass

    logger.info(f"[再平衡] 周五组合再平衡检查...")

    if target_weights:
        _rebalance_markowitz(state, trades, new_trades, report_date_str, target_weights)
    else:
        _rebalance_risk_parity(state, trades, new_trades, report_date_str)

    logger.info(f"[再平衡] 完成")


def _rebalance_markowitz(
    state: dict,
    trades: List[dict],
    new_trades: List[dict],
    report_date_str: str,
    target_weights: dict,
) -> None:
    """马科维茨权重再平衡。"""
    held_codes = list(state["positions"].keys())

    # 归一化权重至当前持仓
    try:
        from quant_engine.markowitz import normalize_weights_for_positions as _nw

        target_weights = _nw(target_weights, held_codes)
        logger.info(
            f"[再平衡] 归一化权重至持仓: "
            f"{dict(zip(held_codes, [target_weights.get(c, 0) for c in held_codes]))}"
        )
    except Exception:
        # 等权 fallback
        target_weights = {c: 1.0 / len(held_codes) for c in held_codes}

    # 计算每个持仓的目标市值
    total_equity = state["cash"] + sum(
        p["quantity"] * p["current_price"]
        for p in state["positions"].values()
    )

    adjustments = _compute_markowitz_adjustments(
        state, total_equity, target_weights
    )
    _apply_adjustments(state, trades, new_trades, adjustments, report_date_str)


def _compute_markowitz_adjustments(
    state: dict,
    total_equity: float,
    target_weights: dict,
) -> List[dict]:
    """计算马科维茨再平衡调整。"""
    adjustments = []
    for code, pos in state["positions"].items():
        target_pct = target_weights.get(code)
        if target_pct is None or target_pct <= 0:
            adjustments.append({
                "code": code,
                "current_qty": pos["quantity"],
                "diff_qty": -pos["quantity"],
                "action": "reduce",
                "deviation_pct": -100,
            })
            continue

        target_value = total_equity * target_pct
        current_value = pos["quantity"] * pos["current_price"]
        target_qty = max(100, int(target_value / pos["current_price"] / 100) * 100)
        diff_qty = target_qty - pos["quantity"]

        if abs(diff_qty) >= 100:
            deviation = (current_value - target_value) / target_value * 100
            adjustments.append({
                "code": code,
                "current_qty": pos["quantity"],
                "target_qty": target_qty,
                "diff_qty": diff_qty,
                "deviation_pct": round(deviation, 1),
                "action": "add" if diff_qty > 0 else "reduce",
            })
    return adjustments


def _rebalance_risk_parity(
    state: dict,
    trades: List[dict],
    new_trades: List[dict],
    report_date_str: str,
) -> None:
    """风险平价等风险贡献 (回退方案)。"""
    positions_with_vol = []
    total_risk = 0
    for code, pos in state["positions"].items():
        entry_score = pos.get("entry_score", 50)
        vol_est = 0.35 - (entry_score - 50) * 0.004
        vol_est = max(0.12, min(0.40, vol_est))
        pos_value = pos["quantity"] * pos["current_price"]
        risk_contribution = pos_value * vol_est
        positions_with_vol.append((code, pos, pos_value, vol_est, risk_contribution))
        total_risk += risk_contribution

    if total_risk <= 0:
        return

    target_risk_per_pos = total_risk / len(positions_with_vol)

    adjustments = _compute_risk_parity_adjustments(
        positions_with_vol, total_risk, target_risk_per_pos
    )
    _apply_adjustments(state, trades, new_trades, adjustments, report_date_str)


def _compute_risk_parity_adjustments(
    positions_with_vol: list,
    total_risk: float,
    target_risk_per_pos: float,
) -> List[dict]:
    """计算风险平价再平衡调整。"""
    adjustments = []
    for code, pos, pos_value, vol_est, risk_cont in positions_with_vol:
        risk_pct = risk_cont / total_risk * 100
        ideal_pct = 100.0 / len(positions_with_vol)
        deviation = (risk_pct - ideal_pct) / ideal_pct

        if abs(deviation) > 0.20:
            target_value = target_risk_per_pos / vol_est
            target_qty = max(
                100, int(target_value / pos["current_price"] / 100) * 100
            )
            diff_qty = target_qty - pos["quantity"]
            adjustments.append({
                "code": code,
                "current_qty": pos["quantity"],
                "target_qty": target_qty,
                "diff_qty": diff_qty,
                "deviation_pct": round(deviation * 100, 1),
                "action": "add" if diff_qty > 0 else "reduce",
            })
    return adjustments


def _apply_adjustments(
    state: dict,
    trades: List[dict],
    new_trades: List[dict],
    adjustments: List[dict],
    report_date_str: str,
) -> None:
    """执行再平衡调整（买入/卖出）。"""
    if not adjustments:
        logger.info(f"[再平衡] 仓位分布合理，无需调整")
        return

    logger.info(f"[再平衡] 发现 {len(adjustments)} 只持仓需调整:")
    for adj in adjustments:
        action_text = (
            f"加仓{adj['diff_qty']}股"
            if adj["diff_qty"] > 0
            else f"减仓{abs(adj['diff_qty'])}股"
        )
        logger.info(f"  {adj['code']}: 偏离{adj['deviation_pct']:+.0f}% → {action_text}")
        pos = state["positions"].get(adj["code"])
        if not pos:
            continue
        price = pos["current_price"]

        if adj["diff_qty"] > 0:
            _apply_rebalance_buy(
                state, trades, new_trades, adj, price, report_date_str
            )
        elif adj["diff_qty"] < 0:
            _apply_rebalance_sell(
                state, trades, new_trades, adj, pos, price, report_date_str
            )


def _apply_rebalance_buy(
    state: dict,
    trades: List[dict],
    new_trades: List[dict],
    adj: dict,
    price: float,
    report_date_str: str,
) -> None:
    """执行再平衡加仓。"""
    from ..execution_layer.fees import calc_buy_fees

    qty = int(adj["diff_qty"] / 100) * 100
    if qty < 100:
        return
    cost = qty * price
    fee = calc_buy_fees(cost)
    total_cost = cost + fee
    if total_cost > state["cash"]:
        qty = int((state["cash"] - fee) / price / 100) * 100
        if qty < 100:
            return
        cost = qty * price
        fee = calc_buy_fees(cost)
        total_cost = cost + fee

    state["cash"] -= total_cost
    state["total_fee"] += fee
    total_qty = pos["quantity"] + qty
    total_invested = pos["avg_cost"] * pos["quantity"] + cost
    pos["avg_cost"] = total_invested / total_qty
    pos["quantity"] = total_qty
    pos["invested"] = round(total_invested, 2)

    trade = {
        "date": report_date_str,
        "code": adj["code"],
        "side": "rebalance_buy",
        "quantity": qty,
        "price": round(price, 3),
        "cost": round(total_cost, 2),
        "fee": round(fee, 2),
        "reason": f"组合再平衡（偏离{adj['deviation_pct']:+.0f}%）",
    }
    new_trades.append(trade)
    trades.append(trade)
    logger.info(f"[再平衡] 加仓 {adj['code']} × {qty} @ {price:.3f}")


def _apply_rebalance_sell(
    state: dict,
    trades: List[dict],
    new_trades: List[dict],
    adj: dict,
    pos: dict,
    price: float,
    report_date_str: str,
) -> None:
    """执行再平衡减仓。"""
    from ..execution_layer.fees import calc_sell_fees

    qty = min(pos["quantity"], abs(adj["diff_qty"]))
    qty = int(qty / 100) * 100
    if qty < 100:
        return
    proceeds_before = qty * price
    fee = calc_sell_fees(proceeds_before)
    pnl_partial = (price - pos["avg_cost"]) * qty - fee

    state["cash"] += proceeds_before - fee
    state["total_pnl"] += pnl_partial
    state["total_fee"] += fee
    pos["quantity"] -= qty

    trade = {
        "date": report_date_str,
        "code": adj["code"],
        "side": "rebalance_sell",
        "quantity": qty,
        "price": round(price, 3),
        "proceeds": round(proceeds_before - fee, 2),
        "fee": round(fee, 2),
        "pnl": round(pnl_partial, 2),
        "reason": f"组合再平衡（偏离{adj['deviation_pct']:+.0f}%）",
    }
    new_trades.append(trade)
    trades.append(trade)
    logger.info(f"[再平衡] 减仓 {adj['code']} × {qty} @ {price:.3f}")
