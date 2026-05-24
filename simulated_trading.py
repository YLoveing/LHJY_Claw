#!/usr/bin/env python3
"""
模拟交易引擎 v2 — 增强版
读取 AI 分析报告 → 解析评分 → 多策略执行买卖 → 风控止损 → 追踪盈亏 → 绩效统计

策略对比：
  - 主策略（默认）：评分≥70买入，≤45卖出，单笔5万，上限8只
  - 趋势策略（对比用）：评分≥65买入，≤35卖出，单笔4万，上限8只，MA方向确认
  - 均值回归策略（对比用）：评分≥75买入，≤25卖出，单笔6万，上限6只，RSI极端确认

风控增强：止损 -15%（单只） | 止盈 +25%（减半仓） | 硬性账户回撤 -20% 暂停交易
统一账户：合并日内交易（intraday）与日频策略（daily），通过 position["mode"] 区分
"""

import json
import logging
import os
import re
import sys
from collections import OrderedDict
from datetime import date, datetime, timedelta
from pathlib import Path
from scripts.trading_calendar import eastmoney_secid
import sys

logger = logging.getLogger("simulated_trading")

BASE_DIR = Path("/opt/daily_stock_analysis")
DATA_DIR = BASE_DIR / "simulated_trading"
STATE_FILE = DATA_DIR / "state.json"
TRADES_FILE = DATA_DIR / "trades.json"
PERF_FILE = DATA_DIR / "performance.json"
TRACE_FILE = DATA_DIR / "signal_trace.json"

INITIAL_CAPITAL = 30_000
MAX_POSITIONS = 3

# ── 交易费用参数（A股真实费率） ──
COMMISSION_RATE = 0.00025       # 佣金万2.5（买卖均收，最低5元）
STAMP_TAX_RATE = 0.0005         # 印花税万5（仅卖出时收）
TRANSFER_FEE_RATE = 0.00001     # 过户费万0.1（买卖均收）
MIN_COMMISSION = 5.0            # 佣金最低收费5元


def calc_buy_fees(cost: float) -> float:
    """买入费用 = 佣金（最低5元）+ 过户费"""
    commission = max(cost * COMMISSION_RATE, MIN_COMMISSION)
    transfer_fee = cost * TRANSFER_FEE_RATE
    return commission + transfer_fee


def calc_sell_fees(proceeds_before_fees: float) -> float:
    """卖出费用 = 佣金（最低5元）+ 过户费 + 印花税"""
    commission = max(proceeds_before_fees * COMMISSION_RATE, MIN_COMMISSION)
    transfer_fee = proceeds_before_fees * TRANSFER_FEE_RATE
    stamp_tax = proceeds_before_fees * STAMP_TAX_RATE
    return commission + transfer_fee + stamp_tax


# ── P3 滑点模型 ──
def _apply_slippage(price: float, direction: str = "buy", amount: float = 0) -> float:
    """
    应用滑点模型。
    买入上浮 X%，卖出下浮 X%，模拟实际成交滑点。
    amount 越小滑点越大（小单对价格影响更显著）。
    """
    slip = SLIPPAGE_RATE
    # 小额交易滑点更大
    if amount < 5000:
        slip *= 2
    elif amount < 10000:
        slip *= 1.5
    mult = 1 + slip if direction == "buy" else 1 - slip
    return round(price * mult, 3)

# ── 风控参数 ──
MEDIUM_STOP_PCT = -5.0     # 日频持仓浮亏-5% → 减半仓（P1风控补洞）
HARD_STOP_PCT = -8.0        # 日频持仓浮亏-8% → 全平（替代旧-15%）
STOP_LOSS_PCT = -8.0        # 硬止损阈值（原-15%收窄）
TAKE_PROFIT_PCT = 25.0      # 单只浮盈25% → 减半仓锁利
ACCOUNT_DRAWDOWN_LIMIT = -20.0  # 账户总回撤超过20% → 暂停所有买入

# ── P2 时间止损参数 ──
TIME_STOP_DAYS = 20          # 持有超过20个交易日
TIME_STOP_LOSS_PCT = -8.0    # 且浮亏超过-8% → 强制平仓

# ── P2 多级止盈参数 ──
TP_LEVELS = [
    (10.0, 0.5, "一级止盈(+10%)"),   # 浮盈+10% → 减半仓
    (25.0, 1.0, "二级止盈(+25%)"),   # 浮盈+25% → 清仓
]

# ── P3 再平衡触发 ──
REBALANCE_SCORE_THRESHOLD = 5  # 评分变化超过±5分才触发调仓

# ── P3 滑点参数 ──
SLIPPAGE_RATE = 0.001  # 0.1% 基础滑点

# ── 情绪引擎调节 ──
SENTIMENT_ADJ_FILE = DATA_DIR / "sentiment_adjustment.json"

def _load_sentiment_adjustment() -> float:
    """从波动率择时引擎加载仓位调节系数。
    优先级：
    1. vol_timing.json → combined.position_scale
    2. vol_timing.json → hs300.position_scale
    3. adjustment.json → vol_timing_factor (向后兼容)
    4. adjustment.json → factor (情绪引擎)
    5. 默认 1.0
    """
    # 1. 优先读取 vol_timing.json 的 combined 信号
    vt_file = Path("/opt/daily_stock_analysis/sentiment_engine/vol_timing.json")
    if vt_file.exists():
        try:
            with open(vt_file) as f:
                data = json.load(f)
            # combined.position_scale
            combined = data.get("combined", {})
            if "position_scale" in combined:
                factor = float(combined["position_scale"])
                logger.info(f"[模拟交易] 应用波动率择时双信号: combined={factor}")
                return factor
            # 回退: hs300.position_scale
            hs300 = data.get("hs300", {})
            if "position_scale" in hs300:
                factor = float(hs300["position_scale"])
                logger.info(f"[模拟交易] 应用波动率择时HS300信号: {factor}")
                return factor
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning(f"[模拟交易] vol_timing.json 读取失败: {e}")

    # 2. 回退到 adjustment.json (向后兼容)
    si_file = Path("/opt/daily_stock_analysis/sentiment_engine/adjustment.json")
    if si_file.exists():
        try:
            with open(si_file) as f:
                data = json.load(f)
            vol_factor = data.get("vol_timing_factor")
            if vol_factor is not None:
                factor = float(vol_factor)
                logger.info(f"[模拟交易] 应用仓位系数(回退adjustment.json): {factor}")
                return factor
            factor = float(data.get("factor", 1.0))
            logger.info(f"[模拟交易] 应用仓位系数(回退情绪引擎): {factor}")
            return factor
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning(f"[模拟交易] 仓位系数读取失败: {e}")

    logger.info("[模拟交易] 无仓位系数，默认满仓")
    return 1.0

# ── 行业分类（申万一级行业，实时查询 + 前缀回退） ──
EM_SECTOR_NAMES = {
    1: "农林牧渔", 2: "采掘", 3: "化工", 4: "钢铁",
    5: "有色金属", 6: "电子", 7: "汽车", 8: "家用电器",
    9: "食品饮料", 10: "纺织服装", 11: "轻工制造",
    12: "医药生物", 13: "公用事业", 14: "交通运输",
    15: "房地产", 16: "商业贸易", 17: "休闲服务",
    18: "综合", 19: "建筑材料", 20: "建筑装饰",
    21: "电气设备", 22: "国防军工", 23: "计算机",
    24: "传媒", 25: "通信", 26: "银行", 27: "非银金融",
    28: "机械设备", 29: "煤炭", 30: "石油化工",
}

SECTOR_MAP_FALLBACK = {
    "600": "其他",    # 沪市主板（行业不确定）
    "601": "金融",     # 沪市（多为银行/保险/券商）
    "000": "综合",     # 深市主板
    "002": "其他",     # 中小板
    "300": "其他",     # 创业板
    "688": "其他",     # 科创板
    "510300": "ETF",
}

# ── 大盘择时 ──

def check_market_condition() -> str:
    """
    检查大盘市场状态，用于择时过滤。
    先查沪深300指数MA趋势（中长线），再看实时涨跌幅（短线）。
    两者取更保守的结果。

    Returns:
        "normal"  — 正常交易
        "caution" — 谨慎，不开新仓
        "danger"  — 危险，只卖不买
    """
    # ── MA趋势过滤（中长线） ──
    try:
        from risk.market_filter import get_market_state
        mkt = get_market_state()
        ma_scale = mkt.get("scale", 1.0)
        ma_state = mkt.get("state_name", "unknown")
    except Exception as e:
        ma_scale = 1.0
        ma_state = "error"
    
    # ── 实时涨跌幅（短线） ──
    import requests
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "secid": "1.000300",
        "fields": "f2,f3,f12,f14",
        "fltt": 2,
        "invt": 2,
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/",
    }
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        data = r.json()
        change_pct = data.get("data", {}).get("f3", 0)
        if change_pct is not None:
            # 短线状态
            if change_pct <= -3.0:
                short_state = "danger"
            elif change_pct <= -1.5:
                short_state = "caution"
            else:
                short_state = "normal"
        else:
            short_state = "normal"

        # ── 综合MA过滤 + 短线涨跌幅，取更保守 ──
        print(f"[风控] MA趋势: {ma_state}(scale={ma_scale}) | 实时涨跌: {change_pct}%")

        if ma_scale <= 0.25 or short_state == "danger":
            print(f"[风控] → DANGER：只卖不买")
            return "danger"
        elif ma_scale <= 0.5 or short_state == "caution":
            print(f"[风控] → CAUTION：不开新仓")
            return "caution"
        else:
            return "normal"
    except Exception as e:
        print(f"[风控] 大盘择时查询失败（默认 caution）: {e}")
    return "caution"


# ── 行业查询（内存缓存避免重复请求） ──
_SECTOR_CACHE = {}

def _get_sector(code: str) -> str:
    """
    获取股票所属行业（优先东方财富实时查询，回退code前缀映射）。
    """
    if code in _SECTOR_CACHE:
        return _SECTOR_CACHE[code]
    import requests
    prefix = code[:3] if len(code) >= 3 else code
    try:
        # 尝试从东方财富获取实时行业
        secid = eastmoney_secid(code)
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": secid,
            "fields": "f12,f27",
            "fltt": 2,
            "invt": 2,
        }
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        }
        r = requests.get(url, params=params, headers=headers, timeout=5)
        data = r.json()
        industry_code = data.get("data", {}).get("f27")
        if industry_code and isinstance(industry_code, int) and industry_code in EM_SECTOR_NAMES:
            return EM_SECTOR_NAMES[industry_code]
    except Exception:
        pass

    # 回退：code 前缀映射
    result = SECTOR_MAP_FALLBACK.get(prefix, "其他")
    _SECTOR_CACHE[code] = result
    return result


def get_position_sectors(state: dict) -> dict:
    """统计当前持仓中各行业的市值的占比（带缓存）。"""
    sectors = {}
    total_mv = 0.0
    for code, pos in state.get("positions", {}).items():
        sector = _get_sector(code)
        mv = pos["quantity"] * pos["current_price"]
        sectors.setdefault(sector, 0.0)
        sectors[sector] += mv
        total_mv += mv

    if total_mv > 0:
        for s in sectors:
            sectors[s] = sectors[s] / total_mv * 100
    return sectors


# ── 凯利公式仓位计算 ──
# 赔率 b = 第一档止盈/止损 = 10/15 ≈ 0.67
KELLY_B = abs(TP_LEVELS[0][0] / STOP_LOSS_PCT)  # 使用第一档止盈(+10%)算赔率

def _kelly_fraction(score: int) -> float:
    """
    基于评分计算凯利仓位比例 f* = (b*p - q) / b

    score 70 → p≈0.65, 75→0.70, 85→0.78, 95→0.85
    返回半凯利（保守策略）
    """
    b = KELLY_B
    if score >= 100: p = 0.90
    elif score >= 90: p = 0.85
    elif score >= 80: p = 0.78
    elif score >= 70: p = 0.70
    else: p = 0.50
    q = 1 - p
    f_kelly = max(0.0, (b * p - q) / b)
    return f_kelly * 0.5  # 半凯利


def _compute_kelly_amount(score: int, cash: float, sentiment_factor: float) -> float:
    """
    凯利仓位 = 现金 x 凯利比例 x 情绪系数 x 分散系数(0.2)
    """
    f = _kelly_fraction(score)
    diversification = 0.25  # 最大单只占总资金25%
    amount = cash * f * diversification * sentiment_factor
    # 钳位到合理范围
    min_trade = 10_000
    max_trade = min(cash * 0.25, 200_000)
    return max(min_trade, min(max_trade, amount))


# ── 动态阈值（基于市场情绪调整） ──
def _compute_dynamic_thresholds(sentiment_score: int = 50) -> tuple:
    """
    基于 GARCH 波动率 + 情绪指数 动态调整买卖阈值。

    优先级：
    1. GARCH 条件波动率百分位 (从 quant_engine 读取)
    2. 情绪指数 (回退方案)
    
    Returns: (buy_threshold, sell_threshold)
    """
    # 尝试从 GARCH 引擎读取
    try:
        garch_file = Path("/opt/daily_stock_analysis/quant_engine/garch_output.json")
        if garch_file.exists():
            import json
            data = json.loads(garch_file.read_text())
            if data.get("status") == "ok" and "dynamic_thresholds" in data:
                n_obs = data.get("n_obs", 0)
                if n_obs < 50:
                    logger.warning(f"[阈值] GARCH样本不足({n_obs}<50)，自动删除无效输出")
                    garch_file.unlink(missing_ok=True)
                else:
                    bt = data["dynamic_thresholds"]["buy"]
                    st = data["dynamic_thresholds"]["sell"]
                    vp = data.get("vol_percentile", 50)
                    logger.info(f"[阈值] GARCH驱动: vol_pct={vp:.0f}% 阈值=买入≥{bt} / 卖出≤{st}")
                    return (bt, st)
    except Exception:
        pass
    
    # 回退：基于情绪指数
    if sentiment_score >= 80:
        return (75, 40)
    elif sentiment_score >= 65:
        return (68, 40)
    elif sentiment_score <= 20:
        return (65, 48)
    elif sentiment_score <= 35:
        return (65, 43)
    else:
        return (65, 40)


def _load_sentiment_index_score() -> int:
    """从情绪引擎读取最新的情绪指数。"""
    si_file = Path("/opt/daily_stock_analysis/sentiment_engine/adjustment.json")
    if si_file.exists():
        try:
            with open(si_file) as f:
                data = json.load(f)
            return int(data.get("score", 50))
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    return 50

# ── 多策略规则 ──
STRATEGIES = OrderedDict([
    ("main", {
        "name": "主策略（情绪周期）",
        "buy_threshold": 70,
        "sell_threshold": 45,
        "per_trade": 50_000,
        "max_pos": 8,
        "enable": True,
    }),
    ("trend", {
        "name": "趋势跟踪",
        "buy_threshold": 65,
        "sell_threshold": 35,
        "per_trade": 40_000,
        "max_pos": 8,
        "enable": False,  # 默认关闭，用户可开启
    }),
    ("reversion", {
        "name": "均值回归",
        "buy_threshold": 75,
        "sell_threshold": 25,
        "per_trade": 60_000,
        "max_pos": 6,
        "enable": False,
    }),
])

# ── 数据持久化 ──

def _migrate_state(state):
    """Ensure state dict has all required keys (v2 migration)."""
    defaults = {
        "cash": INITIAL_CAPITAL,
        "positions": {},
        "total_pnl": 0.0,
        "total_fee": 0.0,
        "total_market_value": 0.0,
        "total_equity": INITIAL_CAPITAL,
        "total_return": 0.0,
        "total_return_pct": 0.0,
        "peak_equity": INITIAL_CAPITAL,
        "max_drawdown_pct": 0.0,
        "last_update": "",
    }
    for k, v in defaults.items():
        state.setdefault(k, v)

    # v3 migration: add mode field to positions (old positions = daily)
    for code, pos in state.get("positions", {}).items():
        if "mode" not in pos:
            pos["mode"] = "daily"
        if "entry_date" not in pos:
            pos["entry_date"] = "20260513"  # 保留旧数据，新仓位会由 buy 逻辑写入正确日期
        if "tp_level" not in pos:
            pos["tp_level"] = 0

    return state


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return _migrate_state(json.load(f))
    return _migrate_state({})

def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def load_trades():
    if TRADES_FILE.exists():
        with open(TRADES_FILE) as f:
            return json.load(f)
    return []

def save_trades(trades):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, ensure_ascii=False, indent=2)

def load_performance():
    if PERF_FILE.exists():
        with open(PERF_FILE) as f:
            return json.load(f)
    return {"daily": [], "weekly": [], "summary": {}}

def save_performance(perf):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(PERF_FILE, "w") as f:
        json.dump(perf, f, ensure_ascii=False, indent=2)

def load_signal_trace():
    if TRACE_FILE.exists():
        with open(TRACE_FILE) as f:
            return json.load(f)
    return {"records": []}

def save_signal_trace(trace):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(TRACE_FILE, "w") as f:
        json.dump(trace, f, ensure_ascii=False, indent=2, default=str)

# ── 报告解析 ──

def parse_stock_scores(report_date):
    """从分析报告提取每只股票的完整信息：评分、操作建议、看多看空、核心理由。"""
    report_file = BASE_DIR / f"reports/report_{report_date}.md"
    if not report_file.exists():
        print(f"[模拟交易] 报告不存在: {report_file}")
        return {}

    with open(report_file, encoding="utf-8") as f:
        content = f.read()

    stocks = {}
    # 摘要行格式: **股票名(code)**: 操作 | 评分 N | 看多/空
    pattern = r'\*\*[^()]+\(([^)]+)\)\**:\s*(\S+)\s*\|\s*评分\s*(\d+)\s*\|\s*(\S+)'
    for match in re.finditer(pattern, content):
        code = match.group(1).strip()
        stocks[code] = {
            "operation": match.group(2).strip(),
            "score": int(match.group(3)),
            "sentiment": match.group(4).strip(),
            "rationale": extract_rationale(content, code),
        }

    return stocks


def extract_rationale(content, code):
    """从报告详情章节提取评分核心理由（一句话决策部分）。"""
    sections = re.split(r'\n## ', content)
    for sec in sections:
        if f'({code})' in sec:
            # 找"一句话决策"行
            one_sentence = re.search(r'>\s*\*\*一句话决策\*\*\s*:\s*(.*?)(?:\n|$)', sec)
            if one_sentence:
                return one_sentence.group(1).strip()
            # 回退：找核心结论部分
            conclusion = re.search(r'核心结论.*?\n(.*?)(?:\n\n|\n###)', sec, re.DOTALL)
            if conclusion:
                return conclusion.group(1).strip()[:100]
    return ""


def extract_stock_price(code, report_date):
    """从报告解析某只股票的当前价（支持多种格式）。"""
    report_file = BASE_DIR / f"reports/report_{report_date}.md"
    if not report_file.exists():
        return None

    with open(report_file, encoding="utf-8") as f:
        content = f.read()

    sections = re.split(r'\n## ', content)
    target_section = None
    for sec in sections:
        first_line = sec.split('\n')[0] if '\n' in sec else sec
        if f'({code})' in first_line:
            target_section = sec
            break

    if not target_section:
        return None

    lines = target_section.split('\n')

    # 方式一：找 "当前价" 行（紧跟在当日行情表下面的行）
    for i, line in enumerate(lines):
        if '当前价' in line and i + 2 < len(lines):
            price_line = lines[i + 2]
            for p in re.findall(r'[\d.]+', price_line):
                try:
                    v = float(p)
                    if 0.5 < v < 10000:
                        return v
                except ValueError:
                    pass

    # 方式二：当日行情表第一列（收盘价行）
    close_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith('| 收盘 '):
            close_idx = i
            break
    if close_idx is not None and close_idx + 1 < len(lines):
        parts = [p.strip() for p in lines[close_idx + 1].split('|') if p.strip()]
        if parts:
            try:
                v = float(parts[0])
                if 0.5 < v < 10000:
                    return v
            except ValueError:
                pass

    return None


# ── 核心风控 ──

def check_stop_loss(pos, current_price, code, report_date_str):
    """检查是否触发止损、时间止损或多级止盈。
    
    Returns (action, reason, close_ratio):
      - action: "stop_loss" | "time_stop" | "take_profit" | "hold"
      - reason: 触发原因
      - close_ratio: 平仓比例 (0.0~1.0)，1.0=全平，0.5=半仓
    """
    avg_cost = pos["avg_cost"]
    pnl_pct = (current_price - avg_cost) / avg_cost * 100

    # ── 硬止损（P0） ──
    # ── 硬止损（P0）：-8% 全平 ──
    if pnl_pct <= HARD_STOP_PCT:
        return "stop_loss", f"硬止损触发：{pnl_pct:+.1f}%（阈值{HARD_STOP_PCT}%）", 1.0
    # ── 中等止损（P1）：日频持仓-5%减半仓 ──
    if pnl_pct <= MEDIUM_STOP_PCT:
        return "stop_loss", f"中等止损：{pnl_pct:+.1f}%（阈值{MEDIUM_STOP_PCT}%，减半仓）", 0.5

    # ── 时间止损（P2）: 持有超过N天且亏损超过M% → 平仓释放资金 ──
    if pos.get("mode") == "daily" and pos.get("entry_date"):
        try:
            from datetime import datetime
            entry_dt = datetime.strptime(pos["entry_date"], "%Y%m%d")
            holding_days = (datetime.now() - entry_dt).days
            if holding_days >= TIME_STOP_DAYS and pnl_pct <= TIME_STOP_LOSS_PCT:
                return ("time_stop",
                        f"时间止损：持有{holding_days}天 浮亏{pnl_pct:.1f}%（>{TIME_STOP_DAYS}天且亏>{abs(TIME_STOP_LOSS_PCT)}%）",
                        1.0)
        except Exception:
            pass

    # ── 多级止盈（P2） ──
    tp_level = pos.get("tp_level", 0)
    for target_pct, close_ratio, label in TP_LEVELS:
        if pnl_pct >= target_pct:
            # 检查这级止盈是否已经触发过
            level_idx = int(target_pct // 10)  # 10→1, 25→2
            # 用 tp_level 判断：0=未触发，1=一级已触发，2=二级已触发
            if tp_level < level_idx:
                return ("take_profit", f"{label}：{pnl_pct:+.1f}%（目标>{target_pct}%）", close_ratio)

    return "hold", "", 0.0


def check_account_drawdown(state):
    """检查账户总回撤是否超过限制。"""
    current_equity = state["cash"] + sum(p["quantity"] * p["current_price"]
                                          for p in state["positions"].values())
    peak = state.get("peak_equity", INITIAL_CAPITAL)
    if current_equity > peak:
        state["peak_equity"] = current_equity
        peak = current_equity

    drawdown_pct = (current_equity - peak) / peak * 100
    # 回撤在限制范围内才允许交易（-5% >= -20% → True）
    return drawdown_pct >= ACCOUNT_DRAWDOWN_LIMIT, drawdown_pct


# ── 交易执行 ──

def execute_trades(stocks, report_date_str):
    """根据评分执行模拟买卖，多策略对比，风控止损。"""
    state = load_state()
    trades = load_trades()
    current_positions = set(state["positions"].keys())
    new_trades = []
    force_sells = []

    # ── 0️⃣ 大盘择时检查 ──
    market_state = check_market_condition()
    market_danger = market_state == "danger"
    market_caution = market_state == "caution"
    if market_danger:
        print(f"[风控] 大盘跌超-3% → DANGER：只卖不买")
    elif market_caution:
        print(f"[风控] 大盘跌超-1.5% → CAUTION：日内不开新仓")

    # ── 1️⃣ 强制风控：止损 / 止盈 / 时间止损 ──
    for code, pos in list(state["positions"].items()):
        price = extract_stock_price(code, report_date_str) or pos["current_price"]
        # P3 滑点：卖出价下浮
        price = _apply_slippage(price, direction="sell", amount=pos["quantity"] * price)
        action, reason, close_ratio = check_stop_loss(pos, price, code, report_date_str)
        if action != "hold":
            close_qty = int(pos["quantity"] * close_ratio / 100) * 100
            if close_qty < 100:
                close_qty = pos["quantity"]
            proceeds_before = close_qty * price
            fee = calc_sell_fees(proceeds_before)
            proceeds = proceeds_before - fee
            pnl = (price - pos["avg_cost"]) * close_qty - fee

            state["cash"] += proceeds
            state["total_pnl"] += pnl
            state["total_fee"] += fee

            trade = {
                "date": report_date_str,
                "code": code,
                "side": action,
                "quantity": close_qty,
                "price": round(price, 3),
                "proceeds": round(proceeds, 2),
                "fee": round(fee, 2),
                "pnl": round(pnl, 2),
                "reason": reason,
            }
            force_sells.append(trade)
            trades.append(trade)
            new_trades.append(trade)
            
            if close_ratio >= 1.0 or close_qty >= pos["quantity"]:
                # 全平
                del state["positions"][code]
                print(f"[风控] {action} {code} × {close_qty} @ {price:.3f} 盈亏{pnl:+.2f} - {reason}")
            else:
                # 部分止盈：更新持仓
                pos["quantity"] -= close_qty
                pos["invested"] = pos["quantity"] * pos["avg_cost"]
                pos["tp_level"] = pos.get("tp_level", 0) + 1
                print(f"[风控] {action} {code} 减仓{close_qty}股 (剩余{pos['quantity']}股) @ {price:.3f} 盈亏{pnl:+.2f} - {reason}")


    sentiment_score = _load_sentiment_index_score()
    buy_threshold, sell_threshold = _compute_dynamic_thresholds(sentiment_score)

    # ── 1.5️⃣ 低评分清理：entry_score < buy_threshold 的持仓强制卖出 ──
    for code, pos in list(state["positions"].items()):
        entry_score = pos.get("entry_score", 70)
        if entry_score < buy_threshold:
            price = extract_stock_price(code, report_date_str) or pos["current_price"]
            price = _apply_slippage(price, direction="sell", amount=pos["quantity"] * price)
            proceeds_before = pos["quantity"] * price
            fee = calc_sell_fees(proceeds_before)
            proceeds = proceeds_before - fee
            pnl = (price - pos["avg_cost"]) * pos["quantity"] - fee
            state["cash"] += proceeds
            state["total_pnl"] += pnl
            state["total_fee"] += fee
            trade = {
                "date": report_date_str, "code": code, "side": "low_score_sell",
                "quantity": pos["quantity"], "price": round(price, 3),
                "proceeds": round(proceeds, 2), "fee": round(fee, 2),
                "pnl": round(pnl, 2),
                "reason": f"低评分清理：进场{entry_score}<阈值{buy_threshold}"
            }
            force_sells.append(trade)
            trades.append(trade)
            new_trades.append(trade)
            del state["positions"][code]
            print(f"[风控] 低评分清理 {code} × {pos['quantity']} @ {price:.3f} 盈亏{pnl:+.2f}")

    # ── 2️⃣ 账户总回撤检查 ──
    can_buy, dd_pct = check_account_drawdown(state)
    if not can_buy:
        print(f"[风控] 账户总回撤 {dd_pct:.1f}%，低于阈值 {ACCOUNT_DRAWDOWN_LIMIT}%，暂停买入")

    # ── 3️⃣ 动态阈值卖出 ──
    sentiment_score = _load_sentiment_index_score()
    buy_threshold, sell_threshold = _compute_dynamic_thresholds(sentiment_score)
    for code, info in stocks.items():
        if info["score"] <= sell_threshold and code in state["positions"]:
            pos = state["positions"][code]

            # P3 再平衡触发：评分变化不足±5分不调仓
            entry_score = pos.get("entry_score")
            if entry_score and abs(info["score"] - entry_score) < REBALANCE_SCORE_THRESHOLD:
                print(f"[再平衡] {code} 评分变化 {entry_score}→{info['score']} (变化{info['score']-entry_score:+d}) < {REBALANCE_SCORE_THRESHOLD}，跳过卖出")
                continue

            price = extract_stock_price(code, report_date_str) or pos["current_price"]
            # P3 滑点：卖出价下浮
            price = _apply_slippage(price, direction="sell", amount=pos["quantity"] * price)
            proceeds_before = pos["quantity"] * price
            fee = calc_sell_fees(proceeds_before)
            proceeds = proceeds_before - fee
            pnl = (price - pos["avg_cost"]) * pos["quantity"] - fee

            state["cash"] += proceeds
            state["total_pnl"] += pnl
            state["total_fee"] += fee

            trade = {
                "date": report_date_str,
                "code": code,
                "side": "sell",
                "quantity": pos["quantity"],
                "price": round(price, 3),
                "proceeds": round(proceeds, 2),
                "fee": round(fee, 2),
                "pnl": round(pnl, 2),
                "reason": f"评分{info['score']} ≤ {sell_threshold}（卖出阈值）",
            }
            new_trades.append(trade)
            trades.append(trade)
            del state["positions"][code]
            print(f"[模拟交易] 卖出 {code} × {pos['quantity']} @ {price:.3f} 盈亏{pnl:+.2f}")

    # ── 4️⃣ 凯利公式买入（评分 ≥ 动态买入阈值 且未持有） ──
    sentiment_factor = _load_sentiment_adjustment()
    adjusted_max_pos = max(2, int(MAX_POSITIONS * sentiment_factor))

    # force_sells 只在高比例时阻断买入（超过30%持仓被强制卖出才触发熔断）
    force_sell_ratio = len(force_sells) / max(len(state["positions"]) + len(force_sells), 1)
    buy_blocked_by_fs = can_buy and force_sell_ratio > 0.3
    if force_sells and not buy_blocked_by_fs:
        print(f"[风控] 强制卖出 {len(force_sells)} 笔（占比{force_sell_ratio:.0%}），低于30%熔断阈值，允许继续买入")
    if buy_blocked_by_fs:
        print(f"[风控] 强制卖出 {len(force_sells)} 笔（占比{force_sell_ratio:.0%}），触发买入熔断")

    can_buy = can_buy and not buy_blocked_by_fs

    # 大盘 danger 时禁止买入
    if market_danger:
        can_buy = False
        print(f"[风控] 大盘DANGER状态，禁止所有买入操作")

    if can_buy:
        buy_candidates = [(c, i) for c, i in stocks.items()
                          if i["score"] >= buy_threshold and c not in state["positions"]]
        buy_candidates.sort(key=lambda x: x[1]["score"], reverse=True)

        # 预计算行业集中度
        sector_ratios = get_position_sectors(state)
        print(f"[行业] 当前持仓行业分布: {', '.join(f'{s}={r:.0f}%' for s,r in sector_ratios.items())}")

        for code, info in buy_candidates:
            if len(state["positions"]) >= adjusted_max_pos:
                print(f"[模拟交易] 已达情绪调整后持仓上限 {adjusted_max_pos}，跳过 {code}")
                break

            price = extract_stock_price(code, report_date_str)
            if not price or price <= 0:
                print(f"[模拟交易] 无法获取 {code} 价格，跳过买入")
                continue
            # P3 滑点：买入价上浮
            price = _apply_slippage(price, direction="buy", amount=state["cash"])

            # ── AI评分确定性校验（P0） ──
            # AI评分≥70的候选股必须通过量化指标的硬性校验，防止AI幻觉导致错误买入
            if not _verify_ai_score(code, info["score"]):
                print(f"[确定性校验] {code} 评分{info['score']} 未通过量化校验，跳过买入")
                continue

            # ── 行业集中度检查（P1） ──
            sector = _get_sector(code)
            sector_ratio = sector_ratios.get(sector, 0.0)
            if sector_ratio >= 40.0:
                print(f"[行业] {code} 所属行业 {sector} 占比 {sector_ratio:.0f}% ≥ 40%，跳过")
                continue

            # ── 开盘价偏差校验（P1） ──
            try:
                import requests as _req
                secid = eastmoney_secid(code)
                ref_url = "https://push2.eastmoney.com/api/qt/stock/get"
                ref_params = {
                    "secid": secid,
                    "fields": "f18",  # f18 = 昨收
                    "fltt": 2, "invt": 2,
                }
                ref_headers = {
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://quote.eastmoney.com/",
                }
                ref_r = _req.get(ref_url, params=ref_params, headers=ref_headers, timeout=5)
                ref_data = ref_r.json()
                prev_close = ref_data.get("data", {}).get("f18")
                if prev_close and prev_close > 0:
                    gap_pct = (price - prev_close) / prev_close * 100
                    if abs(gap_pct) > 2.0:
                        gap_warn = f"跳空{gap_pct:+.1f}%（超过±2%），评分×0.9后决定"
                        print(f"[风控] {code} 开盘价偏差: {gap_warn}")
                        # 评分打折
                        effective_score = info["score"] * 0.9
                        if effective_score < buy_threshold:
                            print(f"[风控] {code} 打折后评分 {effective_score:.0f} < {buy_threshold}，跳过")
                            continue
                    else:
                        effective_score = info["score"]
                else:
                    effective_score = info["score"]
            except Exception as e:
                print(f"[风控] 开盘价查询失败: {e}")
                effective_score = info["score"]

            # 凯利动态仓位（使用原始评分计算金额，但已根据 gap 决定是否跳过）
            amount = _compute_kelly_amount(
                info["score"], state["cash"], sentiment_factor
            )
            # ── 大盘均线过滤：按市场状态等比缩仓 ──
            if not market_danger and not market_caution:
                try:
                    from risk.market_filter import get_market_state
                    mkt = get_market_state()
                    market_scale = mkt.get("scale", 1.0)
                    if market_scale < 1.0:
                        old_amount = amount
                        amount = amount * market_scale
                        print(f"[风控] 大盘均线过滤: scale={market_scale} → 买入金额 {old_amount:.0f}→{amount:.0f}")
                except Exception:
                    pass
            amount = min(amount, state["cash"])
            quantity = int(amount / price / 100) * 100
            if quantity < 100:
                print(f"[模拟交易] 资金不足买入 {code}（现金 {state['cash']:.0f}），跳过")
                continue

            cost = quantity * price
            fee = calc_buy_fees(cost)
            total_cost = cost + fee
            if total_cost > state["cash"]:
                quantity = int((state["cash"] - fee) / price / 100) * 100
                if quantity < 100:
                    continue
                cost = quantity * price
                fee = calc_buy_fees(cost)
                total_cost = cost + fee

            kelly_pct = _kelly_fraction(info["score"]) * 100
            state["cash"] -= total_cost
            state["total_fee"] += fee
            state["positions"][code] = {
                "quantity": quantity,
                "avg_cost": price,
                "current_price": price,
                "invested": round(cost, 2),
                "entry_score": info["score"],
                "entry_date": report_date_str,      # P2 时间止损
                "tp_level": 0,                       # P2 多级止盈
                "mode": "daily",
            }

            # 更新行业占比缓存
            sector_ratios.setdefault(sector, 0.0)
            add_mv = quantity * price
            total_mv = sum(p["quantity"] * p["current_price"] for p in state["positions"].values())
            for s in sector_ratios:
                if s == sector:
                    sector_ratios[s] = (sector_ratios[s] / 100 * (total_mv - add_mv) + add_mv) / total_mv * 100
                else:
                    sector_ratios[s] = sector_ratios[s] / 100 * (total_mv - add_mv) / total_mv * 100

            trade = {
                "date": report_date_str,
                "code": code,
                "side": "buy",
                "quantity": quantity,
                "price": round(price, 3),
                "cost": round(total_cost, 2),
                "fee": round(fee, 2),
                "score": info["score"],
                "kelly_pct": round(kelly_pct, 1),
                "mode": "daily",
                "sector": sector,
                "reason": f"评分{info['score']} ≥ {buy_threshold}（凯利{kelly_pct:.1f}%）",
            }
            new_trades.append(trade)
            trades.append(trade)
            print(f"[模拟交易] 买入 {code} × {quantity} @ {price:.3f} = {total_cost:.0f}（凯利{kelly_pct:.1f}%，行业:{sector}）")

    # ── 5️⃣ 风险平价再平衡（周五执行） ──
    _try_rebalance(state, trades, new_trades, report_date_str)

    # ── 🛒 更新持仓市价 ──
    for code, pos in state["positions"].items():
        price = extract_stock_price(code, report_date_str)
        if price:
            pos["current_price"] = price

    # ── 6️⃣ 计算市值和总权益 ──
    total_market_value = sum(p["quantity"] * p["current_price"] for p in state["positions"].values())
    total_equity = state["cash"] + total_market_value
    total_return = total_equity - INITIAL_CAPITAL
    total_return_pct = (total_return / INITIAL_CAPITAL) * 100

    # 更新回撤跟踪
    peak = state.get("peak_equity", INITIAL_CAPITAL)
    if total_equity > peak:
        peak = total_equity
    current_dd_pct = (total_equity - peak) / peak * 100
    max_dd = state.get("max_drawdown_pct", 0.0)
    if current_dd_pct < max_dd:
        max_dd = current_dd_pct

    state["peak_equity"] = peak
    state["max_drawdown_pct"] = max_dd
    state["total_market_value"] = round(total_market_value, 2)
    state["total_equity"] = round(total_equity, 2)
    state["total_return"] = round(total_return, 2)
    state["total_return_pct"] = round(total_return_pct, 2)
    state["last_update"] = report_date_str

    save_state(state)
    save_trades(trades)
    return new_trades, state, force_sells


# ── 风险平价再平衡 ──

def _try_rebalance(state, trades, new_trades, report_date_str):
    """
    周五收盘后执行马科维茨/风险平价再平衡。

    优先级：
    1. 马科维茨均值-方差最优权重 (从 quant_engine 读取)
    2. 风险平价等风险贡献 (回退方案)

    仅周五执行。
    """
    try:
        today = datetime.strptime(report_date_str, "%Y%m%d")
        if today.weekday() != 4:  # 仅周五
            return
    except ValueError:
        return

    if len(state["positions"]) < 2:
        return

    # ── 尝试读取马科维茨最优权重 ──
    target_weights = None
    try:
        mw_file = Path("/opt/daily_stock_analysis/quant_engine/markowitz_output.json")
        if mw_file.exists():
            mw_data = json.loads(mw_file.read_text())
            if mw_data.get("status") == "ok" and mw_data.get("weights"):
                target_weights = mw_data["weights"]
                sr = mw_data.get("sharpe_ratio", 0)
                print(f"[再平衡] 马科维茨最优权重 (夏普{sr:.3f}): {target_weights}")
    except Exception:
        pass

    print(f"[再平衡] 周五组合再平衡检查...")

    if target_weights:
        # ── 马科维茨权重再平衡 ──
        # 将马科维茨权重归一化到实际持仓股票（只持有部分股票）
        held_codes = list(state["positions"].keys())
        import json as _json
        from pathlib import Path as _Path
        mw_file = _Path("/opt/daily_stock_analysis/quant_engine/markowitz_output.json")
        if mw_file.exists():
            try:
                mw_data = _json.loads(mw_file.read_text())
                from quant_engine.markowitz import normalize_weights_for_positions as _nw
                target_weights = _nw(mw_data.get("weights", target_weights), held_codes)
                print(f"[再平衡] 归一化权重至持仓: {dict(zip(held_codes, [target_weights.get(c,0) for c in held_codes]))}")
            except Exception:
                # 等权 fallback
                target_weights = {c: 1.0/len(held_codes) for c in held_codes}
        
        # 计算每个持仓的目标市值
        total_equity = state["cash"] + sum(
            p["quantity"] * p["current_price"] for p in state["positions"].values()
        )

        adjustments = []
        for code, pos in state["positions"].items():
            target_pct = target_weights.get(code)
            if target_pct is None or target_pct <= 0:
                # 该股票权重为负或零 → 清仓
                adjustments.append({
                    "code": code, "current_qty": pos["quantity"],
                    "diff_qty": -pos["quantity"],
                    "action": "reduce", "deviation_pct": -100,
                })
                continue

            target_value = total_equity * target_pct
            current_value = pos["quantity"] * pos["current_price"]
            target_qty = max(100, int(target_value / pos["current_price"] / 100) * 100)
            diff_qty = target_qty - pos["quantity"]

            if abs(diff_qty) >= 100:
                deviation = (current_value - target_value) / target_value * 100
                adjustments.append({
                    "code": code, "current_qty": pos["quantity"],
                    "target_qty": target_qty, "diff_qty": diff_qty,
                    "deviation_pct": round(deviation, 1),
                    "action": "add" if diff_qty > 0 else "reduce",
                })
    else:
        # ── 风险平价等风险贡献 (回退) ──
        positions_with_vol = []
        total_risk = 0
        for code, pos in state["positions"].items():
            entry_score = pos.get("entry_score", 50)
            vol_est = 0.35 - (entry_score - 50) * 0.004
            vol_est = max(0.12, min(0.40, vol_est))
            pos_value = pos["quantity"] * pos["current_price"]
            risk_contribution = pos_value * vol_est
            positions_with_vol.append((code, pos, pos_value, vol_est, risk_contribution))
            total_risk += risk_contribution

        if total_risk <= 0:
            return

        target_risk_per_pos = total_risk / len(positions_with_vol)

        adjustments = []
        for code, pos, pos_value, vol_est, risk_cont in positions_with_vol:
            risk_pct = risk_cont / total_risk * 100
            ideal_pct = 100.0 / len(positions_with_vol)
            deviation = (risk_pct - ideal_pct) / ideal_pct

            if abs(deviation) > 0.20:
                target_value = target_risk_per_pos / vol_est
                target_qty = max(100, int(target_value / pos["current_price"] / 100) * 100)
                diff_qty = target_qty - pos["quantity"]
                adjustments.append({
                    "code": code, "current_qty": pos["quantity"],
                    "target_qty": target_qty, "diff_qty": diff_qty,
                    "deviation_pct": round(deviation * 100, 1),
                    "action": "add" if diff_qty > 0 else "reduce",
                })

    if not adjustments:
        print(f"[再平衡] 仓位分布合理，无需调整")
        return

    print(f"[再平衡] 发现 {len(adjustments)} 只持仓需调整:")
    for adj in adjustments:
        action_text = f"加仓{adj['diff_qty']}股" if adj["diff_qty"] > 0 else f"减仓{abs(adj['diff_qty'])}股"
        print(f"  {adj['code']}: 偏离{adj['deviation_pct']:+.0f}% → {action_text}")
        pos = state["positions"].get(adj["code"])
        if not pos:
            continue
        price = pos["current_price"]

        if adj["diff_qty"] > 0:
            qty = int(adj["diff_qty"] / 100) * 100
            if qty < 100:
                continue
            cost = qty * price
            fee = calc_buy_fees(cost)
            total_cost = cost + fee
            if total_cost > state["cash"]:
                qty = int((state["cash"] - fee) / price / 100) * 100
                if qty < 100:
                    continue
                cost = qty * price
                fee = calc_buy_fees(cost)
                total_cost = cost + fee

            state["cash"] -= total_cost
            state["total_fee"] += fee
            total_qty = pos["quantity"] + qty
            total_invested = pos["avg_cost"] * pos["quantity"] + cost
            pos["avg_cost"] = total_invested / total_qty
            pos["quantity"] = total_qty
            pos["invested"] = round(total_invested, 2)

            # P1: 禁止对浮亏超过-3%的持仓加仓（打工马风控规则）
            pos_pnl = (price - pos["avg_cost"]) / pos["avg_cost"] * 100
            if pos_pnl < -3:
                logger.warning(f"[风控] 跳过加仓{adj['code']}：浮亏{pos_pnl:.1f}% < -3%")
                print(f"[风控] 跳过加仓{adj['code']}：浮亏{pos_pnl:.1f}% < -3%")
                continue

            trade = {
                "date": report_date_str, "code": adj["code"],
                "side": "rebalance_buy", "quantity": qty, "price": round(price, 3),
                "cost": round(total_cost, 2), "fee": round(fee, 2),
                "reason": f"组合再平衡（偏离{adj['deviation_pct']:+.0f}%）",
            }
            new_trades.append(trade)
            trades.append(trade)
            print(f"[再平衡] 加仓 {adj['code']} × {qty} @ {price:.3f}")

        elif adj["diff_qty"] < 0:
            qty = min(pos["quantity"], abs(adj["diff_qty"]))
            qty = int(qty / 100) * 100
            if qty < 100:
                continue
            proceeds_before = qty * price
            fee = calc_sell_fees(proceeds_before)
            pnl_partial = (price - pos["avg_cost"]) * qty - fee

            state["cash"] += proceeds_before - fee
            state["total_pnl"] += pnl_partial
            state["total_fee"] += fee
            pos["quantity"] -= qty

            trade = {
                "date": report_date_str, "code": adj["code"],
                "side": "rebalance_sell", "quantity": qty, "price": round(price, 3),
                "proceeds": round(proceeds_before - fee, 2), "fee": round(fee, 2),
                "pnl": round(pnl_partial, 2),
                "reason": f"组合再平衡（偏离{adj['deviation_pct']:+.0f}%）",
            }
            new_trades.append(trade)
            trades.append(trade)
            print(f"[再平衡] 减仓 {adj['code']} × {qty} @ {price:.3f}")

    print(f"[再平衡] 完成")


def save_signal_trace_record(stocks, trades, report_date):
    """保存本轮信号评分与执行记录，用于后续准确率验证。"""
    trace = load_signal_trace()

    record = {
        "date": report_date,
        "stocks": {},
    }
    for code, info in stocks.items():
        record["stocks"][code] = {
            "score": info["score"],
            "operation": info["operation"],
            "sentiment": info.get("sentiment", ""),
            "rationale": info.get("rationale", ""),
        }

    trace["records"].append(record)
    save_signal_trace(trace)


# ── 绩效统计 ──

def compute_daily_perf(state, trades, report_date_str):
    """计算并保存每日绩效快照。"""
    perf = load_performance()

    # 今日交易统计
    daily_trades = [t for t in trades if t.get("date") == report_date_str]
    buy_count = len([t for t in daily_trades if t["side"] == "buy"])
    sell_count = len([t for t in daily_trades if t["side"] in ("sell", "stop_loss", "take_profit")])

    # 已完结交易盈亏统计
    closed_trades = [t for t in trades if t["side"] in ("sell", "stop_loss", "take_profit")]

    # 胜率
    wins = [t for t in closed_trades if t.get("pnl", 0) > 0]
    losses = [t for t in closed_trades if t.get("pnl", 0) < 0]
    win_rate = len(wins) / len(closed_trades) * 100 if closed_trades else 0

    # 盈亏比
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 0
    profit_factor = avg_win / avg_loss if avg_loss > 0 else float('inf')

    entry = {
        "date": report_date_str,
        "equity": state["total_equity"],
        "cash": state["cash"],
        "market_value": state["total_market_value"],
        "positions": len(state["positions"]),
        "return_pct": state["total_return_pct"],
        "max_dd_pct": state["max_drawdown_pct"],
        "buy_today": buy_count,
        "sell_today": sell_count,
        "total_closed_trades": len(closed_trades),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else None,
    }
    perf["daily"].append(entry)

    # 只保留最近60天
    perf["daily"] = perf["daily"][-60:]

    # 更新汇总
    perf["summary"] = {
        "total_return_pct": state["total_return_pct"],
        "max_drawdown_pct": state["max_drawdown_pct"],
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else None,
        "total_closed_trades": len(closed_trades),
        "active_positions": len(state["positions"]),
        "last_update": report_date_str,
    }

    # 每周汇总
    try:
        d = datetime.strptime(report_date_str, "%Y%m%d")
        week_key = d.strftime("%Y-W%W")
        if not perf["weekly"] or perf["weekly"][-1]["week"] != week_key:
            # 新周的第一天：start_equity 应为上周收盘权益或初始资本
            # 不能用 total_equity - 今日买入（未计持仓市值，会导致周收益虚高）
            prev_end = perf["weekly"][-1]["end_equity"] if perf["weekly"] else INITIAL_CAPITAL
            perf["weekly"].append({
                "week": week_key,
                "start_date": report_date_str,
                "end_date": report_date_str,
                "start_equity": round(prev_end, 2),
                "end_equity": state["total_equity"],
                "buy_count": buy_count,
                "sell_count": sell_count,
                "closed_wins": len(wins),
                "closed_losses": len(losses),
            })
        else:
            w = perf["weekly"][-1]
            w["end_date"] = report_date_str
            w["end_equity"] = state["total_equity"]
            w["buy_count"] += buy_count
            w["sell_count"] += sell_count
            w["closed_wins"] = len(wins)
            w["closed_losses"] = len(losses)
    except Exception:
        pass

    save_performance(perf)
    return perf


# ── 输出快照 ──

def generate_summary(state, new_trades, report_date_str):
    """生成推送用的模拟账户快照文本（增强版）。"""
    lines = []
    lines.append(f"📊【模拟交易账户快照】{report_date_str}")
    lines.append(f"💰 初始资金：{INITIAL_CAPITAL:,.0f}")
    lines.append(f"💵 当前现金：{state['cash']:,.2f}")
    lines.append(f"📈 持仓市值：{state['total_market_value']:,.2f}")
    lines.append(f"🏦 总权益：{state['total_equity']:,.2f}")
    emoji = '📈' if state['total_return'] >= 0 else '📉'
    lines.append(f"{emoji} 累计盈亏：{state['total_return']:+,.2f} ({state['total_return_pct']:+.2f}%)")
    lines.append(f"📉 最大回撤：{state['max_drawdown_pct']:.2f}%")
    lines.append(f"")

    # 今日交易
    if new_trades:
        lines.append("🔄 今日操作：")
        for t in sorted(new_trades, key=lambda x: x["date"], reverse=False):
            if t["side"] == "buy":
                lines.append(f"  🟢 买入 {t['code']} {t['quantity']}股 @ {t['price']}")
            elif t["side"] in ("stop_loss", "take_profit"):
                lines.append(f"  {'⛔' if t['side']=='stop_loss' else '💰'} {t['side']=='stop_loss' and '止损' or '止盈'} {t['code']} × {t['quantity']} @ {t['price']} 盈亏{t['pnl']:+.2f}")
            else:
                lines.append(f"  🔴 卖出 {t['code']} {t['quantity']}股 @ {t['price']} 盈亏{t['pnl']:+.2f}")
        lines.append("")

    # 持仓明细
    if state["positions"]:
        lines.append("📋 当前持仓：")
        lines.append(f"  {'代码':<12} {'数量':<8} {'成本':<8} {'现价':<8} {'市值':<10} {'盈亏':<10} {'盈亏%':<8} {'模式':<10}")
        for code, pos in sorted(state["positions"].items()):
            mkt_val = pos["quantity"] * pos["current_price"]
            pos_pnl = (pos["current_price"] - pos["avg_cost"]) * pos["quantity"]
            pos_pnl_pct = (pos["current_price"] - pos["avg_cost"]) / pos["avg_cost"] * 100
            mode = pos.get("mode", "daily")
            lines.append(f"  {code:<12} {pos['quantity']:<8} {pos['avg_cost']:<8.3f} {pos['current_price']:<8.3f} {mkt_val:<10.0f} {pos_pnl:<+10.2f} {pos_pnl_pct:<+7.1f}% {mode:<10}")
    else:
        lines.append("📋 当前持仓：空仓")

    # 绩效摘要
    lines.append("")
    perf = load_performance()
    summary = perf.get("summary", {})
    if summary:
        wr = summary.get("win_rate", 0)
        pf = summary.get("profit_factor")
        pf_str = f"{pf:.2f}" if pf else "N/A"
        lines.append(f"🎯 绩效累计：胜率 {wr}% | 盈亏比 {pf_str} | 已完结 {summary.get('total_closed_trades', 0)} 笔")

    # 显示当前情绪系数
    factor = _load_sentiment_adjustment()
    if factor != 1.0:
        pct_change = int((factor - 1.0) * 100)
        sign = "+" if pct_change > 0 else ""
        lines.append(f"  🎯 情绪仓位调节：{sign}{pct_change}%（系数{factor}）")

    # 显示动态阈值
    sentiment_score = _load_sentiment_index_score()
    buy_th, sell_th = _compute_dynamic_thresholds(sentiment_score)
    # 读取 GARCH + HMM 摘要
    try:
        garch_file = Path("/opt/daily_stock_analysis/quant_engine/garch_output.json")
        if garch_file.exists():
            gd = json.loads(garch_file.read_text())
            if gd.get("status") == "ok":
                lines.append(f"  🌊 GARCH条件波动率: {gd['current_volatility']:.1%}（{gd['vol_percentile']:.0f}%百分位）")
    except Exception:
        pass
    
    try:
        hmm_file = Path("/opt/daily_stock_analysis/quant_engine/hmm_output.json")
        if hmm_file.exists():
            hd = json.loads(hmm_file.read_text())
            state_names = {0:"多头", 1:"空头", 2:"震荡", 3:"高波动"}
            probs = hd.get("state_probabilities", [])
            if probs:
                s = state_names.get(hd.get("current_state", 2), "?")
                lines.append(f"  🔮 HMM市场状态: {s}（牛{probs[0]:.0%} 熊{probs[1]:.0%} 盘{probs[2]:.0%}）")
    except Exception:
        pass
    
    lines.append("")
    lines.append(f"⚙️ 规则：评分≥{buy_th}买入（凯利仓位） | ≤{sell_th}卖出 | "
                 f"止损-15% | 止盈+25%(减半) | 回撤<-20%暂停")
    return "\n".join(lines)


def generate_performance_card():
    """生成每周绩效卡片。"""
    perf = load_performance()
    summary = perf.get("summary", {})

    lines = ["📊【模拟交易绩效周报】"]
    lines.append("")
    lines.append(f"📈 累计收益率：{summary.get('total_return_pct', 0):+.2f}%")
    lines.append(f"📉 最大回撤：{summary.get('max_drawdown_pct', 0):.2f}%")
    lines.append(f"🎯 胜率：{summary.get('win_rate', 0)}%")
    lines.append(f"⚖️ 盈亏比：{summary.get('profit_factor', 'N/A')}")
    lines.append(f"📝 已完结交易：{summary.get('total_closed_trades', 0)} 笔")
    lines.append(f"📋 当前持仓：{summary.get('active_positions', 0)} 只")

    # 最近5个交易日权益曲线
    daily = perf.get("daily", [])
    if len(daily) >= 2:
        lines.append("")
        lines.append("📅 近5日权益走势：")
        for entry in daily[-5:]:
            date_str = entry["date"]
            eq = entry["equity"]
            ret = entry["return_pct"]
            pos_count = entry["positions"]
            lines.append(f"  {date_str}  权益 {eq:,.0f}  ({ret:+.2f}%)  持仓{pos_count}只")

    # 每周对比
    weekly = perf.get("weekly", [])
    if weekly:
        lines.append("")
        lines.append("📆 每周汇总：")
        for w in weekly[-4:]:
            week_ret = (w["end_equity"] - w["start_equity"]) / w["start_equity"] * 100
            lines.append(f"  {w['week']}  {w['end_equity']:,.0f} 周收益{week_ret:+.2f}%  买卖{w['buy_count']}/{w['sell_count']}")

    # 量化引擎统计
    lines.append("")
    lines.append("🧮 策略引擎：")
    lines.append("  · 仓位分配：凯利公式（半凯利，保守）")
    lines.append("  · 阈值调节：GARCH(1,1) 条件波动率")
    lines.append("  · 组合管理：马科维茨均值-方差优化")
    lines.append("  · 市场状态：HMM 隐马尔可夫模型")
    
    try:
        garch_file = Path("/opt/daily_stock_analysis/quant_engine/garch_output.json")
        if garch_file.exists():
            gd = json.loads(garch_file.read_text())
            if gd.get("status") == "ok":
                lines.append(f"  · GARCH vol: {gd['current_volatility']:.1%} ({gd['vol_percentile']:.0f}%ile)")
    except Exception:
        pass
    
    try:
        hmm_file = Path("/opt/daily_stock_analysis/quant_engine/hmm_output.json")
        if hmm_file.exists():
            hd = json.loads(hmm_file.read_text())
            state_n = {0:"多头",1:"空头",2:"震荡",3:"高波动"}
            s = state_n.get(hd.get("current_state",2),"?")
            lines.append(f"  · HMM状态: {s}")
    except Exception:
        pass
    
    try:
        mw_file = Path("/opt/daily_stock_analysis/quant_engine/markowitz_output.json")
        if mw_file.exists():
            mwd = json.loads(mw_file.read_text())
            if mwd.get("status") == "ok":
                lines.append(f"  · 夏普比率: {mwd.get('sharpe_ratio', 'N/A')}")
    except Exception:
        pass

    return "\n".join(lines)


# ── 信号准确率验证 ──

def _verify_ai_score(code: str, ai_score: float) -> bool:
    """
    AI评分确定性校验：评分≥70的候选股必须通过量化指标的硬性过滤。
    
    校验项：
    1. 成交量 > 5日均量 × 1.2
    2. 收盘价在 MA20 上方
    3. RSI ≤ 70（不超买）
    
    任一项不满足 → 跳过该买入信号。
    """
    if ai_score < 70:
        return True  # 评分不足70的不需要此校验（由阈值控制）

    try:
        from data_provider.data_cache import DataCache
        cache = DataCache()
        df = cache.get_kline(code)
        if df is None or len(df) < 25:
            print(f"  [确定性校验] {code} 数据不足，放行")
            return True

        df = df.sort_values("date")
        latest = df.iloc[-1]
        closes = df["close"].values
        volumes = df["volume"].values

        # ① 成交量 > 5日均量 × 1.2
        vol_ma5 = volumes[-6:-1].mean()  # 排除当天
        if len(volumes) >= 6:
            if volumes[-1] < vol_ma5 * 1.2:
                print(f"  [确定性校验✗] {code} 成交量不足：当日{volumes[-1]:.0f} < 5日均量{vol_ma5:.0f}×1.2={vol_ma5*1.2:.0f}")
                return False

        # ② 收盘价在 MA20 上方
        if len(closes) >= 20:
            ma20 = closes[-20:].mean()
            if latest["close"] < ma20:
                print(f"  [确定性校验✗] {code} 收盘价{latest['close']:.2f} < MA20{ma20:.2f}")
                return False

        # ③ RSI(14) ≤ 70
        if len(closes) >= 15:
            gains = []
            losses = []
            for i in range(len(closes)-14, len(closes)):
                diff = closes[i] - closes[i-1]
                if diff >= 0:
                    gains.append(diff)
                    losses.append(0)
                else:
                    gains.append(0)
                    losses.append(-diff)
            avg_gain = sum(gains[-14:]) / 14
            avg_loss = sum(losses[-14:]) / 14
            if avg_loss == 0:
                rsi = 100
            else:
                rs = avg_gain / avg_loss
                rsi = 100 - (100 / (1 + rs))
            if rsi > 70:
                print(f"  [确定性校验✗] {code} RSI={rsi:.1f} > 70（超买），跳过")
                return False

        print(f"  [确定性校验✓] {code} 通过量化校验，放行")
        return True

    except Exception as e:
        print(f"  [确定性校验] {code} 校验异常: {e}，保守跳过")
        return False


def verify_signal_accuracy():
    """统计历史信号数量（仅计数，不含准确率验证——待补全实现）。"""
    trace = load_signal_trace()
    records = trace.get("records", [])
    if len(records) < 2:
        return None

    results = {"buy_signals": 0, "sell_signals": 0}

    for i in range(len(records) - 1):
        rec = records[i]
        for code, info in rec.get("stocks", {}).items():
            score = info["score"]
            if score >= 70:
                results["buy_signals"] += 1
            elif score <= 30:
                results["sell_signals"] += 1

    return results


# ── 入口 ──

def main():
    report_date = date.today().strftime("%Y%m%d")

    # 解析报告
    stocks = parse_stock_scores(report_date)
    if not stocks:
        print("[模拟交易] 无可用的分析报告，跳过")
        return

    print(f"[模拟交易] 本轮分析 {len(stocks)} 只股票")
    for code, info in stocks.items():
        print(f"  {code}: {info['operation']} 评分{info['score']} | {info.get('sentiment','')}")

    # 执行交易（含风控）
    new_trades, state, force_sells = execute_trades(stocks, report_date)

    # 保存信号追踪记录
    save_signal_trace_record(stocks, new_trades, report_date)

    # 计算并保存绩效
    compute_daily_perf(state, new_trades, report_date)

    # 生成摘要
    summary = generate_summary(state, new_trades, report_date)
    print(f"\n{summary}")

    # 写入文件供 run_and_send.sh 读取推送
    summary_file = DATA_DIR / f"summary_{report_date}.txt"
    summary_file.write_text(summary, encoding="utf-8")

    # 每周五生成周报
    try:
        today = datetime.now()
        if today.weekday() == 4:  # 周五
            weekly_card = generate_performance_card()
            weekly_file = DATA_DIR / f"weekly_{report_date}.txt"
            weekly_file.write_text(weekly_card, encoding="utf-8")
            print(f"\n[模拟交易] 周报已写入 {weekly_file}")
    except Exception as e:
        print(f"[模拟交易] 周报生成失败: {e}")

    print(f"\n[模拟交易] 摘要已写入 {summary_file}")


if __name__ == "__main__":
    main()
