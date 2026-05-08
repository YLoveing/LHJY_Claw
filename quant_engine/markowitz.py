# -*- coding: utf-8 -*-
"""
马科维茨均值-方差优化 精确版 — 纯 NumPy 实现

优化问题:
    max  w^T μ - λ · w^T Σ w        (风险厌恶形式)
    s.t. w^T 1 = 1
         w ≥ 0                       (仅做多约束)

求解方案:
1. 无约束 → 解析解 (最大夏普)
2. 仅做多 → 关键线算法 (Critical Line Algorithm)
3. 有效前沿 → 对风险厌恶系数 λ 从大到小扫描
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from ._prices import extract_price_matrix

logger = logging.getLogger("quant_engine.markowitz")

_BASE_DIR = Path("/opt/daily_stock_analysis")
_OUTPUT_FILE = _BASE_DIR / "quant_engine" / "markowitz_output.json"
_STATE_FILE = _BASE_DIR / "simulated_trading" / "state.json"
_N_DAY = 252


@dataclass
class MarkowitzResult:
    weights: Dict[str, float]
    expected_return: float
    expected_volatility: float
    sharpe_ratio: float
    min_var_weights: Dict[str, float] = field(default_factory=dict)
    max_sharpe_weights: Dict[str, float] = field(default_factory=dict)
    efficient_frontier: List[Dict[str, float]] = field(default_factory=list)
    status: str = "ok"
    message: str = ""


# ═══════════════════════════════════════════
#  关键线算法 (Critical Line Algorithm)
#  求解: min  w^T Σ w  s.t.  w^T 1 = 1, w ≥ 0
# ═══════════════════════════════════════════

def _cl_solve(sigma: np.ndarray, n: int) -> np.ndarray:
    """
    最小方差组合（仅做多）— CLA 梯度投影法。

    从等权出发，迭代至 KKT 条件满足。
    KKT:
       w_i > 0  ⇒  (2Σw)_i = λ   (活跃约束)
       w_i = 0  ⇒  (2Σw)_i ≥ λ   (非活跃约束)
    """
    w = np.ones(n) / n
    active = np.ones(n, dtype=bool)

    for _ in range(500):
        # 对活跃集求解析解: w_A = Σ_A^{-1} 1 / (1^T Σ_A^{-1} 1)
        m = int(active.sum())
        if m == 0:
            break
        sigma_a = sigma[np.ix_(active, active)]
        try:
            inv = np.linalg.inv(sigma_a)
            ones_a = np.ones(m)
            w_a = inv @ ones_a
            w_a = w_a / (ones_a @ w_a + 1e-15)
        except np.linalg.LinAlgError:
            break

        w_new = np.zeros(n)
        w_new[active] = w_a

        # 如果全部 ≥ 0，检查 KKT
        if (w_new >= -1e-8).all():
            w_new = np.clip(w_new, 0, None)
            w_new /= w_new.sum()

            # KKT: 拉格朗日乘子 = 2 · mean(active sub-gradients)
            grad = 2.0 * sigma @ w_new
            λ = float(np.mean(grad[active])) if active.any() else 0.0

            # 检查: 活跃集梯度偏差 + 非活跃集梯度方向
            max_kkt = 0.0
            for i in range(n):
                if w_new[i] > 1e-8:
                    max_kkt = max(max_kkt, abs(grad[i] - λ))
                else:
                    max_kkt = max(max_kkt, max(0.0, λ - grad[i]))

            if max_kkt < 1e-6:
                w = w_new
                break

            w = w_new
            # 更新 active set: 梯度异常的活跃变量释放
            for i in range(n):
                if w[i] > 1e-8:
                    active[i] = True
                elif grad[i] < λ - 1e-6 and not active[i]:
                    active[i] = True   # 梯度小于 λ 的应进入
            continue

        # 步长截断至单纯形边界
        diff = w_new - w
        t_max = 1.0
        for i in range(n):
            if diff[i] < -1e-12 and w[i] > 1e-12:
                t = -w[i] / diff[i]
                if t < t_max:
                    t_max = t
        t_max = max(t_max, 1e-12)

        w = w + t_max * diff
        w = np.clip(w, 0, None)
        w /= w.sum()

        # 触边的变量出 active
        for i in range(n):
            if w[i] < 1e-8:
                active[i] = False

    w = np.clip(w, 0, None)
    w /= w.sum()
    return w


def _solve_max_sharpe(mu: np.ndarray, sigma: np.ndarray) -> np.ndarray:
    """
    求解最大夏普组合 (仅做多).

    max  w^T μ / sqrt(w^T Σ w)  s.t. w^T 1 = 1, w ≥ 0

    使用 λ 二分搜索:
    对给定 λ, 求 w(λ) = argmax w^T μ - λ · w^T Σ w
    夏普在 λ* 处取最大值.
    """
    n = len(mu)
    # 标准化优化: 最大夏普 = 最大 ret/vol
    # 等价于求 w 使得 w^T μ = 1 时 w^T Σ w 最小
    # 用二分搜索风险厌恶系数 λ

    λ_low, λ_high = 0.0, 50.0
    best_w = np.ones(n) / n
    best_sr = 0.0

    for _ in range(50):
        λ_mid = (λ_low + λ_high) / 2.0

        # 对给定 λ 求最优 w
        # max w^T μ - λ · w^T Σ w
        # 等效于 min w^T Σ w - (1/λ) w^T μ   (但 λ=0 时不行)
        # 用投影法: 从等权开始迭代
        w = np.ones(n) / n
        for _ in range(200):
            grad = 2.0 * sigma @ w - mu  # -∇(μ - λΣw) 的负方向
            w = w + 0.01 * (mu - 2.0 * sigma @ w)  # 梯度上升
            w = np.clip(w, 0, None)
            w /= w.sum() + 1e-15

        r = float(w @ mu)
        v = float(np.sqrt(w @ sigma @ w))
        sr = r / v if v > 1e-10 else 0.0

        if sr > best_sr:
            best_sr = sr
            best_w = w.copy()

        # 调整 λ: 看 w 是否偏向单只股票
        hhi = float((w**2).sum())
        if hhi > 0.8:  # 过于集中 → 增加风险厌恶
            λ_low = λ_mid
        else:
            λ_high = λ_mid

    return best_w


# ═══════════════════════════════════════════
#  参数估计
# ═══════════════════════════════════════════

def _estimate_parameters(prices: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """从价格矩阵估计预期收益率和协方差矩阵 (收缩估计)."""
    T, N = prices.shape
    log_returns = np.diff(np.log(prices), axis=0)

    # 年化协方差
    cov = np.cov(log_returns, rowvar=False) * _N_DAY

    # 收缩估计: target = 对角矩阵 (等方差, 零相关)
    avg_var = np.trace(cov) / N
    target = np.eye(N) * avg_var
    r = len(log_returns)
    # 收缩强度: 数据越少收缩越多
    shrinkage = max(0.0, min(1.0, (N + 2) / (r + N + 2)))
    cov = shrinkage * cov + (1 - shrinkage) * target

    # 确保正定
    eigvals = np.linalg.eigvalsh(cov)
    if eigvals.min() <= 1e-10:
        cov += np.eye(N) * (abs(eigvals.min()) + 1e-8)

    # 预期收益: 历史均值 + 贝叶斯收缩 (先验=10%年化)
    hist_ret = np.mean(log_returns, axis=0) * _N_DAY
    prior = np.ones(N) * 0.10  # 合理长期权益收益率, 非样本中位数
    w_shrink = min(1.0, r / 60)  # 60个交易日以上充分相信历史
    mu = w_shrink * hist_ret + (1 - w_shrink) * prior
    mu = np.clip(mu, -0.15, 0.30)

    return mu, cov


# ═══════════════════════════════════════════
#  有效前沿 — λ 扫描 (正确的实现)
# ═══════════════════════════════════════════

def _compute_efficient_frontier(mu: np.ndarray, sigma: np.ndarray,
                                n_points: int = 30) -> List[Dict[str, float]]:
    """计算有效前沿: 对风险厌恶系数 λ 从大到小扫描."""

    N = len(mu)
    frontiers: List[Dict[str, float]] = []

    # 最小方差: λ → ∞ (只需最小化 Σ, 忽略 μ)
    w_minvar = _cl_solve(sigma, N)
    ret_minvar = float(w_minvar @ mu)
    vol_minvar = float(np.sqrt(w_minvar @ sigma @ w_minvar))

    # 最大夏普: λ ≈ ret_max / (2 · vol_max)
    w_sharpe = _solve_max_sharpe(mu, sigma)
    ret_sharpe = float(w_sharpe @ mu)
    vol_sharpe = float(np.sqrt(w_sharpe @ sigma @ w_sharpe))

    # 起点和终点 λ
    if vol_sharpe > 1e-8:
        λ_max = ret_sharpe / (2.0 * vol_sharpe**2) * 10
    else:
        λ_max = 50.0
    λ_min = max(0.01, λ_max / 1000)

    lambdas = np.logspace(np.log10(λ_max), np.log10(λ_min), n_points)

    for λ in lambdas:
        # 求解 max w^T μ - λ · w^T Σ w   s.t. w^T 1 = 1, w ≥ 0
        # 梯度投影
        w = np.ones(N) / N
        step = 0.001 / max(λ, 0.1)
        for _ in range(500):
            grad = mu - 2.0 * λ * sigma @ w  # 梯度
            w = w + step * grad
            w = np.clip(w, 0, None)
            w /= w.sum() + 1e-15

        r = float(w @ mu)
        v = float(np.sqrt(w @ sigma @ w))
        frontiers.append({"ret": round(r, 4), "vol": round(v, 4)})

    # 去重并确保单调性
    seen: set = set()
    unique: List[Dict[str, float]] = []
    for p in frontiers:
        key = (p["ret"], p["vol"])
        if key not in seen:
            seen.add(key)
            unique.append(p)

    # 确保 volatility 严格递增
    clean: List[Dict[str, float]] = []
    for p in unique:
        if not clean or p["vol"] > clean[-1]["vol"] + 1e-8:
            clean.append(p)

    return clean


# ═══════════════════════════════════════════
#  主函数
# ═══════════════════════════════════════════

def optimize_portfolio(
    stock_codes: Optional[List[str]] = None,
    allow_short: bool = False,
) -> MarkowitzResult:
    """马科维茨均值-方差优化。"""
    try:
        prices, codes, dates = extract_price_matrix()
        if len(prices) < 2 or len(codes) < 2:
            return MarkowitzResult(
                weights={}, expected_return=0.0, expected_volatility=0.0,
                sharpe_ratio=0.0, status="insufficient_data",
                message=f"prices={prices.shape if prices.size > 0 else 0}, codes={codes}",
            )

        if stock_codes:
            idx = [i for i, c in enumerate(codes) if c in stock_codes]
            if len(idx) < 1:
                return MarkowitzResult(
                    weights={}, expected_return=0.0, expected_volatility=0.0,
                    sharpe_ratio=0.0, status="no_matching_stocks",
                )
            # 至少需要2只进行优化
            if len(idx) < 2:
                return MarkowitzResult(
                    weights={}, expected_return=0.0, expected_volatility=0.0,
                    sharpe_ratio=0.0, status="insufficient_data",
                    message=f"至少需要2只股票做优化, 当前 {len(idx)} 只",
                )
            codes = [codes[i] for i in idx]
            prices = prices[:, idx]

        mu, sigma = _estimate_parameters(prices)
        N = len(mu)

        frontier: List[Dict[str, float]] = []

        if allow_short:
            # 最大夏普解析解
            sigma_inv = np.linalg.inv(sigma)
            ones = np.ones(N)
            w = sigma_inv @ mu / (ones @ (sigma_inv @ mu) + 1e-10)
            exp_ret = float(w @ mu)
            vol = float(np.sqrt(w @ sigma @ w))
            sharpe = exp_ret / vol if vol > 1e-10 else 0.0
            w_minvar = _cl_solve(sigma, N)
            w_sharpe = w
        else:
            # 仅做多: 最小方差 + 最大夏普 + 有效前沿
            w_minvar = _cl_solve(sigma, N)
            w_sharpe = _solve_max_sharpe(mu, sigma)
            frontier = _compute_efficient_frontier(mu, sigma)

            r_minvar = float(w_minvar @ mu)
            v_minvar = float(np.sqrt(w_minvar @ sigma @ w_minvar))
            sr_minvar = r_minvar / v_minvar if v_minvar > 1e-10 else 0.0
            r_sharpe = float(w_sharpe @ mu)
            v_sharpe = float(np.sqrt(w_sharpe @ sigma @ w_sharpe))
            sr_sharpe = r_sharpe / v_sharpe if v_sharpe > 1e-10 else 0.0

            if sr_sharpe > sr_minvar:
                w, exp_ret, vol, sharpe = w_sharpe, r_sharpe, v_sharpe, sr_sharpe
            else:
                w, exp_ret, vol, sharpe = w_minvar, r_minvar, v_minvar, sr_minvar

        weights = {codes[i]: round(float(w[i]), 4) for i in range(N)}
        minvar_w = {codes[i]: round(float(w_minvar[i]), 4) for i in range(N)}
        sharpe_w = {codes[i]: round(float(w_sharpe[i]), 4) for i in range(N)}

        logger.info(
            f"[马科维茨] {len(codes)}只 | ER={exp_ret:.2%} σ={vol:.2%} "
            f"SR={sharpe:.3f} | w={weights}"
        )

        result = MarkowitzResult(
            weights=weights,
            expected_return=round(exp_ret, 4),
            expected_volatility=round(vol, 4),
            sharpe_ratio=round(sharpe, 4),
            min_var_weights=minvar_w,
            max_sharpe_weights=sharpe_w,
            status="ok",
        )

        result.efficient_frontier = frontier
        return result

    except Exception as e:
        logger.error(f"[马科维茨] {e}")
        return MarkowitzResult(
            weights={}, expected_return=0.0, expected_volatility=0.0,
            sharpe_ratio=0.0, status=f"error: {e}",
        )


def run_markowitz_and_save(stock_codes: Optional[List[str]] = None) -> Dict[str, Any]:
    result = optimize_portfolio(stock_codes)
    output = {
        "weights": result.weights,
        "expected_return": result.expected_return,
        "expected_volatility": result.expected_volatility,
        "sharpe_ratio": result.sharpe_ratio,
        "min_var_weights": result.min_var_weights,
        "max_sharpe_weights": result.max_sharpe_weights,
        "efficient_frontier": result.efficient_frontier,
        "status": result.status,
        "message": result.message,
        "timestamp": datetime.now().isoformat(),
    }
    _OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    logger.info(f"[马科维茨] 保存至 {_OUTPUT_FILE}")
    return output


def load_markowitz_output() -> Optional[Dict[str, Any]]:
    if _OUTPUT_FILE.exists():
        try:
            return json.loads(_OUTPUT_FILE.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    return None


def normalize_weights_for_positions(
    markowitz_weights: Dict[str, float],
    held_codes: List[str],
) -> Dict[str, float]:
    """
    将马科维茨权重归一化到实际持仓股票。

    例如: markowitz = {A:0.3, B:0.5, C:0.2}, held = [A, B]
    返回: {A: 0.3/0.8, B: 0.5/0.8}
    """
    held_set = set(held_codes)
    relevant = {k: v for k, v in markowitz_weights.items() if k in held_set}
    total = sum(relevant.values())
    if total <= 0:
        n = len(held_codes)
        return {c: 1.0 / n for c in held_codes}
    return {k: round(v / total, 4) for k, v in relevant.items()}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    stock_codes = None
    if _STATE_FILE.exists():
        try:
            state = json.loads(_STATE_FILE.read_text())
            stock_codes = list(state.get("positions", {}).keys())
        except Exception:
            pass
    out = run_markowitz_and_save(stock_codes)
    print(f"\n马科维茨: status={out['status']}")
    if out["status"] == "ok":
        print(f"  最优权重: {out['weights']}")
        print(f"  最小方差: {out['min_var_weights']}")
        print(f"  最大夏普: {out['max_sharpe_weights']}")
        print(f"  预期收益: {out['expected_return']:.2%}")
        print(f"  预期波动: {out['expected_volatility']:.2%}")
        print(f"  夏普比率: {out['sharpe_ratio']:.3f}")
        print(f"  有效前沿: {len(out.get('efficient_frontier', []))} 点")
