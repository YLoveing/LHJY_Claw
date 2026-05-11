#!/bin/bash
# ===========================================
# 股票智能分析 - 运行并推送到 QQ + 微信
# 每天 9:25 / 13:00 / 18:00 由 cron 触发
# ===========================================

set -e

cd /opt/daily_stock_analysis

RUN_DATE=$(date +%Y%m%d)
REPORT_FILE="reports/report_${RUN_DATE}.md"
MARKET_REVIEW_FILE="reports/market_review_${RUN_DATE}.md"
QQ_TARGET="qqbot:c2c:7D15BBF664045E2DD5F33DA4BE0A00E9"
WX_TARGET="o9cq800-zOjMI1JH4SjoT0NocAZI@im.wechat"
WX_ACCOUNT="a463c42ba2be-im-bot"
SIMULATED_SUMMARY="simulated_trading/summary_${RUN_DATE}.txt"
SIMULATED_WEEKLY="simulated_trading/weekly_${RUN_DATE}.txt"
SCREENER_TOP5="screener/top5_${RUN_DATE}.txt"
SCREENER_JSON="screener/candidates_${RUN_DATE}.json"

HOUR=$(date +%H)

# ═══ 0. 选股器扫描（9:25 和 18:00 可选股）═══
if [ "$HOUR" = "09" ] || [ "$HOUR" = "18" ]; then
    echo "[$(date '+%H:%M')] 选股器扫描..."
    python3 run_screener.py --max=15 2>&1 | tee -a /tmp/stock_analysis_cron.log
fi

# ═══ 1. 从候选股提取代码列表，注入分析引擎 ═══
# 把候选股 + 已有持仓股合并，让 main.py 分析并让模拟交易执行
# 注意：main.py(refresh_stock_list)直接从.env文件读取STOCK_LIST，环境变量覆盖无效
# 因此需要临时覆写.env文件
CANDIDATE_LIST="002410,510300"  # 默认自选股兜底
if [ -f "$SCREENER_JSON" ]; then
    export SCREENER_JSON_PATH="$SCREENER_JSON"
    CANDIDATE_CODES=$(python3 -c "
import json, os
path = os.environ.get('SCREENER_JSON_PATH', '')
if path:
    with open(path) as f:
        data = json.load(f)
    codes = [s['code'] for s in data[:15]]
    print(','.join(codes))
" 2>/dev/null)
    if [ -n "$CANDIDATE_CODES" ]; then
        CANDIDATE_LIST="${CANDIDATE_CODES},002410,510300"
        echo "[$(date '+%H:%M')] 候选股注入: ${CANDIDATE_CODES},002410,510300"
    fi
fi

# ═══ 2. 临时覆写.env的STOCK_LIST，运行分析 ═══
# 备份原STOCK_LIST行，临时写入候选股列表，分析完恢复
ORIG_ENV_LINE=$(grep '^STOCK_LIST=' .env 2>/dev/null || true)
if [ -f ".env" ]; then
    if grep -q '^STOCK_LIST=' .env; then
        sed -i "s/^STOCK_LIST=.*/STOCK_LIST=${CANDIDATE_LIST}/" .env
    else
        echo "STOCK_LIST=${CANDIDATE_LIST}" >> .env
    fi
fi
export STOCK_LIST="$CANDIDATE_LIST"

echo "[$(date '+%H:%M')] 开始分析 (${CANDIDATE_LIST})..."
docker compose run --rm analyzer python main.py --force-run 2>&1 | tee -a /tmp/stock_analysis_cron.log

# ═══ 2b. docker运行完后恢复.env ═══
if [ -f ".env" ] && [ -n "$ORIG_ENV_LINE" ]; then
    sed -i "s/^STOCK_LIST=.*/${ORIG_ENV_LINE//\//\\/}/" .env
else
    sed -i '/^STOCK_LIST=/d' .env
fi

# ═══ 3. 模拟交易（紧接分析，确保使用最新评分）═══
echo "[$(date '+%H:%M')] 运行模拟交易..."
python3 simulated_trading.py 2>&1 | tee -a /tmp/stock_analysis_cron.log

# ═══ 4. 检查报告是否生成 ═══
if [ ! -f "$REPORT_FILE" ]; then
    echo "未生成报告，检查旧报告..."
    for i in 1 2 3; do
        PAST_DATE=$(date -d "-${i} day" +%Y%m%d 2>/dev/null || date -v-${i}d +%Y%m%d)
        [ -f "reports/report_${PAST_DATE}.md" ] && REPORT_FILE="reports/report_${PAST_DATE}.md" && break
    done
fi

# ═══ 5. 盘后量化引擎（18:00 仅）═══
if [ "$HOUR" = "18" ]; then
    echo "[$(date '+%H:%M')] 运行量化引擎..."
    python3 -m quant_engine.run_quant 2>&1 | tee -a /tmp/stock_analysis_cron.log

    echo "[$(date '+%H:%M')] 运行情绪引擎..."
    python3 -m sentiment_engine.sentiment_index 2>&1 | tee -a /tmp/stock_analysis_cron.log
fi

# ═══ 6. 推送 ═══
case $HOUR in
    09) PERIOD="早盘" ;;
    13) PERIOD="午盘" ;;
    18) PERIOD="收盘" ;;
    *)  PERIOD="盘中" ;;
esac

# 发送通知标题 - QQ
openclaw message send --channel qqbot --target "$QQ_TARGET" \
  --message "📊【${PERIOD}分析】${RUN_DATE}

⏰ 分析已完成，正在获取报告..." 2>/dev/null || true

# 发送通知标题 - 微信
openclaw message send --channel openclaw-weixin --target "$WX_TARGET" --account "$WX_ACCOUNT" \
  --message "📊【${PERIOD}分析】${RUN_DATE}

⏰ 分析已完成，正在获取报告..." 2>/dev/null || true

# ── 选股器候选推送（09:00 / 13:00） ──
if [ -f "$SCREENER_TOP5" ] && { [ "$HOUR" = "09" ] || [ "$HOUR" = "13" ]; }; then
    SCREENER_MSG=$(cat "$SCREENER_TOP5")

    openclaw message send --channel qqbot --target "$QQ_TARGET" \
      --message "$SCREENER_MSG" 2>/dev/null || true

    openclaw message send --channel openclaw-weixin --target "$WX_TARGET" --account "$WX_ACCOUNT" \
      --message "$SCREENER_MSG" 2>/dev/null || true
fi

# ── 个股分析摘要 ──
if [ -f "$REPORT_FILE" ]; then
    REPORT_SUMMARY=$(sed -n '/核心结论/,/报告生成时间/p' "$REPORT_FILE" 2>/dev/null | head -30)
    QUOTE_INFO=$(sed -n '/当日行情/,/数据透视/p' "$REPORT_FILE" 2>/dev/null | head -10)

    MSG="📊【${PERIOD}分析】${RUN_DATE}

${REPORT_SUMMARY}

${QUOTE_INFO}"

    openclaw message send --channel qqbot --target "$QQ_TARGET" \
      --message "$MSG" 2>/dev/null || true

    openclaw message send --channel openclaw-weixin --target "$WX_TARGET" --account "$WX_ACCOUNT" \
      --message "$MSG" 2>/dev/null || true

fi

# ── 大盘复盘 ──
if [ -f "$MARKET_REVIEW_FILE" ]; then
    REVIEW_FULL=$(sed -n '/一、盘面总览/,/七、风险提示/p' "$MARKET_REVIEW_FILE" 2>/dev/null)

    openclaw message send --channel qqbot --target "$QQ_TARGET" \
      --message "📈【大盘复盘】
${REVIEW_FULL}" 2>/dev/null || true

    openclaw message send --channel openclaw-weixin --target "$WX_TARGET" --account "$WX_ACCOUNT" \
      --message "📈【大盘复盘】
${REVIEW_FULL}" 2>/dev/null || true
fi

# ── 模拟交易账户快照 ──
if [ -f "$SIMULATED_SUMMARY" ]; then
    SIM_MSG=$(cat "$SIMULATED_SUMMARY")

    openclaw message send --channel qqbot --target "$QQ_TARGET" \
      --message "$SIM_MSG" 2>/dev/null || true

    openclaw message send --channel openclaw-weixin --target "$WX_TARGET" --account "$WX_ACCOUNT" \
      --message "$SIM_MSG" 2>/dev/null || true
fi

# ── 周五模拟交易绩效周报 ──
if [ -f "$SIMULATED_WEEKLY" ]; then
    WEEKLY_MSG=$(cat "$SIMULATED_WEEKLY")

    openclaw message send --channel qqbot --target "$QQ_TARGET" \
      --message "$WEEKLY_MSG" 2>/dev/null || true

    openclaw message send --channel openclaw-weixin --target "$WX_TARGET" --account "$WX_ACCOUNT" \
      --message "$WEEKLY_MSG" 2>/dev/null || true
fi

# ── 盘后推送（情绪指数 + 信号统计） ──
if [ "$HOUR" = "18" ]; then
    # 情绪指数
    SENTIMENT_FILE="sentiment_engine/sentiment_${RUN_DATE}.txt"
    if [ -f "$SENTIMENT_FILE" ]; then
        SENTIMENT_MSG=$(cat "$SENTIMENT_FILE")

        openclaw message send --channel qqbot --target "$QQ_TARGET" \
          --message "$SENTIMENT_MSG" 2>/dev/null || true

        openclaw message send --channel openclaw-weixin --target "$WX_TARGET" --account "$WX_ACCOUNT" \
          --message "$SENTIMENT_MSG" 2>/dev/null || true
    fi

    # 信号准确率统计
    VERIFY_RESULT=$(python3 -c "
from simulated_trading import verify_signal_accuracy
r = verify_signal_accuracy()
if r:
    print(f'🎯【信号准确率统计】')
    print(f'看多信号(评分≥70): {r[\"buy_signals\"]}次')
    print(f'看空信号(评分≤30): {r[\"sell_signals\"]}次')
" 2>/dev/null)
    if [ -n "$VERIFY_RESULT" ]; then
        openclaw message send --channel qqbot --target "$QQ_TARGET" \
          --message "$VERIFY_RESULT" 2>/dev/null || true

        openclaw message send --channel openclaw-weixin --target "$WX_TARGET" --account "$WX_ACCOUNT" \
          --message "$VERIFY_RESULT" 2>/dev/null || true
    fi
fi

echo "[$(date '+%H:%M')] 推送完成"
