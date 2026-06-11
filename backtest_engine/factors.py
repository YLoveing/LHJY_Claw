"""
多因子计算模块 — 全部从 OHLCV 算出，无需外部数据。

所有因子返回 float 或 np.nan。nan = 数据不足无法计算。

通用安全约定: 用 (closes[1:] - closes[:-1]) / closes[:-1] 算收益率，
避免了 numpy 因边界对齐错误导致的 broadcast 问题。
"""

import numpy as np
import pandas as pd

EPS = 1e-10  # 防除零


def _returns(closes: np.ndarray) -> np.ndarray:
    """安全计算日收益率序列"""
    if len(closes) < 2:
        return np.array([np.nan])
    return (closes[1:] - closes[:-1]) / np.maximum(closes[:-1], EPS)


# ========== 动量因子 ==========


def mom_1m(closes: np.ndarray) -> float:
    """1个月动量 (≈21个交易日)"""
    if len(closes) < 22:
        return np.nan
    return closes[-1] / closes[-22] - 1


def mom_3m(closes: np.ndarray) -> float:
    """3个月动量 (≈63个交易日)"""
    if len(closes) < 64:
        return np.nan
    return closes[-1] / closes[-64] - 1


def mom_6m(closes: np.ndarray) -> float:
    """6个月动量 (≈126个交易日)"""
    if len(closes) < 127:
        return np.nan
    return closes[-1] / closes[-127] - 1


# ========== 波动率因子 ==========


def volatility_20d(closes: np.ndarray) -> float:
    """20日年化波动率"""
    if len(closes) < 22:
        return np.nan
    rets = _returns(closes[-22:])
    return float(np.std(rets) * np.sqrt(252))


def volatility_60d(closes: np.ndarray) -> float:
    """60日年化波动率"""
    if len(closes) < 62:
        return np.nan
    rets = _returns(closes[-62:])
    return float(np.std(rets) * np.sqrt(252))


def max_drawdown_60d(closes: np.ndarray) -> float:
    """60日最大回撤"""
    if len(closes) < 61:
        return np.nan
    window = closes[-61:]
    peak = np.maximum.accumulate(window)
    dd = (window - peak) / np.maximum(peak, EPS)
    return float(np.min(dd))


# ========== 趋势因子 ==========


def price_to_ma20(closes: np.ndarray) -> float:
    """价格 / MA20 (价格在均线上的百分比偏离)"""
    if len(closes) < 20:
        return np.nan
    ma20 = np.mean(closes[-20:])
    if ma20 == 0:
        return np.nan
    return closes[-1] / ma20 - 1


def price_to_ma60(closes: np.ndarray) -> float:
    """价格 / MA60"""
    if len(closes) < 60:
        return np.nan
    ma60 = np.mean(closes[-60:])
    if ma60 == 0:
        return np.nan
    return closes[-1] / ma60 - 1


def short_long_ma_ratio(closes: np.ndarray) -> float:
    """MA5 / MA20 比值 (短期均线相对长期均线的位置)"""
    if len(closes) < 20:
        return np.nan
    ma5 = np.mean(closes[-5:])
    ma20 = np.mean(closes[-20:])
    if ma20 == 0:
        return np.nan
    return ma5 / ma20 - 1


# ========== 量能因子 ==========


def volume_ratio(volumes: np.ndarray) -> float:
    """当前量 / MA5量 (放量程度)"""
    if len(volumes) < 6:
        return np.nan
    ma5_vol = np.mean(volumes[-6:-1])
    if ma5_vol == 0:
        return np.nan
    return volumes[-1] / ma5_vol


def volume_trend(volumes: np.ndarray) -> float:
    """量能趋势: MA5 / MA20 量比 (量能是否持续放大)"""
    if len(volumes) < 21:
        return np.nan
    ma5 = np.mean(volumes[-5:])
    ma20 = np.mean(volumes[-20:])
    if ma20 == 0:
        return np.nan
    return ma5 / ma20 - 1


# ========== 反转因子 ==========


def rsi_14(closes: np.ndarray) -> float:
    """14日RSI"""
    if len(closes) < 15:
        return np.nan
    rets = _returns(closes[-15:])
    avg_gain = np.mean(np.maximum(rets, 0))
    avg_loss = np.mean(np.maximum(-rets, 0))
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - 100 / (1 + rs))


def rsi_6(closes: np.ndarray) -> float:
    """6日RSI"""
    if len(closes) < 7:
        return np.nan
    rets = _returns(closes[-7:])
    avg_gain = np.mean(np.maximum(rets, 0))
    avg_loss = np.mean(np.maximum(-rets, 0))
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - 100 / (1 + rs))


def close_to_low_20(closes: np.ndarray, lows: np.ndarray) -> float:
    """当前价格在20日高低区间中的位置 (0~1, 0=在最低点, 1=在最高点)"""
    if len(closes) < 20 or len(lows) < 20:
        return np.nan
    h = np.max(closes[-20:])
    l = np.min(lows[-20:])
    if h == l:
        return 0.5
    return (closes[-1] - l) / (h - l)


# ========== 复合因子 ==========

# ========== 标准化工具（单一事实来源，回测/生产共用） ==========


def zscore_cross_section(series: np.ndarray) -> np.ndarray:
    """
    横截面 z-score 标准化，nan-safe。
    与 engine._zscore_day 逻辑一致，供回测引擎和生产流水线共用。
    """
    mu = np.nanmean(series)
    sigma = np.nanstd(series)
    if np.isnan(mu) or sigma is None or sigma < 1e-10:
        return np.zeros_like(series)
    return (series - mu) / sigma


def zscore_cross_section_pandas(col: pd.Series) -> pd.Series:
    """pandas Series 版本的横截面 z-score，委托 numpy 版实现。"""
    arr = zscore_cross_section(col.values)
    return pd.Series(arr, index=col.index)


def score_weighted(row: pd.Series, weights: dict, cols: list) -> float:
    """加权打分，nan-safe。与 BacktestEngine._score_row 等价。"""
    s = 0.0
    n = 0
    for col in cols:
        v = row.get(col)
        if v is not None and not (isinstance(v, float) and np.isnan(v)):
            s += v * weights[col]
            n += 1
    return s / max(n, 1)


def all_factors(
    closes: np.ndarray,
    volumes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
) -> dict:
    """计算所有因子，返回 dict"""
    return {
        "mom_1m": mom_1m(closes),
        "mom_3m": mom_3m(closes),
        "mom_6m": mom_6m(closes),
        "volatility_20d": volatility_20d(closes),
        "volatility_60d": volatility_60d(closes),
        "max_dd_60d": max_drawdown_60d(closes),
        "price_ma20": price_to_ma20(closes),
        "price_ma60": price_to_ma60(closes),
        "short_long_ma": short_long_ma_ratio(closes),
        "volume_ratio": volume_ratio(volumes),
        "volume_trend": volume_trend(volumes),
        "rsi_14": rsi_14(closes),
        "rsi_6": rsi_6(closes),
        "close_low_20": close_to_low_20(closes, lows),
    }
