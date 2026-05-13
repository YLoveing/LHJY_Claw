# -*- coding: utf-8 -*-
"""
策略引擎运行时 — 加载 strategies/*.yaml 并对候选股匹配策略条件。

功能：
1. 扫描并解析 YAML 策略文件 → Strategy 对象
2. 根据 indicators 计算每只候选股匹配的策略
3. 输出 matched_strategies 注入选股结果
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

logger = logging.getLogger("strategies.engine")

_STRATEGIES_DIR = Path(__file__).resolve().parent


@dataclass
class Strategy:
    """解析后的策略对象"""
    name: str
    display_name: str = ""
    description: str = ""
    category: str = "framework"
    core_rules: List[int] = field(default_factory=list)
    required_tools: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)
    default_active: bool = False
    default_router: bool = False
    default_priority: int = 100
    market_regimes: List[str] = field(default_factory=list)
    instructions: str = ""


# ── 检查规则函数 ──

def _check_trend_bullish(ind: Dict) -> bool:
    """多头排列：MA5 > MA10 > MA20"""
    ma5 = ind.get("MA5", 0)
    ma10 = ind.get("MA10", 0)
    ma20 = ind.get("MA20", 0)
    if ma5 and ma10 and ma20:
        return ma5 > ma10 > ma20
    return False


def _check_trend_bearish(ind: Dict) -> bool:
    """空头排列：MA5 < MA10 < MA20"""
    ma5 = ind.get("MA5", 0)
    ma10 = ind.get("MA10", 0)
    ma20 = ind.get("MA20", 0)
    if ma5 and ma10 and ma20:
        return ma5 < ma10 < ma20
    return False


def _check_golden_cross(ind: Dict) -> bool:
    """金叉：MA5 上穿 MA10（今日MA5>MA10且昨日<MA10）"""
    ma5 = ind.get("MA5", 0)
    ma10 = ind.get("MA10", 0)
    pre_ma5 = ind.get("pre_MA5", 0)
    pre_ma10 = ind.get("pre_MA10", 0)
    if ma5 and ma10 and pre_ma5 and pre_ma10:
        return ma5 > ma10 and pre_ma5 <= pre_ma10
    return False


def _check_volume_breakout(ind: Dict) -> bool:
    """放量突破：量比 > 1.5 且 价格站上MA20"""
    vol_ratio = ind.get("vol_ratio", 1)
    price = ind.get("price", 0)
    ma20 = ind.get("MA20", 0)
    if price and ma20:
        return vol_ratio > 1.5 and price >= ma20
    return False


def _check_oversold_rebound(ind: Dict) -> bool:
    """超卖反弹：RSI < 35 且 不在下跌趋势"""
    rsi = ind.get("RSI", 50)
    ma_trend = ind.get("ma_alignment", "")
    if rsi < 35:
        return "bearish" not in ma_trend.lower()
    return False


def _check_near_ma20(ind: Dict) -> bool:
    """回踩MA20：价格在MA20附近 ±3%"""
    price = ind.get("price", 0)
    ma20 = ind.get("MA20", 0)
    if price and ma20:
        pct = (price / ma20 - 1) * 100
        return -3 <= pct <= 3
    return False


# 内置检查器映射 — 从 instructions 关键词匹配到检查函数
_BUILTIN_CHECKS: Dict[str, Callable[[Dict], bool]] = {
    "多头排列": _check_trend_bullish,
    "空头排列": _check_trend_bearish,
    "金叉": _check_golden_cross,
    "放量突破": _check_volume_breakout,
    "超卖": _check_oversold_rebound,
    "回踩": _check_near_ma20,
}


# ── 主引擎 ──

class StrategyEngine:
    """策略引擎 — 加载 YAML 策略并对候选股进行匹配检查。"""

    def __init__(self, strategies_dir: Optional[Path] = None):
        self._strategies_dir = strategies_dir or _STRATEGIES_DIR
        self._strategies: List[Strategy] = []
        self._loaded = False

    def load_strategies(self) -> List[Strategy]:
        """扫描并加载所有 YAML 策略文件。"""
        if self._loaded:
            return self._strategies

        self._strategies = []
        if not self._strategies_dir.exists():
            logger.warning(f"策略目录不存在: {self._strategies_dir}")
            return self._strategies

        for fpath in sorted(self._strategies_dir.glob("*.yaml")):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if not data or "name" not in data:
                    continue
                strategy = Strategy(
                    name=data["name"],
                    display_name=data.get("display_name", data["name"]),
                    description=data.get("description", ""),
                    category=data.get("category", "framework"),
                    core_rules=data.get("core_rules", []),
                    required_tools=data.get("required_tools", []),
                    aliases=data.get("aliases", []),
                    default_active=data.get("default_active", False),
                    default_router=data.get("default_router", False),
                    default_priority=data.get("default_priority", 100),
                    market_regimes=data.get("market_regimes", []),
                    instructions=data.get("instructions", ""),
                )
                self._strategies.append(strategy)
                logger.debug(f"  加载策略: {strategy.name}")
            except Exception as e:
                logger.warning(f"策略文件解析失败 {fpath.name}: {e}")

        self._loaded = True
        logger.info(f"策略引擎: 加载 {len(self._strategies)} 个策略")
        return self._strategies

    def match_strategies(
        self,
        stock: Dict,
        *,
        top_n: int = 3,
        min_score: float = 0.0,
    ) -> List[Dict]:
        """对单只候选股匹配符合条件的策略。

        Args:
            stock: 候选股数据（含 indicators）
            top_n: 最多返回的策略数
            min_score: 最低匹配分数（0-100）

        Returns:
            [{name, display_name, category, match_score, matched_keywords, regime_match}]
        """
        if not self._loaded:
            self.load_strategies()

        ind = stock.get("indicators", stock)
        results = []

        for s in self._strategies:
            # 关键词匹配评分
            instructions_lower = s.instructions.lower()
            matched_keywords = []

            for keyword, check_fn in _BUILTIN_CHECKS.items():
                if keyword in instructions_lower:
                    if check_fn(ind):
                        matched_keywords.append(keyword)

            # 市场体制匹配（简化判断）
            regime_score = 50  # 默认中性
            regimes = s.market_regimes
            if regimes:
                # 根据大盘状态判断
                trend_status = ind.get("trend_status", "").lower()
                if "uptrend" in trend_status or "bull" in trend_status:
                    if "trending_up" in regimes or "bullish_trend" in regimes:
                        regime_score = 80
                elif "downtrend" in trend_status or "bear" in trend_status:
                    if "bearish_trend" in regimes:
                        regime_score = 60
                    else:
                        regime_score = 30

                # 板块热度
                sector_hot = ind.get("sector_hot", False)
                if sector_hot and "sector_hot" in regimes:
                    regime_score = max(regime_score, 75)

            # 综合匹配分数：关键词匹配占60%，体制匹配占40%
            keyword_score = min(100, len(matched_keywords) * 25)  # 每个关键词25分
            match_score = keyword_score * 0.6 + regime_score * 0.4

            if match_score >= min_score:
                results.append({
                    "name": s.name,
                    "display_name": s.display_name,
                    "category": s.category,
                    "match_score": round(match_score, 1),
                    "matched_keywords": matched_keywords,
                    "regime_match": regime_score >= 50,
                })

        # 按匹配度排序
        results.sort(key=lambda x: x["match_score"], reverse=True)
        return results[:top_n]

    def batch_match(
        self,
        stocks: List[Dict],
        *,
        top_n: int = 3,
        min_score: float = 15.0,
    ) -> List[Dict]:
        """批量对候选股列表匹配策略，结果注入到每只股票数据中。"""
        for stock in stocks:
            try:
                matched = self.match_strategies(
                    stock, top_n=top_n, min_score=min_score
                )
                stock["matched_strategies"] = matched
                # 汇总策略名
                if matched:
                    stock["strategy_names"] = ", ".join(
                        m["display_name"] for m in matched[:3]
                    )
                else:
                    stock["strategy_names"] = ""
            except Exception as e:
                logger.warning(f"策略匹配失败 {stock.get('code', '?')}: {e}")
                stock["matched_strategies"] = []
                stock["strategy_names"] = ""
        return stocks


# ── 便捷入口 ──

_engine_instance: Optional[StrategyEngine] = None


def get_engine() -> StrategyEngine:
    """获取（或创建）单例策略引擎。"""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = StrategyEngine()
        _engine_instance.load_strategies()
    return _engine_instance


def match_for_stock(stock: Dict, **kwargs) -> List[Dict]:
    """便捷函数 — 单只候选股匹配策略。"""
    return get_engine().match_strategies(stock, **kwargs)


def batch_match(stocks: List[Dict], **kwargs) -> List[Dict]:
    """便捷函数 — 批量匹配策略。"""
    return get_engine().batch_match(stocks, **kwargs)
