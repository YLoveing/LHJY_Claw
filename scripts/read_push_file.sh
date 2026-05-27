#!/bin/bash
# ===========================================
# read_push_file.sh — 读取指定时段的推送缓存内容
# 供 OpenClaw cron 代理读取元宝推送内容
#
# 用法: ./read_push_file.sh <morning|noon|night>
# 输出: 推送内容（无内容则返回空）
# ===========================================
set -e

PERIOD_TAG="$1"
if [ -z "$PERIOD_TAG" ]; then
    echo "❌ 用法: $0 <morning|noon|night>"
    exit 1
fi

TODAY=$(date +%Y%m%d)
PUSH_FILE="/opt/daily_stock_analysis/push_output/push_${TODAY}_${PERIOD_TAG}.txt"

if [ ! -f "$PUSH_FILE" ]; then
    exit 0  # 文件不存在，静默退出
fi

CONTENT=$(cat "$PUSH_FILE" 2>/dev/null)
if [ -z "$CONTENT" ]; then
    exit 0
fi

echo "$CONTENT"
