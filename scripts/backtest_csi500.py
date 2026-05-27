#!/usr/bin/env python3
"""
中证500回测 — 三策略对比（单轮优化版）
======================================
"""
import sys, json, time, logging, math
from pathlib import Path
from datetime import datetime

sys.path.insert(0, "/opt/daily_stock_analysis")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("csi500_bt")

import pandas as pd
import numpy as np

CACHE_DIR = Path("/opt/daily_stock_analysis/data/cache")
FUND_FILE = CACHE_DIR / "fundamentals" / "csi500_cache.json"
OUTPUT_DIR = Path("/opt/daily_stock_analysis/reports")
MIN_KLINES = 250
INITIAL_CASH = 30000.0
TOP_N = 5


def calc_technicals(df):
    if df is None or len(df) < 30:
        return 0
    close = df["close"].values
    volume = df["volume"].values if "volume" in df.columns else None
    ma5 = close[-5:].mean() if len(close) >= 5 else close.mean()
    ma20 = close[-20:].mean() if len(close) >= 20 else close.mean()
    ma60 = close[-60:].mean() if len(close) >= 60 else close.mean()
    score = 50
    if ma5 > ma20: score += 15
    if ma20 > ma60: score += 15
    if close[-1] > ma5: score += 10
    elif close[-1] < ma20: score -= 10
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
    if len(close) >= 20:
        rets = [(close[i] - close[i-1]) / close[i-1] for i in range(-19, 0)]
        vol = max(rets) - min(rets)
        if vol < 0.05: score += 8
        elif vol > 0.15: score -= 5
        else: score += 3
    return max(0, min(100, score))


def get_market_scale(hs300, today):
    td = hs300[hs300["date"] <= pd.Timestamp(today)]
    if len(td) < 5: return 1.0
    c = td["close"].iloc[-1]
    ma5 = td["ma5"].iloc[-1]
    ma20 = td["ma20"].iloc[-1]
    ma60 = td["ma60"].dropna().iloc[-1] if not td["ma60"].dropna().empty else 0
    ma120 = td["ma120"].dropna().iloc[-1] if not td["ma120"].dropna().empty else 0
    if c > ma60: return 1.0
    if c > ma120: return 0.5
    if c <= ma60 and ma5 < ma20: return 0.0
    return 0.25


def simulate(all_kline, hs300, fundamentals, use_fund, use_ma):
    """单次回测"""
    all_dates = sorted(set(
        d for df in all_kline.values()
        for d in df["date"].dt.strftime("%Y%m%d").values
    ))
    label = "V1纯技术"
    if use_fund: label = "V1+基本面"
    if use_ma: label = "V1+MA风控"

    cash = [INITIAL_CASH]
    pos = [{}]
    eq_curve = [[]]

    start = next(i for i in range(len(all_dates)) if i >= 20)
    total_trades = [0]
    peak = [INITIAL_CASH]

    for idx in range(start, len(all_dates)):
        today = all_dates[idx]

        # 卖（持仓满2个交易日后才卖出）
        sold_today = False
        if pos[0]:
            for code in list(pos[0].keys()):
                p = pos[0][code]
                if idx - p.get("entry_idx", idx) < 2:
                    continue
                kline = all_kline.get(code)
                if kline is None: continue
                td = kline[kline["date"] == pd.Timestamp(today)]
                price = td["close"].iloc[-1] if not td.empty else kline["close"].iloc[-1]
                val = p["qty"] * price
                cash[0] += val - val * 0.00075
                del pos[0][code]
                sold_today = True

        # 评分
        scorable = {}
        for code, kline in all_kline.items():
            td = kline[kline["date"] <= pd.Timestamp(today)]
            if len(td) < 30: continue
            scorable[code] = (calc_technicals(td), td["close"].iloc[-1])

        # 选Top5
        ranked = sorted(scorable.items(), key=lambda x: -x[1][0])
        top5 = ranked[:TOP_N]

        # 基本面过滤（从top5里筛）
        if use_fund:
            top5 = [(c, s) for c, s in top5 if
                    (fundamentals.get(c, {}) or {}).get("roe", 0) is not None
                    and (fundamentals.get(c, {}) or {}).get("roe", 0) > 5
                    and (fundamentals.get(c, {}) or {}).get("revenue_yoy", 0) is not None
                    and (fundamentals.get(c, {}) or {}).get("revenue_yoy", 0) > 0]

        # MA风控
        scale = get_market_scale(hs300, today) if use_ma else 1.0

        # 买（有仓位可卖时才重新买入；无仓位时正常买入建仓）
        if top5 and scale > 0 and (sold_today or not pos[0]):
            usable = cash[0] * scale
            per = usable / len(top5)
            for code, (sc, price) in top5:
                if price <= 0: continue
                qty = max(int(per / price / 100) * 100, 1)
                if qty <= 0: continue
                cost = qty * price
                fee = cost * 0.00025
                total = cost + fee
                if total > cash[0] or total > usable: continue
                cash[0] -= total
                pos[0][code] = {"qty": qty, "avg_cost": price, "entry_idx": idx}
                total_trades[0] += 1

        # 权益
        pv = 0
        for code, p in pos[0].items():
            kline = all_kline.get(code)
            if kline is None: continue
            td = kline[kline["date"] == pd.Timestamp(today)]
            price = td["close"].iloc[-1] if not td.empty else kline["close"].iloc[-1]
            pv += p["qty"] * price
        eq = cash[0] + pv
        eq_curve[0].append(eq)
        peak[0] = max(peak[0], eq)

    # 平仓
    for code, p in pos[0].items():
        kline = all_kline.get(code)
        if kline is None: continue
        price = kline["close"].iloc[-1]
        val = p["qty"] * price
        cash[0] += val - val * 0.00075
    final = cash[0]

    ret = (final - INITIAL_CASH) / INITIAL_CASH * 100
    max_dd = 0
    pk = INITIAL_CASH
    for e in eq_curve[0]:
        pk = max(pk, e)
        dd = (e - pk) / pk * 100
        max_dd = min(max_dd, dd)
    daily = []
    for i in range(1, len(eq_curve[0])):
        daily.append((eq_curve[0][i] - eq_curve[0][i-1]) / eq_curve[0][i-1])
    sharpe = (np.mean(daily) / np.std(daily) * math.sqrt(244)) if daily and np.std(daily) > 0 else 0
    return {
        "strategy": label, "total_return_pct": round(ret, 2),
        "final_value": round(final, 2), "max_drawdown_pct": round(abs(max_dd), 2),
        "sharpe_ratio": round(sharpe, 2), "trading_days": len(eq_curve[0]),
        "total_trades": total_trades[0],
    }


def main():
    t0 = time.time()
    logger.info("中证500回测 — 三策略对比")

    from data_provider.data_cache import DataCache
    cache = DataCache()
    with open(CACHE_DIR / "stock_list" / "csi500.json") as f:
        codes = list(set(s['品种代码'] for s in json.load(f)))
    all_kline = {}
    for code in codes:
        df = cache.get_kline(code)
        if df is not None and len(df) >= MIN_KLINES:
            df = df.copy(); df["date"] = pd.to_datetime(df["date"]); df = df.sort_values("date")
            all_kline[code] = df
    logger.info(f"K线: {len(all_kline)}/{len(codes)}")

    # 沪深300指数
    hs300 = pd.read_pickle(str(CACHE_DIR / "hs300_index.pkl"))
    hs300["date"] = pd.to_datetime(hs300["date"])
    hs300 = hs300.sort_values("date").reset_index(drop=True)
    for m in [5, 20, 60, 120]:
        hs300[f"ma{m}"] = hs300["close"].rolling(m).mean()

    # 基本面
    fundamentals = json.load(open(FUND_FILE)) if FUND_FILE.exists() else {}

    # 跑三个策略
    r1 = simulate(all_kline, hs300, fundamentals, False, False)
    r2 = simulate(all_kline, hs300, fundamentals, True, False)
    r3 = simulate(all_kline, hs300, fundamentals, False, True)
    results = [r1, r2, r3]

    print("\n" + "=" * 78)
    print(f"{'策略':<22} {'收益%':>8} {'最终':>10} {'回撤%':>8} {'夏普':>8} {'交易':>8}")
    print("=" * 78)
    for r in results:
        print(f"{r['strategy']:<22} {r['total_return_pct']:>+8.2f} "
              f"{r['final_value']:>10.2f} {r['max_drawdown_pct']:>8.2f} "
              f"{r['sharpe_ratio']:>8.2f} {r['total_trades']:>8}")
    print("=" * 78)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / f"backtest_csi500_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(out, "w") as f:
        json.dump({"run_date": datetime.now().isoformat(), "stock_count": len(all_kline),
                    "strategies": results}, f, ensure_ascii=False, indent=2)
    logger.info(f"已保存: {out} | 耗时: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
