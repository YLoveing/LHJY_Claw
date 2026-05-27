#!/bin/bash
# 打工马🐴 每日策略自检 - 凌晨3点执行
# 检查系统健康、策略问题、评分异常、提出改进建议

LOG_FILE="/opt/daily_stock_analysis/logs/autopsy_$(date +%Y%m%d).log"

{
  echo "=== 打工马🐴 每日策略自检 $(date '+%Y-%m-%d %H:%M') ==="

  # 1. 检查状态文件
  STATE_FILE="/opt/daily_stock_analysis/simulated_trading/state.json"
  if [ -f "$STATE_FILE" ]; then
    CASH=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(f'现金: ¥{d[\"cash\"]:.2f}, 持仓: {len(d.get(\"positions\",{}))}只, 总收益: {d[\"total_return_pct\"]:.2f}%')")
    echo "[状态] $CASH"
  fi

  # 2. 检查GARCH文件是否再生
  if [ -f "/opt/daily_stock_analysis/quant_engine/garch_output.json" ]; then
    echo "[⚠️ GARCH] garch_output.json 已再生！检查n_obs..."
    N_OBS=$(python3 -c "import json; print(json.load(open('/opt/daily_stock_analysis/quant_engine/garch_output.json')).get('n_obs',0))")
    if [ "$N_OBS" -lt 50 ]; then
      echo "[❌ GARCH] 仅${N_OBS}个样本，可能再次锁死阈值！需干预"
      # 报告给打工虾
    else
      echo "[✅ GARCH] ${N_OBS}个样本，正常"
    fi
  else
    echo "[✅ GARCH] 文件未再生，正常"
  fi

  # 3. 检查最近一次日志是否有异常
  LATEST_LOG=$(ls -t /opt/daily_stock_analysis/logs/stock_analysis_*.log 2>/dev/null | head -1)
  if [ -n "$LATEST_LOG" ]; then
    ERRORS=$(grep -c 'ERROR\|CRITICAL\|Exception\|Traceback' "$LATEST_LOG" 2>/dev/null || echo 0)
    echo "[日志] 最近日志: $(basename $LATEST_LOG) | 错误数: $ERRORS"
  fi

  # 4. 最后交易记录
  TRADES="/opt/daily_stock_analysis/simulated_trading/trades.json"
  if [ -f "$TRADES" ]; then
    LAST_TRADE=$(python3 -c "
import json
trades = json.load(open('$TRADES'))
if trades:
    t = trades[-1]
    print(f'{t[\"date\"]} | {t.get(\"code\",\"?\")} | {t[\"side\"]}' + (f' | pnl={t[\"pnl\"]:.2f}' if 'pnl' in t else ''))
else:
    print('无交易记录')
")
    echo "[交易] 最近一笔: $LAST_TRADE"
  fi

  echo "=== 自检完成 ==="
} > "$LOG_FILE" 2>&1

# 如果有问题，输出到stdout让cron发邮件
grep -q '❌\|ERROR\|CRITICAL' "$LOG_FILE" 2>/dev/null && cat "$LOG_FILE"
