#!/usr/bin/env python3
"""
盘中自动交易模块 — 基于技术信号的日内模拟交易

当 monitor_candidates.py 检测到建仓信号时，自动执行模拟买入，
盘中管理止损/止盈/跟踪止损，收盘前自动清仓。

=== 统一账户模式 ===
使用 simulated_trading 的共享 state.json 和 trades.json。
日内仓位通过 position["mode"] = "intraday" 与日频仓位（"daily"）区分。
check_positions() 只处理 mode="intraday" 的持仓。

用法：
  python3 scripts/intraday_trading.py check     # 检查持仓（盘中cron调用）
  python3 scripts/intraday_trading.py close_all # 收盘平仓
  python3 scripts/intraday_trading.py status    # 查看持仓状态
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("intraday")

BASE_DIR = Path("/opt/daily_stock_analysis")

# ── 导入共享状态模块 ──
sys.path.insert(0, str(BASE_DIR))
from simulated_trading import (load_state, save_state, load_trades, save_trades,
                                calc_buy_fees, calc_sell_fees, INITIAL_CAPITAL,
                                check_market_condition)

# ── 交易参数（日内专属） ──
MAX_POSITIONS = 2                # 最多同时持有2只日内（30K账户合理）
PER_TRADE_MAX = 8_000            # 单笔最大买入金额
MIN_TRADE = 3_000                # 最低买入金额
AUTO_CLOSE_TIME = "14:50"        # 收盘前10分钟强制平仓（匹配最后一轮cron）
NEW_POSITION_CUTOFF = "14:30"    # 14:30后不开新仓

# ── 风控参数（日内比日频更紧） ──
STOP_LOSS_PCT = -3.0             # -3% 止损
TAKE_PROFIT_PCT = 5.0            # +5% 止盈（全仓止盈）
TRAILING_ACTIVATE_PCT = 3.0      # 浮盈超过3%后启动跟踪止损
TRAILING_STOP_PCT = -2.0         # 跟踪止损：从最高点回撤2%平仓

# ── 信号 → 下单匹配 ──
# 哪些建仓信号会触发自动买入
AUTO_BUY_SIGNALS = {
    "放量突破MA20",   # 量价齐升突破均线
    "回踩MA20企稳",   # 回调到MA20反弹
    "早盘强势",       # 开盘半小时强势拉升
}


# ── 信号处理 ──

def maybe_open_position(code: str, name: str, signal: str, detail: str,
                        price: float, volume: int, vol_ratio: Optional[float]) -> Optional[Dict]:
    """
    根据盘中监控的建仓信号尝试开仓。
    返回 trade dict 如果成交，None 如果跳过。

    使用 shared simulated_trading state，注入 mode="intraday"。
    """
    state = load_state()

    # ── P0 大盘择时检查 ──
    market_state = check_market_condition()
    if market_state in ("caution", "danger"):
        log.info(f"  ⏸ [日内交易] 大盘状态={market_state}，不开新仓")
        return None

    # ── P1 震荡市假突破过滤 ──
    try:
        hmm_file = BASE_DIR / "quant_engine" / "hmm_output.json"
        is_oscillation = False
        if hmm_file.exists():
            hd = json.loads(hmm_file.read_text())
            current_state = hd.get("current_state")
            if current_state == 2:  # 震荡
                is_oscillation = True
                log.info(f"  [过滤] HMM状态=震荡，降低仓位权重")

        # 即使没有HMM，也可以从近期K线判断
        if not is_oscillation:
            try:
                from data_provider.data_cache import DataCache
                cache = DataCache()
                df = cache.get_kline(code)
                if df is not None and len(df) >= 5:
                    recent = df.tail(5)
                    amplitude = (recent["high"].max() - recent["low"].min()) / recent["close"].iloc[-1] * 100
                    if amplitude < 3.0 and recent["close"].std() / recent["close"].mean() < 0.015:
                        is_oscillation = True
                        log.info(f"  [过滤] {code} 5日振幅={amplitude:.1f}%<3%，判定为震荡，降低仓位")
            except Exception:
                pass
    except Exception:
        is_oscillation = False

    # ── 检查是否可开新仓 ──
    now = datetime.now()
    now_str = now.strftime("%H:%M")

    # 1. 是否在开仓时段内
    if now_str >= NEW_POSITION_CUTOFF:
        log.info(f"  ⏸ [日内交易] 超过{NEW_POSITION_CUTOFF}，不开新仓")
        return None

    # 2. 是否已持有该股（检查所有模式）
    if code in state["positions"]:
        log.info(f"  ⏸ [日内交易] 已持有 {code}，不重复开仓")
        return None

    # 3. 是否已达日内持仓上限（只统计当前 intraday 仓位）
    intraday_count = sum(1 for p in state["positions"].values() if p.get("mode") == "intraday")
    if intraday_count >= MAX_POSITIONS:
        log.info(f"  ⏸ [日内交易] 已达日内持仓上限 {MAX_POSITIONS}，跳过 {code}")
        return None

    # 4. 现金是否够最低交易
    if state["cash"] < MIN_TRADE:
        log.info(f"  ⏸ [日内交易] 现金不足，跳过 {code}")
        return None

    # ── 计算仓位 ──
    # 信号级别权重
    signal_weights = {
        "放量突破MA20": 1.0,   # 最强信号
        "早盘强势": 0.8,
        "回踩MA20企稳": 0.6,
    }
    weight = signal_weights.get(signal, 0.5)

    # 震荡市仓位缩减
    if is_oscillation:
        weight *= 0.5
        log.info(f"  [过滤] 震荡市权重减半 → {weight:.2f}")

    # 量比加成
    vol_boost = 1.0
    if vol_ratio:
        if vol_ratio >= 3:
            vol_boost = 1.2
        elif vol_ratio >= 2:
            vol_boost = 1.1

    # 计算买入金额
    buy_amount = min(PER_TRADE_MAX, state["cash"]) * weight * vol_boost
    buy_amount = max(MIN_TRADE, min(buy_amount, PER_TRADE_MAX))
    buy_amount = min(buy_amount, state["cash"])

    quantity = int(buy_amount / price / 100) * 100
    if quantity < 100:
        log.info(f"  ⏸ [日内交易] {code} 价格 {price:.2f}，不够买100股，跳过")
        return None

    cost = quantity * price
    fee = calc_buy_fees(cost)
    total_cost = cost + fee
    if total_cost > state["cash"]:
        quantity = int((state["cash"] - fee) / price / 100) * 100
        if quantity < 100:
            return None
        cost = quantity * price
        fee = calc_buy_fees(cost)
        total_cost = cost + fee

    # ── 执行买入（共享 state） ──
    state["cash"] -= total_cost
    state["total_fee"] += fee
    # total_pnl 不变（还未卖出）

    state["positions"][code] = {
        "name": name,
        "quantity": quantity,
        "entry_price": round(price, 3),
        "avg_cost": round(price, 3),
        "current_price": round(price, 3),
        "invested": round(cost, 2),
        "entry_time": now_str,
        "entry_signal": signal,
        "highest_price": price,
        "trailing_stop": None,
        "tp_level": 0,
        "status": "open",
        "mode": "intraday",
    }

    trade = {
        "date": now.strftime("%Y%m%d"),
        "time": now_str,
        "code": code,
        "name": name,
        "side": "buy",
        "quantity": quantity,
        "price": round(price, 3),
        "cost": round(total_cost, 2),
        "fee": round(fee, 2),
        "signal": signal,
        "mode": "intraday",
        "pnl": 0,
    }

    trades = load_trades()
    trades.append(trade)
    save_trades(trades)
    save_state(state)

    log.info(f"  ✅ [日内交易] 买入 {name}({code}) × {quantity} @ ¥{price:.3f} = ¥{total_cost:.0f} "
             f"（信号:{signal} 权重:{weight:.1f} 量比:{vol_ratio}）")

    return trade


def check_positions() -> List[Dict]:
    """
    检查所有日内持仓：止损/止盈/跟踪止损/收盘平仓。
    只处理 mode="intraday" 的仓位。

    返回成交列表。
    """
    state = load_state()
    now = datetime.now()
    now_str = now.strftime("%H:%M")
    today = now.strftime("%Y%m%d")
    closed_trades = []

    # 只检查 intraday 仓位
    intraday_codes = [code for code, pos in state["positions"].items()
                      if pos.get("mode") == "intraday"]
    if not intraday_codes:
        return []

    # 获取当前价格（从缓存 /tmp 或通过API重新拉取）
    prices = _fetch_current_prices(intraday_codes)
    if not prices:
        log.warning("  ⚠ [日内交易] 无法获取当前行情，跳过风控检查")
        return []

    is_auto_close = now_str >= AUTO_CLOSE_TIME

    for code in list(intraday_codes):
        pos = state["positions"].get(code)
        if not pos:
            continue
        current_price = prices.get(code, {}).get("price", 0)
        if current_price <= 0:
            continue
        # P3 滑点：卖出价下浮0.1%（更保守的风控判断）
        current_price = round(current_price * 0.999, 3)

        # 更新最高价（用于跟踪止损）
        if current_price > pos["highest_price"]:
            pos["highest_price"] = current_price

        entry = pos["entry_price"]
        pnl_pct = (current_price - entry) / entry * 100
        peak_return = (pos["highest_price"] - entry) / entry * 100

        action = None
        reason = ""

        # 条件①：收盘自动平仓
        if is_auto_close:
            action = "close"
            reason = f"收盘平仓（{AUTO_CLOSE_TIME}）"

        # 条件②：止盈
        elif pnl_pct >= TAKE_PROFIT_PCT:
            action = "take_profit"
            reason = f"止盈触发：{pnl_pct:+.1f}%（≥{TAKE_PROFIT_PCT}%）"

        # 条件③：止损
        elif pnl_pct <= STOP_LOSS_PCT:
            action = "stop_loss"
            reason = f"止损触发：{pnl_pct:+.1f}%（≤{STOP_LOSS_PCT}%）"

        # 条件④：跟踪止损（浮盈达标后，从最高点回撤）
        elif peak_return >= TRAILING_ACTIVATE_PCT:
            drawdown_from_peak = (current_price - pos["highest_price"]) / pos["highest_price"] * 100
            if drawdown_from_peak <= TRAILING_STOP_PCT:
                action = "trailing_stop"
                reason = f"跟踪止损：最高¥{pos['highest_price']:.3f} 回撤{drawdown_from_peak:.1f}%（≤{TRAILING_STOP_PCT}%）"

        if action:
            # 执行卖出
            qty = pos["quantity"]
            proceeds = qty * current_price
            fee = calc_sell_fees(proceeds)
            net_proceeds = proceeds - fee
            pnl = (current_price - entry) * qty - fee

            state["cash"] += net_proceeds
            state["total_fee"] += fee
            state["total_pnl"] += pnl

            trade = {
                "date": today,
                "time": now_str,
                "code": code,
                "name": pos.get("name", code),
                "side": action,
                "quantity": qty,
                "price": round(current_price, 3),
                "proceeds": round(net_proceeds, 2),
                "fee": round(fee, 2),
                "pnl": round(pnl, 2),
                "signal": pos.get("entry_signal", ""),
                "mode": "intraday",
                "reason": reason,
                "hold_time": pos.get("entry_time", ""),
            }
            closed_trades.append(trade)

            log.info(f"  {'🔴' if action in ('stop_loss','trailing_stop') else '🟢'} [日内交易] "
                     f"{action} {pos.get('name','')}({code}) × {qty} @ ¥{current_price:.3f} "
                     f"盈亏{pnl:+,.2f} — {reason}")

            del state["positions"][code]

    if closed_trades:
        trades = load_trades()
        trades.extend(closed_trades)
        save_trades(trades)
        save_state(state)

    return closed_trades


def _fetch_current_prices(codes: List[str]) -> Dict[str, Dict]:
    """从东方财富批量拉取实时行情"""
    if not codes:
        return {}
    import requests
    codes_str = ",".join(f"1.{c}" if c.startswith("6") else f"0.{c}" for c in codes)
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    params = {
        "fields": "f2,f3,f12,f14",
        "secids": codes_str,
        "fltt": 2,
        "invt": 2,
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/",
    }
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        data = r.json()
        result = {}
        for item in data.get("data", {}).get("diff", []):
            code = str(item.get("f12", ""))
            result[code] = {
                "code": code,
                "name": str(item.get("f14", "")),
                "price": item.get("f2", 0) or 0,
                "change_pct": item.get("f3", 0) or 0,
            }
        return result
    except Exception as e:
        log.error(f"  行情获取失败: {e}")
        return {}


def get_status_text() -> str:
    """生成日内交易状态文本（用于推送）"""
    state = load_state()
    lines = []
    lines.append(f"📊【日内交易】{datetime.now():%Y%m%d}")

    # 分离日内和日频持仓
    intraday_positions = {c: p for c, p in state.get("positions", {}).items()
                          if p.get("mode") == "intraday"}
    daily_positions = {c: p for c, p in state.get("positions", {}).items()
                       if p.get("mode") != "intraday"}

    lines.append(f"💰 可用资金：¥{state['cash']:,.0f}")
    lines.append(f"📈 总权益：¥{state['total_equity']:,.2f}")

    # 日内持仓
    if intraday_positions:
        lines.append("")
        lines.append("📋 日内持仓：")
        for code, pos in intraday_positions.items():
            pnl_pct = "?"
            try:
                pnl_pct = f"{(pos['current_price'] - pos['entry_price']) / pos['entry_price'] * 100:+.1f}%"
            except Exception:
                pass
            lines.append(f"  {pos.get('name',code)}({code}) {pos['quantity']}股 "
                         f"@ ¥{pos['entry_price']:.3f} {pos.get('entry_time','')}入 "
                         f"信号:{pos.get('entry_signal','')} {pnl_pct}")
    else:
        lines.append("📋 日内持仓：空仓")

    if daily_positions:
        lines.append("📋 日频持仓：")
        for code, pos in daily_positions.items():
            lines.append(f"  {code} {pos['quantity']}股 @ ¥{pos['avg_cost']:.3f}")

    lines.append("")
    lines.append(f"⚙️ 止损-{abs(STOP_LOSS_PCT)}% | 止盈+{TAKE_PROFIT_PCT}% | "
                 f"跟踪止损从高点回撤{abs(TRAILING_STOP_PCT)}% | {AUTO_CLOSE_TIME}收盘平仓")
    lines.append(f"📊 最多同时持有{MAX_POSITIONS}只日内，单笔≤¥{PER_TRADE_MAX:,}")

    return "\n".join(lines)


def close_all_positions() -> List[Dict]:
    """收盘强制平仓所有日内持仓"""
    return check_positions()  # AUTO_CLOSE_TIME 逻辑在 check_positions 中


# ── 主入口 ──

def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "check"

    if action == "check":
        closed = check_positions()
        if closed:
            log.info(f"  → 本轮回合平仓 {len(closed)} 笔")
        return

    elif action == "close_all":
        closed = close_all_positions()
        if closed:
            log.info(f"收盘平仓 {len(closed)} 笔")
        else:
            log.info("无日内持仓需要平仓")
        return

    elif action == "status":
        print(get_status_text())
        return

    else:
        print(f"未知操作: {action}")
        print("用法: python3 scripts/intraday_trading.py [check|close_all|status]")
        sys.exit(1)


if __name__ == "__main__":
    main()
