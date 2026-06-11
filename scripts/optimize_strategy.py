#!/usr/bin/env python3
"""
多因子策略优化 — Walk-forward 5折 + 36组参数网格

用法: python3 scripts/optimize_strategy.py
输出: reports/optimization_results.json (全参数排名)
      reports/final_backtest.json  (最优策略全样本权益曲线)
"""

import datetime
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest_engine.engine import (
    BacktestEngine,
    compute_factor_values,
    load_data,
)

# ── 策略权重 — 单一事实来源（config/strategies.py） ──
from config.strategies import STRATEGIES

TOP_N_VALUES = [5, 8, 10, 12, 15, 20]
REBALANCE_VALUES = [21, 42]


def make_walk_folds(dates: list, train_days: int = 84, test_days: int = 42, step: int = 42) -> list:
    """
    生成 Walk-forward 5折

    默认: train=84交易日(~4月), test=42交易日(~2月), step=42(50%重叠)
    返回 [(train_dates, test_dates), ...]
    """
    folds = []
    i = 0
    while i + train_days + test_days <= len(dates):
        train = dates[i : i + train_days]
        test = dates[i + train_days : i + train_days + test_days]
        if len(train) < 60 or len(test) < 20:
            break
        folds.append((train, test))
        i += step
    return folds


def evaluate_params(
    factor_df: pd.DataFrame,
    price_df: pd.DataFrame,
    strategy_name: str,
    weights: dict,
    top_n: int,
    reb_days: int,
    folds: list,
) -> dict:
    """对一组参数跑 Walk-forward 5折, 返回聚合结果"""
    fold_results = []

    for fi, (train_dates, test_dates) in enumerate(folds):
        tr_f = factor_df[factor_df["date"].isin(train_dates)]
        tr_p = price_df[price_df["date"].isin(test_dates)]
        te_f = factor_df[factor_df["date"].isin(test_dates)]
        te_p = price_df[price_df["date"].isin(test_dates)]

        engine = BacktestEngine(
            factor_weights=weights,
            top_n=top_n,
            rebalance_days=reb_days,
            use_open=True,
            slippage=0.0015,
        )

        result = engine.run(te_f, te_p)

        fold_results.append(
            {
                "fold": fi + 1,
                "test_start": str(test_dates[0])[:10],
                "test_end": str(test_dates[-1])[:10],
                "return_pct": result["total_return_pct"],
                "sharpe": result["sharpe_ratio"],
                "max_dd": result["max_drawdown_pct"],
                "win_rate": result["win_rate_pct"],
                "trades": result["total_trades"],
            }
        )

    if not fold_results:
        return {"error": "no folds"}

    sharpe_vals = [r["sharpe"] for r in fold_results]
    ret_vals = [r["return_pct"] for r in fold_results]
    dd_vals = [r["max_dd"] for r in fold_results]
    total_trades = sum(r["trades"] for r in fold_results)

    return {
        "strategy": strategy_name,
        "top_n": top_n,
        "rebalance_days": reb_days,
        "fold_results": fold_results,
        "avg_sharpe": round(np.mean(sharpe_vals), 3),
        "std_sharpe": round(np.std(sharpe_vals), 3),
        "avg_return_pct": round(np.mean(ret_vals), 2),
        "avg_max_dd_pct": round(np.mean(dd_vals), 2),
        "min_max_dd_pct": round(min(dd_vals), 2),
        "total_trades": total_trades,
    }


def main():
    t0 = time.time()
    print("=" * 60)
    print("📊 多因子策略优化 — Walk-forward 5折")
    print("=" * 60)

    # 1. 加载数据 + 计算因子
    df = load_data()
    price_df = df[["date", "code", "open", "close"]].copy()
    factor_df = compute_factor_values(df)

    # 2. 截取有效区间 (2025-03-20 起, 确保足量历史)
    cutoff = pd.Timestamp("2025-03-20")
    factor_df = factor_df[factor_df["date"] >= cutoff].copy()
    price_df = price_df[price_df["date"] >= cutoff].copy()

    all_dates = sorted(factor_df["date"].unique())
    print(f"  有效区间: {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)}天)")

    folds = make_walk_folds(all_dates)
    print(f"  Walk-forward 折数: {len(folds)}")
    for fi, (tr, te) in enumerate(folds):
        print(f"    Fold {fi+1}: train {tr[0]}~{tr[-1]}  test {te[0]}~{te[-1]}")

    # 3. 网格遍历
    all_results = []
    total_combos = len(STRATEGIES) * len(TOP_N_VALUES) * len(REBALANCE_VALUES)
    combo = 0

    for sname, sweights in STRATEGIES.items():
        for top_n in TOP_N_VALUES:
            for reb_d in REBALANCE_VALUES:
                combo += 1
                print(f"\n[{combo}/{total_combos}] {sname} top{top_n} {reb_d}d...", end=" ", flush=True)
                res = evaluate_params(factor_df, price_df, sname, sweights, top_n, reb_d, folds)
                if "error" not in res:
                    all_results.append(res)
                    print(
                        f"✅ 夏普={res['avg_sharpe']:.3f} "
                        f"收益={res['avg_return_pct']:.2f}% "
                        f"回撤={res['avg_max_dd_pct']:.2f}%",
                        flush=True,
                    )
                else:
                    print("❌", res["error"])

    # 4. 排名
    all_results.sort(key=lambda r: r["avg_sharpe"], reverse=True)

    print("\n" + "=" * 80)
    print("🏆 Walk-forward 排名 (按平均夏普降序)")
    print("=" * 80)
    header = f"{'策略':<12} {'top':>4} {'调仓':>4} {'平均夏普':>8} {'夏普std':>8} {'平均收益':>8} {'平均回撤':>10} {'最低回撤':>10} {'交易':>6}"
    print(header)
    print("-" * 80)
    for i, r in enumerate(all_results[:15]):
        print(
            f"{r['strategy']:<12} {r['top_n']:>4} {r['rebalance_days']:>4}d "
            f"{r['avg_sharpe']:>8.3f} {r['std_sharpe']:>8.3f} "
            f"{r['avg_return_pct']:>8.2f}% {r['avg_max_dd_pct']:>10.2f}% "
            f"{r['min_max_dd_pct']:>10.2f}% {r['total_trades']:>6}"
        )

    # 5. 保存结果
    out_path = Path("reports/optimization_results.json")
    with open(out_path, "w") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n💾 报告: {out_path}")

    # 6. 选最优做全样本验证
    best = None
    for r in all_results:
        if r["min_max_dd_pct"] >= -25.0:  # 回撤< -25%
            best = r
            break

    if best:
        print("\n" + "=" * 60)
        print(f"🏆 最优参数: {best['strategy']} top{best['top_n']} " f"{best['rebalance_days']}d")
        print(f"  平均夏普: {best['avg_sharpe']:.3f} (±{best['std_sharpe']:.3f})")
        print(f"  平均收益: {best['avg_return_pct']:.2f}%")
        print(f"  最低回撤: {best['min_max_dd_pct']:.2f}%")
        print("=" * 60)

        print(f"\n📈 全样本验证...", flush=True)
        weights = STRATEGIES[best["strategy"]]
        engine = BacktestEngine(
            factor_weights=weights,
            top_n=best["top_n"],
            rebalance_days=best["rebalance_days"],
            use_open=True,
            slippage=0.0015,
        )
        final = engine.run(factor_df, price_df)
        print(f"  收益: {final['total_return_pct']:.2f}%")
        print(f"  夏普: {final['sharpe_ratio']:.3f}")
        print(f"  回撤: {final['max_drawdown_pct']:.2f}%")
        print(f"  胜率: {final['win_rate_pct']:.1f}%")
        print(f"  交易: {final['total_trades']} 次")
        print(f"  天数: {final['trading_days']}")

        final_path = Path("reports/final_backtest.json")
        with open(final_path, "w") as f:
            json.dump(final, f, ensure_ascii=False, indent=2)
        print(f"💾 权益曲线: {final_path}")

    print(f"\n⏱ 总计: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
