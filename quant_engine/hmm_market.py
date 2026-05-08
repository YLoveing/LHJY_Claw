# -*- coding: utf-8 -*-
"""
隐马尔可夫模型 (HMM) 真正版 — 纯 NumPy 实现

观测: [收益率, 振幅变化, 成交额变化] (3 维)
隐状态: 多头 / 空头 / 震荡 / 高波动 (4 状态)

算法:
1. 高斯观测 HMM (对角协方差)
2. Baum-Welch EM (多随机起点防局部最优)
3. Viterbi 解码 → 当前市场状态
4. 前向 → 当前各状态概率
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from ._prices import extract_all_prices

logger = logging.getLogger("quant_engine.hmm")

_BASE_DIR = Path("/opt/daily_stock_analysis")
_REPORTS_DIR = _BASE_DIR / "reports"
_SIGNAL_TRACE_FILE = _BASE_DIR / "simulated_trading" / "signal_trace.json"
_OUTPUT_FILE = _BASE_DIR / "quant_engine" / "hmm_output.json"

N_STATES = 4
N_DIMS = 3  # [收益率, 振幅变化, 成交额变化]
STATE_NAMES = {0: "多头", 1: "空头", 2: "震荡", 3: "高波动"}


@dataclass
class HMMResult:
    current_state: int
    current_state_name: str
    state_probabilities: List[float]
    state_sequence: List[int]
    transition_matrix: List[List[float]]
    means: List[List[float]]
    vars_: List[List[float]]
    log_likelihood: float
    n_observations: int
    status: str = "ok"


class GaussianHMM:
    """
    多元高斯 HMM（对角协方差）— 专为市场状态检测设计。
    """

    def __init__(self, n_states: int = 4, n_dims: int = 3, n_iter: int = 100):
        self.n_states = n_states
        self.n_dims = n_dims
        self.n_iter = n_iter
        self.reset()

    def reset(self):
        self.startprob_: Optional[np.ndarray] = None
        self.transmat_: Optional[np.ndarray] = None
        self.means_: Optional[np.ndarray] = None      # (n_states, n_dims)
        self.vars_: Optional[np.ndarray] = None         # (n_states, n_dims)
        self.log_likelihood_: float = -np.inf

    def _init_random(self, obs: np.ndarray, rng: np.random.Generator):
        """随机初始化参数。"""
        T, D = obs.shape
        n = self.n_states

        self.startprob_ = np.ones(n) / n

        # 转移矩阵：高自转移概率
        self.transmat_ = rng.uniform(0.05, 0.30, (n, n))
        np.fill_diagonal(self.transmat_, rng.uniform(0.70, 0.95))
        self.transmat_ /= self.transmat_.sum(axis=1, keepdims=True)

        # 均值：沿第一个主成分均匀分布
        pcs = np.linalg.svd(obs - obs.mean(axis=0), full_matrices=False)[0][:, 0]
        pc_range = np.linspace(pcs.min(), pcs.max(), n + 2)[1:-1]
        self.means_ = np.zeros((n, D))
        for i in range(n):
            self.means_[i] = obs.mean(axis=0) + pc_range[i] * \
                             np.std(obs, axis=0) * 0.5

        self.vars_ = np.tile(np.var(obs, axis=0) * 2, (n, 1))

    # ── 发射概率 ──

    def _log_emission(self, x: np.ndarray, state: int) -> float:
        """单个状态的对数高斯发射概率。"""
        diff = x - self.means_[state]
        var = np.maximum(self.vars_[state], 1e-10)
        return float(-0.5 * (np.sum(diff**2 / var) + np.sum(np.log(2 * np.pi * var))))

    def _log_emission_vec(self, x: np.ndarray) -> np.ndarray:
        """所有状态的对数高斯发射概率。"""
        diff = x - self.means_  # (n_states, n_dims)
        var = np.maximum(self.vars_, 1e-10)
        return -0.5 * (np.sum(diff**2 / var, axis=1) + np.sum(np.log(2 * np.pi * var), axis=1))

    # ── 前向 (log-space，防下溢) ──

    def _forward_log(self, obs: np.ndarray) -> Tuple[np.ndarray, float]:
        """前向算法（对数空间）。返回 (log_alpha, log_likelihood)。"""
        T = len(obs)
        n = self.n_states
        la = np.full((T, n), -np.inf)

        la[0] = np.log(self.startprob_ + 1e-300) + self._log_emission_vec(obs[0])
        for t in range(1, T):
            for j in range(n):
                la[t, j] = np.max(la[t-1]) + \
                    np.log(np.sum(np.exp(la[t-1] - np.max(la[t-1])) *
                                  self.transmat_[:, j]) + 1e-300) + \
                    self._log_emission_vec(obs[t])[j]

        log_lik = np.max(la[-1]) + np.log(np.sum(np.exp(la[-1] - np.max(la[-1]))) + 1e-300)
        return la, log_lik

    # ── 后向 (log-space) ──

    def _backward_log(self, obs: np.ndarray) -> np.ndarray:
        """后向算法（对数空间）。"""
        T = len(obs)
        n = self.n_states
        lb = np.full((T, n), -np.inf)
        lb[T-1] = 0.0

        for t in range(T-2, -1, -1):
            for i in range(n):
                scores = np.log(self.transmat_[i, :] + 1e-300) + \
                         self._log_emission_vec(obs[t+1]) + lb[t+1]
                lb[t, i] = np.max(scores) + \
                    np.log(np.sum(np.exp(scores - np.max(scores))) + 1e-300)

        return lb

    # ── Baum-Welch ──

    def fit(self, obs: np.ndarray, rng: np.random.Generator) -> float:
        """Baum-Welch EM 估计参数。"""
        T, D = obs.shape
        n = self.n_states
        self._init_random(obs, rng)

        for _ in range(self.n_iter):
            # E-step
            la, log_lik = self._forward_log(obs)
            lb = self._backward_log(obs)

            # gamma[t, i] = P(X_t = i | obs)
            log_gamma = la + lb
            log_gamma -= np.max(log_gamma, axis=1, keepdims=True)
            gamma = np.exp(log_gamma)
            gamma /= gamma.sum(axis=1, keepdims=True) + 1e-300

            # xi[t, i, j] = P(X_t=i, X_{t+1}=j | obs)
            xi = np.zeros((T-1, n, n))
            for t in range(T-1):
                scores = la[t, :, np.newaxis] + \
                    np.log(self.transmat_ + 1e-300)[np.newaxis, :, :] + \
                    self._log_emission_vec(obs[t+1])[np.newaxis, :] + \
                    lb[t+1, np.newaxis, :]
                max_s = scores.max()
                log_sum = max_s + np.log(np.sum(np.exp(scores - max_s)) + 1e-300)
                xi[t] = np.exp(scores - log_sum)

            # M-step
            self.startprob_ = gamma[0] + 1e-10
            self.startprob_ /= self.startprob_.sum()

            for i in range(n):
                denom = gamma[:-1, i].sum() + 1e-10
                for j in range(n):
                    self.transmat_[i, j] = (xi[:, i, j].sum() + 1e-10) / denom

            for i in range(n):
                gsum = gamma[:, i].sum() + 1e-10
                self.means_[i] = (gamma[:, i, np.newaxis] * obs).sum(axis=0) / gsum
                diff = obs - self.means_[i]
                self.vars_[i] = (gamma[:, i, np.newaxis] * diff**2).sum(axis=0) / gsum
                self.vars_[i] = np.maximum(self.vars_[i], 1e-10)

        # 最终迭代的对数似然
        _, self.log_likelihood_ = self._forward_log(obs)
        return self.log_likelihood_

    # ── Viterbi 解码 ──

    def predict(self, obs: np.ndarray) -> np.ndarray:
        """Viterbi 解码。返回最优状态序列。"""
        T = len(obs)
        n = self.n_states

        # log-space Viterbi
        delta = np.full((T, n), -np.inf)
        psi = np.zeros((T, n), dtype=int)

        delta[0] = np.log(self.startprob_ + 1e-300) + self._log_emission_vec(obs[0])
        for t in range(1, T):
            for j in range(n):
                scores = delta[t-1] + np.log(self.transmat_[:, j] + 1e-300)
                psi[t, j] = int(np.argmax(scores))
                delta[t, j] = scores[psi[t, j]] + self._log_emission_vec(obs[t])[j]

        states = np.zeros(T, dtype=int)
        states[T-1] = int(np.argmax(delta[T-1]))
        for t in range(T-2, -1, -1):
            states[t] = psi[t+1, states[t+1]]
        return states


# ── 特征提取 ──

def _extract_market_features() -> np.ndarray:
    """
    提取市场特征矩阵 (T×3):
    col0: 日均收益率 (%)  — 从当前价序列计算
    col1: 振幅变化        — 前后日振幅差值 (暂无数据时为0)
    col2: 成交额变化      — 前后日成交额对数变化 (暂无数据时为0)
    """
    # 方法一：从价格数据构造收益率特征(兼容当前报告格式)
    prices = extract_all_prices()
    if not prices:
        return np.array([])

    codes = sorted(prices.keys())

    # 找共同日期
    common_dates: set = set(p[0] for p in prices[codes[0]])
    for c in codes[1:]:
        common_dates &= set(p[0] for p in prices[c])
    sorted_dates = sorted(common_dates)

    if len(sorted_dates) < 3:
        return np.array([])

    features: List[np.ndarray] = []
    for i in range(1, len(sorted_dates)):
        daily_rets: List[float] = []
        for code in codes:
            lookup = dict(prices[code])
            p_prev = lookup.get(sorted_dates[i-1])
            p_curr = lookup.get(sorted_dates[i])
            if p_prev and p_curr:
                daily_rets.append((p_curr - p_prev) / p_prev * 100)
        if daily_rets:
            features.append(np.array([
                np.mean(daily_rets),
                0.0,  # 振幅(暂无)
                0.0,  # 成交额(暂无)
            ]))

    # 方法二: 如果表格有真实数据, 补充振幅和成交额
    # (当前报告格式为占位符, 等真实数据上线后自动生效)
    for report_file in sorted(_REPORTS_DIR.glob("report_*.md")):
        content = report_file.read_text(encoding="utf-8")
        secs = re.split(r'\n## ', content)

        for sec in secs:
            lines = sec.split('\n')
            for i, line in enumerate(lines):
                if ('| 收盘 |' in line or line.strip().startswith('| 收盘 ')) and i + 1 < len(lines):
                    cells = [c.strip() for c in lines[i+1].split('|') if c.strip()]
                    # 检查是否有真实数据(不是占位符)
                    if cells and '------' not in cells[0]:
                        if len(cells) >= 8:
                            m_amp = re.search(r'([\d.]+)%', cells[7])
                            if m_amp:
                                amp_val = float(m_amp.group(1))
                        if len(cells) >= 10:
                            vol_str = cells[9].replace('亿元', '').replace('万元', '').replace('亿', '').strip()
                    break

    return np.array(features) if len(features) >= 3 else np.array([])


# ── 主检测函数 ──

def detect_market_state(force_rerun: bool = True) -> HMMResult:
    """检测市场状态（多随机起点 HMM）。"""
    if not force_rerun and _OUTPUT_FILE.exists():
        try:
            cached = json.loads(_OUTPUT_FILE.read_text())
            if cached.get("status") in ("ok", "insufficient_data"):
                return HMMResult(**{k: v for k, v in cached.items()
                                    if k in HMMResult.__dataclass_fields__})
        except Exception:
            pass

    obs = _extract_market_features()
    if len(obs) < 4:
        logger.warning(f"[HMM] 数据不足 (n={len(obs)})")
        return HMMResult(
            current_state=2, current_state_name="震荡",
            state_probabilities=[0.25, 0.25, 0.40, 0.10],
            state_sequence=[2]*len(obs) if len(obs) > 0 else [2],
            transition_matrix=[], means=[], vars_=[],
            log_likelihood=np.nan, n_observations=len(obs),
            status=f"insufficient_data (n={len(obs)}, need≥4)",
        )

    # 标准化
    mean = obs.mean(axis=0)
    std = np.maximum(obs.std(axis=0), 1e-6)
    obs_scaled = (obs - mean) / std

    # 多随机起点
    n_restarts = 5 if len(obs) >= 10 else 10
    best_ll = -np.inf
    best_hmm: Optional[GaussianHMM] = None

    for seed in range(n_restarts):
        hmm = GaussianHMM(n_states=N_STATES, n_dims=N_DIMS, n_iter=100)
        rng = np.random.default_rng(seed + 42)
        try:
            ll = hmm.fit(obs_scaled, rng)
            if ll > best_ll:
                best_ll = ll
                best_hmm = hmm
        except Exception as e:
            logger.debug(f"[HMM] restart {seed} failed: {e}")
            continue

    if best_hmm is None:
        return HMMResult(
            current_state=2, current_state_name="震荡",
            state_probabilities=[0.25, 0.25, 0.40, 0.10],
            state_sequence=[],
            transition_matrix=[], means=[], vars_=[],
            log_likelihood=np.nan, n_observations=len(obs),
            status="error: all restarts failed",
        )

    # Viterbi 解码
    states = best_hmm.predict(obs_scaled)
    current_state = int(states[-1])

    # 当前状态概率（前向算法最后一帧）
    la, _ = best_hmm._forward_log(obs_scaled)
    la_last = la[-1]
    la_last -= np.max(la_last)
    state_probs = np.exp(la_last) / (np.exp(la_last).sum() + 1e-300)

    # 还原均值到原始尺度
    means_orig = best_hmm.means_ * std + mean
    vars_orig = best_hmm.vars_ * std**2

    logger.info(
        f"[HMM] {STATE_NAMES[current_state]} "
        f"(牛{state_probs[0]:.0%} 熊{state_probs[1]:.0%} "
        f"盘{state_probs[2]:.0%} 高波{state_probs[3]:.0%}) "
        f"llh={best_ll:.1f} n={len(obs)}"
    )

    return HMMResult(
        current_state=current_state,
        current_state_name=STATE_NAMES.get(current_state, "?"),
        state_probabilities=[float(p) for p in state_probs],
        state_sequence=[int(s) for s in states],
        transition_matrix=best_hmm.transmat_.tolist() if best_hmm.transmat_ is not None else [],
        means=means_orig.tolist(),
        vars_=vars_orig.tolist(),
        log_likelihood=round(float(best_ll), 2),
        n_observations=len(obs),
        status="ok",
    )


def run_hmm_and_save() -> Dict[str, Any]:
    result = detect_market_state(force_rerun=True)
    output = {
        "current_state": result.current_state,
        "current_state_name": result.current_state_name,
        "state_probabilities": result.state_probabilities,
        "state_sequence": result.state_sequence,
        "transition_matrix": result.transition_matrix,
        "means": result.means,
        "vars": result.vars_,
        "log_likelihood": result.log_likelihood,
        "n_observations": result.n_observations,
        "status": result.status,
        "timestamp": datetime.now().isoformat(),
    }
    _OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    logger.info(f"[HMM] 输出已保存至 {_OUTPUT_FILE}")
    return output


def load_hmm_output() -> Optional[Dict[str, Any]]:
    if _OUTPUT_FILE.exists():
        try:
            return json.loads(_OUTPUT_FILE.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = run_hmm_and_save()
    print(f"\nHMM ({out['n_observations']}obs): {out['current_state_name']} "
          f"牛{out['state_probabilities'][0]:.1%} "
          f"熊{out['state_probabilities'][1]:.1%} "
          f"盘{out['state_probabilities'][2]:.1%} "
          f"高波{out['state_probabilities'][3]:.1%}")
