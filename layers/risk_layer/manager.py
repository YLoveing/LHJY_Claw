"""
RiskManager — 整合所有风控逻辑的统一入口。

将 kelly / stop_loss / drawdown / rebalance 组合为 RiskManager 类。
支持 Protocol 接口 (IRiskManager) 供上层依赖注入。
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Protocol

from .models import (
    AccountState,
    DrawdownState,
    PositionInfo,
    RiskConfig,
    RiskDecision,
    RiskEvent,
)
from .kelly import kelly_fraction, compute_kelly_amount
from .stop_loss import check_stop_loss, check_stop_loss_position, to_risk_event
from .drawdown import check_account_drawdown, check_account_drawdown_state
from .rebalance import try_rebalance

logger = logging.getLogger("risk_layer")


class IRiskManager(Protocol):
    """风控管理器接口 — 供上层（execution_layer / orch）依赖注入。"""

    def check_stop_loss(self, position: PositionInfo) -> Optional[RiskEvent]:
        ...

    def check_account_drawdown(self, state: AccountState) -> RiskDecision:
        ...

    def compute_position_size(
        self,
        score: int,
        cash: float,
        state: AccountState,
        sentiment_factor: float = 1.0,
    ) -> float:
        ...

    def check_buy_eligibility(
        self, code: str, score: int, state: AccountState
    ) -> RiskDecision:
        ...

    def get_thresholds(self) -> dict:
        ...


class RiskManager:
    """风控管理器 — 整合止损/止盈/回撤/仓位/再平衡。

    Args:
        config: 风控配置，None 则使用默认值
        sentiment_adjustment_path: 情绪仓位调节文件路径
    """

    def __init__(
        self,
        config: Optional[RiskConfig] = None,
        sentiment_adjustment_path: str = "",
    ):
        self.config = config or RiskConfig()
        self._sentiment_path = sentiment_adjustment_path

    # ── 情绪调节 ──

    def load_sentiment_adjustment(self) -> float:
        """从情绪引擎加载仓位调节系数。

        从 simulated_trading.py 的 _load_sentiment_adjustment 迁移。
        """
        if self._sentiment_path:
            si_file = Path(self._sentiment_path)
        else:
            si_file = Path(
                "/opt/daily_stock_analysis/sentiment_engine/adjustment.json"
            )
        if si_file.exists():
            try:
                with open(si_file) as f:
                    data = json.load(f)
                factor = float(data.get("factor", 1.0))
                logger.info(
                    f"[风控] 应用情绪仓位系数: {factor} "
                    f"({data.get('label','?')} {data.get('score','?')}/100)"
                )
                return factor
            except (json.JSONDecodeError, ValueError, TypeError) as e:
                logger.warning(f"[风控] 情绪系数读取失败: {e}")
        return 1.0

    def load_sentiment_index_score(self) -> int:
        """从情绪引擎读取最新的情绪指数。

        从 simulated_trading.py 的 _load_sentiment_index_score 迁移。
        """
        if self._sentiment_path:
            si_file = Path(self._sentiment_path)
        else:
            si_file = Path(
                "/opt/daily_stock_analysis/sentiment_engine/adjustment.json"
            )
        if si_file.exists():
            try:
                with open(si_file) as f:
                    data = json.load(f)
                return int(data.get("score", 50))
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
        return 50

    # ── 止损/止盈 ──

    def check_stop_loss(self, position: PositionInfo) -> Optional[RiskEvent]:
        """检查单只持仓是否需要止损止盈。"""
        action, reason = check_stop_loss_position(position, self.config)
        return to_risk_event(action, position.code, reason)
    
    def check_stop_loss_dict(self, pos: dict, current_price: float, code: str) -> tuple:
        """dict 版本的止损止盈检查 — 兼容旧调用方。"""
        return check_stop_loss(pos, current_price, code, config=self.config)

    # ── 账户回撤 ──

    def check_account_drawdown(self, state: AccountState) -> RiskDecision:
        """检查账户总回撤。

        Returns:
            RiskDecision: 是否允许交易及最大买入金额
        """
        ds = check_account_drawdown_state(state, self.config)
        events = []
        if ds.is_hibernating:
            events.append(
                RiskEvent(
                    event_type="drawdown_alert",
                    code="ACCOUNT",
                    reason=f"账户总回撤 {ds.current_drawdown_pct:.1f}%，"
                           f"低于阈值 {self.config.max_drawdown_pct}%，暂停买入",
                    severity="critical",
                )
            )
        max_buy = 0.0
        if not ds.is_hibernating:
            max_buy = state.cash * 0.5  # 最多用一半现金买入

        return RiskDecision(
            allowed=not ds.is_hibernating,
            max_buy_amount=max_buy,
            events=events,
            adjusted_max_positions=self.config.max_positions,
        )

    def check_account_drawdown_dict(self, state: dict) -> tuple:
        """dict 版本的账户回撤检查 — 兼容旧调用方。"""
        return check_account_drawdown(state, self.config)

    # ── 仓位计算 ──

    def compute_position_size(
        self,
        score: int,
        cash: float,
        state: Optional[AccountState] = None,
        sentiment_factor: float = 1.0,
    ) -> float:
        """计算凯利建议买入金额。"""
        return compute_kelly_amount(
            score, cash, sentiment_factor=sentiment_factor, config=self.config
        )

    def compute_kelly_amount_dict(
        self, score: int, cash: float, sentiment_factor: float = 1.0
    ) -> float:
        """dict 兼容版本的凯利金额计算。"""
        return compute_kelly_amount(
            score, cash, sentiment_factor=sentiment_factor, config=self.config
        )

    def kelly_fraction(self, score: int) -> float:
        """计算凯利比例（半凯利）。"""
        return kelly_fraction(score, kelly_b=self.config.kelly_b())

    # ── 买入资格 ──

    def check_buy_eligibility(
        self, code: str, score: int, state: AccountState
    ) -> RiskDecision:
        """检查是否允许买入。

        条件：
        1. 账户回撤未超限
        2. 持仓数量未达上限
        3. 评分达标（由调用方判断买入阈值）
        """
        dd_decision = self.check_account_drawdown(state)
        if not dd_decision.allowed:
            return dd_decision

        events = list(dd_decision.events)
        if len(state.positions) >= dd_decision.adjusted_max_positions:
            events.append(
                RiskEvent(
                    event_type="buy_blocked",
                    code=code,
                    reason=f"已达持仓上限 {dd_decision.adjusted_max_positions}",
                    severity="warning",
                )
            )
            return RiskDecision(
                allowed=False,
                max_buy_amount=0.0,
                events=events,
                adjusted_max_positions=dd_decision.adjusted_max_positions,
            )

        return RiskDecision(
            allowed=True,
            max_buy_amount=dd_decision.max_buy_amount,
            events=events,
            adjusted_max_positions=dd_decision.adjusted_max_positions,
        )

    # ── 动态阈值 ──

    def get_thresholds(self) -> dict:
        """获取风控阈值信息（用于报告/调试）。"""
        return {
            "stop_loss_pct": self.config.stop_loss_pct,
            "take_profit_pct": self.config.take_profit_pct,
            "max_drawdown_pct": self.config.max_drawdown_pct,
            "max_positions": self.config.max_positions,
            "kelly_b": self.config.kelly_b(),
            "kelly_diversification": self.config.kelly_diversification,
        }

    # ── 再平衡 ──

    def try_rebalance(
        self,
        state: dict,
        trades: List[dict],
        new_trades: List[dict],
        report_date_str: str,
        markowitz_path: Optional[Path] = None,
    ) -> None:
        """尝试执行再平衡（仅周五）。"""
        try_rebalance(state, trades, new_trades, report_date_str, markowitz_path)
