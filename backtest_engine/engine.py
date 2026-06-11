"""
回测引擎 — 多因子、月度调仓、Walk-forward 验证

数据源: SQLite (从 JSON 缓存导入的稳定快照)
成交价: 调仓日次日开盘价 + 滑点
费率: 生产模块 calc_buy_fees / calc_sell_fees
打分: 横截面 z-score 标准化后加权求和
"""

import sqlite3
import time
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

from backtest_engine.factors import all_factors, score_weighted, zscore_cross_section
from layers.execution_layer.fees import calc_buy_fees, calc_sell_fees

DB_PATH = Path("data/backtest_data.db")
SLIPPAGE = 0.0015

FactorWeights = Dict[str, float]


def load_data() -> pd.DataFrame:
    """从 SQLite 加载全部数据"""
    t0 = time.time()
    conn = sqlite3.connect(str(DB_PATH))
    df = pd.read_sql(
        "SELECT date, code, open, close, high, low, volume, amount " "FROM daily ORDER BY date, code",
        conn,
        parse_dates=["date"],
    )
    conn.close()
    print(f"  📊 {len(df):,} 行, {df['code'].nunique()} 只, " f"{df['date'].nunique()} 天, {time.time()-t0:.1f}s")
    return df


def compute_factor_values(df: pd.DataFrame) -> pd.DataFrame:
    """逐股票逐日计算因子，返回 [date, code, factor_cols...]"""
    t0 = time.time()
    print("  📐 计算因子...", end=" ", flush=True)
    rows = []
    for code, grp in df.groupby("code", sort=False):
        grp = grp.sort_values("date")
        c, v, h, lo, ds = (grp[cname].values for cname in ("close", "volume", "high", "low", "date"))
        for i in range(20, len(c)):
            factors = all_factors(c[: i + 1], v[: i + 1], h[: i + 1], lo[: i + 1])
            factors["code"] = code
            factors["date"] = ds[i]
            rows.append(factors)
    out = pd.DataFrame(rows)
    print(f"{time.time()-t0:.1f}s | {len(out):,} 行")
    return out


def _zscore_day(day_factors: pd.DataFrame, factor_cols: List[str]) -> pd.DataFrame:
    """对当日所有股票的指定因子列做横截面 z-score 标准化（in-place）。
    委托 backtest_engine.factors.zscore_cross_section 执行。"""
    for col in factor_cols:
        day_factors[col] = zscore_cross_section(day_factors[col].values)
    return day_factors


class BacktestEngine:

    def __init__(
        self,
        factor_weights: FactorWeights,
        top_n: int = 10,
        rebalance_days: int = 21,
        use_open: bool = True,
        slippage: float = SLIPPAGE,
    ):
        self.weights = factor_weights
        self.factor_cols = list(factor_weights.keys())
        self.top_n = top_n
        self.rebalance_days = rebalance_days
        self.use_open = use_open
        self.slippage = slippage

    def _score_row(self, row: pd.Series) -> float:
        """委托 backtest_engine.factors.score_weighted 执行。"""
        return score_weighted(row, self.weights, list(self.weights.keys()))

    def run(
        self,
        factor_df: pd.DataFrame,
        price_df: pd.DataFrame,
        initial_cash: float = 100_000.0,
    ) -> dict:
        """
        运行回测

        标准化策略: 只在调仓日对当日所有股票做横截面 z-score，然后打分选股。
        非调仓日不标准化（也不选股）。

        返回 {strategy, total_return_pct, sharpe_ratio, max_drawdown_pct,
              win_rate_pct, total_trades, total_fees, eq_curve}
        """
        all_dates = sorted(factor_df["date"].unique())
        cash = initial_cash
        positions: Dict[str, dict] = {}
        fees = 0.0
        trades = 0
        wins = 0
        losses = 0
        eq_curve = []
        peak = initial_cash
        next_rebalance = 0

        print(
            f"  🏃 回测: {len(all_dates)}天, top{self.top_n}, " f"{self.rebalance_days}d, 因子={self.factor_cols}",
            flush=True,
        )

        dates_enum = list(enumerate(all_dates))

        for idx, cur_date in dates_enum:
            df_day_factor = factor_df[factor_df["date"] == cur_date]
            if df_day_factor.empty:
                continue
            df_day_price = price_df[price_df["date"] == cur_date]

            # ---- 调仓日: 标准化+打分+执行 ----
            if idx >= next_rebalance:
                # 横截面标准化
                df_z = df_day_factor.copy()
                _zscore_day(df_z, self.factor_cols)

                # 打分排序
                scored = []
                for _, row in df_z.iterrows():
                    s = self._score_row(row)
                    scored.append((s, row["code"]))
                scored.sort(key=lambda x: x[0], reverse=True)
                target = [c for _, c in scored[: self.top_n]]
                tgt_set = set(target)

                # 卖出不在目标中的
                exit_cash = 0.0
                for code in list(positions.keys()):
                    if code in tgt_set:
                        continue
                    pi = positions.pop(code)
                    pr = df_day_price[df_day_price["code"] == code]
                    if pr.empty:
                        continue
                    sp = pr["open"].values[0] if self.use_open else pr["close"].values[0]
                    sp *= 1 - self.slippage
                    q = pi["qty"]
                    sf = calc_sell_fees(q * sp)
                    pnl = q * sp - sf - q * pi["avg_cost"]
                    exit_cash += q * sp - sf
                    fees += sf
                    trades += 1
                    (wins := wins + 1) if pnl > 0 else (losses := losses + 1)
                cash += exit_cash

                # 买入新仓
                if target:
                    buy_per = cash / len(target)
                    used = 0.0
                    for code in target:
                        if code in positions and positions[code]["qty"] > 0:
                            continue
                        pr = df_day_price[df_day_price["code"] == code]
                        if pr.empty:
                            continue
                        bp = pr["open"].values[0] if self.use_open else pr["close"].values[0]
                        bp *= 1 + self.slippage
                        qty = int(buy_per / bp / 100) * 100
                        if qty < 100:
                            continue
                        cost = qty * bp
                        bf = calc_buy_fees(cost)
                        if cost + bf > cash - used:
                            qty = int((cash - used - bf) / bp / 100) * 100
                            if qty < 100:
                                continue
                            cost = qty * bp
                            bf = calc_buy_fees(cost)
                        used += cost + bf
                        trades += 1
                        positions[code] = {"qty": qty, "avg_cost": bp}
                    cash -= used

                next_rebalance = idx + self.rebalance_days

            # ---- 每日权益 ----
            pos_val = sum(
                pi["qty"] * pr_df["close"].values[0]
                for code, pi in positions.items()
                if not (pr_df := df_day_price[df_day_price["code"] == code]).empty
            )
            equity = cash + pos_val
            peak = max(peak, equity)
            dd = (equity - peak) / peak * 100 if peak > 1e-6 else 0
            eq_curve.append(
                {
                    "date": str(cur_date)[:10],
                    "equity": round(equity, 2),
                    "dd": round(dd, 2),
                }
            )

        # 末日出清
        last_date = all_dates[-1]
        ldp = price_df[price_df["date"] == last_date]
        for code, pi in positions.items():
            pr = ldp[ldp["code"] == code]
            if pr.empty:
                continue
            sp = pr["close"].values[0] * (1 - self.slippage)
            sf = calc_sell_fees(pi["qty"] * sp)
            cash += pi["qty"] * sp - sf
            fees += sf

        # 统计
        ret = (cash - initial_cash) / initial_cash * 100
        es = np.array([e["equity"] for e in eq_curve])
        max_dd = min(e["dd"] for e in eq_curve) if eq_curve else 0
        rs = np.diff(es) / np.maximum(es[:-1], 1) if len(es) > 1 else np.array([0])
        sr = float(np.mean(rs) / np.std(rs) * np.sqrt(252)) if np.std(rs) > 1e-8 else 0
        wr = wins / max(trades, 1) * 100

        return {
            "strategy": f"top{self.top_n}_{self.rebalance_days}d",
            "total_return_pct": round(ret, 2),
            "sharpe_ratio": round(sr, 3),
            "max_drawdown_pct": round(max_dd, 2),
            "win_rate_pct": round(wr, 1),
            "total_trades": trades,
            "total_fees": round(fees, 2),
            "trading_days": len(eq_curve),
            "eq_curve": eq_curve,
        }
