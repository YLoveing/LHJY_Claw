# -*- coding: utf-8 -*-
"""
IntelAgent — news & intelligence gathering specialist.

Responsible for:
- Searching latest stock news and announcements
- Running comprehensive intelligence search
- Detecting risk events (reduce holdings, earnings warnings, regulatory)
- Summarising sentiment and catalysts
"""

from __future__ import annotations

import logging
from typing import Optional

from src.agent.agents.base_agent import BaseAgent
from src.agent.protocols import AgentContext, AgentOpinion
from src.agent.runner import try_parse_json

logger = logging.getLogger(__name__)


class IntelAgent(BaseAgent):
    agent_name = "intel"
    max_steps = 4
    tool_names = [
        "search_stock_news",
        "search_comprehensive_intel",
        "get_stock_info",
        "get_capital_flow",
    ]

    def system_prompt(self, ctx: AgentContext) -> str:
        return """\
You are an **Intelligence & Sentiment Agent** specialising in A-shares, \
HK, and US equities.

Your task: gather the latest news, announcements, and risk signals for \
the given stock, then produce a structured JSON opinion.

## Workflow
1. Search latest stock news (earnings, announcements, insider activity)
2. Run comprehensive intel search — this covers latest news, company \
announcements (公司公告), market analysis, risk checks, and earnings outlook
3. For A-share stocks, call get_capital_flow to obtain main-force (主力) \
capital inflow/outflow data and include it in your analysis
4. Classify positive catalysts and risk alerts
5. Assess overall sentiment

## Risk Detection Priorities
- Insider / major shareholder sell-downs (减持)
- Earnings warnings or pre-loss announcements (业绩预亏)
- Regulatory penalties or investigations
- Industry-wide policy headwinds
- Large lock-up expirations (解禁)
- PE valuation anomalies
- Sustained main-force capital outflow (主力持续净流出)

## Capital Flow Interpretation (A-shares only)
- main_net_inflow > 0: bullish signal (主力净流入)
- main_net_inflow < 0: bearish signal (主力净流出)
- inflow_5d / inflow_10d: medium-term accumulation or distribution trend

## Quantitative Sentiment Scoring
Assign a **sentiment_score** (0-100) based on the weighted combination of:
- **news_sentiment** (0-100, 40% weight): Score news headlines and announcements
  - Strong positive catalysts → 70-100, minor positives → 55-69
  - Neutral / mixed → 40-54, minor negatives → 20-39
  - Major risks / negative catalysts → 0-19
- **capital_flow_sentiment** (0-100, 35% weight): Score fund-flow data
  - Strong net inflow → 70-100, slight inflow → 55-69
  - Neutral / low activity → 40-54, slight outflow → 20-39
  - Sustained heavy outflow → 0-19
- **technical_sentiment** (0-100, 25% weight): Score price action context
  - Use current price vs MA5/MA10/MA20 bias, volume trend, and RSI zone
  - Pullback to support + shrinking volume → elevated (55-75)
  - Overextended + heavy volume → reduced (25-40)

Also classify the **sentiment_trend** as "warming" (情绪升温), "cooling" (情绪降温), or "stable" (平稳).

## Output Format
Return **only** a JSON object:
{
  "signal": "strong_buy|buy|hold|sell|strong_sell",
  "confidence": 0.0-1.0,
  "reasoning": "2-3 sentence summary of news/sentiment/capital-flow findings",
  "risk_alerts": ["list", "of", "detected", "risks"],
  "positive_catalysts": ["list", "of", "catalysts"],
  "sentiment_label": "very_positive|positive|neutral|negative|very_negative",
  "sentiment_score": 0-100,
  "sentiment_breakdown": {
    "news_sentiment": 0-100,
    "capital_flow_sentiment": 0-100,
    "technical_sentiment": 0-100
  },
  "sentiment_trend": "warming|cooling|stable",
  "capital_flow_signal": "inflow|outflow|neutral|not_available",
  "key_news": [
    {"title": "...", "impact": "positive|negative|neutral"}
  ]
}
"""

    def build_user_message(self, ctx: AgentContext) -> str:
        parts = [f"Gather intelligence and assess sentiment for stock **{ctx.stock_code}**"]
        if ctx.stock_name:
            parts[0] += f" ({ctx.stock_name})"
        parts.append(
            "Steps:\n"
            "1. Call search_comprehensive_intel to get latest news, company announcements "
            "(公司公告), risk events, and earnings outlook.\n"
            "2. Call get_capital_flow to obtain main-force (主力) capital flow data "
            "(A-share only; skip for HK/US).\n"
            "3. Call get_stock_info for PE / valuation context.\n"
            "4. Output the JSON opinion including:\n"
            "   - sentiment_score (0-100, required)\n"
            "   - sentiment_breakdown (news_sentiment, capital_flow_sentiment, technical_sentiment)\n"
            "   - sentiment_trend (warming|cooling|stable)\n"
            "   - capital_flow_signal"
        )
        return "\n".join(parts)

    def post_process(self, ctx: AgentContext, raw_text: str) -> Optional[AgentOpinion]:
        parsed = try_parse_json(raw_text)
        if parsed is None:
            logger.warning("[IntelAgent] failed to parse opinion JSON")
            return None

        # Cache parsed intel so downstream agents (especially RiskAgent) can
        # reuse it instead of re-searching the same evidence.
        ctx.set_data("intel_opinion", parsed)

        # Propagate risk alerts to context
        for alert in parsed.get("risk_alerts", []):
            if isinstance(alert, str) and alert:
                ctx.add_risk_flag(category="intel", description=alert)

        return AgentOpinion(
            agent_name=self.agent_name,
            signal=parsed.get("signal", "hold"),
            confidence=float(parsed.get("confidence", 0.5)),
            reasoning=parsed.get("reasoning", ""),
            raw_data=parsed,
        )


