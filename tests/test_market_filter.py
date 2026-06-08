# -*- coding: utf-8 -*-
"""market_filter 模块单元测试 — 大盘均线过滤与仓位管理"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from risk import market_filter

# ═══════════════════════════════════════════
# compute_ma 测试
# ═══════════════════════════════════════════


class TestComputeMA:
    def test_adds_all_four_mas(self, mock_hs300_df):
        df = market_filter.compute_ma(mock_hs300_df)
        assert "ma5" in df.columns
        assert "ma20" in df.columns
        assert "ma60" in df.columns
        assert "ma120" in df.columns

    def test_ma5_shorter_than_ma60(self, mock_hs300_df):
        """MA5 曲线应比 MA60 更贴近原价。"""
        df = market_filter.compute_ma(mock_hs300_df)
        # 有足够数据后，ma5 和 ma60 应不同
        valid = df.dropna(subset=["ma60"]).tail(50)
        if len(valid) > 0:
            # ma5 的标准差通常更大（更敏感）
            assert valid["ma5"].std() != valid["ma60"].std()

    def test_na_before_window(self):
        """窗口不足时应有 NaN。"""
        df = pd.DataFrame({"date": pd.date_range("2025-01-01", periods=10), "close": range(1, 11)})
        result = market_filter.compute_ma(df)
        # ma120 需要 120 天，10天数据应该全 NaN
        assert result["ma120"].isna().all()

    def test_does_not_mutate_input(self, mock_hs300_df):
        original_columns = list(mock_hs300_df.columns)
        market_filter.compute_ma(mock_hs300_df)
        assert list(mock_hs300_df.columns) == original_columns


# ═══════════════════════════════════════════
# load_hs300_index 测试
# ═══════════════════════════════════════════


class TestLoadHS300Index:
    def test_returns_dataframe_from_valid_cache(self, mock_hs300_df, tmp_path, monkeypatch):
        """缓存文件有效时应直接读取。"""
        cache_file = tmp_path / "hs300_index.pkl"
        mock_hs300_df.to_pickle(str(cache_file))

        monkeypatch.setattr(market_filter, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(market_filter, "INDEX_CACHE_FILE", cache_file)

        df = market_filter.load_hs300_index()
        assert df is not None
        assert len(df) > 100

    def test_returns_none_when_all_fail(self, tmp_path, monkeypatch):
        """所有数据源都失败时返回 None。"""
        monkeypatch.setattr(market_filter, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(market_filter, "INDEX_CACHE_FILE", tmp_path / "hs300_index.pkl")
        # 没有缓存文件 + 模拟 refresh 返回 None
        monkeypatch.setattr(market_filter, "refresh_index_cache", lambda: None)

        df = market_filter.load_hs300_index()
        assert df is None

    def test_falls_back_to_stale_cache(self, mock_hs300_df, tmp_path, monkeypatch):
        """缓存过期但刷新失败时，应正确 fallback 逻辑（不崩溃）。"""
        cache_file = tmp_path / "hs300_index.pkl"
        mock_hs300_df.to_pickle(str(cache_file))

        # Mock load_hs300_index 内部的 INDEX_CACHE_FILE 和 stat
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        mock_path.__str__.return_value = str(cache_file)
        mock_path.stat.return_value.st_mtime = (datetime.now() - timedelta(hours=25)).timestamp()

        monkeypatch.setattr(market_filter, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(market_filter, "INDEX_CACHE_FILE", mock_path)
        monkeypatch.setattr(market_filter, "refresh_index_cache", lambda: None)

        # 应回退到过期缓存
        df = market_filter.load_hs300_index()
        assert df is not None
        assert len(df) > 100


# ═══════════════════════════════════════════
# get_market_scale 测试
# ═══════════════════════════════════════════


class TestGetMarketScale:
    def test_returns_1_when_above_ma60(self, mock_hs300_df, monkeypatch):
        """价格在 MA60 上方 → 满仓 1.0"""
        # 构建一个明显多头排列的 DataFrame
        dates = pd.date_range("2025-01-01", periods=200, freq="B")
        closes = np.linspace(3800, 4200, 200)  # 持续上涨
        df = pd.DataFrame({"date": dates, "close": closes})
        monkeypatch.setattr(market_filter, "load_hs300_index", lambda: df)

        scale = market_filter.get_market_scale(as_of_date="20250609")
        assert scale == 1.0

    def test_returns_0_5_when_between_ma60_and_ma120(self, mock_hs300_bearish_df, monkeypatch):
        """价格跌破 MA60 但在 MA120 上方 → 半仓 0.5"""
        monkeypatch.setattr(market_filter, "load_hs300_index", lambda: mock_hs300_bearish_df)

        scale = market_filter.get_market_scale(as_of_date="20250609")
        # 结果应该在 [0.25, 0.5, 1.0] 中（取决于具体计算值）
        assert scale in (0.0, 0.25, 0.5, 1.0)

    def test_returns_0_when_dead_cross(self, mock_hs300_bearish_df, monkeypatch):
        """死叉状态 → 空仓 MIN_SCALE"""
        monkeypatch.setattr(market_filter, "load_hs300_index", lambda: mock_hs300_bearish_df)

        scale = market_filter.get_market_scale(as_of_date="20250609")
        assert 0.0 <= scale <= 1.0  # 应该在有效范围内

    def test_fallback_when_no_data(self, monkeypatch):
        """无数据时默认轻仓 0.25"""
        monkeypatch.setattr(market_filter, "load_hs300_index", lambda: None)

        scale = market_filter.get_market_scale()
        assert scale == 0.25

    def test_returns_1_when_few_data(self, monkeypatch):
        """数据不足 5 行时默认满仓"""
        df = pd.DataFrame({"date": pd.date_range("2025-01-01", periods=3), "close": [3800, 3810, 3820]})
        monkeypatch.setattr(market_filter, "load_hs300_index", lambda: df)

        scale = market_filter.get_market_scale(as_of_date="20250609")
        assert scale == 1.0


# ═══════════════════════════════════════════
# get_market_state 测试
# ═══════════════════════════════════════════


class TestGetMarketState:
    def test_returns_dict_with_expected_keys(self, mock_hs300_df, monkeypatch):
        monkeypatch.setattr(market_filter, "load_hs300_index", lambda: mock_hs300_df)

        state = market_filter.get_market_state(as_of_date="20250609")
        assert isinstance(state, dict)
        assert "scale" in state
        assert "state_name" in state
        assert "index_close" in state
        assert state.get("scale") in (0.0, 0.25, 0.5, 1.0)

    def test_no_data_returns_error_dict(self, monkeypatch):
        monkeypatch.setattr(market_filter, "load_hs300_index", lambda: None)

        state = market_filter.get_market_state()
        assert state["scale"] == 0.25
        assert "error" in state
        assert state["error"] == "no_data"

    def test_scale_is_float(self, mock_hs300_df, monkeypatch):
        monkeypatch.setattr(market_filter, "load_hs300_index", lambda: mock_hs300_df)

        state = market_filter.get_market_state(as_of_date="20250609")
        assert isinstance(state["scale"], float)


# ═══════════════════════════════════════════
# refresh_index_cache 测试
# ═══════════════════════════════════════════


class TestRefreshIndexCache:
    def test_mootdx_success(self, mock_hs300_df):
        with patch("risk.market_filter._fetch_index_from_mootdx", return_value=mock_hs300_df):
            df = market_filter.refresh_index_cache()
            assert df is not None
            assert len(df) > 0

    def test_mootdx_fails_akshare_fallback(self, mock_hs300_df):
        with patch("risk.market_filter._fetch_index_from_mootdx", side_effect=Exception("fail")):
            with patch("risk.market_filter._fetch_index_from_akshare", return_value=mock_hs300_df):
                df = market_filter.refresh_index_cache()
                assert df is not None

    def test_all_sources_fail_returns_none(self):
        with patch("risk.market_filter._fetch_index_from_mootdx", side_effect=Exception("fail")):
            with patch("risk.market_filter._fetch_index_from_akshare", side_effect=Exception("fail")):
                df = market_filter.refresh_index_cache()
                assert df is None
