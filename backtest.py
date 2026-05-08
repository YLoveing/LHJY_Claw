#!/usr/bin/env python3
"""
回测引擎 — 2025-05-08 ~ 2026-05-08
无未来函数，逐日滚动，完整重现当前策略

策略逻辑:
  每日技术评分 → GARCH动态阈值 → 凯利仓位 → 风控(止损/止盈/回撤)
  每周五 → 马科维茨再平衡
"""

import json
import logging
import sys
import warnings
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import akshare as ak
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backtest")

# ── 策略参数（与模拟交易一致） ──
INITIAL_CAPITAL = 1_000_000
MAX_POSITIONS = 10
STOP_LOSS = -0.15
TAKE_PROFIT = 0.25
DRAWDOWN_LIMIT = -0.20
KELLY_B = 0.25 / 0.15  # 1.67
DIVERSIFICATION = 0.20  # 单只≤20%总资金
MIN_TRADE = 10_000
BASE_BUY = 70
BASE_SELL = 30

# ── 股票池 ──
STOCKS = {
    "510300": "沪深300ETF华泰柏瑞",
    "002410": "广联达",
}


def fetch_data() -> Dict[str, pd.DataFrame]:
    """获取一年的日线数据。"""
    data = {}
    for code, name in STOCKS.items():
        if code == "510300":
            df = ak.fund_etf_hist_em(symbol=code, period="daily",
                                      start_date="20250508", end_date="20260508", adjust="qfq")
        else:
            df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                     start_date="20250508", end_date="20260508", adjust="qfq")
        df["日期"] = pd.to_datetime(df["日期"])
        df = df.sort_values("日期").reset_index(drop=True)
        data[code] = df
        log.info(f"  {name}({code}): {len(df)} 天, {df['收盘'].iloc[0]:.2f} → {df['收盘'].iloc[-1]:.2f}")
    return data


def technical_score(row: pd.Series) -> int:
    """
    基于技术指标生成评分 (0-100)，模拟 LLM 打分逻辑。
    只使用该行可见的数据（不含未来）。

    因子分解:
      MA趋势: +30 (多头) / -30 (空头) / 0 (震荡)
      RSI:    +15 (超卖) / -15 (超买)
      MACD:   +10 (正值) / -10 (负值)
      量比:   ±5 (放量/缩量)
      价格位置: +10 (站上MA20) / -10 (跌破MA20)
    """
    score = 50  # 基准

    # 1. MA 趋势 (30分)
    if row.get("MA5", 0) > row.get("MA10", 0) > row.get("MA20", 0):
        score += 25  # 多头排列
    elif row.get("MA5", 0) < row.get("MA10", 0) < row.get("MA20", 0):
        score -= 25  # 空头排列
    elif row.get("MA5", 0) > row.get("MA20", 0) and abs(row.get("MA5", 0) - row.get("MA10", 0)) < 0.01:
        score += 10  # 偏多震荡
    elif row.get("MA5", 0) < row.get("MA20", 0) and abs(row.get("MA5", 0) - row.get("MA10", 0)) < 0.01:
        score -= 10  # 偏空震荡

    # 2. RSI (15分)
    rsi = row.get("RSI", 50)
    if rsi < 25:
        score += 15  # 超卖→看多
    elif rsi < 35:
        score += 8
    elif rsi > 75:
        score -= 15  # 超买→看空
    elif rsi > 65:
        score -= 8

    # 3. MACD (10分)
    macd = row.get("MACD", 0)
    if macd > 0:
        score += 8
    elif macd < -0.5:
        score -= 8
    elif macd < 0:
        score -= 3

    # 4. 量比 (5分)
    vol_ratio = row.get("VOL_RATIO", 1.0)
    if vol_ratio > 1.5 and score > 50:
        score += 5  # 上涨放量加分
    elif vol_ratio < 0.5 and score < 50:
        score -= 5  # 下跌缩量加分

    # 5. 价格相对位置 (10分)
    if row.get("MA20", 0) > 0:
        pct_above = (row["收盘"] - row["MA20"]) / row["MA20"]
        if pct_above > 0.05:
            score += 8
        elif pct_above > 0.02:
            score += 3
        elif pct_above < -0.05:
            score -= 8
        elif pct_above < -0.02:
            score -= 3

    # 6. 涨跌幅影响 (日内已经包含在MA中，额外微调)
    chg = row.get("涨跌幅", 0)
    if chg > 2.0 and score < 40:
        score += 5  # 大涨但没有趋势信号 → 补一些
    elif chg < -2.0 and score > 60:
        score -= 5

    return int(np.clip(round(score), 5, 95))


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """计算全体技术指标（不会引入未来信息，因为全是滚动计算）。"""
    d = df.copy()

    # MA
    d["MA5"] = d["收盘"].rolling(5).mean()
    d["MA10"] = d["收盘"].rolling(10).mean()
    d["MA20"] = d["收盘"].rolling(20).mean()

    # RSI (14日)
    delta = d["收盘"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    d["RSI"] = 100 - 100 / (1 + rs)

    # MACD
    ema12 = d["收盘"].ewm(span=12).mean()
    ema26 = d["收盘"].ewm(span=26).mean()
    d["MACD"] = ema12 - ema26
    d["MACD_SIGNAL"] = d["MACD"].ewm(span=9).mean()
    d["MACD_HIST"] = d["MACD"] - d["MACD_SIGNAL"]

    # 量比: 当日成交量 / 20日均量
    d["VOL_MA20"] = d["成交量"].rolling(20).mean()
    d["VOL_RATIO"] = d["成交量"] / d["VOL_MA20"].replace(0, np.nan)

    # 日收益率 (用于GARCH)
    d["RETURN"] = d["收盘"].pct_change()

    return d


def garch_thresholds(returns: np.ndarray) -> Tuple[int, int]:
    """
    简化 GARCH: 滚动20日波动率百分位 → 动态阈值。
    用滚动标准差代替全GARCH MLE以加速回测。
    """
    if len(returns) < 2:
        return (BASE_BUY, BASE_SELL)

    # 滚动波动率
    vol = np.std(returns[-20:]) * np.sqrt(252) if len(returns) >= 20 else \
          np.std(returns) * np.sqrt(252)

    # 用无条件的长期波动率做百分位估计
    vol_series = []
    for i in range(5, len(returns)):
        v = np.std(returns[max(0, i-20):i]) * np.sqrt(252)
        vol_series.append(v)

    if len(vol_series) < 2:
        if vol < 0.10:
            percentile = 10.0
        elif vol > 0.30:
            percentile = 90.0
        else:
            percentile = 50.0
    else:
        percentile = float(np.sum(np.array(vol_series) <= vol) / len(vol_series) * 100)
        percentile = np.clip(percentile, 0, 100)

    # 与 garch.py 的 get_dynamic_thresholds 逻辑一致
    if percentile >= 90:
        return (BASE_BUY + 10, BASE_SELL)
    if percentile >= 75:
        return (BASE_BUY + 5, BASE_SELL)
    if percentile >= 60:
        return (BASE_BUY + 2, BASE_SELL)
    if percentile <= 10:
        return (BASE_BUY - 8, BASE_SELL + 8)
    if percentile <= 25:
        return (BASE_BUY - 3, BASE_SELL + 3)
    return (BASE_BUY, BASE_SELL)


def kelly_size(score: int, cash: float) -> float:
    """半凯利仓位计算。"""
    if score >= 100: p = 0.90
    elif score >= 90: p = 0.85
    elif score >= 80: p = 0.78
    elif score >= 70: p = 0.70
    else: p = 0.50
    q = 1 - p
    f = max(0.0, (KELLY_B * p - q) / KELLY_B) * 0.5 * DIVERSIFICATION
    amount = cash * f
    return max(MIN_TRADE, min(amount, cash * 0.25, 200_000))


def markowitz_weights(
    price_df: pd.DataFrame,
    codes: List[str],
    lookback: int = 60,
) -> Dict[str, float]:
    """简化马科维茨: 用期限波动率做等风险分配。"""
    weights = {}
    last_prices = {}
    for c in codes:
        ser = price_df[c].dropna()
        if len(ser) >= 20:
            ret = ser.pct_change().dropna().values[-lookback:]
            vol = float(np.std(ret)) * np.sqrt(252)
            last_prices[c] = float(ser.iloc[-1])
            weights[c] = 1.0 / max(vol, 0.05)
        else:
            weights[c] = 1.0

    total = sum(weights.values())
    if total > 0:
        return {k: v / total for k, v in weights.items()}
    return {c: 1.0 / len(codes) for c in codes}


def run_backtest() -> Dict[str, Any]:
    """执行全额回测。"""
    log.info("=" * 60)
    log.info("📊 回测启动: 2025-05-08 ~ 2026-05-08")
    log.info("=" * 60)

    # 1. 获取数据
    log.info("\n[1/5] 获取历史数据...")
    raw = fetch_data()
    indicators = {c: compute_indicators(df) for c, df in raw.items()}

    # 2. 对齐日期
    all_dates = sorted(set.intersection(
        *[set(df["日期"].dt.strftime("%Y%m%d")) for df in indicators.values()]
    ))
    log.info(f"  对齐日期: {len(all_dates)} 个交易日")

    # ── 状态初始化 ──
    cash = INITIAL_CAPITAL
    positions: Dict[str, Dict[str, Any]] = OrderedDict()
    trades: List[Dict[str, Any]] = []
    daily_equity: List[Dict[str, Any]] = []
    peak_equity = INITIAL_CAPITAL
    max_drawdown = 0.0
    total_fee = 0.0

    # 价格历史（逐日追加，用于滚动计算）
    price_history: Dict[str, pd.Series] = {c: pd.Series(dtype=float) for c in STOCKS}
    return_history: Dict[str, List[float]] = {c: [] for c in STOCKS}

    warmup = 20  # 前20天只积累指标不交易

    log.info(f"\n[2/5] 逐日模拟交易 ({len(all_dates)} 天)...")
    for day_idx, date_str in enumerate(all_dates):
        dt = datetime.strptime(date_str, "%Y%m%d")
        is_friday = dt.weekday() == 4
        is_warmup = day_idx < warmup

        # 取当日行情
        day_prices = {}
        day_scores = {}
        for code in STOCKS:
            row = indicators[code][indicators[code]["日期"].dt.strftime("%Y%m%d") == date_str]
            if row.empty:
                continue
            row = row.iloc[0]
            price = float(row["收盘"])
            day_prices[code] = price
            price_history[code] = pd.concat([price_history[code], pd.Series({date_str: price})])

            # 评分（第20天开始有足够的技术指标）
            if not is_warmup:
                score = technical_score(row)
                day_scores[code] = score

            # 日收益率（用于GARCH）
            ret = row.get("RETURN", np.nan)
            if not np.isnan(ret):
                return_history[code].append(ret)

        if is_warmup:
            # 记录权益但不交易
            total_value = cash
            daily_equity.append({
                "date": date_str, "equity": total_value,
                "cash": cash, "market_value": 0,
                "positions": 0, "return_pct": 0.0,
            })
            continue

        # ── 计算阈值（用聚合收益率） ──
        all_rets = []
        for c in STOCKS:
            all_rets.extend(return_history[c][-40:])
        if all_rets:
            buy_th, sell_th = garch_thresholds(np.array(all_rets))
        else:
            buy_th, sell_th = BASE_BUY, BASE_SELL

        # ── 当前市值 ──
        current_value = 0
        for code, pos in list(positions.items()):
            price = day_prices.get(code, pos["price"])
            current_value += pos["quantity"] * price

        total_equity = cash + current_value
        peak_equity = max(peak_equity, total_equity)
        dd = (total_equity - peak_equity) / peak_equity
        max_drawdown = min(max_drawdown, dd)
        is_drawdown_blocked = dd < DRAWDOWN_LIMIT

        # ── ① 止损/止盈检查 ──
        for code in list(positions.keys()):
            pos = positions[code]
            price = day_prices.get(code, pos["price"])
            pnl_pct = (price - pos["avg_cost"]) / pos["avg_cost"]

            action = None
            if pnl_pct <= STOP_LOSS:
                action = "stop_loss"
            elif pnl_pct >= TAKE_PROFIT:
                action = "take_profit"

            if action:
                qty = pos["quantity"]
                # 止盈只卖一半
                if action == "take_profit":
                    qty = int(qty / 2 / 100) * 100
                    qty = max(qty, 100)
                if qty <= 0:
                    qty = pos["quantity"]
                # 不能超过实际持仓
                qty = min(qty, pos["quantity"])

                proceeds = qty * price
                fee = proceeds * 0.0003
                pnl = (price - pos["avg_cost"]) * qty - fee

                cash += proceeds - fee
                total_fee += fee
                pos["quantity"] -= qty
                trades.append({
                    "date": date_str, "code": code, "side": action,
                    "qty": qty, "price": round(price, 3),
                    "pnl": round(pnl, 2),
                })
                if pos["quantity"] <= 0:
                    del positions[code]

        # ── ② 卖出信号 ──
        for code in list(positions.keys()):
            score = day_scores.get(code, 50)
            if score <= sell_th and code in positions:
                pos = positions[code]
                qty = pos["quantity"]
                price = day_prices.get(code, pos["price"])
                proceeds = qty * price
                fee = proceeds * 0.0003
                pnl = (price - pos["avg_cost"]) * qty - fee

                cash += proceeds - fee
                total_fee += fee
                trades.append({
                    "date": date_str, "code": code, "side": "sell",
                    "qty": qty, "price": round(price, 3),
                    "pnl": round(pnl, 2),
                    "reason": f"评分{score}≤{sell_th}",
                })
                del positions[code]

        # ── ③ 买入信号（未持仓且评分达标可得） ──
        if not is_drawdown_blocked and len(positions) < MAX_POSITIONS:
            for code in sorted(STOCKS.keys()):
                if code in positions:
                    continue
                score = day_scores.get(code, 50)
                if score >= buy_th and code in day_prices:
                    price = day_prices[code]
                    amount = kelly_size(score, cash)
                    qty = int(amount / price / 100) * 100
                    if qty < 100 or qty * price > cash * 0.25:
                        # 用现金25%上限重算
                        max_qty = int(cash * 0.25 / price / 100) * 100
                        qty = min(qty, max_qty)

                    if qty >= 100:
                        cost = qty * price
                        fee = cost * 0.0003
                        cash -= cost + fee
                        total_fee += fee
                        positions[code] = {
                            "quantity": qty, "avg_cost": price,
                            "price": price, "invested": cost,
                        }
                        trades.append({
                            "date": date_str, "code": code, "side": "buy",
                            "qty": qty, "price": round(price, 3),
                            "amount": round(cost + fee, 2),
                            "reason": f"评分{score}≥{buy_th}",
                        })

        # ── ④ 周五马科维茨再平衡 ──
        if is_friday and len(positions) >= 2:
            codes_held = list(positions.keys())
            price_series = {}
            for c in codes_held:
                price_series[c] = price_history[c]
            price_df_rebal = pd.DataFrame(price_series)
            if len(price_df_rebal) >= 20:
                mw = markowitz_weights(price_df_rebal, codes_held)

                # 归一化到持仓
                total_value = cash + current_value
                for code, target_pct in mw.items():
                    if code not in positions:
                        continue
                    pos = positions[code]
                    price = day_prices.get(code, pos["price"])
                    target_value = total_value * target_pct
                    current_pos_value = pos["quantity"] * price
                    diff = target_value - current_pos_value

                    if abs(diff) < current_pos_value * 0.1:
                        continue  # 偏离<10%不动

                    if diff > 0:
                        # 加仓
                        qty = int(diff / price / 100) * 100
                        qty = min(qty, int(cash * 0.15 / price / 100) * 100)
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
                                "date": date_str, "code": code, "side": "rebalance_buy",
                                "qty": qty, "price": round(price, 3),
                            })
                    else:
                        # 减仓
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
                                "date": date_str, "code": code, "side": "rebalance_sell",
                                "qty": qty, "price": round(price, 3),
                                "pnl": round(pnl, 2),
                            })

        # ── 更新持仓市价 ──
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

    # ── 统计 ──
    log.info("\n[3/5] 计算绩效统计...")
    final_equity = daily_equity[-1]["equity"]
    total_return = (final_equity - INITIAL_CAPITAL) / INITIAL_CAPITAL

    # 年化
    n_days = len(all_dates) - warmup
    years = n_days / 252
    ann_return = (1 + total_return) ** (1 / max(years, 0.01)) - 1 if years > 0 else 0

    # 最大回撤
    equity_series = np.array([d["equity"] for d in daily_equity[warmup:]])
    peaks = np.maximum.accumulate(equity_series)
    drawdowns = (equity_series - peaks) / peaks
    max_dd = float(np.min(drawdowns)) * 100

    # 夏普
    if n_days > 1:
        daily_ret = np.diff(equity_series) / equity_series[:-1]
        sharpe = float(np.mean(daily_ret) / max(np.std(daily_ret), 1e-8) * np.sqrt(252))
    else:
        sharpe = 0.0

    # 胜率
    closed_trades = [t for t in trades if t["side"] in ("sell", "stop_loss", "take_profit")]
    wins = [t for t in closed_trades if t.get("pnl", 0) > 0]
    losses = [t for t in closed_trades if t.get("pnl", 0) <= 0]
    win_rate = len(wins) / len(closed_trades) * 100 if closed_trades else 0

    avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
    avg_loss = abs(np.mean([t["pnl"] for t in losses])) if losses else 0
    profit_factor = avg_win / avg_loss if avg_loss > 0 else float("inf")

    log.info("\n" + "=" * 60)
    log.info("📊 回测结果")
    log.info("=" * 60)
    log.info(f"  总收益率:       {total_return*100:+.2f}%")
    log.info(f"  年化收益率:     {ann_return*100:+.2f}%")
    log.info(f"  夏普比率:       {sharpe:.3f}")
    log.info(f"  最大回撤:       {max_dd:.2f}%")
    log.info(f"  胜率:           {win_rate:.1f}%")
    log.info(f"  盈亏比:         {avg_win/avg_loss:.2f}" if avg_loss > 0 else "  盈亏比: N/A")
    log.info(f"  总交易次数:     {len(trades)}")
    log.info(f"  已完结交易:     {len(closed_trades)}")
    log.info(f"  总手续费:       {total_fee:.2f}")
    log.info(f"  最终权益:       {final_equity:.2f}")

    return {
        "summary": {
            "total_return_pct": round(total_return * 100, 2),
            "ann_return_pct": round(ann_return * 100, 2),
            "sharpe_ratio": round(sharpe, 3),
            "max_drawdown_pct": round(max_dd, 2),
            "win_rate_pct": round(win_rate, 1),
            "avg_win_pnl": round(float(avg_win), 2),
            "avg_loss_pnl": round(float(avg_loss), 2),
            "profit_factor": round(float(profit_factor), 2) if profit_factor != float("inf") else "N/A",
            "total_trades": len(trades),
            "closed_trades": len(closed_trades),
            "total_fee": round(total_fee, 2),
            "final_equity": round(final_equity, 2),
            "trading_days": n_days,
            "years": round(years, 2),
        },
        "trades": trades[-50:],  # 最近50笔
        "monthly_equity": daily_equity[::20],
    }


if __name__ == "__main__":
    result = run_backtest()

    # 保存
    out_path = Path("/opt/daily_stock_analysis/simulated_trading/backtest_result.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"\n✅ 回测结果已保存至 {out_path}")
