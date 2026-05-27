#!/usr/bin/env python3
"""
===================================
舆情盘中监控模块 - Market Sentiment Monitor
===================================

功能：
  在交易时段内扫描持仓股的最新舆情，检测负面信号并推送告警。

设计原则（来自@龙虾派-元宝虾建议）：
  1. 预设负面词库先筛 → 省80%LLM调用成本
  2. 持仓多时分清轻重：浮亏最大 > 刚买5天内 > 近期解禁/财报
  3. 舆情先弹窗人工确认 → 不自动止损
  4. 推文格式一眼能看明白

扫描节奏（cron）：
  10:30 → 早盘情报消化后第一轮
  13:30 → 午盘情报
  14:30 → 收盘前最后一轮

数据源：
  - MX妙想 search_news（金融资讯搜索，含新闻/公告/研报）
  - Tavily（作为 MX 搜索的补充后备）
  - 情绪引擎 sentiment_index（大盘整体情绪参考）

输出：
  - push_output/sentiment_{YYYYMMDD}_{HHMM}.txt → OpenClaw cron 读取
  - 直接推送 QQ + WX（含持仓数据）

用法：
  python3 scripts/sentiment_monitor.py              # 自动检测时段，扫描持仓
  python3 scripts/sentiment_monitor.py --force       # 强制扫描所有持仓
  python3 scripts/sentiment_monitor.py --dry-run     # 仅打印，不推送
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 添加项目根到路径，以便使用 mx_fetcher
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ─── 配置 ───

BASE_DIR = Path("/opt/daily_stock_analysis")
PUSH_OUTPUT_DIR = BASE_DIR / "push_output"
PUSH_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 持仓状态文件
STATE_FILE = BASE_DIR / "simulated_trading" / "state.json"

# 情绪引擎输出
SENTIMENT_HISTORY_FILE = BASE_DIR / "sentiment_engine" / "history.json"
SENTIMENT_ADJUSTMENT_FILE = BASE_DIR / "sentiment_engine" / "adjustment.json"

# 候选股 - 监控候选股也需要舆情扫描
CANDIDATES_FILE = BASE_DIR / "screener" / "candidates_latest.json"

# ─── 负面词库（覆盖A股常见利空信号） ───

NEGATIVE_KEYWORDS_LEVEL1 = [
    # 🔴 监管/法律（最高优先）
    "立案", "调查", "处罚", "监管函", "问询函", "关注函", "警示函",
    "通报批评", "公开谴责", "纪律处分", "责令改正", "停牌核查",
    "停牌", "ST", "*ST", "退市", "风险警示", "暂停上市", "终止上市",
    "涉嫌", "违法违规", "违反", "失信", "被执行人", "冻结",
    "查封", "扣押", "诉讼", "仲裁", "判决", "败诉",
]

NEGATIVE_KEYWORDS_LEVEL2 = [
    # 🟠 业绩/经营（中等优先）
    "亏损", "预亏", "业绩变脸", "大幅下降", "净利润下滑",
    "营收下滑", "裁员", "降薪", "停工", "停产", "违约",
    "逾期", "债务", "资金链", "流动性", "减值", "商誉",
    "计提", "坏账", "担保", "关联交易",
]

NEGATIVE_KEYWORDS_LEVEL3 = [
    # 🟡 利空因素（一般优先）
    "减持", "套现", "利空", "负面", "做空", "唱空",
    "下调评级", "看空", "熊市", "崩盘", "闪崩", "跌停",
    "出货", "抛售", "逃离", "出逃",
]

# ── 股票名称映射（带ETF常见品种）──
COMMON_NAME_MAP = {
    # ETF
    "510300": "沪深300ETF",
    "510050": "上证50ETF",
    "510500": "中证500ETF",
    "588000": "科创50ETF",
    "159915": "创业板ETF",
    "159949": "创业板50ETF",
    "159845": "中证1000ETF",
    "512100": "中证1000ETF",
    "510880": "红利ETF",
    # 自选股/持仓股
    "002410": "广联达",
    "601689": "拓普集团",
    "688187": "时代电气",
    "600626": "申达股份",
    "600372": "中航机载",
    "600584": "长电科技",
    "600160": "巨化股份",
    "603986": "兆易创新",
    "600188": "兖矿能源",
    "002470": "金正大",
}


def load_name_map() -> Dict[str, str]:
    """加载股票名称映射（含 src/data/stock_mapping.py 中的扩展映射）"""
    name_map = dict(COMMON_NAME_MAP)
    # 尝试加载项目内映射
    mapping_py = BASE_DIR / "src" / "data" / "stock_mapping.py"
    if mapping_py.exists():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("stock_mapping", mapping_py)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if hasattr(mod, "STOCK_NAME_MAP"):
                    name_map.update(mod.STOCK_NAME_MAP)
        except Exception:
            pass
    return name_map


def resolve_name(code: str, name_map: Dict[str, str]) -> str:
    """股票代码→中文名（多级回退）"""
    # 1. 先尝试项目映射
    if code in name_map:
        return name_map[code]
    # 2. 从候选股缓存拿
    try:
        if CANDIDATES_FILE.exists():
            cands = json.loads(CANDIDATES_FILE.read_text())
            for c in cands:
                if c.get("code") == code:
                    name = c.get("name", "")
                    if name:
                        return name
    except (json.JSONDecodeError, IOError):
        pass
    # 3. 从 candidates 历史文件找
    try:
        today = datetime.now().strftime("%Y%m%d")
        for offset in range(8):
            from datetime import timedelta
            d = datetime.now() - timedelta(days=offset)
            ds = d.strftime("%Y%m%d")
            cand_file = BASE_DIR / "screener" / f"candidates_{ds}.json"
            if cand_file.exists():
                cands = json.loads(cand_file.read_text())
                for c in cands:
                    if c.get("code") == code:
                        return c.get("name", code)
    except (json.JSONDecodeError, IOError):
        pass
    return code  # fallback


def load_state() -> Optional[Dict]:
    """读取模拟账户持仓状态"""
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, IOError):
        return None


def load_sentiment_history() -> Optional[Dict]:
    """读取情绪引擎历史数据，判断大盘状态"""
    if not SENTIMENT_HISTORY_FILE.exists():
        return None
    try:
        data = json.loads(SENTIMENT_HISTORY_FILE.read_text())
        return data
    except (json.JSONDecodeError, IOError):
        return None


def load_sentiment_adjustment() -> Optional[float]:
    """读取情绪引擎仓位调节系数"""
    if not SENTIMENT_ADJUSTMENT_FILE.exists():
        return None
    try:
        data = json.loads(SENTIMENT_ADJUSTMENT_FILE.read_text())
        return data.get("position_adjustment", 1.0)
    except (json.JSONDecodeError, IOError):
        return None


def search_mx_news(code: str, name: str, count: int = 5) -> List[Dict[str, str]]:
    """
    使用 MX 妙想搜索个股最新资讯。
    优先用股票名搜索（资讯更精准），股票代码作为后备。
    """
    from data_provider.mx_fetcher import search_news as mx_search_news
    
    results = []
    seen_titles = set()
    
    # 优先用股票名称搜索（资讯最相关）
    keywords = []
    if name and name != code:
        keywords.append(name)
    keywords.append(code)
    
    for keyword in keywords:
        try:
            items = mx_search_news(keyword, count=count)
            for item in items:
                t = item.get("title", "")
                if t and t not in seen_titles:
                    seen_titles.add(t)
                    results.append(item)
        except Exception as e:
            print(f"[sentiment_monitor]  MX搜索异常 ({keyword}): {e}", file=sys.stderr)
        time.sleep(0.3)  # 防限流
    
    return results[:count]


def assess_news_risk(
    items: List[Dict[str, str]]
) -> Tuple[str, List[str], List[str]]:
    """
    对搜索结果进行风险评级。

    Returns:
      (level, matched_level1, matched_level2+level3)
      level: "🔴" | "🟠" | "🟡" | "🟢"
    """
    all_text = []
    for item in items:
        text = f"{item.get('title', '')} {item.get('content', '')}"
        all_text.append(text)
    combined = " ".join(all_text).lower()

    matched_l1 = [kw for kw in NEGATIVE_KEYWORDS_LEVEL1 if kw.lower() in combined]
    matched_l2 = [kw for kw in NEGATIVE_KEYWORDS_LEVEL2 if kw.lower() in combined]
    matched_l3 = [kw for kw in NEGATIVE_KEYWORDS_LEVEL3 if kw.lower() in combined]

    if matched_l1:
        return ("🔴", matched_l1, matched_l2 + matched_l3)
    if matched_l2:
        return ("🟠", [], matched_l2 + matched_l3)
    if matched_l3:
        return ("🟡", [], matched_l3)
    return ("🟢", [], [])


def compute_priority(
    code: str,
    position: Dict,
    state: Dict
) -> int:
    """
    计算持仓的监控优先级（数字越大越优先）。

    规则（摘自龙虾派建议）：
      1. 浮亏最大：按浮亏比例排序
      2. 新买5天内：entry_date 距今 ≤5交易日
      3. 买得早的也要看：按 entry_date 排序（老的优先）
      4. 候选股：低优先
    """
    score = 0
    today = datetime.now().strftime("%Y%m%d")

    # 浮亏权重
    invested = position.get("invested", 0)
    if invested > 0:
        current_price = position.get("current_price", 0)
        avg_cost = position.get("avg_cost", 0)
        if avg_cost > 0:
            unrealized_pnl_pct = (current_price - avg_cost) / avg_cost * 100
            # 浮亏越多权重越高
            if unrealized_pnl_pct < 0:
                score += int(abs(unrealized_pnl_pct) * 2)

    # 新买5天内 - 高优先级
    entry_date = position.get("entry_date", "")
    if entry_date and len(entry_date) == 8:
        try:
            entry_dt = datetime.strptime(entry_date, "%Y%m%d")
            days_held = (datetime.now() - entry_dt).days
            if days_held <= 5:
                score += 30
            elif days_held <= 10:
                score += 15
            elif days_held >= 30:
                # 持仓久了也要注意
                score += 5
        except ValueError:
            pass

    return score


def format_alert(
    code: str,
    name: str,
    risk_level: str,
    matched_l1: List[str],
    matched_l2_l3: List[str],
    items: List[Dict[str, str]],
    position_info: str = "",
) -> str:
    """格式化为推送文本（一眼能看明白）"""
    lines = []
    lines.append(f"{risk_level}【舆情警报】{name}({code})")
    if position_info:
        lines.append(f"  持仓: {position_info}")

    if matched_l1:
        lines.append(f"  关键词: {', '.join(matched_l1)}")
    if matched_l2_l3:
        lines.append(f"  风险项: {', '.join(matched_l2_l3)}")

    # 最近一条新闻标题
    if items:
        newest = items[0]
        title = newest.get("title", "")
        source = newest.get("source", "")
        date_str = newest.get("date", "")
        if title:
            lines.append(f"  最近: {title}")
            parts = []
            if source:
                parts.append(source)
            if date_str:
                parts.append(date_str)
            if parts:
                lines.append(f"  ({' / '.join(parts)})")
        # 第二条
        if len(items) > 1:
            title2 = items[1].get("title", "")
            if title2:
                lines.append(f"  关联: {title2}")

    if risk_level == "🟢":
        lines.append(f"  近期无明显负面舆情 ✅")
    else:
        lines.append(f"  ⚠️ 建议关注，人工判断是否需要处理")

    return "\n".join(lines)


def push_message(content: str, dry_run: bool = False):
    """推送消息到 Q+WX + push_output 缓存"""
    if dry_run:
        print(content)
        return

    if not content:
        return
    
    now = datetime.now()
    
    # 写入按小时分类的推送文件（供 OpenClaw cron 读取转发到元宝群）
    hour_tag = now.strftime("%H")
    push_file = PUSH_OUTPUT_DIR / f"sentiment_{now.strftime('%Y%m%d')}_{hour_tag}.txt"
    push_file.write_text(content)
    
    # 追加到当天聚合文件
    agg_file = PUSH_OUTPUT_DIR / f"sentiment_{now.strftime('%Y%m%d')}.txt"
    with open(agg_file, "a", encoding="utf-8") as f:
        f.write(f"--- {now.strftime('%H:%M')} ---\n")
        f.write(content + "\n\n")

    # 直接推送到 QQ
    qq_target = os.environ.get("QQ_TARGET", "")
    if qq_target:
        try:
            subprocess.run(
                ["openclaw", "message", "send", "--channel", "qqbot",
                 "--target", qq_target, "--message", content],
                capture_output=True, timeout=30,
            )
        except Exception:
            pass


def scan_positions(dry_run: bool = False) -> List[str]:
    """扫描所有持仓和候选股，返回告警列表"""
    name_map = load_name_map()
    state = load_state()
    if not state:
        print("[sentiment_monitor] ❌ 无法读取持仓状态")
        return []

    positions = state.get("positions", {})
    total_equity = state.get("total_equity", 0)
    total_return_pct = state.get("total_return_pct", 0)

    if not positions:
        print("[sentiment_monitor] 📭 当前无持仓，跳过舆情扫描")
        return []

    # 先计算每条持仓的优先级
    scored = []
    for code, pos in positions.items():
        priority = compute_priority(code, pos, state)
        scored.append((priority, code, pos))

    # 按优先级降序排列
    scored.sort(key=lambda x: x[0], reverse=True)

    alerts = []
    total_scanned = 0

    if not dry_run:
        now_tag = datetime.now().strftime("%H:%M")
        header = f"📡【盘中舆情扫描】{datetime.now().strftime('%m/%d')} {now_tag}"
        header += f"\n📊 账户: {total_equity:.0f}元 ({total_return_pct:+.1f}%) | 持仓{len(positions)}只"
    else:
        header = ""

    out_lines = [header] if header else []

    for priority, code, pos in scored:
        avg_cost = pos.get("avg_cost", 0)
        current_price = pos.get("current_price", 0)

        # 计算浮动盈亏
        if avg_cost > 0:
            unrealized_pnl = (current_price - avg_cost) * pos.get("quantity", 0)
            unrealized_pct = (current_price - avg_cost) / avg_cost * 100
            pnl_str = f"{unrealized_pnl:+.0f}元({unrealized_pct:+.1f}%)"
        else:
            pnl_str = ""

        name = resolve_name(code, name_map)

        print(f"[sentiment_monitor] 🔍 {name}({code}) 优先级={priority} 浮亏={pnl_str}")

        # 搜索新闻
        items = search_mx_news(code, name, count=3)
        total_scanned += 1

        if not items:
            print(f"[sentiment_monitor]   ⚠ 未搜到近期资讯")
            continue

        # 风险评估
        risk_level, matched_l1, matched_l2_l3 = assess_news_risk(items)

        if risk_level != "🟢":
            # 只推告警
            pos_info = f"{pnl_str} | 成本{avg_cost:.2f}→现价{current_price:.2f}"
            alert_text = format_alert(
                code, name, risk_level, matched_l1, matched_l2_l3, items, pos_info
            )
            alerts.append(alert_text)
            out_lines.append(alert_text)
            out_lines.append("")  # 空行分隔

            print(f"[sentiment_monitor]   {risk_level} 检测到负面舆情！")
            for kw in matched_l1:
                print(f"     🔴 {kw}")
            for kw in matched_l2_l3:
                print(f"     {'🟠' if kw in NEGATIVE_KEYWORDS_LEVEL2 else '🟡'} {kw}")
        else:
            print(f"[sentiment_monitor]   ✅ 无负面舆情")

        time.sleep(0.5)  # MX搜间隔，防限流

    # 汇总输出
    if total_scanned == 0:
        print("[sentiment_monitor] 📭 无持仓可扫描")
        return []

    summary = f"\n📊 扫描小结: {total_scanned}只持仓，{len(alerts)}条警报"
    out_lines.append(summary)
    print(f"[sentiment_monitor] {summary}")

    full_text = "\n".join(out_lines)
    if not dry_run:
        push_message(full_text, dry_run=False)

    return alerts


def main():
    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv or dry_run

    # 自动判断时段（非force模式下）
    if not force:
        now_hour = datetime.now().hour
        if not (9 <= now_hour <= 14):
            print("[sentiment_monitor] ⏰ 非交易时段（9:00-14:59），跳过")
            return
        if now_hour == 9:
            # 早盘9:00-9:30数据还不全
            if datetime.now().minute < 30:
                print("[sentiment_monitor] ⏰ 早盘9:30前数据不全，跳过")
                return

    print(f"[sentiment_monitor] 🚀 启动{'（演练）' if dry_run else ''}...")
    print(f"[sentiment_monitor] 扫描持仓中...")
    scan_positions(dry_run=dry_run)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
