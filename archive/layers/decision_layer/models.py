"""
决策层数据模型 — 评分、信号、策略。

职责边界：
- 定义决策层内部和对外暴露的数据结构（评分因子、信号、策略配置）
- 输出信号给 execution_layer 消费
- 不包含任何计算逻辑

所有模型均为 dataclass，支持序列化（to_dict / from_dict）。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

# ═══════════════════════════════════════════
# 枚举 — 从 src/stock_analyzer.py 原样迁移
# ═══════════════════════════════════════════


class TrendStatus(Enum):
    """趋势状态枚举"""

    STRONG_BULL = "强势多头"  # MA5 > MA10 > MA20，且间距扩大
    BULL = "多头排列"  # MA5 > MA10 > MA20
    WEAK_BULL = "弱势多头"  # MA5 > MA10，但 MA10 < MA20
    CONSOLIDATION = "盘整"  # 均线缠绕
    WEAK_BEAR = "弱势空头"  # MA5 < MA10，但 MA10 > MA20
    BEAR = "空头排列"  # MA5 < MA10 < MA20
    STRONG_BEAR = "强势空头"  # MA5 < MA10 < MA20，且间距扩大


class VolumeStatus(Enum):
    """量能状态枚举"""

    HEAVY_VOLUME_UP = "放量上涨"  # 量价齐升
    HEAVY_VOLUME_DOWN = "放量下跌"  # 放量杀跌
    SHRINK_VOLUME_UP = "缩量上涨"  # 无量上涨
    SHRINK_VOLUME_DOWN = "缩量回调"  # 缩量回调（好）
    NORMAL = "量能正常"


class BuySignal(Enum):
    """买入信号枚举"""

    STRONG_BUY = "强烈买入"
    BUY = "买入"
    HOLD = "持有"
    WAIT = "观望"
    SELL = "卖出"
    STRONG_SELL = "强烈卖出"


class MACDStatus(Enum):
    """MACD状态枚举"""

    GOLDEN_CROSS_ZERO = "零轴上金叉"  # DIF上穿DEA，且在零轴上方
    GOLDEN_CROSS = "金叉"  # DIF上穿DEA
    BULLISH = "多头"  # DIF>DEA>0
    CROSSING_UP = "上穿零轴"  # DIF上穿零轴
    CROSSING_DOWN = "下穿零轴"  # DIF下穿零轴
    BEARISH = "空头"  # DIF<DEA<0
    DEATH_CROSS = "死叉"  # DIF下穿DEA


class RSIStatus(Enum):
    """RSI状态枚举"""

    OVERBOUGHT = "超买"  # RSI > 70
    STRONG_BUY = "强势买入"  # 50 < RSI < 70
    NEUTRAL = "中性"  # 40 <= RSI <= 60
    WEAK = "弱势"  # 30 < RSI < 40
    OVERSOLD = "超卖"  # RSI < 30


class DecisionType(Enum):
    """决策类型 — 最终输出给 execution_layer"""

    STRONG_BUY = "strong_buy"
    BUY = "buy"
    HOLD = "hold"
    WAIT = "wait"
    SELL = "sell"
    STRONG_SELL = "strong_sell"

    @classmethod
    def from_buy_signal(cls, signal: BuySignal) -> "DecisionType":
        mapping = {
            BuySignal.STRONG_BUY: cls.STRONG_BUY,
            BuySignal.BUY: cls.BUY,
            BuySignal.HOLD: cls.HOLD,
            BuySignal.WAIT: cls.WAIT,
            BuySignal.SELL: cls.SELL,
            BuySignal.STRONG_SELL: cls.STRONG_SELL,
        }
        return mapping.get(signal, cls.HOLD)

    @classmethod
    def from_advice(cls, advice: str) -> "DecisionType":
        """从 LLM 操作建议推断决策类型。"""
        advice_lower = advice.strip().lower()
        buy_keywords = ["买入", "加仓", "buy", "add"]
        sell_keywords = ["卖出", "减仓", "sell", "reduce"]
        for kw in buy_keywords:
            if kw in advice_lower:
                return cls.BUY
        for kw in sell_keywords:
            if kw in advice_lower:
                return cls.SELL
        return cls.HOLD


# ═══════════════════════════════════════════
# 技术分析结果 — 从 src/stock_analyzer.py 原样迁移
# ═══════════════════════════════════════════


@dataclass
class TrendAnalysisResult:
    """趋势分析结果"""

    code: str

    # 趋势判断
    trend_status: TrendStatus = TrendStatus.CONSOLIDATION
    ma_alignment: str = ""
    trend_strength: float = 0.0  # 趋势强度 0-100

    # 均线数据
    ma5: float = 0.0
    ma10: float = 0.0
    ma20: float = 0.0
    ma60: float = 0.0
    current_price: float = 0.0

    # 乖离率（与 MA5 的偏离度）
    bias_ma5: float = 0.0
    bias_ma10: float = 0.0
    bias_ma20: float = 0.0

    # 量能分析
    volume_status: VolumeStatus = VolumeStatus.NORMAL
    volume_ratio_5d: float = 0.0
    volume_trend: str = ""

    # 支撑压力
    support_ma5: bool = False
    support_ma10: bool = False
    resistance_levels: List[float] = field(default_factory=list)
    support_levels: List[float] = field(default_factory=list)

    # MACD 指标
    macd_dif: float = 0.0
    macd_dea: float = 0.0
    macd_bar: float = 0.0
    macd_status: MACDStatus = MACDStatus.BULLISH
    macd_signal: str = ""

    # RSI 指标
    rsi_6: float = 0.0
    rsi_12: float = 0.0
    rsi_24: float = 0.0
    rsi_status: RSIStatus = RSIStatus.NEUTRAL
    rsi_signal: str = ""

    # 买入信号
    buy_signal: BuySignal = BuySignal.WAIT
    signal_score: int = 0
    signal_reasons: List[str] = field(default_factory=list)
    risk_factors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "trend_status": self.trend_status.value,
            "ma_alignment": self.ma_alignment,
            "trend_strength": self.trend_strength,
            "ma5": self.ma5,
            "ma10": self.ma10,
            "ma20": self.ma20,
            "ma60": self.ma60,
            "current_price": self.current_price,
            "bias_ma5": self.bias_ma5,
            "bias_ma10": self.bias_ma10,
            "bias_ma20": self.bias_ma20,
            "volume_status": self.volume_status.value,
            "volume_ratio_5d": self.volume_ratio_5d,
            "volume_trend": self.volume_trend,
            "support_ma5": self.support_ma5,
            "support_ma10": self.support_ma10,
            "buy_signal": self.buy_signal.value,
            "signal_score": self.signal_score,
            "signal_reasons": self.signal_reasons,
            "risk_factors": self.risk_factors,
            "macd_dif": self.macd_dif,
            "macd_dea": self.macd_dea,
            "macd_bar": self.macd_bar,
            "macd_status": self.macd_status.value,
            "macd_signal": self.macd_signal,
            "rsi_6": self.rsi_6,
            "rsi_12": self.rsi_12,
            "rsi_24": self.rsi_24,
            "rsi_status": self.rsi_status.value,
            "rsi_signal": self.rsi_signal,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TrendAnalysisResult":
        result = cls(code=d.get("code", ""))
        for key, value in d.items():
            if hasattr(result, key):
                # Handle enum fields
                if key == "trend_status" and isinstance(value, str):
                    try:
                        result.trend_status = TrendStatus(value)
                    except ValueError:
                        pass
                elif key == "volume_status" and isinstance(value, str):
                    try:
                        result.volume_status = VolumeStatus(value)
                    except ValueError:
                        pass
                elif key == "macd_status" and isinstance(value, str):
                    try:
                        result.macd_status = MACDStatus(value)
                    except ValueError:
                        pass
                elif key == "rsi_status" and isinstance(value, str):
                    try:
                        result.rsi_status = RSIStatus(value)
                    except ValueError:
                        pass
                elif key == "buy_signal" and isinstance(value, str):
                    try:
                        result.buy_signal = BuySignal(value)
                    except ValueError:
                        pass
                elif key not in ("code",):
                    setattr(result, key, value)
        return result


# ═══════════════════════════════════════════
# 评分因子分解 — 让调用方了解评分明细
# ═══════════════════════════════════════════


@dataclass
class ScoreBreakdown:
    """评分因子分解明细"""

    trend: float = 0.0  # 趋势评分 0-30
    bias: float = 0.0  # 乖离率评分 0-20
    volume: float = 0.0  # 量能评分 0-15
    support: float = 0.0  # 支撑评分 0-10
    macd: float = 0.0  # MACD评分 0-15
    rsi: float = 0.0  # RSI评分 0-10

    @property
    def total(self) -> float:
        return self.trend + self.bias + self.volume + self.support + self.macd + self.rsi

    def to_dict(self) -> Dict[str, float]:
        return {
            "trend": self.trend,
            "bias": self.bias,
            "volume": self.volume,
            "support": self.support,
            "macd": self.macd,
            "rsi": self.rsi,
            "total": self.total,
        }


# ═══════════════════════════════════════════
# 决策信号 — 输出给 execution_layer
# ═══════════════════════════════════════════


@dataclass
class DecisionSignal:
    """最终决策信号 — 决策层输出给执行层的唯一接口。"""

    code: str
    decision: DecisionType  # 决策类型
    score: int  # 综合评分 0-100
    score_breakdown: Optional[ScoreBreakdown] = None  # 评分明细（技术面评分）
    confidence: str = "中"  # 置信度：高/中/低
    reasons: List[str] = field(default_factory=list)  # 决策理由
    risks: List[str] = field(default_factory=list)  # 风险因素

    # 选填 — 来自 LLM 分析
    trend_prediction: str = ""
    operation_advice: str = ""
    analysis_summary: str = ""

    # 元数据
    source: str = "technical"  # technical / llm / hybrid
    strategy_name: str = "main"
    model_used: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "decision": self.decision.value,
            "score": self.score,
            "score_breakdown": self.score_breakdown.to_dict() if self.score_breakdown else None,
            "confidence": self.confidence,
            "reasons": self.reasons,
            "risks": self.risks,
            "trend_prediction": self.trend_prediction,
            "operation_advice": self.operation_advice,
            "analysis_summary": self.analysis_summary,
            "source": self.source,
            "strategy_name": self.strategy_name,
            "model_used": self.model_used,
        }

    @property
    def is_buy(self) -> bool:
        return self.decision in (DecisionType.STRONG_BUY, DecisionType.BUY)

    @property
    def is_sell(self) -> bool:
        return self.decision in (DecisionType.STRONG_SELL, DecisionType.SELL)

    @property
    def is_hold(self) -> bool:
        return self.decision == DecisionType.HOLD


# ═══════════════════════════════════════════
# LLM 分析结果（轻量 — 来自 analyzer.py 的 AnalysisResult 核心字段）
# ═══════════════════════════════════════════


@dataclass
class LLMAnalysisResult:
    """LLM 分析结果 — 决策层视角的轻量封装。

    从 src/analyzer.py AnalysisResult 提取核心字段。
    """

    code: str
    name: str
    sentiment_score: int  # 综合评分 0-100
    trend_prediction: str  # 趋势预测
    operation_advice: str  # 操作建议
    decision_type: str = "hold"  # buy/hold/sell
    confidence_level: str = "中"  # 高/中/低
    analysis_summary: str = ""
    risk_warning: str = ""
    buy_reason: str = ""
    dashboard: Optional[Dict[str, Any]] = None
    model_used: Optional[str] = None
    success: bool = True
    error_message: Optional[str] = None
    raw_response: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "sentiment_score": self.sentiment_score,
            "trend_prediction": self.trend_prediction,
            "operation_advice": self.operation_advice,
            "decision_type": self.decision_type,
            "confidence_level": self.confidence_level,
            "analysis_summary": self.analysis_summary,
            "risk_warning": self.risk_warning,
            "buy_reason": self.buy_reason,
            "dashboard": self.dashboard,
            "model_used": self.model_used,
            "success": self.success,
        }


__all__ = [
    # Enums
    "TrendStatus",
    "VolumeStatus",
    "BuySignal",
    "MACDStatus",
    "RSIStatus",
    "DecisionType",
    # Data models
    "TrendAnalysisResult",
    "ScoreBreakdown",
    "DecisionSignal",
    "LLMAnalysisResult",
]
