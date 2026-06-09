#!/usr/bin/env python3
"""
全A股回测 — 2,328只股票 × 全部历史数据
========================================
不偷懒，用 SQLite 里的全部 K 线跑一遍。

策略:
  1. 波动率择时（中证全指 20日HV百分位） → 仓位 25%~100%
  2. 技术评分（screen_score: MA/RSI/量比等）- 全向量化
  3. 每日选前5只建仓
  4. 风控：硬止损-15%、时间止损20天、跟踪止损、多级止盈

用法:
  python3 -u scripts/backtest_full.py
"""

import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = "/opt/daily_stock_analysis/data/stock_analysis.db"
INITIAL_CASH = 100_000.0
TOP_N = 5
MAX_POSITIONS = 5
COMMISSION_RATE = 0.00025
STAMP_TAX = 0.0005
SLIPPAGE = 0.001
STOP_LOSS = -0.15
TIME_STOP_DAYS = 20
TIME_STOP_DRAWDOWN = -0.08
TRAILING_ACTIVATE = 0.03
TRAILING_STOP = -0.04


def load_all_kline(db_path: str) -> dict:
    """从 SQLite 加载全部 K 线，返回 {code: DataFrame}"""
    import sqlite3

    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "SELECT stock_code, date, open, close, high, low, volume, amount " "FROM kline ORDER BY stock_code, date"
    )
    rows = cur.fetchall()
    conn.close()
    print(f"  📥 SQLite 读取: {len(rows)} 行", flush=True)

    stocks = defaultdict(list)
    for code, d, o, c, h, l, v, a in rows:
        stocks[code].append((d, o, c, h, l, v, a))

    result = {}
    for code, pts in stocks.items():
        arr = np.array(
            pts,
            dtype=[
                ("date", "datetime64[D]"),
                ("open", "f8"),
                ("close", "f8"),
                ("high", "f8"),
                ("low", "f8"),
                ("volume", "f8"),
                ("amount", "f8"),
            ],
        )
        result[code] = arr
    print(f"  📊 {len(result)} 只股票加载完成", flush=True)
    return result


def vectorized_indicators(arr: np.ndarray, invert: bool = False) -> np.ndarray:
    """全向量化预计算：MA5/10/20, RSI, vol_ratio, 评分"""
    n = len(arr)
    close = arr["close"]
    volume = arr["volume"]

    out = np.zeros(
        n,
        dtype=[
            ("score", "f4"),
            ("close", "f8"),
            ("high", "f8"),
            ("low", "f8"),
            ("vol_MA20", "f8"),
            ("date", "datetime64[D]"),
        ],
    )
    out["close"] = close
    out["high"] = arr["high"]
    out["low"] = arr["low"]
    out["date"] = arr["date"]

    if n < 20:
        return out

    # MA
    ma5 = _rolling_mean(close, 5)
    ma10 = _rolling_mean(close, 10)
    ma20 = _rolling_mean(close, 20)
    vol_ma20 = _rolling_mean(volume, 20)
    out["vol_MA20"] = vol_ma20
    vol_ratio = volume / np.maximum(vol_ma20, 1e-6)
    price_ma20_pct = (close / np.maximum(ma20, 1e-6) - 1) * 100

    # RSI(14)
    delta = np.diff(close, prepend=close[0])
    gains = np.maximum(delta, 0)
    losses = np.maximum(-delta, 0)
    avg_gain = _rolling_mean(gains, 14)
    avg_loss = _rolling_mean(losses, 14)
    rsi = 100 - 100 / (1 + avg_gain / np.maximum(avg_loss, 1e-10))

    # ── score 评分（全向量化）──
    mul = -1 if invert else 1  # 反转评分：正变负
    score = np.full(n, 50.0)

    # 趋势 MA5 > MA10 > MA20
    trend = (ma5 > ma10) & (ma10 > ma20) & (ma5 > 0)
    score += trend * (20 * mul)

    # 偏多 MA5 > MA20 && MA5 > MA10*0.99
    near_trend = (~trend) & (ma5 > ma20) & (ma5 > ma10 * 0.99) & (ma5 > 0)
    score += near_trend * (15 * mul)

    # 放量突破
    breakout = (vol_ratio > 1.5) & (price_ma20_pct > 0.5) & (ma20 > 0)
    score += breakout * (15 * mul)

    # 金叉
    golden = (ma5 > ma10) & (ma5 > 0)
    score += golden * (10 * mul)

    # RSI
    score += ((rsi < 30) & (rsi > 0)) * (8 * mul)
    score -= ((rsi > 75) & (rsi <= 100)) * (10 * mul)

    # 价格位置
    score += ((price_ma20_pct > -3) & (price_ma20_pct < 3)) * (5 * mul)
    score -= (price_ma20_pct > 15) * (5 * mul)

    # 量比
    score += ((vol_ratio > 1.2) & (vol_ratio < 3)) * (5 * mul)
    score -= (vol_ratio > 5) * (3 * mul)

    # 前20天评分=0（数据不够）
    score[:19] = 0
    score = np.clip(score, 0, 100).astype("f4")
    out["score"] = score
    return out


def _rolling_mean(x: np.ndarray, window: int) -> np.ndarray:
    """快速滚动平均（边界填充 NaN）"""
    ret = np.full_like(x, np.nan)
    cum = np.cumsum(np.insert(x, 0, 0))
    ret[window - 1 :] = (cum[window:] - cum[:-window]) / window
    return ret


def load_index_pkl() -> pd.DataFrame:
    """从PKL加载中证全指并计算波动率择时"""
    import pickle as pkl

    with open("/opt/daily_stock_analysis/data/cache/market_index/000985.pkl", "rb") as f:
        zz = pkl.load(f)
    zz["date"] = pd.to_datetime(zz["date"])
    zz = zz.sort_values("date").reset_index(drop=True)
    zz["ret"] = zz["close"].pct_change()
    zz["hv_20"] = zz["ret"].rolling(20).std() * np.sqrt(252)

    hv = zz["hv_20"].values
    pct_arr = np.full(len(hv), 50.0)
    for i in range(120, len(hv)):
        w = hv[i - 119 : i + 1]
        lo, hi = np.nanmin(w), np.nanmax(w)
        pct_arr[i] = (hv[i] - lo) / max(hi - lo, 1e-10) * 100
    zz["hv_percentile"] = pct_arr

    def pos(p):
        if np.isnan(p):
            return 0.75
        if p < 30:
            return 1.0
        if p < 60:
            return 0.75
        if p < 80:
            return 0.50
        return 0.25

    zz["position_pct"] = [pos(p) for p in pct_arr]
    zz["date_str"] = zz["date"].dt.strftime("%Y-%m-%d")
    return zz


def date_range(stocks: dict) -> list:
    """获取所有交易日的并集"""
    all_dates = set()
    for arr in stocks.values():
        ds = arr["date"].astype("datetime64[D]")
        all_dates.update(ds.astype(str))
    return sorted(all_dates)


# ══════════════════════════════════════════════════════════════════════
# 回测主逻辑
# ══════════════════════════════════════════════════════════════════════


def run_backtest(stocks: dict, vol_df: pd.DataFrame, all_dates: list, invert: bool = False):
    """逐日回测"""
    print(f"\n{'='*70}", flush=True)
    print(f"🚀 开始回测: {len(all_dates)} 个交易日, 初始资金 ¥{INITIAL_CASH:,.0f}", flush=True)
    print(f"{'='*70}", flush=True)

    # 预计算所有股票指标
    print("\n[Pre] 预计算技术指标（全向量化）...", flush=True)
    t0 = time.time()
    valid_stocks = {}
    codes = list(stocks.keys())
    for i, code in enumerate(codes):
        arr = vectorized_indicators(stocks[code], invert=invert)
        # 过滤：至少20天有评分
        mask = arr["score"] > 0
        if mask.sum() >= 20:
            valid_stocks[code] = arr[arr["score"] > 0].copy()
        if (i + 1) % 500 == 0:
            print(f"  [{i+1}/{len(codes)}]", end=" ", flush=True)
    stocks = valid_stocks
    print(f"\n  ✅ 达标股票: {len(stocks)} 只, 耗时 {time.time()-t0:.1f}s", flush=True)

    # 构建日期索引：date_str → {code: (score, close)}
    print("[Pre] 构建日期索引...", flush=True)
    t0 = time.time()
    date_index = defaultdict(dict)
    for code, arr in stocks.items():
        dates = arr["date"].astype(str)
        scores = arr["score"]
        closes = arr["close"]
        highs = arr["high"]
        lows = arr["low"]
        vol_ma20 = arr["vol_MA20"]
        for j in range(len(dates)):
            d = dates[j]
            date_index[d][code] = {
                "score": scores[j],
                "close": closes[j],
                "high": highs[j],
                "low": lows[j],
                "vol_ma20": vol_ma20[j],
            }
    print(f"  ✅ {len(date_index)} 个交易日, 耗时 {time.time()-t0:.1f}s", flush=True)

    # 波动率索引
    vol_lookup = dict(zip(vol_df["date_str"], vol_df["position_pct"]))

    # ── 回测变量 ──
    cash = INITIAL_CASH
    positions = {}  # code -> dict
    total_fees = 0.0
    total_trades = 0
    wins = wins_pct = 0
    losses = 0
    equity_curve = []
    peak_equity = INITIAL_CASH

    report_every = max(1, len(all_dates) // 20)
    t_start = time.time()

    for day_idx, date_str in enumerate(all_dates):
        # 1. 波动率仓位
        position_pct = vol_lookup.get(date_str, 0.75)

        todays_stocks = date_index.get(date_str, {})

        # 2. 卖出
        for code in list(positions.keys()):
            pos = positions[code]
            info = todays_stocks.get(code)
            if info is None:
                continue

            cur_price = info["close"]
            low_today = info["low"]
            cost = pos["avg_cost"]
            qty = pos["qty"]

            unrealized = (cur_price - cost) / cost
            unrealized_low = (low_today - cost) / cost  # 用最低价

            # 峰值更新（用最高价跟踪）
            peak = pos.get("peak_price", cost)
            high_today = info.get("high", cur_price)
            pos["peak_price"] = max(peak, high_today, cur_price)
            peak_dd = (low_today - pos["peak_price"]) / pos["peak_price"]  # 用最低价

            do_sell = False
            reason = ""

            # 硬止损 -15%（用最低价判断）
            if unrealized_low <= STOP_LOSS:
                do_sell, reason = True, "硬止损-15%"
            # 时间止损（用收盘价合理）
            elif (
                pd.Timestamp(str(date_str)) - pd.Timestamp(str(pos["entry_date"]))
            ).days >= TIME_STOP_DAYS and unrealized <= TIME_STOP_DRAWDOWN:
                do_sell, reason = True, "时间止损"
            # 跟踪止损（用最低价）
            elif peak_dd <= TRAILING_STOP and pos["peak_price"] > cost * (1 + TRAILING_ACTIVATE):
                do_sell, reason = True, "跟踪止损"
            # 止盈+25%
            elif unrealized >= 0.25:
                do_sell, reason = True, "止盈+25%"
            # 半仓止盈+10%
            elif unrealized >= 0.10 and pos.get("tp_level", 0) < 1:
                half_qty = (qty // 200) * 100  # 按手取整
                if half_qty < 100:
                    half_qty = 0
                if half_qty > 0:
                    proceeds = half_qty * cur_price * (1 - SLIPPAGE)
                    fee = proceeds * (COMMISSION_RATE + STAMP_TAX)
                    fee = max(fee, 5.0)  # 最低佣金¥5
                    rpnl = proceeds - fee - cost * half_qty
                    cash += proceeds - fee
                    total_fees += fee
                    total_trades += 1
                    if rpnl > 0:
                        wins += 1
                    else:
                        losses += 1
                    pos["qty"] -= half_qty
                    pos["tp_level"] = 1

            if do_sell and pos["qty"] > 0:
                qty_sell = pos["qty"]
                # 清仓用收盘价执行（止损已用最低价判断）
                exec_sell_price = (
                    min(cur_price, max(low_today, cost * (1 + STOP_LOSS))) if reason == "硬止损-15%" else cur_price
                )
                proceeds = qty_sell * exec_sell_price * (1 - SLIPPAGE)
                fee = proceeds * (COMMISSION_RATE + STAMP_TAX)
                fee = max(fee, 5.0)  # 最低佣金¥5
                rpnl = proceeds - fee - cost * qty_sell
                cash += proceeds - fee
                total_fees += fee
                total_trades += 1
                if rpnl > 0:
                    wins += 1
                else:
                    losses += 1
                del positions[code]

        # 3. 买入
        current_count = len(positions)
        can_buy = MAX_POSITIONS - current_count

        if can_buy > 0 and position_pct > 0 and date_str in date_index:
            avail_cash = cash * position_pct
            candidates = [(code, info) for code, info in todays_stocks.items() if code not in positions]
            candidates.sort(key=lambda x: -x[1]["score"])
            candidates = candidates[:TOP_N]

            per_cash = avail_cash / max(1, can_buy)

            for code, info in candidates[:can_buy]:
                price = info["close"]
                vol20 = info["vol_ma20"]
                if price <= 0:
                    continue
                if np.isnan(vol20) or vol20 < 1:
                    continue

                # 可用资金按有效买入算（最低佣金影响不大，近似）
                qty = int(per_cash / (price * (1 + SLIPPAGE)))
                qty = (qty // 100) * 100  # 按手取整
                if qty == 0:
                    # fallback: 用剩余现金
                    qty = max(0, int(cash / (price * (1 + SLIPPAGE + COMMISSION_RATE))))
                    qty = (qty // 100) * 100
                if qty < 100:
                    continue

                cost_buy = qty * price * (1 + SLIPPAGE)
                fee = max(cost_buy * COMMISSION_RATE, 5.0)  # 最低佣金¥5
                total_cost = cost_buy + fee

                if total_cost > cash:
                    qty = max(0, int(cash / (price * (1 + SLIPPAGE + COMMISSION_RATE))))
                    qty = (qty // 100) * 100
                    if qty < 100:
                        continue
                    cost_buy = qty * price * (1 + SLIPPAGE)
                    fee = max(cost_buy * COMMISSION_RATE, 5.0)
                    total_cost = cost_buy + fee

                cash -= total_cost
                total_fees += fee
                total_trades += 1
                positions[code] = {
                    "qty": qty,
                    "avg_cost": price * (1 + SLIPPAGE),
                    "entry_date": date_str,
                    "peak_price": price,
                    "tp_level": 0,
                }

        # 4. 当日权益
        market_value = 0
        for code, pos in positions.items():
            info = todays_stocks.get(code)
            market_value += (info["close"] if info else pos["avg_cost"]) * pos["qty"]

        equity = cash + market_value
        peak_equity = max(peak_equity, equity)
        dd_pct = (equity - peak_equity) / peak_equity * 100

        equity_curve.append(
            {
                "date": date_str,
                "equity": round(equity, 2),
                "cash": round(cash, 2),
                "market_value": round(market_value, 2),
                "positions": len(positions),
                "position_pct": float(position_pct),
                "dd_pct": round(dd_pct, 2),
            }
        )

        if day_idx % report_every == 0 or day_idx == len(all_dates) - 1:
            ret = (equity / INITIAL_CASH - 1) * 100
            pct_done = (day_idx + 1) / len(all_dates) * 100
            ts = time.time() - t_start
            print(
                f"  [{pct_done:5.1f}%] {date_str}  权益={equity:>8,.0f}  ({ret:+.2f}%)  "
                f"持仓={len(positions)} 现金={cash:>8,.0f}  |  {ts:.0f}s",
                flush=True,
            )

    elapsed = time.time() - t_start
    print(f"\n⏱  回测耗时: {elapsed:.1f}s", flush=True)
    return equity_curve, wins, losses, total_trades, total_fees, elapsed


# ══════════════════════════════════════════════════════════════════════
# 绩效计算
# ══════════════════════════════════════════════════════════════════════


def compute_metrics(ec, wins, losses, total_trades, total_fees, elapsed):
    import pickle as pkl

    df = pd.DataFrame(ec)
    df["date"] = pd.to_datetime(df["date"])
    df["daily_ret"] = df["equity"].pct_change()
    df["ret_pct"] = (df["equity"] / INITIAL_CASH - 1) * 100

    final = df.iloc[-1]
    total_return = final["ret_pct"]
    n_days = len(df)
    n_years = n_days / 252 if n_days else 1

    ann_return = ((1 + total_return / 100) ** (1 / n_years) - 1) * 100
    ann_vol = df["daily_ret"].std() * np.sqrt(252) * 100
    rf = 2.0
    sharpe = (ann_return - rf) / ann_vol if ann_vol > 0 else 0
    max_dd = df["dd_pct"].min()
    calmar = abs(ann_return / max_dd) if max_dd != 0 else 0
    win_rate = wins / total_trades * 100 if total_trades else 0

    # 沪深300 buy & hold
    try:
        with open("/opt/daily_stock_analysis/data/cache/hs300_index.pkl", "rb") as f:
            hs300 = pkl.load(f)
        hs300["date"] = pd.to_datetime(hs300["date"])
        hs300 = hs300[(hs300["date"] >= df["date"].min()) & (hs300["date"] <= df["date"].max())]
        if len(hs300) > 0:
            hs300_ret = (hs300["close"].iloc[-1] / hs300["close"].iloc[0] - 1) * 100
        else:
            hs300_ret = None
    except:
        hs300_ret = None

    # ═══ 打印报告 ═══
    sep = "=" * 72
    print(f"\n{sep}", flush=True)
    print(f"📊 全A回测绩效报告 (2328只全部参与)", flush=True)
    print(f"{sep}", flush=True)

    print(
        f"\n  回测期间:     {df['date'].min().strftime('%Y-%m-%d')} ~ {df['date'].max().strftime('%Y-%m-%d')}",
        flush=True,
    )
    print(f"  交易日数:     {n_days}  ({n_years:.1f}年)", flush=True)
    print(f"  标的:         2,328 只 A 股", flush=True)
    print(f"  策略:         波动率择时 + 技术评分TOP5 + 多风控", flush=True)

    print(f"\n  {'='*60}", flush=True)
    print(f"  📈 累计收益:     {total_return:>+8.2f}%", flush=True)
    if hs300_ret is not None:
        print(f"  📊 沪深300:      {hs300_ret:>+8.2f}%", flush=True)
        print(f"  🎯 超额收益:     {total_return - hs300_ret:>+8.2f}%", flush=True)
    print(f"  📉 最大回撤:     {max_dd:>8.2f}%", flush=True)
    print(f"  {'='*60}", flush=True)

    print(f"\n  📅 年化收益:     {ann_return:>+8.2f}%", flush=True)
    print(f"  🌊 年化波动:     {ann_vol:>8.2f}%", flush=True)
    print(f"  ⚡ 夏普比率:     {sharpe:>8.2f}  (无风险2%)", flush=True)
    print(f"  🛡️  卡玛比率:     {calmar:>8.2f}", flush=True)
    print(f"  {'='*60}", flush=True)

    print(f"\n  💰 总交易:       {total_trades}", flush=True)
    print(f"  ✅ 盈利交易:     {wins}", flush=True)
    print(f"  ❌ 亏损交易:     {losses}", flush=True)
    print(f"  🎯 胜率:         {win_rate:.1f}%", flush=True)
    print(f"  💸 总费用:       ¥{total_fees:.2f}", flush=True)
    print(f"  🏛️  年换手率:     {total_trades / max(n_years, 0.1) / 2:.0f} 次(双边)", flush=True)
    print(f"  {'='*60}", flush=True)

    print(f"\n  🏆 最终权益:     ¥{final['equity']:>10,.2f}", flush=True)
    print(f"  🥇 峰值权益:     ¥{df['equity'].max():>10,.2f}", flush=True)
    print(f"  {'='*60}\n", flush=True)

    # 月度统计
    df["month"] = df["date"].dt.to_period("M")
    monthly = df.groupby("month")["equity"].agg(["first", "last"])
    monthly["ret"] = (monthly["last"] / monthly["first"] - 1) * 100
    mwin = (monthly["ret"] > 0).sum()
    print(f"  📆 月度胜率: {mwin}/{len(monthly)} ({mwin/len(monthly)*100:.0f}%)", flush=True)

    # 年度收益
    df["year"] = df["date"].dt.year
    yearly = df.groupby("year")["equity"].agg(["first", "last"])
    yearly["ret"] = (yearly["last"] / yearly["first"] - 1) * 100
    for yr, row in yearly.iterrows():
        print(f"    {yr}: {row['ret']:+.2f}%", flush=True)

    print(f"\n  ⏱  计算耗时: {elapsed:.0f}s\n", flush=True)

    # 保存
    report = {
        "period": {"start": str(df["date"].min().date()), "end": str(df["date"].max().date())},
        "total_return_pct": round(total_return, 2),
        "annual_return_pct": round(ann_return, 2),
        "annual_vol_pct": round(ann_vol, 2),
        "sharpe_ratio": round(sharpe, 2),
        "calmar_ratio": round(calmar, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "final_equity": round(final["equity"], 2),
        "total_trades": total_trades,
        "win_rate_pct": round(win_rate, 1),
        "total_fees": round(total_fees, 2),
        "hs300_return_pct": round(hs300_ret, 2) if hs300_ret is not None else None,
        "equity_curve": ec,
    }
    out = "/opt/daily_stock_analysis/reports/full_backtest_report.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"  📝 报告已保存: {out}", flush=True)
    return report


# ══════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import warnings

    warnings.filterwarnings("ignore")

    invert_mode = "--invert" in sys.argv or "-i" in sys.argv
    label = "🔁 反转评分" if invert_mode else "📊 原版评分"

    print(f"{'='*72}", flush=True)
    print(f"📊 全A股回测 — {label}", flush=True)
    print(f"{'='*72}", flush=True)

    t0 = time.time()

    # [1] 加载K线
    print(f"\n[1/4] 加载 K 线数据...", flush=True)
    stocks = load_all_kline(DB_PATH)

    # [2] 加载指数
    print(f"\n[2/4] 加载指数（中证全指 000985）...", flush=True)
    vol_df = load_index_pkl()
    print(f"  {len(vol_df)} 行, {vol_df['date'].min().date()} ~ {vol_df['date'].max().date()}", flush=True)

    # [3] 回测
    all_dates = date_range(stocks)
    print(f"\n[3/4] 运行回测 ({len(all_dates)} 个交易日)...", flush=True)
    ec, wins, losses, trades, fees, elapsed = run_backtest(stocks, vol_df, all_dates, invert=invert_mode)

    # [4] 绩效
    print(f"\n[4/4] 计算绩效指标...", flush=True)
    report = compute_metrics(ec, wins, losses, trades, fees, elapsed)

    total_t = time.time() - t0
    print(f"✅ 全部完成! 总耗时 {total_t:.0f}s", flush=True)
