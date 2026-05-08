# -*- coding: utf-8 -*-
"""
===================================
A股市场情绪指数 (Market Sentiment Index)
===================================

核心函数：
- compute_sentiment_index() → 计算当日市场情绪指数 0-100
- get_sentiment_report() → 生成完整情绪报告文本
- get_sentiment_adjustment() → 返回模拟交易的仓位调节系数

情绪指数 = 0.25 × 涨跌比情绪 + 0.20 × 北向资金情绪 + 0.20 × 主力资金情绪
         + 0.20 × 涨停跌停比情绪 + 0.15 × 两融/成交量情绪

每个子维度标准化到 0-100:
- 涨跌比情绪: 上涨占比映射 0-100（中性50 = 50%上涨）
- 北向资金情绪: 净流入>30亿偏向贪婪
- 主力资金情绪: 净流入>0偏向乐观
- 涨停跌停情绪: 涨停占比映射
- 成交量情绪: 相对5日均量变化
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 数据目录 ──
BASE_DIR = Path("/opt/daily_stock_analysis")
SENTIMENT_DATA_DIR = BASE_DIR / "sentiment_engine"
SENTIMENT_HISTORY_FILE = SENTIMENT_DATA_DIR / "history.json"

# ── 情绪标签 ──
class SentimentLabel(str, Enum):
    EXTREME_FEAR = "extreme_fear"
    FEAR = "fear"
    NEUTRAL = "neutral"
    GREED = "greed"
    EXTREME_GREED = "extreme_greed"

SENTIMENT_LABELS_ZH = {
    SentimentLabel.EXTREME_FEAR: "极度恐慌",
    SentimentLabel.FEAR: "恐慌",
    SentimentLabel.NEUTRAL: "中性",
    SentimentLabel.GREED: "贪婪",
    SentimentLabel.EXTREME_GREED: "极度贪婪",
}

SENTIMENT_LABELS_EN = {
    SentimentLabel.EXTREME_FEAR: "Extreme Fear",
    SentimentLabel.FEAR: "Fear",
    SentimentLabel.NEUTRAL: "Neutral",
    SentimentLabel.GREED: "Greed",
    SentimentLabel.EXTREME_GREED: "Extreme Greed",
}

SENTIMENT_COLORS = {
    SentimentLabel.EXTREME_FEAR: "🔴",
    SentimentLabel.FEAR: "🟠",
    SentimentLabel.NEUTRAL: "🟡",
    SentimentLabel.GREED: "🟢",
    SentimentLabel.EXTREME_GREED: "🟢",
}

# ── 仓位调节系数 ──
# 市场极度恐慌时加仓系数，极度贪婪时减仓系数
SENTIMENT_ADJUSTMENT = {
    SentimentLabel.EXTREME_FEAR: 1.3,    # 恐慌→加仓
    SentimentLabel.FEAR: 1.1,
    SentimentLabel.NEUTRAL: 1.0,
    SentimentLabel.GREED: 0.8,
    SentimentLabel.EXTREME_GREED: 0.5,   # 贪婪→减仓
}


@dataclass
class SentimentResult:
    """当日情绪指数结果"""
    date: str                           # YYYYMMDD
    composite_index: int                # 综合情绪指数 0-100
    label: SentimentLabel               # 情绪标签
    trend: str                          # warming/cooling/stable 情绪趋势
    
    # 子维度
    breadth_sentiment: int              # 涨跌比情绪 0-100
    north_flow_sentiment: int = 50      # 北向资金情绪 0-100
    main_force_sentiment: int = 50      # 主力资金情绪 0-100
    limit_sentiment: int = 50           # 涨停跌停比情绪 0-100
    volume_sentiment: int = 50          # 成交量情绪 0-100
    
    # 原始数据（可选）
    raw_data: Dict[str, Any] = field(default_factory=dict)


def _get_label(score: int) -> SentimentLabel:
    """将情绪分数映射到标签。"""
    if score < 20:
        return SentimentLabel.EXTREME_FEAR
    elif score < 40:
        return SentimentLabel.FEAR
    elif score < 60:
        return SentimentLabel.NEUTRAL
    elif score < 80:
        return SentimentLabel.GREED
    else:
        return SentimentLabel.EXTREME_GREED


def _get_trend(current: int, history: List[SentimentResult]) -> str:
    """根据历史趋势判断情绪方向。"""
    if len(history) < 2:
        return "stable"
    # 看最近3个交易日（含今日已保存）
    recent = [r for r in history[-3:] if r.date != current]
    if len(recent) < 2:
        return "stable"
    avg_prev = sum(r.composite_index for r in recent[:-1]) / len(recent[:-1])
    latest = recent[-1].composite_index
    diff = current - avg_prev
    if diff >= 5:
        return "warming"
    elif diff <= -5:
        return "cooling"
    return "stable"


# ═══════════════════════════════════════════════
# 数据获取（从报告/缓存中读取）
# ═══════════════════════════════════════════════

def _load_market_overview(report_date: str) -> Optional[Dict[str, Any]]:
    """从大盘复盘报告解析关键数据。"""
    review_file = BASE_DIR / f"reports/market_review_{report_date}.md"
    if not review_file.exists():
        return None
    
    with open(review_file, encoding="utf-8") as f:
        content = f.read()
    
    data = {}
    
    # 从表格行解析：上涨/下跌/平盘 | 3520 / 1832 / 143
    m = __import__('re').search(r'\|\s*上涨/下跌/平盘\s*\|\s*(\d+)\s*/\s*(\d+)', content)
    if m:
        data['up_count'] = int(m.group(1))
        data['down_count'] = int(m.group(2))
    
    # 从表格行解析：涨停/跌停 | 126 / 53
    m = __import__('re').search(r'\|\s*涨停/跌停\s*\|\s*(\d+)\s*/\s*(\d+)', content)
    if m:
        data['limit_up'] = int(m.group(1))
        data['limit_down'] = int(m.group(2))
    
    # 从表格行解析：两市成交额 | 31683 亿
    m = __import__('re').search(r'\|\s*两市成交额\s*\|\s*([\d,]+)', content)
    if m:
        data['total_amount'] = float(m.group(1).replace(',', ''))
    
    # 备选：从盘面文字中解析（无表格时）
    if 'up_count' not in data:
        for line in content.split('\n'):
            m = __import__('re').search(r'涨[跌]?家数比[为：]\s*(\d+)[：:](\d+)', line)
            if m:
                data['up_count'] = int(m.group(1))
                data['down_count'] = int(m.group(2))
                break
    
    if 'limit_up' not in data:
        for line in content.split('\n'):
            m = __import__('re').search(r'涨停[^\d]*(\d+)', line)
            if m and not line.startswith('#'):
                data['limit_up'] = int(m.group(1))
            m = __import__('re').search(r'跌停[^\d]*(\d+)', line)
            if m and not line.startswith('#'):
                data['limit_down'] = int(m.group(1))
    
    if 'total_amount' not in data:
        m = __import__('re').search(r'成交[额量][\s\S]*?([\d,]+)\s*亿', content)
        if m:
            data['total_amount'] = float(m.group(1).replace(',', ''))
    
    # 解析指数涨跌幅（从指数表格行）
    index_changes = []
    for line in content.split('\n'):
        if '|' not in line:
            continue
        is_index_row = any(kw in line for kw in ['上证指数', '深证成指', '创业板指', '沪深300'])
        if not is_index_row:
            continue
        cells = [c.strip() for c in line.split('|') if c.strip()]
        for cell in cells:
            m = __import__('re').search(r'([+-]?\d+\.\d+)%', cell)
            if m:
                try:
                    index_changes.append(float(m.group(1)))
                    break
                except ValueError:
                    pass
    if index_changes:
        data['avg_index_change'] = sum(index_changes) / len(index_changes)
    
    logger.info(f"[情绪引擎] 解析报告: up={data.get('up_count','?')} down={data.get('down_count','?')} "
                f"limit_up={data.get('limit_up','?')} limit_down={data.get('limit_down','?')} "
                f"amount={data.get('total_amount','?')}")
    
    return data if data else None


def _load_history() -> List[Dict[str, Any]]:
    """加载情绪历史数据。"""
    if SENTIMENT_HISTORY_FILE.exists():
        with open(SENTIMENT_HISTORY_FILE) as f:
            return json.load(f)
    return []


def _save_history(records: List[Dict[str, Any]]):
    """保存情绪历史数据。"""
    SENTIMENT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 只保留最近60天
    with open(SENTIMENT_HISTORY_FILE, "w") as f:
        json.dump(records[-60:], f, ensure_ascii=False, indent=2)


def _compute_breadth_sentiment(up_count: int, down_count: int) -> int:
    """涨跌比 → 情绪分数。中性50对应50%上涨。"""
    total = up_count + down_count
    if total == 0:
        return 50
    ratio = up_count / total
    # 映射：0%→0, 50%→50, 70%→70, 100%→100
    return int(max(0, min(100, ratio * 100)))


def _compute_limit_sentiment(limit_up: int, limit_down: int) -> int:
    """涨停跌停比 → 情绪分数。"""
    total = limit_up + limit_down
    if total == 0:
        return 50  # 无涨停跌停=中性
    ratio = limit_up / total
    # 映射：0%→0, 50%→50, 80%→80, 100%→100
    return int(max(0, min(100, ratio * 100)))


def _compute_volume_sentiment(amount: float) -> int:
    """成交额情绪——参考今日相对近期均量的比例。
    暂无5日均量数据时直接基于成交额绝对值做大致判断。"""
    if amount <= 0:
        return 50
    # A股正常日成交额约8000-15000亿，缩量<6000为冷清，放量>20000为亢奋
    if amount >= 25000:
        return 80  # 极度亢奋
    elif amount >= 15000:
        return 65  # 放量活跃
    elif amount >= 8000:
        return 50  # 正常
    elif amount >= 5000:
        return 35  # 缩量
    else:
        return 20  # 极度缩量


def _compute_north_flow_sentiment(report_date: str) -> int:
    """北向资金情绪——尝试从已有数据提取。
    暂无API直接调用，使用中性值50并标记。
    如数据源可用可替换为真实值。"""
    # 尝试从复盘报告中提取北向数据
    review_file = BASE_DIR / f"reports/market_review_{report_date}.md"
    if review_file.exists():
        with open(review_file, encoding="utf-8") as f:
            content = f.read()
        m = __import__('re').search(r'北向[^：:]*[：:]\s*([+-]?\d+\.?\d*)', content)
        if m:
            try:
                flow = float(m.group(1))
                if flow > 50:
                    return 80
                elif flow > 20:
                    return 65
                elif flow > 0:
                    return 55
                elif flow > -20:
                    return 40
                elif flow > -50:
                    return 30
                else:
                    return 20
            except ValueError:
                pass
    return 50  # 无数据时中性


def _compute_main_force_sentiment(report_date: str) -> int:
    """主力资金情绪——尝试从报告提取。"""
    review_file = BASE_DIR / f"reports/market_review_{report_date}.md"
    if review_file.exists():
        with open(review_file, encoding="utf-8") as f:
            content = f.read()
        m = __import__('re').search(r'主力[^：:]*[：:]\s*([+-]?\d+\.?\d*)', content)
        if m:
            try:
                flow = float(m.group(1))
                if flow > 100:
                    return 80
                elif flow > 30:
                    return 65
                elif flow > 0:
                    return 55
                elif flow > -30:
                    return 40
                elif flow > -100:
                    return 30
                else:
                    return 20
            except ValueError:
                pass
    return 50


# ═══════════════════════════════════════════════
# 核心函数
# ═══════════════════════════════════════════════

def compute_sentiment_index(
    report_date: Optional[str] = None,
    overview: Optional[Dict[str, Any]] = None,
) -> SentimentResult:
    """
    计算当日市场情绪指数。

    Args:
        report_date: 日期 YYYYMMDD，默认今天
        overview: 可选的预加载概览数据（如从pipeline传入）
    
    Returns:
        SentimentResult 包含综合情绪指数及各子维度
    """
    if report_date is None:
        report_date = date.today().strftime("%Y%m%d")
    
    # 获取数据
    data = overview or _load_market_overview(report_date) or {}
    
    up_count = data.get('up_count', 0)
    down_count = data.get('down_count', 0)
    limit_up = data.get('limit_up', 0)
    limit_down = data.get('limit_down', 0)
    total_amount = data.get('total_amount', 0)
    
    # 计算各子维度
    breadth = _compute_breadth_sentiment(up_count, down_count)
    limit_s = _compute_limit_sentiment(limit_up, limit_down)
    volume = _compute_volume_sentiment(total_amount)
    north = _compute_north_flow_sentiment(report_date)
    main_force = _compute_main_force_sentiment(report_date)
    
    # 综合：涨跌比25% + 北向20% + 主力20% + 涨停跌停20% + 成交量15%
    composite = int(round(
        breadth * 0.25 +
        north * 0.20 +
        main_force * 0.20 +
        limit_s * 0.20 +
        volume * 0.15
    ))
    # 钳位到 0-100
    composite = max(0, min(100, composite))
    
    # 情绪标签
    label = _get_label(composite)
    
    # 加载历史判断趋势
    history = _load_history()
    past_results = [SentimentResult(**r) if isinstance(r, dict) else r for r in history]
    trend = _get_trend(composite, past_results)
    
    result = SentimentResult(
        date=report_date,
        composite_index=composite,
        label=label,
        trend=trend,
        breadth_sentiment=breadth,
        north_flow_sentiment=north,
        main_force_sentiment=main_force,
        limit_sentiment=limit_s,
        volume_sentiment=volume,
        raw_data={
            'up_count': up_count,
            'down_count': down_count,
            'limit_up': limit_up,
            'limit_down': limit_down,
            'total_amount': total_amount,
        },
    )
    
    logger.info(
        f"[情绪指数] {report_date} 综合:{composite}({label.value}) "
        f"趋势:{trend} | "
        f"涨跌比:{breadth} 北向:{north} 主力:{main_force} "
        f"涨跌停:{limit_s} 成交量:{volume}"
    )
    
    # 去重后保存历史（同一日期只保留最新一条）
    history = [r for r in history if r.get('date') != result.date]
    history.append({
        "date": result.date,
        "composite_index": result.composite_index,
        "label": result.label.value,
        "trend": result.trend,
        "breadth_sentiment": result.breadth_sentiment,
        "north_flow_sentiment": result.north_flow_sentiment,
        "main_force_sentiment": result.main_force_sentiment,
        "limit_sentiment": result.limit_sentiment,
        "volume_sentiment": result.volume_sentiment,
    })
    _save_history(history)
    
    return result


def get_sentiment_adjustment(report_date: Optional[str] = None) -> Tuple[float, SentimentLabel, int]:
    """
    获取用于模拟交易的仓位调节系数。

    Returns:
        (adjustment_factor, label, score)
        例如 (0.8, SentimentLabel.GREED, 72) → 市场贪婪，仓位x0.8
    """
    result = compute_sentiment_index(report_date)
    factor = SENTIMENT_ADJUSTMENT.get(result.label, 1.0)
    return factor, result.label, result.composite_index


def get_sentiment_report(report_date: Optional[str] = None, lang: str = "zh") -> str:
    """
    生成可直接推送的情绪报告文本。

    Args:
        report_date: 日期 YYYYMMDD，默认今天
        lang: "zh" 中文 / "en" 英文

    Returns:
        格式化推送文本
    """
    result = compute_sentiment_index(report_date)
    
    labels = SENTIMENT_LABELS_ZH if lang == "zh" else SENTIMENT_LABELS_EN
    label_text = labels.get(result.label, result.label.value)
    color = SENTIMENT_COLORS.get(result.label, "⚪")
    
    if lang == "zh":
        trend_map = {"warming": "升温", "cooling": "降温", "stable": "平稳"}
        trend_text = trend_map.get(result.trend, result.trend)
        
        lines = [
            f"🌡️【A股市场情绪指数】{result.date[:4]}-{result.date[4:6]}-{result.date[6:]}",
            f"",
            f"{color} **综合情绪指数：{result.composite_index}/100** — {label_text}",
            f"📈 情绪趋势：{trend_text}",
            f"",
            f"**子维度分解：**",
            f"  · 涨跌比情绪：{result.breadth_sentiment}/100",
            f"  · 北向资金情绪：{result.north_flow_sentiment}/100",
            f"  · 主力资金情绪：{result.main_force_sentiment}/100",
            f"  · 涨停跌停情绪：{result.limit_sentiment}/100",
            f"  · 成交量情绪：{result.volume_sentiment}/100",
        ]
        
        # 加上交易建议
        if result.composite_index >= 70:
            lines.append(f"")
            lines.append("⚠️ **市场情绪亢奋，注意追高风险，建议控制仓位。**")
        elif result.composite_index >= 55:
            lines.append(f"")
            lines.append("✅ **市场情绪偏暖，可适度参与，注意节奏。**")
        elif result.composite_index >= 40:
            lines.append(f"")
            lines.append("➡️ **市场情绪中性，多看少动，等待方向确认。**")
        elif result.composite_index >= 20:
            lines.append(f"")
            lines.append("🔍 **市场情绪偏弱，恐慌中或藏机会，关注左侧信号。**")
        else:
            lines.append(f"")
            lines.append("🔥 **市场极度恐慌！若非系统性风险，可能是较好的左侧介入机会。**")
        
        # 加历史比较
        history = _load_history()
        if len(history) >= 5:
            recent_5 = history[-6:-1]  # 不含今日
            if recent_5:
                avg_5 = sum(r.get('composite_index', 50) for r in recent_5) / len(recent_5)
                diff = result.composite_index - avg_5
                arrow = "📈" if diff > 0 else "📉"
                lines.append(f"")
                lines.append(f"📊 对比近5日均值({avg_5:.0f}/100)：{arrow} {diff:+.0f}点")
        
        # 仓位建议
        factor = SENTIMENT_ADJUSTMENT.get(result.label, 1.0)
        if factor != 1.0:
            pct = int((factor - 1.0) * 100)
            sign = "+" if pct > 0 else ""
            lines.append(f"")
            lines.append(f"🎯 **仓位调节建议：{sign}{pct}%**（模拟交易自动应用）")
        
    else:
        lines = [
            f"🌡️ **A-Share Market Sentiment Index** — {result.date[:4]}-{result.date[4:6]}-{result.date[6:]}",
            f"",
            f"{color} **Composite Score: {result.composite_index}/100** — {label_text}",
            f"📈 Trend: {result.trend}",
            f"",
            f"**Breakdown:**",
            f"  · Breadth: {result.breadth_sentiment}/100",
            f"  · North-bound Flow: {result.north_flow_sentiment}/100",
            f"  · Main Force Flow: {result.main_force_sentiment}/100",
            f"  · Limit-up/Down: {result.limit_sentiment}/100",
            f"  · Volume: {result.volume_sentiment}/100",
        ]
    
    return "\n".join(lines)


def get_sentiment_history(days: int = 20) -> List[Dict[str, Any]]:
    """获取最近N天的情绪历史。"""
    history = _load_history()
    return history[-days:]


def print_sentiment_curve(days: int = 20) -> str:
    """打印ASCII情绪曲线（用于调试/展示）。"""
    history = _load_history()
    records = history[-days:]
    if not records:
        return "暂无情绪历史数据"
    
    lines = ["📈 情绪指数曲线（最近{}天）：".format(len(records)), ""]
    for r in records:
        d = r['date']
        s = r['composite_index']
        bar_len = s // 5
        bar = "█" * bar_len + "░" * (20 - bar_len)
        label_zh = SENTIMENT_LABELS_ZH.get(SentimentLabel(r['label']), r['label'])
        lines.append(f"  {d[4:6]}-{d[6:]} {s:3d} |{bar}| {label_zh}")
    
    return "\n".join(lines)


# ═══════════════════════════════════════════════
# 独立入口：在 run_and_send.sh 中调用
# ═══════════════════════════════════════════════

def main():
    """独立运行入口，生成情绪报告并保存文件。"""
    report_date = date.today().strftime("%Y%m%d")
    
    # 生成情绪报告
    report = get_sentiment_report(report_date, lang="zh")
    
    # 保存到文件供 run_and_send.sh 读取
    SENTIMENT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    report_file = SENTIMENT_DATA_DIR / f"sentiment_{report_date}.txt"
    report_file.write_text(report, encoding="utf-8")
    
    print(report)
    print()
    print(print_sentiment_curve(20))
    print()
    
    # 打印仓位调节系数
    factor, label, score = get_sentiment_adjustment(report_date)
    label_zh = SENTIMENT_LABELS_ZH.get(label, label.value)
    print(f"[情绪引擎] 仓位调节系数：{factor}（{label_zh}，情绪指数{score}）")
    
    # 保存系数到JSON供simulated_trading读取
    adj_file = SENTIMENT_DATA_DIR / "adjustment.json"
    with open(adj_file, "w") as f:
        json.dump({
            "date": report_date,
            "factor": factor,
            "label": label.value,
            "score": score,
        }, f)
    
    print(f"[情绪引擎] 情绪报告已保存至 {report_file}")
    print(f"[情绪引擎] 仓位系数已保存至 {adj_file}")


if __name__ == "__main__":
    main()
