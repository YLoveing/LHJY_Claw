#!/usr/bin/env python3
"""
migrate_cache.py — 迁移/预填充数据缓存

将现有的 all_kline_cache.json 转换为新的 DataCache 格式（.pkl），
也支持直接从 API 批量拉取并缓存。

用法:
  python3 scripts/migrate_cache.py              # 从现有 JSON 缓存迁移
  python3 scripts/migrate_cache.py --pull       # 从 API 拉取自选股数据并缓存
  python3 scripts/migrate_cache.py --all        # 从 API 拉取全市场数据并缓存
  python3 scripts/migrate_cache.py --stats      # 只输出缓存统计
"""

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

# 加入项目路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data_provider.data_cache import DataCache

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("migrate_cache")


def migrate_from_json():
    """从现有的 all_kline_cache.json 迁移到新缓存格式。"""
    json_path = Path("/opt/daily_stock_analysis/screener/cache/all_kline_cache.json")
    if not json_path.exists():
        log.warning("all_kline_cache.json 不存在，跳过迁移")
        return 0

    log.info(f"加载 {json_path}...")
    with open(json_path) as f:
        old_cache = json.load(f)

    dc = DataCache()
    migrated = 0
    skipped = 0

    for code, kline_dict in old_cache.items():
        if dc.has_kline(code):
            skipped += 1
            continue

        records = []
        for date_str, day_data in kline_dict.items():
            rec = {"date": date_str}
            if isinstance(day_data, dict):
                rec.update(day_data)
            elif isinstance(day_data, list) and len(day_data) >= 5:
                # 如果存的是列表: [open, close, high, low, volume, amount]
                fields = ["open", "close", "high", "low", "volume", "amount"]
                for i, f in enumerate(fields):
                    if i < len(day_data):
                        rec[f] = day_data[i]
            else:
                continue
            records.append(rec)

        if not records:
            skipped += 1
            continue

        df = pd.DataFrame(records)
        dc.save_kline(code, df)
        migrated += 1

        if migrated % 100 == 0:
            log.info(f"  已迁移 {migrated}/{len(old_cache)}...")

    log.info(f"迁移完成: 新缓存 {migrated} 只, 已存在跳过 {skipped} 只")
    return migrated


def pull_and_cache(stock_codes: list, max_per_round: int = 50):
    """从 API 拉取股票数据并缓存。"""
    dc = DataCache()
    pulled = 0
    already = 0

    for i, code in enumerate(stock_codes):
        # 先检查缓存
        if dc.has_kline(code):
            meta = dc.kline_meta(code)
            if meta and meta.get("rows", 0) >= 20:
                already += 1
                continue

        log.info(f"[{i + 1}/{len(stock_codes)}] 拉取 {code}...")
        try:
            df = _fetch_kline_api(code)
            if df is not None and not df.empty:
                dc.save_kline(code, df)
                pulled += 1
        except Exception as e:
            log.warning(f"  {code}: 失败 - {e}")

        # 间隔避免被封
        if (i + 1) % max_per_round == 0:
            log.info(f"  已拉取 {pulled}/{i + 1}, 等待 3s...")
            time.sleep(3)

    log.info(f"拉取完成: 新拉取 {pulled} 只, 已有缓存 {already} 只")
    return pulled


def _fetch_kline_api(code: str, days: int = 120) -> pd.DataFrame:
    """直接用东方财富 API 拉取 K 线。"""
    import requests

    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": f"1.{code}" if code.startswith("6") else f"0.{code}",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "end": "20500101",
        "lmt": days,
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/",
    }
    r = requests.get(url, params=params, headers=headers, timeout=10)
    data = r.json()
    if not data.get("data") or not data["data"].get("klines"):
        raise ValueError("API 返回空")

    records = []
    for line in data["data"]["klines"]:
        parts = line.split(",")
        records.append({
            "date": parts[0],
            "open": float(parts[1]),
            "close": float(parts[2]),
            "high": float(parts[3]),
            "low": float(parts[4]),
            "volume": float(parts[5]),
            "amount": float(parts[6]),
        })
    return pd.DataFrame(records)


def get_default_stocks() -> list:
    """获取默认自选股列表。"""
    from dotenv import dotenv_values
    env_path = Path("/opt/daily_stock_analysis/.env")
    if env_path.exists():
        env = dotenv_values(env_path)
        stock_str = env.get("STOCK_LIST", "")
        if stock_str:
            return [s.strip() for s in stock_str.split(",") if s.strip()]
    return ["510300", "002410"]


def show_stats():
    """显示缓存统计信息。"""
    dc = DataCache()
    stats = dc.stats()
    print("=== 缓存统计 ===")
    print(f"K线缓存: {stats['kline']['stocks']} 只股票, {stats['kline']['size_mb']} MB")
    print(f"股票列表缓存: {'存在' if stats['stock_list']['exists'] else '无'}")
    print(f"选股快照: {stats['screener_snapshots']} 个")
    print(f"基本面缓存: {stats['fundamentals']} 个")
    print(f"缓存目录: {stats['cache_root']}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="数据缓存迁移/预填充工具")
    parser.add_argument("--stats", action="store_true", help="只显示缓存统计")
    parser.add_argument("--pull", action="store_true", help="从 API 拉取自选股并缓存")
    parser.add_argument("--all", action="store_true", help="从 API 拉取全市场并缓存")

    args = parser.parse_args()

    if args.stats:
        show_stats()
        sys.exit(0)

    if args.pull:
        codes = get_default_stocks()
        log.info(f"拉取自选股并缓存: {codes}")
        pull_and_cache(codes)
        show_stats()
        sys.exit(0)

    if args.all:
        # 从 screener 的全 A 股列表读取
        csv_path = Path("/opt/daily_stock_analysis/screener/all_a_codes.csv")
        if csv_path.exists():
            df = pd.read_csv(csv_path)
            codes = df["code"].tolist()
            log.info(f"拉取全市场 {len(codes)} 只股票并缓存...")
            pull_and_cache(codes, max_per_round=30)
        else:
            log.error("全市场代码表不存在: screener/all_a_codes.csv")
        show_stats()
        sys.exit(0)

    # 默认：从 JSON 迁移
    count = migrate_from_json()
    show_stats()
