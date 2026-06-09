#!/usr/bin/env python3
"""
统一回测运行器 — 支持多种回测场景。

用法:
  python3 scripts/run_backtest.py                  # 默认：快速回测（最近30天）
  python3 scripts/run_backtest.py --scenario=quick # 快速（选股器数据）
  python3 scripts/run_backtest.py --scenario=full  # 全A股（2300+只）
  python3 scripts/run_backtest.py --scenario=hs300 # 沪深300（v4消除幸存者偏差）
  python3 scripts/run_backtest.py --days=60        # 指定天数
  python3 scripts/run_backtest.py --all            # 运行所有场景
  python3 scripts/run_backtest.py --compare        # 多策略对比
  python3 scripts/run_backtest.py --save           # 保存结果到 reports/
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("run_backtest")

REPORTS_DIR = Path("reports")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

os.environ["TQDM_DISABLE"] = "1"


def run_quick(days: int = 30, save: bool = False) -> Optional[dict]:
    """快速回测 — 基于选股器历史候选股数据。"""
    try:
        from scripts.backtest_compare import (
            load_historical_candidates,
            print_comparison,
            simulate_backtest,
            strategy_pure_tech,
        )

        candidates_by_date = load_historical_candidates(Path("screener"))
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        filtered = {k: v for k, v in candidates_by_date.items() if k >= cutoff}

        if not filtered:
            log.warning("⚠️ 无候选股历史数据")
            return None

        log.info(f"📊 快速回测: {len(filtered)} 个交易日 ({min(filtered)} ~ {max(filtered)})")
        t0 = time.time()
        result = simulate_backtest(filtered, "纯技术评分", lambda s: strategy_pure_tech(s))
        result["duration_s"] = round(time.time() - t0, 1)
        print_comparison([result])

        if save:
            fname = REPORTS_DIR / f"backtest_quick_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(fname, "w") as f:
                json.dump(
                    {"run_date": datetime.now().isoformat(), "strategies": [result]}, f, ensure_ascii=False, indent=2
                )
            log.info(f"💾 保存到 {fname}")

        return result
    except Exception as e:
        log.warning(f"⚠️ 快速回测跳过: {e}")
        return None


def run_real(days: int = 60, save: bool = False) -> Optional[dict]:
    """🔥 生产代码回测 — DataCache全量 + 真实费率 + 真实风控。"""
    try:
        import subprocess

        cmd = [sys.executable, "scripts/backtest_real.py", f"--days={days}"]
        if save:
            cmd.append("--save")
        log.info(f"📊 生产代码回测 ({days}天, DataCache 1570+只)...")
        t0 = time.time()
        subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
        elapsed = time.time() - t0
        log.info(f"✅ 生产代码回测完成 ({elapsed:.0f}s)")
        return {"status": "ok", "elapsed": elapsed}
    except Exception as e:
        log.warning(f"⚠️ 生产代码回测跳过: {e}")
        return None


def run_full(save: bool = False) -> Optional[dict]:
    """全A股回测（旧版，schema不匹配，建议用real）。"""
    log.warning("⚠️ 旧版全A股回测不可用，请使用 --scenario=real")
    return None


def run_hs300(save: bool = False) -> Optional[dict]:
    """沪深300回测（v4 消除幸存者偏差）。"""
    try:
        log.info("📊 沪深300回测 (v4 幸存者偏差消除)...")
        from scripts.backtest_hs300_v4_survivorship import main as hs300_main

        t0 = time.time()
        result = hs300_main()
        log.info(f"✅ 沪深300回测完成 ({time.time() - t0:.0f}s)")
        return result
    except Exception as e:
        log.warning(f"⚠️ 沪深300回测跳过: {e}")
        return None


def run_compare(days: int = 30, save: bool = False) -> Optional[list]:
    """多策略对比回测。"""
    try:
        log.info("📊 多策略对比回测...")
        from scripts.backtest_compare import main as compare_main

        old_argv = sys.argv
        sys.argv = ["backtest_compare.py", f"--days={days}"]
        result = compare_main()
        sys.argv = old_argv

        if save and result:
            fname = REPORTS_DIR / f"backtest_compare_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(fname, "w") as f:
                json.dump(
                    {"run_date": datetime.now().isoformat(), "strategies": result}, f, ensure_ascii=False, indent=2
                )
            log.info(f"💾 保存到 {fname}")
        return result
    except Exception as e:
        log.warning(f"⚠️ 对比回测跳过: {e}")
        return None


def save_summary(results: dict):
    """保存全场景汇总结果。"""
    fname = REPORTS_DIR / f"backtest_all_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(fname, "w") as f:
        json.dump(
            {"run_date": datetime.now().isoformat(), "results": {k: v for k, v in results.items() if v is not None}},
            f,
            ensure_ascii=False,
            indent=2,
        )
    log.info(f"💾 汇总保存到 {fname}")


def main():
    parser = argparse.ArgumentParser(description="统一回测运行器")
    parser.add_argument(
        "--scenario",
        choices=["quick", "full", "hs300", "compare", "real"],
        default="quick",
        help="回测场景 (default: quick, real=生产代码+全量数据)",
    )
    parser.add_argument("--days", type=int, default=30, help="回测天数 (quick/compare)")
    parser.add_argument("--all", action="store_true", help="运行所有场景")
    parser.add_argument("--compare", action="store_true", help="运行多策略对比")
    parser.add_argument("--save", action="store_true", help="保存结果到 reports/")
    args = parser.parse_args()

    if args.all:
        log.info("=" * 50)
        log.info("🚀 全场景回测启动")
        log.info("=" * 50)
        results = {}
        for scenario in ["quick", "real", "compare", "hs300"]:
            log.info(f"\n── [{scenario}] ──")
            t0 = time.time()
            if scenario == "quick":
                results[scenario] = run_quick(args.days, args.save)
            elif scenario == "real":
                results[scenario] = run_real(args.days, args.save)
            elif scenario == "compare":
                results[scenario] = run_compare(args.days, args.save)
            elif scenario == "hs300":
                results[scenario] = run_hs300(args.save)
            log.info(f"  耗时 {time.time() - t0:.0f}s")

        log.info("\n" + "=" * 50)
        log.info("🏁 全场景回测完成")
        for name, r in results.items():
            if r:
                ret = r.get("total_return_pct", "N/A")
                win = r.get("win_rate_pct", "N/A")
                log.info(f"  {name}: 收益{ret}% 胜率{win}%")

        if args.save:
            save_summary(results)
        return results

    if args.compare:
        args.scenario = "compare"

    t0 = time.time()
    scenario_map = {
        "quick": run_quick,
        "full": run_full,
        "real": run_real,
        "hs300": run_hs300,
        "compare": run_compare,
    }
    runner = scenario_map.get(args.scenario, run_quick)
    result = runner(args.days, args.save) if args.scenario in ("quick", "compare", "real") else runner(args.save)

    elapsed = time.time() - t0
    if result:
        log.info(f"\n✅ 回测完成 ({elapsed:.0f}s)")
    else:
        log.info(f"\n⚠️ 回测未运行 ({elapsed:.0f}s)")

    return result


if __name__ == "__main__":
    main()
