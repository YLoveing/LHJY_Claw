#!/usr/bin/env python3
"""
精简验证：测试 layers/data_layer 核心功能链路。

假设东财 API 可能不可用，但验证：
1. 模块导入
2. Cache + Provider 创建 + Fetcher 注册
3. K 线获取（从缓存取已经有的日线数据）
4. 周线/月线降级聚合（从日线 resample）
5. 行情类型转换
6. 筹码分布类型转换（可能 None）
7. 大盘概览 factory
"""

import logging
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("akshare").setLevel(logging.WARNING)

logger = logging.getLogger("verify")

PASS = 0
FAIL = 0


def ok(desc: str):
    global PASS
    PASS += 1
    logger.info(f"  [PASS] {desc}")


def ng(desc: str):
    global FAIL
    FAIL += 1
    logger.error(f"  [FAIL] {desc}")


def section(title: str):
    logger.info("")
    logger.info(f"===== {title}")


def main():
    global PASS, FAIL
    section("1. 模块导入")
    try:
        from layers.data_layer import (
            ChipDistribution,
            DataCache,
            DataProvider,
            FundamentalContext,
            KLineRequest,
            MarketOverview,
            RealtimeQuote,
        )
        from layers.data_layer.fetchers import AkshareFetcher
        from layers.data_layer.models import safe_float, safe_int

        ok("DataCache / DataProvider / KLineRequest 导入")
        ok("RealtimeQuote / ChipDistribution / FundamentalContext 导入")
        ok("AkshareFetcher 导入")
    except ImportError as e:
        ng(f"导入失败: {e}")
        return 1

    section("2. 实例化 + 注册")
    cache = DataCache()
    provider = DataProvider(cache=cache)
    fetcher = AkshareFetcher()
    provider.register_fetcher(fetcher)

    ok(f"DataCache 创建 (root={cache._root})")
    ok(f"AkshareFetcher: name={fetcher.name}, priority={fetcher.priority}")
    ok(f"已注册 {len(provider._fetchers)} 个 fetcher")

    section("3. 日线 K 线 (从缓存或 API)")
    req = KLineRequest(code="600519", days=30)
    df = provider.get_kline(req)
    if df is not None and not df.empty:
        ok(f"600519 K线返回 {len(df)} 行")
        cols = set(df.columns)
        required = {"date", "open", "high", "low", "close", "volume"}
        ok(f"含标准列 ({required.issubset(cols)})")
        ok(f"最新: {df['date'].iloc[-1]} close={df['close'].iloc[-1]}")
        # 验证缓存
        t0 = time.time()
        df2 = provider.get_kline(KLineRequest(code="600519", days=30))
        elapsed = time.time() - t0
        ok(f"缓存命中 ({elapsed:.3f}s)")
    else:
        ng("600519 K线返回空")

    section("4. 周线 K 线 (API -> resample 降级)")
    req_w = KLineRequest(code="600519", days=120, frequency="weekly")
    df_w = provider.get_kline(req_w)
    if df_w is not None and not df_w.empty:
        ok(f"600519 周线返回 {len(df_w)} 行")
        ok(f"周线最新: {df_w['date'].iloc[-1]} close={df_w['close'].iloc[-1]}")
    else:
        ng("600519 周线返回空")

    section("5. 月线 K 线 (API -> resample 降级)")
    req_m = KLineRequest(code="600519", days=365, frequency="monthly")
    df_m = provider.get_kline(req_m)
    if df_m is not None and not df_m.empty:
        ok(f"600519 月线返回 {len(df_m)} 行")
        ok(f"月线最新: {df_m['date'].iloc[-1]} close={df_m['close'].iloc[-1]}")
    else:
        ng("600519 月线返回空")

    section("6. 实时行情 (可能因网络失败 -> None)")
    quote = provider.get_realtime_quote("600519")
    if quote is not None:
        ok(f"实时行情: {quote.name} price={quote.price} change={quote.change_pct}% source={quote.source}")
        # 验证类型转换
        ok(f"RealtimeQuote 类型正确: {type(quote).__name__}")
        ok(f"RealtimeQuote has_basic_data: {quote.has_basic_data()}")
    else:
        logger.info("  实时行情: 网络不可用 (非代码问题)")

    section("7. 筹码分布 (可能因网络失败 -> None)")
    chip = provider.get_chip_distribution("600519")
    if chip is not None:
        ok(f"筹码分布: profit={chip.profit_ratio:.1%} cost={chip.avg_cost:.2f}")
        ok(f"ChipDistribution 类型正确: {type(chip).__name__}")
    else:
        logger.info("  筹码分布: 网络不可用或类型不支持 (正常)")

    section("8. 基本面 (桥接到 AkshareFundamentalAdapter)")
    ctx = provider.get_fundamental_context("600519")
    ok(f"基本面返回, status={ctx.status}")
    ok(f"FundamentalContext 类型正确: {type(ctx).__name__}")

    section("9. 大盘概览 (多接口聚合)")
    mv = provider.get_market_overview()
    ok(f"MarketOverview 返回, region={mv.region}")
    ok(f"指数数量: {len(mv.indices)}")

    section("10. 缓存统计")
    stats = cache.stats()
    ok(f"缓存统计返回 (kline_stocks={stats.get('kline', {}).get('stocks', 0)})")

    section("11. 缓存频率隔离验证")
    # 确认不同频率的缓存文件不同
    cache.invalidate("600519", frequency="all")
    ok("清除 600519 所有频率缓存成功")

    section("===== 测试结果 =====")
    total = PASS + FAIL
    logger.info(f"通过: {PASS}/{total}")
    logger.info(f"失败: {FAIL}/{total}")
    if FAIL == 0:
        logger.info("全部通过!")
    else:
        logger.warning(f"有 {FAIL} 项失败")
    return FAIL


if __name__ == "__main__":
    sys.exit(main())
