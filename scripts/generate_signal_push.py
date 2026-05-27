#!/usr/bin/env python3
"""
generate_signal_push.py — 生成"三句话"信号推送文本

读取 daily_stock_analysis 数据文件，输出三句话（买入/卖出/持仓）。
18:00 完整输出，09:25 仅输出买入关注，11:30 空输出（由 monitor 自行推送异动）。

数据来源:
  - screener/candidates_{YYYYMMDD}.json  → 买入信号
  - simulated_trading/state.json         → 卖出信号 + 持仓状态
  - sentiment_engine/sentiment_{YYYYMMDD}.txt → 极值告警

用法:
  python3 scripts/generate_signal_push.py              # 自动取今日日期
  python3 scripts/generate_signal_push.py 20260515     # 指定日期
  python3 scripts/generate_signal_push.py --hour=18    # 指定时段
"""

import json
import os
import re
import sys
from datetime import date, datetime

BASE_DIR = "/opt/daily_stock_analysis"

# ─── 常用品种名称映射（ETF/指数/常见股，作为 fallback）───
_STATIC_NAME_MAP = {
    # ETF
    "510300": "沪深300ETF",
    "510050": "上证50ETF",
    "510500": "中证500ETF",
    "588000": "科创50ETF",
    "159915": "创业板ETF",
    "159949": "创业板50ETF",
    "512100": "中证1000ETF",
    "510880": "红利ETF",
    "159845": "中证1000ETF",
    # 常见自选股
    "002410": "广联达",
    "002470": "金正大",
    "600626": "申达股份",
    "600372": "中航机载",
    "601689": "拓普集团",
    "600584": "长电科技",
    "600160": "巨化股份",
    "603986": "兆易创新",
    "600188": "兖矿能源",
}


def yyyymmdd(d):
    return d.strftime("%Y%m%d")


def read_json(rel_path):
    """读取 JSON 文件，失败返回 None。"""
    full = os.path.join(BASE_DIR, rel_path)
    if not os.path.isfile(full):
        return None
    try:
        with open(full, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def read_text(rel_path):
    """读取文本文件，失败返回 None。"""
    full = os.path.join(BASE_DIR, rel_path)
    if not os.path.isfile(full):
        return None
    try:
        with open(full, "r", encoding="utf-8") as f:
            return f.read().strip()
    except IOError:
        return None


def _build_name_cache(target_date):
    """
    从当天 candidates 文件中构建 {code: name} 映射。
    """
    cache = dict(_STATIC_NAME_MAP)
    ds = yyyymmdd(target_date)
    candidates = read_json(f"screener/candidates_{ds}.json")
    if candidates:
        for c in candidates:
            code = str(c.get("code", ""))
            name = c.get("name", "")
            if code and name:
                cache[code] = name.strip()

    # 也尝试从最近几天的候选文件搜索
    for offset in range(1, 8):
        from datetime import timedelta
        d2 = target_date - timedelta(days=offset)
        ds2 = yyyymmdd(d2)
        candidates2 = read_json(f"screener/candidates_{ds2}.json")
        if candidates2:
            for c in candidates2:
                code = str(c.get("code", ""))
                name = c.get("name", "")
                if code and name and code not in cache:
                    cache[code] = name.strip()
    return cache


def resolve_name(code, name_cache):
    """根据 code 解析股票名称。"""
    return name_cache.get(code, code)


def parse_sentiment_value(text):
    """
    从情绪指数文本中解析综合情绪指数数值。
    示例行: 🟡 **综合情绪指数：53/100** — 中性
    返回 int 或 None。
    """
    if not text:
        return None
    m = re.search(r"综合情绪指数[：:]\s*(\d+)(?:/100)?", text)
    if m:
        return int(m.group(1))
    return None


def _pick_condition(candidate):
    """从候选股中选一条最突出的条件文字用于展示。"""
    conds = candidate.get("conditions", [])
    reasoning = candidate.get("llm_reasoning", "") or ""
    indicators = candidate.get("indicators", {}) or {}
    price_ma20_pct = indicators.get("price_MA20_pct", 0) or 0

    if "放量突破" in conds and price_ma20_pct:
        return f"放量突破MA20(+{price_ma20_pct:.1f}%)"
    if "多头" in conds:
        return "多头排列"
    if "金叉" in conds:
        return "MACD金叉"
    for kw in ["放量", "突破", "金叉", "均线", "多头"]:
        if kw in reasoning:
            return reasoning[:20] + ("…" if len(reasoning) > 20 else "")
    return conds[0] if conds else "—"


# ─── 买入信号 ────────────────────────────────────

def gen_buy_signals(target_date, name_cache=None):
    """
    读取 screener/candidates_{YYYYMMDD}.json，
    取 final_score >= 60 的前 3 只，返回格式化文本段。
    """
    d = target_date or date.today()
    ds = yyyymmdd(d)
    candidates = read_json(f"screener/candidates_{ds}.json")
    if not candidates:
        return None

    # 过滤评分 >=60，按 final_score 降序
    scored = [c for c in candidates if (c.get("final_score") or 0) >= 60]
    scored.sort(key=lambda c: -(c.get("final_score") or 0))
    top3 = scored[:3]
    if not top3:
        return None

    lines = ["📗 **今日买入关注**"]
    for i, c in enumerate(top3, 1):
        code = c.get("code", "??????")
        name = c.get("name", "?")
        price = c.get("price", 0)
        score = c.get("final_score", 0)
        cond = _pick_condition(c)
        lines.append(f" {i}. {code} {name} ¥{price:.2f} [评分{score:.0f}] {cond}")
    return "\n".join(lines)


# ─── 卖出信号 ────────────────────────────────────

def gen_sell_signals(target_date, name_cache=None):
    """
    读取 simulated_trading/state.json positions，
    逐仓计算盈亏率，标注亏损仓。
    """
    state = read_json("simulated_trading/state.json")
    if not state:
        return None

    positions = state.get("positions", {})
    if not positions:
        return None

    if name_cache is None:
        name_cache = _build_name_cache(target_date)

    lines = ["📕 **卖出/止损关注**"]
    has_sell = False
    for code, pos in sorted(positions.items()):
        qty = pos.get("quantity", 0)
        if qty == 0:
            continue
        avg_cost = pos.get("avg_cost", 0)
        cur_price = pos.get("current_price", 0)
        pnl_pct = (cur_price - avg_cost) / avg_cost * 100 if avg_cost else 0

        name = resolve_name(code, name_cache)

        if pnl_pct >= 0:
            continue  # 只输出亏损仓

        has_sell = True
        if pnl_pct <= -3.0:
            lines.append(f" · {code} {name} ¥{cur_price:.2f} ({pnl_pct:+.1f}%) 🔴 建议止损")
        else:
            lines.append(f" · {code} {name} ¥{cur_price:.2f} ({pnl_pct:+.1f}%) 观察中")

    if not has_sell:
        lines.append("  无持仓需止损 ✅")
    return "\n".join(lines)


# ─── 持仓状态 ────────────────────────────────────

def gen_position_summary(target_date, name_cache=None):
    """
    读取 simulated_trading/state.json，
    输出总权益 + 逐仓概览。
    """
    state = read_json("simulated_trading/state.json")
    if not state:
        return None

    if name_cache is None:
        name_cache = _build_name_cache(target_date)

    total_equity = state.get("total_equity", 0)
    cash = state.get("cash", 0)
    total_return = state.get("total_return_pct", 0)
    positions = state.get("positions", {})
    last_update = state.get("last_update", yyyymmdd(target_date or date.today()))

    lines = [f"📘 **持仓状态** (更新: {last_update})"]
    lines.append(f" 💰 总权益 ¥{total_equity:.2f} | 现金 ¥{cash:.2f} | 累计 {total_return:+.2f}%")

    if positions:
        for code, pos in sorted(positions.items()):
            qty = pos.get("quantity", 0)
            if qty == 0:
                continue
            avg_cost = pos.get("avg_cost", 0)
            cur_price = pos.get("current_price", 0)
            name = resolve_name(code, name_cache)
            pnl_pct = (cur_price - avg_cost) / avg_cost * 100 if avg_cost else 0
            mkt_val = qty * cur_price
            lines.append(f" · {code} {name} {qty}股 ¥{mkt_val:.0f} ({pnl_pct:+.1f}%)")
    else:
        lines.append("  空仓")

    return "\n".join(lines)


# ─── 极值告警 ────────────────────────────────────

def gen_extreme_warning(target_date=None):
    """
    读取 sentiment_engine/sentiment_{YYYYMMDD}.txt，
    如果恐慌 ≤ 20 或贪婪 ≥ 80，输出告警。
    """
    d = target_date or date.today()
    ds = yyyymmdd(d)
    text = read_text(f"sentiment_engine/sentiment_{ds}.txt")
    if not text:
        return None

    value = parse_sentiment_value(text)
    if value is None:
        return None

    if value <= 20:
        return f"⚠️ **市场恐慌！** 情绪指数 {value}/100，建议减仓观望"
    elif value >= 80:
        return f"⚠️ **市场贪婪！** 情绪指数 {value}/100，注意回调风险"
    return None


# ─── 主入口 ────────────────────────────────────

def main():
    target_date = date.today()
    hour = None

    # 解析命令行参数
    args = sys.argv[1:]
    for arg in args:
        if arg.startswith("--hour="):
            try:
                hour = int(arg.split("=", 1)[1])
            except (ValueError, IndexError):
                pass
        elif arg.startswith("--date="):
            try:
                ds = arg.split("=", 1)[1]
                target_date = datetime.strptime(ds, "%Y%m%d").date()
            except (ValueError, IndexError):
                pass
        elif re.match(r"^\d{8}$", arg):
            target_date = datetime.strptime(arg, "%Y%m%d").date()

    if hour is None:
        hour = datetime.now().hour

    # 构建名称缓存（在首屏加载一次即可）
    name_cache = _build_name_cache(target_date)

    # 时段分支
    if hour == 9:
        # 09:25 早盘 → 只推买入关注
        buy = gen_buy_signals(target_date, name_cache)
        if buy:
            print(buy)
        return

    if hour == 11:
        # 11:30 午盘 → 推午盘快照简报
        from datetime import datetime as dt_module
        now_str = dt_module.now().strftime('%H:%M')

        # 尝试读取当天 report 中的分析结果摘要
        report_path = os.path.join(BASE_DIR, f'reports/report_{yyyymmdd(target_date)}.md')
        buy_lines = []
        sell_lines = []
        neutral_lines = []
        if os.path.isfile(report_path):
            with open(report_path, 'r', encoding='utf-8') as f:
                content = f.read()
            # 从报告头部提取结果摘要
            for line in content.split('\n'):
                line = line.strip()
                # 🟢 / 🟡 / 🔴 + 股票名 + 评分
                m = re.search(r'([🟢🔴🟡])\s*\*\*(.+?)\(?(\d{6})\)?\*\*?:?\s*(.+?)评分\s*(\d+)', line)
                if m:
                    emoji = m.group(1)
                    name = m.group(2).strip()
                    code = m.group(3)
                    action = m.group(4).strip()
                    score = m.group(5)
                    action_clean = action.rstrip(' |').strip()
                    entry = f"· {code} {name} [{action_clean}] 评分{score}"
                    if emoji == '🟢':
                        buy_lines.append(entry)
                    elif emoji == '🔴':
                        sell_lines.append(entry)
                    elif emoji == '🟡':
                        neutral_lines.append(entry)

            # 如果报告头部的正则没匹配到, 再试更宽松的
            if not buy_lines and not sell_lines and not neutral_lines:
                for line in content.split('\n'):
                    m = re.search(r'([🟢🔴🟡])\s*(\S+?)\(?(\d{6})\)?\s*[:：]?\s*(\S+?)\s*[|│]\s*评分\s*(\d+)', line)
                    if m:
                        emoji = m.group(1)
                        name = m.group(2).strip()
                        code = m.group(3)
                        action = m.group(4)
                        score = m.group(5)
                        entry = f"· {code} {name} [{action}] 评分{score}"
                        if emoji == '🟢':
                            buy_lines.append(entry)
                        elif emoji == '🔴':
                            sell_lines.append(entry)
                        elif emoji == '🟡':
                            neutral_lines.append(entry)

        # 尝试读取大盘复盘获取一句话市场概况
        market_line = None
        market_review_path = os.path.join(BASE_DIR, f'reports/market_review_{yyyymmdd(target_date)}.md')
        if os.path.isfile(market_review_path):
            with open(market_review_path, 'r', encoding='utf-8') as f:
                for line in f:
                    m = re.search(r'\*{0,2}核心原因\*{0,2}[：:](.+)', line)
                    if m:
                        market_line = '📈 ' + m.group(1).strip()
                        break
            if not market_line:
                with open(market_review_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                m = re.search(r'盘面温度[：:].+?(\d+)/100', content)
                if m:
                    temp = int(m.group(1))
                    direction = '🟢' if temp >= 60 else '🟡' if temp >= 40 else '🔴'
                    market_line = f'{direction} 盘面温度 {temp}/100'

        parts = []

        # 午盘快照标题
        lines = [f"📊 **午盘快照** ({now_str})"]
        if market_line:
            lines.append(market_line)
        parts.append('\n'.join(lines))

        # 买入关注
        if buy_lines:
            parts.append("🟢 **买入/看多**\n" + '\n'.join(buy_lines[:5]))

        # 持有关注
        if neutral_lines:
            parts.append("🟡 **持有/观望**\n" + '\n'.join(neutral_lines[:5]))

        # 卖出提示
        if sell_lines:
            parts.append("🔴 **卖出/看空**\n" + '\n'.join(sell_lines[:3]))

        # 持仓状态
        pos = gen_position_summary(target_date, name_cache)
        if pos:
            parts.append(pos)

        if len(parts) > 1:
            print('\n\n'.join(parts))
        return

    if hour == 18:
        # 18:00 收盘 → 完整三句话
        parts = []

        buy = gen_buy_signals(target_date, name_cache)
        sell = gen_sell_signals(target_date, name_cache)
        pos = gen_position_summary(target_date, name_cache)

        if buy:
            parts.append(buy)
        if sell:
            parts.append(sell)
        if pos:
            parts.append(pos)

        # 极值告警
        warning = gen_extreme_warning(target_date)
        if warning:
            parts.append(warning)

        if parts:
            print("\n\n".join(parts))
            sys.stderr.write(f"[generate_signal_push] 18:00 推送完成\n")
        return

    # 其他时段 fallback
    buy = gen_buy_signals(target_date, name_cache)
    sell = gen_sell_signals(target_date, name_cache)
    pos = gen_position_summary(target_date, name_cache)
    parts = [p for p in [buy, sell, pos] if p]
    if parts:
        print("\n\n".join(parts))


if __name__ == "__main__":
    main()
