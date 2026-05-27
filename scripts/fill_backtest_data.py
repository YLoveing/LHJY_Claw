#!/usr/bin/env python3
"""
回测数据补全脚本
诊断目标股票的 K线缓存状态，对不足200条的用 fallback 数据源补全。
"""
import sys
import logging
from pathlib import Path

sys.path.insert(0, "/opt/daily_stock_analysis")

# 设置日志
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("data_gap_filler")

# ── 目标股票清单 ──
# 1. 模拟持仓 (state.json)
# 2. 自选股 (.env STOCK_LIST)
# 3. 手动指定需要补全的股票
TARGET_CODES = [
    "601689",  # 拓普集团 - 模拟持仓
    "002410",  # 广联达 - 自选股
    "510300",  # 沪深300 ETF - 自选股
    "600519",  # 贵州茅台 - 指定补拉
]

MIN_ROWS = 200  # 回测最低要求


def diagnose():
    """诊断所有目标股票的缓存状态。"""
    from data_provider.data_cache import DataCache

    cache = DataCache()
    print("\n" + "=" * 70)
    print(f"{'代码':<10} {'缓存行数':>8} {'日期范围':>28} {'状态'}")
    print("=" * 70)

    diagnosis = {}
    for code in TARGET_CODES:
        df = cache.get_kline(code)
        meta = cache.kline_meta(code)

        if df is not None:
            rows = len(df)
            date_min = meta.get("date_range", ["N/A", "N/A"])[0] if meta else "N/A"
            date_max = meta.get("date_range", ["N/A", "N/A"])[1] if meta else "N/A"
            date_range = f"{date_min} ~ {date_max}"
            if rows >= MIN_ROWS:
                status = "✅ 充足"
            elif rows >= 100:
                status = "⚠️ 勉强"
            else:
                status = "🔴 不足"
        else:
            rows = 0
            date_range = "N/A"
            status = "❌ 无缓存"

        print(f"{code:<10} {rows:>8} {date_range:>28} {status}")
        diagnosis[code] = {"rows": rows, "status": status, "date_range": date_range}

    print("=" * 70)

    # 统计
    sufficient = sum(1 for d in diagnosis.values() if d["rows"] >= MIN_ROWS)
    insufficient = sum(1 for d in diagnosis.values() if d["rows"] < MIN_ROWS)
    print(f"\n📊 汇总: {sufficient}/{len(TARGET_CODES)} 达标, {insufficient} 需要补全")
    return diagnosis


def fill_gaps(diagnosis):
    """对数据不足的股票，用 fallback 数据源拉取并写入缓存。"""
    from data_provider.data_cache import DataCache
    from data_provider.fallback_provider import get_fallback_provider

    cache = DataCache()
    provider = get_fallback_provider()

    # 筛选需要补全的股票
    needs_fill = [code for code, info in diagnosis.items() if info["rows"] < MIN_ROWS]

    if not needs_fill:
        print("\n✅ 所有目标股票数据充足，无需补全！")
        return

    print(f"\n🔧 开始补全 {len(needs_fill)} 只股票: {needs_fill}")

    results = {}
    for code in needs_fill:
        print(f"\n--- 补全 {code} ---")
        try:
            # 从 fallback 数据源拉取（尝试300天，足够覆盖回测需求）
            df = provider.get_kline_data(code, days=300)
            if df is not None and not df.empty:
                print(f"  获取到 {len(df)} 行数据 (来源: fallback)")

                # 标准化列名以符合缓存格式
                # mootdx 返回: date, open, high, low, close, volume, amount
                # 确保列名正确
                if "date" not in df.columns:
                    print("  ❌ 数据缺少 date 列，跳过")
                    results[code] = False
                    continue

                # 写入缓存（会自动合并去重）
                cache.save_kline(code, df)

                # 验证写入
                df_cached = cache.get_kline(code)
                new_rows = len(df_cached) if df_cached is not None else 0
                print(f"  ✅ 缓存写入成功: {new_rows} 行")

                results[code] = True
            else:
                print(f"  ❌ fallback 返回空数据")
                results[code] = False
        except Exception as e:
            logger.error(f"补全 {code} 失败: {e}")
            print(f"  ❌ 异常: {e}")
            results[code] = False

    return results


def main():
    print("\n" + "🐴🐴🐴 回测数据补全脚本 🐴🐴🐴")
    print(f"目标股票: {', '.join(TARGET_CODES)}")
    print(f"最低要求: 每只 {MIN_ROWS} 行日K线数据\n")

    # Step 1: 诊断
    diagnosis = diagnose()

    # Step 2: 补全
    results = fill_gaps(diagnosis)

    # Step 3: 再次诊断验证
    print("\n\n--- 补全后再次验证 ---")
    final = diagnose()

    # Step 4: 汇总
    insufficient_after = sum(1 for d in final.values() if d["rows"] < MIN_ROWS)
    if insufficient_after > 0:
        still_needy = [code for code, d in final.items() if d["rows"] < MIN_ROWS]
        print(f"\n⚠️ 仍有 {len(still_needy)} 只股票数据不足: {still_needy}")
        for code in still_needy:
            print(f"  {code}: 当前 {final[code]['rows']} 行, 日期范围: {final[code]['date_range']}")
    else:
        print("\n🎉 所有目标股票数据充足！可以开始回测。")

    print("\n✅ 补全任务完成。")
    return 0 if insufficient_after == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
