# -*- coding: utf-8 -*-
"""策略引擎单元测试"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategies.engine import StrategyEngine, _check_trend_bullish, _check_volume_breakout, _check_near_ma20


# ── 内置检查函数 ──

def test_trend_bullish_detected():
    ind = {"MA5": 11, "MA10": 10, "MA20": 9}
    assert _check_trend_bullish(ind) is True


def test_trend_bullish_not():
    ind = {"MA5": 9, "MA10": 10, "MA20": 11}
    assert _check_trend_bullish(ind) is False


def test_volume_breakout_detected():
    ind = {"vol_ratio": 2.0, "price": 11, "MA20": 10}
    assert _check_volume_breakout(ind) is True


def test_volume_breakout_low_vol():
    ind = {"vol_ratio": 1.0, "price": 11, "MA20": 10}
    assert _check_volume_breakout(ind) is False


def test_near_ma20_inside():
    ind = {"price": 10.2, "MA20": 10.0}  # +2%
    assert _check_near_ma20(ind) is True


def test_near_ma20_exact():
    ind = {"price": 10.0, "MA20": 10.0}
    assert _check_near_ma20(ind) is True


def test_near_ma20_too_far():
    ind = {"price": 12.0, "MA20": 10.0}
    assert _check_near_ma20(ind) is False


# ── 引擎加载 ──

def test_engine_load_strategies():
    engine = StrategyEngine()
    strats = engine.load_strategies()
    assert len(strats) >= 10, f"expected >=10 strategies, got {len(strats)}"


def test_engine_load_names():
    engine = StrategyEngine()
    strats = engine.load_strategies()
    names = {s.name for s in strats}
    assert "bull_trend" in names, f"bull_trend not found in {names}"
    assert "trend_following" in names, f"trend_following not found in {names}"
    assert "volume_breakout" in names, f"volume_breakout not found in {names}"


# ── 策略匹配 ──

def test_match_bullish_stock():
    engine = StrategyEngine()
    engine.load_strategies()
    stock = {
        "code": "600000",
        "indicators": {
            "MA5": 11, "MA10": 10, "MA20": 9,
            "RSI": 55, "vol_ratio": 1.5, "price_MA20_pct": 1.0,
            "price": 10.5, "trend_status": "uptrend",
        }
    }
    matched = engine.match_strategies(stock, top_n=3)
    assert isinstance(matched, list)
    names = [m["name"] for m in matched]
    assert "bull_trend" in names, f"bull_trend should match, got {names}"
    for m in matched:
        assert "match_score" in m
        assert m["match_score"] > 0


def test_match_empty_stock():
    engine = StrategyEngine()
    engine.load_strategies()
    stock = {"code": "999999", "indicators": {}}
    matched = engine.match_strategies(stock, min_score=10)
    assert isinstance(matched, list)
    # 空指标不应匹配任何策略
    for m in matched:
        assert m["match_score"] < 10 or len(m["matched_keywords"]) == 0


# ── Runner ──

if __name__ == "__main__":
    import traceback
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                func()
                print(f"  ✅ {name}")
            except Exception:
                print(f"  ❌ {name}")
                traceback.print_exc()
                failures += 1
    status = "全部通过" if failures == 0 else f"{failures} 个失败"
    print(f"\n📊 策略引擎测试: {status}")
    sys.exit(failures)
