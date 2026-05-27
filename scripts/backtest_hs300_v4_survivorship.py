#!/usr/bin/env python3
"""
沪深300 × 1年 回测 V4 — 消除幸存者偏差（精简版）
=========================================
只跑两轮：V1纯技术 + V3大盘均线过滤，均带纳入日期过滤
"""
import sys, json, time, logging, math
from pathlib import Path
from datetime import datetime

sys.path.insert(0, "/opt/daily_stock_analysis")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("hs300_v4")

import pandas as pd
import numpy as np

OUTPUT_DIR = Path("/opt/daily_stock_analysis/reports")
CACHE_DIR = Path("/opt/daily_stock_analysis/data/cache")
MIN_KLINE = 250
INITIAL_CASH = 30000.0
TOP_N = 5


def load_hs300_index():
    df = pd.read_pickle(str(CACHE_DIR / "hs300_index.pkl"))
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    df["ma120"] = df["close"].rolling(120).mean()
    return df


def get_market_scale(hs300, today):
    td = hs300[hs300["date"] <= pd.Timestamp(today)]
    if len(td) < 5:
        return 1.0
    close = td["close"].iloc[-1]
    ma5 = td["ma5"].iloc[-1]
    ma20 = td["ma20"].iloc[-1]
    ma60 = td["ma60"].dropna().iloc[-1] if not td["ma60"].dropna().empty else 0
    ma120 = td["ma120"].dropna().iloc[-1] if not td["ma120"].dropna().empty else 0
    above_ma60 = close > ma60 if ma60 > 0 else True
    above_ma120 = close > ma120 if ma120 > 0 else True
    below_ma60_ma5ltma20 = (close <= ma60 and ma5 < ma20) if ma60 > 0 else False
    if above_ma60: return 1.0
    if above_ma120: return 0.5
    if below_ma60_ma5ltma20: return 0.0
    return 0.25


def calc_technicals(df):
    if df is None or len(df) < 30:
        return {"score": 0}
    close = df["close"].values
    volume = df["volume"].values if "volume" in df.columns else None
    ma5 = close[-5:].mean() if len(close) >= 5 else close.mean()
    ma20 = close[-20:].mean() if len(close) >= 20 else close.mean()
    ma60 = close[-60:].mean() if len(close) >= 60 else close.mean()
    price = close[-1]
    score = 50
    if ma5 > ma20: score += 15
    if ma20 > ma60: score += 15
    if price > ma5: score += 10
    elif price < ma20: score -= 10
    if volume is not None and len(volume) >= 5:
        vma5 = volume[-5:].mean()
        if vma5 > 0:
            vr = volume[-1] / vma5
            if vr > 1.5: score += 15
            elif vr > 1.2: score += 10
            elif vr > 1.0: score += 5
            if vr < 0.5: score -= 10
    if len(close) >= 15:
        g = l = 0
        for i in range(-14, 0):
            d = close[i] - close[i-1]
            if d > 0: g += d
            else: l -= d
        rsi = 50 if l == 0 else (g / (g + l) * 100) if (g + l) > 0 else 50
        if rsi > 70: score -= 5
        elif rsi < 30: score += 5
        else: score += 5
    if len(close) >= 20:
        rets = [(close[i] - close[i-1]) / close[i-1] for i in range(-19, 0)]
        vol = max(rets) - min(rets)
        if vol < 0.05: score += 8
        elif vol > 0.15: score -= 5
        else: score += 3
    return {"score": max(0, min(100, score))}


def load_hs300_stocks():
    """加载K线 + 纳入日期"""
    from data_provider.data_cache import DataCache
    cache = DataCache()

    hs300_list = CACHE_DIR / "stock_list" / "hs300.json"
    if not hs300_list.exists():
        import akshare as ak
        df = ak.index_stock_cons(symbol="000300")
        hs300_list.parent.mkdir(parents=True, exist_ok=True)
        with open(hs300_list, "w") as f:
            json.dump(df.to_dict('records'), f, ensure_ascii=False, default=str)
    with open(hs300_list) as f:
        stock_records = json.load(f)

    all_kline = {}
    include_dates = {}
    for rec in stock_records:
        code = rec["品种代码"]
        inc = rec.get("纳入日期", "2000-01-01")
        try: inc_ymd = pd.Timestamp(inc).strftime("%Y%m%d")
        except: inc_ymd = "20000101"
        df = cache.get_kline(code)
        if df is not None and len(df) >= MIN_KLINE:
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date")
            all_kline[code] = df
            include_dates[code] = inc_ymd

    logger.info(f"有效K线: {len(all_kline)} 只, 含纳入日期过滤")

    # 统计可用池
    for cd in ["20240515", "20240930", "20250212", "20251216", "20260522"]:
        n = sum(1 for code, inc in include_dates.items()
                if cd >= inc
                and len(all_kline[code][all_kline[code]["date"] <= pd.Timestamp(cd)]) >= 30)
        logger.info(f"  {cd}: {n} 只有资格")
    return all_kline, include_dates


def run_backtest(all_kline, hs300, include_dates, use_market_filter):
    all_dates = sorted(set(
        d for df in all_kline.values()
        for d in df["date"].dt.strftime("%Y%m%d").values
    ))
    label = "V1+大盘均线过滤（幸存者修正）" if use_market_filter else "V1纯技术（幸存者修正）"

    cash = INITIAL_CASH
    position = {}
    trades = []
    equity_curve = []
    peak = INITIAL_CASH
    scale_log = []
    excluded_count = 0

    start_idx = next((i for i in range(len(all_dates)) if i >= 20), 20)

    for i in range(start_idx, len(all_dates)):
        today = all_dates[i]

        # 卖出
        if position:
            for code in list(position.keys()):
                kline = all_kline.get(code)
                if kline is None: continue
                td = kline[kline["date"] == pd.Timestamp(today)]
                price = td["close"].iloc[-1] if not td.empty else kline["close"].iloc[-1]
                pos = position[code]
                val = pos["qty"] * price
                fee = val * 0.0005
                cash += val - fee
                pnl = (price - pos["avg_cost"]) * pos["qty"] - fee
                trades.append({"date": today, "code": code, "action": "sell", "price": price, "pnl": pnl})
            position = {}

        # 评分（纳入日期过滤）
        candidates = []
        for code, kline in all_kline.items():
            inc_date = include_dates.get(code, "20000101")
            if today < inc_date:
                excluded_count += 1
                continue
            td = kline[kline["date"] <= pd.Timestamp(today)]
            if len(td) < 30: continue
            tech = calc_technicals(td)
            candidates.append({"code": code, "score": tech["score"], "price": td["close"].iloc[-1]})
        candidates.sort(key=lambda x: -x["score"])

        scale = get_market_scale(hs300, today) if use_market_filter else 1.0
        scale_log.append({"date": today, "scale": scale})

        # 买入
        if candidates and scale > 0:
            top = candidates[:TOP_N]
            usable = cash * scale
            per = usable / len(top)
            for c in top:
                price = c["price"]
                if price <= 0: continue
                qty = max(int(per / price / 100) * 100, 1)
                if qty <= 0: continue
                cost = qty * price
                fee = cost * 0.0005
                total = cost + fee
                if total > cash or total > usable: continue
                cash -= total
                position[c["code"]] = {"qty": qty, "avg_cost": price}

        # 权益
        pos_val = 0
        for code, pos in position.items():
            kline = all_kline.get(code)
            if kline is None: continue
            td = kline[kline["date"] == pd.Timestamp(today)]
            price = td["close"].iloc[-1] if not td.empty else kline["close"].iloc[-1]
            pos_val += pos["qty"] * price
        eq = cash + pos_val
        equity_curve.append({"date": today, "equity": eq})
        peak = max(peak, eq)

    # 平仓
    for code, pos in position.items():
        kline = all_kline.get(code)
        if kline is None: continue
        price = kline["close"].iloc[-1]
        val = pos["qty"] * price
        cash += val - val * 0.0005
    final = cash

    total_ret = (final - INITIAL_CASH) / INITIAL_CASH * 100
    max_dd = 0
    pk = INITIAL_CASH
    for e in equity_curve:
        pk = max(pk, e["equity"])
        dd = (e["equity"] - pk) / pk * 100
        max_dd = min(max_dd, dd)

    win = sum(1 for t in trades if t.get("pnl", 0) > 0)
    loss = sum(1 for t in trades if t.get("pnl", 0) < 0)

    daily_ret = []
    for j in range(1, len(equity_curve)):
        r = (equity_curve[j]["equity"] - equity_curve[j-1]["equity"]) / equity_curve[j-1]["equity"]
        daily_ret.append(r)
    sharpe = (np.mean(daily_ret) / np.std(daily_ret) * math.sqrt(244)) if daily_ret and np.std(daily_ret) > 0 else 0

    avg_scale = np.mean([s["scale"] for s in scale_log]) if scale_log else 1.0
    days_cash = sum(1 for s in scale_log if s["scale"] == 0)
    days_full = sum(1 for s in scale_log if s["scale"] >= 1.0)

    return {
        "strategy": label,
        "total_return_pct": round(total_ret, 2),
        "final_value": round(final, 2),
        "max_drawdown_pct": round(abs(max_dd), 2),
        "sharpe_ratio": round(sharpe, 2),
        "total_trades": len(trades),
        "win_rate_pct": round(win / (win + loss) * 100, 2) if (win + loss) > 0 else 0,
        "avg_scale": round(avg_scale, 2),
        "days_full_cash": f"{days_cash}/{len(scale_log)}",
        "days_full_pos": f"{days_full}/{len(scale_log)}",
        "excluded_by_date": excluded_count,
        "equity_curve": equity_curve,
    }


def main():
    t0 = time.time()
    logger.info("=" * 60)
    logger.info("沪深300回测 V4 — 消除幸存者偏差")
    logger.info("=" * 60)

    all_kline, include_dates = load_hs300_stocks()
    hs300 = load_hs300_index()

    logger.info("跑 V1纯技术（幸存者修正）...")
    r1 = run_backtest(all_kline, hs300, include_dates, use_market_filter=False)

    logger.info("跑 V1+大盘均线过滤（幸存者修正）...")
    r2 = run_backtest(all_kline, hs300, include_dates, use_market_filter=True)

    results = [r1, r2]

    print("\n" + "=" * 100)
    print(f"{'策略':<30} {'收益%':>8} {'最终':>10} {'回撤%':>8} "
          f"{'夏普':>8} {'胜率%':>8} {'均仓':>6} {'排除':>6}")
    print("=" * 100)
    for r in results:
        print(f"{r['strategy']:<30} {r['total_return_pct']:>+8.2f} "
              f"{r['final_value']:>10.2f} {r['max_drawdown_pct']:>8.2f} "
              f"{r['sharpe_ratio']:>8.2f} {r['win_rate_pct']:>8.2f} "
              f"{r['avg_scale']:>6.2f} {r.get('excluded_by_date',0):>6}")
    print("=" * 100)

    # 与旧版对比（读取旧报告）
    v3_reports = sorted(OUTPUT_DIR.glob("backtest_hs300_v3_*.json"))
    if v3_reports:
        with open(v3_reports[-1]) as f:
            old = json.load(f)
        old_v1 = old['strategies'][0]
        old_v3 = old['strategies'][1]
        print(f"\n与旧版(V3, 无幸存者修正)对比:")
        print(f"  V1纯技术: 旧{old_v1['total_return_pct']:+.2f}% → 新{r1['total_return_pct']:+.2f}%"
              f"  (Δ{r1['total_return_pct']-old_v1['total_return_pct']:+.2f}%)")
        print(f"  回撤:     旧{old_v1['max_drawdown_pct']:.2f}% → 新{r1['max_drawdown_pct']:.2f}%")
        print(f"  V3均线过滤: 旧{old_v3['total_return_pct']:+.2f}% → 新{r2['total_return_pct']:+.2f}%"
              f"  (Δ{r2['total_return_pct']-old_v3['total_return_pct']:+.2f}%)")
        print(f"  回撤:     旧{old_v3['max_drawdown_pct']:.2f}% → 新{r2['max_drawdown_pct']:.2f}%")

    out = OUTPUT_DIR / f"backtest_hs300_v4_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(out, "w") as f:
        json.dump({"run_date": datetime.now().isoformat(), "stock_count": len(all_kline),
                    "strategies": results}, f, ensure_ascii=False, indent=2)
    logger.info(f"已保存: {out}")
    logger.info(f"耗时: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
