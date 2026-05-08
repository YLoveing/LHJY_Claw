#!/usr/bin/env python3
"""
backtest_screener.py — 带选股的一年回测引擎

流程：
  1. 从今日选股器选出的 ~73 只活跃股作为固定候选池
  2. 下载一年日K线数据并缓存
  3. 逐日模拟：每日从池中技术筛选 → 评分 → 按策略交易
  4. 无未来函数，逐日滚动
"""

import json
import logging
import time
import warnings
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backtest_screener")

# ── 路径 ──
CACHE_DIR = Path("/opt/daily_stock_analysis/screener/cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── 策略参数 ──
INITIAL_CAPITAL = 1_000_000
MAX_POSITIONS = 10
STOP_LOSS = -0.15
TAKE_PROFIT = 0.25
DRAWDOWN_LIMIT = -0.20
KELLY_B = 0.25 / 0.15
DIVERSIFICATION = 0.20
MIN_TRADE = 10_000
BASE_BUY, BASE_SELL = 70, 30

# 选股条件参数
COND_PARAMS = {
    "min_volume_yi": 0.3,
    "min_price": 3.0,
    "min_kline_days": 30,
}

# 东方财富 API 头
EM_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://quote.eastmoney.com/",
}


# ═══════════════════════════════════════════
#  数据层
# ═══════════════════════════════════════════

def get_liquid_stocks(min_volume_yi: float = 0.3) -> List[Dict]:
    """获取今日流动性好的股票作为候选池。"""
    log.info("获取全A股实时行情...")
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    items = []
    page = 1
    while True:
        params = {
            "pn": page, "pz": 500, "po": 0, "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": 2, "invt": 2,
            "fid": "f3", "fs": "m:0+t:1,m:1+t:2,m:0+t:7",
            "fields": "f2,f3,f6,f12,f14,f20,f21",
        }
        try:
            r = requests.get(url, params=params, headers=EM_HEADERS, timeout=10)
            data = r.json()
            batch = data["data"]["diff"]
            items.extend(batch)
            total = data["data"]["total"]
            if page * 500 >= total:
                break
            page += 1
            time.sleep(0.3)
        except Exception:
            break
    
    # 基础过滤
    pool = []
    for item in items:
        code = str(item.get("f12", ""))
        name = str(item.get("f14", ""))
        price = item.get("f2", 0) or 0
        amount_yi = (item.get("f20", 0) or 0) / 1e8
        mcap_yi = (item.get("f21", 0) or 0) / 1e8
        
        if price < COND_PARAMS["min_price"] or price > 200: continue
        if amount_yi < min_volume_yi: continue
        if mcap_yi < 20: continue
        if not code[:6].isdigit(): continue
        if "ST" in name.upper() or "退" in name: continue
        
        pool.append({"code": code, "name": name, "price": price, 
                      "amount_yi": amount_yi, "mcap_yi": mcap_yi})
    
    log.info(f"  候选池: {len(pool)} 只 (成交额>{min_volume_yi}亿)")
    return pool


def fetch_kline(code: str, days: int = 300) -> Optional[Dict]:
    """从东方财富获取K线数据。返回 {日期→ {收盘, 成交量, ...}} 字典。"""
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    secid = f"1.{code}" if code.startswith("6") else f"0.{code}"
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "end": "20500101",
        "lmt": days,
    }
    try:
        r = requests.get(url, params=params, headers=EM_HEADERS, timeout=10)
        data = r.json()
        if data.get("data") and data["data"].get("klines"):
            result = {}
            for line in data["data"]["klines"]:
                parts = line.split(",")
                result[parts[0]] = {
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "volume": float(parts[5]),
                    "amount": float(parts[6]),
                }
            return result
    except Exception:
        pass
    return None


def download_all(pool: List[Dict]) -> Dict[str, Dict]:
    """下载候选池所有股票的历史K线并缓存。"""
    cache_file = CACHE_DIR / "all_kline_cache.json"

    if cache_file.exists():
        log.info(f"\n  读取缓存: {cache_file} ({(cache_file.stat().st_size/1024/1024):.1f}MB)")
        raw = cache_file.read_text(encoding="utf-8")
        cached = json.loads(raw)
        # 检查是否完整
        cached_codes = set(cached.keys())
        need_codes = set(s["code"] for s in pool)
        missing = need_codes - cached_codes
        if not missing:
            log.info(f"  缓存完整: {len(cached)} 只股票")
            return cached
        log.info(f"  缓存缺 {len(missing)} 只，增量下载...")
        all_data = cached
    else:
        all_data = {}
        missing = set(s["code"] for s in pool)

    total = len(missing)
    for i, code in enumerate(sorted(missing), 1):
        kline = fetch_kline(code)
        if kline:
            all_data[code] = kline
        log.info(f"  [{i}/{total}] {code} {'✅' if kline else '❌'} "
                 f"({len(kline) if kline else 0} 天)")
        time.sleep(0.2)  # 限速

    # 保存缓存
    cache_file.write_text(json.dumps(all_data, default=str, ensure_ascii=False))
    log.info(f"\n  缓存已保存 ({len(all_data)} 只)")
    return all_data


# ═══════════════════════════════════════════
#  技术评分 & 筛选
# ═══════════════════════════════════════════

def compute_indicators_for_day(
    kline: Dict, date_str: str, lookback: int = 60
) -> Optional[Dict]:
    """
    对某个时间点的历史K线计算技术指标。
    只使用 date_str 之前的数据（不含当天收盘，除非已收盘）。
    返回 None 如果数据不够。
    """
    dates = sorted([d for d in kline.keys() if d <= date_str])
    if len(dates) < COND_PARAMS["min_kline_days"]:
        return None

    # 取最近 lookback 天
    recent = dates[-lookback:]
    closes = np.array([kline[d]["close"] for d in recent])
    volumes = np.array([kline[d]["volume"] for d in recent])
    n = len(closes)

    ind = {"_n": n, "latest_close": closes[-1]}

    if n >= 5:
        ind["MA5"] = float(np.mean(closes[-5:]))
    if n >= 10:
        ind["MA10"] = float(np.mean(closes[-10:]))
    if n >= 20:
        ind["MA20"] = float(np.mean(closes[-20:]))
        ind["price_MA20_pct"] = (closes[-1] / ind["MA20"] - 1) * 100
        ind["vol_MA20"] = float(np.mean(volumes[-20:]))
        ind["vol_ratio"] = volumes[-1] / max(ind["vol_MA20"], 1)

    if n >= 15:
        delta = np.diff(closes[-15:])
        gains = np.maximum(delta, 0)
        losses = np.maximum(-delta, 0)
        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)
        ind["RSI"] = 100 - 100 / (1 + avg_gain / max(avg_loss, 1e-8))
    else:
        ind["RSI"] = 50

    return ind


def screen_and_score(ind: Dict) -> Tuple[List[str], float]:
    """
    条件筛选 + 评分。
    Returns: (满足条件的标签列表, 综合评分 0-100)
    """
    conds = []
    
    # 条件A: 多头排列
    if ind.get("MA5", 0) > ind.get("MA10", 0) > ind.get("MA20", 0):
        conds.append("多头")
    elif ind.get("MA5", 0) > ind.get("MA20", 0) and ind.get("MA5", 0) > ind.get("MA10", 0) * 0.99:
        conds.append("偏多")
    
    # 条件B: 超卖反弹
    if ind.get("RSI", 50) < 35 and ind.get("latest_close", 0) > ind.get("MA5", 0):
        conds.append("超卖")
    
    # 条件C: 放量突破
    if ind.get("vol_ratio", 0) > 1.5 and ind.get("price_MA20_pct", -100) > 0.5:
        conds.append("放量")
    
    # 条件D: MA金叉
    if ind.get("_n", 0) >= 10 and ind.get("MA5", 0) > ind.get("MA10", 0):
        conds.append("金叉")

    if not conds:
        return ([], 0.0)

    # 评分
    score = 50.0
    if "多头" in conds: score += 20
    if "放量" in conds: score += 15
    if "金叉" in conds: score += 10
    if "超卖" in conds: score += 8

    rsi = ind.get("RSI", 50)
    if rsi < 30: score += 8
    elif rsi > 75: score -= 10

    pct = ind.get("price_MA20_pct", 0)
    if -3 < pct < 3: score += 5
    elif pct > 15: score -= 5

    vr = ind.get("vol_ratio", 1)
    if 1.2 < vr < 3: score += 5
    elif vr > 5: score -= 3

    return (conds, round(np.clip(score, 0, 100), 1))


# ═══════════════════════════════════════════
#  策略引擎（与 backtest.py 一致）
# ═══════════════════════════════════════════

def garch_thresholds(all_rets: List[float]) -> Tuple[int, int]:
    """滚动波动率百分位 → 动态GARCH阈值。"""
    if len(all_rets) < 2:
        return (BASE_BUY, BASE_SELL)
    
    vol_series = []
    for i in range(5, len(all_rets)):
        v = float(np.std(all_rets[max(0, i-20):i])) * np.sqrt(252)
        vol_series.append(v)

    current_vol = float(np.std(all_rets[-20:])) * np.sqrt(252) if len(all_rets) >= 20 else \
                  float(np.std(all_rets)) * np.sqrt(252)

    if len(vol_series) < 2:
        if current_vol < 0.10: p = 10.0
        elif current_vol > 0.30: p = 90.0
        else: p = 50.0
    else:
        p = float(np.sum(np.array(vol_series) <= current_vol) / len(vol_series) * 100)
        p = np.clip(p, 0, 100)

    if p >= 90:  return (BASE_BUY + 10, BASE_SELL)
    if p >= 75:  return (BASE_BUY + 5, BASE_SELL)
    if p >= 60:  return (BASE_BUY + 2, BASE_SELL)
    if p <= 10:  return (BASE_BUY - 8, BASE_SELL + 8)
    if p <= 25:  return (BASE_BUY - 3, BASE_SELL + 3)
    return (BASE_BUY, BASE_SELL)


def kelly_amount(score: float, cash: float) -> float:
    """半凯利仓位。"""
    if score >= 100: p = 0.90
    elif score >= 90: p = 0.85
    elif score >= 80: p = 0.78
    elif score >= 70: p = 0.70
    else: p = 0.50
    q = 1 - p
    f = max(0.0, (KELLY_B * p - q) / KELLY_B) * 0.5 * DIVERSIFICATION
    amount = cash * f
    return max(MIN_TRADE, min(amount, cash * 0.25, 200_000))


def risk_parity_weights(returns_by_code: Dict[str, List[float]]) -> Dict[str, float]:
    """风险平价权重 (代替马科维茨用于多股票场景)。"""
    vols = {}
    for code, rets in returns_by_code.items():
        if len(rets) >= 5:
            v = float(np.std(rets[-20:])) * np.sqrt(252) if len(rets) >= 20 else \
                float(np.std(rets)) * np.sqrt(252)
        else:
            v = 0.25
        vols[code] = max(v, 0.05)
    
    inv_var = {c: 1.0 / v for c, v in vols.items()}
    total = sum(inv_var.values())
    if total > 0:
        return {c: v / total for c, v in inv_var.items()}
    return {}


# ═══════════════════════════════════════════
#  回测主逻辑
# ═══════════════════════════════════════════

def run_backtest(cache_data: Dict) -> Dict[str, Any]:
    """执行带选股的完整回测。"""
    log.info("\n" + "=" * 60)
    log.info("📊 带选股回测启动")
    log.info("=" * 60)

    # 找出所有交易日（对齐候选池所有股票）
    all_dates = set()
    for code, kline in cache_data.items():
        all_dates.update(kline.keys())
    all_dates = sorted(all_dates)
    # 只保留 2025-05-08 ~ 2026-05-08
    start = "2025-05-08"
    end_date = "2026-05-08"
    all_dates = [d for d in all_dates if start <= d < end_date]
    log.info(f"  交易日: {len(all_dates)} 天")
    
    # 前缀匹配（A股股票代码前6位）
    def code_id(code: str) -> str:
        return code[:6].lstrip("0")

    # ── 状态初始化 ──
    cash = INITIAL_CAPITAL
    positions: Dict[str, Dict] = OrderedDict()
    trades = []
    daily_equity = []
    peak_equity = INITIAL_CAPITAL
    total_fee = 0.0
    
    # 收益率历史（用于滚动GARCH）
    return_history: Dict[str, List[float]] = {c: [] for c in cache_data}
    
    warmup = 30  # 前30天只积累数据不交易

    # ⏳ 逐日模拟
    for day_idx, date_str in enumerate(all_dates):
        is_friday = datetime.strptime(date_str, "%Y-%m-%d").weekday() == 4
        is_warmup = day_idx < warmup
        is_last_20_days = day_idx >= len(all_dates) - 20  # 最后20天减少加仓

        # ── ① 当日筛选 & 评分 ──
        screened = []  # [(code, name, score, conditions)]
        for code, kline in cache_data.items():
            ind = compute_indicators_for_day(kline, date_str)
            if ind is None:
                continue
            
            # 每日流动性过滤（消除幸存者偏差：用当天实际数据而非今天）
            if date_str in kline and ind["_n"] >= 20:
                daily_data = kline[date_str]
                daily_amt_yi = daily_data.get("amount", 0) / 1e8
                daily_price = daily_data.get("close", 0)
                if daily_amt_yi < 0.3 or daily_price < 3 or daily_price > 200:
                    continue  # 当日不符合流动性 → 跳过
            
            # 日收益率更新
            if ind["_n"] >= 2:
                prev_close = list(kline.values())[-2]["close"] if len(kline) >= 2 else ind["latest_close"]
                ret = (ind["latest_close"] / prev_close - 1) if prev_close > 0 else 0
                return_history[code].append(ret)

            if is_warmup:
                continue

            conds, score = screen_and_score(ind)
            if score > 0 and conds:
                screened.append((code, score, conds, ind))

        if is_warmup:
            daily_equity.append({
                "date": date_str, "equity": cash, "cash": cash,
                "market_value": 0, "positions": 0, "return_pct": 0.0,
            })
            continue

        # 按评分排序取前 MAX_POSITIONS*2 名
        screened.sort(key=lambda x: x[1], reverse=True)
        top_candidates = {s[0]: s for s in screened[:MAX_POSITIONS * 3]}

        # ── ② GARCH 阈值 ──
        all_rets = []
        for code in cache_data:
            all_rets.extend(return_history[code][-40:])
        buy_th, sell_th = garch_thresholds(all_rets) if all_rets else (BASE_BUY, BASE_SELL)

        # ── ③ 价格快照 ──
        day_prices = {}
        for code in cache_data:
            kline = cache_data[code]
            if date_str in kline:
                day_prices[code] = kline[date_str]["close"]

        # ── ④ 权益 & 回撤 ──
        market_value = 0
        for code, pos in list(positions.items()):
            price = day_prices.get(code, pos.get("price", 0))
            market_value += pos["quantity"] * price
        
        total_equity = cash + market_value
        peak_equity = max(peak_equity, total_equity)
        dd = (total_equity - peak_equity) / peak_equity
        is_drawdown_blocked = dd < DRAWDOWN_LIMIT

        # ── ⑤ 止损/止盈 ──
        for code in list(positions.keys()):
            pos = positions[code]
            price = day_prices.get(code, pos.get("price"))
            if price is None:
                continue
            pnl_pct = (price - pos["avg_cost"]) / pos["avg_cost"]
            
            action = None
            if pnl_pct <= STOP_LOSS:
                action = "stop_loss"
            elif pnl_pct >= TAKE_PROFIT:
                action = "take_profit"

            if action:
                qty = pos["quantity"]
                if action == "take_profit":
                    qty = max(int(qty / 2 / 100) * 100, 100)
                qty = min(qty, pos["quantity"])

                proceeds = qty * price
                fee = proceeds * 0.0003
                pnl = (price - pos["avg_cost"]) * qty - fee
                cash += proceeds - fee
                total_fee += fee
                pos["quantity"] -= qty
                trades.append({
                    "date": date_str, "code": code,
                    "side": action, "qty": qty,
                    "price": round(price, 3),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct * 100, 1),
                })
                if pos["quantity"] <= 0:
                    del positions[code]

        # ── ⑥ 卖出（评分低于门槛） ──
        for code in list(positions.keys()):
            pos = positions[code]
            info = top_candidates.get(code)
            score = info[1] if info else 0
            price = day_prices.get(code, pos.get("price"))

            if score <= sell_th and price:
                qty = pos["quantity"]
                proceeds = qty * price
                fee = proceeds * 0.0003
                pnl = (price - pos["avg_cost"]) * qty - fee
                cash += proceeds - fee
                total_fee += fee
                trades.append({
                    "date": date_str, "code": code,
                    "side": "sell", "qty": qty,
                    "price": round(price, 3),
                    "pnl": round(pnl, 2),
                    "reason": f"评分{score}≤{sell_th}",
                })
                del positions[code]

        # ── ⑦ 买入 ──
        if not is_drawdown_blocked and not is_last_20_days and len(positions) < MAX_POSITIONS:
            for code, score, conds, ind in screened:
                if code in positions:
                    continue
                if score < buy_th:
                    continue
                price = day_prices.get(code)
                if price is None or price <= 0:
                    continue

                amount = kelly_amount(score, cash)
                qty = int(amount / price / 100) * 100
                max_qty = int(cash * 0.25 / price / 100) * 100
                qty = min(qty, max_qty)

                if qty >= 100 and qty * price <= cash * 0.30:
                    cost = qty * price
                    fee = cost * 0.0003
                    cash -= cost + fee
                    total_fee += fee
                    positions[code] = {
                        "quantity": qty, "avg_cost": price,
                        "price": price, "invested": cost,
                    }
                    trades.append({
                        "date": date_str, "code": code,
                        "side": "buy", "qty": qty,
                        "price": round(price, 3),
                        "amount": round(cost + fee, 2),
                        "score": score,
                        "reason": f"评分{score}≥{buy_th}",
                    })

                if len(positions) >= MAX_POSITIONS:
                    break

        # ── ⑧ 周五风险平价再平衡 ──
        if is_friday and len(positions) >= 2:
            pos_returns = {}
            for code in positions:
                pos_returns[code] = return_history[code]
            rpw = risk_parity_weights(pos_returns)
            
            total_value = cash + market_value
            for code, target_pct in rpw.items():
                if code not in positions:
                    continue
                pos = positions[code]
                price = day_prices.get(code, pos.get("price", 0))
                if price <= 0:
                    continue
                target_val = total_value * target_pct
                cur_val = pos["quantity"] * price
                diff = target_val - cur_val

                if abs(diff) < cur_val * 0.1:
                    continue

                if diff > 0:
                    qty = int(min(diff, cash * 0.15) / price / 100) * 100
                    if qty >= 100:
                        cost = qty * price
                        fee = cost * 0.0003
                        cash -= cost + fee
                        total_fee += fee
                        total_qty = pos["quantity"] + qty
                        total_inv = pos["avg_cost"] * pos["quantity"] + cost
                        pos["avg_cost"] = total_inv / total_qty
                        pos["quantity"] = total_qty
                        trades.append({
                            "date": date_str, "code": code,
                            "side": "rebalance_buy", "qty": qty,
                            "price": round(price, 3),
                        })
                else:
                    qty = min(pos["quantity"], int(abs(diff) / price / 100) * 100)
                    if qty >= 100:
                        proceeds = qty * price
                        fee = proceeds * 0.0003
                        pnl = (price - pos["avg_cost"]) * qty - fee
                        cash += proceeds - fee
                        total_fee += fee
                        pos["quantity"] -= qty
                        if pos["quantity"] <= 0:
                            del positions[code]
                        trades.append({
                            "date": date_str, "code": code,
                            "side": "rebalance_sell", "qty": qty,
                            "price": round(price, 3),
                            "pnl": round(pnl, 2),
                        })

        # ── 更新权益 ──
        market_value = 0
        for code in list(positions.keys()):
            price = day_prices.get(code)
            if price:
                positions[code]["price"] = price
                market_value += positions[code]["quantity"] * price

        equity = cash + market_value
        peak_equity = max(peak_equity, equity)
        return_pct = (equity - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
        dd_pct = (equity - peak_equity) / peak_equity * 100

        daily_equity.append({
            "date": date_str,
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "market_value": round(market_value, 2),
            "positions": len(positions),
            "return_pct": round(return_pct, 2),
            "dd_pct": round(dd_pct, 2),
        })

    # ═══ 统计 ═══
    n_days = len(all_dates) - warmup
    years = n_days / 252
    final_eq = daily_equity[-1]["equity"]
    total_return = (final_eq - INITIAL_CAPITAL) / INITIAL_CAPITAL
    ann_return = (1 + total_return) ** (1 / max(years, 0.01)) - 1 if years > 0 else 0

    eq_series = np.array([d["equity"] for d in daily_equity[warmup:]])
    peaks = np.maximum.accumulate(eq_series)
    drawdowns = (eq_series - peaks) / peaks
    max_dd = float(np.min(drawdowns)) * 100

    if n_days > 1:
        daily_ret = np.diff(eq_series) / eq_series[:-1]
        sharpe = float(np.mean(daily_ret) / max(np.std(daily_ret), 1e-8) * np.sqrt(252))
    else:
        sharpe = 0.0

    closed_trades = [t for t in trades if t["side"] in ("sell", "stop_loss", "take_profit")]
    wins = [t for t in closed_trades if t.get("pnl", 0) > 0]
    losses = [t for t in closed_trades if t.get("pnl", 0) <= 0]
    win_rate = len(wins) / len(closed_trades) * 100 if closed_trades else 0
    avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
    avg_loss = abs(np.mean([t["pnl"] for t in losses])) if losses else 0

    return {
        "summary": {
            "total_return_pct": round(total_return * 100, 2),
            "ann_return_pct": round(ann_return * 100, 2),
            "sharpe_ratio": round(sharpe, 3),
            "max_drawdown_pct": round(max_dd, 2),
            "win_rate_pct": round(win_rate, 1),
            "profit_factor": round(avg_win / avg_loss, 2) if avg_loss > 0 else "N/A",
            "total_trades": len(trades),
            "closed_trades": len(closed_trades),
            "buy_trades": len([t for t in trades if t["side"] == "buy"]),
            "sell_trades": len([t for t in trades if t["side"] == "sell"]),
            "stop_loss_trades": len([t for t in trades if t["side"] == "stop_loss"]),
            "take_profit_trades": len([t for t in trades if t["side"] == "take_profit"]),
            "total_fee": round(total_fee, 2),
            "final_equity": round(final_eq, 2),
            "pool_size": len(cache_data),
            "trading_days": n_days,
            "years": round(years, 2),
        },
        "trades": trades[-100:],
        "daily_equity": daily_equity,
    }


# ═══════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════

if __name__ == "__main__":
    t0 = time.time()
    
    # 1. 获取候选池
    pool = get_liquid_stocks(min_volume_yi=0.3)
    if not pool:
        log.error("候选池为空，无法回测")
        sys.exit(1)
    
    # 2. 下载历史数据
    log.info("\n[1/3] 下载历史K线数据...")
    cache = download_all(pool)
    
    # 3. 回测
    log.info("\n[2/3] 执行逐日回测...")
    result = run_backtest(cache)
    
    # 4. 报告
    s = result["summary"]
    log.info("\n" + "=" * 60)
    log.info(f"📊 带选股回测结果 (候选池 {s['pool_size']} 只)")
    log.info("=" * 60)
    log.info(f"  总收益率:     {s['total_return_pct']:>+8.2f}%")
    log.info(f"  年化收益率:   {s['ann_return_pct']:>+8.2f}%")
    log.info(f"  夏普比率:     {s['sharpe_ratio']:>8.3f}")
    log.info(f"  最大回撤:     {s['max_drawdown_pct']:>+8.2f}%")
    log.info(f"  胜率:         {s['win_rate_pct']:>8.1f}%")
    log.info(f"  盈亏比:       {s['profit_factor']:>8}")
    log.info(f"  总交易:       {s['total_trades']:>8} 笔")
    log.info(f"  (买入{s['buy_trades']}/卖出{s['sell_trades']}"
             f"/止损{s['stop_loss_trades']}/止盈{s['take_profit_trades']})")
    log.info(f"  最终权益:     {s['final_equity']:>10.2f}")
    log.info(f"  耗时:     {time.time()-t0:.0f} 秒")
    
    # 保存
    out_path = Path("/opt/daily_stock_analysis/simulated_trading/backtest_screener_result.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    log.info(f"\n✅ 结果已保存至 {out_path}")
