# -*- coding: utf-8 -*-
"""
JQData 基本面数据源 — 稳定获取营收增速/净利润增速/ROE/毛利率/PE/PB。

数据来源：聚宽 JQData SDK
权限：通过 .env 中 JQD_PHONE / JQD_PWD 认证
容错：JQData 不可用时回退到 AkshareFundamentalAdapter
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd

log = logging.getLogger(__name__)


def _code_to_jq(stock_code: str) -> str:
    """600xxx -> 600xxx.XSHG, 000xxx -> 000xxx.XSHE"""
    code = str(stock_code).zfill(6)
    if code.startswith(("6", "5")):
        return f"{code}.XSHG"
    return f"{code}.XSHE"


def _code_from_jq(jq_code: str) -> str:
    """600xxx.XSHG -> 600xxx"""
    return re.sub(r"\..*$", "", str(jq_code))


def _safe_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        v = float(val)
        return v if abs(v) < 1e12 else None
    except (TypeError, ValueError):
        return None


class JQDataFundamental:
    """聚宽 JQData 基本面数据获取器。

    使用 JQData SDK 查询 income/indicator/valuation 三张表，
    返回标准化 get_fundamentals(stock_code) 结果。
    """

    def __init__(self):
        self._jq = None
        self._authed = False
        self._phone = os.environ.get("JQD_PHONE", "")
        self._pwd = os.environ.get("JQD_PWD", "")

    def _ensure_auth(self) -> bool:
        if self._authed:
            return True
        if not self._phone or not self._pwd:
            log.debug("JQData: 未配置账号密码，跳过")
            return False
        try:
            import jqdatasdk as jq
            self._jq = jq
            jq.auth(self._phone, self._pwd)
            self._authed = True
            return True
        except Exception as e:
            log.warning(f"JQData 认证失败: {e}")
            return False

    @property
    def is_available(self) -> bool:
        return self._ensure_auth()

    def get_fundamentals(self, stock_code: str) -> Dict[str, Any]:
        """获取个股基本面核心指标。

        Returns:
            {
                "revenue_yoy": float or None,   # 营收同比增速 (%)
                "profit_yoy": float or None,    # 归母净利润同比增速 (%)
                "roe": float or None,           # 净资产收益率 (%)
                "gross_margin": float or None,   # 毛利率 (%)
                "net_margin": float or None,     # 净利率 (%)
                "pe_ttm": float or None,        # PE(TTM)
                "pb": float or None,            # PB
                "ps_ttm": float or None,        # PS(TTM)
                "report_date": str or None,     # 报告期
                "source": "jqdata" or "akshare",
            }
        """
        result: Dict[str, Any] = {
            "revenue_yoy": None,
            "profit_yoy": None,
            "roe": None,
            "gross_margin": None,
            "net_margin": None,
            "pe_ttm": None,
            "pb": None,
            "ps_ttm": None,
            "report_date": None,
            "source": "none",
        }

        if not self._ensure_auth():
            return self._fallback_akshare(stock_code)

        jq_code = _code_to_jq(stock_code)

        try:
            # ── 1. Indicator 表：ROE / 毛利率 / 净利率 ──
            from jqdatasdk import finance, query

            q_ind = query(finance.STK_FINANCIAL_INDICATOR).filter(
                finance.STK_FINANCIAL_INDICATOR.code == jq_code
            ).order_by(
                finance.STK_FINANCIAL_INDICATOR.report_date.desc()
            ).limit(2)
            df_ind = finance.run_query(q_ind)

            if df_ind is not None and not df_ind.empty:
                latest_ind = df_ind.iloc[0]
                result["roe"] = _safe_float(latest_ind.get("roe"))
                result["gross_margin"] = _safe_float(latest_ind.get("gross_profit_margin"))
                result["net_margin"] = _safe_float(latest_ind.get("net_profit_margin"))
                if result["roe"] is not None:
                    result["roe"] = round(result["roe"], 2)
                if result["gross_margin"] is not None:
                    result["gross_margin"] = round(result["gross_margin"], 2)
                if result["net_margin"] is not None:
                    result["net_margin"] = round(result["net_margin"], 2)
                # 报告日期
                rd = latest_ind.get("report_date")
                if pd.notna(rd):
                    result["report_date"] = str(rd)[:10]

                # YoY 计算：对比去年同期
                if len(df_ind) >= 2:
                    prev_ind = df_ind.iloc[1]
                else:
                    try:
                        q_ind_prev = query(finance.STK_FINANCIAL_INDICATOR).filter(
                            finance.STK_FINANCIAL_INDICATOR.code == jq_code,
                            finance.STK_FINANCIAL_INDICATOR.report_date <= (rd - pd.Timedelta(days=370)),
                        ).order_by(
                            finance.STK_FINANCIAL_INDICATOR.report_date.desc()
                        ).limit(1)
                        df_ind_prev = finance.run_query(q_ind_prev)
                        prev_ind = df_ind_prev.iloc[0] if (df_ind_prev is not None and not df_ind_prev.empty) else None
                    except Exception:
                        prev_ind = None

                if prev_ind is not None:
                    cur_np = _safe_float(latest_ind.get("net_profit"))
                    prev_np = _safe_float(prev_ind.get("net_profit"))
                    if cur_np and prev_np and prev_np != 0:
                        result["profit_yoy"] = round((cur_np / abs(prev_np) - 1) * 100, 2)

                    cur_rev = _safe_float(latest_ind.get("operating_revenue"))
                    prev_rev = _safe_float(prev_ind.get("operating_revenue"))
                    if cur_rev and prev_rev and prev_rev != 0:
                        result["revenue_yoy"] = round((cur_rev / abs(prev_rev) - 1) * 100, 2)

            # ── 2. Income 表：营收/净利润（若 indicator 未提供 YoY）───
            if result["revenue_yoy"] is None or result["profit_yoy"] is None:
                try:
                    q_inc = query(finance.STK_INCOME_STATEMENT).filter(
                        finance.STK_INCOME_STATEMENT.code == jq_code
                    ).order_by(
                        finance.STK_INCOME_STATEMENT.report_date.desc()
                    ).limit(2)
                    df_inc = finance.run_query(q_inc)

                    if df_inc is not None and len(df_inc) >= 2:
                        cur = df_inc.iloc[0]
                        prev = df_inc.iloc[1]

                        if result["revenue_yoy"] is None:
                            cur_rev = _safe_float(cur.get("total_operating_revenue"))
                            prev_rev = _safe_float(prev.get("total_operating_revenue"))
                            if cur_rev and prev_rev and prev_rev != 0:
                                result["revenue_yoy"] = round((cur_rev / abs(prev_rev) - 1) * 100, 2)

                        if result["profit_yoy"] is None:
                            cur_np = _safe_float(cur.get("net_profit"))
                            prev_np = _safe_float(prev.get("net_profit"))
                            if cur_np and prev_np and prev_np != 0:
                                result["profit_yoy"] = round((cur_np / abs(prev_np) - 1) * 100, 2)

                    if result["report_date"] is None and df_inc is not None and not df_inc.empty:
                        rd = df_inc.iloc[0].get("report_date")
                        if pd.notna(rd):
                            result["report_date"] = str(rd)[:10]
                except Exception as e:
                    log.debug(f"Income 表查询失败 {stock_code}: {e}")

            # ── 3. Valuation 表：PE / PB / PS ──
            try:
                q_val = query(finance.STK_VALUATION).filter(
                    finance.STK_VALUATION.code == jq_code
                ).order_by(
                    finance.STK_VALUATION.date.desc()
                ).limit(1)
                df_val = finance.run_query(q_val)

                if df_val is not None and not df_val.empty:
                    val = df_val.iloc[0]
                    result["pe_ttm"] = _safe_float(val.get("pe_ratio"))
                    result["pb"] = _safe_float(val.get("pb_ratio"))
                    result["ps_ttm"] = _safe_float(val.get("ps_ratio"))
            except Exception as e:
                log.debug(f"Valuation 表查询失败 {stock_code}: {e}")

            result["source"] = "jqdata"
            log.debug(f"JQData 基本面 {stock_code}: ROE={result['roe']}, PE={result['pe_ttm']}")

        except Exception as e:
            log.warning(f"JQData 基本面获取失败 {stock_code}: {e}")
            return self._fallback_akshare(stock_code)

        # 如果所有字段都为空，说明获取失败，回退
        all_null = all(
            result[k] is None
            for k in ("revenue_yoy", "profit_yoy", "roe", "gross_margin", "pe_ttm", "pb")
        )
        if all_null:
            return self._fallback_akshare(stock_code)

        return result

    def _fallback_akshare(self, stock_code: str) -> Dict[str, Any]:
        """JQData 不可用时回退到 akshare。"""
        log.debug(f"回退 akshare 基本面: {stock_code}")
        try:
            from data_provider.fundamental_adapter import AkshareFundamentalAdapter
            adapter = AkshareFundamentalAdapter()
            bundle = adapter.get_fundamental_bundle(stock_code)

            growth = bundle.get("growth", {})
            return {
                "revenue_yoy": growth.get("revenue_yoy"),
                "profit_yoy": growth.get("net_profit_yoy"),
                "roe": growth.get("roe"),
                "gross_margin": growth.get("gross_margin"),
                "net_margin": None,
                "pe_ttm": None,
                "pb": None,
                "ps_ttm": None,
                "report_date": None,
                "source": "akshare",
            }
        except Exception as e:
            log.debug(f"akshare 回退也失败: {e}")
            return {
                "revenue_yoy": None,
                "profit_yoy": None,
                "roe": None,
                "gross_margin": None,
                "net_margin": None,
                "pe_ttm": None,
                "pb": None,
                "ps_ttm": None,
                "report_date": None,
                "source": "none",
            }
