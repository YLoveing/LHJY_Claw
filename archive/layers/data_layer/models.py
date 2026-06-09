"""
数据模型 — 从 data_provider/realtime_types.py 萃取核心类型

设计目标：
1. 统一各数据源的返回结构
2. 独立于具体实现，可被上层安全引用
3. 保持向后兼容（UnifiedRealtimeQuote → RealtimeQuote 别名在 fetcher 层处理）
"""

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

# ============================================
# 类型转换工具函数（源自 realtime_types.py）
# ============================================


def safe_float(val: Any, default: Optional[float] = None) -> Optional[float]:
    """安全转换为浮点数，处理 None / 空 / NaN / 字符串。

    Returns: 转换后的浮点数，或 default。
    """
    try:
        if val is None:
            return default
        if isinstance(val, str):
            val = val.strip()
            if val == "" or val == "-" or val == "--":
                return default
        try:
            if math.isnan(float(val)):
                return default
        except (ValueError, TypeError):
            pass
        return float(val)
    except (ValueError, TypeError):
        return default


def safe_int(val: Any, default: Optional[int] = None) -> Optional[int]:
    """安全转换为整数，先转 float 再取整。"""
    f_val = safe_float(val, default=None)
    if f_val is not None:
        return int(f_val)
    return default


# ============================================
# 基础数据模型
# ============================================


@dataclass
class StockData:
    """股票基础信息 — 精简版，仅用于引用传递。"""

    code: str
    name: str = ""
    market: str = "cn"  # cn / hk / us


@dataclass
class KLineData:
    """单根 K 线数据点。"""

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float = 0.0
    pct_chg: float = 0.0


@dataclass
class KLineRequest:
    """K 线查询参数。

    Args:
        code: 股票代码
        start_date: 开始日期 YYYY-MM-DD，None 则根据 days 推算
        end_date: 结束日期 YYYY-MM-DD，None 则使用当天
        days: 需要最近 N 个交易日（仅当 start_date 为 None 时生效）
        force_refresh: 是否跳过缓存
        frequency: 频率，可选 "daily" / "weekly" / "monthly"
    """

    code: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    days: int = 30
    force_refresh: bool = False
    frequency: str = "daily"  # daily / weekly / monthly


@dataclass
class RealtimeQuote:
    """统一实时行情数据结构（精简版，源自 UnifiedRealtimeQuote）。

    各数据源返回的字段可能不同，缺失字段用 None 表示。
    """

    code: str
    name: str = ""
    source: str = ""

    # 核心价格
    price: Optional[float] = None
    change_pct: Optional[float] = None
    change_amount: Optional[float] = None

    # 量价
    volume: Optional[int] = None
    amount: Optional[float] = None
    volume_ratio: Optional[float] = None
    turnover_rate: Optional[float] = None
    amplitude: Optional[float] = None

    # 价格区间
    open_price: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    pre_close: Optional[float] = None

    # 估值
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None
    total_mv: Optional[float] = None
    circ_mv: Optional[float] = None

    # 其他
    change_60d: Optional[float] = None
    high_52w: Optional[float] = None
    low_52w: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "code": self.code,
            "name": self.name,
            "source": self.source,
        }
        for f in RealtimeQuote.__dataclass_fields__:
            if f in ("code", "name", "source"):
                continue
            val = getattr(self, f, None)
            if val is not None:
                result[f] = val
        return result

    def has_basic_data(self) -> bool:
        return self.price is not None and self.price > 0

    def has_volume_data(self) -> bool:
        return self.volume_ratio is not None or self.turnover_rate is not None


@dataclass
class ChipDistribution:
    """筹码分布数据（源自 realtime_types.py 的 ChipDistribution）。

    反映持仓成本分布和获利情况。
    """

    code: str
    date: str = ""
    source: str = ""

    profit_ratio: float = 0.0
    avg_cost: float = 0.0
    cost_90_low: float = 0.0
    cost_90_high: float = 0.0
    concentration_90: float = 0.0
    cost_70_low: float = 0.0
    cost_70_high: float = 0.0
    concentration_70: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "date": self.date,
            "source": self.source,
            "profit_ratio": self.profit_ratio,
            "avg_cost": self.avg_cost,
            "cost_90_low": self.cost_90_low,
            "cost_90_high": self.cost_90_high,
            "concentration_90": self.concentration_90,
            "concentration_70": self.concentration_70,
        }


@dataclass
class FundamentalContext:
    """基本面上下文（源自 REFACTOR_PLAN.md 的定义 + 现有代码范式）。"""

    code: str
    industry: Optional[str] = None
    market_cap: Optional[float] = None
    pe_ttm: Optional[float] = None
    pb: Optional[float] = None
    roe: Optional[float] = None
    revenue_growth: Optional[float] = None
    profit_growth: Optional[float] = None
    belong_boards: List[dict] = field(default_factory=list)
    source_chain: List[str] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    status: str = "not_supported"  # ok / partial / failed / not_supported


@dataclass
class MarketOverview:
    """大盘概览。"""

    region: str = "cn"
    up_count: int = 0
    down_count: int = 0
    flat_count: int = 0
    limit_up: int = 0
    limit_down: int = 0
    total_amount: float = 0.0
    indices: List[dict] = field(default_factory=list)
    sector_rankings: List[dict] = field(default_factory=list)
