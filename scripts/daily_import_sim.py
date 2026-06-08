#!/usr/bin/env python3
"""
每日模拟交易数据自动入库 SQLite

数据来源：
  /opt/daily_stock_analysis/simulated_trading/performance.json  — daily + weekly + summary
  /opt/daily_stock_analysis/simulated_trading/state.json         — 当前状态 + 持仓快照
  /opt/daily_stock_analysis/simulated_trading/trades.json        — 成交记录

幂等规则：
  sim_equity_daily → date 去重
  sim_trades       → (date, code, side, price) 去重
  sim_positions    → 先清空再写入（快照）
  sim_state        → 先清空再写入（快照）
  sim_weekly       → week 去重
"""
import json
import logging
import os
import sqlite3
import sys
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

DB_PATH = "/opt/daily_stock_analysis/data/stock_analysis.db"
SIM_DIR = "/opt/daily_stock_analysis/simulated_trading"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _float(v) -> Optional[float]:
    """安全转换为 float；None/空字符串返回 None"""
    if v is None or v == "" or v == "nan":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _int(v, default=None) -> Optional[int]:
    """安全转换为 int；None/空字符串返回默认值"""
    if v is None or v == "":
        return default
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return default


def load_json(path: str):
    """加载 JSON 文件，出错时抛出（调用方处理）"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def import_equity_daily(conn: sqlite3.Connection, performance: dict) -> int:
    """导入每日权益，date 去重，返回新增行数"""
    daily = performance.get("daily", [])
    if not daily:
        return 0

    added = 0
    for day in daily:
        date = day.get("date")
        if not date:
            continue

        # 幂等：已有 date 则跳过
        existing = conn.execute(
            "SELECT COUNT(*) FROM sim_equity_daily WHERE date = ?", (date,)
        ).fetchone()[0]
        if existing > 0:
            continue

        conn.execute(
            """INSERT INTO sim_equity_daily
               (date, equity, cash, market_value, positions, return_pct, max_dd_pct,
                buy_today, sell_today, total_closed, win_rate)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                date,
                _float(day.get("equity")),
                _float(day.get("cash")),
                _float(day.get("market_value")),
                _int(day.get("positions"), 0),
                _float(day.get("return_pct")),
                _float(day.get("max_dd_pct")),
                _int(day.get("buy_today"), 0),
                _int(day.get("sell_today"), 0),
                _int(day.get("total_closed_trades"), 0),
                _float(day.get("win_rate")),
            ),
        )
        added += 1

    return added


def import_weekly(conn: sqlite3.Connection, performance: dict) -> int:
    """导入周报，week 去重，返回新增行数"""
    weekly = performance.get("weekly", [])
    if not weekly:
        return 0

    added = 0
    for wk in weekly:
        week = wk.get("week")
        if not week:
            continue

        existing = conn.execute(
            "SELECT COUNT(*) FROM sim_weekly WHERE week = ?", (week,)
        ).fetchone()[0]
        if existing > 0:
            continue

        conn.execute(
            """INSERT INTO sim_weekly
               (week, start_date, end_date, start_equity, end_equity,
                buy_count, sell_count, closed_wins, closed_losses)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                week,
                wk.get("start_date"),
                wk.get("end_date"),
                _float(wk.get("start_equity")),
                _float(wk.get("end_equity")),
                _int(wk.get("buy_count")),
                _int(wk.get("sell_count")),
                _int(wk.get("closed_wins")),
                _int(wk.get("closed_losses")),
            ),
        )
        added += 1

    return added


def import_trades(conn: sqlite3.Connection, trades: list) -> int:
    """导入成交记录，(date, code, side, price) 去重，返回新增行数"""
    if not trades:
        return 0

    added = 0
    for t in trades:
        if not isinstance(t, dict):
            continue
        date = t.get("date")
        code = t.get("code")
        side = t.get("side")
        price = _float(t.get("price"))

        if not date or not code or not side:
            continue

        if price is None:
            continue

        # 幂等：唯一键 (date, code, side, price) 使用精度容差
        existing = conn.execute(
            """SELECT COUNT(*) FROM sim_trades
               WHERE date = ? AND code = ? AND side = ? AND ABS(price - ?) < 0.001""",
            (date, code, side, price),
        ).fetchone()[0]
        if existing > 0:
            continue

        conn.execute(
            """INSERT INTO sim_trades
               (date, code, side, price, shares, pnl, reason)
               VALUES (?,?,?,?,?,?,?)""",
            (
                date,
                code,
                side,
                price,
                _int(t.get("quantity"), 0),  # JSON 字段名是 quantity
                _float(t.get("pnl")),
                t.get("reason"),
            ),
        )
        added += 1

    return added


def import_state_and_positions(conn: sqlite3.Connection, state: dict) -> int:
    """清空 sim_state 和 sim_positions，重新写入快照，返回持仓数"""
    conn.execute("SAVEPOINT snapshot")
    # 清空旧数据
    conn.execute("DELETE FROM sim_state")
    conn.execute("DELETE FROM sim_positions")

    try:
        # 写入状态
        conn.execute(
            """INSERT INTO sim_state
               (cash, total_market_value, total_equity, total_pnl, total_fee,
                total_return_pct, peak_equity, max_drawdown_pct, last_update)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                _float(state.get("cash")),
                _float(state.get("total_market_value")),
                _float(state.get("total_equity")),
                _float(state.get("total_pnl")),
                _float(state.get("total_fee")),
                _float(state.get("total_return_pct")),
                _float(state.get("peak_equity")),
                _float(state.get("max_drawdown_pct")),
                state.get("last_update"),
            ),
        )

        # 写入持仓
        pos_count = 0
        positions = state.get("positions", {})
        for code, pos in positions.items():
            conn.execute(
                """INSERT INTO sim_positions
                   (code, quantity, avg_cost, current_price, invested,
                    entry_score, entry_date, tp_level, mode)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    code,
                    _int(pos.get("quantity")),
                    _float(pos.get("avg_cost")),
                    _float(pos.get("current_price")),
                    _float(pos.get("invested")),
                    _int(pos.get("entry_score")),
                    pos.get("entry_date"),
                    _int(pos.get("tp_level")),
                    pos.get("mode"),
                ),
            )
            pos_count += 1

        conn.execute("RELEASE SAVEPOINT snapshot")
        return pos_count
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT snapshot")
        raise


def main():
    # 检查数据目录
    if not os.path.isdir(SIM_DIR):
        logger.error("数据目录不存在: %s", SIM_DIR)
        sys.exit(1)

    conn = None
    results = {
        "equity_daily": 0,
        "weekly": 0,
        "trades": 0,
        "positions": 0,
        "state": False,
    }
    errors = []

    try:
        conn = get_conn()

        # 1. performance.json
        try:
            perf = load_json(os.path.join(SIM_DIR, "performance.json"))
            results["equity_daily"] = import_equity_daily(conn, perf)
            results["weekly"] = import_weekly(conn, perf)
        except FileNotFoundError:
            errors.append("performance.json 文件不存在")
        except json.JSONDecodeError as e:
            errors.append(f"performance.json JSON 解析错误: {e}")

        # 2. trades.json
        try:
            trades = load_json(os.path.join(SIM_DIR, "trades.json"))
            if isinstance(trades, list):
                results["trades"] = import_trades(conn, trades)
            else:
                errors.append("trades.json 不是数组")
        except FileNotFoundError:
            errors.append("trades.json 文件不存在")
        except json.JSONDecodeError as e:
            errors.append(f"trades.json JSON 解析错误: {e}")

        # 3. state.json（快照：清空再写入）
        try:
            state = load_json(os.path.join(SIM_DIR, "state.json"))
            results["positions"] = import_state_and_positions(conn, state)
            results["state"] = True
        except FileNotFoundError:
            errors.append("state.json 文件不存在")
        except json.JSONDecodeError as e:
            errors.append(f"state.json JSON 解析错误: {e}")

        conn.commit()

    except sqlite3.Error as e:
        logger.error("数据库错误: %s", e)
        sys.exit(2)
    except Exception as e:
        logger.error("未知错误: %s", e)
        sys.exit(3)
    finally:
        if conn:
            conn.close()

    # 输出汇总
    parts = []
    if results["equity_daily"]:
        parts.append(f"{results['equity_daily']} 天权益")
    if results["weekly"]:
        parts.append(f"{results['weekly']} 份周报")
    if results["trades"]:
        parts.append(f"{results['trades']} 笔成交")
    if results["positions"]:
        parts.append(f"{results['positions']} 个持仓")
    if results["state"]:
        parts.append("状态已更新")

    if parts:
        logger.info("已导入 %s", ', '.join(parts))
    else:
        logger.info("无新数据导入")

    # 错误汇总
    if errors:
        for e in errors:
            logger.warning("%s", e)


if __name__ == "__main__":
    main()
