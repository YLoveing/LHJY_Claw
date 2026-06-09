#!/usr/bin/env python3
"""
验证脚本：测试 layers/data_layer 模块的数据获取链路。

测试内容：
1. 基础导入
2. 创建 DataCache + DataProvider
3. 注册 AkshareFetcher
4. 获取 K 线（日线/周线/月线）
5. 获取实时行情
6. 获取筹码分布
7. 获取大盘概览
8. 缓存命中

用法：
  cd /opt/daily_stock_analysis
  python test_data_layer.py

注意：需要网络连接且 akshare 已安装。
"""

import logging
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)

# 抑制过于冗长的日志
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("akshare").setLevel(logging.WARNING)
logging.getLogger("layers.data_layer.cache").setLevel(logging.DEBUG)
logging.getLogger("layers.data_layer.provider").setLevel(logging.DEBUG)

logger = logging.getLogger("test")

PASS = 0
FAIL = 0


def check(description: str, condition: bool):
    global PASS, FAIL
    if condition:
        logger.info(f"  [PASS] {description}")
        PASS += 1
    else:
        logger.error(f"  [FAIL] {description}")
        FAIL += 1


def section(title: str):
    logger.info("")
    logger.info("=" * 60)
    logger.info(f"  {title}")
    logger.info("=" * 60)


def test_kline(provider, code: str, label: str = "A 股"):
    """测试 K 线获取"""
    from layers.data_layer.models import KLineRequest

    # 日线
    section(f"K 线 - {label} ({code}) 日线")
    df = provider.get_kline(KLineRequest(code=code, days=30))
    check(f"{code} 日线返回 DataFrame", df is not None)
    if df is not None:
        check(f"{code} 日线非空", not df.empty)
        if not df.empty:
            check(f"{code} 日线含标准列", {"date", "open", "high", "low", "close", "volume"}.issubset(df.columns))
            check(f"{code} 日线 {len(df)} 行", len(df) >= 5)
            logger.info(f"  {code} 日线: {df[['date','close']].tail(3).to_string(index=False)}")
            # 验证缓存命中
            logger.info("  [缓存验证] 第二次请求应命中缓存...")
            t0 = time.time()
            df2 = provider.get_kline(KLineRequest(code=code, days=30))
            elapsed = time.time() - t0
            check(f"{code} 日线缓存命中 (耗时 {elapsed:.3f}s)", elapsed < 1.0 and df2 is not None and not df2.empty)

    # 周线
    section(f"K 线 - {label} ({code}) 周线")
    df_w = provider.get_kline(KLineRequest(code=code, days=120, frequency="weekly"))
    check(f"{code} 周线返回 DataFrame", df_w is not None)
    if df_w is not None and not df_w.empty:
        check(f"{code} 周线含标准列", {"date", "open", "high", "low", "close", "volume"}.issubset(df_w.columns))
        check(f"{code} 周线 {len(df_w)} 行", len(df_w) >= 2)
        logger.info(f"  {code} 周线: {df_w[['date','close']].tail(3).to_string(index=False)}")

    # 月线
    section(f"K 线 - {label} ({code}) 月线")
    df_m = provider.get_kline(KLineRequest(code=code, days=365, frequency="monthly"))
    check(f"{code} 月线返回 DataFrame", df_m is not None)
    if df_m is not None and not df_m.empty:
        check(f"{code} 月线含标准列", {"date", "open", "high", "low", "close", "volume"}.issubset(df_m.columns))
        check(f"{code} 月线 {len(df_m)} 行", len(df_m) >= 2)
        logger.info(f"  {code} 月线: {df_m[['date','close']].tail(3).to_string(index=False)}")


def test_realtime(provider, code: str, label: str = "A 股"):
    """测试实时行情"""
    section(f"实时行情 - {label} ({code})")
    quote = provider.get_realtime_quote(code)
    check(f"{code} 实时行情返回", quote is not None)
    if quote is not None:
        check(f"{code} 有名称 '{quote.name}'", bool(quote.name))
        check(f"{code} 有价格", quote.price is not None and quote.price > 0)
        logger.info(
            f"  {code} {quote.name}: 价格={quote.price}, 涨跌幅={quote.change_pct}%, "
            f"量比={quote.volume_ratio}, 换手率={quote.turnover_rate}%"
        )


def test_chip(provider, code: str):
    """测试筹码分布"""
    section(f"筹码分布 - {code}")
    chip = provider.get_chip_distribution(code)
    if chip is not None:
        check(f"{code} 筹码分布有获利比例", chip.profit_ratio > 0)
        check(f"{code} 筹码分布有平均成本", chip.avg_cost > 0)
        logger.info(
            f"  {code} 筹码: 获利比例={chip.profit_ratio:.1%}, "
            f"平均成本={chip.avg_cost:.2f}, 90%集中度={chip.concentration_90:.2%}"
        )
    else:
        logger.info(f"  {code} 筹码分布: 无数据（对于 ETF/指数/美港股属正常）")


def test_fundamental(provider, code: str, label: str = "A 股"):
    """测试基本面"""
    section(f"基本面 - {label} ({code})")
    ctx = provider.get_fundamental_context(code)
    check(f"{code} 基本面返回", ctx is not None)
    if ctx is not None:
        check(f"{code} 基本面对应状态", ctx.status in ("ok", "partial", "failed", "not_supported"))
        logger.info(
            f"  {code} 基本面: state={ctx.status}, "
            f"pe={ctx.pe_ttm}, pb={ctx.pb}, roe={ctx.roe}, "
            f"market_cap={ctx.market_cap}"
        )


def test_market_overview(provider):
    """测试大盘概览"""
    section("大盘概览")
    mv = provider.get_market_overview()
    check("大盘概览返回", mv is not None)
    if mv is not None:
        check(f"有指数数据 ({len(mv.indices)} 个)", len(mv.indices) > 0)
        logger.info(f"  指数: {[i.get('name','') for i in mv.indices[:3]]}")
        logger.info(f"  涨跌: {mv.up_count}涨 / {mv.down_count}跌 / {mv.flat_count}平")


def test_cache_stats(provider):
    """测试缓存统计"""
    section("缓存统计")
    if hasattr(provider, "_cache") and provider._cache:
        stats = provider._cache.stats()
        logger.info(f"  缓存统计: {stats}")
        check("缓存统计返回字典", isinstance(stats, dict))
    else:
        logger.info("  未启用缓存")


def main():
    logger.info("=" * 60)
    logger.info("  data_layer 验证脚本")
    logger.info("=" * 60)

    # ── 1. 基础导入 ──
    section("1. 模块导入")
    try:
        from layers.data_layer import (
            ChipDistribution,
            DataCache,
            DataProvider,
            FundamentalContext,
            KLineRequest,
            RealtimeQuote,
        )
        from layers.data_layer.fetchers import AkshareFetcher

        check("DataCache 可导入", True)
        check("DataProvider 可导入", True)
        check("KLineRequest 可导入", True)
        check("RealtimeQuote 可导入", True)
        check("ChipDistribution 可导入", True)
        check("FundamentalContext 可导入", True)
        check("AkshareFetcher 可导入", True)
    except ImportError as e:
        logger.error(f"导入失败: {e}")
        check("模块导入", False)
        return

    # ── 2. 创建实例 ──
    section("2. 创建实例")
    cache = DataCache()
    provider = DataProvider(cache=cache)
    fetcher = AkshareFetcher()
    check("DataCache 创建成功", True)
    check("DataProvider 创建成功", True)
    check("AkshareFetcher 创建成功", True)

    # ── 3. 注册 fetcher ──
    section("3. 注册 Fetcher")
    provider.register_fetcher(fetcher)
    check("Fetcher 已注册", len(provider._fetchers) == 1)
    check(f"Fetcher 名称: {fetcher.name}", fetcher.name == "AkshareFetcher")
    check(f"Fetcher 优先级: {fetcher.priority}", fetcher.priority == 1)
    check("Fetcher 可用", fetcher.is_available())

    # ── 4. A 股测试 ──
    section("4. A 股测试 (600519 贵州茅台)")
    test_kline(provider, "600519", "A 股")
    test_realtime(provider, "600519", "A 股")
    test_chip(provider, "600519")
    test_fundamental(provider, "600519", "A 股")

    # ── 5. ETF 测试 ──
    section("5. ETF 测试 (512400 有色龙头ETF)")
    test_kline(provider, "512400", "ETF")
    test_realtime(provider, "512400", "ETF")

    # ── 6. 港股测试 ──
    section("6. 港股测试 (00700 腾讯控股)")
    test_kline(provider, "00700", "港股")
    test_realtime(provider, "00700", "港股")

    # ── 7. 大盘概览 ──
    section("7. 大盘概览")
    test_market_overview(provider)

    # ── 8. 缓存统计 ──
    test_cache_stats(provider)

    # ── 9. 总结 ──
    section("测试总结")
    total = PASS + FAIL
    logger.info(f"  通过: {PASS}/{total}")
    logger.info(f"  失败: {FAIL}/{total}")
    if FAIL == 0:
        logger.info("  ==== 全部通过 ====")
    else:
        logger.warning(f"  存在 {FAIL} 项失败，请检查日志")
    return FAIL


if __name__ == "__main__":
    sys.exit(main())
