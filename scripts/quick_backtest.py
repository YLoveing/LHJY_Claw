#!/usr/bin/env python3
"""Quick backtest runner - pure tech only (skip slow fundamental)."""
import sys, json, time, math
from pathlib import Path
from datetime import datetime

sys.path.insert(0, "/opt/daily_stock_analysis")

from scripts.backtest_compare import (
    load_historical_candidates, simulate_backtest, 
    print_comparison, strategy_pure_tech
)

data_dir = Path("/opt/daily_stock_analysis/screener")
candidates_by_date = load_historical_candidates(data_dir)
cutoff = "20260508"
filtered = {k: v for k, v in candidates_by_date.items() if k >= cutoff}
print(f"交易日: {len(filtered)} days ({min(filtered.keys())} ~ {max(filtered.keys())})")

# Pure tech only (fast)
t0 = time.time()
result = simulate_backtest(filtered, "纯技术评分", lambda s: strategy_pure_tech(s))
result["duration_s"] = round(time.time() - t0, 1)
results = [result]
print_comparison(results)

# Save
out = Path("/opt/daily_stock_analysis/reports") / f"backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
out.parent.mkdir(parents=True, exist_ok=True)
with open(out, "w") as f:
    json.dump({"run_date": datetime.now().isoformat(), "strategies": results}, f, ensure_ascii=False, indent=2)
print(f"\n💾 Saved: {out}")
