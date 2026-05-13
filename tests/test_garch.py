# -*- coding: utf-8 -*-
"""GARCH(1,1) 单元测试 — 适配实际函数签名"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["SKIP_GARCH_IO"] = "1"

import math
import numpy as np
from quant_engine.garch import (
    _garch_llh,
    estimate_garch,
    get_dynamic_thresholds,
    GARCHResult,
)


# ── GARCHResult 数据类 ──

def test_garch_result_creation():
    """GARCHResult 可以直接构造。"""
    r = GARCHResult(
        omega=0.01, alpha=0.1, beta=0.85,
        se_omega=0.005, se_alpha=0.02, se_beta=0.03,
        unconditional_vol=0.20, cond_vol_series=[0.15, 0.18, 0.20],
        current_volatility=0.18, vol_percentile=50.0,
        log_likelihood=-123.45, n_obs=252,
    )
    assert isinstance(r, GARCHResult)
    assert r.status == "ok"  # default


def test_garch_result_fields():
    """GARCHResult 包含所有必要字段。"""
    r = estimate_garch(stock_code=None)
    for attr in ["omega", "alpha", "beta", "se_omega", "se_alpha", "se_beta",
                 "current_volatility", "unconditional_vol",
                 "vol_percentile", "n_obs", "status"]:
        assert hasattr(r, attr), f"missing field: {attr}"


# ── Log-likelihood ──

def test_garch_llh_shape():
    rets = np.array([0.01, -0.02, 0.015, -0.005, 0.008])
    llh = _garch_llh(np.array([0.01, 0.1, 0.85]), rets)
    assert isinstance(llh, (float, np.floating)), f"expected float, got {type(llh)}"
    assert math.isfinite(llh)


def test_garch_llh_stability():
    rng = np.random.default_rng(42)
    rets = rng.normal(0, 0.02, 100)
    for alpha in [0.05, 0.10]:
        for beta in [0.80, 0.85]:
            if alpha + beta >= 1:
                continue
            llh = _garch_llh(np.array([0.01, alpha, beta]), rets)
            assert math.isfinite(llh)


def test_garch_llh_nonstationary():
    """α+β>=1 不应崩溃。"""
    rets = np.array([0.01, -0.02, 0.015])
    llh = _garch_llh(np.array([0.01, 0.5, 0.6]), rets)
    assert isinstance(llh, (float, np.floating))


# ── estimate_garch ──

def test_estimate_garch_runs():
    """整体估计：不崩溃，返回有效结果。"""
    result = estimate_garch(stock_code=None)
    assert isinstance(result, GARCHResult)
    # 非交易时间可能无数据，但不崩即可
    assert hasattr(result, "status")


def test_vol_percentile_bounds():
    """vol_percentile 应在 0-100 范围。"""
    result = estimate_garch(stock_code=None)
    vp = result.vol_percentile
    assert 0 <= vp <= 100, f"vol_percentile={vp} out of range"


# ── Dynamic thresholds ──

def test_thresholds_return_type():
    """返回 (buy, sell) 元组。"""
    t = get_dynamic_thresholds()
    assert isinstance(t, tuple) and len(t) == 2
    buy, sell = t
    assert isinstance(buy, int)
    assert isinstance(sell, int)


def test_thresholds_buy_ge_sell():
    """买阈值应 ≥ 卖阈值。"""
    buy, sell = get_dynamic_thresholds()
    assert buy >= sell, f"buy={buy} < sell={sell}"


def test_thresholds_high_vol():
    """高波动率应提高阈值。"""
    result_low = GARCHResult(
        omega=0.01, alpha=0.1, beta=0.85,
        se_omega=0.005, se_alpha=0.02, se_beta=0.03,
        unconditional_vol=0.15, cond_vol_series=[0.15],
        current_volatility=0.15, vol_percentile=10.0,
        log_likelihood=-100.0, n_obs=252,
    )
    result_high = GARCHResult(
        omega=0.01, alpha=0.1, beta=0.85,
        se_omega=0.005, se_alpha=0.02, se_beta=0.03,
        unconditional_vol=0.35, cond_vol_series=[0.35],
        current_volatility=0.35, vol_percentile=95.0,
        log_likelihood=-100.0, n_obs=252,
    )
    buy_low, sell_low = get_dynamic_thresholds(result_low)
    buy_high, sell_high = get_dynamic_thresholds(result_high)
    assert buy_high >= buy_low, f"high vol buy={buy_high} < low vol buy={buy_low}"


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
    print(f"\n📊 GARCH 测试: {status}")
    sys.exit(failures)
