#!/usr/bin/env python3
"""
将所有回测数据迁移到 SQLite 数据库（bt_ 前缀表空间）
"""
import csv
import json
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

DB_PATH = "/opt/daily_stock_analysis/data/stock_analysis.db"
WORKSPACE = os.path.join(os.path.dirname(__file__), "..")
PROJECT = "/opt/daily_stock_analysis"


def get_conn():
    import sqlite3
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def create_schema(conn):
    """创建 bt_ 前缀的 schema，逐句执行避免隐式提交"""
    statements = [
        # ===== 核心回测运行 =====
        """CREATE TABLE IF NOT EXISTS bt_backtest_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_name    TEXT NOT NULL,
            source      TEXT,
            run_date    TEXT,
            stock_count INTEGER,
            notes       TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_bt_runs_name ON bt_backtest_runs(run_name)",

        # ===== 交易明细 =====
        """CREATE TABLE IF NOT EXISTS bt_trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          INTEGER REFERENCES bt_backtest_runs(id),
            code            TEXT,
            name            TEXT,
            entry_date      TEXT,
            entry_price     REAL,
            exit_date       TEXT,
            exit_price      REAL,
            return_pct      REAL,
            net_return_pct  REAL,
            hold_days       INTEGER,
            entry_turn      REAL,
            entry_vol_ratio REAL,
            entry_mcap      REAL,
            next_high_pct   REAL,
            next_low_pct    REAL,
            open_pct        REAL,
            entry_daily_ret REAL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_bt_trades_run ON bt_trades(run_id)",
        "CREATE INDEX IF NOT EXISTS idx_bt_trades_code ON bt_trades(code)",

        # ===== 月度统计 =====
        """CREATE TABLE IF NOT EXISTS bt_monthly_stats (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      INTEGER REFERENCES bt_backtest_runs(id),
            year_month  TEXT,
            trade_count INTEGER,
            avg_return  REAL,
            win_count   INTEGER,
            win_rate    REAL,
            avg_ret_pct REAL
        )""",

        # ===== 季度统计 =====
        """CREATE TABLE IF NOT EXISTS bt_quarterly_stats (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      INTEGER REFERENCES bt_backtest_runs(id),
            quarter     TEXT,
            trade_count INTEGER,
            avg_return  REAL,
            win_count   INTEGER,
            win_rate    REAL,
            avg_ret_pct REAL
        )""",

        # ===== 汇总指标 =====
        """CREATE TABLE IF NOT EXISTS bt_metrics_summary (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          INTEGER REFERENCES bt_backtest_runs(id),
            total_return    REAL,
            ann_return      REAL,
            win_rate        REAL,
            trade_count     INTEGER,
            profit_ratio    REAL,
            avg_win_pct     REAL,
            avg_loss_pct    REAL,
            avg_return_pct  REAL,
            max_drawdown    REAL,
            sharpe_ratio    REAL,
            backtest_days   INTEGER,
            final_equity    REAL,
            n_stocks        INTEGER
        )""",

        # ===== 权益曲线(日) =====
        """CREATE TABLE IF NOT EXISTS bt_equity_daily (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          INTEGER REFERENCES bt_backtest_runs(id),
            date            TEXT,
            equity          REAL,
            cash            REAL,
            market_value    REAL,
            positions       INTEGER,
            return_pct      REAL,
            max_dd_pct      REAL,
            buy_today       INTEGER DEFAULT 0,
            sell_today      INTEGER DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS idx_bt_equity_run ON bt_equity_daily(run_id)",

        # ===== 策略对比 =====
        """CREATE TABLE IF NOT EXISTS bt_strategy_comparisons (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      INTEGER REFERENCES bt_backtest_runs(id),
            strategy    TEXT,
            ann_ret     REAL,
            sharpe      REAL,
            max_dd      REAL,
            win_rate    REAL,
            total_ret   REAL,
            trade_count INTEGER,
            final_equity REAL,
            n_stocks    INTEGER,
            json_data   TEXT
        )""",

        # ===== 模拟交易: 当前状态 =====
        """CREATE TABLE IF NOT EXISTS bt_sim_state (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            cash            REAL,
            total_market_value REAL,
            total_equity    REAL,
            total_pnl       REAL,
            total_fee       REAL,
            total_return_pct REAL,
            peak_equity     REAL,
            max_drawdown_pct REAL,
            last_update     TEXT
        )""",

        # ===== 模拟交易: 持仓 =====
        """CREATE TABLE IF NOT EXISTS bt_sim_positions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            code        TEXT,
            quantity    INTEGER,
            avg_cost    REAL,
            current_price REAL,
            invested    REAL,
            entry_score INTEGER,
            entry_date  TEXT,
            tp_level    INTEGER,
            mode        TEXT
        )""",

        # ===== 模拟交易: 每日权益 =====
        """CREATE TABLE IF NOT EXISTS bt_sim_equity_daily (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT,
            equity          REAL,
            cash            REAL,
            market_value    REAL,
            positions       INTEGER,
            return_pct      REAL,
            max_dd_pct      REAL,
            buy_today       INTEGER DEFAULT 0,
            sell_today      INTEGER DEFAULT 0,
            total_closed    INTEGER DEFAULT 0,
            win_rate        REAL
        )""",

        # ===== 模拟交易: 成交记录 =====
        """CREATE TABLE IF NOT EXISTS bt_sim_trades (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            date    TEXT,
            code    TEXT,
            side    TEXT,
            price   REAL,
            shares  INTEGER,
            pnl     REAL,
            reason  TEXT
        )""",

        # ===== 模拟交易: 周报 =====
        """CREATE TABLE IF NOT EXISTS bt_sim_weekly (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            week        TEXT,
            start_date  TEXT,
            end_date    TEXT,
            start_equity REAL,
            end_equity  REAL,
            buy_count   INTEGER DEFAULT 0,
            sell_count  INTEGER DEFAULT 0,
            closed_wins INTEGER DEFAULT 0,
            closed_losses INTEGER DEFAULT 0
        )""",

        # ===== 回测报告(多策略) =====
        """CREATE TABLE IF NOT EXISTS bt_backtest_reports (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      INTEGER REFERENCES bt_backtest_runs(id),
            strategy    TEXT,
            ann_ret     REAL,
            ann_vol     REAL,
            sharpe      REAL,
            calmar      REAL,
            max_dd      REAL,
            max_dd_pct  REAL,
            win_rate    REAL,
            profit_factor REAL,
            total_ret   REAL,
            total_trades INTEGER,
            avg_hold    REAL,
            json_data   TEXT
        )""",
    ]
    for stmt in statements:
        if stmt.strip():
            conn.execute(stmt)

    # 数据元信息表（用于幂等标记）
    conn.execute("""CREATE TABLE IF NOT EXISTS data_meta (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT DEFAULT (datetime('now'))
    )""")

    conn.commit()


def check_idempotent(conn) -> bool:
    """检查是否已执行过迁移"""
    row = conn.execute(
        "SELECT value FROM data_meta WHERE key = ?", ("migration_v1",)
    ).fetchone()
    if row:
        logger.info("迁移已执行（标记: migration_v1=%s），跳过", row[0])
        return True
    return False


def mark_migrated(conn):
    """写入迁移完成标记"""
    conn.execute(
        "INSERT OR REPLACE INTO data_meta (key, value, updated_at) VALUES (?, ?, datetime('now'))",
        ("migration_v1", "completed"),
    )
    conn.commit()


def import_workspace_results(conn):
    """导入 workspace/backtest_results/ 下的 CSV 和 metrics.json"""
    results_dir = os.path.join(WORKSPACE, "backtest_results")

    # --- metrics.json → bt_metrics_summary ---
    metrics_file = os.path.join(results_dir, "metrics.json")
    if os.path.exists(metrics_file):
        with open(metrics_file) as f:
            m = json.load(f)
        total_ret = m.get("总收益率(%)")
        ann_ret = m.get("年化收益率(%)")
        win_rate = m.get("胜率(%)")
        trade_cnt = m.get("交易次数")
        profit_r = m.get("盈亏比")
        avg_win = m.get("平均盈利(%)")
        avg_loss = m.get("平均亏损(%)")
        avg_ret = m.get("平均收益率(%)")
        max_dd = m.get("最大回撤(%)")
        days = m.get("回测总天数")

        conn.execute(
            "INSERT INTO bt_backtest_runs(run_name, source, notes) VALUES (?,?,?)",
            ("全市场回测(workspace)", "workspace", "workspace backtest_results/metrics.json"),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        conn.execute(
            """INSERT INTO bt_metrics_summary(run_id,total_return,ann_return,win_rate,
            trade_count,profit_ratio,avg_win_pct,avg_loss_pct,avg_return_pct,
            max_drawdown,backtest_days) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, total_ret, ann_ret, win_rate,
             trade_cnt, profit_r, avg_win, avg_loss, avg_ret,
             max_dd, days),
        )

        logger.info("  [✓] metrics.json → run_id=%d", run_id)

    # --- all_trades.csv ---
    trades_file = os.path.join(results_dir, "all_trades.csv")
    if os.path.exists(trades_file):
        with open(trades_file) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        run_id = conn.execute(
            "SELECT id FROM bt_backtest_runs WHERE source='workspace' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if run_id:
            run_id = run_id[0]
            for row in rows:
                conn.execute(
                    """INSERT INTO bt_trades(run_id,code,name,entry_date,entry_price,
                    exit_date,exit_price,return_pct,net_return_pct,hold_days,
                    entry_turn,entry_vol_ratio,entry_mcap,next_high_pct,
                    next_low_pct,open_pct,entry_daily_ret)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id, row.get("code"), row.get("name"),
                        row.get("entry_date"), _float(row.get("entry_price")),
                        row.get("exit_date"), _float(row.get("exit_price")),
                        _float(row.get("return_pct")), _float(row.get("net_return_pct")),
                        _int(row.get("hold_days")),
                        _float(row.get("entry_turn")), _float(row.get("entry_vol_ratio")),
                        _float(row.get("entry_mcap")), _float(row.get("next_high_pct")),
                        _float(row.get("next_low_pct")), _float(row.get("open_pct")),
                        _float(row.get("entry_daily_ret")),
                    ),
                )
            logger.info("  [✓] all_trades.csv → %d 条交易", len(rows))

    # --- monthly_stats.csv ---
    monthly_file = os.path.join(results_dir, "monthly_stats.csv")
    if os.path.exists(monthly_file):
        with open(monthly_file) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        run_id = conn.execute(
            "SELECT id FROM bt_backtest_runs WHERE source='workspace' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if run_id:
            run_id = run_id[0]
            for row in rows:
                # CSV 列名 fallback: 中文/英文混用
                trade_count = _int(
                    row.get("交易次数") or row.get("trade_count") or row.get("trades")
                )
                avg_return = _float(
                    row.get("平均收益率") or row.get("avg_return") or row.get("avg_return_pct")
                )
                win_count = _int(
                    row.get("盈利次数") or row.get("win_count") or row.get("wins")
                )
                win_rate = _float(
                    row.get("胜率(%)") or row.get("win_rate") or row.get("win_rate_pct")
                )
                avg_ret_pct = _float(
                    row.get("平均收益率(%)") or row.get("avg_ret_pct") or row.get("avg_return_pct")
                )
                conn.execute(
                    """INSERT INTO bt_monthly_stats(run_id,year_month,trade_count,
                    avg_return,win_count,win_rate,avg_ret_pct) VALUES (?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        row.get("year_month") or row.get("month"),
                        trade_count, avg_return, win_count, win_rate, avg_ret_pct,
                    ),
                )
            logger.info("  [✓] monthly_stats.csv → %d 条", len(rows))

    # --- quarterly_stats.csv ---
    quarterly_file = os.path.join(results_dir, "quarterly_stats.csv")
    if os.path.exists(quarterly_file):
        with open(quarterly_file) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        run_id = conn.execute(
            "SELECT id FROM bt_backtest_runs WHERE source='workspace' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if run_id:
            run_id = run_id[0]
            for row in rows:
                trade_count = _int(
                    row.get("交易次数") or row.get("trade_count") or row.get("trades")
                )
                avg_return = _float(
                    row.get("平均收益率") or row.get("avg_return") or row.get("avg_return_pct")
                )
                win_count = _int(
                    row.get("盈利次数") or row.get("win_count") or row.get("wins")
                )
                win_rate = _float(
                    row.get("胜率(%)") or row.get("win_rate") or row.get("win_rate_pct")
                )
                avg_ret_pct = _float(
                    row.get("平均收益率(%)") or row.get("avg_ret_pct") or row.get("avg_return_pct")
                )
                conn.execute(
                    """INSERT INTO bt_quarterly_stats(run_id,quarter,trade_count,
                    avg_return,win_count,win_rate,avg_ret_pct) VALUES (?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        row.get("quarter"),
                        trade_count, avg_return, win_count, win_rate, avg_ret_pct,
                    ),
                )
            logger.info("  [✓] quarterly_stats.csv → %d 条", len(rows))

    conn.commit()


def import_simulated_trading(conn):
    """导入 /opt/daily_stock_analysis/simulated_trading/ 下的数据"""
    import sqlite3
    sim_dir = os.path.join(PROJECT, "simulated_trading")
    if not os.path.isdir(sim_dir):
        logger.info("  [–] simulated_trading 目录不存在，跳过")
        return

    # --- 当前模拟状态 ---
    state_file = os.path.join(sim_dir, "state.json")
    if os.path.exists(state_file):
        with open(state_file) as f:
            s = json.load(f)
        conn.execute(
            """INSERT INTO bt_sim_state(cash,total_market_value,total_equity,
            total_pnl,total_fee,total_return_pct,peak_equity,max_drawdown_pct,
            last_update) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                _float(s.get("cash")), _float(s.get("total_market_value")), _float(s.get("total_equity")),
                _float(s.get("total_pnl")), _float(s.get("total_fee")), _float(s.get("total_return_pct")),
                _float(s.get("peak_equity")), _float(s.get("max_drawdown_pct")), s.get("last_update"),
            ),
        )
        for code, pos in s.get("positions", {}).items():
            conn.execute(
                """INSERT INTO bt_sim_positions(code,quantity,avg_cost,current_price,
                invested,entry_score,entry_date,tp_level,mode)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    code, _int(pos.get("quantity")), _float(pos.get("avg_cost")),
                    _float(pos.get("current_price")), _float(pos.get("invested")),
                    _int(pos.get("entry_score")), pos.get("entry_date"),
                    _int(pos.get("tp_level")), pos.get("mode"),
                ),
            )
        logger.info("  [✓] sim_state → 状态 + %d 持仓", len(s.get("positions", {})))

    # --- 模拟交易: 每日权益（date 去重） ---
    perf_file = os.path.join(sim_dir, "performance.json")
    if os.path.exists(perf_file):
        with open(perf_file) as f:
            p = json.load(f)
        daily_added = 0
        for day in p.get("daily", []):
            date = day.get("date")
            if not date:
                continue
            existing = conn.execute(
                "SELECT COUNT(*) FROM bt_sim_equity_daily WHERE date = ?", (date,)
            ).fetchone()[0]
            if existing > 0:
                continue
            conn.execute(
                """INSERT INTO bt_sim_equity_daily(date,equity,cash,market_value,
                positions,return_pct,max_dd_pct,buy_today,sell_today,
                total_closed,win_rate) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    date, _float(day.get("equity")), _float(day.get("cash")),
                    _float(day.get("market_value")), _int(day.get("positions")),
                    _float(day.get("return_pct")), _float(day.get("max_dd_pct")),
                    _int(day.get("buy_today"), 0), _int(day.get("sell_today"), 0),
                    _int(day.get("total_closed_trades"), 0), _float(day.get("win_rate")),
                ),
            )
            daily_added += 1

        weekly_added = 0
        for wk in p.get("weekly", []):
            week = wk.get("week")
            if not week:
                continue
            existing = conn.execute(
                "SELECT COUNT(*) FROM bt_sim_weekly WHERE week = ?", (week,)
            ).fetchone()[0]
            if existing > 0:
                continue
            conn.execute(
                """INSERT INTO bt_sim_weekly(week,start_date,end_date,
                start_equity,end_equity,buy_count,sell_count,
                closed_wins,closed_losses) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    week, wk.get("start_date"), wk.get("end_date"),
                    _float(wk.get("start_equity")), _float(wk.get("end_equity")),
                    _int(wk.get("buy_count"), 0), _int(wk.get("sell_count"), 0),
                    _int(wk.get("closed_wins"), 0), _int(wk.get("closed_losses"), 0),
                ),
            )
            weekly_added += 1

        logger.info("  [✓] sim_equity_daily → %d天 + %d周", daily_added, weekly_added)

    # --- 模拟交易: 成交记录（(date, code, side, price) 去重） ---
    trades_file = os.path.join(sim_dir, "trades.json")
    if os.path.exists(trades_file):
        with open(trades_file) as f:
            tlist = json.load(f)
        trades_added = 0
        for t in tlist if isinstance(tlist, list) else []:
            date = t.get("date")
            code = t.get("code")
            side = t.get("side")
            price = _float(t.get("price"))
            if not date or not code or not side:
                continue
            existing = conn.execute(
                """SELECT COUNT(*) FROM bt_sim_trades
                   WHERE date = ? AND code = ? AND side = ? AND ABS(price - ?) < 0.001""",
                (date, code, side, price if price is not None else 0),
            ).fetchone()[0]
            if existing > 0:
                continue
            conn.execute(
                """INSERT INTO bt_sim_trades(date,code,side,price,shares,pnl,reason)
                VALUES (?,?,?,?,?,?,?)""",
                (
                    t.get("date"), t.get("code"), t.get("side"),
                    _float(t.get("price")), _int(t.get("shares")), _float(t.get("pnl")),
                    t.get("reason"),
                ),
            )
            trades_added += 1
        logger.info("  [✓] sim_trades → %d 条（去重后）", trades_added)

    conn.commit()


def import_bs_backtests(conn):
    """导入 simulated_trading/backtest_*.json 历史回测"""
    sim_dir = os.path.join(PROJECT, "simulated_trading")
    if not os.path.isdir(sim_dir):
        return

    def _strat_name(s):
        n = s.get("name") or s.get("params")
        if not n:
            logger.warning("策略缺少 name 和 params 字段, 使用 fallback 'unknown'")
            return "unknown"
        return str(n)

    # backtest_bs_summary.json (多策略对比)
    bsf = os.path.join(sim_dir, "backtest_bs_summary.json")
    if os.path.exists(bsf):
        with open(bsf) as f:
            summaries = json.load(f)
        if isinstance(summaries, list):
            conn.execute(
                "INSERT INTO bt_backtest_runs(run_name,source,notes) VALUES (?,?,?)",
                ("BS策略回测对比", "simulated", "backtest_bs_summary.json"),
            )
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            for s in summaries:
                conn.execute(
                    """INSERT INTO bt_strategy_comparisons(run_id,strategy,ann_ret,sharpe,
                    max_dd,win_rate,total_ret,trade_count,final_equity,n_stocks,json_data)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id, _strat_name(s),
                        s.get("ann_ret"), s.get("sharpe"),
                        s.get("max_dd"), s.get("win_rate"),
                        s.get("total_ret"), s.get("n_buy_trades") or s.get("n_sell_trades"),
                        s.get("final_equity"), s.get("n_stocks"),
                        json.dumps(s, ensure_ascii=False),
                    ),
                )
            logger.info("  [✓] backtest_bs_summary.json → %d 策略", len(summaries))

    # backtest_full_summary.json
    bfs = os.path.join(sim_dir, "backtest_full_summary.json")
    if os.path.exists(bfs):
        with open(bfs) as f:
            summaries = json.load(f)
        if isinstance(summaries, list):
            conn.execute(
                "INSERT INTO bt_backtest_runs(run_name,source,notes) VALUES (?,?,?)",
                ("全量回测总结", "simulated", "backtest_full_summary.json"),
            )
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            for s in summaries:
                conn.execute(
                    """INSERT INTO bt_strategy_comparisons(run_id,strategy,ann_ret,sharpe,
                    max_dd,win_rate,total_ret,trade_count,final_equity,n_stocks,json_data)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id, _strat_name(s),
                        s.get("ann_ret"), s.get("sharpe"),
                        s.get("max_dd"), s.get("win_rate"),
                        s.get("total_ret"), s.get("n_buy_trades") or s.get("n_sell_trades"),
                        s.get("final_equity"), s.get("n_stocks"),
                        json.dumps(s, ensure_ascii=False),
                    ),
                )
            logger.info("  [✓] backtest_full_summary.json → %d 策略", len(summaries))

    # backtest_screener_result.json
    srf = os.path.join(sim_dir, "backtest_screener_result.json")
    if os.path.exists(srf):
        with open(srf) as f:
            d = json.load(f)
        sm = d.get("summary", {})
        if sm:
            conn.execute(
                "INSERT INTO bt_backtest_runs(run_name,source,notes) VALUES (?,?,?)",
                ("Screener 选股回测", "simulated", "backtest_screener_result.json"),
            )
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                """INSERT INTO bt_metrics_summary(run_id,total_return,ann_return,win_rate,
                trade_count,profit_ratio,max_drawdown,sharpe_ratio,backtest_days)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, sm.get("total_return_pct"), sm.get("ann_return_pct"),
                    sm.get("win_rate_pct"), sm.get("total_trades"),
                    sm.get("profit_factor"), sm.get("max_drawdown_pct"),
                    sm.get("sharpe_ratio"), None,
                ),
            )
            for t in d.get("trades", []):
                conn.execute(
                    """INSERT INTO bt_trades(run_id,code,entry_date,entry_price,
                    exit_date,exit_price,return_pct,hold_days)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        run_id, t.get("code"), t.get("entry_date"),
                        _float(t.get("entry_price")), t.get("exit_date"),
                        _float(t.get("exit_price")), _float(t.get("return_pct")),
                        _int(t.get("hold_days")),
                    ),
                )
            for eq in d.get("daily_equity", []):
                conn.execute(
                    "INSERT INTO bt_equity_daily(run_id,date,equity) VALUES (?,?,?)",
                    (run_id, eq.get("date"), _float(eq.get("equity"))),
                )
            logger.info(
                "  [✓] backtest_screener_result.json → trades=%d equity=%d",
                len(d.get("trades", [])), len(d.get("daily_equity", [])),
            )

    # backtest_result.json
    brf = os.path.join(sim_dir, "backtest_result.json")
    if os.path.exists(brf):
        with open(brf) as f:
            d = json.load(f)
        sm = d.get("summary", {})
        if sm:
            conn.execute(
                "INSERT INTO bt_backtest_runs(run_name,source,notes) VALUES (?,?,?)",
                ("早期回测结果", "simulated", "backtest_result.json"),
            )
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                """INSERT INTO bt_metrics_summary(run_id,total_return,ann_return,win_rate,
                trade_count,profit_ratio,avg_win_pct,avg_loss_pct,max_drawdown,
                sharpe_ratio) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, sm.get("total_return_pct"), sm.get("ann_return_pct"),
                    sm.get("win_rate_pct"), sm.get("total_trades"),
                    sm.get("profit_factor"), sm.get("avg_win_pnl"),
                    sm.get("avg_loss_pnl"), sm.get("max_drawdown_pct"),
                    sm.get("sharpe_ratio"),
                ),
            )
            for t in d.get("trades", []):
                conn.execute(
                    """INSERT INTO bt_trades(run_id,code,entry_date,entry_price,
                    exit_date,exit_price,return_pct,hold_days)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        run_id, t.get("code"), t.get("entry_date"),
                        _float(t.get("entry_price")), t.get("exit_date"),
                        _float(t.get("exit_price")), _float(t.get("return_pct")),
                        _int(t.get("hold_days")),
                    ),
                )
            for eq in d.get("monthly_equity", []):
                conn.execute(
                    "INSERT INTO bt_equity_daily(run_id,date,equity) VALUES (?,?,?)",
                    (run_id, eq.get("date"), _float(eq.get("equity"))),
                )
            logger.info("  [✓] backtest_result.json → trades=%d", len(d.get("trades", [])))

    # backtest_llm_v6_result.json
    lf = os.path.join(sim_dir, "backtest_llm_v6_result.json")
    if os.path.exists(lf):
        with open(lf) as f:
            d = json.load(f)
        conn.execute(
            "INSERT INTO bt_backtest_runs(run_name,source,notes) VALUES (?,?,?)",
            ("LLM V6 选股回测", "simulated", "backtest_llm_v6_result.json (pure_tech vs llm_v6)"),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for strat_name, data in d.items():
            if isinstance(data, dict) and "summary" in data:
                sm = data["summary"]
                conn.execute(
                    """INSERT INTO bt_backtest_reports(run_id,strategy,total_ret,
                    win_rate,total_trades,json_data) VALUES (?,?,?,?,?,?)""",
                    (
                        run_id, strat_name,
                        sm.get("total_return_pct"), sm.get("win_rate_pct"),
                        sm.get("closed_trades"),
                        json.dumps(data, ensure_ascii=False),
                    ),
                )
        logger.info("  [✓] backtest_llm_v6_result.json → %s", list(d.keys()))

    # backtest_jq_result.json
    jf = os.path.join(sim_dir, "backtest_jq_result.json")
    if os.path.exists(jf):
        with open(jf) as f:
            d = json.load(f)
        sm = d if isinstance(d, dict) and "total_return_pct" in d else d.get("summary", {})
        if sm:
            conn.execute(
                "INSERT INTO bt_backtest_runs(run_name,source,notes) VALUES (?,?,?)",
                ("聚宽回测结果", "simulated", "backtest_jq_result.json"),
            )
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                """INSERT INTO bt_metrics_summary(run_id,total_return,ann_return,win_rate,
                trade_count,profit_ratio,max_drawdown,sharpe_ratio)
                VALUES (?,?,?,?,?,?,?,?)""",
                (
                    run_id, sm.get("total_return_pct"), sm.get("ann_return_pct"),
                    sm.get("win_rate_pct"), sm.get("total_trades") or sm.get("closed_trades"),
                    sm.get("profit_factor"), sm.get("max_drawdown_pct"),
                    sm.get("sharpe_ratio"),
                ),
            )
            logger.info("  [✓] backtest_jq_result.json")

    # BS 各策略 equity/trades
    for prefix in [
        "backtest_bs_保守型", "backtest_bs_精选型", "backtest_bs_当前默认",
        "backtest_full_当前默认",
    ]:
        eq_f = os.path.join(sim_dir, f"{prefix}_eq.json")
        tr_f = os.path.join(sim_dir, f"{prefix}_trades.json")
        if os.path.exists(eq_f) and os.path.exists(tr_f):
            strategy_name = prefix.replace("backtest_", "").replace("_", " ").strip()
            conn.execute(
                "INSERT INTO bt_backtest_runs(run_name,source,notes) VALUES (?,?,?)",
                (f"BS {strategy_name}", "simulated", f"{prefix}_eq + trades"),
            )
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            with open(eq_f) as f:
                eq_data = json.load(f)
            for eq in eq_data if isinstance(eq_data, list) else eq_data.values():
                if isinstance(eq, dict) and "date" in eq:
                    conn.execute(
                        """INSERT INTO bt_equity_daily(run_id,date,equity,return_pct,max_dd_pct)
                        VALUES (?,?,?,?,?)""",
                        (
                            run_id, eq.get("date"), _float(eq.get("equity")),
                            _float(eq.get("return_pct")), _float(eq.get("max_dd")),
                        ),
                    )
            with open(tr_f) as f:
                tr_data = json.load(f)
            for t in tr_data if isinstance(tr_data, list) else tr_data.get("trades", []):
                if isinstance(t, dict) and "code" in t:
                    conn.execute(
                        """INSERT INTO bt_trades(run_id,code,entry_date,entry_price,
                        exit_date,exit_price,return_pct,hold_days)
                        VALUES (?,?,?,?,?,?,?,?)""",
                        (
                            run_id, t.get("code"),
                            t.get("entry_date") or t.get("buy_date"),
                            _float(t.get("entry_price") or t.get("buy_price")),
                            t.get("exit_date") or t.get("sell_date"),
                            _float(t.get("exit_price") or t.get("sell_price")),
                            _float(t.get("return_pct")),
                            _int(t.get("hold_days")),
                        ),
                    )
            logger.info("  [✓] %s → equity + trades", prefix)

    conn.commit()


def import_reports(conn):
    """导入 /opt/daily_stock_analysis/reports/ 下的回测报告"""
    rep_dir = os.path.join(PROJECT, "reports")
    if not os.path.isdir(rep_dir):
        return

    for fname in sorted(os.listdir(rep_dir)):
        if not fname.endswith(".json") or not fname.startswith("backtest_"):
            continue
        fpath = os.path.join(rep_dir, fname)
        try:
            with open(fpath) as f:
                d = json.load(f)
        except Exception:
            continue

        run_date = d.get("run_date", "")
        stock_cnt = d.get("stock_count")
        strategies = d.get("strategies", [])

        conn.execute(
            "INSERT INTO bt_backtest_runs(run_name,source,run_date,stock_count,notes) VALUES (?,?,?,?,?)",
            (fname, "report", run_date, stock_cnt, f"从 {fname} 导入"),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        for s in strategies:
            conn.execute(
                """INSERT INTO bt_backtest_reports(run_id,strategy,ann_ret,ann_vol,sharpe,
                calmar,max_dd,win_rate,profit_factor,total_ret,total_trades,
                avg_hold,json_data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, s.get("name"), _float(s.get("ann_ret")),
                    _float(s.get("ann_vol")), _float(s.get("sharpe")),
                    _float(s.get("calmar")), _float(s.get("max_dd")),
                    _float(s.get("win_rate")), _float(s.get("profit_factor")),
                    _float(s.get("total_ret")), _int(s.get("total_trades")),
                    _float(s.get("avg_hold")),
                    json.dumps(s, ensure_ascii=False),
                ),
            )
        logger.info("  [✓] %s → %d 策略, stock_count=%s", fname, len(strategies), stock_cnt)

    conn.commit()


def _float(v):
    if v is None or v == "" or v == "nan":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def summary(conn):
    logger.info("=" * 60)
    logger.info("📊 数据库汇总")
    logger.info("=" * 60)
    bt_tables = [
        "bt_backtest_runs", "bt_trades", "bt_monthly_stats", "bt_quarterly_stats",
        "bt_metrics_summary", "bt_equity_daily", "bt_strategy_comparisons",
        "bt_sim_state", "bt_sim_positions", "bt_sim_equity_daily", "bt_sim_trades",
        "bt_sim_weekly", "bt_backtest_reports",
    ]
    for table in bt_tables:
        cnt = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        logger.info("  %s: %d 条记录", table, cnt)
    db_size = os.path.getsize(DB_PATH)
    logger.info("  数据库文件: %s", DB_PATH)
    logger.info("  文件大小: %.1f KB", db_size / 1024)


def main():
    logger.info("🚀 开始迁移回测数据到 SQLite...\n")
    import sqlite3
    conn = get_conn()

    # 幂等性检查
    if check_idempotent(conn):
        conn.close()
        return

    create_schema(conn)
    import_workspace_results(conn)
    import_simulated_trading(conn)
    import_bs_backtests(conn)
    import_reports(conn)

    # 标记迁移完成
    mark_migrated(conn)

    summary(conn)
    conn.close()
    logger.info("\n✅ 迁移完成!")


if __name__ == "__main__":
    main()
