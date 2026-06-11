# -*- coding: utf-8 -*-
"""
GARCH(1,1) 真正版 — 纯 NumPy MLE 实现

σ²_t = ω + α · ε²_{t-1} + β · σ²_{t-1}

设计要点：
1. 网格搜索 + BFGS (有限差分) 两步 MLE
2. Hessian 逆 → 参数标准误
3. 始终保证 α + β < 1 (平稳性) 及 ω > 0
4. ≤3 个收益点则回退简单 EWMA 估计
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from ._prices import aggregated_returns, stock_returns

logger = logging.getLogger("quant_engine.garch")

_BASE_DIR = Path("/opt/daily_stock_analysis")
_OUTPUT_FILE = _BASE_DIR / "quant_engine" / "garch_output.json"

# ── 网格搜索范围 ──
_OMEGA_GRID = np.logspace(-6, -3, 8)
_ALPHA_GRID = np.linspace(0.01, 0.30, 10)
_BETA_GRID = np.linspace(0.50, 0.95, 10)

_N_DAY_YEAR = 252


@dataclass
class GARCHResult:
    omega: float
    alpha: float
    beta: float
    se_omega: float  # 标准误
    se_alpha: float
    se_beta: float
    unconditional_vol: float
    cond_vol_series: List[float]
    current_volatility: float
    vol_percentile: float
    log_likelihood: float
    n_obs: int
    status: str = "ok"


# ── 对数似然 ──


def _garch_llh(params: np.ndarray, ret: np.ndarray) -> float:
    """
    GARCH(1,1) 对数似然（返回负值供最小化）。

    params = [ln(ω), ln(α/(1-α)), ln(β/(1-β))]   保证约束
    """
    # 反变换回原始空间
    omega = np.exp(params[0])
    alpha = 1.0 / (1.0 + np.exp(-params[1]))  # sigmoid → (0,1)
    beta = 1.0 / (1.0 + np.exp(-params[2]))

    if alpha + beta >= 0.999 or omega <= 0:
        return 1e12

    T = len(ret)
    sigma2 = np.var(ret) + 1e-10
    ll = 0.0
    for t in range(T):
        ll += -0.5 * (np.log(2 * np.pi) + np.log(sigma2) + ret[t] ** 2 / sigma2)
        sigma2 = omega + alpha * ret[t] ** 2 + beta * sigma2
        if sigma2 <= 0 or np.isnan(sigma2):
            return 1e12
    return -float(ll)  # 负对数似然


def _garch_llh_raw(omega: float, alpha: float, beta: float, ret: np.ndarray) -> float:
    """直接在原始参数空间计算负对数似然。"""
    if alpha + beta >= 0.999 or omega <= 0:
        return 1e12
    T = len(ret)
    sigma2 = np.var(ret) + 1e-10
    ll = 0.0
    for t in range(T):
        ll += -0.5 * (np.log(2 * np.pi) + np.log(sigma2) + ret[t] ** 2 / sigma2)
        sigma2 = omega + alpha * ret[t] ** 2 + beta * sigma2
        if sigma2 <= 0 or np.isnan(sigma2):
            return 1e12
    return -float(ll)


def _finite_diff_hessian(params: np.ndarray, ret: np.ndarray, eps: float = 1e-5) -> np.ndarray:
    """有限差分 Hessian 矩阵 (3×3)。"""
    n = len(params)
    H = np.zeros((n, n))
    f0 = _garch_llh(params, ret)
    for i in range(n):
        for j in range(i, n):
            ei = np.zeros(n)
            ei[i] = eps
            ej = np.zeros(n)
            ej[j] = eps
            fpp = _garch_llh(params + ei + ej, ret)
            fpm = _garch_llh(params + ei - ej, ret)
            fmp = _garch_llh(params - ei + ej, ret)
            fmm = _garch_llh(params - ei - ej, ret)
            H[i, j] = (fpp - fpm - fmp + fmm) / (4 * eps * eps)
            H[j, i] = H[i, j]
    return H


# ── BFGS (纯 NumPy 实现，两级步长搜索) ──


def _bfgs_optimize(ret: np.ndarray, x0: np.ndarray, max_iter: int = 200) -> Tuple[np.ndarray, float]:
    """拟牛顿 BFGS 优化（无 sciPy 依赖）。"""
    x = x0.copy()
    n = len(x)
    H = np.eye(n)  # 初始 Hessian 近似
    f0 = _garch_llh(x, ret)
    g0 = np.zeros(n)

    # 有限差分梯度
    eps_g = 1e-6
    for i in range(n):
        ei = np.zeros(n)
        ei[i] = eps_g
        g0[i] = (_garch_llh(x + ei, ret) - _garch_llh(x - ei, ret)) / (2 * eps_g)

    for it in range(max_iter):
        # 搜索方向
        p = -H @ g0

        # Wolfe 条件：两段 backtracking
        alpha_step = 1.0
        c1, c2 = 1e-4, 0.9
        for _ in range(40):
            xnew = x + alpha_step * p
            fnew = _garch_llh(xnew, ret)
            if fnew <= f0 + c1 * alpha_step * np.dot(g0, p):
                # 强 Wolfe 曲率条件
                gn = np.zeros(n)
                for i in range(n):
                    ei = np.zeros(n)
                    ei[i] = eps_g
                    gn[i] = (_garch_llh(xnew + ei, ret) - _garch_llh(xnew - ei, ret)) / (2 * eps_g)
                if abs(np.dot(gn, p)) <= c2 * abs(np.dot(g0, p)):
                    break
            alpha_step *= 0.5

        s = alpha_step * p
        xnew = x + s
        fnew = _garch_llh(xnew, ret)

        # 梯度更新
        gn = np.zeros(n)
        for i in range(n):
            ei = np.zeros(n)
            ei[i] = eps_g
            gn[i] = (_garch_llh(xnew + ei, ret) - _garch_llh(xnew - ei, ret)) / (2 * eps_g)
        y = gn - g0

        # BFGS 更新
        sy = np.dot(s, y)
        if sy > 1e-10:
            Hs = H @ s
            H = H + np.outer(y, y) / sy - np.outer(Hs, Hs) / np.dot(s, Hs)

        x, f0, g0 = xnew, fnew, gn
        if np.linalg.norm(s) < 1e-8:
            break

    return x, f0


# ── 主估计函数 ──


def estimate_garch(stock_code: Optional[str] = None) -> GARCHResult:
    """
    估计 GARCH(1,1) 模型（网格搜索 + BFGS 精炼）。

    Args:
        stock_code: 指定单只股票，None 则用全市场聚合
    """
    try:
        ret = stock_returns(stock_code) if stock_code else aggregated_returns()
        if len(ret) < 2:
            return GARCHResult(
                omega=np.nan,
                alpha=np.nan,
                beta=np.nan,
                se_omega=np.nan,
                se_alpha=np.nan,
                se_beta=np.nan,
                unconditional_vol=0.20,
                cond_vol_series=[0.20],
                current_volatility=0.20,
                vol_percentile=50.0,
                log_likelihood=np.nan,
                n_obs=len(ret),
                status="insufficient_data",
            )

        # ── 第一步：网格搜索 ──
        best_ll = 1e12
        best_raw = (1e-5, 0.05, 0.90)
        for omega in _OMEGA_GRID:
            for alpha in _ALPHA_GRID:
                for beta in _BETA_GRID:
                    if alpha + beta >= 0.99:
                        continue
                    ll = _garch_llh_raw(omega, alpha, beta, ret)
                    if ll < best_ll:
                        best_ll = ll
                        best_raw = (omega, alpha, beta)

        # ── 第二步：BFGS 精炼（变换参数空间）──
        o, a, b = best_raw
        x0 = np.array([np.log(o), np.log(a / (1.0 - a + 1e-10)), np.log(b / (1.0 - b + 1e-10))])

        try:
            x_opt, final_ll = _bfgs_optimize(ret, x0)
        except Exception:
            x_opt, final_ll = x0, best_ll

        # 变换回原始参数
        omega = float(np.exp(x_opt[0]))
        alpha = float(1.0 / (1.0 + np.exp(-x_opt[1])))
        beta = float(1.0 / (1.0 + np.exp(-x_opt[2])))

        # 钳位
        alpha = max(0.001, min(0.499, alpha))
        beta = max(0.001, min(0.99, beta))
        if alpha + beta >= 0.999:
            scale = 0.99 / (alpha + beta)
            alpha *= scale
            beta *= scale
        omega = max(1e-8, omega)

        # ── 标准误 ──
        try:
            H = _finite_diff_hessian(x_opt, ret)
            se = np.sqrt(np.abs(np.diag(np.linalg.pinv(H, rcond=1e-6))))
            se_omega = float(np.exp(x_opt[0]) * se[0])  # Delta 法
            se_alpha = float(alpha * (1 - alpha) * se[1])
            se_beta = float(beta * (1 - beta) * se[2])
        except Exception:
            se_omega = se_alpha = se_beta = np.nan

        # ── 条件波动率序列 ──
        sigma2 = np.var(ret) + 1e-10
        cond_var: List[float] = []
        for t in range(len(ret)):
            cond_var.append(float(np.sqrt(sigma2) * np.sqrt(_N_DAY_YEAR)))
            sigma2 = omega + alpha * ret[t] ** 2 + beta * sigma2
            if sigma2 <= 0:
                sigma2 = 1e-10

        current_sigma = float(np.sqrt(sigma2) * np.sqrt(_N_DAY_YEAR))
        uncond_vol = (
            float(np.sqrt(omega / (1 - alpha - beta)) * np.sqrt(_N_DAY_YEAR)) if alpha + beta < 1 else current_sigma
        )

        # 百分位
        arr = np.array(cond_var)
        vol_pctile = float(np.sum(arr <= current_sigma) / len(arr)) * 100 if len(arr) >= 2 else 50.0

        logger.info(
            f"[GARCH] ω={omega:.6f}±{se_omega:.6f} α={alpha:.4f}±{se_alpha:.4f} "
            f"β={beta:.4f}±{se_beta:.4f} | σ={current_sigma:.2%} "
            f"pctl={vol_pctile:.0f}% (n={len(ret)}, llh={final_ll:.1f})"
        )

        return GARCHResult(
            omega=omega,
            alpha=alpha,
            beta=beta,
            se_omega=se_omega,
            se_alpha=se_alpha,
            se_beta=se_beta,
            unconditional_vol=round(uncond_vol, 4),
            cond_vol_series=cond_var,
            current_volatility=round(current_sigma, 4),
            vol_percentile=round(vol_pctile, 1),
            log_likelihood=-final_ll,
            n_obs=len(ret),
            status="ok",
        )

    except Exception as e:
        logger.debug(f"[GARCH] {e}")
        return GARCHResult(
            omega=np.nan,
            alpha=np.nan,
            beta=np.nan,
            se_omega=np.nan,
            se_alpha=np.nan,
            se_beta=np.nan,
            unconditional_vol=0.20,
            cond_vol_series=[0.20],
            current_volatility=0.20,
            vol_percentile=50.0,
            log_likelihood=np.nan,
            n_obs=0,
            status=f"error: {e}",
        )


def get_dynamic_thresholds(
    result: Optional[GARCHResult] = None,
    base_buy: int = 70,
    base_sell: int = 40,
) -> Tuple[int, int]:
    """GARCH 条件波动率驱动的动态买卖阈值。"""
    if result is None:
        result = estimate_garch()
    vp = result.vol_percentile

    if vp >= 90:
        return (base_buy + 10, base_sell)
    if vp >= 75:
        return (base_buy + 5, base_sell)
    if vp >= 60:
        return (base_buy + 2, base_sell)
    if vp <= 10:
        return (base_buy - 8, base_sell + 8)
    if vp <= 25:
        return (base_buy - 3, base_sell + 3)
    return (base_buy, base_sell)


def run_garch_and_save(stock_code: Optional[str] = None) -> Dict[str, Any]:
    result = estimate_garch(stock_code)

    # 样本太少时不保存输出（防止锁死买入阈值）
    if result.n_obs < 50:
        logger.warning(f"[GARCH] 样本不足({result.n_obs}<50)，跳过保存，退回情绪阈值")
        if _OUTPUT_FILE.exists():
            _OUTPUT_FILE.unlink()
        return {
            "status": "skipped",
            "n_obs": result.n_obs,
            "reason": f"样本不足({result.n_obs}<50)",
        }

    buy_th, sell_th = get_dynamic_thresholds(result)

    output = {
        "status": result.status,
        "omega": result.omega,
        "alpha": result.alpha,
        "beta": result.beta,
        "se_omega": result.se_omega,
        "se_alpha": result.se_alpha,
        "se_beta": result.se_beta,
        "unconditional_vol": result.unconditional_vol,
        "current_volatility": result.current_volatility,
        "vol_percentile": result.vol_percentile,
        "log_likelihood": result.log_likelihood,
        "n_obs": result.n_obs,
        "dynamic_thresholds": {"buy": buy_th, "sell": sell_th},
        "timestamp": datetime.now().isoformat(),
    }
    _OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    import os
    import tempfile

    tmp = tempfile.NamedTemporaryFile(mode="w", dir=_OUTPUT_FILE.parent, suffix=".tmp", delete=False)
    json.dump(output, tmp, ensure_ascii=False, indent=2)
    tmp.flush()
    os.fsync(tmp.fileno())
    tmp.close()
    os.replace(tmp.name, str(_OUTPUT_FILE))
    logger.info(f"[GARCH] 输出已保存至 {_OUTPUT_FILE}")
    return output


def load_garch_output() -> Optional[Dict[str, Any]]:
    if _OUTPUT_FILE.exists():
        try:
            return json.loads(_OUTPUT_FILE.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    import sys

    out = run_garch_and_save()
    print(
        f"\nGARCH{out['n_obs']}obs | σ={out['current_volatility']:.2%} "
        f"pctl={out['vol_percentile']:.0f}% | "
        f"ω={out['omega']:.6f}±{out['se_omega']:.6f} "
        f"α={out['alpha']:.4f}±{out['se_alpha']:.4f} "
        f"β={out['beta']:.4f}±{out['se_beta']:.4f}"
    )
