#!/usr/bin/env python3
"""
100只股票 × 1年回测（使用缓存基本面）
======================================
用之前缓存的100只股票K线+基本面数据做回测，
不依赖 JQData/akshare 实时拉取，全部读磁盘缓存。
"""
import sys, json, time, logging, math
from pathlib import Path
from datetime import datetime

sys.path.insert(0, "/opt/daily_stock_analysis")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("backtest_100")

CACHE_DIR = Path("/opt/daily_stock_analysis/data/cache")
FUND_CACHE_FILE = CACHE_DIR / "fundamentals" / "backtest_100_fundamental_cache.json"
STOCK_LIST_FILE = CACHE_DIR / "stock_list" / "backtest_100.json"
OUTPUT_DIR = Path("/opt/daily_stock_analysis/reports")

MIN_KLINE = 250       # 至少250行
INITIAL_CASH = 30000.0
TOP_N = 5             # 每天选前5只


def load_stock_list() -> list:
    """加载100只股票列表"""
    if STOCK_LIST_FILE.exists():
        with open(STOCK_LIST_FILE) as f:
            data = json.load(f)
        return [s["code"] for s in data["stocks"]]
    else:
        # 从 Kline 缓存选
        from data_provider.data_cache import DataCache
        cache = DataCache()
        kline_dir = Path(cache.stats()["cache_root"]) / "kline"
        stocks = []
        for f in os.listdir(kline_dir):
            if not f.endswith("_meta.json"):
                continue
            code = f.replace("_meta.json", "")
            try:
                with open(kline_dir / f) as fp:
                    meta = json.load(fp)
                rows = meta.get("rows", 0)
                stocks.append((code, rows))
            except:
                pass
        stocks.sort(key=lambda x: -x[1])
        return [s[0] for s in stocks[:100]]


def load_fundamentals() -> dict:
    """加载基本面缓存"""
    if FUND_CACHE_FILE.exists():
        with open(FUND_CACHE_FILE) as f:
            return json.load(f)
    return {}


def load_kline(code: str) -> list:
    """从缓存加载K线数据，返回按时间排序的DataFrame"""
    from data_provider.data_cache import DataCache
    cache = DataCache()
    df = cache.get_kline(code)
    if df is None or df.empty:
        return None
    df = df.copy()
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
    return df


def calc_technicals(df) -> dict:
    """
    计算技术评分（与 backtest_compare.py 的 strategy_pure_tech 一致）
    返回最新一个交易日的评分
    """
    if df is None or len(df) < 30:
        return {"score": 0, "ma5": 0, "ma20": 0, "ma60": 0, "vol_ma5": 0, "rsi": 50}

    close = df["close"].values
    volume = df["volume"].values if "volume" in df.columns else None
    high = df["high"].values if "high" in df.columns else None
    low = df["low"].values if "low" in df.columns else None

    # 均线
    ma5 = close[-5:].mean() if len(close) >= 5 else close.mean()
    ma20 = close[-20:].mean() if len(close) >= 20 else close.mean()
    ma60 = close[-60:].mean() if len(close) >= 60 else close.mean()

    # 价格位置
    price = close[-1]
    score = 50  # 基础分

    # 趋势因子 (40分)
    trend_score = 0
    if ma5 > ma20:
        trend_score += 15
    if ma20 > ma60:
        trend_score += 15
    if price > ma5:
        trend_score += 10
    elif price < ma20:
        trend_score -= 10
    score += trend_score

    # 量价因子 (30分)
    vol_score = 0
    if volume is not None and len(volume) >= 5:
        vol_ma5 = volume[-5:].mean()
        if vol_ma5 > 0:
            vol_ratio = volume[-1] / vol_ma5
            if vol_ratio > 1.5:
                vol_score += 15
            elif vol_ratio > 1.2:
                vol_score += 10
            elif vol_ratio > 1.0:
                vol_score += 5
            if vol_ratio < 0.5:
                vol_score -= 10
    score += vol_score

    # RSI (15分)
    rsi = 50
    if len(close) >= 15:
        gains = 0
        losses = 0
        for i in range(-14, 0):
            diff = close[i] - close[i-1]
            if diff > 0:
                gains += diff
            else:
                losses -= diff
        if gains + losses > 0:
            rsi = 50 if losses == 0 else (gains / (gains + losses) * 100)
        if rsi > 70:
            score -= 5  # 超买
        elif rsi < 30:
            score += 5   # 超卖
        else:
            score += 5

    # 波动率调整 (15分)
    if len(close) >= 20:
        returns = [(close[i] - close[i-1]) / close[i-1] for i in range(-19, 0)]
        vol_20 = max(returns) - min(returns)
        if vol_20 < 0.05:
            score += 8   # 低波动加分
        elif vol_20 > 0.15:
            score -= 5   # 高波动减分
        else:
            score += 3

    return {
        "score": max(0, min(100, score)),
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "rsi": rsi,
    }


def apply_fundamental_boost(tech_score: float, fundamentals: dict) -> float:
    """基本面增强（与 backtest_compare.py 一致）"""
    bonus = 0
    roe = fundamentals.get("roe")
    revenue_yoy = fundamentals.get("revenue_yoy")
    profit_yoy = fundamentals.get("profit_yoy")

    if roe and roe > 15:
        bonus += 10
    elif roe and roe > 10:
        bonus += 5

    if revenue_yoy and revenue_yoy > 20:
        bonus += 10
    elif revenue_yoy and revenue_yoy > 10:
        bonus += 5

    if profit_yoy and profit_yoy > 30:
        bonus += 10
    elif profit_yoy and profit_yoy > 15:
        bonus += 5

    return min(100, tech_score + bonus)


def simulate_backtest_one_year(stocks: list, fundamentals: dict, use_fundamental: bool = False) -> dict:
    """
    一年回测：每日从100只中选TOP 5评分，次日收盘卖出
    """
    import pandas as pd

    logger.info(f"开始回测: {'基本面增强' if use_fundamental else '纯技术评分'}")

    # 加载所有股票的K线
    all_kline = {}
    for code in stocks:
        df = load_kline(code)
        if df is not None and len(df) >= MIN_KLINE:
            all_kline[code] = df

    logger.info(f"有效股票: {len(all_kline)} 只 (≥{MIN_KLINE}行)")

    # 找出所有交易日并排序
    all_dates = set()
    for code, df in all_kline.items():
        dates = df["date"].dt.strftime("%Y%m%d").values
        all_dates.update(dates)
    all_dates = sorted(all_dates)

    if not all_dates:
        return {"error": "无交易数据"}

    logger.info(f"交易日范围: {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)} 天)")

    # ── 回测主循环 ──
    cash = INITIAL_CASH
    position = {}  # code -> {"qty": 0, "avg_cost": 0}
    trades = []
    equity_curve = []
    peak_equity = INITIAL_CASH

    start_date_idx = 0
    # 跳过前20天（需要足够的数据计算指标）
    for i, d in enumerate(all_dates):
        if i >= 20:
            start_date_idx = i
            break

    for i in range(start_date_idx, len(all_dates)):
        today = all_dates[i]

        # ── 收盘卖出昨日持仓 ──
        total_sell = 0
        if position:
            sellable = list(position.keys())
            for code in sellable:
                pos = position[code]
                kline = all_kline.get(code)
                if kline is None:
                    continue
                today_rows = kline[kline["date"] == pd.Timestamp(today)]
                if today_rows.empty:
                    # 非交易日，用最新收盘价估算
                    price = kline["close"].iloc[-1]
                else:
                    price = today_rows["close"].iloc[-1]

                sell_value = pos["qty"] * price
                fee = sell_value * 0.0005  # 万五
                net_sell = sell_value - fee
                cash += net_sell
                total_sell += net_sell

                pnl = (price - pos["avg_cost"]) * pos["qty"] - fee
                trades.append({
                    "date": today,
                    "code": code,
                    "action": "sell",
                    "qty": pos["qty"],
                    "price": price,
                    "pnl": pnl,
                })
            position = {}

        # ── 选股评分 ──
        candidates = []
        for code in stocks:
            kline = all_kline.get(code)
            if kline is None:
                continue
            # 取到今天的数据
            today_rows = kline[kline["date"] <= pd.Timestamp(today)]
            if len(today_rows) < 30:
                continue

            tech = calc_technicals(today_rows)
            if use_fundamental:
                fund = fundamentals.get(code, {})
                final_score = apply_fundamental_boost(tech["score"], fund)
            else:
                final_score = tech["score"]

            candidates.append({
                "code": code,
                "score": final_score,
                "price": today_rows["close"].iloc[-1],
            })

        # 取Top N
        candidates.sort(key=lambda x: -x["score"])
        top = candidates[:TOP_N]

        # ── 等权重买入 ──
        if top:
            per_stock_cash = cash / len(top)
            for c in top:
                price = c["price"]
                if price <= 0:
                    continue
                qty = int(per_stock_cash / price / 100) * 100  # 整百股
                if qty <= 0:
                    qty = int(per_stock_cash / price)
                if qty <= 0:
                    continue

                cost = qty * price
                fee = cost * 0.0005
                total_cost = cost + fee
                if total_cost > cash:
                    continue

                cash -= total_cost
                position[c["code"]] = {"qty": qty, "avg_cost": price}
                trades.append({
                    "date": today,
                    "code": c["code"],
                    "action": "buy",
                    "qty": qty,
                    "price": price,
                    "score": c["score"],
                })

        # ── 权益曲线 ──
        pos_value = 0
        for code, pos in position.items():
            kline = all_kline.get(code)
            if kline is None:
                continue
            today_rows = kline[kline["date"] == pd.Timestamp(today)]
            if today_rows.empty:
                price = kline["close"].iloc[-1]
            else:
                price = today_rows["close"].iloc[-1]
            pos_value += pos["qty"] * price

        equity = cash + pos_value
        equity_curve.append({"date": today, "equity": equity})
        peak_equity = max(peak_equity, equity)

    # ── 最后平仓 ──
    final_pos_value = 0
    for code, pos in position.items():
        kline = all_kline.get(code)
        if kline is None:
            continue
        price = kline["close"].iloc[-1]
        sell_value = pos["qty"] * price
        fee = sell_value * 0.0005
        cash += sell_value - fee
        final_pos_value += sell_value

    final_equity = cash

    # ── 计算回测指标 ──
    total_return_pct = (final_equity - INITIAL_CASH) / INITIAL_CASH * 100
    max_dd = 0
    peak = INITIAL_CASH
    for e in equity_curve:
        peak = max(peak, e["equity"])
        dd = (e["equity"] - peak) / peak * 100
        max_dd = min(max_dd, dd)

    # 胜率
    win_trades = [t for t in trades if t.get("pnl", 0) > 0]
    loss_trades = [t for t in trades if t.get("pnl", 0) < 0]
    total_trades = len(trades)
    win_rate = len(win_trades) / total_trades * 100 if total_trades > 0 else 0

    # 夏普 (年化)
    import numpy as np
    daily_returns = []
    for i in range(1, len(equity_curve)):
        r = (equity_curve[i]["equity"] - equity_curve[i - 1]["equity"]) / equity_curve[i - 1]["equity"]
        daily_returns.append(r)
    if daily_returns:
        avg_ret = np.mean(daily_returns)
        std_ret = np.std(daily_returns)
        sharpe = (avg_ret / std_ret * math.sqrt(244)) if std_ret > 0 else 0
    else:
        sharpe = 0

    result = {
        "strategy": "基本面增强" if use_fundamental else "纯技术评分",
        "stock_universe": len(all_kline),
        "trading_days": len(equity_curve),
        "total_trades": total_trades,
        "total_return_pct": round(total_return_pct, 2),
        "final_value": round(final_equity, 2),
        "win_rate_pct": round(win_rate, 2),
        "max_drawdown_pct": round(abs(max_dd), 2),
        "sharpe_ratio": round(sharpe, 2),
        "wins": len(win_trades),
        "losses": len(loss_trades),
    }
    return result


def main():
    logger.info("=" * 60)
    logger.info("100只股票 × 1年回测")
    logger.info("=" * 60)

    t0 = time.time()

    # 加载数据
    stocks = load_stock_list()
    fundamentals = load_fundamentals()
    logger.info(f"股票池: {len(stocks)} 只")
    logger.info(f"基本面缓存: {len(fundamentals)} 只")

    # 纯技术
    logger.info("\n" + "-" * 50)
    r1 = simulate_backtest_one_year(stocks, fundamentals, use_fundamental=False)

    # 基本面增强
    logger.info("\n" + "-" * 50)
    r2 = simulate_backtest_one_year(stocks, fundamentals, use_fundamental=True)

    # 输出
    results = [r1, r2]
    print("\n" + "=" * 60)
    print(f"{'策略':<20} {'收益%':>8} {'最终价值':>12} {'胜率%':>8} {'最大回撤%':>10} {'夏普':>8} {'交易次':>8}")
    print("=" * 60)
    for r in results:
        if "error" in r:
            print(f"{r['strategy']:<20} ❌ {r['error']}")
        else:
            print(f"{r['strategy']:<20} {r['total_return_pct']:>+8.2f} "
                  f"{r['final_value']:>12.2f} {r['win_rate_pct']:>8.2f} "
                  f"{r['max_drawdown_pct']:>10.2f} {r['sharpe_ratio']:>8.2f} "
                  f"{r['total_trades']:>8}")
    print("=" * 60)

    # 保存
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUTPUT_DIR / f"backtest_100_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(out_file, "w") as f:
        json.dump({"run_date": datetime.now().isoformat(), "stock_count": len(stocks),
                    "strategies": results}, f, ensure_ascii=False, indent=2)
    logger.info(f"\n结果保存: {out_file}")

    elapsed = time.time() - t0
    logger.info(f"总耗时: {elapsed:.1f}s")


if __name__ == "__main__":
    # Need pandas
    import pandas as pd
    import os as os
    main()
