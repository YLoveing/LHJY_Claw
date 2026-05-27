# -*- coding: utf-8 -*-
"""
===================================
MootdxFetcher - 通达信备用数据源 (Priority 9)
===================================

数据来源：通达信行情服务器（mootdx 库）
特点：免费、无需 Token、与 PytdxFetcher 同源但作为最后兜底
安装：pip install mootdx

使用时作为 PytdxFetcher 的后备，在所有主数据源失效时启用
"""

import logging
from typing import Optional, List, Dict, Any

import pandas as pd

from .base import BaseFetcher, DataFetchError, STANDARD_COLUMNS

logger = logging.getLogger(__name__)


class MootdxFetcher(BaseFetcher):
    """mootdx 通达信数据源（最后兜底）"""

    name = "MootdxFetcher"
    priority = 9  # 最低优先级，排在所有数据源最后

    def __init__(self):
        super().__init__()
        self._client = None

    def _get_client(self):
        """延迟初始化 mootdx 客户端"""
        if self._client is None:
            try:
                from mootdx.quotes import Quotes
                self._client = Quotes.factory(market='std')
                logger.info("MootdxFetcher 客户端初始化成功")
            except ImportError:
                logger.warning("mootdx 未安装，请执行: pip install mootdx")
                raise
            except Exception as e:
                logger.warning(f"MootdxFetcher 客户端初始化失败: {e}")
                raise
        return self._client

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        从 mootdx 获取日K线原始数据
        frequency=9 表示日线
        """
        client = self._get_client()
        try:
            # 算一下要多少条（按交易日估算）
            import datetime
            start_dt = datetime.datetime.strptime(start_date, '%Y-%m-%d')
            end_dt = datetime.datetime.strptime(end_date, '%Y-%m-%d')
            # 估算交易日数量
            estimated_days = int((end_dt - start_dt).days * 0.7) + 20
            estimated_days = min(estimated_days, 500)  # 最多取500条
            offset = max(estimated_days, 30)

            code = stock_code.zfill(6)
            df = client.bars(symbol=code, frequency=9, start=0, offset=offset)

            if df is None or df.empty:
                raise DataFetchError(f"MootdxFetcher 未获取到 {stock_code} 的数据")

            return df

        except Exception as e:
            raise DataFetchError(f"MootdxFetcher 获取 {stock_code} 失败: {e}") from e

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """
        将 mootdx 的列名标准化为统一格式

        mootdx 返回列: open, close, high, low, vol, amount, year, month, day
        目标列: date, open, high, low, close, volume, amount, pct_chg
        """
        df = df.copy()

        # 构建日期列
        df['date'] = pd.to_datetime(
            df['year'].astype(str) + '-' +
            df['month'].astype(str).str.zfill(2) + '-' +
            df['day'].astype(str).str.zfill(2),
            errors='coerce'
        )

        # 重命名
        result = pd.DataFrame()
        result['date'] = df['date']
        result['open'] = df['open'].astype(float)
        result['high'] = df['high'].astype(float)
        result['low'] = df['low'].astype(float)
        result['close'] = df['close'].astype(float)
        result['volume'] = pd.to_numeric(df['vol'], errors='coerce').fillna(0)
        result['amount'] = pd.to_numeric(df['amount'], errors='coerce').fillna(0)

        # 计算涨跌幅
        result['pct_chg'] = result['close'].pct_change() * 100
        result['pct_chg'] = result['pct_chg'].fillna(0.0).round(2)

        # 过滤日期范围（mootdx 可能返回超出范围的数据）
        result = result.dropna(subset=['date'])

        return result

    def get_realtime_quote(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """
        通过 mootdx 获取实时行情
        返回 dict 或 None
        """
        try:
            client = self._get_client()
            code = stock_code.zfill(6)
            rt = client.quotes(symbol=[code])
            if rt is not None and not rt.empty and len(rt) > 0:
                row = rt.iloc[0]
                return {
                    'price': float(row.get('price', 0)),
                    'last_close': float(row.get('last_close', 0)),
                    'open': float(row.get('open', 0)),
                    'high': float(row.get('high', 0)),
                    'low': float(row.get('low', 0)),
                    'volume': int(row.get('active1', 0)),
                    'name': '',
                    'code': stock_code,
                    'source': self.name,
                }
        except Exception as e:
            logger.debug(f"MootdxFetcher 实时行情失败 ({stock_code}): {e}")
        return None
