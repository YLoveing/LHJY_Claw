# -*- coding: utf-8 -*-
"""共享 pytest fixtures — mock 数据提供器、通知服务等"""

import sys
import os
from pathlib import Path
from datetime import date, datetime
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
import pandas as pd
import numpy as np

# 确保项目根目录在 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ═══════════════════════════════════════════
# Mock 数据工厂
# ═══════════════════════════════════════════

class MockDailyBar:
    """模拟 OHLC 日线 Bar，实现 DailyBarLike Protocol"""

    def __init__(
        self,
        dt: Optional[date] = None,
        high: Optional[float] = None,
        low: Optional[float] = None,
        close: Optional[float] = None,
    ):
        self.date = dt or date(2025, 1, 1)
        self.high = high
        self.low = low
        self.close = close


class MockBacktestResult:
    """模拟回测结果对象，实现 BacktestResultLike Protocol"""

    def __init__(
        self,
        eval_status: str = "completed",
        position_recommendation: Optional[str] = "long",
        outcome: Optional[str] = "win",
        direction_correct: Optional[bool] = True,
        stock_return_pct: Optional[float] = None,
        simulated_return_pct: Optional[float] = None,
        hit_stop_loss: Optional[bool] = None,
        hit_take_profit: Optional[bool] = None,
        first_hit: Optional[str] = "neither",
        first_hit_trading_days: Optional[int] = None,
        operation_advice: Optional[str] = "买入",
    ):
        self.eval_status = eval_status
        self.position_recommendation = position_recommendation
        self.outcome = outcome
        self.direction_correct = direction_correct
        self.stock_return_pct = stock_return_pct
        self.simulated_return_pct = simulated_return_pct
        self.hit_stop_loss = hit_stop_loss
        self.hit_take_profit = hit_take_profit
        self.first_hit = first_hit
        self.first_hit_trading_days = first_hit_trading_days
        self.operation_advice = operation_advice


# ═══════════════════════════════════════════
# 通用 Fixtures
# ═══════════════════════════════════════════

@pytest.fixture
def sample_date() -> date:
    """基准日期 fixture"""
    return date(2025, 6, 9)


@pytest.fixture
def forward_bars_uptrend() -> List[MockDailyBar]:
    """模拟 20 天上升趋势 Bar 序列"""
    base = 10.0
    bars = []
    for i in range(20):
        d = date(2025, 6, 10 + i)
        close = base + i * 0.1
        bars.append(MockDailyBar(
            dt=d,
            high=close + 0.05,
            low=close - 0.05,
            close=close,
        ))
    return bars


@pytest.fixture
def forward_bars_downtrend() -> List[MockDailyBar]:
    """模拟 20 天下跌趋势 Bar 序列"""
    base = 10.0
    bars = []
    for i in range(20):
        d = date(2025, 6, 10 + i)
        close = base - i * 0.1
        bars.append(MockDailyBar(
            dt=d,
            high=close + 0.05,
            low=close - 0.05,
            close=close,
        ))
    return bars


@pytest.fixture
def forward_bars_flat() -> List[MockDailyBar]:
    """模拟 20 天震荡横盘 Bar 序列"""
    bars = []
    for i in range(20):
        d = date(2025, 6, 10 + i)
        close = 10.0 + np.sin(i * 0.3) * 0.1
        bars.append(MockDailyBar(
            dt=d,
            high=close + 0.03,
            low=close - 0.03,
            close=close,
        ))
    return bars


@pytest.fixture
def forward_bars_with_stop_loss() -> List[MockDailyBar]:
    """模拟触及止损的 Bar 序列（第5天跌破8.0）"""
    bars = []
    base = 10.0
    for i in range(20):
        d = date(2025, 6, 10 + i)
        if i < 4:
            close = base - i * 0.1
        elif i == 4:
            close = 7.5  # 触止损
        else:
            close = 8.5 + i * 0.05
        bars.append(MockDailyBar(
            dt=d,
            high=close + 0.1,
            low=close - 0.15,
            close=close,
        ))
    return bars


@pytest.fixture
def forward_bars_with_take_profit() -> List[MockDailyBar]:
    """模拟触及止盈的 Bar 序列（第8天涨破12.0）"""
    bars = []
    base = 10.0
    for i in range(20):
        d = date(2025, 6, 10 + i)
        if i < 7:
            close = base + i * 0.2
        elif i == 7:
            close = 12.5  # 触止盈
        else:
            close = 12.0 - i * 0.05
        bars.append(MockDailyBar(
            dt=d,
            high=close + 0.15,
            low=close - 0.1,
            close=close,
        ))
    return bars


@pytest.fixture
def short_forward_bars() -> List[MockDailyBar]:
    """模拟不足 10 天的 Bar 序列（模拟 insufficient_data）"""
    bars = []
    for i in range(5):
        d = date(2025, 6, 10 + i)
        close = 10.0 + i * 0.1
        bars.append(MockDailyBar(dt=d, high=close + 0.05, low=close - 0.05, close=close))
    return bars


# ═══════════════════════════════════════════
# market_filter fixtures
# ═══════════════════════════════════════════

@pytest.fixture
def mock_hs300_df() -> pd.DataFrame:
    """模拟沪深300指数 200 天数据（多头排列）"""
    dates = pd.date_range(start="2025-01-01", periods=200, freq="B")
    n = 200
    rng = np.random.default_rng(42)
    closes = 3800 + np.cumsum(rng.normal(0.05, 1.5, n))
    closes = np.maximum(closes, 2000)

    df = pd.DataFrame({
        "date": dates[:len(closes)],
        "close": closes[:len(closes)],
    })
    return df


@pytest.fixture
def mock_hs300_bearish_df() -> pd.DataFrame:
    """模拟沪深300指数 200 天数据（空头排列，持续下跌）"""
    dates = pd.date_range(start="2025-01-01", periods=200, freq="B")
    n = 200
    rng = np.random.default_rng(99)
    closes = 3800 - np.cumsum(np.abs(rng.normal(0.5, 2.0, n)))
    closes = np.maximum(closes, 1500)

    df = pd.DataFrame({
        "date": dates[:len(closes)],
        "close": closes[:len(closes)],
    })
    return df


# ═══════════════════════════════════════════
# Data source fixtures
# ═══════════════════════════════════════════

@pytest.fixture
def mock_daily_kline_df() -> pd.DataFrame:
    """模拟标准化K线数据"""
    dates = pd.date_range(start="2025-01-01", periods=60, freq="B")
    n = len(dates)
    rng = np.random.default_rng(42)
    close = 10 + np.cumsum(rng.normal(0, 0.2, n))

    df = pd.DataFrame({
        "date": dates,
        "open": close - rng.uniform(-0.1, 0.1, n),
        "high": close + rng.uniform(0, 0.15, n),
        "low": close - rng.uniform(0, 0.15, n),
        "close": close,
        "volume": rng.integers(100000, 1000000, n),
        "amount": close * rng.integers(100000, 1000000, n) * 100,
        "pct_chg": rng.normal(0, 1.5, n),
    })
    return df


@pytest.fixture
def mock_quote_data() -> Dict[str, Any]:
    """模拟实时行情数据"""
    return {
        "code": "000001",
        "name": "平安银行",
        "current": 12.50,
        "change": 0.25,
        "change_pct": 2.04,
        "open": 12.30,
        "high": 12.60,
        "low": 12.25,
        "prev_close": 12.25,
        "volume": 50000000.0,
        "amount": 625000000.0,
        "amplitude": 2.86,
    }


# ═══════════════════════════════════════════
# Notification / Search mock fixtures
# ═══════════════════════════════════════════

@pytest.fixture
def mock_notifier():
    """模拟通知服务"""
    notifier = MagicMock()
    notifier.send.return_value = True
    return notifier


@pytest.fixture
def mock_search_service():
    """模拟搜索服务"""
    svc = MagicMock()
    svc.search_stock_news.return_value = None
    return svc


# ═══════════════════════════════════════════
# Market overview fixtures
# ═══════════════════════════════════════════

@pytest.fixture
def mock_market_index_dict() -> Dict[str, Any]:
    """模拟指数行情字典"""
    return {
        "code": "sh000001",
        "name": "上证指数",
        "current": 3350.50,
        "change": 15.30,
        "change_pct": 0.46,
        "open": 3340.0,
        "high": 3360.0,
        "low": 3335.0,
        "prev_close": 3335.20,
        "volume": 15000000000.0,
        "amount": 280000000000.0,
        "amplitude": 0.75,
    }


@pytest.fixture
def mock_market_stats_dict() -> Dict[str, Any]:
    """模拟市场涨跌统计"""
    return {
        "up_count": 2500,
        "down_count": 1800,
        "flat_count": 300,
        "limit_up_count": 45,
        "limit_down_count": 12,
        "total_amount": 8900.0,
    }


# ═══════════════════════════════════════════
# HMM / price extraction fixtures
# ═══════════════════════════════════════════

@pytest.fixture
def mock_price_series() -> Dict[str, List[Tuple[str, float]]]:
    """模拟多只股票的价格序列（50天）"""
    dates = [f"202505{str(d).zfill(2)}" for d in range(1, 32)]
    rng = np.random.default_rng(123)
    prices: Dict[str, List[Tuple[str, float]]] = {}
    for code in ["000001", "000002", "600001"]:
        base = rng.uniform(5, 50)
        series = []
        for i, d in enumerate(dates):
            ret = rng.normal(0.001, 0.015)
            base *= (1 + ret)
            series.append((d, round(float(base), 2)))
        prices[code] = series
    return prices
