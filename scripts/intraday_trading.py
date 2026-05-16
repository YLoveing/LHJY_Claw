#!/usr/bin/env python3
"""
盘中信号交易模块 — 技术信号驱动的短线交易

=== A股合规设计（T+1制度） ===
本模块遵守A股T+1交易制度：
- 盘中检测到的建仓信号 → 写入待执行队列（pending_signals.json）
- 次日上午 09:25 以开盘价统一执行待执信号
- 持仓按日频进行止损/止盈/跟踪止损管理（无当日强制平仓）

=== 统一账户模式 ===
使用 simulated_trading 的共享 state.json 和 trades.json。
日内信号产生的仓位通过 position["mode"] = "intraday" 与日频仓位（"daily"）区分。
check_positions() 只处理 mode="intraday" 的持仓。

用法：
  python3 scripts/intraday_trading.py execute    # 执行待执信号（09:25由run_and_send.sh调用）
  python3 scripts/intraday_trading.py check      # 检查持仓风控（盘中cron调用）
  python3 scripts/intraday_trading.py status     # 查看持仓状态
  python3 scripts/intraday_trading.py record     # ❌ 仅限被monitor_candidates.py导入调用record_signal()
                                                 #   CLI不能直接用，因为信号参数需从API实时获取
"""

import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("intraday")

BASE_DIR = Path("/opt/daily_stock_analysis")
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── 导入共享状态模块 ──
sys.path.insert(0, str(BASE_DIR))
from scripts.trading_calendar import eastmoney_secid

from simulated_trading import (load_state, save_state, load_trades, save_trades,
                                calc_buy_fees, calc_sell_fees, INITIAL_CAPITAL,
                                check_market_condition, MAX_POSITIONS as SHARED_MAX_POS)

# ── 交易参数 ──
MAX_POSITIONS = 2                # 最多同时持有2只（30K账户合理）
PER_TRADE_MAX = 8_000            # 单笔最大买入金额
MIN_TRADE = 3_000                # 最低买入金额
NEW_SIGNAL_CUTOFF = "14:30"      # 14:30后不产生新信号（来不及次日执行）

# ── 风控参数（日内信号持仓比日频主策略更紧） ──
STOP_LOSS_PCT = -3.0             # -3% 止损（硬止损）
TAKE_PROFIT_PCT = 5.0            # +5% 止盈（全仓止盈）
TRAILING_ACTIVATE_PCT = 3.0      # 浮盈超过3%后启动跟踪止损
TRAILING_STOP_PCT = -4.0         # 跟踪止损：从最高点回撤4%平仓（放宽避免被扫）

# ── 待执行队列文件 ──
PENDING_FILE = DATA_DIR / "pending_signals.json"

# ── 信号 → 权重匹配 ──
AUTO_BUY_SIGNALS = {
    "放量突破MA20",   # 量价齐升突破均线
    "回踩MA20企稳",   # 回调到MA20反弹
    "早盘强势",       # 开盘半小时强势拉升
}

SIGNAL_WEIGHTS = {
    "放量突破MA20": 1.0,   # 最强信号
    "早盘强势": 0.8,
    "回踩MA20企稳": 0.6,
}


# ── 待执行队列管理 ──

def _load_pending() -> List[Dict]:
    """加载待执行信号队列"""
    if PENDING_FILE.exists():
        try:
            return json.loads(PENDING_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_pending(signals: List[Dict]):
    """保存待执行信号队列"""
    PENDING_FILE.write_text(json.dumps(signals, ensure_ascii=False, indent=2))


def record_signal(code: str, name: str, signal: str, detail: str,
                  price: float, volume: int, vol_ratio: Optional[float]) -> Optional[Dict]:
    """
    记录建仓信号到待执行队列（不立即买入）。
    由 monitor_candidates.py 在盘中检测到信号时调用。
    信号将在次日上午 09:25 由 execute_pending_signals() 执行。

    返回信号记录 dict（不是交易记录）。
    """
    now = datetime.now()
    now_str = now.strftime("%H:%M")
    today = now.strftime("%Y%m%d")

    # ── 开仓时段检查 ──
    if now_str >= NEW_SIGNAL_CUTOFF:
        log.info(f"  ⏸ [信号记录] 超过{NEW_SIGNAL_CUTOFF}，不产生新信号")
        return None

    # ── 大盘择时检查 ──
    market_state = check_market_condition()
    if market_state in ("caution", "danger"):
        log.info(f"  ⏸ [信号记录] 大盘状态={market_state}，不记录信号")
        return None

    # ── 是否已有待执信号（去重） ──
    pending = _load_pending()
    existing_codes = {s["code"]: s for s in pending if s.get("status") == "pending"}
    if code in existing_codes:
        log.info(f"  ⏸ [信号记录] {code} 已有待执信号，不重复记录")
        return None

    # ── 是否已持有该股 ──
    state = load_state()
    if code in state.get("positions", {}):
        log.info(f"  ⏸ [信号记录] 已持有 {code}，不重复记录")
        return None

    # ── 震荡市过滤 ──
    is_oscillation = _check_oscillation(code)
    weight = SIGNAL_WEIGHTS.get(signal, 0.5)
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

    # ── 流动性过滤（P3） ──
    try:
        from data_provider.data_cache import DataCache
        cache = DataCache()
        df = cache.get_kline(code)
        if df is not None and len(df) > 1:
            recent_avg_amount = (df["volume"].tail(5).mean() * df["close"].tail(5).mean()) / 1e8
            if recent_avg_amount < 0.5:
                log.info(f"  ⏸ [信号记录] {code} 日均成交额¥{recent_avg_amount:.2f}亿<0.5亿，跳过")
                return None
    except Exception:
        pass

    # ── 构造信号记录 ──
    signal_record = {
        "code": code,
        "name": name,
        "signal": signal,
        "detail": detail,
        "record_price": round(price, 3),
        "record_time": now_str,
        "record_date": today,
        "weight": round(weight, 2),
        "vol_boost": round(vol_boost, 2),
        "vol_ratio": vol_ratio,
        "status": "pending",     # pending | executed | expired
        "created_at": now.isoformat(),
    }

    pending.append(signal_record)
    _save_pending(pending)

    log.info(f"  📝 [信号记录] {name}({code}) 信号:{signal} "
             f"价¥{price:.3f} 权重:{weight:.1f} 等待次日执行")

    return signal_record


def _check_oscillation(code: str) -> bool:
    """检查个股是否处于震荡状态"""
    try:
        # HMM 状态检查
        hmm_file = BASE_DIR / "quant_engine" / "hmm_output.json"
        if hmm_file.exists():
            hd = json.loads(hmm_file.read_text())
            if hd.get("current_state") == 2:  # 震荡
                return True

        # K线振幅检查
        from data_provider.data_cache import DataCache
        cache = DataCache()
        df = cache.get_kline(code)
        if df is not None and len(df) >= 5:
            recent = df.tail(5)
            amplitude = (recent["high"].max() - recent["low"].min()) / recent["close"].iloc[-1] * 100
            if amplitude < 3.0 and recent["close"].std() / recent["close"].mean() < 0.015:
                return True
    except Exception:
        pass
    return False


def execute_pending_signals() -> List[Dict]:
    """
    执行待执行队列中的建仓信号。
    在 09:25 早盘分析时调用，以开盘价买入。

    返回成交 trade 列表。
    """
    now = datetime.now()
    pending = _load_pending()
    pending_active = [s for s in pending if s.get("status") == "pending"]

    if not pending_active:
        log.info("  [信号执行] 无待执信号")
        return []

    log.info(f"  [信号执行] 待执信号 {len(pending_active)} 个")

    # 获取开盘价（如果没有开盘价就用记录价）
    codes = [s["code"] for s in pending_active]
    open_prices = _fetch_open_prices(codes)

    state = load_state()
    trades = load_trades()
    today = now.strftime("%Y%m%d")
    executed = []

    for s in pending_active:
        code = s["code"]
        name = s.get("name", code)

        # ── 检查是否还可开仓 ──
        if code in state.get("positions", {}):
            log.info(f"  ⏸ [信号执行] {code} 已被其他策略买入，跳过")
            s["status"] = "expired"
            continue

        # 检查共享持仓上限（日频+信号合计 ≤ SHARED_MAX_POS=3）
        if len(state["positions"]) >= SHARED_MAX_POS:
            log.info(f"  ⏸ [信号执行] 已达共享持仓上限 {SHARED_MAX_POS}，跳过 {code}")
            s["status"] = "expired"
            continue

        # 检查信号交易内部上限（最多2只）
        intraday_count = sum(1 for p in state["positions"].values()
                             if p.get("mode") == "intraday")
        if intraday_count >= MAX_POSITIONS:
            log.info(f"  ⏸ [信号执行] 已达信号交易上限 {MAX_POSITIONS}，跳过 {code}")
            s["status"] = "expired"
            continue

        if state["cash"] < MIN_TRADE:
            log.info(f"  ⏸ [信号执行] 现金不足，跳过 {code}")
            s["status"] = "expired"
            continue

        # 决定成交价
        price = s.get("record_price", 0)
        if code in open_prices and open_prices[code] > 0:
            price = open_prices[code]

        # P3 滑点：买入上浮
        buy_amount = s["weight"] * s.get("vol_boost", 1.0)
        price = price * 1.001  # +0.1% 滑点

        # 计算买入数量
        buy_amount = min(PER_TRADE_MAX, state["cash"]) * buy_amount
        buy_amount = max(MIN_TRADE, min(buy_amount, PER_TRADE_MAX))
        buy_amount = min(buy_amount, state["cash"])

        quantity = int(buy_amount / price / 100) * 100
        if quantity < 100:
            log.info(f"  ⏸ [信号执行] {code} 不够买100股，跳过")
            s["status"] = "expired"
            continue

        cost = quantity * price
        fee = calc_buy_fees(cost)
        total_cost = cost + fee
        if total_cost > state["cash"]:
            quantity = int((state["cash"] - fee) / price / 100) * 100
            if quantity < 100:
                s["status"] = "expired"
                continue
            cost = quantity * price
            fee = calc_buy_fees(cost)
            total_cost = cost + fee

        # ── 执行买入 ──
        state["cash"] -= total_cost
        state["total_fee"] += fee

        signal = s.get("signal", "系统触发")
        state["positions"][code] = {
            "name": name,
            "quantity": quantity,
            "entry_price": round(price, 3),
            "avg_cost": round(price, 3),
            "current_price": round(price, 3),
            "invested": round(cost, 2),
            "entry_date": today,
            "entry_time": today,  # 兼容旧state中可能存在的字段
            "entry_signal": signal,
            "highest_price": price,
            "trailing_stop": None,
            "tp_level": 0,
            "status": "open",
            "mode": "intraday",
        }

        trade = {
            "date": today,
            "time": "09:25",
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

        trades.append(trade)
        s["status"] = "executed"
        s["executed_at"] = now.isoformat()
        executed.append(trade)

        log.info(f"  ✅ [信号执行] 买入 {name}({code}) × {quantity} @ ¥{price:.3f} = ¥{total_cost:.0f} "
                 f"（信号:{signal} 来源:{s.get('record_date','')} {s.get('record_time','')}）")

    # 保存
    _save_pending(pending)
    save_trades(trades)
    save_state(state)

    return executed


# ── 共享 DataFetcherManager 实例（P1: 取代直调 push2 API）──
_MANAGER_INSTANCE = None


def _get_manager():
    """获取或创建 DataFetcherManager 单例"""
    global _MANAGER_INSTANCE
    if _MANAGER_INSTANCE is None:
        try:
            # 从 base.py 导入并初始化
            import importlib
            dp_base = importlib.import_module("data_provider.base")
            manager = dp_base.DataFetcherManager()
            _MANAGER_INSTANCE = manager
        except Exception as e:
            log.warning(f"DataFetcherManager 初始化失败: {e}，回退到直连")
            _MANAGER_INSTANCE = False  # 标记回退
    return _MANAGER_INSTANCE if _MANAGER_INSTANCE else None


def _fetch_open_prices(codes: List[str]) -> Dict[str, float]:
    """获取指定股票的开盘价（优先 DataFetcherManager，失败细粒度回退到 push2）"""
    if not codes:
        return {}

    result = {}
    failed_codes = []

    manager = _get_manager()
    if manager:
        for code in codes:
            try:
                quote = manager.get_realtime_quote(code)
                if quote and quote.open_price is not None:
                    result[code] = float(quote.open_price)
                    continue
            except Exception as e:
                log.debug(f"  {code} 开盘价获取失败(manager): {e}")
            failed_codes.append(code)
    else:
        failed_codes = list(codes)

    # 细粒度回退：只回退 manager 失败的 code
    if failed_codes:
        log.info(f"  → {len(failed_codes)}/{len(codes)} 只回退到 push2 获取开盘价")
        _fallback_open_prices(failed_codes, result)

    return result


def _fallback_open_prices(codes: List[str], result: Dict[str, float]):
    """回退：通过 push2 API 批量获取开盘价（原地补充 result）"""
    if not codes:
        return
    import requests
    codes_str = ",".join(eastmoney_secid(c) for c in codes)
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    params = {"fields": "f2,f17,f12", "secids": codes_str, "fltt": 2, "invt": 2}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        data = r.json()
        for item in data.get("data", {}).get("diff", []):
            code = str(item.get("f12", ""))
            if code not in result:
                open_price = item.get("f17", 0) or 0
                result[code] = float(open_price)
    except Exception as e:
        log.error(f"  push2 开盘价回退失败: {e}")


def check_positions() -> List[Dict]:
    """
    检查所有日内持仓：止损/止盈/跟踪止损。
    只处理 mode="intraday" 的仓位。
    ⚠️ A股T+1制度下，当日不强制平仓，只做检查。

    返回成交列表。
    """
    state = load_state()
    now = datetime.now()
    today = now.strftime("%Y%m%d")
    closed_trades = []

    intraday_codes = [code for code, pos in state["positions"].items()
                      if pos.get("mode") == "intraday"]
    if not intraday_codes:
        return []

    # 获取当前价格
    prices = _fetch_current_prices(intraday_codes)
    if not prices:
        log.warning("  ⚠ [日内交易] 无法获取当前行情，跳过风控检查")
        return []

    for code in list(intraday_codes):
        pos = state["positions"].get(code)
        if not pos:
            continue
        current_price = prices.get(code, {}).get("price", 0)
        if current_price <= 0:
            continue
        # P3 滑点：检查时卖出价下浮0.1%（更保守的风控判断）
        current_price = round(current_price * 0.999, 3)

        # 更新最高价（用于跟踪止损）
        if current_price > pos["highest_price"]:
            pos["highest_price"] = current_price

        entry = pos["entry_price"]
        pnl_pct = (current_price - entry) / entry * 100
        peak_return = (pos["highest_price"] - entry) / entry * 100

        action = None
        reason = ""

        # 条件①：止盈
        if pnl_pct >= TAKE_PROFIT_PCT:
            action = "take_profit"
            reason = f"止盈触发：{pnl_pct:+.1f}%（≥{TAKE_PROFIT_PCT}%）"

        # 条件②：止损
        elif pnl_pct <= STOP_LOSS_PCT:
            action = "stop_loss"
            reason = f"止损触发：{pnl_pct:+.1f}%（≤{STOP_LOSS_PCT}%）"

        # 条件③：跟踪止损（浮盈达标后，从最高点回撤）
        elif peak_return >= TRAILING_ACTIVATE_PCT:
            drawdown_from_peak = (current_price - pos["highest_price"]) / pos["highest_price"] * 100
            if drawdown_from_peak <= TRAILING_STOP_PCT:
                action = "trailing_stop"
                reason = f"跟踪止损：最高¥{pos['highest_price']:.3f} 回撤{drawdown_from_peak:.1f}%（≤{TRAILING_STOP_PCT}%）"

        if action:
            # 执行卖出（模拟），盈亏次日结算到现金
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
                "time": now.strftime("%H:%M"),
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
                "hold_days": (now - datetime.strptime(pos.get("entry_date", today), "%Y%m%d")).days
                        if pos.get("entry_date") and len(pos["entry_date"]) == 8 else 0,
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
    """获取指定股票的实时行情（优先 DataFetcherManager，失败细粒度回退到 push2）"""
    if not codes:
        return {}

    result = {}
    failed_codes = []

    manager = _get_manager()
    if manager:
        for code in codes:
            try:
                quote = manager.get_realtime_quote(code)
                if quote and quote.price is not None:
                    result[code] = {
                        "code": code,
                        "name": quote.name or "",
                        "price": float(quote.price),
                        "change_pct": float(quote.change_pct) if quote.change_pct is not None else 0.0,
                    }
                    continue
            except Exception as e:
                log.debug(f"  {code} 行情获取失败(manager): {e}")
            failed_codes.append(code)
    else:
        failed_codes = list(codes)

    # 细粒度回退：只回退 manager 失败的 code
    if failed_codes:
        log.info(f"  → {len(failed_codes)}/{len(codes)} 只回退到 push2 获取行情")
        _fallback_current_prices(failed_codes, result)

    return result


def _fallback_current_prices(codes: List[str], result: Dict[str, Dict]):
    """回退：通过 push2 API 批量获取实时行情（原地补充 result）"""
    if not codes:
        return
    import requests
    codes_str = ",".join(eastmoney_secid(c) for c in codes)
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    params = {"fields": "f2,f3,f12,f14", "secids": codes_str, "fltt": 2, "invt": 2}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        data = r.json()
        for item in data.get("data", {}).get("diff", []):
            code = str(item.get("f12", ""))
            if code not in result:
                result[code] = {
                    "code": code,
                    "name": str(item.get("f14", "")),
                    "price": item.get("f2", 0) or 0,
                    "change_pct": item.get("f3", 0) or 0,
                }
    except Exception as e:
        log.error(f"  push2 行情回退失败: {e}")


def get_status_text() -> str:
    """生成日内信号交易状态文本（用于推送）"""
    state = load_state()
    lines = []
    lines.append(f"📊【日内信号交易】{datetime.now():%Y%m%d}")

    intraday_positions = {c: p for c, p in state.get("positions", {}).items()
                          if p.get("mode") == "intraday"}
    daily_positions = {c: p for c, p in state.get("positions", {}).items()
                       if p.get("mode") != "intraday"}

    lines.append(f"💰 可用资金：¥{state['cash']:,.0f}")
    lines.append(f"📈 总权益：¥{state['total_equity']:,.2f}")

    # 日内持仓
    if intraday_positions:
        lines.append("")
        lines.append("📋 日内信号持仓：")
        for code, pos in intraday_positions.items():
            lines.append(f"  {pos.get('name',code)}({code}) {pos['quantity']}股 "
                         f"@ ¥{pos['entry_price']:.3f} 信号:{pos.get('entry_signal','')}")
    else:
        lines.append("📋 日内信号持仓：空仓")

    # 待执行信号
    pending = _load_pending()
    pending_active = [s for s in pending if s.get("status") == "pending"]
    if pending_active:
        lines.append("")
        lines.append("📋 待执行信号（下次开盘执行）：")
        for s in pending_active:
            lines.append(f"  {s.get('name','')}({s['code']}) {s['signal']} "
                         f"记录于{s.get('record_time','')} ¥{s.get('record_price','?')}")
    else:
        lines.append("📋 待执行信号：无")

    if daily_positions:
        lines.append("📋 日频主策略持仓：")
        for code, pos in daily_positions.items():
            lines.append(f"  {code} {pos['quantity']}股 @ ¥{pos['avg_cost']:.3f}")

    lines.append("")
    lines.append(f"⚙️ 止损-{abs(STOP_LOSS_PCT)}% | 止盈+{TAKE_PROFIT_PCT}% | "
                 f"跟踪止损从高点回撤{abs(TRAILING_STOP_PCT)}% | 信号→次日开盘执行")
    lines.append(f"📊 最多同时持有{MAX_POSITIONS}只，单笔≤¥{PER_TRADE_MAX:,}")

    return "\n".join(lines)


# ── 主入口 ──

def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "check"

    if action == "record":
        log.info("[日内信号交易] record 模式需由 monitor_candidates.py 调用 record_signal()")
        return

    elif action == "execute":
        executed = execute_pending_signals()
        log.info(f"  → 执行 {len(executed)} 笔")
        return

    elif action == "check":
        closed = check_positions()
        if closed:
            log.info(f"  → 本轮回合平仓 {len(closed)} 笔")
        return

    elif action == "status":
        print(get_status_text())
        return

    else:
        print(f"未知操作: {action}")
        print("用法: python3 scripts/intraday_trading.py [record|execute|check|status]")
        sys.exit(1)


if __name__ == "__main__":
    main()
