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

# 加载项目 .env（兼容 cron 不设 set -a; source .env 的情况）
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_path)
    except ImportError:
        # 手动解析 .env 回退
        for line in _env_path.read_text().split("\n"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("monitor")

# 导入盘中自动交易模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import intraday_trading

# ─── 推送配置（优先读环境变量，有 .env 自动注入） ───
QQ_TARGET = os.getenv("QQ_TARGET") or "qqbot:c2c:7D15BBF664045E2DD5F33DA4BE0A00E9"



sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.trading_calendar import is_trading_day, eastmoney_secid
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
    codes_str = ",".join(eastmoney_secid(c) for c in codes)
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
    
    # 计算当前量比
    # ⚠️ 注意：vol 是盘中实时累计量（截至当前时刻），vol_ma5 是5日日均完整日线量
    # 必须将实时量按已开盘时长折算到全日预估值，再与日均量对比
    vol_ratio = None
    if kline and kline.get("vol_ma5", 0) > 0 and vol > 0:
        now = datetime.now()
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        elapsed = (now - market_open).total_seconds()
        # A股交易时段：09:30-11:30(2h) + 13:00-15:00(2h) = 4小时 = 14400秒
        # 中午休市(11:30-13:00)不计入，elapsed 会包含这段时间
        # 所以需要修正：减去休市时间
        noon_start = now.replace(hour=11, minute=30, second=0, microsecond=0)
        noon_end = now.replace(hour=13, minute=0, second=0, microsecond=0)
        trading_seconds = 14400.0  # 4小时 = 14400秒
        if noon_start <= now <= noon_end:
            # 午间休市，直接用已完成的上午时段估算
            morning_seconds = 7200.0  # 09:30-11:30 = 2h
            elapsed_effective = morning_seconds
        elif now > noon_end:
            elapsed_effective = elapsed - 3600.0  # 扣除中午休市1小时
        else:
            elapsed_effective = elapsed  # 上午时段
        
        if 0 < elapsed_effective < trading_seconds:
            daily_vol_est = vol / (elapsed_effective / trading_seconds)
            vol_ratio = daily_vol_est / kline["vol_ma5"]
        else:
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
                        "vol_ratio": vol_ratio,
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
                    "vol_ratio": vol_ratio,
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
                    "vol_ratio": vol_ratio,
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
    """判断当前是否在交易时段（含交易日历）"""
    now = datetime.now()
    if not is_trading_day(now):
        return False  # 非交易日
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

    # ── 早盘新闻预取（09:30-10:00） ──
    now_hm = datetime.now().strftime("%H%M")
    is_morning_window = "0930" <= now_hm <= "1000"
    news_prefetched = False
    if is_morning_window and candidates:
        try:
            from data_provider.mx_fetcher import search_news
            mx_apikey = os.environ.get("MX_APIKEY", "")
            if mx_apikey:
                news_cache = {}
                for c in candidates[:3]:  # 最多预取3只
                    code = c.get("code", "")
                    name = c.get("name", "")
                    kw = name or code
                    news = search_news(kw + " 最新消息", count=3)
                    if news:
                        news_cache[code] = {"name": name, "news": news}
                if news_cache:
                    news_file = Path(f"/tmp/monitor_news_{datetime.now():%Y%m%d}.json")
                    news_file.write_text(json.dumps(news_cache, ensure_ascii=False, indent=2))
                    news_prefetched = True
                    log.info(f"  → 早盘新闻预取 {len(news_cache)} 只")
        except ImportError:
            pass  # MX未安装，跳过
        except Exception as e:
            log.warning(f"  新闻预取失败: {e}")

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
    
    # ── 盘中风控：先检查已有持仓止损/止盈/跟踪止损 ──
    closed = intraday_trading.check_positions()
    if closed:
        log.info(f"  → [日内交易] 平仓 {len(closed)} 笔")

    # ── 建仓信号 → 记录到待执行队列（次日开盘执行，遵守A股T+1） ──
    recorded_signals = []
    for a in all_alerts:
        if a.get("type") == "建仓" and a.get("signal") in intraday_trading.AUTO_BUY_SIGNALS:
            code = a["code"]
            live_data = live.get(code, {})
            sig = intraday_trading.record_signal(
                code=code,
                name=a.get("name", ""),
                signal=a["signal"],
                detail=a.get("detail", ""),
                price=live_data.get("price", 0),
                volume=live_data.get("volume", 0),
                vol_ratio=a.get("vol_ratio"),  # 从 check_signals 透传
            )
            if sig:
                recorded_signals.append(sig)

    if all_alerts or recorded_signals or closed:
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
        # 推送（含信号记录/平仓信息）
        _send_notifications(all_alerts, recorded_signals, closed)
    else:
        log.info(f"  无新告警")
    
    return all_alerts


def _load_holding_codes() -> set:
    """读取 state.json 中实际持仓的股票代码集合"""
    state_file = Path("/opt/daily_stock_analysis/simulated_trading/state.json")
    if not state_file.exists():
        return set()
    try:
        state = json.loads(state_file.read_text())
        return set(state.get("positions", {}).keys())
    except (json.JSONDecodeError, IOError):
        return set()


def _send_notifications(alerts: List[Dict], recorded_signals: List[Dict] = None, closed: List[Dict] = None):
    """
    推送新告警（仅推送持仓股相关告警，候选股不推送）+ 信号记录/平仓动态
    
    @用户要求：舆情只分析持仓的，别乱发浪费token
    """
    if not any([alerts, recorded_signals, closed]):
        return
    
    # 筛选：仅推送持仓股相关的告警
    holding_codes = _load_holding_codes()
    relevant_alerts = [a for a in alerts if a.get("code", "") in holding_codes]
    
    now = datetime.now().strftime("%H:%M")
    lines = []
    
    # 持仓告警
    if relevant_alerts:
        lines.append(f"🔔【持仓监控】{now}")
        for a in relevant_alerts:
            s = a.get("signal", "")
            icon = {"建仓": "🟢", "风控": "🔴", "大幅波动": "💥", "量能异动": "⚡", "趋势反转": "🔄"}.get(s, "⚠")
            code = a.get("code", "")
            price = a.get("price", "")
            detail = a.get("detail", "")
            lines.append(f"{icon} [{s}] {code} {a.get('name','')} 价{price}")
            if detail:
                lines.append(f"   {detail}")
    
    # 候选股告警不推送，只记日志
    skipped = len(alerts) - len(relevant_alerts)
    if skipped > 0:
        log.info(f"  📋 候选股告警 {skipped} 条已跳过推送（非持仓）")
    
    # 平仓
    if closed:
        lines.append("")
        lines.append("🔴【风控平仓】：")
        for t in closed:
            pnl_str = f"盈亏{t['pnl']:+,.2f}"
            lines.append(f"  卖出 {t.get('name','')}({t['code']}) × {t['quantity']}股 @ ¥{t['price']:.3f} {pnl_str} — {t.get('reason','')}")
    
    # 日内信号交易状态摘要
    try:
        status_text = intraday_trading.get_status_text()
        if status_text.strip():
            lines.append("")
            for line in status_text.split("\n"):
                if line.startswith("💰") or "日内信号交易" in line:
                    lines.append(line)
    except Exception:
        pass
    
    msg = "\n".join(lines)
    
    if not msg.strip():
        log.info("  无持仓相关告警，跳过推送")
        return
    
    # 用 subprocess 避免 shell 转义问题
    import subprocess
    for target in [QQ_TARGET]:
        try:
            r = subprocess.run(
                ["openclaw", "message", "send",
                 "--channel", "qqbot",
                 "--target", target,
                 "--message", msg],
                capture_output=True, timeout=15
            )
            log.info(f"  ✅ 已推送 {target.split(':')[0]}")
        except Exception as e:
            log.warning(f"  ⚠ 推送失败: {e}")


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
