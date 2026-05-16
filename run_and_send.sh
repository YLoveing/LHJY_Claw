#!/bin/bash
# ===========================================
# run_and_send.sh — 股票智能分析流水线
# 每天 09:25 / 11:30 / 18:00 由 cron 触发
# 09:25=早盘(选股+信号执行+全量), 11:30=午盘(精简快照), 18:00=收盘(完整+量化)
# v3: 14:30→11:30, 分时段报告文件名
# ===========================================

# ── 错误捕获 ──
# 不设 set -e，手动捕获每一步的退出码
cd /opt/daily_stock_analysis
RUN_LOG="/tmp/stock_analysis_cron.log"
DETAIL_LOG="/tmp/stock_analysis_error_detail.txt"

# 加载 .env
set -a; source .env 2>/dev/null; set +a

RUN_DATE=$(date +%Y%m%d)
RUN_HMS=$(date +%H%M)
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

# ═══ 时段分支逻辑 ═══

# ── Step 0: 选股器扫描（09:25 选当日候选 + 18:00 选次日候选）──
if [ "$HOUR" = "09" ] || [ "$HOUR" = "18" ]; then
    if [ -n "$MX_APIKEY" ]; then
        run_step "妙想选股" python3 run_screener.py --max=5 --mx
    else
        run_step "选股器" python3 run_screener.py --max=5
    fi
fi

# ── Step 0.5: 执行昨日待执信号（仅09:25）──
# A股T+1制度下，盘中检测到的建仓信号记录到待执行队列，次日开盘执行
if [ "$HOUR" = "09" ]; then
    run_step "信号执行" python3 scripts/intraday_trading.py execute
fi

# ── Step 0.8: 妙想财务预取（09:25 和 11:30）──
if [ "$HOUR" = "09" ] || [ "$HOUR" = "11" ]; then
    if [ -n "$MX_APIKEY" ] && [ -n "$CANDIDATE_LIST" ]; then
        run_step "妙想财务预取" python3 scripts/mx_enrich.py --codes="${CANDIDATE_LIST}"
    fi
fi

# ── Step 1: 全量分析（09:25/18:00 完整, 11:30 精简）──
if [ "$HOUR" = "11" ]; then
    # 午盘精简：只用 MX 快照数据快速跑 main.py
    run_step "午盘快照" python3 main.py --force-run
else
    run_step "全量分析" python3 main.py --force-run
fi

# ── Step 2: 模拟交易（仅早盘+收盘跑，午盘数据不完整跳过）──
if [ "$HOUR" != "11" ]; then
    run_step "模拟交易" python3 simulated_trading.py
fi

# ── Step 3: 盘后量化引擎（18:00 仅） ──
if [ "$HOUR" = "18" ]; then
    run_step "量化引擎" python3 -m quant_engine.run_quant
    run_step "情绪引擎" python3 -m sentiment_engine.sentiment_index
fi

# ── Step 4: 推送 ──

# 时段报告文件名（避免一天被覆盖3次）
case $HOUR in
    09) PERIOD="早盘"; PERIOD_TAG="morning"; SCREENER_PUSH=true ;;
    11) PERIOD="午盘"; PERIOD_TAG="noon";    SCREENER_PUSH=false ;;
    18) PERIOD="收盘"; PERIOD_TAG="night";   SCREENER_PUSH=false ;;
    *)  PERIOD="盘中"; PERIOD_TAG="intra";   SCREENER_PUSH=false ;;
esac

REPORT_FILE_PERIOD="reports/report_${RUN_DATE}_${PERIOD_TAG}.md"

# 优先读分时段报告，没有则回退到当日通用报告
if [ -f "$REPORT_FILE_PERIOD" ]; then
    REPORT_FILE="$REPORT_FILE_PERIOD"
elif [ ! -f "$REPORT_FILE" ]; then
    for i in 1 2 3; do
        PAST_DATE=$(date -d "-${i} day" +%Y%m%d 2>/dev/null || date -v-${i}d +%Y%m%d)
        [ -f "reports/report_${PAST_DATE}.md" ] && REPORT_FILE="reports/report_${PAST_DATE}.md" && break
    done
fi

_push() {
    local content="$1"
    [ -z "$content" ] && return
    [ -n "$QQ_TARGET" ] && openclaw message send --channel qqbot --target "$QQ_TARGET" --message "$content" 2>/dev/null || true
    [ -n "$WX_TARGET" ] && openclaw message send --channel openclaw-weixin --target "$WX_TARGET" --account "$WX_ACCOUNT" --message "$content" 2>/dev/null || true
}

_push "📊【${PERIOD}分析】${RUN_DATE} ⏰ 分析完成"

# 选股器候选推送（仅早盘）
if [ -f "$SCREENER_TOP5" ] && [ "$SCREENER_PUSH" = true ]; then
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

# 大盘复盘（仅收盘档有完整复盘数据）
if [ "$HOUR" = "18" ] && [ -f "$MARKET_REVIEW_FILE" ]; then
    REVIEW_FULL=$(sed -n '/一、盘面总览/,/七、风险提示/p' "$MARKET_REVIEW_FILE" 2>/dev/null)
    _push "📈【大盘复盘】${REVIEW_FULL}"
fi

# 午盘轻量简报（不推大盘复盘，只推核心要点）
if [ "$HOUR" = "11" ]; then
    _push "📌【午间简报】上午行情过半，关注下午变盘信号"
fi

# 模拟交易快照（仅早盘/收盘跑过模拟交易才推）
if [ "$HOUR" != "11" ] && [ -f "$SIMULATED_SUMMARY" ]; then
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
