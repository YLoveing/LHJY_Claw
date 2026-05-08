#!/usr/bin/env python3
"""
expand_cache.py — 扩展回测缓存，消除幸存者偏差

流程：
  1. 读取当前缓存 (candidates_20260508.json 中的股票)
  2. 读取全A股代码列表
  3. 补下载不在当前缓存中的股票的数据
  4. 合并缓存

这样回测就能买到"已经死了"的股票——如果它们曾经符合条件。
"""

import json
import logging
import sys
import time
from pathlib import Path

import akshare as ak
import requests

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("expand")

CACHE_DIR = Path("/opt/daily_stock_analysis/screener/cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "all_kline_cache.json"

EM_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://quote.eastmoney.com/",
}


def load_cache() -> dict:
    if CACHE_FILE.exists():
        raw = CACHE_FILE.read_text(encoding="utf-8")
        return json.loads(raw)
    return {}


def save_cache(data: dict):
    CACHE_FILE.write_text(json.dumps(data, default=str, ensure_ascii=False))
    log.info(f"  缓存已保存: {len(data)} 只, {CACHE_FILE.stat().st_size/1024/1024:.1f}MB")


def fetch_kline(code: str, days: int = 300) -> dict:
    """从东方财富获取K线数据。"""
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    secid = f"1.{code}" if code.startswith("6") else f"0.{code}"
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101", "fqt": "1",
        "end": "20500101", "lmt": days,
    }
    try:
        r = requests.get(url, params=params, headers=EM_HEADERS, timeout=10)
        data = r.json()
        if data.get("data") and data["data"].get("klines"):
            result = {}
            for line in data["data"]["klines"]:
                parts = line.split(",")
                result[parts[0]] = {
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "volume": float(parts[5]),
                    "amount": float(parts[6]),
                }
            return result
    except Exception as e:
        pass
    return {}


def main():
    log.info("=" * 60)
    log.info("📦 扩展缓存 — 消除幸存者偏差")
    log.info("=" * 60)

    # 1. 获取全A股列表
    log.info("\n[1/4] 获取全A股代码列表...")
    codes_df = ak.stock_info_a_code_name()
    all_codes = []
    for _, row in codes_df.iterrows():
        code = str(row["code"]).strip().zfill(6)
        name = str(row["name"]).strip()
        if "ST" in name.upper() or "退" in name: continue
        if code.startswith("8") or code.startswith("4"): continue  # 北交所
        all_codes.append(code)
    log.info(f"  全A股(非ST/非北交所): {len(all_codes)} 只")

    # 2. 现有缓存
    log.info("\n[2/4] 读取现有缓存...")
    cache = load_cache()
    existing_codes = set(cache.keys())
    log.info(f"  缓存中: {len(existing_codes)} 只")

    # 3. 按流动性补充 — 找出今日有足够成交量的股票
    log.info("\n[3/4] 从全A股列表中筛选需要补充的股票...")
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    need_codes = []
    page = 1
    total_pages = 2000 // 100 + 1  # 最多取2000只
    while page <= total_pages:
        params = {
            "pn": page, "pz": 100, "po": 0, "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": 2, "invt": 2,
            "fid": "f3", "fs": "m:0+t:1,m:1+t:2,m:0+t:7",
            "fields": "f2,f12,f14,f20",
        }
        try:
            r = requests.get(url, params=params, headers=EM_HEADERS, timeout=10)
            data = r.json()
            batch = data["data"]["diff"]
            for item in batch:
                code = str(item.get("f12", "")).strip()
                price = item.get("f2")
                try: price = float(price)
                except: price = 0
                if not code[:6].isdigit(): continue
                if "ST" in str(item.get("f14", "")).upper(): continue
                if price < 3: continue
                if code not in existing_codes:
                    need_codes.append(code)
            if page * 100 >= data["data"]["total"]: break
            page += 1
        except Exception as e:
            log.warning(f"  分页{page}失败: {e}")
            break

    log.info(f"  需要下载: {len(need_codes)} 只 (今日价>3元的非ST非缓存股票)")

    # 按是否在 A 股主列表过滤
    all_set = set(all_codes)
    need_codes = [c for c in need_codes if c in all_set]
    log.info(f"  在A股主列表中: {len(need_codes)} 只")

    # 4. 下载
    log.info("\n[4/4] 补下载K线数据...")
    downloaded = 0
    failed = 0
    for i, code in enumerate(need_codes, 1):
        kline = fetch_kline(code)
        if kline and len(kline) >= 20:
            cache[code] = kline
            downloaded += 1
        else:
            failed += 1
        
        if i % 50 == 0:
            save_cache(cache)
            log.info(f"  [{i}/{len(need_codes)}] 累计已下载{downloaded}只, 失败{failed}只")

    save_cache(cache)

    log.info(f"\n{'=' * 60}")
    log.info(f"✅ 扩展完成")
    log.info(f"  原有缓存: {len(existing_codes)} 只")
    log.info(f"  新增下载: {downloaded} 只")
    log.info(f"  下载失败: {failed} 只")
    log.info(f"  总缓存:   {len(cache)} 只")
    log.info(f"{'=' * 60}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
