"""
fetchers/ -- 数据源 fetcher 集合

各数据源的具体实现：
- base.py: Fetcher 基类 + 注册机制
- akshare.py: Akshare 数据源（已完成，主数据源）
- efinance_fetcher.py (待实现)
- pytdx_fetcher.py (待实现)
- ...

注册流程：
  from layers.data_layer.fetchers import AkshareFetcher
  fetcher = AkshareFetcher()
  provider.register_fetcher(fetcher)

或者通过 FetcherRegistry 批量注册（当前已有 AkshareFetcher、EfinanceFetcher、PytdxFetcher）。
"""

from .base import BaseFetcher, FetcherRegistry, FetcherRegistrationError
from .akshare import AkshareFetcher

__all__ = [
    "BaseFetcher",
    "FetcherRegistry",
    "FetcherRegistrationError",
    "AkshareFetcher",
]
