"""
交易执行引擎 — 从 simulated_trading.py 的 execute_trades 提取核心逻辑。

SimulatedExecutionEngine 将执行逻辑封装为类，支持：
- 单笔买入/卖出操作
- 费用自动计算
- 状态和交易记录管理
- 协议接口 (IExecutionEngine) 供上层依赖注入
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Protocol

from ..risk_layer import (
    RiskManager,
    check_stop_loss,
    check_account_drawdown,
)
from .fees import calc_buy_fees, calc_sell_fees
from .persistence import (
    load_state,
    save_state,
    load_trades,
    save_trades,
    INITIAL_CAPITAL,
)

logger = logging.getLogger("execution_layer")


class IExecutionEngine(Protocol):
    """交易执行引擎接口 — 供编排层依赖注入。"""

    def place_order(self, order: dict) -> Optional[dict]:
        """下单"""
        ...

    def execute_buy(self, code: str, amount: float, reason: str) -> Optional[dict]:
        """执行买入"""
        ...

    def execute_sell(self, code: str, quantity: int, reason: str) -> Optional[dict]:
        """执行卖出"""
        ...

    def get_state(self) -> dict:
        """获取账户状态"""
        ...

    def get_trades(self) -> List[dict]:
        """获取交易记录"""
        ...


class SimulatedExecutionEngine:
    """模拟交易执行引擎。

    封装 simulated_trading.py 的核心执行逻辑，以类的形式提供。
    支持全局常量（INITIAL_CAPITAL, MAX_POSITIONS）的配置化。

    Args:
        risk_manager: 风控管理器（可选），不传则内部创建默认实例
        data_dir: 数据目录路径
        initial_capital: 初始资金
        max_positions: 最大持仓数
    """

    def __init__(
        self,
        risk_manager: Optional[RiskManager] = None,
        data_dir: Optional[Path] = None,
        initial_capital: float = INITIAL_CAPITAL,
        max_positions: int = 8,
    ):
        self.risk = risk_manager or RiskManager()
        self._data_dir = data_dir or Path(
            "/opt/daily_stock_analysis/simulated_trading"
        )
        self._initial_capital = initial_capital
        self._max_positions = max_positions

    # ── 状态管理 ──

    def load_state(self) -> dict:
        """加载账户状态。"""
        return load_state(self._data_dir)

    def save_state(self, state: dict) -> None:
        """保存账户状态。"""
        save_state(state, self._data_dir)

    def get_state(self) -> dict:
        """别名：兼容 IExecutionEngine 协议。"""
        return self.load_state()

    def load_trades(self) -> List[dict]:
        """加载交易记录。"""
        return load_trades(self._data_dir)

    def save_trades(self, trades: List[dict]) -> None:
        """保存交易记录。"""
        save_trades(trades, self._data_dir)

    def get_trades(self) -> List[dict]:
        """别名：兼容 IExecutionEngine 协议。"""
        return self.load_trades()

    # ── 单笔执行 ──

    def execute_buy(
        self,
        code: str,
        price: float,
        quantity: int,
        reason: str = "",
        state: Optional[dict] = None,
        trades: Optional[List[dict]] = None,
        report_date_str: str = "",
    ) -> Optional[dict]:
        """执行一笔买入。

        从 simulated_trading.py 的 execute_trades 买入逻辑提取。

        Args:
            code: 股票代码
            price: 买入价格
            quantity: 买入数量（自动取整到100的倍数）
            reason: 买入原因
            state: 账户状态（不传则从文件加载）
            trades: 交易记录列表（不传则从文件加载）
            report_date_str: 报告日期

        Returns:
            交易字典，或 None（资金不足时）
        """
        s = state if state is not None else self.load_state()
        t = trades if trades is not None else self.load_trades()

        # 数量取整
        qty = int(quantity / 100) * 100
        if qty < 100:
            return None

        cost = qty * price
        fee = calc_buy_fees(cost)
        total_cost = cost + fee
        if total_cost > s["cash"]:
            qty = int((s["cash"] - fee) / price / 100) * 100
            if qty < 100:
                return None
            cost = qty * price
            fee = calc_buy_fees(cost)
            total_cost = cost + fee

        s["cash"] -= total_cost
        s["total_fee"] += fee
        s["positions"][code] = {
            "quantity": qty,
            "avg_cost": price,
            "current_price": price,
            "invested": round(cost, 2),
            "entry_score": 0,
        }

        trade = {
            "date": report_date_str,
            "code": code,
            "side": "buy",
            "quantity": qty,
            "price": round(price, 3),
            "cost": round(total_cost, 2),
            "fee": round(fee, 2),
            "reason": reason,
        }
        t.append(trade)

        if state is None:
            self.save_state(s)
        if trades is None:
            self.save_trades(t)

        logger.info(
            f"[执行] 买入 {code} × {qty} @ {price:.3f} = {total_cost:.0f} - {reason}"
        )
        return trade

    def execute_sell(
        self,
        code: str,
        price: float,
        quantity: int,
        reason: str = "",
        state: Optional[dict] = None,
        trades: Optional[List[dict]] = None,
        report_date_str: str = "",
        side_label: str = "sell",
    ) -> Optional[dict]:
        """执行一笔卖出。

        从 simulated_trading.py 的 execute_trades 卖出逻辑提取。

        Args:
            code: 股票代码
            price: 卖出价格
            quantity: 卖出数量
            reason: 卖出原因
            state: 账户状态（不传则从文件加载）
            trades: 交易记录列表（不传则从文件加载）
            report_date_str: 报告日期
            side_label: side 字段值（sell / stop_loss / take_profit / rebalance_sell）

        Returns:
            交易字典，或 None
        """
        s = state if state is not None else self.load_state()
        t = trades if trades is not None else self.load_trades()

        pos = s["positions"].get(code)
        if not pos:
            return None

        qty = min(pos["quantity"], int(quantity / 100) * 100)
        if qty < 100:
            return None

        proceeds_before = qty * price
        fee = calc_sell_fees(proceeds_before)
        proceeds = proceeds_before - fee
        pnl = (price - pos["avg_cost"]) * qty - fee

        s["cash"] += proceeds
        s["total_pnl"] += pnl
        s["total_fee"] += fee

        pos["quantity"] -= qty
        if pos["quantity"] <= 0:
            del s["positions"][code]

        trade = {
            "date": report_date_str,
            "code": code,
            "side": side_label,
            "quantity": qty,
            "price": round(price, 3),
            "proceeds": round(proceeds, 2),
            "fee": round(fee, 2),
            "pnl": round(pnl, 2),
            "reason": reason,
        }
        t.append(trade)

        if state is None:
            self.save_state(s)
        if trades is None:
            self.save_trades(t)

        logger.info(
            f"[执行] 卖出 {code} × {qty} @ {price:.3f} 盈亏{pnl:+.2f} - {reason}"
        )
        return trade

    # ── 完整交易流程（兼容 execute_trades 模式） ──

    def execute_trades(
        self,
        stocks: Dict[str, dict],
        report_date_str: str,
        buy_threshold: int = 70,
        sell_threshold: int = 45,
        extract_price_fn=None,
    ) -> tuple:
        """完整交易流程：风控→卖出→买入→再平衡→更新持仓。

        与原 simulated_trading.py 的 execute_trades 对应，
        但使用注入的依赖（而非模块级函数）。

        Args:
            stocks: 股票评分字典 {code: {score, operation, ...}}
            report_date_str: 报告日期 YYYYMMDD
            buy_threshold: 买入阈值
            sell_threshold: 卖出阈值
            extract_price_fn: 价格提取函数 (code, date) -> float|None

        Returns:
            (new_trades, state, force_sells)
        """
        state = self.load_state()
        trades = self.load_trades()
        current_positions = set(state["positions"].keys())
        new_trades = []
        force_sells = []

        # ── 1️⃣ 强制风控：止损 / 止盈 ──
        for code, pos in list(state["positions"].items()):
            price = (
                extract_price_fn(code, report_date_str)
                if extract_price_fn
                else pos["current_price"]
            )
            if not price:
                price = pos["current_price"]

            action, reason = self.risk.check_stop_loss_dict(
                pos, price, code
            )
            if action != "hold":
                trade = self.execute_sell(
                    code, price, pos["quantity"],
                    reason=reason,
                    state=state, trades=trades,
                    report_date_str=report_date_str,
                    side_label=action,
                )
                if trade:
                    force_sells.append(trade)
                    new_trades.append(trade)
                    logger.info(
                        f"[风控] {action} {code} × {pos['quantity']} @ "
                        f"{price:.3f} 盈亏{trade['pnl']:+.2f} - {reason}"
                    )

        # ── 2️⃣ 账户总回撤检查 ──
        can_buy, dd_pct = self.risk.check_account_drawdown_dict(state)
        if not can_buy:
            logger.info(
                f"[风控] 账户总回撤 {dd_pct:.1f}%，"
                f"低于阈值 {self.risk.config.max_drawdown_pct}%，暂停买入"
            )

        # ── 3️⃣ 动态阈值卖出（评分低于卖出阈值且持有） ──
        for code, info in stocks.items():
            if info["score"] <= sell_threshold and code in state["positions"]:
                pos = state["positions"][code]
                price = (
                    extract_price_fn(code, report_date_str)
                    if extract_price_fn
                    else pos["current_price"]
                )
                if not price:
                    price = pos["current_price"]

                trade = self.execute_sell(
                    code, price, pos["quantity"],
                    reason=f"评分{info['score']} ≤ {sell_threshold}（卖出阈值）",
                    state=state, trades=trades,
                    report_date_str=report_date_str,
                    side_label="sell",
                )
                if trade:
                    new_trades.append(trade)

        # ── 4️⃣ 凯利公式买入 ──
        sentiment_factor = self.risk.load_sentiment_adjustment()
        adjusted_max_pos = max(
            2, int(self._max_positions * sentiment_factor)
        )

        if can_buy and not force_sells:
            buy_candidates = [
                (c, i)
                for c, i in stocks.items()
                if i["score"] >= buy_threshold and c not in state["positions"]
            ]
            buy_candidates.sort(key=lambda x: x[1]["score"], reverse=True)

            for code, info in buy_candidates:
                if len(state["positions"]) >= adjusted_max_pos:
                    logger.info(
                        f"[执行] 已达情绪调整后持仓上限 {adjusted_max_pos}，跳过 {code}"
                    )
                    break

                price = (
                    extract_price_fn(code, report_date_str)
                    if extract_price_fn
                    else None
                )
                if not price or price <= 0:
                    logger.info(
                        f"[执行] 无法获取 {code} 价格，跳过买入"
                    )
                    continue

                amount = self.risk.compute_kelly_amount_dict(
                    info["score"], state["cash"], sentiment_factor
                )
                amount = min(amount, state["cash"])
                quantity = int(amount / price / 100) * 100
                if quantity < 100:
                    logger.info(
                        f"[执行] 资金不足买入 {code}（现金 {state['cash']:.0f}），跳过"
                    )
                    continue

                kelly_pct = self.risk.kelly_fraction(info["score"]) * 100
                trade = self.execute_buy(
                    code, price, quantity,
                    reason=f"评分{info['score']} ≥ {buy_threshold}"
                           f"（凯利{kelly_pct:.1f}%）",
                    state=state, trades=trades,
                    report_date_str=report_date_str,
                )
                if trade:
                    trade["score"] = info["score"]
                    trade["kelly_pct"] = round(kelly_pct, 1)
                    trade["side"] = "buy"
                    # 更新持仓的 entry_score
                    if code in state["positions"]:
                        state["positions"][code]["entry_score"] = info["score"]
                    new_trades.append(trade)

        # ── 5️⃣ 风险平价再平衡（周五执行） ──
        self.risk.try_rebalance(state, trades, new_trades, report_date_str)

        # ── 6️⃣ 更新持仓市价 ──
        for code, pos in state["positions"].items():
            if extract_price_fn:
                price = extract_price_fn(code, report_date_str)
                if price:
                    pos["current_price"] = price

        # ── 更新市值和总权益 ──
        total_market_value = sum(
            p["quantity"] * p["current_price"]
            for p in state["positions"].values()
        )
        total_equity = state["cash"] + total_market_value
        total_return = total_equity - self._initial_capital
        total_return_pct = (
            (total_return / self._initial_capital) * 100
        )

        peak = state.get("peak_equity", self._initial_capital)
        if total_equity > peak:
            peak = total_equity
        current_dd_pct = (total_equity - peak) / peak * 100
        max_dd = state.get("max_drawdown_pct", 0.0)
        if current_dd_pct < max_dd:
            max_dd = current_dd_pct

        state["peak_equity"] = peak
        state["max_drawdown_pct"] = max_dd
        state["total_market_value"] = round(total_market_value, 2)
        state["total_equity"] = round(total_equity, 2)
        state["total_return"] = round(total_return, 2)
        state["total_return_pct"] = round(total_return_pct, 2)
        state["last_update"] = report_date_str

        self.save_state(state)
        self.save_trades(trades)
        return new_trades, state, force_sells
