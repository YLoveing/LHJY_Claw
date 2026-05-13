# -*- coding: utf-8 -*-
"""多因子评分单元测试 + 统一测试入口"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from screener.stock_screener import screen_score, llm_multi_factor_score


# ── 技术评分 screen_score ──

def test_screen_score_neutral():
    """基础评分应为 50（无特殊条件时）。"""
    ind = {
        "MA5": 10, "MA10": 10, "MA20": 10,
        "RSI": 50, "vol_ratio": 1.0, "price_MA20_pct": 0,
        "_n": 20,
    }
    s = screen_score(ind)
    assert 40 <= s <= 60, f"neutral score should be ~50, got {s}"


def test_screen_score_uptrend_boost():
    """多头排列应提升评分。"""
    ind_base = {
        "MA5": 10, "MA10": 10, "MA20": 10,
        "RSI": 50, "vol_ratio": 1.0, "price_MA20_pct": 0,
        "_n": 20,
    }
    base = screen_score(ind_base)

    ind_up = dict(ind_base, MA5=12, MA10=11, MA20=10)
    up = screen_score(ind_up)
    assert up >= base, f"uptrend should boost score {up} >= {base}"


def test_screen_score_oversold_boost():
    """超卖状态应提升评分。"""
    ind = {
        "MA5": 10, "MA10": 10, "MA20": 10,
        "RSI": 25, "vol_ratio": 1.0, "price_MA20_pct": 0,
        "_n": 20,
    }
    s = screen_score(ind)
    assert s > 50, f"oversold should boost >50, got {s}"


def test_screen_score_overbought_penalty():
    """超买状态应降低评分。"""
    ind = {
        "MA5": 10, "MA10": 10, "MA20": 10,
        "RSI": 80, "vol_ratio": 1.0, "price_MA20_pct": 0,
        "_n": 20,
    }
    s = screen_score(ind)
    assert s < 55, f"overbought should reduce score, got {s}"


def test_screen_score_clamped():
    """评分应在 0-100 范围内。"""
    ind = {"MA5": 100, "MA10": 1, "MA20": 1, "RSI": 95, "vol_ratio": 10, "price_MA20_pct": 50, "_n": 20}
    s = screen_score(ind)
    assert 0 <= s <= 100, f"score out of range [0,100]: {s}"


# ── LLM 多因子评分（错误处理+回退） ──

def test_llm_score_empty():
    """空列表应返回空。"""
    result = llm_multi_factor_score([])
    assert result == [], f"expected empty list, got {result}"


def test_llm_score_fallback():
    """LLM 不可用时应按技术评分排序回退。"""
    stocks = [
        {"code": "000001", "name": "平安", "screen_score": 80, "price": 10, "change_pct": 1, "conditions": ["多头"], "indicators": {}},
        {"code": "000002", "name": "万科", "screen_score": 40, "price": 8, "change_pct": -1, "conditions": [], "indicators": {}},
    ]
    result = llm_multi_factor_score(stocks)
    assert len(result) == 2
    # 应优先放回退排序
    assert result[0]["code"] == "000001", f"expected 000001 first, got {result[0]['code']}"


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
    print(f"\n📊 评分测试: {status}")
    sys.exit(failures)
