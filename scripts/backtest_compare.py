#!/usr/bin/env python3
"""
回测对比框架 — 对比不同选股策略的历史表现。

用法:
  python3 scripts/backtest_compare.py                     # 默认最近30天
  python3 scripts/backtest_compare.py --days=60           # 近60天
  python3 scripts/backtest_compare.py --start=20260401    # 指定起始日期

输出:
  - 控制台表格对比各策略
  - backtest_comparison_YYYYMMDD.json 详细数据
"""

# 抑制第三方库的噪音输出
import os

os.environ["TQDM_DISABLE"] = "1"

import json
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── 🔥 生产代码唯一来源 ──
from layers.execution_layer.fees import calc_buy_fees, calc_sell_fees

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backtest_compare")


def parse_args():
    days = 30
    start_date = None
    for arg in sys.argv[1:]:
        if arg.startswith("--days="):
            days = int(arg.split("=")[1])
        elif arg.startswith("--start="):
            start_date = arg.split("=")[1]
    return days, start_date


def load_historical_candidates(data_dir: Path) -> Dict[str, List[Dict]]:
    """加载历史选股结果 (screener/candidates_*.json)。"""
    candidates_by_date = {}
    for fpath in sorted(data_dir.glob("candidates_*.json")):
        date_key = fpath.stem.replace("candidates_", "")
        if not date_key.isdigit() or len(date_key) != 8:
            continue
        try:
            with open(fpath) as f:
                data = json.load(f)
            if isinstance(data, list) and len(data) > 0:
                candidates_by_date[date_key] = data
        except Exception:
            continue
    return candidates_by_date


# ── 策略评分函数 ──


def strategy_pure_tech(stock: Dict) -> float:
    """纯技术评分 (screen_score 从 indicators 重新计算)。"""
    ind = stock.get("indicators", {})
    if not ind:
        return stock.get("screen_score", 0)
    from screener.stock_screener import screen_score

    return screen_score(ind)


def strategy_llm_multi(stock: Dict) -> Dict:
    """LLM多因子评分（模拟：技术分 + 基础LLM权重）。"""
    base = strategy_pure_tech(stock)
    # LLM 模拟：假设权重 60/40
    return {
        "llm_total_score": base * 1.05,  # LLM 倾向于微调
        "screen_score": base,
        "final_score": base * 0.4 + base * 1.05 * 0.6,
    }


_FUNDAMENTAL_CACHE: Dict[str, Dict] = {}  # 避免重复查询


def _get_fundamentals_cached(code: str) -> Dict:
    if code not in _FUNDAMENTAL_CACHE:
        try:
            from data_provider.jqdata_fundamental import JQDataFundamental

            f = JQDataFundamental()
            _FUNDAMENTAL_CACHE[code] = f.get_fundamentals(code)
        except Exception:
            _FUNDAMENTAL_CACHE[code] = {}
    return _FUNDAMENTAL_CACHE[code]


def strategy_fundamental_boost(stock: Dict) -> float:
    """基本面增强评分（如有基本面数据则加权）。"""
    base = strategy_pure_tech(stock)
    code = stock.get("code", "")
    fundamentals = _get_fundamentals_cached(code)

    # 基本面加分
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

    return min(100, base + bonus)


# ── 回测模拟 ──


def simulate_backtest(
    candidates_by_date: Dict[str, List[Dict]],
    strategy_name: str,
    score_fn,
    top_n: int = 5,
    initial_cash: float = 30000.0,
) -> Dict[str, Any]:
    """
    模拟一个策略的历史表现。

    简化回测：每日按评分选 Top N，假设等权重买入，次日收盘卖出。
    计算累计收益、胜率、最大回撤、夏普。
    """
    import math

    import numpy as np

    dates = sorted(candidates_by_date.keys())
    if len(dates) < 2:
        return {"error": "数据不足"}

    cash = initial_cash
    peak = initial_cash
    portfolio_value = initial_cash
    daily_returns = []
    trades = []
    wins = 0
    losses = 0

    for i in range(len(dates) - 1):
        today = dates[i]
        tomorrow = dates[i + 1]
        today_candidates = candidates_by_date[today]
        tomorrow_candidates_map = {s["code"]: s for s in candidates_by_date[tomorrow]}

        # 评分排序
        scored = []
        for s in today_candidates:
            score = score_fn(s)
            if isinstance(score, dict):
                score = score.get("final_score", score.get("screen_score", 0))
            scored.append((score, s))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:top_n]

        if not top:
            continue

        # 等权重买入
        buy_per_stock = portfolio_value / len(top)
        day_pnl = 0.0

        for score, s in top:
            code = s["code"]
            tomorrow_s = tomorrow_candidates_map.get(code)
            if not tomorrow_s:
                continue

            buy_price = s.get("price", 0)
            sell_price = tomorrow_s.get("price", 0)
            if buy_price <= 0 or sell_price <= 0:
                continue

            shares = int(buy_per_stock / buy_price)
            if shares <= 0:
                continue

            buy_fee = calc_buy_fees(shares * buy_price)  # 引自生产模块
            sell_fee = calc_sell_fees(shares * sell_price)  # 引自生产模块
            cost = shares * buy_price + buy_fee
            proceeds = shares * sell_price - sell_fee
            pnl = proceeds - cost
            pnl_pct = ((sell_price - buy_price) / buy_price * 100) if buy_price > 0 else 0

            total_fees = buy_fee + sell_fee
            day_pnl += pnl

            trades.append(
                {
                    "date": today,
                    "code": code,
                    "name": s.get("name", ""),
                    "buy_price": round(buy_price, 2),
                    "sell_price": round(sell_price, 2),
                    "shares": shares,
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "score": round(score, 1),
                }
            )

            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1

        portfolio_value += day_pnl
        daily_return = day_pnl / (portfolio_value - day_pnl) if (portfolio_value - day_pnl) > 0 else 0
        daily_returns.append(daily_return)

        if portfolio_value > peak:
            peak = portfolio_value

    # 绩效指标
    total_days = len(daily_returns)
    total_return_pct = ((portfolio_value - initial_cash) / initial_cash) * 100
    max_drawdown = 0.0
    valley = initial_cash
    running_peak = initial_cash
    for dr in daily_returns:
        running_peak = max(running_peak, running_peak * (1 + dr))
        valley = min(valley, running_peak * (1 + dr))
        dd = (valley - running_peak) / running_peak * 100
        max_drawdown = min(max_drawdown, dd)

    if daily_returns:
        avg_return = np.mean(daily_returns)
        std_return = np.std(daily_returns)
        sharpe = (avg_return / std_return * math.sqrt(252)) if std_return > 0 else 0
    else:
        avg_return = std_return = sharpe = 0

    win_rate = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

    return {
        "strategy": strategy_name,
        "trading_days": total_days,
        "total_trades": len(trades),
        "total_return_pct": round(total_return_pct, 2),
        "final_value": round(portfolio_value, 2),
        "win_rate_pct": round(win_rate, 1),
        "max_drawdown_pct": round(abs(max_drawdown), 2),
        "sharpe_ratio": round(sharpe, 3),
        "wins": wins,
        "losses": losses,
    }


def print_comparison(results: List[Dict]):
    """打印对比表格。"""
    print()
    print("=" * 80)
    print("📊 选股策略回测对比")
    print("=" * 80)
    print(f"{'策略':<22} {'交易日':>6} {'交易次数':>8} {'总收益%':>8} {'胜率%':>7} {'最大回撤%':>9} {'夏普':>7}")
    print("-" * 80)
    for r in results:
        if "error" in r:
            print(f"{r['strategy']:<22} {'—':>6} {'—':>8} {'—':>8} {'—':>7} {'—':>9} {'—':>7}")
            print(f"  ⚠ {r['error']}")
            continue
        print(
            f"{r['strategy']:<22} "
            f"{r['trading_days']:>6} "
            f"{r['total_trades']:>8} "
            f"{r['total_return_pct']:>7.2f}% "
            f"{r['win_rate_pct']:>6.1f}% "
            f"{r['max_drawdown_pct']:>8.2f}% "
            f"{r['sharpe_ratio']:>7.3f}"
        )
    print("=" * 80)
    print(f"初始资金: ¥30,000 | 每期持仓: 等权重 Top 5 | 买入: 当日 | 卖出: 次日")
    print()


def main():
    days, start_str = parse_args()
    data_dir = Path("/opt/daily_stock_analysis/screener")
    out_dir = Path("/opt/daily_stock_analysis/reports")

    log.info(f"📥 加载选股历史数据 (最近{days}天)...")
    candidates_by_date = load_historical_candidates(data_dir)

    # 按日期过滤
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    if start_str:
        cutoff = start_str
    filtered = {k: v for k, v in candidates_by_date.items() if k >= cutoff}
    log.info(f"  共 {len(filtered)} 个交易日有选股记录")

    if len(filtered) < 5:
        log.warning(f"⚠ 数据不足 (仅 {len(filtered)} 天)，建议 --days 改大或用 --start 指定有数据的时间段")
        sys.exit(1)

    # 定义要对比的策略
    strategies = [
        ("纯技术评分", lambda s: strategy_pure_tech(s)),
        ("基本面增强", lambda s: strategy_fundamental_boost(s)),
    ]

    results = []
    for name, fn in strategies:
        log.info(f"🔄 回测: {name}...")
        t0 = time.time()
        try:
            result = simulate_backtest(filtered, name, fn)
            result["duration_s"] = round(time.time() - t0, 1)
            results.append(result)
            log.info(f"  ✅ {result['total_trades']} 笔交易, {result['total_return_pct']:.2f}%, {time.time()-t0:.1f}s")
        except Exception as e:
            log.warning(f"  ❌ {name} 回测失败: {e}")
            results.append({"strategy": name, "error": str(e)})

    print_comparison(results)

    # 保存 JSON
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"backtest_comparison_{date_str}.json"
    with open(out_path, "w") as f:
        json.dump({"run_date": date_str, "strategies": results}, f, ensure_ascii=False, indent=2)
    log.info(f"💾 已保存: {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
