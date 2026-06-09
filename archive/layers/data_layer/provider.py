"""
DataProvider — 数据层轻量调度器

职责：
1. 持有一组 Fetcher（按优先级排序），请求时自动切换
2. 整合 Cache 层：先查缓存，miss 后再问 Fetcher
3. 对外暴露 DataProtocol 接口，供上层（decision_layer 等）使用
4. 熔断器判断源可用性（桥接到 realtime_types 中的全局熔断器）

设计要点：
- 是现有 DataFetcherManager 的轻量替代品，而非完全重写
- 默认使用现有 data_provider 中的 DataCache 实例
- Fetcher 通过 register_fetcher() 注册，由外部调用方（如工厂函数或配置）负责注册顺序
"""

import logging
from typing import Dict, List, Optional

import pandas as pd

from .definition import CacheProtocol, DataProtocol, FetcherProtocol
from .models import (
    ChipDistribution,
    FundamentalContext,
    KLineRequest,
    MarketOverview,
    RealtimeQuote,
)

logger = logging.getLogger(__name__)


class DataProvider(DataProtocol):
    """数据提供器 — 轻量调度器。

    Args:
        cache: 可选的 CacheProtocol 实现。不传则无缓存。
    """

    def __init__(self, cache: Optional[CacheProtocol] = None):
        self._cache = cache
        self._fetchers: List[FetcherProtocol] = []  # 按 priority 升序

    def register_fetcher(self, fetcher: FetcherProtocol) -> None:
        """注册一个数据源 Fetcher。

        优先级数字越小越优先。插入后按 priority 排序。
        可重复调用注册多个。
        """
        self._fetchers.append(fetcher)
        self._fetchers.sort(key=lambda f: f.priority)
        logger.info(
            f"[DataProvider] 已注册 fetcher: {fetcher.name} "
            f"(priority={fetcher.priority}, 当前共{len(self._fetchers)}个)"
        )

    def _find_available(self) -> List[FetcherProtocol]:
        """返回当前可用的 fetcher 列表（按优先级排序）。"""
        return [f for f in self._fetchers if f.is_available()]

    def get_kline(self, req: KLineRequest) -> pd.DataFrame:
        """获取 K 线数据：先查缓存，miss 后轮询 fetcher。

        支持多频率（daily / weekly / monthly）的缓存-获取-缓存流程。
        不同频率使用独立的缓存键（{code}_{freq}.pkl）。
        """
        code = req.code
        freq = req.frequency

        # 先查缓存（除非强制刷新）
        if not req.force_refresh and self._cache is not None:
            cached = self._cache.get_kline(code, frequency=freq)
            if cached is not None:
                logger.debug(f"[DataProvider] 缓存命中: {code} (freq={freq}, {len(cached)}行)")
                return cached

        # 轮询 fetcher
        available = self._find_available()
        if not available:
            raise RuntimeError(f"[DataProvider] 无可用数据源获取 {code} 的 K 线数据 (freq={freq})")

        last_error: Optional[Exception] = None
        for fetcher in available:
            try:
                df = fetcher.get_kline(req)
                if df is not None and not df.empty:
                    # 写入缓存（按频率分离）
                    if self._cache is not None:
                        self._cache.save_kline(code, df, frequency=freq)
                    return df
            except Exception as e:
                last_error = e
                logger.warning(f"[DataProvider] Fetcher {fetcher.name} 获取 {code} " f"K 线失败 (freq={freq}): {e}")
                continue

        raise RuntimeError(f"[DataProvider] 所有数据源获取 {code} K 线均失败 (freq={freq})") from last_error

    def get_realtime_quote(self, code: str) -> Optional[RealtimeQuote]:
        """获取实时行情：轮询 fetcher，返回第一个成功的。"""
        available = self._find_available()
        for fetcher in available:
            try:
                quote = fetcher.get_realtime_quote(code)
                if quote is not None:
                    return quote
            except Exception as e:
                logger.debug(f"[DataProvider] Fetcher {fetcher.name} 获取 {code} 实时行情失败: {e}")
                continue
        return None

    def get_chip_distribution(self, code: str) -> Optional[ChipDistribution]:
        """获取筹码分布。"""
        available = self._find_available()
        for fetcher in available:
            try:
                result = fetcher.get_chip_distribution(code)
                if result is not None:
                    return result
            except Exception as e:
                logger.debug(f"[DataProvider] Fetcher {fetcher.name} 获取 {code} 筹码分布失败: {e}")
                continue
        return None

    def get_fundamental_context(self, code: str) -> FundamentalContext:
        """获取基本面上下文。"""
        available = self._find_available()
        for fetcher in available:
            # 如果 fetcher 没有实现该方法，跳过
            if not hasattr(fetcher, "get_fundamental_context"):
                continue
            try:
                ctx = fetcher.get_fundamental_context(code)  # type: ignore
                if ctx is not None:
                    return ctx
            except Exception as e:
                logger.debug(f"[DataProvider] Fetcher {fetcher.name} 获取 {code} 基本面失败: {e}")
                continue
        return FundamentalContext(code=code, status="failed")

    def get_market_overview(self, region: str = "cn") -> MarketOverview:
        """获取大盘概览。"""
        available = self._find_available()
        for fetcher in available:
            if not hasattr(fetcher, "get_market_overview"):
                continue
            try:
                overview = fetcher.get_market_overview(region)  # type: ignore
                if overview is not None:
                    return overview
            except Exception as e:
                logger.debug(f"[DataProvider] Fetcher {fetcher.name} 获取大盘数据失败: {e}")
                continue
        return MarketOverview(region=region)

    def prefetch_quotes(self, codes: List[str]) -> int:
        """批量预取行情，返回成功预取数量。"""
        available = self._find_available()
        if not available:
            return 0
        fetcher = available[0]
        if not hasattr(fetcher, "prefetch_quotes"):
            return 0
        try:
            return fetcher.prefetch_quotes(codes)  # type: ignore
        except Exception as e:
            logger.warning(f"[DataProvider] 预取行情失败: {e}")
            return 0

    def get_stock_name(self, code: str) -> str:
        """获取股票名称。"""
        available = self._find_available()
        for fetcher in available:
            if not hasattr(fetcher, "get_stock_name"):
                continue
            try:
                name = fetcher.get_stock_name(code)  # type: ignore
                if name:
                    return name
            except Exception as e:
                logger.debug(f"[DataProvider] Fetcher {fetcher.name} 获取 {code} 名称失败: {e}")
                continue
        return ""
