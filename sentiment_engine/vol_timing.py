#!/usr/bin/env python3
"""
波动率择时模块 — Volatility Timing
======================================
核心逻辑：当20日滚动波动率处于历史高分位 → 减仓；低分位 → 满仓。
零参数调优，只基于滚动历史百分位。

输出:
  - sentiment_engine/vol_timing.json  → 供 simulated_trading 读取
  - 终端/日志输出信号

集成:
  python3 -m sentiment_engine.vol_timing
"""
import sys
import json
import logging
from pathlib import Path
from datetime import datetime

sys.path.insert(0, "/opt/daily_stock_analysis")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [vol_timing] %(levelname)s %(message)s")
logger = logging.getLogger("vol_timing")

import numpy as np
import pandas as pd

BASE_DIR = Path("/opt/daily_stock_analysis")
CACHE_DIR = BASE_DIR / "data" / "cache"
OUTPUT_DIR = BASE_DIR / "sentiment_engine"
HS300_PKL = CACHE_DIR / "hs300_index.pkl"

# ── 参数 ──
VOL_WINDOW = 20          # 波动率计算窗口
HISTORY_DAYS = 120       # 历史百分位计算窗口（够形成稳定分布即可）

# 仓位映射（百分位 → scale）
# 百分位越高（波动越大）→ 仓位越低
POSITION_MAP = [
    (0.80, 0.25),   # 前20%高波动 → 25%仓位
    (0.60, 0.50),   # 前40%高波动 → 50%仓位
    (0.30, 0.75),   # 前70%高波动 → 75%仓位
    (0.00, 1.00),   # 低波动 → 满仓
]

# 信号等级标签
SIGNAL_LABELS = {
    (0.00, 0.30): ("🟢", "低波动", "市场平稳，建议满仓操作"),
    (0.30, 0.60): ("🟡", "中等波动", "市场正常波动，建议适度仓位"),
    (0.60, 0.80): ("🟠", "偏高波动", "市场波动加剧，建议降低仓位"),
    (0.80, 1.00): ("🔴", "高波动", "市场剧烈波动，建议轻仓观望"),
}


def load_hs300() -> pd.DataFrame:
    """加载沪深300指数数据"""
    if not HS300_PKL.exists():
        logger.error(f"HS300指数数据不存在: {HS300_PKL}")
        return None
    df = pd.read_pickle(str(HS300_PKL))
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def compute_volatility_signal(df: pd.DataFrame) -> dict:
    """
    计算波动率择时信号

    Returns:
        dict 含：
          - date: 信号日期
          - vol_20d: 当前20日波动率（日收益率标准差）
          - vol_annualized: 年化波动率
          - historical_pct: 历史百分位 (0~1, 越高波动越大)
          - position_scale: 建议仓位 (0.25~1.0)
          - signal_label: 信号等级名称
          - signal_icon: 信号图标
          - vol_percentile_curve: 近60日百分位走势
    """
    if df is None or len(df) < VOL_WINDOW + 20:
        return {"error": "数据不足", "position_scale": 1.0}

    # 计算日收益率和滚动波动率
    df = df.copy()
    df["returns"] = df["close"].pct_change()
    df["vol_20d"] = df["returns"].rolling(VOL_WINDOW).std()

    # 当天
    today = df.iloc[-1]
    today_date = today["date"].strftime("%Y-%m-%d")
    current_vol = today["vol_20d"]

    if pd.isna(current_vol):
        logger.warning("当前波动率无效，返回默认信号")
        return {"date": today_date, "position_scale": 1.0, "error": "波动率N/A"}

    # 年化波动率
    vol_annualized = current_vol * np.sqrt(244)

    # 历史百分位：在过去 HISTORY_DAYS 天中的排名
    vol_history = df["vol_20d"].dropna().tail(HISTORY_DAYS).values
    pct_rank = (vol_history > current_vol).mean()  # 高于当前的比例
    # 转换成"低百分位=低波动"的标准百分位
    historical_pct = 1.0 - pct_rank  # 0=历史最低波动, 1=历史最高波动

    if len(vol_history) < 20:
        logger.warning("波动率历史序列太短")
        historical_pct = 0.5

    # 映射到仓位
    position_scale = 1.0
    for threshold, scale in POSITION_MAP:
        if historical_pct >= threshold:
            position_scale = scale
            break

    # 信号标签
    signal_label = "未知"
    signal_icon = "⚪"
    signal_desc = ""
    for (lo, hi), (icon, label, desc) in SIGNAL_LABELS.items():
        if lo <= historical_pct < hi:
            signal_label = label
            signal_icon = icon
            signal_desc = desc
            break
    if historical_pct >= 0.80:
        signal_label = "高波动"
        signal_icon = "🔴"
        signal_desc = "市场剧烈波动，建议轻仓观望"

    # 近60日百分位走势（用于分析趋势）
    vol_curve = []
    for i in range(min(60, len(vol_history)), 0, -1):
        if i <= len(vol_history):
            sub = vol_history[:i]
            if len(sub) > 0:
                p = (sub > vol_history[i - 1]).mean()
                vol_curve.append(round(1.0 - p, 2))

    result = {
        "date": today_date,
        "date_ymd": today["date"].strftime("%Y%m%d"),
        "vol_20d": round(current_vol, 6),
        "vol_annualized_pct": round(vol_annualized * 100, 2),
        "historical_percentile": round(historical_pct, 3),
        "position_scale": position_scale,
        "signal_label": signal_label,
        "signal_icon": signal_icon,
        "signal_description": signal_desc,
        "hs300_close": round(today["close"], 2),
        "hs300_change_pct": round(today.get("returns", 0) * 100, 2),
        "vol_percentile_curve": vol_curve,
    }
    return result


def save_signal(signal: dict):
    """保存信号到 JSON 文件"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUTPUT_DIR / "vol_timing.json"
    with open(out_file, "w") as f:
        json.dump(signal, f, ensure_ascii=False, indent=2)
    logger.info(f"已保存波动率信号: {out_file}")

    # 同时更新 adjustment.json（合并写入，保留情绪引擎字段）
    adj_file = OUTPUT_DIR / "adjustment.json"
    existing = {}
    if adj_file.exists():
        try:
            existing = json.loads(adj_file.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            pass
    existing.update({
        "vol_timing_factor": signal["position_scale"],
        "vol_timing_label": signal["signal_label"],
        "vol_timing_percentile": signal["historical_percentile"],
        "vol_timing_date": signal.get("date_ymd", signal["date"]),
        # 仍然设置 factor 供 simulated_trading 读取，但不覆盖已有的情绪 score
        "factor": signal["position_scale"],
    })
    with open(adj_file, "w") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    logger.info(f"合并写入 adjustment.json: vol_timing_factor={signal['position_scale']}")


def generate_report(signal: dict) -> str:
    """生成可推送的文本报告"""
    if "error" in signal:
        return f"⚠️ 波动率择时信号异常: {signal['error']}"

    icon = signal["signal_icon"]
    label = signal["signal_label"]
    scale = signal["position_scale"]
    pct = signal["historical_percentile"] * 100
    vol_ann = signal["vol_annualized_pct"]
    desc = signal["signal_description"]
    hs300_close = signal["hs300_close"]

    # 仓位建议
    if scale >= 1.0:
        position_advice = "✅ 建议满仓操作"
    elif scale >= 0.75:
        position_advice = "✅ 建议正常仓位(75%)"
    elif scale >= 0.50:
        position_advice = "⚠️ 建议中等仓位(50%)"
    elif scale >= 0.25:
        position_advice = "⚠️ 建议轻仓操作(25%)"
    else:
        position_advice = "🔴 建议空仓观望"

    report = (
        f"🌊 **波动率择时信号**  {signal['date']}\n"
        f"\n"
        f"{icon} **市场状态：{label}**（历史百分位 {pct:.0f}%）\n"
        f"{desc}\n"
        f"\n"
        f"**核心数据：**\n"
        f"  · 沪深300收盘：{hs300_close:.2f} ({signal['hs300_change_pct']:+.2f}%)\n"
        f"  · 20日波动率(年化)：**{vol_ann:.1f}%**\n"
        f"  · 波动率历史百分位：**{pct:.0f}%**（{pct:.0f}% = 比历史{pct:.0f}%的时间都低/高）\n"
        f"\n"
        f"**🎯 仓位建议：{scale:.0%}**\n"
        f"{position_advice}\n"
    )
    return report


def main():
    """主入口"""
    logger.info("=" * 50)
    logger.info("波动率择时信号生成")
    logger.info("=" * 50)

    df = load_hs300()
    if df is None:
        logger.error("数据加载失败，写入默认信号")
        save_signal({"date": datetime.now().strftime("%Y-%m-%d"),
                      "position_scale": 1.0, "signal_label": "默认",
                      "signal_icon": "⚪", "signal_description": "数据异常，默认满仓",
                      "historical_percentile": 0.5, "vol_annualized_pct": 0,
                      "hs300_close": 0, "hs300_change_pct": 0,
                      "vol_percentile_curve": []})
        return 1

    signal = compute_volatility_signal(df)
    if "error" in signal:
        logger.error(f"信号计算失败: {signal['error']}，写入默认信号")
        save_signal({"date": datetime.now().strftime("%Y-%m-%d"),
                      "position_scale": 1.0, "signal_label": "默认",
                      "signal_icon": "⚪", "signal_description": f"计算异常: {signal['error']}，默认满仓",
                      "historical_percentile": 0.5, "vol_annualized_pct": 0,
                      "hs300_close": 0, "hs300_change_pct": 0,
                      "vol_percentile_curve": []})
        return 1

    save_signal(signal)

    # 生成报告
    report = generate_report(signal)
    print("\n" + report + "\n")

    # 日志信息
    logger.info(f"信号: {signal['signal_icon']} {signal['signal_label']} "
                f"| 百分位: {signal['historical_percentile']:.1%} "
                f"| 仓位: {signal['position_scale']:.0%} "
                f"| 年化波动: {signal['vol_annualized_pct']:.1f}%")

    return 0


if __name__ == "__main__":
    sys.exit(main())
