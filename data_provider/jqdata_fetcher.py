"""
JQDataFetcher - 聚宽 JQData 数据源

数据来源：聚宽 (JoinQuant) JQData SDK
特点：数据质量高，A股全市场+指数成分股+财务数据
权限：需单独申请 JQData SDK 调用权限（免费试用）

优先级别：P0_5（与 Efinance 并列最高）
"""

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential, before_sleep_log

from .base import BaseFetcher, DataFetchError, STANDARD_COLUMNS

log = logging.getLogger(__name__)


class JQDataFetcher(BaseFetcher):
    """聚宽 JQData 数据源"""

    priority = 0  # 同 Efinance 同级

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._jq = None
        self._authed = False
        self._phone = os.environ.get("JQD_PHONE", "")
        self._pwd = os.environ.get("JQD_PWD", "")

    def _ensure_auth(self) -> bool:
        """确保已认证，若未认证则登录"""
        if self._authed:
            return True
        if not self._phone or not self._pwd:
            log.warning("JQData: 未配置账号密码，跳过")
            return False
        try:
            import jqdatasdk as jq
            self._jq = jq
            jq.auth(self._phone, self._pwd)
            self._authed = True
            log.info("JQData 认证成功")
            return True
        except Exception as e:
            self._authed = False
            log.warning(f"JQData 认证失败: {e}")
            return False

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取日线数据"""
        if not self._ensure_auth():
            raise DataFetchError("JQData 未认证")

        # 转换代码格式: 510300 -> 510300.XSHG, 002410 -> 002410.XSHE
        if stock_code.startswith("6") or stock_code.startswith("5"):
            jq_code = f"{stock_code}.XSHG"
        else:
            jq_code = f"{stock_code}.XSHE"

        try:
            df = self._jq.get_price(
                jq_code,
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
                frequency="daily",
                fields=["open", "close", "high", "low", "volume", "money"],
            )
            if df is None or df.empty:
                raise DataFetchError(f"JQData 未返回数据: {stock_code}")

            # 标准化列名到系统统一格式
            df = df.rename(columns={
                "open": "开盘",
                "close": "收盘",
                "high": "最高",
                "low": "最低",
                "volume": "成交量",
                "money": "成交额",
            })
            df["股票代码"] = stock_code
            df["日期"] = df.index.strftime("%Y-%m-%d")
            df = df.reset_index(drop=True)
            return df[["股票代码", "日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"]]

        except Exception as e:
            err = str(e)
            if "权限" in err or "申请" in err:
                log.warning(f"JQData 权限不足: {e}")
                raise DataFetchError(f"JQData 权限受限: {e}")
            raise DataFetchError(f"JQData 获取失败 {stock_code}: {e}") from e

    def get_security_list(self, security_type: str = "stock") -> List[str]:
        """获取全市场证券列表"""
        if not self._ensure_auth():
            return []
        try:
            securities = self._jq.get_all_securities([security_type])
            return list(securities.index)
        except Exception as e:
            log.warning(f"JQData 获取证券列表失败: {e}")
            return []

    def get_index_stocks(self, index_code: str) -> List[str]:
        """获取指数成分股"""
        if not self._ensure_auth():
            return []
        try:
            return list(self._jq.get_index_stocks(index_code))
        except Exception as e:
            log.warning(f"JQData 获取指数成分股失败 {index_code}: {e}")
            return []

    def get_fundamentals(self, table: str, stock_code: str, start_date: str, end_date: str,
                         fields: Optional[List[str]] = None) -> Optional[pd.DataFrame]:
        """获取财务/估值数据"""
        if not self._ensure_auth():
            return None
        try:
            q = self._jq.get_query(
                table, stock_code,
                start_date=start_date,
                end_date=end_date,
                fields=fields or ["pe_ratio", "pb_ratio", "market_cap"],
            )
            return q
        except Exception as e:
            log.warning(f"JQData 获取基本面数据失败: {e}")
            return None

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """标准化：已在 _fetch_raw_data 中完成，此处直接返回"""
        if "股票代码" not in df.columns:
            df["股票代码"] = stock_code
        return df

    def get_daily_data(self, stock_code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """统一接口：获取日线数据"""
        try:
            return self._fetch_raw_data(stock_code, start_date, end_date)
        except Exception as e:
            log.warning(f"[JQDataFetcher] {stock_code} 获取失败: {e}")
            return None
