# -*- coding: utf-8 -*-
"""quant_engine HMM 市场状态检测测试"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quant_engine.hmm_market import (
    GaussianHMM,
    HMMResult,
    N_STATES,
    N_DIMS,
    STATE_NAMES,
    detect_market_state,
    _extract_market_features,
)


# ═══════════════════════════════════════════
# GaussianHMM 测试
# ═══════════════════════════════════════════

class TestGaussianHMM:
    @pytest.fixture
    def obs(self):
        """生成模拟观测数据 (100×3)。"""
        rng = np.random.default_rng(42)
        T = 100
        D = 3
        return rng.normal(0, 1, (T, D))

    def test_init_resets_parameters(self):
        hmm = GaussianHMM(n_states=4, n_dims=3, n_iter=50)
        assert hmm.startprob_ is None
        assert hmm.transmat_ is None
        assert hmm.means_ is None
        assert hmm.vars_ is None

    def test_init_random_creates_valid_params(self, obs):
        hmm = GaussianHMM(n_states=4, n_dims=3)
        rng = np.random.default_rng(42)
        hmm._init_random(obs, rng)

        assert hmm.startprob_ is not None
        assert hmm.transmat_ is not None
        assert hmm.means_ is not None
        assert hmm.vars_ is not None

        # 概率验证
        assert np.isclose(hmm.startprob_.sum(), 1.0)
        assert np.allclose(hmm.transmat_.sum(axis=1), 1.0)

    def test_fit_improves_likelihood(self, obs):
        hmm = GaussianHMM(n_states=4, n_dims=3, n_iter=50)
        rng = np.random.default_rng(42)
        ll = hmm.fit(obs, rng)
        assert np.isfinite(ll)
        assert ll != -np.inf

    def test_predict_returns_valid_sequence(self, obs):
        hmm = GaussianHMM(n_states=4, n_dims=3, n_iter=50)
        rng = np.random.default_rng(42)
        hmm.fit(obs, rng)
        states = hmm.predict(obs)

        assert len(states) == len(obs)
        assert all(0 <= s < 4 for s in states)
        assert isinstance(states[0], (int, np.integer))

    def test_reset_clears_state(self, obs):
        hmm = GaussianHMM(n_states=4, n_dims=3, n_iter=50)
        rng = np.random.default_rng(42)
        hmm.fit(obs, rng)
        hmm.reset()
        assert hmm.startprob_ is None

    def test_log_emission_returns_finite(self, obs):
        hmm = GaussianHMM(n_states=4, n_dims=3)
        rng = np.random.default_rng(42)
        hmm._init_random(obs, rng)
        for s in range(4):
            val = hmm._log_emission(obs[0], s)
            assert np.isfinite(val)

    def test_log_emission_vec_shape(self, obs):
        hmm = GaussianHMM(n_states=4, n_dims=3)
        rng = np.random.default_rng(42)
        hmm._init_random(obs, rng)
        vec = hmm._log_emission_vec(obs[0])
        assert vec.shape == (4,)

    def test_forward_log_returns_valid(self, obs):
        hmm = GaussianHMM(n_states=4, n_dims=3)
        rng = np.random.default_rng(42)
        hmm._init_random(obs, rng)
        la, log_lik = hmm._forward_log(obs)
        assert la.shape == (len(obs), 4)
        assert np.isfinite(log_lik)

    def test_backward_log_returns_valid(self, obs):
        hmm = GaussianHMM(n_states=4, n_dims=3)
        rng = np.random.default_rng(42)
        hmm._init_random(obs, rng)
        lb = hmm._backward_log(obs)
        assert lb.shape == (len(obs), 4)


# ═══════════════════════════════════════════
# HMMResult 测试
# ═══════════════════════════════════════════

class TestHMMResult:
    def test_create_with_defaults(self):
        result = HMMResult(
            current_state=2,
            current_state_name="震荡",
            state_probabilities=[0.25, 0.25, 0.40, 0.10],
            state_sequence=[2],
            transition_matrix=[],
            means=[],
            vars_=[],
            log_likelihood=-100.0,
            n_observations=10,
            status="ok",
        )
        assert result.current_state == 2
        assert result.status == "ok"

    def test_error_status(self):
        result = HMMResult(
            current_state=2,
            current_state_name="震荡",
            state_probabilities=[0.25] * 4,
            state_sequence=[2],
            transition_matrix=[],
            means=[],
            vars_=[],
            log_likelihood=float("nan"),
            n_observations=0,
            status="insufficient_data (n=0)",
        )
        assert "insufficient" in result.status


# ═══════════════════════════════════════════
# detect_market_state 测试
# ═══════════════════════════════════════════

class TestDetectMarketState:
    def test_insufficient_data_returns_fallback(self):
        """数据不足时应返回默认震荡状态。"""
        with patch("quant_engine.hmm_market._extract_market_features", return_value=np.array([])):
            result = detect_market_state(force_rerun=True)
            assert result.status.startswith("insufficient_data")
            assert result.current_state_name == "震荡"

    def test_few_observations_returns_fallback(self):
        """观测 < 4 时应返回默认状态。"""
        obs = np.random.default_rng(42).normal(0, 1, (3, 3))
        with patch("quant_engine.hmm_market._extract_market_features", return_value=obs):
            result = detect_market_state(force_rerun=True)
            assert "insufficient" in result.status

    def test_normal_data_detects_state(self):
        """足够数据应检测出有效状态。"""
        obs = np.random.default_rng(42).normal(0, 1, (50, 3))
        with patch("quant_engine.hmm_market._extract_market_features", return_value=obs):
            result = detect_market_state(force_rerun=True)
            assert result.status == "ok"
            assert result.current_state in (0, 1, 2, 3)
            assert result.current_state_name in STATE_NAMES.values()
            assert len(result.state_probabilities) == 4
            # 概率和应接近 1
            assert np.isclose(sum(result.state_probabilities), 1.0, atol=0.01)

    def test_result_has_complete_fields(self):
        obs = np.random.default_rng(42).normal(0, 1, (30, 3))
        with patch("quant_engine.hmm_market._extract_market_features", return_value=obs):
            result = detect_market_state(force_rerun=True)
            if result.status == "ok":
                assert result.transition_matrix  # 非空
                assert result.means  # 非空
                assert result.vars_  # 非空
                assert np.isfinite(result.log_likelihood)
                assert len(result.state_sequence) == len(obs)
