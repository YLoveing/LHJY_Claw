#!/usr/bin/env python3
"""
mx_enrich.py — 在AI分析前用妙想预取财务数据

调用方式（由 run_and_send.sh 在 main.py 之前运行）：
    python3 scripts/mx_enrich.py --codes=002410,510300

输出：
    reports/fundamentals_{YYYYMMDD}.json  — 供 main.py 使用的基本面数据快照
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path("/opt/daily_stock_analysis")
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def main():
    codes_str = "002410,510300"
    for arg in sys.argv[1:]:
        if arg.startswith("--codes="):
            codes_str = arg.split("=", 1)[1]

    codes = [c.strip() for c in codes_str.split(",") if c.strip()]
    today = datetime.now().strftime("%Y%m%d")

    # 将 data_provider 加入路径
    sys.path.insert(0, str(BASE_DIR))

    from data_provider.mx_fetcher import get_financial, get_realtime_fundamentals

    result = {}
    for code in codes:
        try:
            fd = get_realtime_fundamentals(code)
            if fd:
                result[code] = fd
                # 也获取财务趋势数据
                df = get_financial(code, "最新收盘价 涨跌幅 成交量 市盈率 近20天")
                if df is not None:
                    result[code]["kline_count"] = len(df)
                time.sleep(0.3)  # 防限流
        except Exception as e:
            print(f"  [mx_enrich] {code}: {e}", file=sys.stderr)

    if result:
        out = REPORTS_DIR / f"fundamentals_{today}.json"
        out.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str)
        )
        print(f"[mx_enrich] ✅ 已保存 {len(result)} 只股票基本面数据 → {out}")
    else:
        print("[mx_enrich] ⚠ 未获取到任何基本面数据")


if __name__ == "__main__":
    main()
