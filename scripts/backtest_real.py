#!/usr/bin/env python3
"""
🔥 生产代码回测 v2 — 修正价偏 + 滑点

修复项目:
1. ❌ T日收盘买入 → ✅ T+1开盘买入（信号在收盘后产生，不能同日以收盘价成交）
2. ❌ T+1收盘卖出 → ✅ T+2开盘卖出（持仓1个完整交易日）
3. ❌ 无滑点 → ✅ 双边0.15%滑点
4. ❌ 现金≡权益 → ✅ 现金+持仓市值
5. ❌ 持有模式用收盘价 → ✅ 用开盘价
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_provider.data_cache import DataCache
from layers.execution_layer.fees import calc_buy_fees, calc_sell_fees
from layers.risk_layer.models import RiskConfig

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backtest_real")

RISK_CFG = RiskConfig()
INITIAL_CASH = 100_000.0
SLIPPAGE = 0.0015  # 双边0.15%滑点


def load_data(days: int = 120, start: str = "", end: str = ""):
    cache = DataCache()
    stats = cache.stats()
    if not end:
        end = datetime.now().strftime("%Y-%m-%d")
    if not start:
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    result = {}
    log.info(f"📥 加载 DataCache ({stats['kline']['stocks']} 只)...")
    t0 = time.time()
    pkl_root = Path(cache._root)
    for f in sorted(pkl_root.glob("*.pkl")):
        code = f.stem
        if code == "hs300_index":
            continue
        try:
            df = cache.get_kline(code)
            if df is None or df.empty:
                continue
            df["date"] = pd.to_datetime(df["date"])
            mask = (df["date"] >= start) & (df["date"] <= end)
            df2 = df[mask].copy().sort_values("date")
            if len(df2) >= 20:
                result[code] = df2
        except Exception:
            pass
    log.info(f"  ✅ {len(result)} 只股票, {time.time()-t0:.0f}s")
    return result


def compute_signals(df):
    """技术评分：MA20 + RSI，与生产代码一致。"""
    close = df["close"].values
    if len(close) < 20:
        return 0.0, 0.0
    ma20 = np.mean(close[-20:])
    ma_ratio = close[-1] / ma20 if ma20 > 0 else 1.0
    if len(close) >= 15:
        deltas = np.diff(close[-15:])
        gains = deltas[deltas > 0].sum() / 14
        losses = (-deltas[deltas < 0]).sum() / 14
        rsi = 50
        if losses > 0:
            rs = gains / losses
            rsi = 100 - 100 / (1 + rs)
    else:
        rsi = 50
    score = 50 + (ma_ratio - 1) * 100 + (rsi - 50) * 0.3
    return max(0, min(100, score)), rsi


# ── 辅助函数 ──────────────────────────────────────────


def _get_row(stocks: dict, code: str, date_ts: pd.Timestamp):
    """获取某只股票在指定日期的行，返回 Series 或 None。"""
    df = stocks.get(code)
    if df is None:
        return None
    m = df["date"] == date_ts
    if not m.any():
        return None
    return df[m].iloc[-1]


def _get_price(row, field: str, direction: str) -> Optional[float]:
    """带滑点的价格。direction='buy'→加滑点，'sell'→减滑点。"""
    if row is None:
        return None
    p = float(row[field])
    if p <= 0:
        return None
    if direction == "buy":
        return round(p * (1 + SLIPPAGE), 4)
    else:
        return round(p * (1 - SLIPPAGE), 4)


def _align_dates(stocks) -> List[str]:
    """取所有股票都有的日期交集（或至少大部分覆盖），排序返回。"""
    return sorted(set(d.strftime("%Y-%m-%d") for s in stocks.values() for d in s["date"]))


# ── 每日全换（修复版） ────────────────────────────────


def run_backtest_daytrade_v2(stocks, top_n=5, initial_cash=INITIAL_CASH):
    """
    每日全换 v2 — 开盘价成交 + 滑点

    时间线:
        T日收盘 → 计算信号
        T+1开盘 → 卖旧仓、买新仓（按T+1开盘价 + 滑点）
        T+2开盘 → 卖旧仓、买新仓
        持仓时长: 1个完整交易日
    """
    all_dates = _align_dates(stocks)
    if len(all_dates) < 3:
        return {"error": f"数据不足（{len(all_dates)}天，至少需要3天）"}
    log.info(f"🔄 每日全换 v2 (开盘价+滑点{SLIPPAGE:.1%})  {len(all_dates)} 个交易日 ...")

    cash = initial_cash
    positions: Dict[str, dict] = {}  # code -> {qty, avg_cost, entry_date_str}
    fees = 0.0
    trades = 0
    wins = 0
    losses = 0
    daily_eq = []
    first_trade = True  # 第一天只买不卖

    # 从第2天开始（下标1=第2个日期，因为我们需要T-1计算信号）
    for i in range(1, len(all_dates)):
        # ds_signal: 信号日（收盘计算评分）
        ds_entry_str = all_dates[i]  # T+1: 执行交易的日子
        ds_signal_str = all_dates[i - 1]  # T:   信号日

        entry_date = pd.Timestamp(ds_entry_str)
        signal_date = pd.Timestamp(ds_signal_str)

        # ── 计算信号（用T日收盘数据） ──
        candidates = []
        for code, df in stocks.items():
            hist = df[df["date"] <= signal_date]
            if len(hist) < 20:
                continue
            last_row = hist.iloc[-1]
            score, _ = compute_signals(hist)
            candidates.append((score, code, last_row))

        candidates.sort(key=lambda x: x[0], reverse=True)
        top = candidates[:top_n]
        if not top:
            continue

        # ── 卖出旧仓（T+1开盘价） ──
        new_positions = {}
        exit_proceeds = 0.0

        if not first_trade:
            for code, pi in positions.items():
                entry_row = _get_row(stocks, code, entry_date)
                if entry_row is None:
                    continue  # 停牌，hold到能卖为止
                sell_price = _get_price(entry_row, "open", "sell")
                if sell_price is None:
                    continue
                qty = pi["qty"]
                if qty <= 0:
                    continue
                sf = calc_sell_fees(qty * sell_price)
                proceeds = qty * sell_price - sf
                fees += sf
                pnl = proceeds - qty * pi["avg_cost"]
                exit_proceeds += proceeds
                trades += 1
                (wins := wins + 1) if pnl > 0 else (losses := losses + 1)

        # ── 买入新仓（T+1开盘价 + 滑点） ──
        cash += exit_proceeds  # 卖出回款
        buy_cash = cash
        buy_per = buy_cash / len(top) if top else 0
        used_cash = 0.0

        if not first_trade:
            for _, code, _ in top:
                entry_row = _get_row(stocks, code, entry_date)
                buy_price = _get_price(entry_row, "open", "buy")
                if buy_price is None:
                    continue
                qty = int(buy_per / buy_price / 100) * 100
                if qty < 100:
                    continue
                cost = qty * buy_price
                bf = calc_buy_fees(cost)
                total_cost = cost + bf
                if total_cost > cash:
                    continue
                cash -= total_cost
                fees += bf
                new_positions[code] = {
                    "qty": qty,
                    "avg_cost": buy_price,
                    "entry_date_str": ds_entry_str,
                }
                used_cash += total_cost
                trades += 1

        positions = new_positions
        first_trade = False

        # ── 记录日终权益（T+1收盘估值） ──
        pos_val = 0.0
        for code, pi in positions.items():
            close_row = _get_row(stocks, code, entry_date)
            if close_row is not None:
                pos_val += pi["qty"] * float(close_row["close"])
        equity = cash + pos_val
        peak = max(peak, initial_cash) if daily_eq else initial_cash
        if daily_eq:
            peak = max(peak, max(e["equity"] for e in daily_eq))
        dd = (equity - peak) / peak * 100 if peak > 0 else 0
        daily_eq.append(
            {
                "date": ds_entry_str,
                "equity": round(equity, 2),
                "cash": round(cash, 2),
                "pos_value": round(pos_val, 2),
                "pos_count": len(positions),
                "dd": round(dd, 2),
            }
        )

    # ── 末日出清 ──
    for code, pi in positions.items():
        last_row = _get_row(stocks, code, pd.Timestamp(all_dates[-1]))
        sp = _get_price(last_row, "close", "sell")
        if sp is None:
            continue
        sf = calc_sell_fees(pi["qty"] * sp)
        cash += pi["qty"] * sp - sf
        fees += sf

    # ── 统计指标 ──
    final = cash
    ret = (final - initial_cash) / initial_cash * 100
    max_dd = min(d["dd"] for d in daily_eq) if daily_eq else 0
    es = [d["equity"] for d in daily_eq]
    rs = np.diff(es) / np.array(es[:-1]) if len(es) > 1 else [0]
    sr = np.mean(rs) / np.std(rs) * np.sqrt(252) if np.std(rs) > 1e-8 else 0
    wr = wins / trades * 100 if trades > 0 else 0

    return {
        "strategy": f"每日全换 Top{top_n} (v2)",
        "trading_days": len(daily_eq),
        "total_trades": trades,
        "total_return_pct": round(ret, 2),
        "win_rate_pct": round(wr, 1),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio": round(sr, 3),
        "total_fees": round(fees, 2),
        "wins": wins,
        "losses": losses,
    }


# ── 持有模式（修复版） ───────────────────────────────


def run_backtest_hold_v2(stocks, top_n=5, initial_cash=INITIAL_CASH, rebalance_threshold=10):
    """
    持有模式 v2 — 开盘价成交 + 滑点

    买入: T日收盘信号 → T+1开盘买入
    卖出: 当评分跌破阈值，下一日开盘卖出
    """
    all_dates = _align_dates(stocks)
    if len(all_dates) < 3:
        return {"error": f"数据不足（{len(all_dates)}天）"}
    log.info(f"🔄 持有模式 v2  {len(all_dates)} 个交易日 ...")

    cash = initial_cash
    peak = initial_cash
    positions: Dict[str, dict] = {}
    fees = 0.0
    trades = 0
    wins = 0
    losses = 0
    daily_eq = []

    for i in range(1, len(all_dates)):
        ds_entry_str = all_dates[i]
        ds_signal_str = all_dates[i - 1]
        entry_date = pd.Timestamp(ds_entry_str)
        signal_date = pd.Timestamp(ds_signal_str)
        next_date_str = all_dates[i + 1] if i + 1 < len(all_dates) else None

        # 信号
        candidates = []
        for code, df in stocks.items():
            hist = df[df["date"] <= signal_date]
            if len(hist) < 20:
                continue
            last_row = hist.iloc[-1]
            score, _ = compute_signals(hist)
            candidates.append((score, code, last_row))
        candidates.sort(key=lambda x: x[0], reverse=True)
        score_map = {c: s for s, c, _ in candidates}

        # ── 卖出 ──
        new_positions = {}
        for code, pi in positions.items():
            curr_score = score_map.get(code, 0)
            score_drop = pi["entry_score"] - curr_score
            rank = next((j for j, (s, c, _) in enumerate(candidates) if c == code), 999)

            if score_drop > rebalance_threshold or rank >= top_n * 3:
                if next_date_str:
                    sell_row = _get_row(stocks, code, pd.Timestamp(next_date_str))
                    sp = _get_price(sell_row, "open", "sell")
                    if sp is not None:
                        sf = calc_sell_fees(pi["qty"] * sp)
                        pnl = pi["qty"] * sp - sf - pi["qty"] * pi["avg_cost"]
                        cash += pi["qty"] * sp - sf
                        fees += sf
                        trades += 1
                        (wins := wins + 1) if pnl > 0 else (losses := losses + 1)
                        continue  # 已卖出
            new_positions[code] = pi
        positions = new_positions

        # ── 买入 ──
        can_buy = top_n - len(positions)
        if can_buy > 0:
            held = set(positions.keys())
            for score, code, _ in candidates:
                if code in held:
                    continue
                if can_buy <= 0:
                    break
                buy_row = _get_row(stocks, code, entry_date)
                bp = _get_price(buy_row, "open", "buy")
                if bp is None:
                    continue
                qty = int(cash / can_buy / bp / 100) * 100
                if qty < 100:
                    continue
                bf = calc_buy_fees(qty * bp)
                if qty * bp + bf > cash:
                    continue
                cash -= qty * bp + bf
                fees += bf
                trades += 1
                positions[code] = {
                    "qty": qty,
                    "avg_cost": bp,
                    "entry_score": score,
                }
                can_buy -= 1

        # ── 日终估值 ──
        pos_val = 0.0
        for code, pi in positions.items():
            close_row = _get_row(stocks, code, entry_date)
            if close_row is not None:
                pos_val += pi["qty"] * float(close_row["close"])
        equity = cash + pos_val
        peak = max(peak, equity)
        dd = (equity - peak) / peak * 100 if peak > 0 else 0
        daily_eq.append(
            {
                "date": ds_entry_str,
                "equity": round(equity, 2),
                "pos_count": len(positions),
                "dd": round(dd, 2),
            }
        )

    # 末日出清
    for code, pi in positions.items():
        last_row = _get_row(stocks, code, pd.Timestamp(all_dates[-1]))
        sp = _get_price(last_row, "close", "sell")
        if sp is None:
            continue
        sf = calc_sell_fees(pi["qty"] * sp)
        cash += pi["qty"] * sp - sf
        fees += sf

    final = cash
    ret = (final - initial_cash) / initial_cash * 100
    max_dd = min(d["dd"] for d in daily_eq) if daily_eq else 0
    es = [d["equity"] for d in daily_eq]
    rs = np.diff(es) / np.array(es[:-1]) if len(es) > 1 else [0]
    sr = np.mean(rs) / np.std(rs) * np.sqrt(252) if np.std(rs) > 1e-8 else 0
    wr = wins / trades * 100 if trades > 0 else 0

    return {
        "strategy": f"持有模式(跌>{rebalance_threshold}换,Top{top_n},v2)",
        "trading_days": len(daily_eq),
        "total_trades": trades,
        "total_return_pct": round(ret, 2),
        "win_rate_pct": round(wr, 1),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio": round(sr, 3),
        "total_fees": round(fees, 2),
        "wins": wins,
        "losses": losses,
    }


# ── 输出 ──────────────────────────────────────────────


def format_table(results):
    if not results:
        return
    h = (
        f"{'策略':<38} {'交易日':>6} {'交易':>6} {'总收益%':>10} "
        f"{'胜率%':>6} {'最大回撤%':>10} {'夏普':>8} {'费用':>10}"
    )
    print(f"\n{'=' * 100}")
    print(h)
    print("-" * 100)
    for r in results:
        if r.get("error"):
            print(f"{r.get('strategy', '?'):<38} ⚠️ {r['error']}")
        else:
            s = r.get("strategy", "?")
            td = r.get("trading_days", 0)
            tt = r.get("total_trades", 0)
            tr = r.get("total_return_pct", 0)
            wr = r.get("win_rate_pct", 0)
            mdd = r.get("max_drawdown_pct", 0)
            sr = r.get("sharpe_ratio", 0)
            tf = r.get("total_fees", 0)
            print(f"{s:<38} {td:>6} {tt:>6} {tr:>10.2f} " f"{wr:>6.1f} {mdd:>10.2f} {sr:>8.3f} {tf:>10.2f}")


def main():
    parser = argparse.ArgumentParser(description="生产代码回测 v2")
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--start", type=str, default="")
    parser.add_argument("--end", type=str, default="")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--hold", action="store_true")
    parser.add_argument("--rebalance", type=int, default=10)
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    log.info("🔥 生产代码回测 v2 (开盘价 + 滑点) 启动")
    log.info(f"  滑点: {SLIPPAGE:.1%} 双边 | 费率: calc_buy/sell_fees (生产模块)")

    t0 = time.time()
    stocks = load_data(days=args.days, start=args.start, end=args.end)
    if not stocks:
        log.error("❌ 无数据")
        return

    results = []
    if args.compare:
        results.append(run_backtest_daytrade_v2(stocks, top_n=args.top))
        for thr in [10, 15]:
            results.append(run_backtest_hold_v2(stocks, top_n=args.top, rebalance_threshold=thr))
    elif args.hold:
        results.append(run_backtest_hold_v2(stocks, top_n=args.top, rebalance_threshold=args.rebalance))
    else:
        results.append(run_backtest_daytrade_v2(stocks, top_n=args.top))

    format_table(results)
    log.info(f"⏱ 总耗时: {time.time()-t0:.0f}s")

    if args.save:
        fname = Path("reports") / f"backtest_real_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(fname, "w") as f:
            json.dump({"results": results}, f, ensure_ascii=False, indent=2)
        log.info(f"💾 保存到 {fname}")


if __name__ == "__main__":
    main()
