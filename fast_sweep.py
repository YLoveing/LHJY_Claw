#!/usr/bin/env python3
"""
fast_sweep.py — 快速参数扫描

优化：先批量预计算每日筛选举果（评分/价格/指标），保存后重复使用。
"""

import json, pickle, sys, time, warnings
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

warnings.filterwarnings("ignore")

CACHE_FILE = Path("/opt/daily_stock_analysis/screener/cache/all_kline_cache.json")
SCREEN_CACHE = Path("/opt/daily_stock_analysis/screener/cache/daily_scores.pkl")

INITIAL_CAPITAL = 1_000_000
STOP_LOSS, TAKE_PROFIT = -0.15, 0.25
DRAWDOWN_LIMIT = -0.20
KELLY_B = 0.25 / 0.15
MIN_TRADE = 10_000


# ═══ 第一步：预计算每天每只股票的评分 ═══

def precompute_scores():
    """计算每只股票每天的评分和技术指标，保存到缓存。"""
    print("预计算每日评分...")
    cache = json.loads(CACHE_FILE.read_text())
    
    all_dates = sorted(set(
        d for k in cache.values() for d in k.keys() if "2025-05-08" <= d < "2026-05-08"
    ))
    
    # 结构: daily_data[date_str] = [(code, score, price, market_value, daily_amt), ...]
    # 以及 stocks_by_date[code][date_idx] = score (用于快速查询持仓评分)
    daily_data = {}
    stocks_by_date = {}
    
    warmup = 30
    total = len(cache)
    
    for day_idx, date_str in enumerate(all_dates):
        day_items = []
        
        for code, kline in cache.items():
            dates = sorted([d for d in kline.keys() if d <= date_str])
            if len(dates) < warmup:
                continue
            recent = dates[-60:]
            closes = np.array([kline[d]["close"] for d in recent])
            volumes = np.array([kline[d]["volume"] for d in recent])
            n = len(closes)
            
            if code not in stocks_by_date:
                stocks_by_date[code] = [0.0] * len(all_dates)
            
            # 每日流动性过滤
            if date_str in kline and n >= 20:
                daily_amt = kline[date_str].get("amount", 0) / 1e8
                daily_price = kline[date_str].get("close", 0)
                if daily_amt < 0.3 or daily_price < 3 or daily_price > 200:
                    stocks_by_date[code][day_idx] = 0.0
                    continue
            else:
                stocks_by_date[code][day_idx] = 0.0
                continue
            
            # 日收益率（回传用于GARCH）
            ret = (closes[-1] / closes[-2] - 1) if n >= 2 and closes[-2] > 0 else 0.0
            stocks_by_date[code][day_idx] = ret  # 先存收益率
            
            # 技术指标
            m5 = float(np.mean(closes[-5:])) if n >= 5 else 0
            m10 = float(np.mean(closes[-10:])) if n >= 10 else 0
            m20 = float(np.mean(closes[-20:])) if n >= 20 else 0
            
            conds = []
            if m5 > m10 > m20: conds.append("多头")
            elif m5 > m20 and m5 > m10 * 0.99: conds.append("偏多")
            
            if n >= 15:
                delta = np.diff(closes[-15:])
                gains = np.maximum(delta, 0); losses = np.maximum(-delta, 0)
                rsi = 100 - 100 / (1 + np.mean(gains) / max(np.mean(losses), 1e-8))
            else:
                rsi = 50.0
            if rsi < 35 and closes[-1] > m5: conds.append("超卖")
            
            if n >= 20:
                vol_ratio = volumes[-1] / max(np.mean(volumes[-20:]), 1)
            else:
                vol_ratio = 1.0
            if vol_ratio > 1.5 and n >= 20: conds.append("放量")
            if n >= 10 and m5 > m10: conds.append("金叉")
            
            if not conds:
                stocks_by_date[code][day_idx] = 0.0
                continue
            
            # 评分
            score = 50.0
            if "多头" in conds: score += 20
            if "放量" in conds: score += 15
            if "金叉" in conds: score += 10
            if "超卖" in conds: score += 8
            if rsi < 30: score += 8
            elif rsi > 75: score -= 10
            score = np.clip(round(score, 1), 0, 100)
            
            stocks_by_date[code][day_idx] = float(score)
            
            price = float(kline[date_str]["close"])
            daily_amt = kline[date_str].get("amount", 0) / 1e8
            day_items.append((code, score, price, daily_amt))
        
        # 按评分排序
        day_items.sort(key=lambda x: x[1], reverse=True)
        daily_data[date_str] = day_items
        
        if (day_idx + 1) % 50 == 0:
            print(f"  [{day_idx+1}/{len(all_dates)}] {date_str} {len(day_items)} 候选")
    
    # 保存
    print(f"保存预计算缓存: {len(daily_data)} 天, {len(stocks_by_date)} 只股票")
    with open(SCREEN_CACHE, "wb") as f:
        pickle.dump({
            "daily_data": daily_data,
            "stocks_by_date": stocks_by_date,
            "all_dates": all_dates,
        }, f)
    
    return daily_data, stocks_by_date, all_dates


# ═══ 第二步：快速调参回测 ═══

def fast_backtest(
    daily_data: Dict,
    stocks_by_date: Dict,
    all_dates: List[str],
    base_buy: int = 70,
    base_sell: int = 30,
    max_positions: int = 10,
    diversification: float = 0.20,
) -> Dict:
    """快速回测，基于预计算的评分数据。"""
    cash = INITIAL_CAPITAL
    positions: Dict = {}
    trades = []
    peak_equity = INITIAL_CAPITAL
    total_fee = 0.0
    warmup = 30
    
    daily_equity = []
    n_dates = len(all_dates)
    
    # 收益历史（GARCH）
    ret_history: Dict[str, List[float]] = {}
    
    # 阶段1: warmup - 只收集收益率
    for day_idx in range(warmup):
        date_str = all_dates[day_idx]
        for code in stocks_by_date:
            val = stocks_by_date[code][day_idx]
            if isinstance(val, float) and val != 0.0:
                if code not in ret_history:
                    ret_history[code] = []
                ret_history[code].append(val)
        
        # 仍记录权益
        daily_equity.append({"date": date_str, "equity": float(cash), "return_pct": 0.0, "dd_pct": 0.0, "positions": 0})
    
    # 阶段2: 交易
    for day_idx in range(warmup, n_dates):
        date_str = all_dates[day_idx]
        day_items = daily_data.get(date_str, [])
        
        # 更新收益历史
        for code in stocks_by_date:
            val = stocks_by_date[code][day_idx]
            if isinstance(val, float) and val != 0.0 and val < 5.0:  # 这是收益率 (0附近)
                if code not in ret_history:
                    ret_history[code] = []
                ret_history[code].append(val)
        
        # GARCH阈值
        all_rets = []
        for c in ret_history:
            all_rets.extend(ret_history[c][-40:])
        if len(all_rets) >= 2:
            vol_s = [float(np.std(all_rets[max(0, i-20):i])) * np.sqrt(252) for i in range(5, len(all_rets))]
            cur_v = float(np.std(all_rets[-20:])) * np.sqrt(252) if len(all_rets) >= 20 else float(np.std(all_rets)) * np.sqrt(252)
            p = float(np.sum(np.array(vol_s) <= cur_v) / len(vol_s) * 100) if len(vol_s) >= 2 else 50.0
            p = np.clip(p, 0, 100)
            if p >= 90: buy_th, sell_th = base_buy + 10, base_sell
            elif p >= 75: buy_th, sell_th = base_buy + 5, base_sell
            elif p >= 60: buy_th, sell_th = base_buy + 2, base_sell
            elif p <= 10: buy_th, sell_th = base_buy - 8, base_sell + 8
            elif p <= 25: buy_th, sell_th = base_buy - 3, base_sell + 3
            else: buy_th, sell_th = base_buy, base_sell
        else:
            buy_th, sell_th = base_buy, base_sell
        
        # 价格表
        day_prices = {}
        for item in day_items:
            day_prices[item[0]] = item[2]
        
        # 持仓评分
        pos_scores = {}
        for item in day_items:
            pos_scores[item[0]] = item[1]
        # 补充持仓中但当天未上榜的股票
        for code in positions:
            if code not in pos_scores:
                pos_scores[code] = 0
        
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
            if pnl_pct <= STOP_LOSS:
                qty = pos["quantity"]
                proceeds = qty * price; fee = proceeds * 0.0003
                pnl = (price - pos["avg_cost"]) * qty - fee
                cash += proceeds - fee; total_fee += fee
                trades.append({"date": date_str, "code": code, "side": "stop_loss", "qty": qty, "price": round(price, 3), "pnl": round(pnl, 2)})
                del positions[code]
            elif pnl_pct >= TAKE_PROFIT:
                qty = max(int(pos["quantity"] / 2 / 100) * 100, 100)
                qty = min(qty, pos["quantity"])
                if qty > 0:
                    proceeds = qty * price; fee = proceeds * 0.0003
                    pnl = (price - pos["avg_cost"]) * qty - fee
                    cash += proceeds - fee; total_fee += fee
                    pos["quantity"] -= qty
                    trades.append({"date": date_str, "code": code, "side": "take_profit", "qty": qty, "price": round(price, 3), "pnl": round(pnl, 2)})
                    if pos["quantity"] <= 0: del positions[code]
        
        # 卖出
        for code in list(positions.keys()):
            score = pos_scores.get(code, 0)
            price = day_prices.get(code, positions[code].get("price"))
            if score <= sell_th and price:
                qty = positions[code]["quantity"]
                proceeds = qty * price; fee = proceeds * 0.0003
                pnl = (price - positions[code]["avg_cost"]) * qty - fee
                cash += proceeds - fee; total_fee += fee
                trades.append({"date": date_str, "code": code, "side": "sell", "qty": qty, "price": round(price, 3), "pnl": round(pnl, 2), "reason": f"评分{score}≤{sell_th}"})
                del positions[code]
        
        # 买入
        is_last_20 = day_idx >= n_dates - 20
        if not blocked and not is_last_20 and len(positions) < max_positions:
            for item in day_items:
                code, score, price, _ = item
                if code in positions or score < buy_th: continue
                if price <= 0: continue
                p = 0.9 if score >= 100 else 0.85 if score >= 90 else 0.78 if score >= 80 else 0.70 if score >= 70 else 0.50
                q = 1 - p
                f = max(0.0, (KELLY_B * p - q) / KELLY_B) * 0.5 * diversification
                amount = cash * f
                amount = max(MIN_TRADE, min(amount, cash * 0.25, 200_000))
                qty = int(amount / price / 100) * 100
                qty = min(qty, int(cash * 0.25 / price / 100) * 100)
                if qty >= 100 and qty * price <= cash * 0.30:
                    cost = qty * price; fee = cost * 0.0003
                    cash -= cost + fee; total_fee += fee
                    positions[code] = {"quantity": qty, "avg_cost": price, "price": price}
                    trades.append({"date": date_str, "code": code, "side": "buy", "qty": qty, "price": round(price, 3), "score": score})
                if len(positions) >= max_positions: break
        
        # 更新权益记录
        market_value = 0
        for code in list(positions.keys()):
            price = day_prices.get(code)
            if price: positions[code]["price"] = price; market_value += positions[code]["quantity"] * price
        equity = cash + market_value
        peak_equity = max(peak_equity, equity)
        daily_equity.append({
            "date": date_str, "equity": round(equity, 2), "positions": len(positions),
            "return_pct": round((equity - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 2),
            "dd_pct": round((equity - peak_equity) / peak_equity * 100, 2),
        })
    
    # 统计
    n_days = n_dates - warmup
    years = n_days / 252
    final_eq = daily_equity[-1]["equity"]
    total_return = (final_eq - INITIAL_CAPITAL) / INITIAL_CAPITAL
    ann_return = (1 + total_return) ** (1 / max(years, 0.01)) - 1 if years > 0 else 0
    eq_s = np.array([d["equity"] for d in daily_equity])
    peaks = np.maximum.accumulate(eq_s)
    max_dd = float(np.min((eq_s - peaks) / peaks)) * 100
    if n_days > 1:
        daily_ret = np.diff(eq_s) / eq_s[:-1]
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


def run_sweeps():
    # 预计算
    pkl = SCREEN_CACHE
    if pkl.exists():
        print("加载预计算缓存...")
        with open(pkl, "rb") as f:
            data = pickle.load(f)
        daily_data = data["daily_data"]
        stocks_by_date = data["stocks_by_date"]
        all_dates = data["all_dates"]
        print(f"  已加载: {len(all_dates)} 天, {len(stocks_by_date)} 只股票")
    else:
        daily_data, stocks_by_date, all_dates = precompute_scores()
    
    print(f"\n候选池: {len(stocks_by_date)} 只, 交易日: {len(all_dates)}")
    
    # 扫描1: 买入门槛
    print("\n" + "=" * 70)
    print("📈 [扫描1] 买入门槛 (BASE_BUY)")
    print(f"   固定: BASE_SELL=30, MAX_POS=10, DIV=0.20")
    print(f"{'门槛':>6} {'总收益':>8} {'年化':>8} {'夏普':>8} {'回撤':>8} {'胜率':>7} {'盈亏比':>8} {'交易':>6} {'权益':>10}")
    print("-" * 70)
    for buy in range(55, 96, 5):
        t0 = time.time()
        r = fast_backtest(daily_data, stocks_by_date, all_dates, base_buy=buy, base_sell=30)
        t = time.time() - t0
        print(f"  {buy:>3}  {r['total_return_pct']:>+7.2f}% {r['ann_return_pct']:>+7.2f}% "
              f"{r['sharpe_ratio']:>7.3f} {r['max_drawdown_pct']:>+7.2f}% "
              f"{r['win_rate_pct']:>6.1f}% {r['profit_factor']:>7} "
              f"{r['total_trades']:>5} {r['final_equity']:>10.2f}")
    
    # 扫描2: 卖出门槛
    print("\n" + "=" * 70)
    print("📉 [扫描2] 卖出门槛 (BASE_SELL)")
    print(f"   固定: BASE_BUY=70, MAX_POS=10, DIV=0.20")
    print(f"{'门槛':>6} {'总收益':>8} {'年化':>8} {'夏普':>8} {'回撤':>8} {'胜率':>7} {'盈亏比':>8} {'交易':>6} {'权益':>10}")
    print("-" * 70)
    for sell in range(20, 56, 5):
        r = fast_backtest(daily_data, stocks_by_date, all_dates, base_buy=70, base_sell=sell)
        print(f"  {sell:>3}  {r['total_return_pct']:>+7.2f}% {r['ann_return_pct']:>+7.2f}% "
              f"{r['sharpe_ratio']:>7.3f} {r['max_drawdown_pct']:>+7.2f}% "
              f"{r['win_rate_pct']:>6.1f}% {r['profit_factor']:>7} "
              f"{r['total_trades']:>5} {r['final_equity']:>10.2f}")
    
    # 扫描3: 分散系数
    print("\n" + "=" * 70)
    print("📊 [扫描3] 凯利分散系数 (DIVERSIFICATION)")
    print(f"   固定: BASE_BUY=70, BASE_SELL=30, MAX_POS=10")
    print(f"{'分散':>6} {'总收益':>8} {'年化':>8} {'夏普':>8} {'回撤':>8} {'胜率':>7} {'盈亏比':>8} {'交易':>6} {'权益':>10}")
    print("-" * 70)
    for div in [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]:
        r = fast_backtest(daily_data, stocks_by_date, all_dates, base_buy=70, base_sell=30, diversification=div)
        print(f"  {div:.2f} {r['total_return_pct']:>+7.2f}% {r['ann_return_pct']:>+7.2f}% "
              f"{r['sharpe_ratio']:>7.3f} {r['max_drawdown_pct']:>+7.2f}% "
              f"{r['win_rate_pct']:>6.1f}% {r['profit_factor']:>7} "
              f"{r['total_trades']:>5} {r['final_equity']:>10.2f}")
    
    # 扫描4: 最大持仓
    print("\n" + "=" * 70)
    print("📋 [扫描4] 最大持仓数 (MAX_POSITIONS)")
    print(f"   固定: BASE_BUY=70, BASE_SELL=30, DIV=0.20")
    print(f"{'持仓':>6} {'总收益':>8} {'年化':>8} {'夏普':>8} {'回撤':>8} {'胜率':>7} {'盈亏比':>8} {'交易':>6} {'权益':>10}")
    print("-" * 70)
    for mp in [3, 5, 8, 10, 12, 15]:
        r = fast_backtest(daily_data, stocks_by_date, all_dates, base_buy=70, base_sell=30, max_positions=mp)
        print(f"  {mp:>3}  {r['total_return_pct']:>+7.2f}% {r['ann_return_pct']:>+7.2f}% "
              f"{r['sharpe_ratio']:>7.3f} {r['max_drawdown_pct']:>+7.2f}% "
              f"{r['win_rate_pct']:>6.1f}% {r['profit_factor']:>7} "
              f"{r['total_trades']:>5} {r['final_equity']:>10.2f}")


if __name__ == "__main__":
    t0 = time.time()
    run_sweeps()
    print(f"\n⏱ 总耗时: {time.time() - t0:.0f} 秒")
