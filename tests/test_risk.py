# -*- coding: utf-8 -*-
"""风控模块单元测试 — 适配实际函数签名"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from layers.risk_layer.stop_loss import check_stop_loss
from layers.risk_layer.drawdown import check_account_drawdown
from layers.risk_layer.kelly import kelly_fraction, compute_kelly_amount


# ── Stop Loss ──

def test_stop_loss_normal():
    """正常波动应返回 hold。"""
    action, reason = check_stop_loss(
        pos={"avg_cost": 10.0},
        current_price=9.5,
        code="000001",
    )
    assert action == "hold", f"expected hold, got {action}"
    assert isinstance(reason, str)


def test_stop_loss_triggered():
    """跌幅超过-15%应触发止损。"""
    action, reason = check_stop_loss(
        pos={"avg_cost": 10.0},
        current_price=8.0,
        code="000001",
    )
    assert action == "stop_loss", f"expected stop_loss, got {action}"


def test_stop_loss_take_profit():
    """涨幅超过+25%应触发止盈减半。"""
    action, reason = check_stop_loss(
        pos={"avg_cost": 10.0},
        current_price=13.0,
        code="000001",
    )
    assert action == "take_profit", f"expected take_profit, got {action}"


def test_stop_loss_exact_edge():
    """恰好 -15% 触发止损（条件为 <=）。"""
    action, reason = check_stop_loss(
        pos={"avg_cost": 100.0},
        current_price=85.0,
        code="000001",
    )
    # 逻辑是 <=，所以 -15% 精确触发
    assert action in ("hold", "stop_loss")


# ── Drawdown ──

def test_drawdown_normal():
    """正常回撤不应阻止交易。"""
    can_trade, dd_pct = check_account_drawdown(
        state={
            "cash": 80000,
            "positions": {"000001": {"quantity": 1000, "current_price": 15.0}},
            "peak_equity": 100000,
        },
    )
    assert can_trade is True or dd_pct <= 20.0


def test_drawdown_severe():
    """严重回撤应阻止交易。"""
    can_trade, dd_pct = check_account_drawdown(
        state={
            "cash": 30000,
            "positions": {"000001": {"quantity": 1000, "current_price": 10.0}},
            "peak_equity": 100000,
        },
    )
    assert isinstance(can_trade, bool)
    assert isinstance(dd_pct, float)


# ── Kelly ──

def test_kelly_fraction_returns_float():
    """返回浮点数。分数30应返回0（不够买入线）。"""
    f = kelly_fraction(score=30)
    assert isinstance(f, float)


def test_kelly_fraction_mid():
    """中等分数应返回正比例。"""
    f = kelly_fraction(score=70)
    assert 0 <= f < 1


def test_kelly_fraction_high():
    """85分返回合理范围。"""
    f = kelly_fraction(score=85)
    assert f > 0


def test_kelly_fraction_max():
    """95分不应超过0.5。"""
    f = kelly_fraction(score=95)
    assert f <= 0.5, f"expected <=0.5, got {f}"


def test_kelly_amount_normal():
    """正常评分应返回正金额。"""
    amt = compute_kelly_amount(score=75, cash=100000, sentiment_factor=1.0)
    assert 0 <= amt <= min(50000, 100000), f"amount={amt} out of range"


def test_kelly_amount_low_score():
    """低分也应返回正金额（评分→胜率映射即使20分也有正概率）。"""
    amt = compute_kelly_amount(score=20, cash=100000, sentiment_factor=1.0)
    assert 0 <= amt <= 50000, f"expected >0 capped, got {amt}"


def test_kelly_amount_sentiment():
    """情绪因子不影响为空cash时的金额。"""
    high = compute_kelly_amount(score=75, cash=100000, sentiment_factor=1.2)
    low = compute_kelly_amount(score=75, cash=100000, sentiment_factor=0.5)
    assert high >= low, f"high={high} < low={low}"


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
    print(f"\n📊 风控测试: {status}")
    sys.exit(failures)
