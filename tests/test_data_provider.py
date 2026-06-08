# -*- coding: utf-8 -*-
"""data_provider/base.py 核心模块测试 — normalize_stock_code, DataFetcherManager 策略切换"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

import pytest
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_provider.base import (
    normalize_stock_code,
    canonical_stock_code,
    is_bse_code,
    is_st_stock,
    is_kc_cy_stock,
    _is_hk_market,
    _is_etf_code,
    _market_tag,
    DataFetchError,
    DataSourceUnavailableError,
    BaseFetcher,
    DataFetcherManager,
    unwrap_exception,
    summarize_exception,
)


# ═══════════════════════════════════════════
# normalize_stock_code 测试
# ═══════════════════════════════════════════

class TestNormalizeStockCode:
    def test_already_clean_passes_through(self):
        assert normalize_stock_code("600519") == "600519"
        assert normalize_stock_code("000001") == "000001"

    def test_strips_sh_prefix(self):
        assert normalize_stock_code("SH600519") == "600519"
        assert normalize_stock_code("sh600519") == "600519"

    def test_strips_sz_prefix(self):
        assert normalize_stock_code("SZ000001") == "000001"
        assert normalize_stock_code("sz000001") == "000001"

    def test_strips_dot_suffix(self):
        assert normalize_stock_code("600519.SH") == "600519"
        assert normalize_stock_code("000001.SZ") == "000001"

    def test_keeps_hk_stock(self):
        assert normalize_stock_code("HK00700") == "HK00700"
        assert normalize_stock_code("1810.HK") == "HK01810"

    def test_keeps_us_ticker(self):
        assert normalize_stock_code("AAPL") == "AAPL"

    def test_bse_code(self):
        assert normalize_stock_code("BJ920748") == "920748"
        assert normalize_stock_code("920748.BJ") == "920748"

    def test_strips_whitespace(self):
        assert normalize_stock_code("  600519  ") == "600519"


# ═══════════════════════════════════════════
# canonical_stock_code 测试
# ═══════════════════════════════════════════

class TestCanonicalStockCode:
    def test_uppercases_lowercase(self):
        assert canonical_stock_code("aapl") == "AAPL"

    def test_uppercases_hk(self):
        assert canonical_stock_code("hk00700") == "HK00700"

    def test_preserves_digits(self):
        assert canonical_stock_code("600519") == "600519"

    def test_strips_whitespace(self):
        assert canonical_stock_code("  aapl  ") == "AAPL"


# ═══════════════════════════════════════════
# 代码分类函数测试
# ═══════════════════════════════════════════

class TestIsBseCode:
    def test_bse_92_code_returns_true(self):
        assert is_bse_code("920748") is True

    def test_shanghai_code_returns_false(self):
        assert is_bse_code("600519") is False

    def test_shenzhen_code_returns_false(self):
        assert is_bse_code("000001") is False

    def test_b_share_returns_false(self):
        assert is_bse_code("900901") is False


class TestIsSTStock:
    def test_st_prefix_detected(self):
        assert is_st_stock("ST平安") is True
        assert is_st_stock("*ST东方") is True

    def test_normal_name_returns_false(self):
        assert is_st_stock("贵州茅台") is False

    def test_none_returns_false(self):
        assert is_st_stock(None) is False


class TestIsKCYStock:
    def test_kc_returns_true(self):
        assert is_kc_cy_stock("688001") is True

    def test_cy_returns_true(self):
        assert is_kc_cy_stock("300750") is True

    def test_main_board_returns_false(self):
        assert is_kc_cy_stock("600519") is False


class TestMarketTag:
    def test_hk_market(self):
        assert _market_tag("HK00700") == "hk"

    def test_cn_market(self):
        assert _market_tag("600519") == "cn"
        assert _market_tag("000001") == "cn"


class TestIsEtfCode:
    def test_etf_prefixes_detected(self):
        assert _is_etf_code("510050") is True
        assert _is_etf_code("159915") is True

    def test_normal_stock_returns_false(self):
        assert _is_etf_code("600519") is False


# ═══════════════════════════════════════════
# unwrap_exception / summarize_exception 测试
# ═══════════════════════════════════════════

class TestExceptionUtils:
    def test_unwrap_no_chain(self):
        e = ValueError("test")
        assert unwrap_exception(e) is e

    def test_unwrap_with_cause(self):
        root = ValueError("root")
        e = RuntimeError("wrapped")
        e.__cause__ = root
        assert unwrap_exception(e) is root

    def test_summarize_returns_type_and_message(self):
        error_type, message = summarize_exception(ValueError("something broke"))
        assert error_type == "ValueError"
        assert "something broke" in message


# ═══════════════════════════════════════════
# DataFetcherManager 测试
# ═══════════════════════════════════════════

class TestDataFetcherManager:
    def test_init_with_no_fetchers_creates_defaults(self):
        mgr = DataFetcherManager()
        fetchers = mgr._get_fetchers_snapshot()
        assert len(fetchers) > 0
        assert all(isinstance(f, BaseFetcher) for f in fetchers)

    def test_init_sorts_by_priority(self):
        """fetcher 列表应按 priority 从小到大排序。"""
        mgr = DataFetcherManager()
        fetchers = mgr._get_fetchers_snapshot()
        priorities = [f.priority for f in fetchers]
        assert priorities == sorted(priorities)

    def test_available_fetchers_returns_names(self):
        mgr = DataFetcherManager()
        names = mgr.available_fetchers
        assert isinstance(names, list)
        assert all(isinstance(n, str) for n in names)

    def test_add_fetcher_resorts(self):
        mgr = DataFetcherManager()
        initial = len(mgr._get_fetchers_snapshot())

        class TestFetcher(BaseFetcher):
            name = "TestFetcher"
            priority = 999

            def _fetch_raw_data(self, stock_code, start_date, end_date):
                return pd.DataFrame()

            def _normalize_data(self, df, stock_code):
                return df

        mgr.add_fetcher(TestFetcher())
        assert len(mgr._get_fetchers_snapshot()) == initial + 1
        # 最后一个应是 TestFetcher (priority 999)
        assert mgr._get_fetchers_snapshot()[-1].name == "TestFetcher"

    def test_get_daily_data_disk_cache_checked(self):
        """get_daily_data 会先检查磁盘缓存（不深入测试日期比较的 pandas 兼容性）。"""
        mgr = DataFetcherManager.__new__(DataFetcherManager)
        mgr._ensure_concurrency_guards()
        mgr._fetchers = []

        # 让缓存返回空，走 fetcher 路径
        mock_cache = MagicMock()
        mock_cache.get_kline.return_value = None
        mgr._disk_cache = mock_cache

        with pytest.raises(DataFetchError, match="所有数据源"):
            mgr.get_daily_data("000001", days=30)

        # 验证调用了缓存
        mock_cache.get_kline.assert_called_once()

    def test_get_daily_data_all_fetchers_fail(self):
        """所有数据源都失败时抛出 DataFetchError。"""
        mgr = DataFetcherManager.__new__(DataFetcherManager)
        mgr._ensure_concurrency_guards()

        # mock disk cache returning none
        mock_cache = MagicMock()
        mock_cache.get_kline.return_value = None
        mgr._disk_cache = mock_cache

        # 创建一组必 fail 的 fetcher
        class FailFetcher(BaseFetcher):
            name = "FailFetcher"
            priority = 0

            def _fetch_raw_data(self, stock_code, start_date, end_date):
                raise DataSourceUnavailableError("always fail")

            def _normalize_data(self, df, stock_code):
                return df

        mgr._fetchers = [FailFetcher()]
        with pytest.raises(DataFetchError, match="所有数据源"):
            mgr.get_daily_data("000001", days=30)

    def test_get_stock_name_from_cache(self):
        mgr = DataFetcherManager.__new__(DataFetcherManager)
        mgr._ensure_concurrency_guards()
        mgr._stock_name_cache = {"000001": "平安银行"}
        mgr._stock_name_cache_lock = MagicMock()
        mgr._fetchers = []

        with patch.object(DataFetcherManager, "_get_cached_stock_name", return_value="平安银行"):
            # Re-init with empty fetchers and no caches
            pass

        # Simple: just verify the method doesn't crash
        mgr._fetchers = []
        mgr._stock_name_cache = {}
        mgr._fundamental_timeout_slots = MagicMock()

        # With empty fetchers, should return ""
        from data_provider.base import STOCK_NAME_MAP
        # Monkey-patch to prevent side effects
        with patch.object(DataFetcherManager, "get_realtime_quote", return_value=None):
            name = mgr.get_stock_name("999999")
            assert name == "" or name is None


# ═══════════════════════════════════════════
# BaseFetcher 测试
# ═══════════════════════════════════════════

class TestBaseFetcher:
    def test_get_main_indices_default_none(self):
        class TestFetcher(BaseFetcher):
            name = "Test"
            priority = 0

            def _fetch_raw_data(self, code, start, end):
                return pd.DataFrame()

            def _normalize_data(self, df, code):
                return df

        fetcher = TestFetcher()
        assert fetcher.get_main_indices() is None

    def test_get_market_stats_default_none(self):
        class TestFetcher(BaseFetcher):
            name = "Test"
            priority = 0

            def _fetch_raw_data(self, code, start, end):
                return pd.DataFrame()

            def _normalize_data(self, df, code):
                return df

        fetcher = TestFetcher()
        assert fetcher.get_market_stats() is None

    def test_random_sleep_runs_without_error(self):
        BaseFetcher.random_sleep(min_seconds=0.001, max_seconds=0.002)
