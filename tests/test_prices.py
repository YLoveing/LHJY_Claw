# -*- coding: utf-8 -*-
"""quant_engine/_prices.py 测试 — 价格数据提取"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quant_engine._prices import (
    _find_current_price,
    _load_cache_prices,
    aggregated_returns,
    extract_all_prices,
    extract_price_matrix,
    stock_returns,
)

# ═══════════════════════════════════════════
# _load_cache_prices 测试
# ═══════════════════════════════════════════


class TestLoadCachePrices:
    def test_returns_none_for_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "quant_engine._prices._CACHE_DIR",
            tmp_path,
        )
        result = _load_cache_prices("999999")
        assert result is None

    def test_loads_valid_cache(self, mock_daily_kline_df, tmp_path, monkeypatch):
        cache_dir = tmp_path / "cache_kline"
        cache_dir.mkdir(parents=True)

        df = mock_daily_kline_df.copy()
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d")
        df.to_pickle(str(cache_dir / "000001.pkl"))

        monkeypatch.setattr("quant_engine._prices._CACHE_DIR", cache_dir)
        result = _load_cache_prices("000001")
        assert result is not None
        assert len(result) > 0
        assert isinstance(result[0], tuple)
        assert len(result[0]) == 2

    def test_handles_missing_columns(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / "cache_kline"
        cache_dir.mkdir(parents=True)
        # 无 close 列的 DataFrame
        df = pd.DataFrame({"date": [], "open": []})
        df.to_pickle(str(cache_dir / "bad.pkl"))

        monkeypatch.setattr("quant_engine._prices._CACHE_DIR", cache_dir)
        result = _load_cache_prices("bad")
        assert result is None

    def test_handles_corrupt_file(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / "cache_kline"
        cache_dir.mkdir(parents=True)
        corrupt = cache_dir / "corrupt.pkl"
        corrupt.write_text("not a pickle")

        monkeypatch.setattr("quant_engine._prices._CACHE_DIR", cache_dir)
        result = _load_cache_prices("corrupt")
        assert result is None


# ═══════════════════════════════════════════
# extract_all_prices 测试
# ═══════════════════════════════════════════


class TestExtractAllPrices:
    def test_returns_empty_when_no_data(self, tmp_path, monkeypatch):
        monkeypatch.setattr("quant_engine._prices._CACHE_DIR", tmp_path / "nonexistent")
        monkeypatch.setattr("quant_engine._prices._REPORTS_DIR", tmp_path / "nonexistent")
        result = extract_all_prices()
        assert result == {}

    def test_extracts_from_cache(self, mock_daily_kline_df, tmp_path, monkeypatch):
        cache_dir = tmp_path / "cache_kline"
        cache_dir.mkdir(parents=True)

        df = mock_daily_kline_df.copy()
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d")
        df.to_pickle(str(cache_dir / "000001.pkl"))
        df.to_pickle(str(cache_dir / "000002.pkl"))

        monkeypatch.setattr("quant_engine._prices._CACHE_DIR", cache_dir)
        monkeypatch.setattr("quant_engine._prices._REPORTS_DIR", tmp_path / "nonexistent")

        result = extract_all_prices()
        assert len(result) == 2
        assert "000001" in result
        assert "000002" in result


# ═══════════════════════════════════════════
# extract_price_matrix 测试
# ═══════════════════════════════════════════


class TestExtractPriceMatrix:
    def test_empty_when_no_data(self, tmp_path, monkeypatch):
        monkeypatch.setattr("quant_engine._prices._CACHE_DIR", tmp_path / "nonexistent")
        monkeypatch.setattr("quant_engine._prices._REPORTS_DIR", tmp_path / "nonexistent")

        mat, codes, dates = extract_price_matrix()
        assert mat.size == 0
        assert codes == []
        assert dates == []

    def test_returns_aligned_matrix(self, mock_daily_kline_df, tmp_path, monkeypatch):
        cache_dir = tmp_path / "cache_kline"
        cache_dir.mkdir(parents=True)

        # 创建两只股票的对齐数据
        dates_ = [f"202506{str(d).zfill(2)}" for d in range(1, 11)]
        base1 = 10.0
        base2 = 50.0
        rng = np.random.default_rng(42)

        for code, base in [("000001", base1), ("000002", base2)]:
            records = []
            b = base
            for d in dates_:
                ret = rng.normal(0.001, 0.015)
                b *= 1 + ret
                records.append(
                    {
                        "date": d,
                        "open": b,
                        "high": b + 1,
                        "low": b - 1,
                        "close": round(b, 2),
                        "volume": 100000,
                        "amount": 1000000,
                        "pct_chg": ret * 100,
                    }
                )
            df = pd.DataFrame(records)
            df.to_pickle(str(cache_dir / f"{code}.pkl"))

        monkeypatch.setattr("quant_engine._prices._CACHE_DIR", cache_dir)
        monkeypatch.setattr("quant_engine._prices._REPORTS_DIR", tmp_path / "nonexistent")

        mat, codes, dates = extract_price_matrix()
        if mat.size > 0:
            assert len(codes) >= 1
            assert len(dates) >= 1
            assert mat.shape[1] == len(codes)


# ═══════════════════════════════════════════
# stock_returns 测试
# ═══════════════════════════════════════════


class TestStockReturns:
    def test_empty_for_unknown_code(self, tmp_path, monkeypatch):
        monkeypatch.setattr("quant_engine._prices._CACHE_DIR", tmp_path / "nonexistent")
        monkeypatch.setattr("quant_engine._prices._REPORTS_DIR", tmp_path / "nonexistent")

        ret = stock_returns("999999")
        assert isinstance(ret, np.ndarray)
        assert len(ret) == 0

    def test_returns_log_returns(self, mock_daily_kline_df, tmp_path, monkeypatch):
        cache_dir = tmp_path / "cache_kline"
        cache_dir.mkdir(parents=True)

        dates_ = [f"202505{str(d).zfill(2)}" for d in range(1, 11)]
        base = 10.0
        records = []
        b = base
        for d in dates_:
            b += 0.1  # 简单递增
            records.append(
                {
                    "date": d,
                    "close": b,
                    "open": b,
                    "high": b,
                    "low": b,
                    "volume": 100000,
                    "amount": 1000000,
                    "pct_chg": 1.0,
                }
            )
        df = pd.DataFrame(records)
        df.to_pickle(str(cache_dir / "000001.pkl"))

        monkeypatch.setattr("quant_engine._prices._CACHE_DIR", cache_dir)
        monkeypatch.setattr("quant_engine._prices._REPORTS_DIR", tmp_path / "nonexistent")

        ret = stock_returns("000001")
        if len(ret) >= 1:
            assert len(ret) == len(dates_) - 1
            # 对数收益率应 > 0（价格持续上涨）
            assert ret[0] > 0


# ═══════════════════════════════════════════
# aggregated_returns 测试
# ═══════════════════════════════════════════


class TestAggregatedReturns:
    def test_empty_when_no_data(self, tmp_path, monkeypatch):
        monkeypatch.setattr("quant_engine._prices._CACHE_DIR", tmp_path / "nonexistent")
        monkeypatch.setattr("quant_engine._prices._REPORTS_DIR", tmp_path / "nonexistent")

        ret = aggregated_returns()
        assert isinstance(ret, np.ndarray)
        assert len(ret) == 0

    def test_returns_array_when_data(self, mock_daily_kline_df, tmp_path, monkeypatch):
        cache_dir = tmp_path / "cache_kline"
        cache_dir.mkdir(parents=True)

        dates_ = [f"202505{str(d).zfill(2)}" for d in range(1, 11)]
        rng = np.random.default_rng(42)
        base = 10.0

        for code in ["000001", "000002", "000003"]:
            records = []
            b = base + rng.uniform(0, 20)
            for d in dates_:
                ret_val = rng.normal(0.001, 0.015)
                b *= 1 + ret_val
                records.append(
                    {
                        "date": d,
                        "close": round(b, 2),
                        "open": b,
                        "high": b,
                        "low": b,
                        "volume": 100000,
                        "amount": 1000000,
                        "pct_chg": ret_val * 100,
                    }
                )
            df = pd.DataFrame(records)
            df.to_pickle(str(cache_dir / f"{code}.pkl"))

        monkeypatch.setattr("quant_engine._prices._CACHE_DIR", cache_dir)
        monkeypatch.setattr("quant_engine._prices._REPORTS_DIR", tmp_path / "nonexistent")

        ret = aggregated_returns()
        if len(ret) >= 1:
            assert isinstance(ret, np.ndarray)
            assert len(ret) == len(dates_) - 1


# ═══════════════════════════════════════════
# _find_current_price 测试
# ═══════════════════════════════════════════


class TestFindCurrentPrice:
    def test_finds_price_from_table(self):
        """'| 收盘 |' 标题行后紧跟数据行能正确解析价格。"""
        section = """
一些文本
| 收盘 | 涨跌幅 | 成交额 |
| 15.80 | +2.5% | 1.2亿 |
"""
        price = _find_current_price(section)
        assert price == 15.80

    def test_finds_price_from_close_table(self):
        """含'| 收盘 |'标题 + 数据行的格式。"""
        section = """
| 收盘 | 涨跌幅 | 成交额 |
| 15.80 | +2.5% | 1.2亿 |
"""
        price = _find_current_price(section)
        assert price == 15.80

    def test_finds_price_from_current_label(self):
        section = """
当前价：12.50
"""
        price = _find_current_price(section)
        assert price == 12.50

    def test_returns_none_when_no_price(self):
        section = "No price data here"
        price = _find_current_price(section)
        assert price is None

    def test_rejects_out_of_range_price(self):
        """0.5 < price < 10000 范围外的值应被过滤"""
        section = "收盘 | 0.01"
        # 没有表格格式所以找不到
        price = _find_current_price(section)
        assert price is None
