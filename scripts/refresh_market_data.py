#!/usr/bin/env python3
"""
refresh_market_data.py — 全市场数据增量刷新 (Baostock 源)

从 Baostock 拉取增量日 K 线数据 → 更新 JSON 缓存 → 更新 backtest_data.db

用法:
  python3 scripts/refresh_market_data.py           # 增量刷新
  python3 scripts/refresh_market_data.py --full    # 全量刷新（覆盖重建）
"""
import sys, time, json, sqlite3
from datetime import datetime, date
from pathlib import Path

BASE_DIR = Path("/opt/daily_stock_analysis")
sys.path.insert(0, str(BASE_DIR))

JSON_PATH = BASE_DIR / "screener" / "cache" / "all_kline_cache.json"
DB_PATH = BASE_DIR / "data" / "backtest_data.db"
TODAY = date.today().strftime("%Y-%m-%d")
BATCH_SIZE = 100


def main():
    full_refresh = "--full" in sys.argv
    t0_total = time.time()

    print(f"╔{'═' * 50}╗")
    print(f"║  📡 全市场数据刷新 (Baostock)            ║")
    print(f"║  {TODAY}                   ║")
    print(f"╚{'═' * 50}╝")

    # 1. 加载现有缓存, 确定最新日期
    print(f"\n📥 加载现有缓存...", end=" ", flush=True)
    with open(JSON_PATH) as f:
        cache = json.load(f)
    codes = sorted(cache.keys())
    print(f"{len(codes)} 只")

    # 找到所有股票中最晚的日期
    all_max_dates = sorted(
        set(max(v.keys()) for v in cache.values() if v),
        reverse=True
    )
    cache_max = all_max_dates[0] if all_max_dates else "0000-00-00"
    need_update = sum(1 for v in cache.values() if not v or max(v.keys()) < TODAY)
    print(f"  缓存最新: {cache_max}, 需更新: {need_update}/{len(codes)} 只")

    if need_update == 0:
        print(f"  ✅ 数据已是最新! 跳过刷新")
        return 0

    # 2. 连接 Baostock
    import baostock as bs
    print(f"\n🔗 连接 Baostock...", end=" ", flush=True)
    lg = bs.login()
    if lg.error_code != "0":
        print(f"❌ Baostock 登录失败: {lg.error_msg}")
        return 1
    print("OK")

    # 3. 只拉取增量数据 (从缓存最新日期的下一天到今日)
    start = cache_max if not full_refresh else "2024-12-01"
    pulled = 0
    failed = 0
    skipped = 0
    total_rows = 0
    t0 = time.time()
    last_progress = 0

    for idx, code in enumerate(codes, 1):
        # 检查是否需要
        old_dates = set(cache.get(code, {}).keys())
        if old_dates and max(old_dates) >= TODAY:
            skipped += 1
            continue

        prefix = "sh" if code.startswith("6") else "sz"

        try:
            rs = bs.query_history_k_data_plus(
                f"{prefix}.{code}",
                "date,code,open,high,low,close,volume,amount",
                start_date=start,
                end_date=TODAY,
                frequency="d",
                adjustflag="2",
            )
            if rs.error_code != "0":
                failed += 1
                continue

            records = {}
            while rs.next():
                row = rs.get_row_data()
                d, c, o, h, lo, cl, v, a = row
                if o and cl:
                    records[d] = {
                        "open": float(o),
                        "close": float(cl),
                        "high": float(h),
                        "low": float(lo),
                        "volume": float(v or 0),
                        "amount": float(a or 0),
                    }
            if records:
                # 合并：保留旧数据 + 新数据
                if code in cache:
                    cache[code].update(records)
                else:
                    cache[code] = records
                total_rows += len(records)
                pulled += 1
            else:
                skipped += 1
        except Exception as e:
            failed += 1
            if idx % 200 == 0:
                print(f"  [{idx}] {code}: {e}", flush=True)

        # 进度报告（每 100 只）
        if idx - last_progress >= 100:
            elapsed = time.time() - t0
            rps = idx / elapsed if elapsed > 0 else 0
            remaining = need_update - idx
            eta = remaining / rps if rps > 0 else 0
            print(f"  [{idx}/{len(codes)}] 拉取 {pulled} 失败 {failed} 跳过 {skipped} "
                  f"| {rps:.1f}只/s ETA {eta:.0f}s", flush=True)
            last_progress = idx

    bs.logout()
    elapsed_dl = time.time() - t0
    print(f"\n✅ 下载完成: {pulled} 只, 跳过 {skipped}, 失败 {failed}")
    print(f"   增量行: {total_rows:,} | 下载用时: {elapsed_dl:.0f}s")

    if pulled == 0 and total_rows == 0:
        print("  ⚠️ 无需更新")
        return 0

    # 4. 写入 JSON 缓存
    print(f"\n💾 写入 JSON 缓存...", end=" ", flush=True)
    t0 = time.time()
    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    JSON_PATH.rename(JSON_PATH.with_suffix(".json.bak"))
    with open(JSON_PATH, "w") as f:
        json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))
    new_mb = JSON_PATH.stat().st_size / 1024 / 1024
    print(f"{new_mb:.0f}MB ({time.time()-t0:.1f}s)")

    # 5. 增量更新 backtest_data.db
    print(f"\n🏗️  更新 backtest_data.db...", flush=True)
    t0 = time.time()

    if not DB_PATH.exists() or full_refresh:
        # 重建
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily (
                date TEXT NOT NULL,
                code TEXT NOT NULL,
                open REAL, close REAL, high REAL, low REAL,
                volume REAL, amount REAL,
                PRIMARY KEY (date, code)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_date ON daily(date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_code ON daily(code)")
    else:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")

    # 获取已存在的 (date,code) 集合（增量更新用）
    existing = set()
    if not full_refresh:
        cursor = conn.execute("SELECT date, code FROM daily")
        existing = {(r[0], r[1]) for r in cursor.fetchall()}

    # 批量插入新数据
    batch = []
    insert_count = 0
    for code, klines in cache.items():
        for d, k in klines.items():
            if (d, code) in existing and not full_refresh:
                continue
            batch.append((
                d, code,
                float(k["open"]), float(k["close"]),
                float(k["high"]), float(k["low"]),
                float(k["volume"]), float(k.get("amount", 0)),
            ))
            insert_count += 1
            if len(batch) >= BATCH_SIZE:
                conn.executemany(
                    "INSERT OR IGNORE INTO daily VALUES (?,?,?,?,?,?,?,?)", batch
                )
                conn.commit()
                batch = []
    if batch:
        conn.executemany(
            "INSERT OR IGNORE INTO daily VALUES (?,?,?,?,?,?,?,?)", batch
        )
        conn.commit()

    cursor = conn.execute(
        "SELECT COUNT(DISTINCT code), COUNT(*), MIN(date), MAX(date) FROM daily"
    )
    n_stocks, n_rows, d_min, d_max = cursor.fetchone()
    conn.close()

    print(f"  ✅ {n_stocks} 只, {n_rows:,} 行, {d_min} ~ {d_max}")
    print(f"  💾 写入新行: {insert_count:,} | DB 重建: {time.time()-t0:.1f}s")

    total_elapsed = time.time() - t0_total
    print(f"\n{'=' * 50}")
    print(f"🎉 刷新完成! 总耗时: {total_elapsed:.0f}s")
    print(f"   缓存: {new_mb:.0f}MB | DB: {n_stocks}只 × {n_rows:,}行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
