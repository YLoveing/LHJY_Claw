#!/bin/bash
# ===========================================
# run_and_send.sh — 股票智能分析流水线
# 每天 9:25 / 13:00 / 18:00 由 cron 触发
# _v2: 加入错误告警 + set -e 前捕获
# ===========================================

# ── 错误捕获 ──
# 不设 set -e，手动捕获每一步的退出码
cd /opt/daily_stock_analysis
RUN_LOG="/tmp/stock_analysis_cron.log"
DETAIL_LOG="/tmp/stock_analysis_error_detail.txt"

# 加载 .env
set -a; source .env 2>/dev/null; set +a

RUN_DATE=$(date +%Y%m%d)
REPORT_FILE="reports/report_${RUN_DATE}.md"
MARKET_REVIEW_FILE="reports/market_review_${RUN_DATE}.md"
SIMULATED_SUMMARY="simulated_trading/summary_${RUN_DATE}.txt"
SIMULATED_WEEKLY="simulated_trading/weekly_${RUN_DATE}.txt"
SCREENER_TOP5="screener/top5_${RUN_DATE}.txt"
SCREENER_JSON="screener/candidates_${RUN_DATE}.json"
HOUR=$(date +%H)

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$RUN_LOG"; }
run_step() {
    local step_name="$1"; shift
    "$@" 2>&1 | tee -a "$RUN_LOG"
    local rc=${PIPESTATUS[0]}
    if [ "$rc" -ne 0 ]; then
        log "❌ 步骤失败: $step_name (exit=$rc)"
        tail -30 "$RUN_LOG" > "$DETAIL_LOG"
        python3 scripts/alert.py "$step_name" "$rc" "$DETAIL_LOG"
        return "$rc"
    fi
    return 0
}

log "═══════════════════════════════════════════"
log "📊 股票分析流水线启动 — ${RUN_DATE} (${HOUR}:00)"

# ── 候选股注入准备 ──
CANDIDATE_LIST="002410,510300"
if [ -f "$SCREENER_JSON" ]; then
    CANDIDATE_CODES=$(python3 -c "
import json
with open('$SCREENER_JSON') as f:
    data = json.load(f)
codes = [s['code'] for s in data[:5]]
print(','.join(codes))
" 2>/dev/null)
    if [ -n "$CANDIDATE_CODES" ]; then
        CANDIDATE_LIST="${CANDIDATE_CODES},002410,510300"
        log "候选股注入: ${CANDIDATE_CODES}"
    fi
fi
export STOCK_LIST="$CANDIDATE_LIST"

# ── Step 0: 选股器扫描 ──
if [ "$HOUR" = "09" ] || [ "$HOUR" = "18" ]; then
    run_step "选股器" python3 run_screener.py --max=5
fi

# ── Step 1: 全量分析（Docker） ──
run_step "全量分析" docker compose run --rm \
    -e STOCK_LIST="$CANDIDATE_LIST" \
    analyzer python main.py --force-run

# ── Step 2: 模拟交易 ──
run_step "模拟交易" python3 simulated_trading.py

# ── Step 3: 盘后量化引擎（18:00 仅） ──
if [ "$HOUR" = "18" ]; then
    run_step "量化引擎" python3 -m quant_engine.run_quant
    run_step "情绪引擎" python3 -m sentiment_engine.sentiment_index
fi

# ── Step 4: 推送 ──

# 找报告
if [ ! -f "$REPORT_FILE" ]; then
    for i in 1 2 3; do
        PAST_DATE=$(date -d "-${i} day" +%Y%m%d 2>/dev/null || date -v-${i}d +%Y%m%d)
        [ -f "reports/report_${PAST_DATE}.md" ] && REPORT_FILE="reports/report_${PAST_DATE}.md" && break
    done
fi

case $HOUR in
    09) PERIOD="早盘" ;;
    13) PERIOD="午盘" ;;
    18) PERIOD="收盘" ;;
    *)  PERIOD="盘中" ;;
esac

_push() {
    local content="$1"
    [ -z "$content" ] && return
    [ -n "$QQ_TARGET" ] && openclaw message send --channel qqbot --target "$QQ_TARGET" --message "$content" 2>/dev/null || true
    [ -n "$WX_TARGET" ] && openclaw message send --channel openclaw-weixin --target "$WX_TARGET" --account "$WX_ACCOUNT" --message "$content" 2>/dev/null || true
}

_push "📊【${PERIOD}分析】${RUN_DATE} ⏰ 分析完成"

# 选股器候选推送
if [ -f "$SCREENER_TOP5" ] && { [ "$HOUR" = "09" ] || [ "$HOUR" = "13" ]; }; then
    _push "$(cat "$SCREENER_TOP5")"
fi

# 个股分析摘要
if [ -f "$REPORT_FILE" ]; then
    REPORT_SUMMARY=$(sed -n '/核心结论/,/报告生成时间/p' "$REPORT_FILE" 2>/dev/null | head -30)
    QUOTE_INFO=$(sed -n '/当日行情/,/数据透视/p' "$REPORT_FILE" 2>/dev/null | head -10)
    _push "📊【${PERIOD}分析】${RUN_DATE}

${REPORT_SUMMARY}

${QUOTE_INFO}"
fi

# 大盘复盘
if [ -f "$MARKET_REVIEW_FILE" ]; then
    REVIEW_FULL=$(sed -n '/一、盘面总览/,/七、风险提示/p' "$MARKET_REVIEW_FILE" 2>/dev/null)
    _push "📈【大盘复盘】${REVIEW_FULL}"
fi

# 模拟交易快照
if [ -f "$SIMULATED_SUMMARY" ]; then
    _push "$(cat "$SIMULATED_SUMMARY")"
fi

# 周五周报
if [ -f "$SIMULATED_WEEKLY" ]; then
    _push "$(cat "$SIMULATED_WEEKLY")"
fi

# 盘后：情绪指数 + 信号统计
if [ "$HOUR" = "18" ]; then
    SENTIMENT_FILE="sentiment_engine/sentiment_${RUN_DATE}.txt"
    [ -f "$SENTIMENT_FILE" ] && _push "$(cat "$SENTIMENT_FILE")"

    VERIFY_RESULT=$(python3 -c "
from simulated_trading import verify_signal_accuracy
r = verify_signal_accuracy()
if r:
    print(f'🎯【信号准确率统计】')
    print(f'看多信号(评分≥70): {r[\"buy_signals\"]}次')
    print(f'看空信号(评分≤30): {r[\"sell_signals\"]}次')
" 2>/dev/null)
    [ -n "$VERIFY_RESULT" ] && _push "$VERIFY_RESULT"
fi

log "✅ 流水线完成"
