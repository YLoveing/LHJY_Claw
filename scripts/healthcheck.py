#!/usr/bin/env python3
"""
系统健康检查 — 检测各模块是否正常运转

用法：
  python3 scripts/healthcheck.py                   # 完整检查
  python3 scripts/healthcheck.py --quick           # 快速检查（适合cron）

退出码：
  0 = 正常
  1 = 有告警
  2 = 有严重问题
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 交易日历
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.trading_calendar import is_trading_day

BASE_DIR = Path("/opt/daily_stock_analysis")
LOG_DIR = Path("/tmp")
HEALTH_LOG = LOG_DIR / "healthcheck_result.json"

WARN = "⚠️"
CRIT = "🔴"
OK = "✅"


def check_cron_logs():
    """检查cron日志是否有最近执行记录"""
    issues = []
    now = datetime.now()

    checks = [
        ("早盘分析(09:25)", "/tmp/stock_analysis_cron.log", "09:.*:00"),
        ("尾盘分析(14:30)", "/tmp/stock_analysis_cron.log", "14:.*:00"),
        ("收盘分析(18:00)", "/tmp/stock_analysis_cron.log", "18:.*:00"),
        ("盘中监控(最近10min)", "/tmp/monitor_cron.log", None),
    ]

    for name, log_path, pattern in checks:
        log_file = Path(log_path)
        if not log_file.exists():
            issues.append((CRIT, f"{name}: 日志文件不存在"))
            continue

        mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
        hours_since = (now - mtime).total_seconds() / 3600

        # 盘中监控应在最近30分钟内更新过
        if "监控" in name:
            if hours_since > 0.5:
                issues.append((WARN, f"{name}: 最后一次更新在{hours_since:.1f}小时前"))
        # 其他日志应在最近48小时更新过
        elif hours_since > 48:
            issues.append((CRIT, f"{name}: 超过48小时未更新"))

    return issues


def check_state_file():
    """检查模拟交易状态文件"""
    issues = []
    state_file = BASE_DIR / "simulated_trading" / "state.json"

    if not state_file.exists():
        issues.append((CRIT, "模拟交易状态文件不存在"))
        return issues

    try:
        state = json.loads(state_file.read_text())
        cash = state.get("cash", 0)
        equity = state.get("total_equity", 0)
        positions = len(state.get("positions", {}))

        if positions > 3:
            issues.append((WARN, f"持仓数量 {positions} > 建议上限 3"))

        # 权益大幅下跌告警
        peak = state.get("peak_equity", equity)
        if peak > 0 and equity > 0:
            dd = (equity - peak) / peak * 100
            if dd <= -15:
                issues.append((WARN, f"账户回撤 {dd:.1f}%（接近熔断线-20%）"))
            elif dd <= -20:
                issues.append((CRIT, f"账户回撤 {dd:.1f}%（已触发熔断）"))

        print(f"  {OK} 状态文件: 现金¥{cash:,.0f} 权益¥{equity:,.0f} 持仓{positions}只")

    except Exception as e:
        issues.append((CRIT, f"状态文件读取出错: {e}"))

    return issues


def check_pending_signals():
    """检查待执行队列是否堆积"""
    issues = []
    pending_file = BASE_DIR / "data" / "pending_signals.json"

    if not pending_file.exists():
        return issues

    try:
        pending = json.loads(pending_file.read_text())
        active = [s for s in pending if s.get("status") == "pending"]
        if len(active) > 5:
            issues.append((WARN, f"待执行信号队列堆积 {len(active)} 条（未及时清理）"))
        elif active:
            print(f"  {OK} 待执行信号 {len(active)} 条")
    except Exception:
        pass

    return issues


def check_disk_space():
    """检查磁盘空间"""
    import shutil
    usage = shutil.disk_usage(BASE_DIR)
    pct = usage.used / usage.total * 100
    if pct > 90:
        return [(CRIT, f"磁盘使用率 {pct:.0f}%（>90%）")]
    elif pct > 80:
        return [(WARN, f"磁盘使用率 {pct:.0f}%（>80%）")]
    return []


def main():
    quick = "--quick" in sys.argv

    print(f"🔍 系统健康检查 — {datetime.now():%Y-%m-%d %H:%M}")
    print()

    all_issues = []

    # 非交易日的处理：跳过全部实质性检查，避免长假期误报cron超时
    is_trading = is_trading_day()

    if not is_trading:
        print("📅 非交易日，跳过实质性检查")
        print(f"{OK} 正常（非交易日）")

    else:
        if not quick:
            print("📋 日志检查：")
            all_issues.extend(check_cron_logs())

        print("📋 状态检查：")
        all_issues.extend(check_state_file())
        all_issues.extend(check_pending_signals())

        print("📋 磁盘检查：")
        all_issues.extend(check_disk_space())

        print()

        if all_issues:
            print(f"⚠️ 发现 {len(all_issues)} 个问题：")
            for level, msg in all_issues:
                print(f"  {level} {msg}")
            exit_code = 2 if any(l == CRIT for l, _ in all_issues) else 1
        else:
            print(f"{OK} 全部正常")
        exit_code = 0

    # 保存结果
    HEALTH_LOG.write_text(json.dumps({
        "time": datetime.now().isoformat(),
        "issues": [{"level": l, "msg": m} for l, m in all_issues],
        "exit_code": exit_code,
    }, ensure_ascii=False, indent=2))

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
