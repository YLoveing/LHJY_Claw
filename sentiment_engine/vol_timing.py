#!/usr/bin/env python3
"""
波动率择时模块 v3 — 五因子等权 Volatility Timing
==================================================
核心逻辑：五个因子等权平均（各20%），输出 0.25~1.0 的仓位建议。

五个因子：
  f1. HV（波动率） — 20日滚动波动率 × 60日历史百分位
  f2. 主力资金（价量代理） — 成交额/均价 × 涨跌幅规则
  f3. 量能 — 成交额比的60日历史百分位
  f4. 均线综合 — 价格 vs MA5/20/60/120
  f5. MACD — 快慢线交叉 + 柱状图方向

输出:
  - sentiment_engine/vol_timing.json  → 供 simulated_trading 读取（含 factor 明细）
  - 终端报告

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
HISTORY_DAYS = 120       # 历史百分位计算窗口

# 仓位档位（各因子都输出这四个值之一）
POSITION_TIERS = [0.25, 0.50, 0.75, 1.0]


# ── 数据加载（保持不变） ──

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
    """加载中证全指(000985)日K线数据"""
    if BROAD_MARKET_PKL.exists():
        try:
            df = pd.read_pickle(str(BROAD_MARKET_PKL))
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
            logger.info(f"中证全指数据从缓存读取: {BROAD_MARKET_PKL} ({len(df)}条)")
            return df
        except Exception as e:
            logger.warning(f"缓存读取失败: {e}，重新获取")

    try:
        import akshare as ak
        df = ak.stock_zh_index_daily_tx(symbol="sh000985")
        if df is None or len(df) == 0:
            logger.error("中证全指数据为空")
            return None
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        MARKET_INDEX_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_pickle(str(BROAD_MARKET_PKL))
        logger.info(f"中证全指数据已缓存: {BROAD_MARKET_PKL} ({len(df)}条)")
        return df
    except Exception as e:
        logger.error(f"中证全指数据获取失败: {e}")
        return None


# ── 工具函数 ──

def _round_to_tier(value: float) -> float:
    """四舍五入到最近的 0.25 档位"""
    return round(value * 4) / 4.0


def _percentile_in_window(values: np.ndarray, current: float) -> float:
    """
    计算 current 在 values 中的历史百分位。
    返回 0~1，值越高表示 current 在历史中越高。
    """
    if len(values) < 10:
        return 0.5
    rank = (values > current).mean()
    return 1.0 - rank  # 0=最低, 1=最高


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _signal_label(scale: float) -> tuple:
    """返回 (icon, label)"""
    if scale >= 0.875:
        return "🟢", "积极"
    elif scale >= 0.625:
        return "🟡", "中性偏积极"
    elif scale >= 0.375:
        return "🟠", "中性偏保守"
    else:
        return "🔴", "保守"


# ═══════════════════════════════════════════════
#  五个因子
# ═══════════════════════════════════════════════

def _prepare_broad_data(df: pd.DataFrame) -> pd.DataFrame:
    """
    预计算宽表：returns, vol_20d, amount_ratio, MA, MACD
    返回副本，不修改原 df。
    """
    d = df.copy()
    d["returns"] = d["close"].pct_change()
    d["vol_20d"] = d["returns"].rolling(VOL_WINDOW).std()
    d["amount_ma20"] = d["amount"].rolling(20).mean()
    d["amount_ratio"] = d["amount"] / d["amount_ma20"]

    # 均线
    for p in [5, 20, 60, 120]:
        d[f"ma{p}"] = d["close"].rolling(p).mean()

    # MACD
    d["ema12"] = d["close"].ewm(span=12, adjust=False).mean()
    d["ema26"] = d["close"].ewm(span=26, adjust=False).mean()
    d["macd_line"] = d["ema12"] - d["ema26"]
    d["signal_line"] = d["macd_line"].ewm(span=9, adjust=False).mean()
    d["macd_hist"] = d["macd_line"] - d["signal_line"]
    d["macd_hist_prev"] = d["macd_hist"].shift(1)

    return d


# ── 因子1: HV（波动率） ──

def compute_hv_factor(df: pd.DataFrame, row_idx: int) -> dict:
    """
    因子1: 20日波动率 × 60日历史百分位
    Returns: {position_scale, percentile, vol_20d, vol_annualized_pct}
    """
    # 需要至少 VOL_WINDOW + 10 天历史
    if row_idx < VOL_WINDOW + 5:
        return {"position_scale": 1.0, "percentile": 0.5, "vol_20d": 0, "vol_annualized_pct": 0}
    row = df.iloc[row_idx]
    current_vol = row["vol_20d"]
    if pd.isna(current_vol) or current_vol <= 0:
        return {"position_scale": 1.0, "percentile": 0.5, "vol_20d": 0, "vol_annualized_pct": 0}

    vol_history = df["vol_20d"].iloc[max(0, row_idx - HISTORY_DAYS + 1):row_idx + 1].dropna().values
    pct = _percentile_in_window(vol_history, current_vol)
    vol_ann = current_vol * np.sqrt(244)

    # 百分位映射到仓位
    scale = 1.0
    if pct > 0.80:
        scale = 0.25
    elif pct > 0.60:
        scale = 0.50
    elif pct > 0.30:
        scale = 0.75
    return {
        "position_scale": scale,
        "percentile": round(pct, 3),
        "vol_20d": round(current_vol, 6),
        "vol_annualized_pct": round(vol_ann * 100, 2),
    }


# ── 因子2: 主力资金（价量代理） ──

def compute_money_flow_factor(df: pd.DataFrame, row_idx: int) -> dict:
    """
    因子2: 成交额比率 × 涨跌幅 → 主力资金流向代理
    Rules:
      大涨(>1%) + 放量(>1.5x) → 1.0
      上涨 + 放量(>1.2x)      → 0.9
      大跌(<-1%) + 放量(>1.5x) → 0.25
      下跌 + 放量(>1.2x)      → 0.5
      缩量(<0.6x)             → 0.5
      默认                     → 0.75
    """
    if row_idx < 21:
        return {"position_scale": 0.75, "amount_ratio": 0, "returns_pct": 0, "rule_applied": "数据不足-默认"}

    row = df.iloc[row_idx]
    ret_pct = row["returns"] * 100 if pd.notna(row["returns"]) else 0
    ar = row["amount_ratio"] if pd.notna(row["amount_ratio"]) else 0

    rule = "默认"
    scale = 0.75

    if ret_pct > 1.0 and ar > 1.5:
        scale = 1.0
        rule = "大涨+放量>1.5x"
    elif ret_pct > 0 and ar > 1.2:
        scale = 0.9
        rule = "上涨+放量>1.2x"
    elif ret_pct < -1.0 and ar > 1.5:
        scale = 0.25
        rule = "大跌+放量>1.5x"
    elif ret_pct < 0 and ar > 1.2:
        scale = 0.5
        rule = "下跌+放量>1.2x"
    elif ar < 0.6:
        scale = 0.5
        rule = "缩量<0.6x"
    return {
        "position_scale": scale,
        "amount_ratio": round(ar, 3),
        "returns_pct": round(ret_pct, 2),
        "rule_applied": rule,
    }


# ── 因子3: 量能 ──

def compute_volume_factor(df: pd.DataFrame, row_idx: int) -> dict:
    """
    因子3: amount_ratio 的 60 日历史百分位
      百分位>80%→0.25, >60%→0.50, >30%→0.75, else→1.0
    """
    if row_idx < 61:
        return {"position_scale": 0.75, "amount_percentile": 0.5, "amount_ratio": 0}

    row = df.iloc[row_idx]
    ar = row["amount_ratio"] if pd.notna(row["amount_ratio"]) else 1.0
    ar_history = df["amount_ratio"].iloc[max(0, row_idx - 60 + 1):row_idx + 1].dropna().values
    pct = _percentile_in_window(ar_history, ar)

    scale = 1.0
    if pct > 0.80:
        scale = 0.25
    elif pct > 0.60:
        scale = 0.50
    elif pct > 0.30:
        scale = 0.75
    return {
        "position_scale": scale,
        "amount_percentile": round(pct, 3),
        "amount_ratio": round(ar, 3),
    }


# ── 因子4: 均线综合 ──

def compute_ma_factor(df: pd.DataFrame, row_idx: int) -> dict:
    """
    因子4: 价格 vs MA5/20/60/120
      每在一条均线上方 +0.25
      多头排列(MA5>MA20>MA60) +0.1
      空头排列(MA5<MA20<MA60) -0.1
      clamp 到 0.0~1.0
    """
    if row_idx < 5:  # 至少需要 MA5
        return {"position_scale": 0.75, "above_count": 0, "detail": "数据不足"}

    row = df.iloc[row_idx]
    close = row["close"]

    mas = {}
    above_count = 0
    for p in [5, 20, 60, 120]:
        ma_val = row.get(f"ma{p}")
        if pd.notna(ma_val) and ma_val > 0:
            mas[f"above_ma{p}"] = bool(close > ma_val)
            if close > ma_val:
                above_count += 1
        else:
            mas[f"above_ma{p}"] = False

    score = above_count * 0.25  # 0.0 ~ 1.0

    # 多头排列: MA5 > MA20 > MA60 (全部有效)
    ma5_val = row.get("ma5")
    ma20_val = row.get("ma20")
    ma60_val = row.get("ma60")
    if all(pd.notna(x) for x in [ma5_val, ma20_val, ma60_val]):
        if ma5_val > ma20_val > ma60_val:
            score += 0.1
            mas["bullish_alignment"] = True
        elif ma5_val < ma20_val < ma60_val:
            score -= 0.1
            mas["bearish_alignment"] = True
    mas.setdefault("bullish_alignment", False)
    mas.setdefault("bearish_alignment", False)

    score = _clamp(score)

    scale = _round_to_tier(score)
    # 映射到 [0.25, 1.0]
    if scale < 0.25:
        scale = 0.25
    elif scale > 1.0:
        scale = 1.0

    return {
        "position_scale": scale,
        "above_count": above_count,
        "raw_score": round(score, 3),
        **mas,
    }


# ── 因子5: MACD ──

def compute_macd_factor(df: pd.DataFrame, row_idx: int) -> dict:
    """
    因子5: MACD
      MACD快线 > 慢线 → +0.25, else -0.25
      柱状图 > 0 → +0.15, else -0.15
      柱状图变长 → +0.1, else -0.1
      clamp 到 0.0~1.0
    """
    if row_idx < 35:  # 至少需要 EMA26 + signal(9)
        return {"position_scale": 0.75, "raw_score": 0.5,
                "fast_above_slow": True, "histogram_positive": True, "detail": "数据不足"}

    row = df.iloc[row_idx]
    macd_line = row["macd_line"]
    signal_line = row["signal_line"]
    hist = row["macd_hist"]
    hist_prev = row["macd_hist_prev"]

    score = 0.0

    fast_above_slow = pd.notna(macd_line) and pd.notna(signal_line) and macd_line > signal_line
    if fast_above_slow:
        score += 0.25
    else:
        score -= 0.25

    hist_pos = pd.notna(hist) and hist > 0
    if hist_pos:
        score += 0.15
    else:
        score -= 0.15

    hist_lengthens = pd.notna(hist) and pd.notna(hist_prev) and abs(hist) > abs(hist_prev)
    if hist_lengthens:
        score += 0.1
    else:
        score -= 0.1

    score = _clamp(score)

    # 映射到档位
    scale = _round_to_tier(score)
    if scale < 0.25:
        scale = 0.25
    elif scale > 1.0:
        scale = 1.0

    return {
        "position_scale": scale,
        "raw_score": round(score, 3),
        "fast_above_slow": bool(fast_above_slow),
        "histogram_positive": bool(hist_pos),
        "histogram_lengthening": bool(hist_lengthens) if pd.notna(hist_lengthens) else False,
    }


# ═══════════════════════════════════════════════
#  五因子等权合并
# ═══════════════════════════════════════════════

def compute_five_factors(df: pd.DataFrame, row_idx: int) -> dict:
    """
    计算五个因子并等权平均。
    Returns: {
        "factors": { hv, money_flow, volume, ma, macd },
        "combined": { position_scale, avg_scale_rounded, signal_label, signal_icon }
    }
    """
    prepared = _prepare_broad_data(df)

    f1 = compute_hv_factor(prepared, row_idx)
    f2 = compute_money_flow_factor(prepared, row_idx)
    f3 = compute_volume_factor(prepared, row_idx)
    f4 = compute_ma_factor(prepared, row_idx)
    f5 = compute_macd_factor(prepared, row_idx)

    scales = [f1["position_scale"], f2["position_scale"], f3["position_scale"],
              f4["position_scale"], f5["position_scale"]]
    avg = np.mean(scales)
    rounded = _round_to_tier(avg)

    icon, label = _signal_label(rounded)

    return {
        "factors": {
            "hv": f1,
            "money_flow": f2,
            "volume": f3,
            "ma": f4,
            "macd": f5,
        },
        "combined": {
            "position_scale": rounded,  # 四舍五入到档位
            "avg_scale_raw": round(avg, 3),
            "avg_scale_rounded": rounded,
            "signal_label": label,
            "signal_icon": icon,
        }
    }


# ═══════════════════════════════════════════════
#  保持原有的 HS300 纯HV信号（向后兼容）
# ═══════════════════════════════════════════════

# 仓位映射（百分位 → scale）
POSITION_MAP = [
    (0.80, 0.25),
    (0.60, 0.50),
    (0.30, 0.75),
    (0.00, 1.00),
]

SIGNAL_LABELS = {
    (0.00, 0.30): ("🟢", "低波动", "市场平稳，建议满仓操作"),
    (0.30, 0.60): ("🟡", "中等波动", "市场正常波动，建议适度仓位"),
    (0.60, 0.80): ("🟠", "偏高波动", "市场波动加剧，建议降低仓位"),
    (0.80, 1.00): ("🔴", "高波动", "市场剧烈波动，建议轻仓观望"),
}


def compute_hv_only_signal(df: pd.DataFrame) -> dict:
    """
    纯 HV 信号（HS300 / 保留向后兼容）
    与旧的 compute_volatility_signal 逻辑一致。
    """
    if df is None or len(df) < VOL_WINDOW + 20:
        return {"error": "数据不足", "position_scale": 1.0}

    d = df.copy()
    d["returns"] = d["close"].pct_change()
    d["vol_20d"] = d["returns"].rolling(VOL_WINDOW).std()

    today = d.iloc[-1]
    today_date = today["date"].strftime("%Y-%m-%d")
    current_vol = today["vol_20d"]

    if pd.isna(current_vol):
        return {"date": today_date, "position_scale": 1.0, "error": "波动率N/A"}

    vol_annualized = current_vol * np.sqrt(244)
    vol_history = d["vol_20d"].dropna().tail(HISTORY_DAYS).values
    pct_rank = (vol_history > current_vol).mean()
    historical_pct = 1.0 - pct_rank

    if len(vol_history) < 20:
        historical_pct = 0.5

    position_scale = 1.0
    for threshold, scale in POSITION_MAP:
        if historical_pct >= threshold:
            position_scale = scale
            break

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

    vol_curve = []
    for i in range(min(60, len(vol_history)), 0, -1):
        if i <= len(vol_history):
            sub = vol_history[:i]
            if len(sub) > 0:
                p = (sub > vol_history[i - 1]).mean()
                vol_curve.append(round(1.0 - p, 2))

    return {
        "date": today_date,
        "date_ymd": today["date"].strftime("%Y%m%d"),
        "vol_20d": round(current_vol, 6),
        "vol_annualized_pct": round(vol_annualized * 100, 2),
        "historical_percentile": round(historical_pct, 3),
        "position_scale": position_scale,
        "signal_label": signal_label,
        "signal_icon": signal_icon,
        "signal_description": signal_desc,
        "index_close": round(today["close"], 2),
        "index_change_pct": round(today.get("returns", 0) * 100, 2),
        "vol_percentile_curve": vol_curve,
    }


# ═══════════════════════════════════════════════
#  输出
# ═══════════════════════════════════════════════

def save_signal(hs300_signal: dict, broad_market_hv: dict = None, five_factor_result: dict = None):
    """
    保存信号到 JSON 文件。

    输出结构:
      hs300:         纯HV（向后兼容）
      broad_market:  纯HV（向后兼容）
      factors:       五因子明细
      combined:      五因子等权仓位 + HS300 HV 参考
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    hs300_scale = hs300_signal.get("position_scale", 1.0)
    broad_hv_scale = broad_market_hv.get("position_scale", 1.0) if broad_market_hv else 1.0

    # 五因子等权仓位
    if five_factor_result:
        f_combined = five_factor_result["combined"]
        combined_scale = f_combined["position_scale"]
    else:
        combined_scale = min(hs300_scale, broad_hv_scale)

    # ── vol_timing.json ──
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
            "index_close": hs300_signal.get("index_close", 0),
            "index_change_pct": hs300_signal.get("index_change_pct", 0),
        },
        "combined": {
            "position_scale": combined_scale,
        },
    }

    if broad_market_hv:
        vol_timing["broad_market"] = {
            "position_scale": broad_hv_scale,
            "vol_annualized_pct": broad_market_hv.get("vol_annualized_pct", 0),
            "historical_percentile": broad_market_hv.get("historical_percentile", 0.5),
            "vol_20d": broad_market_hv.get("vol_20d", 0),
            "signal_label": broad_market_hv.get("signal_label", "默认"),
            "signal_icon": broad_market_hv.get("signal_icon", "⚪"),
            "signal_description": broad_market_hv.get("signal_description", ""),
            "index_close": broad_market_hv.get("index_close", 0),
            "index_change_pct": broad_market_hv.get("index_change_pct", 0),
        }

    # 五因子明细
    if five_factor_result:
        vol_timing["factors"] = five_factor_result["factors"]
        # 合并到 combined
        vol_timing["combined"] = {
            "position_scale": combined_scale,
            "avg_scale_raw": five_factor_result["combined"].get("avg_scale_raw", combined_scale),
            "avg_scale_rounded": five_factor_result["combined"].get("avg_scale_rounded", combined_scale),
            "signal_label": five_factor_result["combined"].get("signal_label", "默认"),
            "signal_icon": five_factor_result["combined"].get("signal_icon", "⚪"),
        }

    out_file = OUTPUT_DIR / "vol_timing.json"
    with open(out_file, "w") as f:
        json.dump(vol_timing, f, ensure_ascii=False, indent=2)
    logger.info(f"已保存五因子择时信号: {out_file}")

    # ── adjustment.json（合并写入） ──
    adj_file = OUTPUT_DIR / "adjustment.json"
    existing = {}
    if adj_file.exists():
        try:
            existing = json.loads(adj_file.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            pass
    existing.update({
        "vol_timing_factor": combined_scale,
        "vol_timing_label": five_factor_result["combined"]["signal_label"] if five_factor_result else hs300_signal.get("signal_label", "默认"),
        "vol_timing_percentile": hs300_signal.get("historical_percentile", 0.5),
        "vol_timing_date": hs300_signal.get("date_ymd", hs300_signal.get("date", "")),
        "vol_timing_broad_factor": broad_hv_scale,
        "factor": combined_scale,

        # 新增：五因子明细
        "vol_timing_method": "5factor",
        "vol_timing_5factor_scale": combined_scale,
        "vol_timing_hv_scale": hs300_scale,
    })
    if five_factor_result:
        factors = five_factor_result["factors"]
        existing["vol_timing_factors"] = {
            k: v["position_scale"] for k, v in factors.items()
        }

    with open(adj_file, "w") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    logger.info(f"合并写入 adjustment.json: 5factor_scale={combined_scale}")


def generate_report(hs300_signal: dict, five_factor_result: dict, broad_market_hv: dict = None) -> str:
    """生成可推送的文本报告"""
    if "error" in hs300_signal:
        return f"⚠️ 波动率择时信号异常: {hs300_signal['error']}"

    # HS300 HV参考
    hs300_icon = hs300_signal["signal_icon"]
    hs300_label = hs300_signal["signal_label"]
    hs300_pct = hs300_signal["historical_percentile"] * 100
    hs300_vol = hs300_signal["vol_annualized_pct"]
    hs300_desc = hs300_signal["signal_description"]
    hs300_close = hs300_signal["index_close"]

    lines = [
        f"🌊 **五因子择时信号**  {hs300_signal['date']}\n",
    ]

    # ── 五因子等权结果 ──
    if five_factor_result:
        f_comb = five_factor_result["combined"]
        icon = f_comb["signal_icon"]
        label = f_comb["signal_label"]
        scale = f_comb["position_scale"]
        raw = f_comb["avg_scale_raw"]
        lines.append(f"{icon} **五因子等权：{label}**（等权均值 {raw:.2f} → 档位 {scale:.0%}）")

        # 各因子明细
        lines.append(f"")
        lines.append(f"📊 **因子明细**")
        for fname, fdata in five_factor_result["factors"].items():
            name_map = {"hv": "波动率HV", "money_flow": "主力资金", "volume": "量能", "ma": "均线综合", "macd": "MACD"}
            f_scale = fdata["position_scale"]
            f_icon = "🟢" if f_scale >= 0.75 else "🟡" if f_scale >= 0.5 else "🔴"
            lines.append(f"  {f_icon} {name_map.get(fname, fname)}: {f_scale:.0%}")
    else:
        lines.append(f"{hs300_icon} **沪深300(HV参考)：{hs300_label}**（百分位 {hs300_pct:.0f}%）")

    # ── HS300 HV参考（一行） ──
    lines.append(f"")
    lines.append(f"📊 **参考**")
    lines.append(f"  {hs300_icon} 沪深300(HV): {hs300_label}（年化波动 {hs300_vol:.1f}%，百分位 {hs300_pct:.0f}%）")
    lines.append(f"  · 收盘 {hs300_close:.2f} ({hs300_signal['index_change_pct']:+.2f}%)")
    if broad_market_hv and "error" not in broad_market_hv:
        bm_icon = broad_market_hv["signal_icon"]
        bm_label = broad_market_hv["signal_label"]
        bm_pct = broad_market_hv["historical_percentile"] * 100
        bm_close = broad_market_hv.get("index_close", 0)
        lines.append(f"  {bm_icon} 中证全指(HV): {bm_label}（百分位 {bm_pct:.0f}% 收盘 {bm_close:.2f}）")

    # ── 仓位建议 ──
    scale_for_advice = five_factor_result["combined"]["position_scale"] if five_factor_result else hs300_signal["position_scale"]
    if scale_for_advice >= 1.0:
        advice = "✅ 建议满仓操作"
    elif scale_for_advice >= 0.75:
        advice = "✅ 建议正常仓位(75%)"
    elif scale_for_advice >= 0.50:
        advice = "⚠️ 建议中等仓位(50%)"
    elif scale_for_advice >= 0.25:
        advice = "⚠️ 建议轻仓操作(25%)"
    else:
        advice = "🔴 建议空仓观望"

    lines.append(f"")
    lines.append(f"**🎯 综合仓位建议：{scale_for_advice:.0%}**")
    lines.append(f"{advice}")
    if five_factor_result:
        lines.append(f"*五因子等权（HV+资金+量能+均线+MACD），各因子权重20%*")

    return "\n".join(lines)


# ═══════════════════════════════════════════════
#  回测
# ═══════════════════════════════════════════════

def run_backtest():
    """
    回测比较：五因子等权 vs 纯HV (2025-01 ~ 今)
    将两种信号应用于同一指数日收益率，比较净值曲线和绩效指标。

    假设:
      - 每日按信号调整仓位，无交易成本
      - 基准为满仓持有
      - HV 信号 = broad_market 的 HV 百分位映射
      5factor 信号 = 五个因子等权平均
    """
    logger.info("=" * 50)
    logger.info("回测：五因子等权 vs 纯HV（2025-01 ~ 今）")
    logger.info("=" * 50)

    df_raw = load_broad_market()
    if df_raw is None or len(df_raw) < 200:
        logger.error("数据不足，无法回测")
        return None, None

    df = _prepare_broad_data(df_raw)

    # 筛选可用的有效范围：需要至少 120 天历史
    min_idx = max(120, df[df["date"] >= "2025-01-01"].index[0] if len(df[df["date"] >= "2025-01-01"]) > 0 else 120)
    valid_indices = list(range(min_idx, len(df)))

    if len(valid_indices) < 20:
        logger.error("回测数据点不足")
        return None, None

    logger.info(f"回测期间: {df.iloc[valid_indices[0]]['date'].strftime('%Y-%m-%d')} ~ {df.iloc[valid_indices[-1]]['date'].strftime('%Y-%m-%d')} ({len(valid_indices)}个交易日)")

    bt_dates = []
    hv_scales = []
    five_scales = []

    for idx in valid_indices:
        bt_dates.append(df.iloc[idx]["date"])

        # ── HV 信号 ──
        row = df.iloc[idx]
        cur_vol = row["vol_20d"]
        if pd.notna(cur_vol) and cur_vol > 0:
            vol_hist = df["vol_20d"].iloc[max(0, idx - HISTORY_DAYS + 1):idx + 1].dropna().values
            pct = _percentile_in_window(vol_hist, cur_vol)
            hv_scale = 1.0
            if pct > 0.80:
                hv_scale = 0.25
            elif pct > 0.60:
                hv_scale = 0.50
            elif pct > 0.30:
                hv_scale = 0.75
        else:
            hv_scale = 1.0
        hv_scales.append(hv_scale)

        # ── 五因子信号 ──
        five_result = compute_five_factors(df_raw.iloc[:idx + 1], idx)
        five_scales.append(five_result["combined"]["position_scale"])

    # 构建回测 DataFrame
    bt_df = pd.DataFrame({
        "date": bt_dates,
        "returns": df.iloc[valid_indices]["returns"].values,
        "hv_scale": hv_scales,
        "five_scale": five_scales,
    }).dropna()

    # 策略收益 = 每日收益率 × 前一日仓位
    # (使用当日信号在次日执行，但简化处理为当日调整)
    bt_df["hv_ret"] = bt_df["returns"] * bt_df["hv_scale"]
    bt_df["five_ret"] = bt_df["returns"] * bt_df["five_scale"]

    # 净值
    bt_df["hv_nav"] = (1 + bt_df["hv_ret"]).cumprod()
    bt_df["five_nav"] = (1 + bt_df["five_ret"]).cumprod()
    bt_df["benchmark_nav"] = (1 + bt_df["returns"]).cumprod()

    # 绩效指标
    def calc_metrics(returns: pd.Series) -> dict:
        """计算年化收益、波动率、夏普、最大回撤"""
        ann_ret = returns.mean() * 244
        ann_vol = returns.std() * np.sqrt(244)
        sharpe = ann_ret / ann_vol if ann_vol > 0 else 0
        # 最大回撤
        nav = (1 + returns).cumprod()
        peak = nav.expanding().max()
        dd = (nav - peak) / peak
        max_dd = dd.min()
        # 胜率
        win_rate = (returns > 0).mean()
        return {
            "年化收益": round(ann_ret * 100, 2),
            "年化波动": round(ann_vol * 100, 2),
            "夏普比率": round(sharpe, 3),
            "最大回撤": round(max_dd * 100, 2),
            "胜率": round(win_rate * 100, 1),
            "累计收益": round((1 + returns).prod() * 100 - 100, 2),
            "交易天数": len(returns),
        }

    hv_m = calc_metrics(bt_df["hv_ret"])
    five_m = calc_metrics(bt_df["five_ret"])
    bench_m = calc_metrics(bt_df["returns"])

    # ── 打印对比表 ──
    header = f"{'指标':<14} {'基准(满仓)':>12} {'纯HV':>12} {'五因子':>12}"
    sep = "─" * 52
    print(f"\n{sep}")
    print(f"📊 回测绩效对比 — {bt_df['date'].iloc[0].strftime('%Y-%m-%d')} ~ {bt_df['date'].iloc[-1].strftime('%Y-%m-%d')}")
    print(sep)
    print(header)
    print(sep)
    for k in ["累计收益", "年化收益", "年化波动", "夏普比率", "最大回撤", "胜率", "交易天数"]:
        b = bench_m.get(k, "N/A")
        h = hv_m.get(k, "N/A")
        f = five_m.get(k, "N/A")
        print(f"{k:<14} {str(b):>12} {str(h):>12} {str(f):>12}")
    print(sep)

    # 信号分布
    print(f"\n📊 信号分布")
    for model, scales in [("纯HV", hv_scales), ("五因子", five_scales)]:
        counts = {t: 0 for t in [0.25, 0.50, 0.75, 1.0]}
        for s in scales:
            cs = round(s * 4) / 4
            counts[cs] = counts.get(cs, 0) + 1
        total = sum(counts.values())
        print(f"  {model}:")
        for t in [1.0, 0.75, 0.50, 0.25]:
            pct = counts[t] / total * 100 if total > 0 else 0
            bar = "█" * int(pct / 5)
            print(f"    {t:.0%}: {bar} {counts[t]:>4}次 ({pct:.0f}%)")

    return bt_df, {"hv": hv_m, "five": five_m, "benchmark": bench_m}


# ═══════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════

def main():
    """主入口"""
    logger.info("=" * 50)
    logger.info("五因子择时信号生成")
    logger.info("=" * 50)

    # ── 1. 沪深300 HV信号（向后兼容） ──
    df_hs300 = load_hs300()
    hs300_signal = None
    if df_hs300 is not None:
        hs300_signal = compute_hv_only_signal(df_hs300)
        if hs300_signal and "error" in hs300_signal:
            logger.error(f"沪深300信号计算失败: {hs300_signal['error']}")
            hs300_signal = None

    if hs300_signal is None:
        hs300_signal = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "date_ymd": datetime.now().strftime("%Y%m%d"),
            "position_scale": 1.0,
            "signal_label": "默认",
            "signal_icon": "⚪",
            "signal_description": "沪深300数据异常，回退默认满仓",
            "historical_percentile": 0.5,
            "vol_annualized_pct": 0,
            "index_close": 0,
            "index_change_pct": 0,
            "vol_percentile_curve": [],
        }

    # ── 2. 中证全指 HV 信号（向后兼容） ──
    broad_hv = None
    try:
        df_broad = load_broad_market()
        if df_broad is not None:
            broad_hv = compute_hv_only_signal(df_broad)
            if broad_hv and "error" in broad_hv:
                logger.warning(f"中证全指 HV 信号异常: {broad_hv['error']}")
                broad_hv = None
            if broad_hv:
                broad_hv["date_source"] = "broad_market"
                logger.info(f"中证全指(HV): 百分位={broad_hv['historical_percentile']:.1%}")
    except Exception as e:
        logger.warning(f"中证全指 HV 计算异常: {e}")
        broad_hv = None

    # ── 3. 五因子等权信号（中证全指） ──
    five_factor_result = None
    try:
        df_broad = load_broad_market()
        if df_broad is not None and len(df_broad) >= 120:
            last_idx = len(df_broad) - 1
            five_factor_result = compute_five_factors(df_broad, last_idx)
            f_comb = five_factor_result["combined"]
            logger.info(f"五因子等权: {f_comb['signal_icon']} {f_comb['signal_label']} "
                        f"均分={f_comb['avg_scale_raw']:.3f} → 档位={f_comb['position_scale']:.0%}")

            # 打印各因子
            for fname, fdata in five_factor_result["factors"].items():
                logger.info(f"  {fname}: scale={fdata['position_scale']:.0%}")
    except Exception as e:
        logger.warning(f"五因子计算异常: {e}", exc_info=True)
        five_factor_result = None

    # ── 4. 保存信号 ──
    save_signal(hs300_signal, broad_market_hv=broad_hv, five_factor_result=five_factor_result)

    # ── 5. 生成报告 ──
    report = generate_report(hs300_signal, five_factor_result, broad_market_hv=broad_hv)
    print("\n" + report + "\n")

    # ── 6. 回测 ──
    print("\n" + "=" * 60)
    bt_result, bt_metrics = run_backtest() or (None, None)

    return 0


if __name__ == "__main__":
    sys.exit(main())
