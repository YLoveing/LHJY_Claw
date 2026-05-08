#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
量化引擎统一入口 — GARCH + HMM + 马科维茨

在 run_and_send.sh 中调用（先于模拟交易）:
    python3 quant_engine/run_quant.py 2>&1

输出:
    quant_engine/garch_output.json      — 波动率 + 动态阈值
    quant_engine/hmm_output.json        — 市场状态 + 概率
    quant_engine/markowitz_output.json  — 最优权重 + 组合指标
"""

import json
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="[%(name)s] %(message)s",
    stream=sys.stdout,
)

_BASE_DIR = Path("/opt/daily_stock_analysis")
_STATE_FILE = _BASE_DIR / "simulated_trading" / "state.json"


def run_all():
    """依次运行三个量化模型，汇总输出。"""
    from .garch import run_garch_and_save, load_garch_output
    from .hmm_market import run_hmm_and_save, load_hmm_output
    from .markowitz import run_markowitz_and_save, load_markowitz_output
    
    print("=" * 50)
    print("📊 量化引擎启动")
    print("=" * 50)
    
    # 1. GARCH
    print("\n[1/3] GARCH(1,1) MLE 波动率估计...")
    garch_out = run_garch_and_save()
    go = garch_out
    if go.get("status") == "ok":
        print(f"  ✅ 当前条件波动率: {go['current_volatility']:.2%}")
        print(f"  ✅ 波动率百分位: {go['vol_percentile']:.0f}%")
        print(f"  ✅ 参数: ω={go['omega']:.6f} α={go['alpha']:.4f} β={go['beta']:.4f}")
        print(f"  ✅ 标准误: ω±{go['se_omega']:.6f} α±{go['se_alpha']:.4f} β±{go['se_beta']:.4f}")
        print(f"  ✅ 动态阈值: 买入≥{go['dynamic_thresholds']['buy']} / 卖出≤{go['dynamic_thresholds']['sell']}")
    else:
        print(f"  ⚠️ {go.get('status', 'unknown')}")
    
    # 2. HMM
    print("\n[2/3] HMM 市场状态检测 (多起点 Baum-Welch + Viterbi)...")
    hmm_out = run_hmm_and_save()
    ho = hmm_out
    if ho.get("status") == "ok" or "insufficient" in ho.get("status", ""):
        state = ho.get("current_state", 2)
        probs = ho.get("state_probabilities", [0.25, 0.25, 0.25, 0.25])
        sn = {0: "多头", 1: "空头", 2: "震荡", 3: "高波动"}
        print(f"  ✅ 当前状态: {sn.get(state, '?')}")
        print(f"  ✅ 状态概率: 多头{probs[0]:.1%} 空头{probs[1]:.1%} 震荡{probs[2]:.1%} 高波动{probs[3]:.1%}")
        print(f"  ✅ 对数似然: {ho.get('log_likelihood', 'N/A')}")
    else:
        print(f"  ⚠️ {ho.get('status', 'unknown')}")
    
    # 3. 马科维茨
    stock_codes = None
    if _STATE_FILE.exists():
        try:
            state = json.loads(_STATE_FILE.read_text())
            sc = list(state.get("positions", {}).keys())
            if len(sc) >= 2:
                stock_codes = sc
            elif len(sc) == 1:
                # 单只持仓时用全市场估值
                stock_codes = None
        except (json.JSONDecodeError, IOError):
            pass
    
    print(f"\n[3/3] 马科维茨均值-方差优化 (股票: {stock_codes or '全部'})...")
    m_out = run_markowitz_and_save(stock_codes)
    mo = m_out
    if mo.get("status") == "ok":
        w = mo["weights"]
        print(f"  ✅ 最优权重: {w}")
        print(f"  ✅ 最小方差权重: {mo.get('min_var_weights', {})}")
        print(f"  ✅ 预期组合收益: {mo['expected_return']:.2%}")
        print(f"  ✅ 预期组合波动: {mo['expected_volatility']:.2%}")
        print(f"  ✅ 夏普比率: {mo['sharpe_ratio']:.3f}")
    elif mo.get("status") == "insufficient_data":
        print(f"  ⚠️ {mo.get('message', '数据不足')}")
    else:
        print(f"  ⚠️ {mo.get('status', 'unknown')}")
    
    # 汇总
    print("\n" + "=" * 50)
    print("📊 量化引擎完成")
    print("=" * 50)
    
    return {
        "garch": load_garch_output(),
        "hmm": load_hmm_output(),
        "markowitz": load_markowitz_output(),
    }


if __name__ == "__main__":
    run_all()
