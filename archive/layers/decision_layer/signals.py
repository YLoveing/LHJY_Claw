"""
信号生成 — 评分 → 决策信号（Buy / Sell / Hold）。

职责：
- 从 TrendAnalysisResult 生成 DecisionSignal
- 融合 LLMAnalysisResult（可选）生成混合信号
- 输出给 execution_layer
"""

import logging
from typing import Optional

from .models import (
    BuySignal,
    DecisionSignal,
    DecisionType,
    LLMAnalysisResult,
    ScoreBreakdown,
    TrendAnalysisResult,
    TrendStatus,
)
from .scoring import (
    composite_scoring,
    generate_reasons,
    generate_risks,
    score_to_buy_signal,
)

logger = logging.getLogger(__name__)


def _resolve_decision_type(
    score: int,
    trend_status: TrendStatus,
    buy_signal: Optional[BuySignal] = None,
    llm_result: Optional[LLMAnalysisResult] = None,
) -> DecisionType:
    """综合技术面评分和 LLM 建议，确定最终决策类型。

    优先级：技术面 BuySignal > LLM decision_type（当冲突时以技术面为准）。
    """
    if buy_signal is None:
        buy_signal = score_to_buy_signal(score, trend_status)

    # 从技术面信号直接映射
    tech_decision = DecisionType.from_buy_signal(buy_signal)

    # 融合 LLM（可选）-> 仅在 LLM 也是同一方向时才升级
    if llm_result and llm_result.success:
        llm_decision = DecisionType.from_advice(llm_result.operation_advice)
        # 如果 LLM 和技术的方向一致，升级信号
        if llm_decision == DecisionType.STRONG_SELL and tech_decision in (DecisionType.SELL, DecisionType.WAIT):
            return DecisionType.STRONG_SELL
        elif llm_decision == DecisionType.SELL and tech_decision in (DecisionType.WAIT, DecisionType.HOLD):
            return DecisionType.SELL
        elif llm_decision == DecisionType.STRONG_BUY and tech_decision in (DecisionType.BUY, DecisionType.HOLD):
            return DecisionType.STRONG_BUY
        elif llm_decision == DecisionType.BUY and tech_decision in (DecisionType.HOLD, DecisionType.WAIT):
            return DecisionType.BUY

    return tech_decision


def _resolve_confidence(
    score: int,
    llm_result: Optional[LLMAnalysisResult] = None,
) -> str:
    """根据评分和 LLM 置信度确定最终置信度。

    评分越高，置信度越高。LLM 置信度作为辅助参考。
    """
    if score >= 75:
        base = "高"
    elif score >= 60:
        base = "高"
    elif score >= 45:
        base = "中"
    elif score >= 30:
        base = "中"
    else:
        base = "低"

    # LLM 辅助判断：如果 LLM 给的置信度更低且评分处于边界，降一级
    if llm_result and llm_result.success:
        llm_conf = str(llm_result.confidence_level or "中").strip()
        if llm_conf == "低" and base == "中":
            return "低"
        elif llm_conf == "高" and base == "中":
            return "高"

    return base


def generate_signal(
    result: TrendAnalysisResult,
    bias_threshold: float = 5.0,
    llm_result: Optional[LLMAnalysisResult] = None,
    strategy_name: str = "main",
) -> DecisionSignal:
    """从技术分析结果生成决策信号。

    单一入口：接收 TrendAnalysisResult（必选）+ LLMAnalysisResult（可选），
    输出 DecisionSignal 供 execution_layer 消费。

    Args:
        result: 技术面趋势分析结果
        bias_threshold: 乖离率阈值（%）
        llm_result: 可选的 LLM 分析结果
        strategy_name: 策略名称

    Returns:
        DecisionSignal 对象
    """
    # 1. 评分
    breakdown = composite_scoring(result, bias_threshold)
    score = int(round(breakdown.total))
    reasons = generate_reasons(result)
    risks = generate_risks(result)
    buy_signal = score_to_buy_signal(score, result.trend_status)

    # 2. 融合 LLM 理由（可选）
    if llm_result and llm_result.success:
        if llm_result.analysis_summary:
            reasons.append(f"🤖 LLM: {llm_result.analysis_summary}")
        if llm_result.risk_warning:
            risks.append(f"🤖 LLM风险: {llm_result.risk_warning}")

    # 3. 确定最终决策
    decision = _resolve_decision_type(score, result.trend_status, buy_signal, llm_result)
    confidence = _resolve_confidence(score, llm_result)

    # 4. 确定信号来源
    source = "hybrid" if (llm_result and llm_result.success) else "technical"

    return DecisionSignal(
        code=result.code,
        decision=decision,
        score=score,
        score_breakdown=breakdown,
        confidence=confidence,
        reasons=reasons,
        risks=risks,
        trend_prediction=llm_result.trend_prediction if llm_result else "",
        operation_advice=llm_result.operation_advice if llm_result else "",
        analysis_summary=llm_result.analysis_summary if llm_result else "",
        source=source,
        strategy_name=strategy_name,
        model_used=llm_result.model_used if llm_result else None,
    )


__all__ = [
    "generate_signal",
    "_resolve_decision_type",
    "_resolve_confidence",
]
