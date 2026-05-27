#!/usr/bin/env python3
"""
大盘均线过滤模块 — 基于沪深300指数技术状态的仓位管理
====================================================
规则（不依赖账户权益曲线，抗过拟合）：
  沪深300指数在MA60上方     → 满仓 (1.0)
  跌破MA60但在MA120上方    → 半仓 (0.5)
  跌破MA120                → 轻仓 (0.25)
  MA60下方且MA5<MA20(空头) → 空仓 (0.0)
"""

import logging
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

logger = logging.getLogger("market_filter")

CACHE_DIR = Path("/opt/daily_stock_analysis/data/cache")
INDEX_CACHE_FILE = CACHE_DIR / "hs300_index.pkl"

# ── 允许的最小 scale（防止完全空仓踏空） ──
MIN_SCALE = 0.0


def _fetch_index_from_mootdx() -> pd.DataFrame:
    """从 mootdx 获取沪深300指数数据"""
    try:
        from mootdx.quotes import Quotes
        client = Quotes.factory(market='std')
        df = client.index(frequency=9, market=1, symbol='000001', start=0, offset=500)
        if df is None or df.empty:
            raise ValueError("mootdx index empty")
        result = pd.DataFrame()
        result['date'] = pd.to_datetime(
            df['year'].astype(str) + '-' +
            df['month'].astype(str).str.zfill(2) + '-' +
            df['day'].astype(str).str.zfill(2)
        )
        result['close'] = df['close'].astype(float)
        result = result.sort_values('date').reset_index(drop=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        result.to_pickle(str(INDEX_CACHE_FILE))
        logger.info("沪深300指数已从 mootdx 刷新缓存")
        return result
    except Exception as e:
        logger.warning(f"mootdx 获取指数失败: {e}")
        raise


def _fetch_index_from_akshare() -> pd.DataFrame:
    """从 akshare 获取沪深300指数数据（备用）"""
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily_em(symbol="sh000300")
        if df is None or df.empty:
            raise ValueError("akshare index empty")
        result = pd.DataFrame()
        result['date'] = pd.to_datetime(df['date'])
        result['close'] = df['close'].astype(float)
        result = result.sort_values('date').reset_index(drop=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        result.to_pickle(str(INDEX_CACHE_FILE))
        logger.info("沪深300指数已从 akshare 刷新缓存")
        return result
    except Exception as e:
        logger.warning(f"akshare 获取指数失败: {e}")
        raise


def refresh_index_cache():
    """刷新沪深300指数缓存（从 mootdx 或 akshare）"""
    try:
        return _fetch_index_from_mootdx()
    except Exception:
        try:
            return _fetch_index_from_akshare()
        except Exception as e:
            logger.error(f"所有数据源均无法获取沪深300指数: {e}")
            return None


def load_hs300_index() -> pd.DataFrame:
    """加载沪深300指数数据（优先读缓存，过期则刷新）"""
    # 检查缓存是否存在且不过期（24小时内）
    if INDEX_CACHE_FILE.exists():
        mtime = datetime.fromtimestamp(INDEX_CACHE_FILE.stat().st_mtime)
        age = datetime.now() - mtime
        if age < timedelta(hours=24):
            try:
                df = pd.read_pickle(str(INDEX_CACHE_FILE))
                if df is not None and len(df) > 100:
                    return df
            except Exception:
                pass
        logger.info("沪深300指数缓存过期，重新拉取")

    # 刷新缓存
    df = refresh_index_cache()
    if df is not None:
        return df

    # 最后尝试读过期缓存
    if INDEX_CACHE_FILE.exists():
        logger.warning("使用过期缓存")
        return pd.read_pickle(str(INDEX_CACHE_FILE))
    return None


def compute_ma(df: pd.DataFrame) -> pd.DataFrame:
    """计算均线"""
    df = df.copy()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    df["ma120"] = df["close"].rolling(120).mean()
    return df


def get_market_scale(as_of_date: str = None) -> float:
    """
    获取当前大盘仓位系数

    Args:
        as_of_date: 日期 YYYYMMDD，None 表示今天

    Returns:
        float: 仓位系数 0.0 ~ 1.0
    """
    df = load_hs300_index()
    if df is None:
        logger.warning("沪深300指数数据不可用，默认轻仓")
        return 0.25

    df = compute_ma(df)

    if as_of_date is None:
        as_of_date = datetime.now().strftime("%Y%m%d")

    td = df[df["date"] <= pd.Timestamp(as_of_date)]
    if len(td) < 5:
        return 1.0

    close = td["close"].iloc[-1]
    ma5 = td["ma5"].iloc[-1]
    ma20 = td["ma20"].iloc[-1]
    ma60 = td["ma60"].dropna().iloc[-1] if not td["ma60"].dropna().empty else 0
    ma120 = td["ma120"].dropna().iloc[-1] if not td["ma120"].dropna().empty else 0

    above_ma60 = close > ma60 if ma60 > 0 else True
    below_ma60_ma5ltma20 = (close <= ma60 and ma5 < ma20) if ma60 > 0 else False
    above_ma120 = close > ma120 if ma120 > 0 else True

    if above_ma60:
        return 1.0
    elif above_ma120:
        return 0.5
    elif below_ma60_ma5ltma20:
        return max(MIN_SCALE, 0.0)
    else:
        return 0.25


def get_market_state(as_of_date: str = None) -> dict:
    """
    获取完整的大盘状态信息

    Returns:
        dict: {scale, state_name, index_close, ma5, ma20, ma60, ma120}
    """
    df = load_hs300_index()
    if df is None:
        return {"scale": 0.25, "state_name": "unknown", "error": "no_data"}

    df = compute_ma(df)
    if as_of_date is None:
        as_of_date = datetime.now().strftime("%Y%m%d")
    td = df[df["date"] <= pd.Timestamp(as_of_date)]

    if len(td) < 5:
        return {"scale": 1.0, "state_name": "unknown"}

    close = td["close"].iloc[-1]
    ma5 = td["ma5"].iloc[-1]
    ma20 = td["ma20"].iloc[-1]
    ma60 = td["ma60"].dropna().iloc[-1] if not td["ma60"].dropna().empty else 0
    ma120 = td["ma120"].dropna().iloc[-1] if not td["ma120"].dropna().empty else 0

    above_ma60 = close > ma60 if ma60 > 0 else True
    below_ma60_ma5ltma20 = (close <= ma60 and ma5 < ma20) if ma60 > 0 else False
    above_ma120 = close > ma120 if ma120 > 0 else True

    if above_ma60:
        state, scale = "多头 (MA60上方)", 1.0
    elif above_ma120:
        state, scale = "谨慎 (MA60~MA120)", 0.5
    elif below_ma60_ma5ltma20:
        state, scale = "空头 (MA60下方+死叉)", max(MIN_SCALE, 0.0)
    else:
        state, scale = "弱势 (MA60下方)", 0.25

    return {
        "scale": scale,
        "state_name": state,
        "index_close": round(close, 2),
        "ma5": round(ma5, 2),
        "ma20": round(ma20, 2),
        "ma60": round(ma60, 2) if ma60 > 0 else None,
        "ma120": round(ma120, 2) if ma120 > 0 else None,
    }


if __name__ == "__main__":
    # 命令行测试
    logging.basicConfig(level=logging.INFO)
    state = get_market_state()
    print(f"沪深300指数状态:")
    print(f"  当前点位: {state.get('index_close')}")
    print(f"  MA5: {state.get('ma5')}  MA20: {state.get('ma20')}")
    print(f"  MA60: {state.get('ma60')}  MA120: {state.get('ma120')}")
    print(f"  市场状态: {state.get('state_name')}")
    print(f"  仓位系数: {state.get('scale')}")
