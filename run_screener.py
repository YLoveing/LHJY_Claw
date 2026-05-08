#!/usr/bin/env python3
"""
run_screener.py — 选股器命令行入口，供 run_and_send.sh 调用。

用法:
  python3 run_screener.py [--max 15] [--fast]

输出:
  screener/candidates_{YYYYMMDD}.json  — 当日候选清单
  screener/top5_{YYYYMMDD}.txt         — 推送给用户的前5名摘要
"""

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

LOG_DIR = Path("/tmp")
log = logging.getLogger("run_screener")

# 确保 screener 模块在路径中
sys.path.insert(0, str(Path(__file__).parent))


def main():
    from screener.stock_screener import (
        run_screener, save_candidates, fetch_realtime_snapshot,
        extract_stock_data, basic_filter, fetch_kline_data,
        compute_screening_indicators, screen_score,
        condition_uptrend, condition_oversold,
        condition_breakout, condition_ma_cross,
    )

    max_candidates = 15
    fast_mode = False
    
    for arg in sys.argv[1:]:
        if arg.startswith("--max="):
            max_candidates = int(arg.split("=")[1])
        elif arg == "--fast":
            fast_mode = True

    logging.basicConfig(level=logging.INFO, 
                        format="[%(asctime)s] %(message)s",
                        datefmt="%H:%M:%S")

    date_str = datetime.now().strftime("%Y%m%d")
    log.info(f"选股器启动: {date_str} max={max_candidates}")

    # Step 1: 全A快照
    t0 = time.time()
    log.info("[1/3] 获取全A股实时行情...")
    snapshot = fetch_realtime_snapshot()
    log.info(f"  → {len(snapshot)} 只, {time.time()-t0:.0f}s")

    # Step 2: 基础过滤
    t0 = time.time()
    log.info("[2/3] 基础过滤...")
    candidates = []
    for item in snapshot:
        stock = extract_stock_data(item)
        if basic_filter(stock):
            candidates.append(stock)
    log.info(f"  → {len(candidates)} 只通过, {time.time()-t0:.0f}s")

    # Step 3: 技术面筛选
    t0 = time.time()
    log.info(f"[3/3] 技术面筛选 (max={max_candidates})...")
    scored = []
    skipped = 0
    for i, stock in enumerate(candidates, 1):
        kline = fetch_kline_data(stock["code"], days=60)
        if kline is None or len(kline) < 20:
            skipped += 1
            continue
        ind = compute_screening_indicators(kline)

        conds = []
        if condition_uptrend(ind):
            conds.append("多头")
        if condition_oversold(ind):
            conds.append("超卖反弹")
        if condition_breakout(ind):
            conds.append("放量突破")
        if condition_ma_cross(ind):
            conds.append("金叉")
        if not conds:
            continue

        s = screen_score(ind)
        stock["screen_score"] = s
        stock["conditions"] = conds
        stock["indicators"] = {
            "MA5": round(ind.get("MA5", 0), 2),
            "MA10": round(ind.get("MA10", 0), 2),
            "MA20": round(ind.get("MA20", 0), 2),
            "RSI": round(ind.get("RSI", 0), 1),
            "vol_ratio": round(ind.get("vol_ratio", 0), 2),
            "price_MA20_pct": round(ind.get("price_MA20_pct", 0), 1),
        }
        scored.append(stock)

        if (i % 50) == 0:
            log.info(f"  处理 {i}/{len(candidates)} ({len(scored)} 候选)")

    # 排序
    scored.sort(key=lambda x: x["screen_score"], reverse=True)
    selected = scored[:max_candidates]
    log.info(f"  → {len(selected)} 只候选, 跳过{skipped}只K线不足, {time.time()-t0:.0f}s")

    # 保存 JSON
    json_path = f"screener/candidates_{date_str}.json"
    save_candidates(selected, json_path)

    # 生成摘要
    top5_path = f"screener/top5_{date_str}.txt"
    lines = [f"📋 今日选股 Top {len(selected)}"]
    lines.append(f"扫描{len(candidates)}只 > 筛选{len(scored)}只 > 候选{len(selected)}只")
    lines.append("")
    
    for i, s in enumerate(selected[:5], 1):
        conds_str = "|".join(s["conditions"])
        lines.append(
            f"#{i} {s['code']} {s['name']} "
            f"评分{s['screen_score']:.0f} [{conds_str}] "
            f"价{s['price']:.2f}"
        )

    if len(selected) > 5:
        lines.append(f"... 共{len(selected)}只候选，详情见筛选清单")

    Path(top5_path).write_text("\n".join(lines), encoding="utf-8")
    log.info(f"  ✅ 摘要已保存: {top5_path}")

    print(f"\n✅ 选股完成: {len(selected)} 只候选")
    return 0


if __name__ == "__main__":
    sys.exit(main())
