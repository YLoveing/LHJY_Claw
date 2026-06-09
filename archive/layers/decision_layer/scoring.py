"""
评分引擎 — 多因子综合评分。

从 src/stock_analyzer.py StockTrendAnalyzer._generate_signal 原样迁移。
保持纯函数风格，不持有状态。

评分体系（满分100）：
- 趋势 30分：多头排列得分高
- 乖离率 20分：接近 MA5 得分高
- 量能 15分：缩量回调得分高
- 支撑 10分：获得均线支撑得分高
- MACD 15分：金叉和多头得分高
- RSI 10分：超卖和强势得分高
"""

import logging
from typing import Optional

from .models import (
    BuySignal,
    MACDStatus,
    RSIStatus,
    ScoreBreakdown,
    TrendAnalysisResult,
    TrendStatus,
    VolumeStatus,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════
# 因子评分 — 每个因子对应独立的评分函数
# ═══════════════════════════════════════════


def score_trend(result: TrendAnalysisResult) -> float:
    """趋势评分（满分30分）。

    原逻辑对应 StockTrendAnalyzer._generate_signal 的趋势评分部分。
    """
    trend_scores = {
        TrendStatus.STRONG_BULL: 30,
        TrendStatus.BULL: 26,
        TrendStatus.WEAK_BULL: 18,
        TrendStatus.CONSOLIDATION: 12,
        TrendStatus.WEAK_BEAR: 8,
        TrendStatus.BEAR: 4,
        TrendStatus.STRONG_BEAR: 0,
    }
    return float(trend_scores.get(result.trend_status, 12))


def score_bias(
    result: TrendAnalysisResult,
    bias_threshold: float = 5.0,
) -> float:
    """乖离率评分（满分20分）。

    原逻辑对应 StockTrendAnalyzer._generate_signal 的乖离率评分部分。
    支持强势趋势补偿（STRONG_BULL 时阈值放宽 1.5 倍）。

    Args:
        result: 趋势分析结果
        bias_threshold: 乖离率阈值（%），默认 5.0

    Returns:
        乖离率评分 0-20
    """
    bias = result.bias_ma5
    if bias != bias or bias is None:  # NaN or None defense
        bias = 0.0

    # Strong trend compensation: relax threshold for STRONG_BULL with high strength
    trend_strength = result.trend_strength if result.trend_strength == result.trend_strength else 0.0
    if result.trend_status == TrendStatus.STRONG_BULL and (trend_strength or 0) >= 70:
        effective_threshold = bias_threshold * 1.5
    else:
        effective_threshold = bias_threshold

    if bias < 0:
        # Price below MA5 (pullback)
        if bias > -3:
            return 20.0
        elif bias > -5:
            return 16.0
        else:
            return 8.0
    elif bias < 2:
        return 18.0
    elif bias < bias_threshold:
        return 14.0
    elif bias > effective_threshold:
        return 4.0
    elif bias > bias_threshold:
        # Only reachable when is_strong_trend is True
        return 10.0
    else:
        return 4.0


def score_volume(result: TrendAnalysisResult) -> float:
    """量能评分（满分15分）。

    原逻辑对应 StockTrendAnalyzer._generate_signal 的量能评分部分。
    """
    volume_scores = {
        VolumeStatus.SHRINK_VOLUME_DOWN: 15,  # 缩量回调最佳
        VolumeStatus.HEAVY_VOLUME_UP: 12,  # 放量上涨次之
        VolumeStatus.NORMAL: 10,
        VolumeStatus.SHRINK_VOLUME_UP: 6,  # 无量上涨较差
        VolumeStatus.HEAVY_VOLUME_DOWN: 0,  # 放量下跌最差
    }
    return float(volume_scores.get(result.volume_status, 8))


def score_support(result: TrendAnalysisResult) -> float:
    """支撑评分（满分10分）。

    原逻辑对应 StockTrendAnalyzer._generate_signal 的支撑评分部分。
    MA5 支撑 +5分，MA10 支撑 +5分。
    """
    score = 0.0
    if result.support_ma5:
        score += 5
    if result.support_ma10:
        score += 5
    return score


def score_macd(result: TrendAnalysisResult) -> float:
    """MACD 评分（满分15分）。

    原逻辑对应 StockTrendAnalyzer._generate_signal 的 MACD 评分部分。
    """
    macd_scores = {
        MACDStatus.GOLDEN_CROSS_ZERO: 15,  # 零轴上金叉最强
        MACDStatus.GOLDEN_CROSS: 12,  # 金叉
        MACDStatus.CROSSING_UP: 10,  # 上穿零轴
        MACDStatus.BULLISH: 8,  # 多头
        MACDStatus.BEARISH: 2,  # 空头
        MACDStatus.CROSSING_DOWN: 0,  # 下穿零轴
        MACDStatus.DEATH_CROSS: 0,  # 死叉
    }
    return float(macd_scores.get(result.macd_status, 5))


def score_rsi(result: TrendAnalysisResult) -> float:
    """RSI 评分（满分10分）。

    原逻辑对应 StockTrendAnalyzer._generate_signal 的 RSI 评分部分。
    """
    rsi_scores = {
        RSIStatus.OVERSOLD: 10,  # 超卖最佳
        RSIStatus.STRONG_BUY: 8,  # 强势
        RSIStatus.NEUTRAL: 5,  # 中性
        RSIStatus.WEAK: 3,  # 弱势
        RSIStatus.OVERBOUGHT: 0,  # 超买最差
    }
    return float(rsi_scores.get(result.rsi_status, 5))


# ═══════════════════════════════════════════
# 综合评分
# ═══════════════════════════════════════════


def composite_scoring(
    result: TrendAnalysisResult,
    bias_threshold: float = 5.0,
) -> ScoreBreakdown:
    """多因子综合评分。

    计算所有因子评分，返回 ScoreBreakdown。

    Args:
        result: 趋势分析结果（含技术指标）
        bias_threshold: 乖离率阈值（%），默认 5.0

    Returns:
        评分明细，含 total 属性
    """
    breakdown = ScoreBreakdown(
        trend=score_trend(result),
        bias=score_bias(result, bias_threshold),
        volume=score_volume(result),
        support=score_support(result),
        macd=score_macd(result),
        rsi=score_rsi(result),
    )
    return breakdown


# ═══════════════════════════════════════════
# 理由/风险生成（辅助函数）
# ═══════════════════════════════════════════


def generate_reasons(result: TrendAnalysisResult) -> list:
    """生成买入理由列表。

    原逻辑对应 StockTrendAnalyzer._generate_signal 的理由追加部分。
    """
    reasons = []

    # 趋势理由
    if result.trend_status in [TrendStatus.STRONG_BULL, TrendStatus.BULL]:
        reasons.append(f"✅ {result.trend_status.value}，顺势做多")

    # 乖离率理由（在 score_bias 中已隐含，这里补充文字）
    bias = result.bias_ma5 if result.bias_ma5 == result.bias_ma5 else 0.0
    if bias < 0:
        if bias > -3:
            reasons.append(f"✅ 价格略低于MA5({bias:.1f}%)，回踩买点")
        elif bias > -5:
            reasons.append(f"✅ 价格回踩MA5({bias:.1f}%)，观察支撑")
    elif bias < 2:
        reasons.append(f"✅ 价格贴近MA5({bias:.1f}%)，介入好时机")
    elif bias < 5:
        reasons.append(f"⚡ 价格略高于MA5({bias:.1f}%)，可小仓介入")

    # 量能理由
    if result.volume_status == VolumeStatus.SHRINK_VOLUME_DOWN:
        reasons.append("✅ 缩量回调，主力洗盘")

    # 支撑理由
    if result.support_ma5:
        reasons.append("✅ MA5支撑有效")
    if result.support_ma10:
        reasons.append("✅ MA10支撑有效")

    # MACD 理由
    if result.macd_status in [MACDStatus.GOLDEN_CROSS_ZERO, MACDStatus.GOLDEN_CROSS]:
        reasons.append(f"✅ {result.macd_signal}")
    elif result.macd_status not in [MACDStatus.DEATH_CROSS, MACDStatus.CROSSING_DOWN]:
        reasons.append(result.macd_signal)

    # RSI 理由
    if result.rsi_status in [RSIStatus.OVERSOLD, RSIStatus.STRONG_BUY]:
        reasons.append(f"✅ {result.rsi_signal}")
    elif result.rsi_status != RSIStatus.OVERBOUGHT:
        reasons.append(result.rsi_signal)

    return reasons


def generate_risks(result: TrendAnalysisResult) -> list:
    """生成风险因素列表。

    原逻辑对应 StockTrendAnalyzer._generate_signal 的风险追加部分。
    """
    risks = []

    # 趋势风险
    if result.trend_status in [TrendStatus.BEAR, TrendStatus.STRONG_BEAR]:
        risks.append(f"⚠️ {result.trend_status.value}，不宜做多")

    # 乖离率风险
    bias = result.bias_ma5 if result.bias_ma5 == result.bias_ma5 else 0.0
    if bias > 5:
        risks.append(f"❌ 乖离率过高({bias:.1f}%>5.0%)，严禁追高！")
    elif bias < -5:
        risks.append(f"⚠️ 乖离率过大({bias:.1f}%)，可能破位")

    # 量能风险
    if result.volume_status == VolumeStatus.HEAVY_VOLUME_DOWN:
        risks.append("⚠️ 放量下跌，注意风险")

    # MACD 风险
    if result.macd_status in [MACDStatus.DEATH_CROSS, MACDStatus.CROSSING_DOWN]:
        risks.append(f"⚠️ {result.macd_signal}")

    # RSI 风险
    if result.rsi_status == RSIStatus.OVERBOUGHT:
        risks.append(f"⚠️ {result.rsi_signal}")

    return risks


# ═══════════════════════════════════════════
# 评分 → 信号映射
# ═══════════════════════════════════════════


def score_to_buy_signal(
    score: int,
    trend_status: TrendStatus,
) -> BuySignal:
    """根据综合评分和趋势状态生成买入信号。

    原逻辑对应 StockTrendAnalyzer._generate_signal 的综合判断部分。

    Args:
        score: 综合评分 0-100
        trend_status: 趋势状态

    Returns:
        BuySignal 枚举值
    """
    if score >= 75 and trend_status in [TrendStatus.STRONG_BULL, TrendStatus.BULL]:
        return BuySignal.STRONG_BUY
    elif score >= 60 and trend_status in [TrendStatus.STRONG_BULL, TrendStatus.BULL, TrendStatus.WEAK_BULL]:
        return BuySignal.BUY
    elif score >= 45:
        return BuySignal.HOLD
    elif score >= 30:
        return BuySignal.WAIT
    elif trend_status in [TrendStatus.BEAR, TrendStatus.STRONG_BEAR]:
        return BuySignal.STRONG_SELL
    else:
        return BuySignal.SELL


def run_scoring(
    result: TrendAnalysisResult,
    bias_threshold: float = 5.0,
) -> TrendAnalysisResult:
    """一站式评分入口 — 计算评分、理由、风险、信号，写入 result。

    保持与 StockTrendAnalyzer._generate_signal 一致的语义。

    Args:
        result: 趋势分析结果（含技术指标）
        bias_threshold: 乖离率阈值（%）

    Returns:
        更新后的 TrendAnalysisResult（signal_score, signal_reasons, risk_factors, buy_signal 已填充）
    """
    breakdown = composite_scoring(result, bias_threshold)
    score = int(round(breakdown.total))
    reasons = generate_reasons(result)
    risks = generate_risks(result)
    buy_signal = score_to_buy_signal(score, result.trend_status)

    result.signal_score = score
    result.signal_reasons = reasons
    result.risk_factors = risks
    result.buy_signal = buy_signal

    return result


__all__ = [
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
]
