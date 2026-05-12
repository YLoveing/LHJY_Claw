#!/usr/bin/env python3
"""
盘中实时监控 — 候选股异动监测

读取盘前选出的候选股列表，在交易时段内轮询实时行情，
检测建仓信号、风控信号、异动信号，输出结构化告警。

用法:
  python3 monitor_candidates.py                # 一次检查（适合cron）
  python3 monitor_candidates.py --loop         # 持续循环（适合tmux）

输出:
  - stdout 实时告警
  - 状态文件 /tmp/monitor_state.json（避免重复告警）
"""
import json, logging, os, sys, time, requests, re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("monitor")

# ─── 推送配置 ───
QQ_TARGET = "qqbot:c2c:7D15BBF664045E2DD5F33DA4BE0A00E9"
WX_TARGET = "o9cq800-zOjMI1JH4SjoT0NocAZI@im.wechat"


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data_provider.data_cache import DataCache

# ─── 配置 ───

CANDIDATES_FILE = "/opt/daily_stock_analysis/screener/candidates_latest.json"
STATE_FILE = "/tmp/monitor_state.json"
POLL_INTERVAL = 300  # 5分钟
MARKET_OPEN = "09:30"
MARKET_CLOSE = "15:00"

EAST_MONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}


def load_candidates() -> List[Dict]:
    """读取最新的候选股列表"""
    for path_str in [CANDIDATES_FILE]:
        p = Path(path_str)
        if p.exists():
            return json.loads(p.read_text())
    
    # 回退到日期文件
    today = datetime.now().strftime("%Y%m%d")
    fallback = Path(f"/opt/daily_stock_analysis/screener/candidates_{today}.json")
    if fallback.exists():
        return json.loads(fallback.read_text())
    
    log.warning("⚠️ 未找到候选股文件，尝试从最近文件加载")
    files = sorted(Path("/opt/daily_stock_analysis/screener/").glob("candidates_*.json"))
    if files:
        return json.loads(files[-1].read_text())
    return []


def load_state() -> Dict:
    """加载上次告警状态"""
    p = Path(STATE_FILE)
    if p.exists():
        return json.loads(p.read_text())
    return {"signals": {}, "last_daily": {}}


def save_state(state: Dict):
    """保存告警状态"""
    Path(STATE_FILE).write_text(json.dumps(state, ensure_ascii=False, indent=2))


def fetch_realtime_batch(codes: List[str]) -> Dict[str, Dict]:
    """批量获取候选股实时行情"""
    # 用东方财富批量接口（一次请求拿多只）
    codes_str = ",".join(f"1.{c}" if c.startswith("6") else f"0.{c}" for c in codes)
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    params = {
        "fields": "f2,f3,f4,f5,f6,f12,f14,f15,f16,f17,f18,f20,f21",
        "secids": codes_str,
        "fltt": 2,
        "invt": 2,
    }
    try:
        r = requests.get(url, params=params, headers=EAST_MONEY_HEADERS, timeout=10)
        data = r.json()
        result = {}
        for item in data.get("data", {}).get("diff", []):
            code = str(item.get("f12", ""))
            result[code] = {
                "code": code,
                "name": str(item.get("f14", "")),
                "price": item.get("f2", 0) or 0,
                "change_pct": item.get("f3", 0) or 0,
                "change_amount": item.get("f4", 0) or 0,
                "volume": item.get("f5", 0) or 0,
                "amount": (item.get("f20", 0) or 0) / 1e8,
                "high": item.get("f15", 0) or 0,
                "low": item.get("f16", 0) or 0,
                "open": item.get("f17", 0) or 0,
                "prev_close": item.get("f18", 0) or 0,
                "market_cap": (item.get("f21", 0) or 0) / 1e8,
                "time": datetime.now().strftime("%H:%M:%S"),
            }
        return result
    except Exception as e:
        log.error(f"批量行情获取失败: {e}")
        return {}


def fetch_kline_data(code: str, cache: DataCache) -> Tuple[Optional[Dict], Optional[float], Optional[float]]:
    """获取最近日线数据（MA5, MA10, MA20）"""
    df = cache.get_kline(code)
    if df is None or df.empty:
        return None, None, None
    
    df = df.sort_values("date")
    closes = df["close"].tail(25).values
    if len(closes) < 5:
        return None, None, None
    
    ma5 = closes[-5:].mean()
    ma10 = closes[-10:].mean() if len(closes) >= 10 else None
    ma20 = closes[-20:].mean() if len(closes) >= 20 else None
    vol = df["volume"].tail(6).values
    vol_ma5 = vol[:-1].mean() if len(vol) > 1 else 0
    
    return {"ma5": ma5, "ma10": ma10, "ma20": ma20, "vol_ma5": vol_ma5}, None, None


def check_signals(code: str, live: Dict, kline: Optional[Dict], state: Dict, is_holding: bool = False) -> List[Dict]:
    """
    检测触发条件。
    返回告警列表 [{"type": str, "code": str, "signal": str, "detail": str}, ...]
    """
    alerts = []
    signal_key = f"{code}_{state.get('_cycle', '')}"
    prev_signals = state.get("signals", {}).get(code, [])
    
    price = live.get("price", 0)
    pct = live.get("change_pct", 0)
    vol = live.get("volume", 0)
    amount_yi = live.get("amount", 0)
    name = live.get("name", code)
    
    # 计算当前量比（东方财富没有直接给量比时用近似值）
    vol_ratio = None
    if kline and kline.get("vol_ma5", 0) > 0 and vol > 0:
        vol_ratio = vol / kline["vol_ma5"]
    
    # ── ① 建仓信号 ──
    if not is_holding:
        # 放量突破 MA20
        if kline and kline.get("ma20") and price > 0 and "breakout_ma20" not in prev_signals:
            ma20 = kline["ma20"]
            if pct >= 3 and vol_ratio and vol_ratio >= 1.5:
                if price > ma20 * 1.01 and price < ma20 * 1.05:
                    alerts.append({
                        "type": "建仓", "code": code, "name": name,
                        "signal": "放量突破MA20",
                        "detail": f"涨幅{pct:+.1f}%, 量比{vol_ratio:.1f}, 价¥{price:.2f}, MA20=¥{ma20:.2f}",
                        "level": "important",
                    })
        
        # 回踩MA20企稳反弹
        if kline and kline.get("ma20") and price > 0 and "ma20_bounce" not in prev_signals:
            ma20 = kline["ma20"]
            lower = ma20 * 0.98
            upper = ma20 * 1.02
            if lower <= price <= upper and pct > 0 and vol_ratio and vol_ratio >= 1.0:
                alerts.append({
                    "type": "建仓", "code": code, "name": name,
                    "signal": "回踩MA20企稳",
                    "detail": f"价¥{price:.2f}, MA20=¥{ma20:.2f}, 涨幅{pct:+.1f}%, 量比{vol_ratio:.1f}",
                    "level": "info",
                })
        
        # 早盘强势
        hour = datetime.now().hour
        minute = datetime.now().minute
        if (hour == 9 and minute >= 30) or (hour == 10 and minute <= 0):
            if pct >= 2 and vol_ratio and vol_ratio >= 2:
                alerts.append({
                    "type": "建仓", "code": code, "name": name,
                    "signal": "早盘强势",
                    "detail": f"开盘30min内涨幅{pct:+.1f}%, 量比{vol_ratio:.1f}, 价¥{price:.2f}",
                    "level": "important",
                })
    
    # ── ② 持仓风控（is_holding 由调用者传入，这里也检测通用风险）─
    if is_holding:
        if pct <= -5 and "stop_loss" not in prev_signals:
            alerts.append({
                "type": "风控", "code": code, "name": name,
                "signal": "快速下跌-5%⚠️",
                "detail": f"盘中跌幅{pct:.1f}%, 价¥{price:.2f}, 考虑止损",
                "level": "critical",
            })
        
        if vol_ratio and vol_ratio >= 3 and pct < 0.5 and "volume_stall" not in prev_signals:
            alerts.append({
                "type": "风控", "code": code, "name": name,
                "signal": "放量滞涨🚩",
                "detail": f"量比{vol_ratio:.1f}但涨幅仅{pct:+.1f}%, 主力出货嫌疑",
                "level": "warning",
            })
    
    # ── ③ 异动关注 ──
    if abs(pct) >= 7 and "big_move" not in prev_signals:
        alerts.append({
            "type": "异动", "code": code, "name": name,
            "signal": "大幅波动",
            "detail": f"涨跌幅{pct:+.1f}%, 价¥{price:.2f}, 额¥{amount_yi:.1f}亿",
            "level": "warning",
        })
    
    if vol_ratio and vol_ratio >= 4 and "abnormal_vol" not in prev_signals:
        alerts.append({
            "type": "异动", "code": code, "name": name,
            "signal": "异常放量",
            "detail": f"量比{vol_ratio:.1f}, 价¥{price:.2f}, 涨跌幅{pct:+.1f}%",
            "level": "info",
        })
    
    return alerts


def format_alert(a: Dict) -> str:
    """格式化告警消息"""
    icons = {"建仓": "📈", "风控": "🚨", "异动": "💥"}
    icon = icons.get(a["type"], "⚡")
    return f"{icon} {a['signal']}  {a['name']}({a['code']}) — {a['detail']}"


def should_monitor() -> bool:
    """判断当前是否在交易时段"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False  # 周末
    t = now.strftime("%H:%M")
    return MARKET_OPEN <= t <= MARKET_CLOSE


def run_once() -> List[Dict]:
    """执行一轮监控检查"""
    if not should_monitor():
        return []
    
    candidates = load_candidates()
    if not candidates:
        log.info("⏸ 无候选股，跳过监控")
        return []
    
    codes = [c.get("code", "") for c in candidates if c.get("code")]
    log.info(f"[{datetime.now():%H:%M:%S}] 监控 {len(codes)} 只候选股...")
    
    live = fetch_realtime_batch(codes)
    if not live:
        return []
    
    state = load_state()
    state["_cycle"] = datetime.now().strftime("%Y%m%d_%H%M")
    if "signals" not in state:
        state["signals"] = {}
    
    # 加载 K 线数据用于均线判断
    cache = DataCache()
    
    all_alerts = []
    for c in codes:
        if c not in live:
            continue
        kline_data, _, _ = fetch_kline_data(c, cache)
        alerts = check_signals(c, live[c], kline_data, state, is_holding=False)
        
        if alerts:
            state["signals"].setdefault(c, [])
            for a in alerts:
                if a["signal"] not in state["signals"][c]:
                    state["signals"][c].append(a["signal"])
                    all_alerts.append(a)
    
    if all_alerts:
        save_state(state)
        log.info(f"  → {len(all_alerts)} 条新告警")
        for a in all_alerts:
            log.info(f"    {format_alert(a)}")
        # 写入告警文件供读取推送
        alert_file = Path("/tmp/monitor_new_alerts.json")
        existing = []
        if alert_file.exists():
            try:
                existing = json.loads(alert_file.read_text())
            except (json.JSONDecodeError, OSError) as e: log.debug(f"读取告警文件失败(可能为空): {e}")
        existing.extend(all_alerts)
        alert_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
        # 推送新告警到 QQ + 微信
        _send_notifications(all_alerts)
    else:
        log.info(f"  无新告警")
    
    return all_alerts


def _send_notifications(alerts: List[Dict]):
    """推送新告警到 QQ"""
    if not alerts:
        return
    now = datetime.now().strftime("%H:%M")
    lines = [f"🚨 盘中监控 {now}"]
    for a in alerts:
        s = a.get("signal", "")
        icon = {"建仓": "🟢", "风控": "🔴", "大幅波动": "💥", "量能异动": "⚡", "趋势反转": "🔄"}.get(s, "⚠")
        code = a.get("code", "")
        price = a.get("price", "")
        detail = a.get("detail", "")
        lines.append(f"{icon} [{s}] {code} {a.get('name','')} 价{price}")
        if detail:
            lines.append(f"   {detail}")
    msg = "\n".join(lines)  # 使用真实换行符，发给QQ才有换行
    
    # 写入临时文件避免shell转义问题
    tmp = Path("/tmp/monitor_push_msg.txt")
    tmp.write_text(msg, encoding="utf-8")
    for target in [QQ_TARGET]:
        cmd = f'openclaw message send --channel qqbot --target "{target}" --message "$(cat {tmp})" 2>/dev/null || true'
        ret = os.system(cmd)
        if ret == 0 or ret == 256:
            log.info(f"  ✅ 已推送 {target.split(':')[0]}")
        else:
            log.warning(f"  ⚠ 推送失败 rc={ret}")
    tmp.unlink(missing_ok=True)


def loop():
    """持续循环模式"""
    log.info(f"🔄 盘中监控启动，间隔{POLL_INTERVAL}s")
    log.info(f"  交易时段: {MARKET_OPEN}~{MARKET_CLOSE}")
    
    while True:
        try:
            run_once()
        except Exception as e:
            log.error(f"监控异常: {e}")
        
        log.info(f"  等待 {POLL_INTERVAL}s...")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        loop()
    else:
        run_once()
