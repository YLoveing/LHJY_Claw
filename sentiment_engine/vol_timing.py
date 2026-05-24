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
BROAD_MARKET_PKL = CACHE_DIR / "market_index" / "000985.pkl"
MARKET_INDEX_CACHE_DIR = CACHE_DIR / "market_index"

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


def load_broad_market() -> pd.DataFrame:
    """加载中证全指(000985)日K线数据，优先读缓存，首次通过akshare获取。"""
    if BROAD_MARKET_PKL.exists():
        try:
            df = pd.read_pickle(str(BROAD_MARKET_PKL))
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
            logger.info(f"中证全指数据从缓存读取: {BROAD_MARKET_PKL} ({len(df)}条)")
            return df
        except Exception as e:
            logger.warning(f"缓存读取失败: {e}，重新获取")

    # 通过 akshare 获取
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily_tx(symbol="sh000985")
        if df is None or len(df) == 0:
            logger.error("中证全指数据为空")
            return None
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        # 缓存
        MARKET_INDEX_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_pickle(str(BROAD_MARKET_PKL))
        logger.info(f"中证全指数据已缓存: {BROAD_MARKET_PKL} ({len(df)}条)")
        return df
    except Exception as e:
        logger.error(f"中证全指数据获取失败: {e}")
        return None


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


def compute_broad_market_signal() -> dict:
    """
    计算中证全指的波动率择时信号，复用 compute_volatility_signal 逻辑。
    返回与 HS300 信号相同结构的 dict。
    """
    df = load_broad_market()
    if df is None:
        logger.error("中证全指数据加载失败，跳过宽基信号")
        return None

    signal = compute_volatility_signal(df)
    if signal is None or "error" in signal:
        logger.error(f"中证全指信号计算失败: {signal.get('error', 'unknown')}")
        return None

    # 将 hs300 相关字段改为 broad_market 通用字段名
    signal["broad_close"] = signal.pop("hs300_close", 0)
    signal["broad_change_pct"] = signal.pop("hs300_change_pct", 0)
    signal["date_source"] = "broad_market"
    logger.info(f"中证全指信号: 百分位={signal['historical_percentile']:.1%} 仓位={signal['position_scale']:.0%}")
    return signal


def save_signal(hs300_signal: dict, broad_market_signal: dict = None):
    """保存信号到 JSON 文件，包含沪深300和中证全指双信号"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 计算 combined 信号（取最保守的 scale）
    hs300_scale = hs300_signal.get("position_scale", 1.0)
    broad_scale = broad_market_signal.get("position_scale", 1.0) if broad_market_signal else 1.0
    combined_scale = min(hs300_scale, broad_scale)

    # ── 写入 vol_timing.json（嵌套结构） ──
    vol_timing = {
        "hs300": {
            "position_scale": hs300_scale,
            "vol_annualized_pct": hs300_signal.get("vol_annualized_pct", 0),
            "historical_percentile": hs300_signal.get("historical_percentile", 0.5),
            "vol_20d": hs300_signal.get("vol_20d", 0),
            "signal_label": hs300_signal.get("signal_label", "默认"),
            "signal_icon": hs300_signal.get("signal_icon", "⚪"),
            "signal_description": hs300_signal.get("signal_description", ""),
            "date": hs300_signal.get("date", ""),
            "date_ymd": hs300_signal.get("date_ymd", ""),
            "hs300_close": hs300_signal.get("hs300_close", 0),
            "hs300_change_pct": hs300_signal.get("hs300_change_pct", 0),
        },
        "combined": {
            "position_scale": combined_scale,
        }
    }

    if broad_market_signal:
        vol_timing["broad_market"] = {
            "position_scale": broad_scale,
            "vol_annualized_pct": broad_market_signal.get("vol_annualized_pct", 0),
            "historical_percentile": broad_market_signal.get("historical_percentile", 0.5),
            "vol_20d": broad_market_signal.get("vol_20d", 0),
            "signal_label": broad_market_signal.get("signal_label", "默认"),
            "signal_icon": broad_market_signal.get("signal_icon", "⚪"),
            "signal_description": broad_market_signal.get("signal_description", ""),
            "broad_close": broad_market_signal.get("broad_close", 0),
            "broad_change_pct": broad_market_signal.get("broad_change_pct", 0),
        }

    out_file = OUTPUT_DIR / "vol_timing.json"
    with open(out_file, "w") as f:
        json.dump(vol_timing, f, ensure_ascii=False, indent=2)
    logger.info(f"已保存波动率信号(双指数): {out_file}")

    # ── 同时更新 adjustment.json（合并写入，保留情绪引擎字段） ──
    adj_file = OUTPUT_DIR / "adjustment.json"
    existing = {}
    if adj_file.exists():
        try:
            existing = json.loads(adj_file.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            pass
    existing.update({
        "vol_timing_factor": combined_scale,
        "vol_timing_label": hs300_signal.get("signal_label", "默认"),
        "vol_timing_percentile": hs300_signal.get("historical_percentile", 0.5),
        "vol_timing_date": hs300_signal.get("date_ymd", hs300_signal.get("date", "")),
        "vol_timing_broad_factor": broad_scale,
        # 设置 factor 供向后兼容
        "factor": combined_scale,
    })
    with open(adj_file, "w") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    logger.info(f"合并写入 adjustment.json: combined_factor={combined_scale}")


def generate_report(hs300_signal: dict, broad_market_signal: dict = None, combined_scale: float = None) -> str:
    """生成可推送的文本报告（双指数对比）"""
    if "error" in hs300_signal:
        return f"⚠️ 波动率择时信号异常: {hs300_signal['error']}"

    icon = hs300_signal["signal_icon"]
    label = hs300_signal["signal_label"]
    scale = hs300_signal["position_scale"]
    pct = hs300_signal["historical_percentile"] * 100
    vol_ann = hs300_signal["vol_annualized_pct"]
    desc = hs300_signal["signal_description"]
    hs300_close = hs300_signal["hs300_close"]

    if combined_scale is None:
        combined_scale = scale

    # 仓位建议
    if combined_scale >= 1.0:
        position_advice = "✅ 建议满仓操作"
    elif combined_scale >= 0.75:
        position_advice = "✅ 建议正常仓位(75%)"
    elif combined_scale >= 0.50:
        position_advice = "⚠️ 建议中等仓位(50%)"
    elif combined_scale >= 0.25:
        position_advice = "⚠️ 建议轻仓操作(25%)"
    else:
        position_advice = "🔴 建议空仓观望"

    lines = [
        f"🌊 **波动率择时信号**  {hs300_signal['date']}\n",
        f"{icon} **沪深300：{label}**（年化波动 {vol_ann:.1f}%，百分位 {pct:.0f}%）",
        f"{desc}",
        f"· 沪深300收盘：{hs300_close:.2f} ({hs300_signal['hs300_change_pct']:+.2f}%)",
    ]

    if broad_market_signal and "error" not in broad_market_signal:
        bm_pct = broad_market_signal["historical_percentile"] * 100
        bm_vol = broad_market_signal["vol_annualized_pct"]
        bm_icon = broad_market_signal["signal_icon"]
        bm_label = broad_market_signal["signal_label"]
        bm_close = broad_market_signal.get("broad_close", 0)
        bm_chg = broad_market_signal.get("broad_change_pct", 0)
        lines.append(f"")
        lines.append(f"{bm_icon} **中证全指：{bm_label}**（年化波动 {bm_vol:.1f}%，百分位 {bm_pct:.0f}%）")
        lines.append(f"· 中证全指收盘：{bm_close:.2f} ({bm_chg:+.2f}%)")

    lines.append(f"")
    lines.append(f"**🎯 综合仓位建议：{combined_scale:.0%}**")
    lines.append(f"{position_advice}")
    lines.append(f"*取沪深300与中证全指中最保守值*")

    return "\n".join(lines)


def main():
    """主入口"""
    logger.info("=" * 50)
    logger.info("波动率择时信号生成（双指数）")
    logger.info("=" * 50)

    # ── 1. 沪深300信号 ──
    df_hs300 = load_hs300()
    hs300_signal = None
    broad_signal = None

    if df_hs300 is not None:
        hs300_signal = compute_volatility_signal(df_hs300)
        if hs300_signal and "error" in hs300_signal:
            logger.error(f"沪深300信号计算失败: {hs300_signal['error']}")
            hs300_signal = None

    if hs300_signal is None:
        logger.error("沪深300数据加载/计算失败")
        hs300_signal = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "date_ymd": datetime.now().strftime("%Y%m%d"),
            "position_scale": 1.0,
            "signal_label": "默认",
            "signal_icon": "⚪",
            "signal_description": "沪深300数据异常，回退默认满仓",
            "historical_percentile": 0.5,
            "vol_annualized_pct": 0,
            "hs300_close": 0,
            "hs300_change_pct": 0,
            "vol_percentile_curve": [],
        }

    # ── 2. 中证全指信号 ──
    try:
        broad_signal = compute_broad_market_signal()
    except Exception as e:
        logger.warning(f"中证全指信号计算异常: {e}，跳过")
        broad_signal = None

    # ── 3. 保存信号 ──
    save_signal(hs300_signal, broad_market_signal=broad_signal)

    # ── 4. 计算 combined 用于报告 ──
    hs300_scale = hs300_signal.get("position_scale", 1.0)
    broad_scale = broad_signal.get("position_scale", 1.0) if broad_signal else 1.0
    combined_scale = min(hs300_scale, broad_scale)

    # ── 5. 生成报告 ──
    report = generate_report(hs300_signal, broad_market_signal=broad_signal, combined_scale=combined_scale)
    print("\n" + report + "\n")

    # 日志信息
    logger.info(f"沪深300: {hs300_signal['signal_icon']} {hs300_signal['signal_label']} "
                f"百分位={hs300_signal['historical_percentile']:.1%} 仓位={hs300_signal['position_scale']:.0%}")
    if broad_signal:
        logger.info(f"中证全指: {broad_signal['signal_icon']} {broad_signal['signal_label']} "
                    f"百分位={broad_signal['historical_percentile']:.1%} 仓位={broad_signal['position_scale']:.0%}")
    logger.info(f"综合仓位: {combined_scale:.0%} (取最保守)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
