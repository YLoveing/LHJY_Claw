"""
接口定义 — typing.Protocol

使用 Protocol 而非 ABC 实现接口隔离，
上层只需要知道方法签名，无需耦合具体实现。

Protocol 优点：
1. 结构子类型（structural subtyping）：无需继承即可被视为实现了接口
2. 轻量：不需要 from abc import ABC, abstractmethod
3. 测试友好：Mock 对象只要方法签名匹配即可
"""

from typing import Dict, List, Optional, Protocol

import pandas as pd

from .models import (
    ChipDistribution,
    FundamentalContext,
    KLineRequest,
    MarketOverview,
    RealtimeQuote,
)


class DataProtocol(Protocol):
    """数据层统一获取接口。

    适用于：decision_layer / news_layer / backtest_layer 等上层使用者。
    """

    def get_kline(self, req: KLineRequest) -> pd.DataFrame:
        """获取 K 线数据，返回标准化的 DataFrame。
        Columns: date, open, high, low, close, volume, amount, pct_chg

        支持多 frequency 参数通过 KLineRequest.frequency 传递：
        - "daily": 日线（默认）
        - "weekly": 周线
        - "monthly": 月线
        """
        ...

    def get_realtime_quote(self, code: str) -> Optional[RealtimeQuote]:
        """获取实时行情。"""
        ...

    def get_chip_distribution(self, code: str) -> Optional[ChipDistribution]:
        """获取筹码分布。"""
        ...

    def get_fundamental_context(self, code: str) -> FundamentalContext:
        """获取基本面上下文。"""
        ...

    def get_market_overview(self, region: str = "cn") -> MarketOverview:
        """获取大盘概览。"""
        ...

    def prefetch_quotes(self, codes: List[str]) -> int:
        """批量预取行情，返回成功预取的数量。"""
        ...

    def get_stock_name(self, code: str) -> str:
        """根据代码获取股票名称。"""
        ...


class CacheProtocol(Protocol):
    """缓存接口。

    适用于：DataProvider 的实现，以及上层手动操作缓存。
    """

    def get_kline(self, code: str, frequency: str = "daily") -> Optional[pd.DataFrame]:
        """读取缓存的 K 线数据。无缓存时返回 None。

        Args:
            code: 股票代码
            frequency: 频率 daily/weekly/monthly
        """
        ...

    def save_kline(self, code: str, df: pd.DataFrame, frequency: str = "daily") -> None:
        """保存 K 线数据到缓存（增量追加 + 去重）。

        Args:
            code: 股票代码
            df: K 线 DataFrame
            frequency: 频率 daily/weekly/monthly
        """
        ...

    def has_kline(self, code: str, frequency: str = "daily") -> bool:
        """检查是否有 K 线缓存。"""
        ...

    def invalidate(self, code: str, frequency: str = "daily") -> None:
        """使指定股票的缓存失效。"""
        ...

    def stats(self) -> Dict[str, object]:
        """返回缓存统计信息。"""
        ...


class FetcherProtocol(Protocol):
    """单个数据源 fetcher 接口。

    适用于：fetchers/ 目录下的各数据源实现（akshare, efinance, pytdx ...）
    DataProvider 通过组合模式持有多个 Fetcher 并按优先级切换。
    """

    name: str
    priority: int

    def get_kline(self, req: KLineRequest) -> pd.DataFrame:
        """获取 K 线数据。"""
        ...

    def get_realtime_quote(self, code: str) -> Optional[RealtimeQuote]:
        """获取实时行情。"""
        ...

    def get_chip_distribution(self, code: str) -> Optional[ChipDistribution]:
        """获取筹码分布。未实现时返回 None。"""
        ...

    def is_available(self) -> bool:
        """检查该数据源当前是否可用。"""
        ...
