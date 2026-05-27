"""
决策引擎 — 封装 LLM 调用（Litellm / DeepSeek）。

职责：
- 封装 LLM 调用链路
- 格式化分析 Prompt
- 解析 LLM 响应为结构化 LLMAnalysisResult
- 模型切换 / 重试 / 响应校验

与旧 src/analyzer.py 的 GeminiAnalyzer 接口兼容。
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import litellm
    from litellm import Router as LiteLLMRouter
    from json_repair import repair_json
except ImportError:
    litellm = None

from .models import LLMAnalysisResult

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════
# 引擎配置
# ═══════════════════════════════════════════

@dataclass
class DecisionEngineConfig:
    """LLM 决策引擎配置。

    Args:
        model: 主要模型名（如 deepseek/deepseek-chat）
        fallback_models: 备用模型列表
        temperature: 采样温度
        max_output_tokens: 最大输出 token 数
        request_delay: 请求前延时（秒）
        system_prompt: 自定义系统提示词（可选）
        report_language: 报告语言 zh/en
    """
    model: str = "deepseek/deepseek-chat"
    fallback_models: List[str] = field(default_factory=list)
    temperature: float = 0.7
    max_output_tokens: int = 8192
    request_delay: float = 0.0
    system_prompt: str = ""
    report_language: str = "zh"

    @classmethod
    def from_config(cls, cfg: Any) -> "DecisionEngineConfig":
        """从项目 Config 对象创建配置。"""
        return cls(
            model=cfg.litellm_model,
            fallback_models=cfg.litellm_fallback_models or [],
            temperature=cfg.llm_temperature,
            request_delay=cfg.gemini_request_delay,
            report_language=cfg.report_language or "zh",
        )


# ═══════════════════════════════════════════
# 决策引擎
# ═══════════════════════════════════════════

class DecisionEngine:
    """LLM 决策引擎 — 封装 DeepSeek 分析调用。

    职责：
    1. 调用 LLM API 进行股票分析
    2. 结合技术面 + 新闻数据生成分析报告
    3. 解析 AI 返回的 JSON 结果
    4. 返回结构化 LLMAnalysisResult

    线程安全：每次调用 create 新实例或使用 analyze() 方法。
    """

    # 默认系统提示词（简版，完整版可外部注入）
    DEFAULT_SYSTEM_PROMPT = """你是一位专业的股票投资分析师，负责生成【决策仪表盘】分析报告。

## 输出格式：决策仪表盘 JSON

请严格按照以下 JSON 格式输出：

{{
    "stock_name": "股票中文名称",
    "sentiment_score": 0-100整数,
    "trend_prediction": "强烈看多/看多/震荡/看空/强烈看空",
    "operation_advice": "买入/加仓/持有/减仓/卖出/观望",
    "decision_type": "buy/hold/sell",
    "confidence_level": "高/中/低",
    "dashboard": {{
        "core_conclusion": {{
            "one_sentence": "一句话核心结论",
            "signal_type": "🟢买入信号/🟡持有观望/🔴卖出信号/⚠️风险警告",
            "time_sensitivity": "立即行动/今日内/本周内/不急",
            "position_advice": {{
                "no_position": "空仓者建议",
                "has_position": "持仓者建议"
            }}
        }},
        "intelligence": {{
            "risk_alerts": ["风险点1", "风险点2"],
            "positive_catalysts": ["利好1", "利好2"]
        }},
        "battle_plan": {{
            "sniper_points": {{
                "ideal_buy": "理想买入点",
                "secondary_buy": "次优买入点",
                "stop_loss": "止损位",
                "take_profit": "目标位"
            }},
            "action_checklist": ["检查项1", "检查项2"]
        }}
    }},
    "analysis_summary": "综合分析摘要",
    "key_points": "核心看点",
    "risk_warning": "风险提示",
    "buy_reason": "操作理由"
}}

## 评分标准
- 强烈买入（80-100分）：多头排列 + 低乖离率 + 缩量回调/放量突破 + 筹码健康 + 消息面利好
- 买入（60-79分）：多头排列 + 乖离率<5% + 量能正常
- 观望（40-59分）：乖离率>5% / 均线缠绕 / 有风险事件
- 卖出/减仓（0-39分）：空头排列 / 跌破MA20 / 放量下跌 / 重大利空
"""

    def __init__(
        self,
        config: Optional[DecisionEngineConfig] = None,
        system_prompt: Optional[str] = None,
    ):
        self.config = config or DecisionEngineConfig()
        self._system_prompt = system_prompt or self.DEFAULT_SYSTEM_PROMPT
        self._router: Optional[LiteLLMRouter] = None

    # ═══════════════════════════════════════
    # 公共方法
    # ═══════════════════════════════════════

    def analyze(
        self,
        code: str,
        name: str,
        technical_context: Dict[str, Any],
        news_context: Optional[str] = None,
        stock_name: Optional[str] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> LLMAnalysisResult:
        """分析单只股票。

        流程：
        1. 格式化输入数据（技术面 + 新闻）
        2. 调用 LLM API（带重试和模型切换）
        3. 解析 JSON 响应
        4. 返回结构化结果

        Args:
            code: 股票代码
            name: 股票名称
            technical_context: 技术面上下文数据
            news_context: 新闻内容（可选）
            stock_name: 股票展示名（可选，优先级高于 name）
            progress_callback: 进度回调 (progress, message)

        Returns:
            LLMAnalysisResult 对象
        """
        display_name = stock_name or name
        effective_language = self.config.report_language

        if not self.config.model:
            return self._make_unavailable_result(code, name, effective_language)

        try:
            prompt = self._format_prompt(
                code, display_name, technical_context, news_context,
                report_language=effective_language,
            )
            logger.info(
                "[DecisionEngine] Analyzing %s(%s), prompt len=%d",
                display_name, code, len(prompt),
            )

            if self.config.request_delay > 0:
                time.sleep(self.config.request_delay)

            response_text, model_used, usage = self._call_llm(
                prompt, system_prompt=self._build_system_prompt(effective_language, stock_code=code),
            )

            result = self._parse_response(response_text, code, name, effective_language)
            result.model_used = model_used
            result.raw_response = response_text
            return result

        except Exception as e:
            logger.error("[DecisionEngine] %s(%s) failed: %s", name, code, e)
            return LLMAnalysisResult(
                code=code,
                name=name,
                sentiment_score=50,
                trend_prediction="震荡",
                operation_advice="持有",
                confidence_level="低",
                analysis_summary=f"分析失败: {str(e)[:100]}",
                success=False,
                error_message=str(e),
            )

    def generate_text(
        self,
        prompt: str,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> Optional[str]:
        """自由文本生成入口。

        Args:
            prompt: 文本提示
            max_tokens: 最大 token 数
            temperature: 采样温度

        Returns:
            响应文本，失败返回 None
        """
        try:
            result = self._call_llm(
                prompt,
                system_prompt="You are a helpful assistant.",
                max_tokens=max_tokens,
                temperature=temperature,
            )
            if isinstance(result, tuple):
                text, _, _ = result
                return text
            return result
        except Exception as exc:
            logger.error("[DecisionEngine.generate_text] LLM call failed: %s", exc)
            return None

    # ═══════════════════════════════════════
    # LLM 调用
    # ═══════════════════════════════════════

    def _call_llm(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> Tuple[str, str, Dict[str, Any]]:
        """调用 LLM（支持模型切换）。

        Returns:
            (response_text, model_used, usage_dict)

        Raises:
            RuntimeError: 所有模型都失败时
        """
        if litellm is None:
            raise RuntimeError("litellm is not installed")

        models_to_try = [self.config.model] + self.config.fallback_models
        models_to_try = [m for m in models_to_try if m]

        last_error: Optional[Exception] = None

        for model in models_to_try:
            try:
                messages = [{"role": "user", "content": prompt}]
                if system_prompt:
                    messages.insert(0, {"role": "system", "content": system_prompt})

                kwargs = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature or self.config.temperature,
                    "max_tokens": max_tokens or self.config.max_output_tokens,
                }

                logger.debug("[DecisionEngine] Calling %s ...", model)
                response = litellm.completion(**kwargs)

                if response and response.choices and response.choices[0].message.content:
                    content = response.choices[0].message.content
                    usage = getattr(response, "usage", {})
                    usage_dict = {
                        "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                        "completion_tokens": getattr(usage, "completion_tokens", 0),
                        "total_tokens": getattr(usage, "total_tokens", 0),
                    }
                    logger.info("[DecisionEngine] %s responded, %d chars", model, len(content))
                    return content, model, usage_dict

                raise ValueError("LLM returned empty response")

            except Exception as e:
                logger.warning("[DecisionEngine] %s failed: %s", model, e)
                last_error = e
                continue

        raise RuntimeError(
            f"All LLM models failed (tried {len(models_to_try)}). Last error: {last_error}"
        )

    # ═══════════════════════════════════════
    # Prompt 构建
    # ═══════════════════════════════════════

    def _build_system_prompt(
        self,
        report_language: str = "zh",
        stock_code: Optional[str] = None,
    ) -> str:
        """构建系统提示词。

        如果提供了自定义 system_prompt，优先使用。
        """
        if self.config.system_prompt:
            return self.config.system_prompt
        return self.DEFAULT_SYSTEM_PROMPT

    def _format_prompt(
        self,
        code: str,
        name: str,
        context: Dict[str, Any],
        news_context: Optional[str] = None,
        report_language: str = "zh",
    ) -> str:
        """格式化分析提示词。

        Args:
            code: 股票代码
            name: 股票名称
            context: 技术面数据
            news_context: 新闻内容
            report_language: 报告语言

        Returns:
            格式化后的 prompt 字符串
        """
        today = context.get("today", {})
        lines = [
            f"# 决策仪表盘分析请求\n",
            f"## 股票基础信息",
            f"- 股票代码：{code}",
            f"- 股票名称：{name}",
            f"- 分析日期：{context.get('date', 'N/A')}\n",
            f"## 技术面数据\n",
            f"### 今日行情",
            f"- 收盘价：{today.get('close', 'N/A')}",
            f"- 涨跌幅：{today.get('pct_chg', 'N/A')}%",
            f"- 成交量：{today.get('volume', 'N/A')}\n",
            f"### 均线系统",
            f"- MA5：{today.get('ma5', 'N/A')}",
            f"- MA10：{today.get('ma10', 'N/A')}",
            f"- MA20：{today.get('ma20', 'N/A')}",
            f"- 均线形态：{context.get('ma_status', 'N/A')}\n",
        ]

        if "realtime" in context:
            rt = context["realtime"]
            lines.extend([
                f"### 实时行情",
                f"- 当前价格：{rt.get('price', 'N/A')}",
                f"- 量比：{rt.get('volume_ratio', 'N/A')}",
                f"- 换手率：{rt.get('turnover_rate', 'N/A')}%\n",
            ])

        if "chip" in context:
            chip = context["chip"]
            lines.extend([
                f"### 筹码结构",
                f"- 获利比例：{chip.get('profit_ratio', 'N/A')}",
                f"- 平均成本：{chip.get('avg_cost', 'N/A')}",
                f"- 集中度：{chip.get('concentration', 'N/A')}",
                f"- 筹码健康：{chip.get('chip_health', 'N/A')}\n",
            ])

        if news_context:
            lines.extend([
                f"## 新闻/消息面",
                news_context,
                "",
            ])

        return "\n".join(lines)

    # ═══════════════════════════════════════
    # 响应解析
    # ═══════════════════════════════════════

    def _parse_response(
        self,
        response_text: str,
        code: str,
        name: str,
        report_language: str = "zh",
    ) -> LLMAnalysisResult:
        """解析 LLM 响应为 LLMAnalysisResult。

        尝试从响应中提取 JSON，解析失败时返回默认值。
        """
        # 尝试提取 JSON 块
        json_text = self._extract_json(response_text)

        if json_text:
            try:
                data = json.loads(json_text) if isinstance(json_text, str) else json_text
            except json.JSONDecodeError:
                try:
                    data = json.loads(repair_json(json_text))
                except Exception:
                    data = {}
        else:
            data = {}

        if not data or not isinstance(data, dict):
            return self._make_default_result(code, name, report_language)

        # 提取核心字段
        sentiment_score = data.get("sentiment_score", 50)
        if not isinstance(sentiment_score, (int, float)):
            sentiment_score = 50

        return LLMAnalysisResult(
            code=code,
            name=data.get("stock_name", name),
            sentiment_score=int(sentiment_score),
            trend_prediction=data.get("trend_prediction", "震荡"),
            operation_advice=data.get("operation_advice", "持有"),
            decision_type=data.get("decision_type", "hold"),
            confidence_level=data.get("confidence_level", "中"),
            analysis_summary=data.get("analysis_summary", ""),
            risk_warning=data.get("risk_warning", ""),
            buy_reason=data.get("buy_reason", ""),
            dashboard=data.get("dashboard"),
            success=True,
        )

    @staticmethod
    def _extract_json(text: str) -> Optional[str]:
        """从文本中提取 JSON 块。

        优先查找 ```json ... ``` 代码块，然后尝试直接解析。
        """
        # Try markdown code block first
        json_start_markers = [
            "```json",
            "```json\n",
            "```",
        ]
        for marker in json_start_markers:
            start = text.find(marker)
            if start >= 0:
                # Find the end of the block
                content_start = start + len(marker)
                end = text.find("```", content_start)
                if end >= 0:
                    candidate = text[content_start:end].strip()
                    # Quick validation: try to parse
                    try:
                        json.loads(candidate)
                        return candidate
                    except json.JSONDecodeError:
                        pass

        # Try to find JSON object directly
        brace_start = text.find("{")
        if brace_start >= 0:
            brace_count = 0
            for i in range(brace_start, len(text)):
                if text[i] == "{":
                    brace_count += 1
                elif text[i] == "}":
                    brace_count -= 1
                    if brace_count == 0:
                        candidate = text[brace_start : i + 1]
                        try:
                            json.loads(candidate)
                            return candidate
                        except json.JSONDecodeError:
                            pass

        # Last resort: return the whole text
        return text.strip()

    # ═══════════════════════════════════════
    # 错误结果
    # ═══════════════════════════════════════

    def _make_unavailable_result(
        self, code: str, name: str, report_language: str = "zh"
    ) -> LLMAnalysisResult:
        """模型不可用时返回的默认结果。"""
        return LLMAnalysisResult(
            code=code,
            name=name,
            sentiment_score=50,
            trend_prediction="震荡",
            operation_advice="持有",
            confidence_level="低",
            analysis_summary="AI 分析功能未启用（未配置模型）",
            risk_warning="请配置 LLM 模型后重试",
            success=False,
            error_message="LLM model is not configured",
        )

    @staticmethod
    def _make_default_result(
        code: str, name: str, report_language: str = "zh"
    ) -> LLMAnalysisResult:
        """解析失败时返回的默认结果。"""
        return LLMAnalysisResult(
            code=code,
            name=name,
            sentiment_score=50,
            trend_prediction="震荡",
            operation_advice="持有",
            confidence_level="低",
            analysis_summary="LLM 响应解析失败",
            success=False,
            error_message="Failed to parse LLM response as JSON",
        )


__all__ = [
    "DecisionEngine",
    "DecisionEngineConfig",
]
