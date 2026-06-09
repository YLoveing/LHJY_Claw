"""
DecisionManager — 决策层编排器。

职责：
- 接收 data_layer / risk_layer 的数据
- 协调技术评分 + LLM 分析
- 生成最终 DecisionSignal → 输出给 execution_layer
- 策略选择与参数注入

单向依赖：
- 依赖 data_layer（获取技术数据）
- 依赖 risk_layer（获取风控信息）
- 不依赖 execution_layer
"""

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .engine import DecisionEngine, DecisionEngineConfig
from .models import (
    DecisionSignal,
    DecisionType,
    LLMAnalysisResult,
    ScoreBreakdown,
    TrendAnalysisResult,
    TrendStatus,
)
from .scoring import run_scoring
from .signals import generate_signal
from .strategy import StrategyConfig, get_strategy

logger = logging.getLogger(__name__)


@dataclass
class AnalysisInput:
    """决策层输入 — 接收来自 data_layer 和 risk_layer 的数据。

    Args:
        code: 股票代码
        name: 股票名称
        df: 包含 OHLCV 数据的 DataFrame（用于技术面分析）
        technical_context: 技术面上下文（dict 格式，供 LLM prompt 使用）
        news_context: 新闻内容（可选，供 LLM 使用）
        market_snapshot: 实时行情快照（可选）
    """

    code: str
    name: str = ""
    df: Any = None  # pd.DataFrame
    technical_context: Optional[Dict[str, Any]] = None
    news_context: Optional[str] = None
    market_snapshot: Optional[Dict[str, Any]] = None

    # 预分析结果（可选 — 如果上游已经做了技术分析）
    pre_analyzed: Optional[TrendAnalysisResult] = None


class DecisionManager:
    """决策管理器 — 一站式决策入口。

    接收 AnalysisInput，输出 DecisionSignal。
    协调技术评分 + LLM 分析 + 信号生成。

    使用方式：
        mgr = DecisionManager()
        signal = mgr.make_decision(input_data)
    """

    def __init__(
        self,
        strategy: Optional[StrategyConfig] = None,
        llm_engine: Optional[DecisionEngine] = None,
        llm_enabled: bool = True,
    ):
        """
        Args:
            strategy: 策略配置（默认使用 main 策略）
            llm_engine: LLM 引擎实例（可选）
            llm_enabled: 是否启用 LLM 分析
        """
        self.strategy = strategy or get_strategy("main")
        self._llm_engine = llm_engine
        self._llm_enabled = llm_enabled and llm_engine is not None
        self._engine: Optional[DecisionEngine] = llm_engine

    @property
    def llm_enabled(self) -> bool:
        """LLM 是否已启用。"""
        return self._llm_enabled and self._engine is not None

    # ═══════════════════════════════════════
    # 一站式决策入口
    # ═══════════════════════════════════════

    def make_decision(
        self,
        input_data: AnalysisInput,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> DecisionSignal:
        """一站式决策：技术分析 + LLM（可选）+ 信号生成。

        Args:
            input_data: 分析输入
            progress_callback: 进度回调 (progress, message)

        Returns:
            DecisionSignal 对象
        """
        code = input_data.code
        name = input_data.name or code

        def _emit(progress: int, message: str) -> None:
            if progress_callback:
                try:
                    progress_callback(progress, message)
                except Exception:
                    pass

        # Step 1: 技术面分析（如果上游没做）
        trend_result = input_data.pre_analyzed
        if trend_result is None and input_data.df is not None:
            _emit(20, f"{name}：技术分析中...")
            trend_result = self._analyze_technical(input_data.df, code)
        elif trend_result is None:
            # 用默认空结果
            trend_result = TrendAnalysisResult(code=code)
            trend_result.risk_factors.append("缺少技术分析数据")

        # Step 2: 技术评分
        _emit(40, f"{name}：技术评分中...")
        run_scoring(trend_result, bias_threshold=self.strategy.bias_threshold)

        # Step 3: LLM 分析（可选）
        llm_result: Optional[LLMAnalysisResult] = None
        if self.llm_enabled and input_data.technical_context:
            _emit(60, f"{name}：LLM 分析中...")
            llm_result = self._analyze_llm(
                code=code,
                name=name,
                technical_context=input_data.technical_context,
                news_context=input_data.news_context,
                stock_name=name,
                progress_callback=progress_callback,
            )
        elif not self.llm_enabled:
            logger.debug("[DecisionManager] LLM 分析已禁用，仅使用技术评分")

        # Step 4: 生成决策信号
        _emit(90, f"{name}：生成信号中...")
        signal = generate_signal(
            result=trend_result,
            bias_threshold=self.strategy.bias_threshold,
            llm_result=llm_result,
            strategy_name=self.strategy.name,
        )

        logger.info(
            "[DecisionManager] %s(%s) → %s (score=%d, source=%s)",
            name,
            code,
            signal.decision.value,
            signal.score,
            signal.source,
        )

        return signal

    # ═══════════════════════════════════════
    # 批量决策
    # ═══════════════════════════════════════

    def make_decisions(
        self,
        inputs: List[AnalysisInput],
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> List[DecisionSignal]:
        """批量决策入口。

        Args:
            inputs: 分析输入列表
            progress_callback: 进度回调

        Returns:
            决策信号列表（顺序与输入一致）
        """
        results: List[DecisionSignal] = []
        total = len(inputs)

        for i, inp in enumerate(inputs):
            logger.info("[DecisionManager] Batch [%d/%d] %s", i + 1, total, inp.code)
            try:
                signal = self.make_decision(inp, progress_callback)
                results.append(signal)
            except Exception as e:
                logger.error("[DecisionManager] %s failed: %s", inp.code, e)
                results.append(
                    DecisionSignal(
                        code=inp.code,
                        decision=DecisionType.HOLD,
                        score=0,
                        reasons=[f"DecisionMaker error: {e}"],
                        risks=["analysis error"],
                        source="technical",
                    )
                )

        return results

    # ═══════════════════════════════════════
    # 内部方法
    # ═══════════════════════════════════════

    def _analyze_technical(self, df: Any, code: str) -> TrendAnalysisResult:
        """技术分析 — 计算均线、MACD、RSI 等指标。

        使用 data_layer 或内置计算函数。
        这里对 StockTrendAnalyzer 做了一个简易内联迁移。
        """
        import pandas as pd

        result = TrendAnalysisResult(code=code)

        if df is None or df.empty or len(df) < 20:
            logger.warning("[DecisionManager] %s 数据不足", code)
            result.risk_factors.append("数据不足")
            return result

        df = df.sort_values("date").reset_index(drop=True) if "date" in df.columns else df

        # 计算均线
        df["MA5"] = df["close"].rolling(window=5).mean()
        df["MA10"] = df["close"].rolling(window=10).mean()
        df["MA20"] = df["close"].rolling(window=20).mean()
        df["MA60"] = df["close"].rolling(window=60).mean() if len(df) >= 60 else df["MA20"]

        # 计算 MACD
        ema_fast = df["close"].ewm(span=self.strategy.macd_fast, adjust=False).mean()
        ema_slow = df["close"].ewm(span=self.strategy.macd_slow, adjust=False).mean()
        df["MACD_DIF"] = ema_fast - ema_slow
        df["MACD_DEA"] = df["MACD_DIF"].ewm(span=self.strategy.macd_signal, adjust=False).mean()
        df["MACD_BAR"] = (df["MACD_DIF"] - df["MACD_DEA"]) * 2

        # 计算 RSI
        for period in [self.strategy.rsi_short, self.strategy.rsi_mid, self.strategy.rsi_long]:
            delta = df["close"].diff()
            gain = delta.where(delta > 0, 0)
            loss = -delta.where(delta < 0, 0)
            avg_gain = gain.rolling(window=period).mean()
            avg_loss = loss.rolling(window=period).mean()
            rs = avg_gain / avg_loss
            df[f"RSI_{period}"] = (100 - (100 / (1 + rs))).fillna(50)

        latest = df.iloc[-1]
        result.current_price = float(latest["close"])
        result.ma5 = float(latest["MA5"])
        result.ma10 = float(latest["MA10"])
        result.ma20 = float(latest["MA20"])
        result.ma60 = float(latest.get("MA60", 0))

        # 趋势判断
        ma5, ma10, ma20 = result.ma5, result.ma10, result.ma20
        if ma5 > ma10 > ma20:
            prev = df.iloc[-5] if len(df) >= 5 else df.iloc[-1]
            prev_spread = (prev["MA5"] - prev["MA20"]) / prev["MA20"] * 100 if prev["MA20"] > 0 else 0
            curr_spread = (ma5 - ma20) / ma20 * 100 if ma20 > 0 else 0
            if curr_spread > prev_spread and curr_spread > 5:
                result.trend_status = TrendStatus.STRONG_BULL
                result.ma_alignment = "强势多头排列，均线发散上行"
                result.trend_strength = 90
            else:
                result.trend_status = TrendStatus.BULL
                result.ma_alignment = "多头排列 MA5>MA10>MA20"
                result.trend_strength = 75
        elif ma5 > ma10 and ma10 <= ma20:
            result.trend_status = TrendStatus.WEAK_BULL
            result.ma_alignment = "弱势多头，MA5>MA10 但 MA10≤MA20"
            result.trend_strength = 55
        elif ma5 < ma10 < ma20:
            prev = df.iloc[-5] if len(df) >= 5 else df.iloc[-1]
            prev_spread = (prev["MA20"] - prev["MA5"]) / prev["MA5"] * 100 if prev["MA5"] > 0 else 0
            curr_spread = (ma20 - ma5) / ma5 * 100 if ma5 > 0 else 0
            if curr_spread > prev_spread and curr_spread > 5:
                result.trend_status = TrendStatus.STRONG_BEAR
                result.ma_alignment = "强势空头排列，均线发散下行"
                result.trend_strength = 10
            else:
                result.trend_status = TrendStatus.BEAR
                result.ma_alignment = "空头排列 MA5<MA10<MA20"
                result.trend_strength = 25
        elif ma5 < ma10 and ma10 >= ma20:
            result.trend_status = TrendStatus.WEAK_BEAR
            result.ma_alignment = "弱势空头，MA5<MA10 但 MA10≥MA20"
            result.trend_strength = 40
        else:
            result.trend_status = TrendStatus.CONSOLIDATION
            result.ma_alignment = "均线缠绕，趋势不明"
            result.trend_strength = 50

        # 乖离率
        price = result.current_price
        if result.ma5 > 0:
            result.bias_ma5 = (price - result.ma5) / result.ma5 * 100
        if result.ma10 > 0:
            result.bias_ma10 = (price - result.ma10) / result.ma10 * 100
        if result.ma20 > 0:
            result.bias_ma20 = (price - result.ma20) / result.ma20 * 100

        # 量能分析
        if len(df) >= 5:
            vol_5d_avg = df["volume"].iloc[-6:-1].mean()
            if vol_5d_avg > 0:
                result.volume_ratio_5d = float(latest["volume"]) / vol_5d_avg
            prev_close = df.iloc[-2]["close"]
            price_change = (latest["close"] - prev_close) / prev_close * 100

            v_shrink = self.strategy.volume_shrink_ratio
            v_heavy = self.strategy.volume_heavy_ratio
            if result.volume_ratio_5d >= v_heavy:
                if price_change > 0:
                    result.volume_status = type(result.volume_status).HEAVY_VOLUME_UP
                    result.volume_trend = "放量上涨，多头力量强劲"
                else:
                    result.volume_status = type(result.volume_status).HEAVY_VOLUME_DOWN
                    result.volume_trend = "放量下跌，注意风险"
            elif result.volume_ratio_5d <= v_shrink:
                if price_change > 0:
                    result.volume_status = type(result.volume_status).SHRINK_VOLUME_UP
                    result.volume_trend = "缩量上涨，上攻动能不足"
                else:
                    result.volume_status = type(result.volume_status).SHRINK_VOLUME_DOWN
                    result.volume_trend = "缩量回调，洗盘特征明显（好）"
            else:
                result.volume_status = type(result.volume_status).NORMAL
                result.volume_trend = "量能正常"

        # 支撑
        tol = self.strategy.ma_support_tolerance
        if result.ma5 > 0:
            ma5_dist = abs(price - result.ma5) / result.ma5
            if ma5_dist <= tol and price >= result.ma5:
                result.support_ma5 = True
                result.support_levels.append(result.ma5)
        if result.ma10 > 0:
            ma10_dist = abs(price - result.ma10) / result.ma10
            if ma10_dist <= tol and price >= result.ma10:
                result.support_ma10 = True
                if result.ma10 not in result.support_levels:
                    result.support_levels.append(result.ma10)
        if result.ma20 > 0 and price >= result.ma20:
            result.support_levels.append(result.ma20)

        # MACD 分析
        if len(df) >= self.strategy.macd_slow:
            prev = df.iloc[-2]
            result.macd_dif = float(latest["MACD_DIF"])
            result.macd_dea = float(latest["MACD_DEA"])
            result.macd_bar = float(latest["MACD_BAR"])

            prev_dif_dea = prev["MACD_DIF"] - prev["MACD_DEA"]
            curr_dif_dea = result.macd_dif - result.macd_dea
            is_golden_cross = prev_dif_dea <= 0 and curr_dif_dea > 0
            is_death_cross = prev_dif_dea >= 0 and curr_dif_dea < 0
            is_crossing_up = prev["MACD_DIF"] <= 0 and result.macd_dif > 0
            is_crossing_down = prev["MACD_DIF"] >= 0 and result.macd_dif < 0

            if is_golden_cross and result.macd_dif > 0:
                result.macd_status = type(result.macd_status).GOLDEN_CROSS_ZERO
                result.macd_signal = "⭐ 零轴上金叉，强烈买入信号！"
            elif is_crossing_up:
                result.macd_status = type(result.macd_status).CROSSING_UP
                result.macd_signal = "⚡ DIF上穿零轴，趋势转强"
            elif is_golden_cross:
                result.macd_status = type(result.macd_status).GOLDEN_CROSS
                result.macd_signal = "✅ 金叉，趋势向上"
            elif is_death_cross:
                result.macd_status = type(result.macd_status).DEATH_CROSS
                result.macd_signal = "❌ 死叉，趋势向下"
            elif is_crossing_down:
                result.macd_status = type(result.macd_status).CROSSING_DOWN
                result.macd_signal = "⚠️ DIF下穿零轴，趋势转弱"
            elif result.macd_dif > 0 and result.macd_dea > 0:
                result.macd_status = type(result.macd_status).BULLISH
                result.macd_signal = "✓ 多头排列，持续上涨"
            elif result.macd_dif < 0 and result.macd_dea < 0:
                result.macd_status = type(result.macd_status).BEARISH
                result.macd_signal = "⚠ 空头排列，持续下跌"
            else:
                result.macd_status = type(result.macd_status).BULLISH
                result.macd_signal = " MACD 中性区域"
        else:
            result.macd_signal = "数据不足"

        # RSI 分析
        if len(df) >= self.strategy.rsi_long:
            result.rsi_6 = float(latest[f"RSI_{self.strategy.rsi_short}"])
            result.rsi_12 = float(latest[f"RSI_{self.strategy.rsi_mid}"])
            result.rsi_24 = float(latest[f"RSI_{self.strategy.rsi_long}"])
            rsi_mid = result.rsi_12

            if rsi_mid > self.strategy.rsi_overbought:
                result.rsi_status = type(result.rsi_status).OVERBOUGHT
                result.rsi_signal = f"⚠️ RSI超买({rsi_mid:.1f}>70)，短期回调风险高"
            elif rsi_mid > 60:
                result.rsi_status = type(result.rsi_status).STRONG_BUY
                result.rsi_signal = f"✅ RSI强势({rsi_mid:.1f})，多头力量充足"
            elif rsi_mid >= 40:
                result.rsi_status = type(result.rsi_status).NEUTRAL
                result.rsi_signal = f" RSI中性({rsi_mid:.1f})，震荡整理中"
            elif rsi_mid >= self.strategy.rsi_oversold:
                result.rsi_status = type(result.rsi_status).WEAK
                result.rsi_signal = f"⚡ RSI弱势({rsi_mid:.1f})，关注反弹"
            else:
                result.rsi_status = type(result.rsi_status).OVERSOLD
                result.rsi_signal = f"⭐ RSI超卖({rsi_mid:.1f}<30)，反弹机会大"
        else:
            result.rsi_signal = "数据不足"

        return result

    def _analyze_llm(
        self,
        code: str,
        name: str,
        technical_context: Dict[str, Any],
        news_context: Optional[str] = None,
        stock_name: Optional[str] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> Optional[LLMAnalysisResult]:
        """LLM 分析。

        Args:
            code: 股票代码
            name: 股票名称
            technical_context: 技术面上下文
            news_context: 新闻内容
            stock_name: 股票展示名
            progress_callback: 进度回调

        Returns:
            LLM 分析结果，失败返回 None
        """
        if not self._engine:
            logger.debug("[DecisionManager] LLM engine not configured, skipping LLM analysis")
            return None

        try:
            return self._engine.analyze(
                code=code,
                name=name,
                technical_context=technical_context,
                news_context=news_context,
                stock_name=stock_name,
                progress_callback=progress_callback,
            )
        except Exception as e:
            logger.error("[DecisionManager] LLM analysis %s(%s) failed: %s", name, code, e)
            return None

    # ═══════════════════════════════════════
    # 工厂方法
    # ═══════════════════════════════════════

    @classmethod
    def create(
        cls,
        strategy_name: str = "main",
        llm_model: Optional[str] = None,
        llm_config: Optional[DecisionEngineConfig] = None,
        llm_enabled: bool = True,
    ) -> "DecisionManager":
        """工厂方法 — 快速创建 DecisionManager。

        Args:
            strategy_name: 策略名称
            llm_model: LLM 模型名（如 deepseek/deepseek-chat）
            llm_config: LLM 引擎配置（可选，优先级高于 llm_model）
            llm_enabled: 是否启用 LLM

        Returns:
            DecisionManager 实例
        """
        strategy = get_strategy(strategy_name)

        engine: Optional[DecisionEngine] = None
        if llm_enabled and (llm_config or llm_model):
            cfg = llm_config or DecisionEngineConfig(model=llm_model)
            engine = DecisionEngine(config=cfg)

        return cls(
            strategy=strategy,
            llm_engine=engine,
            llm_enabled=llm_enabled,
        )


__all__ = [
    "DecisionManager",
    "AnalysisInput",
]
