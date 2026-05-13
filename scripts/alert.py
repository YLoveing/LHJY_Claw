#!/usr/bin/env python3
"""
脚本执行状态告警 — 跑崩了立即通知 QQ。

用法:
  python3 scripts/alert.py <阶段名称> <退出码> [错误详情文件路径]

如果退出码 != 0，自动推送告警到 QQ（配置在 .env 中）。
"""
import json
import os
import subprocess
import sys
from datetime import datetime


def load_env(path: str = "/opt/daily_stock_analysis/.env") -> dict:
    """简易 .env 加载。"""
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    except Exception:
        pass
    return env


def get_error_detail(detail_path: str = None) -> str:
    """从最近日志中提取错误信息。"""
    parts = []
    
    # 1. 指定错误文件
    if detail_path and os.path.exists(detail_path):
        try:
            with open(detail_path) as f:
                content = f.read().strip()
                if content:
                    parts.append(content[-500:])  # 只取尾500字符
        except Exception:
            pass
    
    # 2. cron 日志尾20行
    log_path = "/tmp/stock_analysis_cron.log"
    if os.path.exists(log_path):
        try:
            result = subprocess.run(
                ["tail", "-20", log_path],
                capture_output=True, text=True, timeout=5
            )
            if result.stdout.strip():
                parts.append("--- 最近日志 ---")
                parts.append(result.stdout.strip())
        except Exception:
            pass
    
    return "\n".join(parts) if parts else "（无详细错误信息）"


def send_alert(stage: str, exit_code: int, detail: str = ""):
    """发送告警到 QQ。"""
    env = load_env()
    
    now = datetime.now().strftime("%H:%M:%S")
    date_str = datetime.now().strftime("%Y-%m-%d")
    
    title = "❌" if exit_code != 0 else "⚠️"
    msg = f"""{title}【分析异常告警】{date_str}

阶段: {stage}
状态: {'失败' if exit_code != 0 else f'退出码={exit_code}'}
时间: {now}

{detail[:600]}

🛠 请登录服务器排查: /opt/daily_stock_analysis
"""

    # 👇 允许在 .env 中配置多个通知渠道
    channels = env.get("ALERT_CHANNELS", "qqbot").split(",")
    qq_target = env.get("QQ_TARGET", "")
    wx_target = env.get("WX_TARGET", "")
    wx_account = env.get("WX_ACCOUNT", "")

    for ch in channels:
        ch = ch.strip()
        try:
            if ch == "qqbot" and qq_target:
                subprocess.run(
                    ["openclaw", "message", "send", "--channel", "qqbot",
                     "--target", qq_target, "--message", msg],
                    capture_output=True, timeout=10,
                )
            elif ch == "weixin" and wx_target and wx_account:
                subprocess.run(
                    ["openclaw", "message", "send", "--channel", "openclaw-weixin",
                     "--target", wx_target, "--account", wx_account,
                     "--message", msg],
                    capture_output=True, timeout=10,
                )
        except Exception as e:
            print(f"[alert] 通知失败 ({ch}): {e}", file=sys.stderr)


def main():
    if len(sys.argv) < 3:
        print(f"用法: {sys.argv[0]} <阶段名称> <退出码> [错误详情文件]")
        sys.exit(1)
    
    stage = sys.argv[1]
    try:
        exit_code = int(sys.argv[2])
    except ValueError:
        exit_code = 1
    
    detail_path = sys.argv[3] if len(sys.argv) > 3 else None
    
    if exit_code != 0:
        detail = get_error_detail(detail_path)
        send_alert(stage, exit_code, detail)
        print(f"[alert] 已发送 {stage} 失败告警 (exit={exit_code})")
    else:
        # 可选：成功也可以通知（比如关键阶段）
        if os.environ.get("ALERT_ON_SUCCESS"):
            send_alert(stage, exit_code, "✅ 执行成功")
    
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
