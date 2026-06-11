#!/usr/bin/env python3
"""
将 JSON 缓存导入专用 SQLite 作为回测数据源。
单文件、稳定、全量（1570只 × 333天）
"""

import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path("data/backtest_data.db")
JSON_PATH = Path("screener/cache/all_kline_cache.json")


def main():
    t0 = time.time()
    print("📥 加载 JSON 缓存...", flush=True)
    with open(JSON_PATH) as f:
        raw = json.load(f)

    # 建库
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")

    # 建表: 按日期分区存储
    conn.execute("""
        CREATE TABLE daily (
            date TEXT NOT NULL,
            code TEXT NOT NULL,
            open REAL,
            close REAL,
            high REAL,
            low REAL,
            volume REAL,
            amount REAL,
            PRIMARY KEY (date, code)
        )
    """)
    conn.execute("CREATE INDEX idx_daily_date ON daily(date)")
    conn.execute("CREATE INDEX idx_daily_code ON daily(code)")

    # 批量写入
    batch = []
    total = 0
    for code, klines in raw.items():
        for date, k in klines.items():
            batch.append(
                (
                    date,
                    code,
                    float(k["open"]),
                    float(k["close"]),
                    float(k["high"]),
                    float(k["low"]),
                    float(k["volume"]),
                    float(k.get("amount", 0)),
                )
            )
            total += 1
            if len(batch) >= 50000:
                conn.executemany("INSERT INTO daily VALUES (?,?,?,?,?,?,?,?)", batch)
                conn.commit()
                batch = []

    if batch:
        conn.executemany("INSERT INTO daily VALUES (?,?,?,?,?,?,?,?)", batch)
        conn.commit()

    # 统计
    cursor = conn.execute("SELECT COUNT(DISTINCT code), COUNT(*), MIN(date), MAX(date) FROM daily")
    n_stocks, n_rows, d_min, d_max = cursor.fetchone()
    print(f"  ✅ {n_stocks} 只股票, {n_rows:,} 行, {d_min} ~ {d_max}, {time.time()-t0:.1f}s")

    conn.close()


if __name__ == "__main__":
    main()
