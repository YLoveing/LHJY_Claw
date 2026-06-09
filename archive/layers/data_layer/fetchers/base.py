"""
fetcher 基类 + 注册机制

设计模式：模板方法 (Template Method Pattern)
- BaseFetcher: 抽象基类，定义统一接口骨架
- FetcherRegistry: 全局注册器，管理所有 fetcher 类

与现有 data_provider/base.py 中 BaseFetcher 的区别：
1. 适配新定义的 FetcherProtocol
2. 从 ABC 改为混入 Protocol 的结构子类型
3. 保留关键抽象方法：_fetch_raw_kline / _normalize_kline
4. `priority` 和 `name` 为类属性，子类覆盖
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type

import pandas as pd

from ..definition import FetcherProtocol
from ..models import (
    ChipDistribution,
    KLineRequest,
    RealtimeQuote,
    safe_float,
)

logger = logging.getLogger(__name__)

STANDARD_KLINE_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]


class BaseFetcher(ABC):
    """数据源抽象基类 — 同时作为 FetcherProtocol 的结构子类型。

    子类必须实现：
    - _fetch_raw_kline(stock_code, start_date, end_date) -> pd.DataFrame
    - _normalize_kline(raw_df, stock_code) -> pd.DataFrame

    可选实现：
    - get_realtime_quote(code) -> Optional[RealtimeQuote]
    - get_chip_distribution(code) -> Optional[ChipDistribution]
    - is_available() -> bool

    属性（子类覆盖）：
    - name: str          — 用于日志和调试
    - priority: int      — 越小越优先
    """

    name: str = "BaseFetcher"
    priority: int = 99

    # ──────────── K 线（子类必须实现）────────────

    @abstractmethod
    def _fetch_raw_kline(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """从数据源获取原始 K 线数据（子类必须实现）。"""
        ...

    @abstractmethod
    def _normalize_kline(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """将原始 K 线数据标准化为标准列名（子类必须实现）。

        输入：原始数据源的列名
        输出：['date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']
        """
        ...

    # ──────────── 公共 K 线入口 ────────────

    def get_kline(self, req: KLineRequest) -> pd.DataFrame:
        """获取日 K 线数据。

        实现了 FetcherProtocol.get_kline。
        子类一般不需要覆盖此方法。
        """
        import math

        end_date = req.end_date
        if end_date is None:
            from datetime import datetime

            end_date = datetime.now().strftime("%Y-%m-%d")

        start_date = req.start_date
        if start_date is None:
            from datetime import datetime, timedelta

            start_dt = datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=req.days * 2)
            start_date = start_dt.strftime("%Y-%m-%d")

        raw_df = self._fetch_raw_kline(req.code, start_date, end_date)
        if raw_df is None or raw_df.empty:
            return pd.DataFrame()

        df = self._normalize_kline(raw_df, req.code)
        df = self._clean_kline(df)
        df = self._calculate_indicators(df)
        return df

    def _clean_kline(self, df: pd.DataFrame) -> pd.DataFrame:
        """数据清洗：类型转换 + 去空 + 排序。"""
        df = df.copy()
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
        numeric_cols = ["open", "high", "low", "close", "volume", "amount", "pct_chg"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close", "volume"])
        df = df.sort_values("date", ascending=True).reset_index(drop=True)
        return df

    def _calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算技术指标（MA, Volume Ratio）。"""
        df = df.copy()
        df["ma5"] = df["close"].rolling(window=5, min_periods=1).mean()
        df["ma10"] = df["close"].rolling(window=10, min_periods=1).mean()
        df["ma20"] = df["close"].rolling(window=20, min_periods=1).mean()
        avg_volume_5 = df["volume"].rolling(window=5, min_periods=1).mean()
        df["volume_ratio"] = df["volume"] / avg_volume_5.shift(1)
        df["volume_ratio"] = df["volume_ratio"].fillna(1.0)
        for col in ["ma5", "ma10", "ma20", "volume_ratio"]:
            if col in df.columns:
                df[col] = df[col].round(2)
        return df

    # ──────────── 实时行情 / 筹码（可选实现）────────────

    def get_realtime_quote(self, code: str) -> Optional[RealtimeQuote]:
        """获取实时行情。默认未实现，子类按需覆盖。"""
        return None

    def get_chip_distribution(self, code: str) -> Optional[ChipDistribution]:
        """获取筹码分布。默认未实现，子类按需覆盖。"""
        return None

    def is_available(self) -> bool:
        """检查数据源是否可用。默认返回 True，子类按需覆盖。"""
        return True


# ═══════════════════════════════════════════
# 注册机制
# ═══════════════════════════════════════════


class FetcherRegistrationError(Exception):
    """Fetcher 注册异常。"""

    pass


class FetcherRegistry:
    """全局 fetcher 注册器。

    管理所有 fetcher 类，用于：
    1. 集中注册和发现
    2. 按配置批量实例化
    3. 单元测试时替换实现

    使用方式：
        FetcherRegistry.register(AkshareFetcher)
        FetcherRegistry.register(EfinanceFetcher)
        fetchers = FetcherRegistry.instantiate_all()  # 返回按 priority 排序的实例列表
    """

    _registry: Dict[str, Type[BaseFetcher]] = {}

    @classmethod
    def register(cls, fetcher_cls: Type[BaseFetcher]) -> None:
        """注册 fetcher 类。"""
        name = getattr(fetcher_cls, "name", fetcher_cls.__name__)
        if name in cls._registry:
            raise FetcherRegistrationError(f"Fetcher 名称冲突: {name} 已被 {cls._registry[name].__name__} 注册")
        cls._registry[name] = fetcher_cls
        logger.info(f"[FetcherRegistry] 已注册: {name} ({fetcher_cls.__name__})")

    @classmethod
    def unregister(cls, name: str) -> None:
        """注销 fetcher 类。"""
        cls._registry.pop(name, None)

    @classmethod
    def get_registered(cls) -> Dict[str, Type[BaseFetcher]]:
        """获取所有已注册的 fetcher 类。"""
        return dict(cls._registry)

    @classmethod
    def instantiate_all(cls, **kwargs: Any) -> List[BaseFetcher]:
        """实例化所有已注册的 fetcher，按 priority 排序返回。"""
        instances = []
        for name, cls_ in cls._registry.items():
            try:
                instance = cls_(**kwargs)
                instances.append(instance)
            except Exception as e:
                logger.error(f"[FetcherRegistry] 实例化 {name} 失败: {e}")
        instances.sort(key=lambda f: getattr(f, "priority", 99))
        return instances

    @classmethod
    def clear(cls) -> None:
        """清空注册表（主要用于测试）。"""
        cls._registry.clear()
