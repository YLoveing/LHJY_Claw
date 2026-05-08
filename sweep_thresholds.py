#!/usr/bin/env python3
"""
sweep_thresholds.py — 买入门槛参数扫描

测试不同 BASE_BUY 对策略绩效的影响。
避免未来函数：用年度数据的分段验证。

当前门槛 (BASE_BUY/BASE_SELL):
  基础:    70 / 30
  GARCH调: 62~80 / 30~38 (取决于波动率百分位)
"""

import json
import logging
import sys
import time
import warnings
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.WARNING)

# 路径
CACHE_FILE = Path("/opt/daily_stock_analysis/screener/cache/all_kline_cache.json")
RESULT_DIR = Path("/opt/daily_stock_analysis/simulated_trading")
RESULT_DIR.mkdir(parents=True, exist_ok=True)

# 策略参数（固定部分）
INITIAL_CAPITAL = 1_000_000
MAX_POSITIONS = 10
STOP_LOSS = -0.15
TAKE_PROFIT = 0.25
DRAWDOWN_LIMIT = -0.20
KELLY_B = 0.25 / 0.15
DIVERSIFICATION = 0.20
MIN_TRADE = 10_000


def run_backtest(
    cache_data: Dict,
    all_dates: List[str],
    base_buy: int = 70,
    base_sell: int = 30,
    max_positions: int = 10,
    diversification: float = 0.20,
) -> Dict:
    """
    单次回测运行，参数化阈值。
    与 backtest_screener.py 保持相同逻辑。
    """
    cash = INITIAL_CAPITAL
    positions = OrderedDict()
    trades = []
    daily_equity = []
    peak_equity = INITIAL_CAPITAL
    total_fee = 0.0
    return_history = {c: [] for c in cache_data}
    warmup = 30

    for day_idx, date_str in enumerate(all_dates):
        is_friday = datetime.strptime(date_str, "%Y-%m-%d").weekday() == 4
        is_warmup = day_idx < warmup
        is_last_20 = day_idx >= len(all_dates) - 20

        # 当日筛选
        screened = []
        for code, kline in cache_data.items():
            # compute_indicators_for_day 内联
            dates = sorted([d for d in kline.keys() if d <= date_str])
            if len(dates) < 30:
                continue
            recent = dates[-60:]
            closes = np.array([kline[d]["close"] for d in recent])
            volumes = np.array([kline[d]["volume"] for d in recent])
            n = len(closes)

            # 每日流动性过滤
            if date_str in kline and n >= 20:
                daily_amt = kline[date_str].get("amount", 0) / 1e8
                daily_price = kline[date_str].get("close", 0)
                if daily_amt < 0.3 or daily_price < 3 or daily_price > 200:
                    continue

            # 日收益率
            if n >= 2:
                ret = (closes[-1] / closes[-2] - 1) if closes[-2] > 0 else 0
                return_history[code].append(ret)

            if is_warmup:
                continue

            # 技术评分
            ind = {}
            ind["_n"] = n
            ind["latest_close"] = closes[-1]
            if n >= 5:
                ind["MA5"] = float(np.mean(closes[-5:]))
            if n >= 10:
                ind["MA10"] = float(np.mean(closes[-10:]))
            if n >= 20:
                ind["MA20"] = float(np.mean(closes[-20:]))
                ind["vol_MA20"] = float(np.mean(volumes[-20:]))
                ind["vol_ratio"] = volumes[-1] / max(ind["vol_MA20"], 1)
            if n >= 15:
                delta = np.diff(closes[-15:])
                gains = np.maximum(delta, 0)
                losses = np.maximum(-delta, 0)
                ag = np.mean(gains)
                al = np.mean(losses)
                ind["RSI"] = 100 - 100 / (1 + ag / max(al, 1e-8))
            else:
                ind["RSI"] = 50

            # 筛选条件
            conds = []
            if ind.get("MA5", 0) > ind.get("MA10", 0) > ind.get("MA20", 0):
                conds.append("多头")
            elif ind.get("MA5", 0) > ind.get("MA20", 0) and ind.get("MA5", 0) > ind.get("MA10", 0) * 0.99:
                conds.append("偏多")
            if ind.get("RSI", 50) < 35 and ind["latest_close"] > ind.get("MA5", 0):
                conds.append("超卖")
            if ind.get("vol_ratio", 0) > 1.5 and ind.get("vol_MA20", 0) > 0:
                conds.append("放量")
            if n >= 10 and ind.get("MA5", 0) > ind.get("MA10", 0):
                conds.append("金叉")
            if not conds:
                continue

            # 评分
            score = 50.0
            if "多头" in conds: score += 20
            if "放量" in conds: score += 15
            if "金叉" in conds: score += 10
            if "超卖" in conds: score += 8
            rsi = ind.get("RSI", 50)
            if rsi < 30: score += 8
            elif rsi > 75: score -= 10
            score = np.clip(round(score, 1), 0, 100)

            screened.append((code, score, conds, ind))

        if is_warmup:
            continue

        # 排序、GARCH阈值、交易
        screened.sort(key=lambda x: x[1], reverse=True)

        # GARCH 阈值（简化）
        all_rets = []
        for c in cache_data:
            all_rets.extend(return_history[c][-40:])
        if len(all_rets) >= 2:
            vol_series = []
            for i in range(5, len(all_rets)):
                v = float(np.std(all_rets[max(0, i-20):i])) * np.sqrt(252)
                vol_series.append(v)
            cur_vol = float(np.std(all_rets[-20:])) * np.sqrt(252) if len(all_rets) >= 20 else float(np.std(all_rets)) * np.sqrt(252)
            p = float(np.sum(np.array(vol_series) <= cur_vol) / len(vol_series) * 100) if len(vol_series) >= 2 else 50.0
            p = np.clip(p, 0, 100)
            if p >= 90: buy_th, sell_th = base_buy + 10, base_sell
            elif p >= 75: buy_th, sell_th = base_buy + 5, base_sell
            elif p >= 60: buy_th, sell_th = base_buy + 2, base_sell
            elif p <= 10: buy_th, sell_th = base_buy - 8, base_sell + 8
            elif p <= 25: buy_th, sell_th = base_buy - 3, base_sell + 3
            else: buy_th, sell_th = base_buy, base_sell
        else:
            buy_th, sell_th = base_buy, base_sell

        # 价格
        day_prices = {c: cache_data[c][date_str]["close"] for c in cache_data if date_str in cache_data[c]}

        # 权益
        market_value = sum(pos["quantity"] * day_prices.get(code, pos["price"]) for code, pos in positions.items())
        total_equity = cash + market_value
        peak_equity = max(peak_equity, total_equity)
        dd = (total_equity - peak_equity) / peak_equity
        blocked = dd < DRAWDOWN_LIMIT

        # 止损/止盈
        for code in list(positions.keys()):
            pos = positions[code]
            price = day_prices.get(code, pos.get("price"))
            if price is None: continue
            pnl_pct = (price - pos["avg_cost"]) / pos["avg_cost"]
            action = None
            if pnl_pct <= STOP_LOSS: action = "stop_loss"
            elif pnl_pct >= TAKE_PROFIT: action = "take_profit"
            if action:
                qty = min(pos["quantity"], max(int(pos["quantity"] / 2 / 100) * 100, 100)) if action == "take_profit" else pos["quantity"]
                qty = min(qty, pos["quantity"])
                if qty <= 0: continue
                proceeds = qty * price; fee = proceeds * 0.0003
                pnl = (price - pos["avg_cost"]) * qty - fee
                cash += proceeds - fee; total_fee += fee
                pos["quantity"] -= qty
                trades.append({"date": date_str, "code": code, "side": action, "qty": qty, "price": round(price, 3), "pnl": round(pnl, 2)})
                if pos["quantity"] <= 0: del positions[code]

        # 卖出
        for code in list(positions.keys()):
            s = next((s[1] for s in screened if s[0] == code), 0)
            price = day_prices.get(code, positions[code].get("price"))
            if s <= sell_th and price:
                qty = positions[code]["quantity"]
                proceeds = qty * price; fee = proceeds * 0.0003
                pnl = (price - positions[code]["avg_cost"]) * qty - fee
                cash += proceeds - fee; total_fee += fee
                trades.append({"date": date_str, "code": code, "side": "sell", "qty": qty, "price": round(price, 3), "pnl": round(pnl, 2), "reason": f"评分{s}≤{sell_th}"})
                del positions[code]

        # 买入
        if not blocked and not is_last_20 and len(positions) < max_positions:
            for code, score, conds, ind in screened:
                if code in positions or score < buy_th: continue
                price = day_prices.get(code)
                if price is None or price <= 0: continue
                p = 0.9 if score >= 100 else 0.85 if score >= 90 else 0.78 if score >= 80 else 0.70 if score >= 70 else 0.50
                q = 1 - p
                f_kelly = max(0.0, (KELLY_B * p - q) / KELLY_B) * 0.5 * diversification
                amount = cash * f_kelly
                amount = max(MIN_TRADE, min(amount, cash * 0.25, 200_000))
                qty = int(amount / price / 100) * 100
                qty = min(qty, int(cash * 0.25 / price / 100) * 100)
                if qty >= 100 and qty * price <= cash * 0.30:
                    cost = qty * price; fee = cost * 0.0003
                    cash -= cost + fee; total_fee += fee
                    positions[code] = {"quantity": qty, "avg_cost": price, "price": price, "invested": cost}
                    trades.append({"date": date_str, "code": code, "side": "buy", "qty": qty, "price": round(price, 3), "score": score})
                if len(positions) >= max_positions: break

        # 权益
        market_value = 0
        for code in list(positions.keys()):
            price = day_prices.get(code)
            if price: positions[code]["price"] = price; market_value += positions[code]["quantity"] * price
        equity = cash + market_value
        peak_equity = max(peak_equity, equity)
        daily_equity.append({"date": date_str, "equity": round(equity, 2), "positions": len(positions),
                             "return_pct": round((equity - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 2),
                             "dd_pct": round((equity - peak_equity) / peak_equity * 100, 2)})

    # 统计
    n_days = len(all_dates) - warmup
    years = n_days / 252
    final_eq = daily_equity[-1]["equity"]
    total_return = (final_eq - INITIAL_CAPITAL) / INITIAL_CAPITAL
    ann_return = (1 + total_return) ** (1 / max(years, 0.01)) - 1 if years > 0 else 0
    eq_series = np.array([d["equity"] for d in daily_equity])
    peaks = np.maximum.accumulate(eq_series)
    max_dd = float(np.min((eq_series - peaks) / peaks)) * 100
    if n_days > 1:
        daily_ret = np.diff(eq_series) / eq_series[:-1]
        sharpe = float(np.mean(daily_ret) / max(np.std(daily_ret), 1e-8) * np.sqrt(252))
    else:
        sharpe = 0.0
    closed = [t for t in trades if t["side"] in ("sell", "stop_loss", "take_profit")]
    wins = [t for t in closed if t.get("pnl", 0) > 0]
    losses = [t for t in closed if t.get("pnl", 0) <= 0]
    wr = len(wins) / len(closed) * 100 if closed else 0
    pf = np.mean([t["pnl"] for t in wins]) / abs(np.mean([t["pnl"] for t in losses])) if losses and np.mean([t["pnl"] for t in losses]) != 0 else "N/A"

    return {
        "total_return_pct": round(total_return * 100, 2),
        "ann_return_pct": round(ann_return * 100, 2),
        "sharpe_ratio": round(sharpe, 3),
        "max_drawdown_pct": round(max_dd, 2),
        "win_rate_pct": round(wr, 1),
        "profit_factor": round(pf, 2) if pf != "N/A" else "N/A",
        "total_trades": len(trades),
        "closed_trades": len(closed),
        "final_equity": round(final_eq, 2),
    }


def main():
    print("=" * 70)
    print("📊 买入门槛参数扫描")
    print("=" * 70)

    # 加载缓存
    cache = json.loads(CACHE_FILE.read_text())
    all_dates = sorted(set(
        d for k in cache.values() for d in k.keys() if "2025-05-08" <= d < "2026-05-08"
    ))
    print(f"\n候选池: {len(cache)} 只")
    print(f"交易日: {len(all_dates)} 天")

    # ── 扫描 1: 买入门槛 ──
    print("\n" + "-" * 70)
    print("📈 [扫描1] 买入门槛 (BASE_BUY)")
    print(f"  卖出门槛固定=30, 最多持仓=10, 分散系数=0.20")
    print("-" * 70)
    print(f"{'门槛':>6} {'总收益':>8} {'年化':>8} {'夏普':>8} {'回撤':>8} {'胜率':>6} {'盈亏比':>8} {'交易':>6} {'最终权益':>10}")
    print("-" * 70)

    results_buy = []
    for buy_th in range(55, 96, 5):
        r = run_backtest(cache, all_dates, base_buy=buy_th, base_sell=30)
        results_buy.append((buy_th, r))
        print(f"  BUY={buy_th:>3} {r['total_return_pct']:>+7.2f}% {r['ann_return_pct']:>+7.2f}% "
              f"{r['sharpe_ratio']:>7.3f} {r['max_drawdown_pct']:>+7.2f}% "
              f"{r['win_rate_pct']:>5.1f}% {r['profit_factor']:>7} "
              f"{r['total_trades']:>5} {r['final_equity']:>10.2f}")

    # ── 扫描 2: 卖出门槛 ──
    print("\n" + "-" * 70)
    print("📉 [扫描2] 卖出门槛 (BASE_SELL) — 买入门槛=70固定")
    print("-" * 70)
    print(f"{'门槛':>6} {'总收益':>8} {'年化':>8} {'夏普':>8} {'回撤':>8} {'胜率':>6} {'盈亏比':>8} {'交易':>6} {'最终权益':>10}")
    print("-" * 70)

    results_sell = []
    for sell_th in range(20, 51, 5):
        r = run_backtest(cache, all_dates, base_buy=70, base_sell=sell_th)
        results_sell.append((sell_th, r))
        print(f"  SELL={sell_th:>3} {r['total_return_pct']:>+7.2f}% {r['ann_return_pct']:>+7.2f}% "
              f"{r['sharpe_ratio']:>7.3f} {r['max_drawdown_pct']:>+7.2f}% "
              f"{r['win_rate_pct']:>5.1f}% {r['profit_factor']:>7} "
              f"{r['total_trades']:>5} {r['final_equity']:>10.2f}")

    # ── 扫描 3: 分散系数 ──
    print("\n" + "-" * 70)
    print("📊 [扫描3] 凯利分散系数 (DIVERSIFICATION) — 买入70/卖出30固定")
    print("-" * 70)
    print(f"{'分散':>6} {'总收益':>8} {'年化':>8} {'夏普':>8} {'回撤':>8} {'胜率':>6} {'盈亏比':>8} {'交易':>6} {'最终权益':>10}")
    print("-" * 70)

    results_div = []
    for div in [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]:
        r = run_backtest(cache, all_dates, base_buy=70, base_sell=30, diversification=div)
        results_div.append((div, r))
        print(f"  div={div:.2f} {r['total_return_pct']:>+7.2f}% {r['ann_return_pct']:>+7.2f}% "
              f"{r['sharpe_ratio']:>7.3f} {r['max_drawdown_pct']:>+7.2f}% "
              f"{r['win_rate_pct']:>5.1f}% {r['profit_factor']:>7} "
              f"{r['total_trades']:>5} {r['final_equity']:>10.2f}")

    # ── 扫描 4: 最大持仓数 ──
    print("\n" + "-" * 70)
    print("📋 [扫描4] 最大持仓数 (MAX_POSITIONS) — 买入70/卖出30固定")
    print("-" * 70)
    print(f"{'持仓':>6} {'总收益':>8} {'年化':>8} {'夏普':>8} {'回撤':>8} {'胜率':>6} {'盈亏比':>8} {'交易':>6} {'最终权益':>10}")
    print("-" * 70)

    results_pos = []
    for mp in [3, 5, 8, 10, 12, 15]:
        r = run_backtest(cache, all_dates, base_buy=70, base_sell=30, max_positions=mp)
        results_pos.append((mp, r))
        print(f"  N={mp:>4} {r['total_return_pct']:>+7.2f}% {r['ann_return_pct']:>+7.2f}% "
              f"{r['sharpe_ratio']:>7.3f} {r['max_drawdown_pct']:>+7.2f}% "
              f"{r['win_rate_pct']:>5.1f}% {r['profit_factor']:>7} "
              f"{r['total_trades']:>5} {r['final_equity']:>10.2f}")

    # 推荐
    print("\n" + "=" * 70)
    print("📌 推荐配置")
    print("=" * 70)

    # 按夏普排序找最优
    best_by_sharpe = sorted(results_buy, key=lambda x: x[1]["sharpe_ratio"], reverse=True)[0]
    best_by_return = sorted(results_buy, key=lambda x: x[1]["total_return_pct"], reverse=True)[0]
    best_dd = sorted(results_buy, key=lambda x: x[1]["max_drawdown_pct"], reverse=True)[0]

    print(f"\n夏普最优: BUY={best_by_sharpe[0]}, 总收益{best_by_sharpe[1]['total_return_pct']:+}%, "
          f"夏普{best_by_sharpe[1]['sharpe_ratio']}, 回撤{best_by_sharpe[1]['max_drawdown_pct']:.1f}%")
    print(f"收益最优: BUY={best_by_return[0]}, 总收益{best_by_return[1]['total_return_pct']:+}%, "
          f"夏普{best_by_return[1]['sharpe_ratio']}, 回撤{best_by_return[1]['max_drawdown_pct']:.1f}%")
    print(f"回撤最优: BUY={best_dd[0]}, 总收益{best_dd[1]['total_return_pct']:+}%, "
          f"夏普{best_dd[1]['sharpe_ratio']}, 回撤{best_dd[1]['max_drawdown_pct']:.1f}%")


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"\n⏱ 总耗时: {time.time()-t0:.0f} 秒")
