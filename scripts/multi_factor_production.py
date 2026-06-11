#!/usr/bin/env python3
"""
多因子 B 全面反转 — 生产流水线
=================================
基于回测最优参数（B_全面反转, top20, 42天调仓），每日计算全市场因子分数，
输出持仓推荐、下次再平衡日，并落地 JSON + SQLite 供推送/交易使用。

铁律: 此脚本的因子计算必须引用生产模块（backtest_engine.factors），
因子权重必须引用 config.strategies（单一事实来源），
不可自定任何独立实现。
"""

import json
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path("/opt/daily_stock_analysis")
sys.path.insert(0, str(BASE_DIR))

# ── 导入生产模块（铁律：不得自实现） ──
from backtest_engine.factors import all_factors, score_weighted, zscore_cross_section_pandas
from config.strategies import BEST_PARAMS, STRATEGIES

# ══════════════════════════════════════════
# 最优策略参数（从 config.strategies 单一来源导入）
# ══════════════════════════════════════════
STRATEGY_NAME = "B_全面反转"
FACTOR_WEIGHTS = STRATEGIES[STRATEGY_NAME]  # ← 单一事实来源
PARAMS = BEST_PARAMS[STRATEGY_NAME]
TOP_N = PARAMS["top_n"]  # 20
REBALANCE_DAYS = PARAMS["rebalance_days"]  # 42
SLIPPAGE = PARAMS["slippage"]  # 0.0015
FACTOR_COLS = list(FACTOR_WEIGHTS.keys())

# ── 状态目录 ──
STATE_DIR = BASE_DIR / "multi_factor"
STATE_FILE = STATE_DIR / "state.json"
RANKING_FILE = STATE_DIR / "rankings.json"
SIGNAL_FILE = STATE_DIR / "signal.json"
STATE_DIR.mkdir(parents=True, exist_ok=True)

# ── SQLite 持久化 ──
DB_PATH = BASE_DIR / "data" / "stock_analysis.db"

# 交易日历推算：从数据集的交易日序列中数出 N 个交易日后
# 比固定日历日倍数更准确，与回测引擎的 idx + rebalance_days 逻辑一致
_TRADING_DAYS_CACHE = None


def _get_trading_days(df: pd.DataFrame = None) -> list:
    """获取已排序的完整交易日列表"""
    global _TRADING_DAYS_CACHE
    if _TRADING_DAYS_CACHE is not None:
        return _TRADING_DAYS_CACHE
    # 从加载的数据中提取交易日
    if df is not None:
        days = sorted(df["date"].unique())
        _TRADING_DAYS_CACHE = days
    return _TRADING_DAYS_CACHE or []


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "last_rebalance_date": None,
        "next_rebalance_date": None,
        "holdings": [],
        "strategy": STRATEGY_NAME,
        "top_n": TOP_N,
        "rebalance_days": REBALANCE_DAYS,
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# 标准化和打分委托 backtest_engine.factors 执行（单一事实来源）
_zscore = zscore_cross_section_pandas
_score_row = score_weighted


def save_to_db(date_str: str, rankings: list):
    """将每日排名写入 SQLite stock_analysis.db"""
    if not DB_PATH.exists():
        print(f"  ⚠️ DB 不存在: {DB_PATH}")
        return
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS multi_factor_rankings (
                date TEXT NOT NULL,
                rank INTEGER NOT NULL,
                code TEXT NOT NULL,
                score REAL,
                strategy TEXT NOT NULL DEFAULT 'B_全面反转',
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                PRIMARY KEY (date, code)
            )
        """)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM multi_factor_rankings WHERE date = ? AND strategy = ?", (date_str, STRATEGY_NAME))
        for rank, h in enumerate(rankings, 1):
            cursor.execute(
                "INSERT OR REPLACE INTO multi_factor_rankings "
                "(date, rank, code, score, strategy) VALUES (?,?,?,?,?)",
                (date_str, rank, h["code"], h["score"], STRATEGY_NAME),
            )
        conn.commit()
        conn.close()
        print(f"  💾 SQLite 持久化: {len(rankings)} 条 (date={date_str})")
    except Exception as e:
        print(f"  ⚠️ SQLite 写入失败: {e}")


def load_data() -> pd.DataFrame:
    """
    从数据源加载全市场 OHLCV 数据。

    优先顺序:
    1. stock_analysis.db stock_daily 表（日常增量）
    2. backtest_data.db daily 表（全量快照）
    """
    t0 = time.time()
    # 优先使用 backtest_data.db（全量 1570 只），后补 stock_analysis.db
    for db_path, tbl in [
        (BASE_DIR / "data" / "backtest_data.db", "daily"),
        (BASE_DIR / "data" / "stock_analysis.db", "stock_daily"),
    ]:
        if db_path.exists():
            try:
                conn = sqlite3.connect(str(db_path))
                df = pd.read_sql(
                    f"SELECT date, code, open, close, high, low, volume, amount " f"FROM [{tbl}] ORDER BY date, code",
                    conn,
                    parse_dates=["date"],
                )
                conn.close()
                print(
                    f"  📦 {db_path.name}.{tbl}: {len(df):,} 行, " f"{df['code'].nunique()} 只, {time.time()-t0:.1f}s"
                )
                return df
            except Exception:
                continue
    raise RuntimeError("❌ 无可用数据源！")


def compute_scores(df: pd.DataFrame) -> pd.DataFrame:
    """全市场因子计算 → z-score → 加权得分"""
    t0 = time.time()
    print("  📐 计算因子...", end=" ", flush=True)
    rows = []
    for code, grp in df.groupby("code", sort=False):
        grp = grp.sort_values("date")
        c, v, h, lo = (grp[cname].values for cname in ("close", "volume", "high", "low"))
        for i in range(max(61, 127), len(c)):
            factors = all_factors(c[: i + 1], v[: i + 1], h[: i + 1], lo[: i + 1])
            factors["code"] = code
            factors["date"] = grp["date"].values[i]
            rows.append(factors)
    out = pd.DataFrame(rows)
    print(f"{time.time()-t0:.1f}s | {len(out):,} 行")

    print("  📊 标准化+打分...", end=" ", flush=True)
    scored_rows = []
    for d, day_df in out.groupby("date", sort=True):
        for col in FACTOR_COLS:
            if col in day_df.columns:
                day_df[col] = _zscore(day_df[col])
        for _, row in day_df.iterrows():
            score = _score_row(row, FACTOR_WEIGHTS, FACTOR_COLS)
            entry = {
                "date": row["date"],
                "code": row["code"],
                "score": round(score, 4),
            }
            for col in FACTOR_COLS:
                if col in row:
                    entry[col] = round(row[col], 6)
            scored_rows.append(entry)
    result = pd.DataFrame(scored_rows)
    print(f"{time.time()-t0:.1f}s")
    return result


def get_top_holdings(scored_df: pd.DataFrame, date_str: str) -> list:
    """取指定日期得分最高的 TOP_N"""
    day = scored_df[scored_df["date"] == date_str]
    if day.empty:
        return []
    day = day.sort_values("score", ascending=False).head(TOP_N)
    return day.to_dict("records")


def check_rebalance(state: dict, today: date) -> dict:
    """检查再平衡需求"""
    last = state.get("last_rebalance_date")
    next_d = state.get("next_rebalance_date")
    if next_d:
        next_date = datetime.strptime(next_d, "%Y-%m-%d").date()
        days_left = (next_date - today).days
    else:
        days_left = 0
    should = days_left <= 0 and last is not None or last is None
    reason = (
        "首次运行，需初始化"
        if last is None
        else (f"再平衡日已到 (距上次 {last})" if days_left <= 0 else f"距下次再平衡 {days_left} 天")
    )
    return {
        "should_rebalance": should,
        "reason": reason,
        "last_rebalance": last or "无",
        "next_rebalance": next_d or "待算",
        "days_until_next": days_left,
    }


def format_report(state: dict, signal: dict, top_holdings: list) -> str:
    lines = [
        f"📊【多因子 {STRATEGY_NAME}】{date.today()}",
        f"{'=' * 40}",
        f"策略: {STRATEGY_NAME} | top{TOP_N} | {REBALANCE_DAYS}d调仓",
        f"上次再平衡: {state.get('last_rebalance_date') or '待初始化'}",
        f"下次再平衡: {signal['next_rebalance']} ({signal.get('reason', '')})",
        "",
        f"{'🔄 今日再平衡日！' if signal.get('should_rebalance') else '📋 当前持仓'} ({len(top_holdings)}/{TOP_N}):",
    ]
    for i, h in enumerate(top_holdings[:TOP_N], 1):
        score = h["score"]
        bar = "█" * max(1, min(20, int(abs(score) * 5)))
        lines.append(f"  {i:>2}. {h['code']}  得分 {score:+.4f}  {bar}")
    lines.extend(
        [
            "",
            f"💡 策略: 多因子横截面打分，反转因子主导",
            f"   回测: +15.49% | 夏普 0.90 | 回撤 -12.45% | Walk-forward 4折",
        ]
    )
    return "\n".join(lines)


def main():
    today = date.today()
    print(f"╔{'═' * 48}╗")
    print(f"║  📊 多因子 {STRATEGY_NAME} — 生产流水线    ║")
    print(f"║  {today}                      ║")
    print(f"╚{'═' * 48}╝")

    df = load_data()
    latest_date = df["date"].max()
    print(f"  最新数据: {latest_date}")

    scored = compute_scores(df)
    last_date = scored["date"].max()
    print(f"  最新因子: {last_date}")
    top = get_top_holdings(scored, last_date)

    state = load_state()
    signal = check_rebalance(state, today)

    # ── 推算下次再平衡日（交易日计数，与回测引擎逻辑一致） ──
    trading_days = _get_trading_days(df)
    if trading_days and str(today) in trading_days:
        today_idx = trading_days.index(str(today))
        next_idx = min(today_idx + REBALANCE_DAYS, len(trading_days) - 1)
        next_rebal_date_str = trading_days[next_idx]
    else:
        # 回退：按自然日估算（交易日 ≈ 自然日 × 1.49）
        from datetime import timedelta

        next_rebal_date_str = str(today + timedelta(days=int(REBALANCE_DAYS * 365 / 245)))

    if signal["should_rebalance"]:
        state["last_rebalance_date"] = str(today)
        state["next_rebalance_date"] = next_rebal_date_str
        state["holdings"] = top
        save_state(state)
        print(f"\n🔄 再平衡执行！新持仓 {len(top)} 只")
    elif state.get("holdings"):
        top = state["holdings"]
        print(f"\n📋 持有 {len(top)} 只 (上次 {state.get('last_rebalance_date')})")
    else:
        state["last_rebalance_date"] = str(today)
        state["next_rebalance_date"] = next_rebal_date_str
        state["holdings"] = top
        save_state(state)
        print(f"\n🆕 首次初始化！持仓 {len(top)} 只")

    # 保存 JSON
    today_str = str(today)
    ranking_data = {
        "date": today_str,
        "strategy": STRATEGY_NAME,
        "data_date": str(latest_date)[:10],
        "factor_date": str(last_date)[:10],
        "top_holdings": top,
        "signal": signal,
        "state": {k: v for k, v in state.items() if k != "holdings"},
    }
    RANKING_FILE.write_text(json.dumps(ranking_data, ensure_ascii=False, indent=2))
    print(f"\n💾 JSON: {RANKING_FILE}")

    # 写入 SQLite（数据入库）
    save_to_db(today_str, top)

    # 精简信号
    signal_data = {
        "date": today_str,
        "strategy": STRATEGY_NAME,
        "should_rebalance": signal["should_rebalance"],
        "next_rebalance": signal["next_rebalance"],
        "holdings": [h["code"] for h in top],
        "scores": {h["code"]: h["score"] for h in top},
    }
    SIGNAL_FILE.write_text(json.dumps(signal_data, ensure_ascii=False, indent=2))

    # 文字报告
    print(f"\n{format_report(state, signal, top)}")

    return ranking_data


if __name__ == "__main__":
    main()
