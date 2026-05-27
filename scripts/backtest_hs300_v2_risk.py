#!/usr/bin/env python3
"""
沪深300 × 1年 回测 V2 — 对比 V1 vs V1+自适应回撤降仓
==================================================
风控规则（不改评分函数）：
  20日滚动回撤 ≤5%  → 满仓 (1.0)
          5~10%    → 半仓 (0.5)
          10~15%   → 轻仓 (0.25)
          >15%     → 空仓 (0.0)
"""
import sys, os, json, time, logging, math
from pathlib import Path
from datetime import datetime

sys.path.insert(0, "/opt/daily_stock_analysis")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("hs300_v2")

import pandas as pd
import numpy as np

CACHE_DIR = Path("/opt/daily_stock_analysis/data/cache")
OUTPUT_DIR = Path("/opt/daily_stock_analysis/reports")
MIN_KLINE = 250
INITIAL_CASH = 30000.0
TOP_N = 5


def calc_20d_max_dd(equity_window: list) -> float:
    """20日滚动最大回撤（%）"""
    if len(equity_window) < 5:
        return 0.0
    pk = max(equity_window)
    trough = min(equity_window)
    return (trough - pk) / pk * 100 if pk != 0 else 0.0


def get_position_scale(rolling_dd: float) -> float:
    """自适应回撤降仓"""
    dd = abs(rolling_dd)
    if dd <= 5: return 1.0
    elif dd <= 10: return 0.5
    elif dd <= 15: return 0.25
    else: return 0.0


def calc_technicals(df):
    """V1 评分函数（不变）"""
    if df is None or len(df) < 30:
        return {"score": 0}
    close = df["close"].values
    volume = df["volume"].values if "volume" in df.columns else None
    ma5 = close[-5:].mean() if len(close) >= 5 else close.mean()
    ma20 = close[-20:].mean() if len(close) >= 20 else close.mean()
    ma60 = close[-60:].mean() if len(close) >= 60 else close.mean()
    price = close[-1]
    score = 50
    # 趋势
    if ma5 > ma20: score += 15
    if ma20 > ma60: score += 15
    if price > ma5: score += 10
    elif price < ma20: score -= 10
    # 量价
    if volume is not None and len(volume) >= 5:
        vma5 = volume[-5:].mean()
        if vma5 > 0:
            vr = volume[-1] / vma5
            if vr > 1.5: score += 15
            elif vr > 1.2: score += 10
            elif vr > 1.0: score += 5
            if vr < 0.5: score -= 10
    # RSI
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
    # 波动率
    if len(close) >= 20:
        rets = [(close[i] - close[i-1]) / close[i-1] for i in range(-19, 0)]
        vol = max(rets) - min(rets)
        if vol < 0.05: score += 8
        elif vol > 0.15: score -= 5
        else: score += 3
    return {"score": max(0, min(100, score))}


def run_backtest(all_kline: dict, use_risk: bool = False) -> dict:
    """回测核心"""
    all_dates = sorted(set(
        d for df in all_kline.values()
        for d in df["date"].dt.strftime("%Y%m%d").values
    ))
    logger.info(f"交易日: {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)} 天)")

    cash = INITIAL_CASH
    position = {}
    trades = []
    equity_curve = []
    peak = INITIAL_CASH
    rolling_20d = []  # 用于风控的滚动窗口

    label = "V1+自适应降仓" if use_risk else "V1纯技术"
    start_idx = next((i for i in range(len(all_dates)) if i >= 20), 20)

    for i in range(start_idx, len(all_dates)):
        today = all_dates[i]

        # ── 卖出 ──
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
                trades.append({"date": today, "code": code, "action": "sell", "qty": pos["qty"], "price": price, "pnl": pnl})
            position = {}

        # ── 评分 ──
        candidates = []
        for code, kline in all_kline.items():
            td = kline[kline["date"] <= pd.Timestamp(today)]
            if len(td) < 30: continue
            tech = calc_technicals(td)
            candidates.append({"code": code, "score": tech["score"], "price": td["close"].iloc[-1]})

        candidates.sort(key=lambda x: -x["score"])
        top = candidates[:TOP_N]

        # ── 风控：仓位系数 ──
        scale = 1.0
        if use_risk and len(rolling_20d) >= 5:
            dd = calc_20d_max_dd(rolling_20d)
            scale = get_position_scale(dd)

        # ── 买入 ──
        if top and scale > 0:
            usable_cash = cash * scale
            per = usable_cash / len(top)
            for c in top:
                price = c["price"]
                if price <= 0: continue
                qty = max(int(per / price / 100) * 100, 1)
                if qty <= 0: continue
                cost = qty * price
                fee = cost * 0.0005
                total = cost + fee
                if total > cash or total > usable_cash: continue
                cash -= total
                position[c["code"]] = {"qty": qty, "avg_cost": price}
                trades.append({"date": today, "code": c["code"], "action": "buy", "qty": qty, "price": price, "score": c["score"]})

        # ── 权益 ──
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

        # 更新滚动窗口（仅风控模式需要）
        rolling_20d.append(eq)
        if len(rolling_20d) > 20:
            rolling_20d.pop(0)

    # 平仓
    for code, pos in position.items():
        kline = all_kline.get(code)
        if kline is None: continue
        price = kline["close"].iloc[-1]
        val = pos["qty"] * price
        cash += val - val * 0.0005

    final = cash

    # ── 指标 ──
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
    for i in range(1, len(equity_curve)):
        ret = (equity_curve[i]["equity"] - equity_curve[i-1]["equity"]) / equity_curve[i-1]["equity"]
        daily_ret.append(ret)
    sharpe = (np.mean(daily_ret) / np.std(daily_ret) * math.sqrt(244)) if daily_ret and np.std(daily_ret) > 0 else 0

    return {
        "strategy": label,
        "stock_universe": len(all_kline),
        "trading_days": len(equity_curve),
        "total_trades": win + loss,
        "total_return_pct": round(total_ret, 2),
        "final_value": round(final, 2),
        "win_rate_pct": round(win / (win + loss) * 100, 2) if (win + loss) > 0 else 0,
        "max_drawdown_pct": round(abs(max_dd), 2),
        "sharpe_ratio": round(sharpe, 2),
        "wins": win,
        "losses": loss,
        "equity_curve": equity_curve,
    }


def load_hs300_data() -> dict:
    """加载沪深300成分股K线"""
    from data_provider.data_cache import DataCache
    cache = DataCache()

    hs300_file = CACHE_DIR / "stock_list" / "hs300.json"
    if not hs300_file.exists():
        import akshare as ak
        df = ak.index_stock_cons(symbol="000300")
        hs300_file.parent.mkdir(parents=True, exist_ok=True)
        with open(hs300_file, "w") as f:
            json.dump(df.to_dict('records'), f, ensure_ascii=False)
    with open(hs300_file) as f:
        codes = [s['品种代码'] for s in json.load(f)]

    all_kline = {}
    for code in codes:
        df = cache.get_kline(code)
        if df is not None and len(df) >= MIN_KLINE:
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date")
            all_kline[code] = df

    logger.info(f"有效K线: {len(all_kline)}/{len(codes)} 只")
    return all_kline


def main():
    t0 = time.time()
    logger.info("=" * 60)
    logger.info("沪深300回测 V2 — 对比 V1 vs V1+风控")
    logger.info("=" * 60)

    all_kline = load_hs300_data()
    if len(all_kline) < 50:
        logger.error(f"有效股票不足 ({len(all_kline)})")
        return

    r1 = run_backtest(all_kline, use_risk=False)
    r2 = run_backtest(all_kline, use_risk=True)
    results = [r1, r2]

    print("\n" + "=" * 70)
    print(f"{'策略':<22} {'收益%':>8} {'最终':>10} {'胜率%':>8} {'回撤%':>8} {'夏普':>8} {'交易':>8}")
    print("=" * 70)
    for r in results:
        print(f"{r['strategy']:<22} {r['total_return_pct']:>+8.2f} "
              f"{r['final_value']:>10.2f} {r['win_rate_pct']:>8.2f} "
              f"{r['max_drawdown_pct']:>8.2f} {r['sharpe_ratio']:>8.2f} "
              f"{r['total_trades']:>8}")
    print("=" * 70)

    # 改善统计
    imp = {
        'return_change': round(r2['total_return_pct'] - r1['total_return_pct'], 2),
        'dd_change': round(r2['max_drawdown_pct'] - r1['max_drawdown_pct'], 2),
        'sharpe_change': round(r2['sharpe_ratio'] - r1['sharpe_ratio'], 2),
    }
    print(f"  收益变化: {imp['return_change']:+.2f}%  |  回撤变化: {imp['dd_change']:+.2f}%  |  夏普变化: {imp['sharpe_change']:+.2f}")
    print()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / f"backtest_hs300_v2_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(out, "w") as f:
        json.dump({"run_date": datetime.now().isoformat(), "stock_count": len(all_kline),
                    "improvement": imp, "strategies": results}, f, ensure_ascii=False, indent=2)
    logger.info(f"已保存: {out}")
    logger.info(f"耗时: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
