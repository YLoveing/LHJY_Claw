# -*- coding: utf-8 -*-
"""BacktestEngine 单元测试 — 核心回测引擎"""

import sys
from datetime import date
from pathlib import Path
from typing import List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.backtest_engine import (
    OVERALL_SENTINEL_CODE,
    BacktestEngine,
    EvaluationConfig,
)

# ═══════════════════════════════════════════
# 操作建议推断测试
# ═══════════════════════════════════════════


class TestInferDirection:
    def test_bullish_advice_returns_up(self):
        assert BacktestEngine.infer_direction_expected("买入") == "up"
        assert BacktestEngine.infer_direction_expected("强烈买入") == "up"
        assert BacktestEngine.infer_direction_expected("buy") == "up"

    def test_bearish_advice_returns_down(self):
        assert BacktestEngine.infer_direction_expected("卖出") == "down"
        assert BacktestEngine.infer_direction_expected("强烈卖出") == "down"
        assert BacktestEngine.infer_direction_expected("sell") == "down"

    def test_wait_advice_returns_flat(self):
        assert BacktestEngine.infer_direction_expected("观望") == "flat"
        assert BacktestEngine.infer_direction_expected("等待") == "flat"

    def test_hold_advice_returns_not_down(self):
        assert BacktestEngine.infer_direction_expected("持有") == "not_down"
        assert BacktestEngine.infer_direction_expected("hold") == "not_down"

    def test_empty_advice_returns_flat(self):
        assert BacktestEngine.infer_direction_expected(None) == "flat"
        assert BacktestEngine.infer_direction_expected("") == "flat"

    def test_negated_bullish_returns_flat(self):
        """否定前缀应阻止看多匹配。"""
        assert BacktestEngine.infer_direction_expected("不要买入") == "flat"
        assert BacktestEngine.infer_direction_expected("不买入") == "flat"

    def test_negated_bearish_returns_flat(self):
        assert BacktestEngine.infer_direction_expected("不要卖出") == "flat"
        assert BacktestEngine.infer_direction_expected("do not sell") == "flat"


class TestInferPosition:
    def test_bullish_returns_long(self):
        assert BacktestEngine.infer_position_recommendation("买入") == "long"

    def test_hold_returns_long(self):
        assert BacktestEngine.infer_position_recommendation("持有") == "long"

    def test_bearish_returns_cash(self):
        assert BacktestEngine.infer_position_recommendation("卖出") == "cash"

    def test_wait_returns_cash(self):
        assert BacktestEngine.infer_position_recommendation("观望") == "cash"

    def test_unknown_returns_cash(self):
        assert BacktestEngine.infer_position_recommendation(None) == "cash"
        assert BacktestEngine.infer_position_recommendation("随机文本") == "cash"


# ═══════════════════════════════════════════
# evaluate_single 测试
# ═══════════════════════════════════════════


class TestEvaluateSingle:
    @pytest.fixture
    def config(self):
        return EvaluationConfig(eval_window_days=10)

    def test_bullish_in_uptrend_returns_win(self, config, forward_bars_uptrend):
        result = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2025, 6, 9),
            start_price=10.0,
            forward_bars=forward_bars_uptrend,
            stop_loss=None,
            take_profit=None,
            config=config,
        )
        assert result["eval_status"] == "completed"
        assert result["outcome"] == "win"
        assert result["direction_correct"] is True
        assert result["position_recommendation"] == "long"
        assert result["simulated_return_pct"] > 0

    def test_bullish_in_downtrend_returns_loss(self, config, forward_bars_downtrend):
        result = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2025, 6, 9),
            start_price=10.0,
            forward_bars=forward_bars_downtrend,
            stop_loss=None,
            take_profit=None,
            config=config,
        )
        assert result["eval_status"] == "completed"
        assert result["outcome"] == "loss"
        assert result["direction_correct"] is False

    def test_bullish_in_flat_market_returns_neutral(self, config, forward_bars_flat):
        result = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2025, 6, 9),
            start_price=10.0,
            forward_bars=forward_bars_flat,
            stop_loss=None,
            take_profit=None,
            config=config,
        )
        assert result["eval_status"] == "completed"
        assert result["outcome"] == "neutral"

    def test_bearish_in_downtrend_returns_win(self, config, forward_bars_downtrend):
        result = BacktestEngine.evaluate_single(
            operation_advice="卖出",
            analysis_date=date(2025, 6, 9),
            start_price=10.0,
            forward_bars=forward_bars_downtrend,
            stop_loss=None,
            take_profit=None,
            config=config,
        )
        assert result["position_recommendation"] == "cash"
        # cash position: no simulated entry
        assert result["simulated_return_pct"] == 0.0

    def test_hold_in_downtrend_returns_loss(self, config, forward_bars_downtrend):
        result = BacktestEngine.evaluate_single(
            operation_advice="持有",
            analysis_date=date(2025, 6, 9),
            start_price=10.0,
            forward_bars=forward_bars_downtrend,
            stop_loss=None,
            take_profit=None,
            config=config,
        )
        assert result["direction_expected"] == "not_down"
        assert result["outcome"] in ("loss", "neutral")

    def test_insufficient_data(self, config, short_forward_bars):
        result = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2025, 6, 9),
            start_price=10.0,
            forward_bars=short_forward_bars,
            stop_loss=None,
            take_profit=None,
            config=config,
        )
        assert result["eval_status"] == "insufficient_data"

    def test_invalid_start_price(self, config, forward_bars_uptrend):
        result = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2025, 6, 9),
            start_price=0.0,
            forward_bars=forward_bars_uptrend,
            stop_loss=None,
            take_profit=None,
            config=config,
        )
        assert result["eval_status"] == "error"

    def test_none_start_price(self, config, forward_bars_uptrend):
        result = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2025, 6, 9),
            start_price=None,
            forward_bars=forward_bars_uptrend,
            stop_loss=None,
            take_profit=None,
            config=config,
        )
        assert result["eval_status"] == "error"

    def test_negative_eval_window_raises(self, forward_bars_uptrend):
        with pytest.raises(ValueError, match="positive"):
            BacktestEngine.evaluate_single(
                operation_advice="买入",
                analysis_date=date(2025, 6, 9),
                start_price=10.0,
                forward_bars=forward_bars_uptrend,
                stop_loss=None,
                take_profit=None,
                config=EvaluationConfig(eval_window_days=0),
            )

    def test_stop_loss_hit(self, config, forward_bars_with_stop_loss):
        result = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2025, 6, 9),
            start_price=10.0,
            forward_bars=forward_bars_with_stop_loss,
            stop_loss=8.0,
            take_profit=None,
            config=config,
        )
        assert result["hit_stop_loss"] is True
        assert result["first_hit"] == "stop_loss"
        assert result["simulated_exit_reason"] == "stop_loss"
        assert result["simulated_return_pct"] < 0

    def test_take_profit_hit(self, config, forward_bars_with_take_profit):
        result = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2025, 6, 9),
            start_price=10.0,
            forward_bars=forward_bars_with_take_profit,
            stop_loss=None,
            take_profit=12.0,
            config=config,
        )
        assert result["hit_take_profit"] is True
        assert result["first_hit"] == "take_profit"
        assert result["simulated_exit_reason"] == "take_profit"
        assert result["simulated_return_pct"] > 0

    def test_neither_target_hit_when_none_set(self, config, forward_bars_uptrend):
        result = BacktestEngine.evaluate_single(
            operation_advice="买入",
            analysis_date=date(2025, 6, 9),
            start_price=10.0,
            forward_bars=forward_bars_uptrend,
            stop_loss=None,
            take_profit=None,
            config=config,
        )
        assert result["first_hit"] == "neither"
        assert result["simulated_exit_reason"] == "window_end"

    def test_cash_position_no_targets(self, config, forward_bars_uptrend):
        result = BacktestEngine.evaluate_single(
            operation_advice="卖出",
            analysis_date=date(2025, 6, 9),
            start_price=10.0,
            forward_bars=forward_bars_uptrend,
            stop_loss=8.0,
            take_profit=12.0,
            config=config,
        )
        assert result["first_hit"] == "not_applicable"
        assert result["hit_stop_loss"] is None
        assert result["simulated_return_pct"] == 0.0


# ═══════════════════════════════════════════
# compute_summary 测试
# ═══════════════════════════════════════════


class TestComputeSummary:
    class Result:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    def test_empty_results(self):
        summary = BacktestEngine.compute_summary(
            results=[],
            scope=OVERALL_SENTINEL_CODE,
            code=None,
            eval_window_days=10,
            engine_version="v1",
        )
        assert summary["total_evaluations"] == 0
        assert summary["completed_count"] == 0

    def test_single_win_result(self):
        results = [
            self.Result(
                eval_status="completed",
                position_recommendation="long",
                outcome="win",
                direction_correct=True,
                stock_return_pct=5.0,
                simulated_return_pct=5.0,
                hit_stop_loss=False,
                hit_take_profit=False,
                first_hit="neither",
                first_hit_trading_days=None,
                operation_advice="买入",
            ),
        ]
        summary = BacktestEngine.compute_summary(
            results=results,
            scope="000001",
            code="000001",
            eval_window_days=10,
            engine_version="v1",
        )
        assert summary["completed_count"] == 1
        assert summary["win_count"] == 1
        assert summary["win_rate_pct"] == 100.0
        assert summary["direction_accuracy_pct"] == 100.0

    def test_mixed_results(self):
        results = [
            self.Result(
                eval_status="completed",
                position_recommendation="long",
                outcome="win",
                direction_correct=True,
                stock_return_pct=5.0,
                simulated_return_pct=5.0,
                hit_stop_loss=False,
                hit_take_profit=False,
                first_hit="neither",
                first_hit_trading_days=None,
                operation_advice="买入",
            ),
            self.Result(
                eval_status="completed",
                position_recommendation="long",
                outcome="loss",
                direction_correct=False,
                stock_return_pct=-5.0,
                simulated_return_pct=-5.0,
                hit_stop_loss=True,
                hit_take_profit=False,
                first_hit="stop_loss",
                first_hit_trading_days=3,
                operation_advice="买入",
            ),
            self.Result(
                eval_status="insufficient_data",
                position_recommendation=None,
                outcome=None,
                direction_correct=None,
                stock_return_pct=None,
                simulated_return_pct=None,
                hit_stop_loss=None,
                hit_take_profit=None,
                first_hit=None,
                first_hit_trading_days=None,
                operation_advice="买入",
            ),
        ]
        summary = BacktestEngine.compute_summary(
            results=results,
            scope=OVERALL_SENTINEL_CODE,
            code=None,
            eval_window_days=10,
            engine_version="v1",
        )
        assert summary["total_evaluations"] == 3
        assert summary["completed_count"] == 2
        assert summary["insufficient_count"] == 1
        assert summary["win_count"] == 1
        assert summary["loss_count"] == 1
        assert summary["win_rate_pct"] == 50.0
        assert summary["direction_accuracy_pct"] == 50.0

    def test_stop_loss_trigger_rate(self):
        results = [
            self.Result(
                eval_status="completed",
                position_recommendation="long",
                outcome="loss",
                direction_correct=False,
                stock_return_pct=-10.0,
                simulated_return_pct=-10.0,
                hit_stop_loss=True,
                hit_take_profit=False,
                first_hit="stop_loss",
                first_hit_trading_days=3,
                operation_advice="买入",
            ),
            self.Result(
                eval_status="completed",
                position_recommendation="long",
                outcome="win",
                direction_correct=True,
                stock_return_pct=5.0,
                simulated_return_pct=5.0,
                hit_stop_loss=False,
                hit_take_profit=False,
                first_hit="neither",
                first_hit_trading_days=None,
                operation_advice="买入",
            ),
        ]
        summary = BacktestEngine.compute_summary(
            results=results,
            scope=OVERALL_SENTINEL_CODE,
            code=None,
            eval_window_days=10,
            engine_version="v1",
        )
        assert summary["stop_loss_trigger_rate"] == 50.0

    def test_advice_breakdown_included(self):
        results = [
            self.Result(
                eval_status="completed",
                position_recommendation="long",
                outcome="win",
                direction_correct=True,
                stock_return_pct=5.0,
                simulated_return_pct=5.0,
                hit_stop_loss=False,
                hit_take_profit=False,
                first_hit="neither",
                first_hit_trading_days=None,
                operation_advice="买入",
            ),
        ]
        summary = BacktestEngine.compute_summary(
            results=results,
            scope=OVERALL_SENTINEL_CODE,
            code=None,
            eval_window_days=10,
            engine_version="v1",
        )
        assert "advice_breakdown" in summary
        assert "买入" in summary["advice_breakdown"]


# ═══════════════════════════════════════════
# 辅助方法测试
# ═══════════════════════════════════════════


class TestNormalizeText:
    def test_none_returns_empty(self):
        assert BacktestEngine._normalize_text(None) == ""

    def test_strips_and_lowercases(self):
        assert BacktestEngine._normalize_text("  BUY  ") == "buy"


class TestMatchesIntent:
    def test_exact_match(self):
        assert BacktestEngine._matches_intent("买入", ("买入",)) is True

    def test_substring_without_negation(self):
        assert BacktestEngine._matches_intent("建议买入", ("买入",)) is True

    def test_negated_keyword_returns_false(self):
        assert BacktestEngine._matches_intent("不要买入", ("买入",)) is False

    def test_empty_text_returns_false(self):
        assert BacktestEngine._matches_intent("", ("买入",)) is False

    def test_no_match_returns_false(self):
        assert BacktestEngine._matches_intent("持有观望", ("买入", "卖出")) is False


class TestClassifyOutcome:
    def test_up_direction_large_gain_is_win(self):
        outcome, correct = BacktestEngine._classify_outcome(
            stock_return_pct=5.0,
            direction_expected="up",
            neutral_band_pct=2.0,
        )
        assert outcome == "win"
        assert correct is True

    def test_up_direction_large_loss_is_loss(self):
        outcome, correct = BacktestEngine._classify_outcome(
            stock_return_pct=-5.0,
            direction_expected="up",
            neutral_band_pct=2.0,
        )
        assert outcome == "loss"
        assert correct is False

    def test_up_direction_neutral(self):
        outcome, correct = BacktestEngine._classify_outcome(
            stock_return_pct=1.0,
            direction_expected="up",
            neutral_band_pct=2.0,
        )
        assert outcome == "neutral"
        assert correct is None

    def test_down_direction_large_loss_is_win(self):
        outcome, correct = BacktestEngine._classify_outcome(
            stock_return_pct=-5.0,
            direction_expected="down",
            neutral_band_pct=2.0,
        )
        assert outcome == "win"
        assert correct is True

    def test_not_down_zero_return_is_win(self):
        outcome, correct = BacktestEngine._classify_outcome(
            stock_return_pct=0.0,
            direction_expected="not_down",
            neutral_band_pct=2.0,
        )
        assert outcome == "win"
        assert correct is True

    def test_flat_within_band_is_win(self):
        outcome, correct = BacktestEngine._classify_outcome(
            stock_return_pct=1.0,
            direction_expected="flat",
            neutral_band_pct=2.0,
        )
        assert outcome == "win"
        assert correct is True

    def test_flat_outside_band_is_loss(self):
        outcome, correct = BacktestEngine._classify_outcome(
            stock_return_pct=5.0,
            direction_expected="flat",
            neutral_band_pct=2.0,
        )
        assert outcome == "loss"
        assert correct is False

    def test_none_return_returns_none(self):
        outcome, correct = BacktestEngine._classify_outcome(
            stock_return_pct=None,
            direction_expected="up",
            neutral_band_pct=2.0,
        )
        assert outcome is None
        assert correct is None
