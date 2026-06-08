# -*- coding: utf-8 -*-
"""MarketAnalyzer 单元测试 — 大盘复盘分析器"""

import sys
from pathlib import Path
from datetime import date
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.market_analyzer import (
    MarketAnalyzer,
    MarketOverview,
    MarketIndex,
)


# ═══════════════════════════════════════════
# MarketIndex 测试
# ═══════════════════════════════════════════

class TestMarketIndex:
    def test_to_dict_contains_all_fields(self):
        idx = MarketIndex(
            code="sh000001",
            name="上证指数",
            current=3350.50,
            change=15.30,
            change_pct=0.46,
            open=3340.0,
            high=3360.0,
            low=3335.0,
            prev_close=3335.20,
            volume=15000000000.0,
            amount=280000000000.0,
            amplitude=0.75,
        )
        d = idx.to_dict()
        assert d["code"] == "sh000001"
        assert d["name"] == "上证指数"
        assert d["current"] == 3350.50
        assert d["change_pct"] == 0.46


# ═══════════════════════════════════════════
# MarketOverview 测试
# ═══════════════════════════════════════════

class TestMarketOverview:
    def test_default_values(self):
        overview = MarketOverview(date="2025-06-09")
        assert overview.date == "2025-06-09"
        assert overview.indices == []
        assert overview.up_count == 0
        assert overview.down_count == 0

    def test_with_indices(self):
        idx = MarketIndex(code="sh000001", name="上证指数", current=3350.0)
        overview = MarketOverview(date="2025-06-09", indices=[idx])
        assert len(overview.indices) == 1
        assert overview.indices[0].name == "上证指数"


# ═══════════════════════════════════════════
# MarketAnalyzer 测试
# ═══════════════════════════════════════════

class TestMarketAnalyzer:
    @pytest.fixture
    def analyzer(self):
        """创建不依赖外部服务的 MarketAnalyzer 实例。"""
        with patch("src.market_analyzer.get_config") as mock_cfg:
            mock_cfg.return_value = MagicMock()
            with patch("src.market_analyzer.get_profile") as mock_profile:
                mock_profile.return_value = MagicMock(
                    has_market_stats=True,
                    has_sector_rankings=True,
                    mood_index_code="000001",
                    news_queries=["大盘走势"],
                    prompt_index_hint="分析上证指数",
                )
                with patch("src.market_analyzer.get_market_strategy_blueprint") as mock_strategy:
                    mock_strategy.return_value = MagicMock()
                    # Mock DataFetcherManager to avoid real data source init
                    with patch("src.market_analyzer.DataFetcherManager") as mock_dm:
                        mock_dm.return_value = MagicMock()
                        return MarketAnalyzer(
                            search_service=MagicMock(),
                            analyzer=None,
                            region="cn",
                        )

    def test_init_defaults(self, analyzer):
        assert analyzer.region == "cn"
        assert analyzer.search_service is not None
        assert analyzer.analyzer is None

    def test_get_review_language_cn(self, analyzer):
        lang = analyzer._get_review_language()
        assert lang in ("zh", "en")

    def test_get_market_scope_name(self, analyzer):
        name = analyzer._get_market_scope_name("zh")
        assert "A股" in name

    def test_format_turnover_value(self, analyzer):
        result = analyzer._format_turnover_value(890000000000)
        assert "8900" in result or "0.89" in result

    def test_format_optional_number_valid(self):
        assert MarketAnalyzer._format_optional_number(10.5) == "10.50"

    def test_format_optional_number_zero(self):
        assert MarketAnalyzer._format_optional_number(0.0) == "N/A"

    def test_format_optional_pct_valid(self):
        assert MarketAnalyzer._format_optional_pct(5.5) == "5.50%"

    def test_format_optional_pct_zero(self):
        assert MarketAnalyzer._format_optional_pct(0.0) == "N/A"

    def test_format_signed_pct(self):
        assert MarketAnalyzer._format_signed_pct(3.5) == "+3.50%"
        assert MarketAnalyzer._format_signed_pct(-1.2) == "-1.20%"

    def test_format_signed_pct_invalid(self):
        assert MarketAnalyzer._format_signed_pct("N/A") == "N/A"

    def test_escape_table_cell(self):
        assert MarketAnalyzer._escape_table_cell("a|b") == "a\\|b"

    def test_build_temperature_bar(self):
        bar = MarketAnalyzer._build_temperature_bar(75)
        assert "█" in bar
        assert "░" in bar
        assert len(bar) == 10

    def test_build_temperature_bar_extreme(self):
        assert MarketAnalyzer._build_temperature_bar(0) == "░" * 10
        assert MarketAnalyzer._build_temperature_bar(100) == "█" * 10

    def test_describe_turnover(self):
        assert "活跃" in MarketAnalyzer._describe_turnover(20000)
        assert "中等" in MarketAnalyzer._describe_turnover(10000)
        assert "缩量" in MarketAnalyzer._describe_turnover(5000)
        assert MarketAnalyzer._describe_turnover(0) == "暂无数据"

    def test_build_market_temperature_range(self, analyzer):
        overview = MarketOverview(
            date="2025-06-09",
            up_count=3000,
            down_count=1000,
            flat_count=200,
            limit_up_count=50,
            limit_down_count=10,
            total_amount=9000.0,
            indices=[
                MarketIndex(code="sh000001", name="上证指数", change_pct=1.5),
            ],
        )
        score, label = analyzer._build_market_temperature(overview)
        assert 0 <= score <= 100
        assert isinstance(label, str)
        assert len(label) > 0

    def test_build_market_temperature_empty(self, analyzer):
        overview = MarketOverview(date="2025-06-09")
        score, label = analyzer._build_market_temperature(overview)
        assert 0 <= score <= 100  # 即使无数据也应返回有效范围

    def test_build_market_light_snapshot_returns_dict(self, analyzer):
        overview = MarketOverview(
            date="2025-06-09",
            up_count=2500,
            down_count=1800,
            flat_count=300,
            total_amount=8900.0,
            indices=[
                MarketIndex(code="sh000001", name="上证指数", change_pct=0.5),
            ],
        )
        light = analyzer.build_market_light_snapshot(overview)
        assert "status" in light
        assert "label" in light
        assert "score" in light
        assert "reasons" in light
        assert "guidance" in light
        assert light["status"] in ("green", "yellow", "red")

    def test_insert_after_section(self, analyzer):
        text = "### 一、盘面总览\ncontent here\n### 二、指数结构\nmore content"
        result = MarketAnalyzer._insert_after_section(text, r"###\s*一、", "**插入内容**")
        assert "**插入内容**" in result
        assert "### 一、" in result
        assert "### 二、" in result

    def test_get_market_overview_mocked(self, analyzer):
        analyzer.data_manager.get_main_indices.return_value = [
            {
                "code": "sh000001", "name": "上证指数",
                "current": 3350.0, "change": 15.0, "change_pct": 0.45,
                "open": 3340.0, "high": 3360.0, "low": 3335.0,
                "prev_close": 3335.0, "volume": 1e10, "amount": 2.8e11,
                "amplitude": 0.75,
            },
        ]
        analyzer.data_manager.get_market_stats.return_value = {
            "up_count": 2500, "down_count": 1800, "flat_count": 300,
            "limit_up_count": 45, "limit_down_count": 12, "total_amount": 8900.0,
        }
        analyzer.data_manager.get_sector_rankings.return_value = (
            [{"name": "半导体", "change_pct": 3.5}],
            [{"name": "银行", "change_pct": -1.5}],
        )

        overview = analyzer.get_market_overview()
        assert isinstance(overview, MarketOverview)
        assert overview.date is not None
        assert len(overview.indices) >= 1
        assert overview.indices[0].name == "上证指数"

    def test_get_market_overview_handles_exceptions(self, analyzer):
        analyzer.data_manager.get_main_indices.side_effect = Exception("network error")
        analyzer.data_manager.get_market_stats.return_value = {}

        overview = analyzer.get_market_overview()
        assert isinstance(overview, MarketOverview)
        assert overview.indices == []

    def test_search_market_news_empty_when_no_service(self, analyzer):
        analyzer.search_service = None
        news = analyzer.search_market_news()
        assert news == []

    def test_search_market_news_with_service(self, analyzer):
        mock_result = MagicMock()
        mock_result.results = [MagicMock(title="test news", snippet="snippet")]
        analyzer.search_service.search_stock_news.return_value = mock_result

        news = analyzer.search_market_news()
        assert len(news) >= 0

    def test_generate_market_review_without_analyzer(self, analyzer):
        overview = MarketOverview(
            date="2025-06-09",
            indices=[
                MarketIndex(code="sh000001", name="上证指数", change_pct=0.5),
            ],
            up_count=2500, down_count=1800,
        )
        # 无 AI analyzer 应回退到模板
        review = analyzer.generate_market_review(overview, [])
        assert "大盘复盘" in review or "Market Recap" in review

    def test_build_stats_block_returns_empty_when_no_data(self, analyzer):
        overview = MarketOverview(date="2025-06-09")
        # 用中文语言
        with patch.object(analyzer, "_get_review_language", return_value="zh"):
            block = analyzer._build_stats_block(overview)
            assert block == ""

    def test_build_indices_block_returns_empty_when_no_indices(self, analyzer):
        overview = MarketOverview(date="2025-06-09")
        assert analyzer._build_indices_block(overview) == ""

    def test_build_sector_block_returns_empty_when_no_sectors(self, analyzer):
        overview = MarketOverview(date="2025-06-09")
        assert analyzer._build_sector_block(overview) == ""

    def test_build_news_block_returns_empty_when_no_news(self, analyzer):
        assert analyzer._build_news_block([]) == ""
