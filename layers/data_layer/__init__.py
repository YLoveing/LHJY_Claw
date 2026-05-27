"""
data_layer — 数据获取 + 缓存

统一的数据获取入口（K 线、实时行情、基本面、筹码分布），
多数据源故障切换，磁盘/内存两级缓存。
"""

from .models import (
    StockData,
    KLineData,
    KLineRequest,
    RealtimeQuote,
    ChipDistribution,
    FundamentalContext,
    MarketOverview,
    safe_float,
    safe_int,
)
from .definition import (
    DataProtocol,
    CacheProtocol,
    FetcherProtocol,
)
from .cache import DataCache
from .provider import DataProvider

__all__ = [
    # models
    "StockData",
    "KLineData",
    "KLineRequest",
    "RealtimeQuote",
    "ChipDistribution",
    "FundamentalContext",
    "MarketOverview",
    "safe_float",
    "safe_int",
    # interfaces
    "DataProtocol",
    "CacheProtocol",
    "FetcherProtocol",
    # impl
    "DataCache",
    "DataProvider",
]
