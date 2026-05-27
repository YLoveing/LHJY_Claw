"""
decision_layer — 决策层

职责边界：
- 接收 data_layer 的技术面数据 + risk_layer 的风控信息
- 技术评分（多因子综合评分，迁移自 src/stock_analyzer.py）
- LLM 分析（封装 DeepSeek 调用，迁移自 src/analyzer.py）
- 生成决策信号 → 输出给 execution_layer

单向依赖：
- 依赖 data_layer（数据输入）
- 依赖 risk_layer（风控信息，如仓位约束）
- 不依赖 execution_layer

模块结构：
- models.py    — 数据模型（枚举、dataclass）
- scoring.py   — 多因子评分引擎
- signals.py   — 信号生成（Buy/Sell/Hold）
- strategy.py  — 策略配置与选择
- engine.py    — LLM 决策引擎（DeepSeek）
- manager.py   — DecisionManager 编排器
"""

from .models import (
    TrendStatus,
    VolumeStatus,
    BuySignal,
    MACDStatus,
    RSIStatus,
    DecisionType,
    TrendAnalysisResult,
    ScoreBreakdown,
    DecisionSignal,
    LLMAnalysisResult,
)
from .scoring import (
    score_trend,
    score_bias,
    score_volume,
    score_support,
    score_macd,
    score_rsi,
    composite_scoring,
    generate_reasons,
    generate_risks,
    score_to_buy_signal,
    run_scoring,
)
from .signals import generate_signal
from .strategy import (
    StrategyConfig,
    default_strategy,
    conservative_strategy,
    aggressive_strategy,
    STRATEGY_REGISTRY,
    get_strategy,
    register_strategy,
)
from .engine import DecisionEngine, DecisionEngineConfig
from .manager import DecisionManager, AnalysisInput

__all__ = [
    # models
    "TrendStatus",
    "VolumeStatus",
    "BuySignal",
    "MACDStatus",
    "RSIStatus",
    "DecisionType",
    "TrendAnalysisResult",
    "ScoreBreakdown",
    "DecisionSignal",
    "LLMAnalysisResult",
    # scoring
    "score_trend",
    "score_bias",
    "score_volume",
    "score_support",
    "score_macd",
    "score_rsi",
    "composite_scoring",
    "generate_reasons",
    "generate_risks",
    "score_to_buy_signal",
    "run_scoring",
    # signals
    "generate_signal",
    # strategy
    "StrategyConfig",
    "default_strategy",
    "conservative_strategy",
    "aggressive_strategy",
    "STRATEGY_REGISTRY",
    "get_strategy",
    "register_strategy",
    # engine
    "DecisionEngine",
    "DecisionEngineConfig",
    # manager
    "DecisionManager",
    "AnalysisInput",
]
