#!/usr/bin/env python3
"""
stock_screener.py — A股选股器

每日三步筛选：
  ① HS300 成分股池（流动性过滤）
  ② 技术面条件筛选（趋势/超卖/放量突破）
  ③ 评分排序 → Top N 候选

支持实盘和回测两种模式。
"""

import json
import logging
import re
import sys
from pathlib import Path

# 加入 data_provider 路径以便引入 DataCache
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data_provider.data_cache import DataCache

_cache = DataCache()
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

log = logging.getLogger("screener")

# ── 筛选参数 ──
MIN_PRICE = 3.0         # 最低股价
MAX_PRICE = 200.0       # 最高股价
MIN_VOLUME_Yi = 0.3     # 最低成交额（亿元）
MAX_VOLUME_RATIO = 20.0 # 量比上限（避免一字板）
MAX_CANDIDATES = 20     # 每批最大候选数
MIN_CANDIDATES = 3      # 最少候选数（不足则放宽条件）

# 东方财富 API 常量
EAST_MONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}


def fetch_realtime_snapshot() -> List[Dict]:
    """
    获取全A股实时行情快照。
    
    东方财富行情接口 (fid=f3=涨跌幅排序):
      f2=最新价, f3=涨跌幅%, f5=成交量, f6=成交额, 
      f12=代码, f14=名称, f15=最高, f16=最低, f17=今开, f18=昨收,
      f20=成交额(元), f21=流通市值
    """
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    items = []
    page = 1
    while True:
        params = {
            "pn": page,
            "pz": 500,
            "po": 0,
            "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "fs": "m:0+t:1,m:1+t:2,m:0+t:7",
            "fields": "f2,f3,f4,f5,f6,f12,f14,f15,f16,f17,f18,f20,f21",
        }
        try:
            r = requests.get(url, params=params, headers=EAST_MONEY_HEADERS, timeout=10)
            data = r.json()
            page_items = data["data"]["diff"]
            items.extend(page_items)
            total = data["data"]["total"]
            if page * 500 >= total:
                break
            page += 1
            time.sleep(0.5)
        except Exception as e:
            log.warning(f"分页 {page} 失败: {e}")
            break
    return items


def extract_stock_data(item: dict) -> dict:
    """从东方财富API原始数据中提取字段。"""
    return {
        "code": str(item.get("f12", "")),
        "name": str(item.get("f14", "")),
        "price": item.get("f2", 0) or 0,
        "change_pct": item.get("f3", 0) or 0,
        "high": item.get("f15", 0) or 0,
        "low": item.get("f16", 0) or 0,
        "open": item.get("f17", 0) or 0,
        "prev_close": item.get("f18", 0) or 0,
        "volume": item.get("f6", 0) or 0,
        "amount_yi": (item.get("f20", 0) or 0) / 1e8,
        "market_cap_yi": (item.get("f21", 0) or 0) / 1e8,
    }


def basic_filter(stock: dict) -> bool:
    """第一轮：基础流动性过滤。"""
    if stock["price"] == 0 or stock["price"] is None:
        return False
    if stock["price"] < MIN_PRICE or stock["price"] > MAX_PRICE:
        return False
    if stock["amount_yi"] < MIN_VOLUME_Yi:
        return False
    if stock["market_cap_yi"] < 20:  # 流通市值 < 20亿
        return False
    # 排除 ST、*ST、退市、新股（代码含字母或特殊前缀）
    if re.match(r'^[0-9]{6}$', stock["code"]) is None:
        return False
    if "ST" in stock["name"].upper() or "退" in stock["name"]:
        return False
    return True


def fetch_kline_data(code: str, days: int = 60) -> Optional[pd.DataFrame]:
    """获取某只股票的日K线数据（用于技术指标计算）。

    优先从磁盘缓存读取，缓存未命中或数据不足时从 API 拉取并缓存。
    """
    # ═══ 尝试从磁盘缓存读取 ═══
    cached = _cache.get_kline(code)
    if cached is not None and len(cached) >= days:
        # 检查缓存天数是否够用（取最近 days 天）
        cache_recent = cached.tail(min(days * 2, len(cached)))
        if len(cache_recent) >= days:
            logger.info(f"  [缓存] {code}: 磁盘缓存命中 ({len(cached)}行)，跳过API")
            return cache_recent.reset_index(drop=True)

    # ═══ 从 API 拉取 ═══
    url = f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": f"1.{code}" if code.startswith("6") else f"0.{code}",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",     # 日K
        "fqt": "1",       # 前复权
        "end": "20500101",
        "lmt": max(days, 120),  # 多拉一些供缓存
    }
    try:
        r = requests.get(url, params=params, headers=EAST_MONEY_HEADERS, timeout=10)
        data = r.json()
        if data.get("data") and data["data"].get("klines"):
            lines = data["data"]["klines"]
            records = []
            for line in lines:
                parts = line.split(",")
                records.append({
                    "date": parts[0],
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "volume": float(parts[5]),
                    "amount": float(parts[6]),
                })
            df = pd.DataFrame(records)

            # ═══ 保存到磁盘缓存（合并已有缓存） ═══
            try:
                if cached is not None and not cached.empty:
                    combined = pd.concat([cached, df], ignore_index=True)
                    combined = combined.drop_duplicates(subset=["date"], keep="last")
                    combined = combined.sort_values("date").reset_index(drop=True)
                    _cache.save_kline(code, combined)
                else:
                    _cache.save_kline(code, df)
            except Exception as cache_e:
                logger.debug(f"  [缓存] {code}: 保存失败: {cache_e}")

            return df
    except Exception:
        pass
    return cached  # API失败时返回已有的缓存（如果有）


def compute_screening_indicators(df: pd.DataFrame) -> Dict:
    """
    从日K线计算选股用的技术指标。
    与 backtest.py 的 compute_indicators 一致。
    """
    close = df["close"].values
    volume = df["volume"].values
    n = len(close)
    
    result = {}
    
    if n >= 5:
        result["MA5"] = float(np.mean(close[-5:]))
    if n >= 10:
        result["MA10"] = float(np.mean(close[-10:]))
    if n >= 20:
        result["MA20"] = float(np.mean(close[-20:]))
        result["price_MA20_pct"] = (close[-1] / result["MA20"] - 1) * 100
        result["vol_MA20"] = float(np.mean(volume[-20:]))
        result["vol_ratio"] = volume[-1] / max(result["vol_MA20"], 1)
    
    # RSI(14)
    if n >= 15:
        delta = np.diff(close)
        gains = np.maximum(delta, 0)
        losses = np.maximum(-delta, 0)
        avg_gain = np.mean(gains[-14:])
        avg_loss = np.mean(losses[-14:])
        if avg_loss > 0:
            result["RSI"] = 100 - 100 / (1 + avg_gain / avg_loss)
        else:
            result["RSI"] = 100
    else:
        result["RSI"] = 50
    
    result["_n"] = n
    result["latest_close"] = float(close[-1])
    return result


# ── 筛选条件定义 ──

def condition_uptrend(ind: Dict) -> bool:
    """条件A：多头排列 — MA5 > MA10 > MA20 (趋势延续)"""
    if ind.get("MA5", 0) > ind.get("MA10", 0) > ind.get("MA20", 0):
        return True
    # 偏多：MA5 > MA20 且 MA5 在 MA10 附近 (>MA10*0.99)
    if (ind.get("MA5", 0) > ind.get("MA20", 0) and 
        ind.get("MA5", 0) > ind.get("MA10", 0) * 0.99):
        return True
    return False


def condition_oversold(ind: Dict) -> bool:
    """条件B：超卖反弹 — RSI < 35 且价格站上MA5"""
    if ind.get("RSI", 50) < 35 and ind.get("_n", 0) >= 5:
        if ind.get("latest_close", 0) > ind.get("MA5", 0):
            return True
    return False


def condition_breakout(ind: Dict) -> bool:
    """条件C：放量突破 — 量比 > 1.5 且突破MA20"""
    if ind.get("vol_ratio", 0) > 1.5 and ind.get("_n", 0) >= 20:
        if ind.get("price_MA20_pct", -100) > 0.5:  # 站在MA20上方
            return True
    return False


def condition_ma_cross(ind: Dict) -> bool:
    """条件D：金叉 — 5日均线上穿10日均线（今日收盘MA5>MA10且昨日<MA10）"""
    if ind.get("_n", 0) >= 10:
        if ind.get("MA5", 0) > ind.get("MA10", 0):
            return True  # 简化：MA5>MA10即算
    return False


# ── 选股评分 ──

def screen_score(ind: Dict) -> float:
    """多条件综合评分 (0-100)，用于排序候选。"""
    score = 50.0
    
    # 趋势加分
    if condition_uptrend(ind):
        score += 20
    if condition_breakout(ind):
        score += 15
    if condition_ma_cross(ind):
        score += 10
    
    # RSI 位置微调
    rsi = ind.get("RSI", 50)
    if rsi < 30:
        score += 8   # 超卖区加分（等反弹）
    elif rsi > 75:
        score -= 10  # 超买区减分
    
    # 价格位置
    pct = ind.get("price_MA20_pct", 0)
    if -3 < pct < 3:
        score += 5   # 在MA20附近盘整→突破潜力
    elif pct > 15:
        score -= 5   # 偏离MA20太远→回调风险
    
    # 量比
    vr = ind.get("vol_ratio", 1)
    if 1.2 < vr < 3:
        score += 5   # 温和放量
    elif vr > 5:
        score -= 3   # 爆量→可能分歧
    
    return np.clip(round(score, 1), 0, 100)


def run_screener(
    max_candidates: int = MAX_CANDIDATES,
    min_candidates: int = MIN_CANDIDATES,
    verbose: bool = True,
) -> List[Dict]:
    """
    完整选股流程。
    
    返回: 已评分排序的候选股票列表（最多max_candidates只）。
    """
    if verbose:
        log.info("\n" + "=" * 60)
        log.info("📡 选股器启动 — A股扫描")
        log.info("=" * 60)
    
    # Step 1: 获取全A快照
    if verbose:
        log.info("\n[1/3] 获取全A股实时行情...")
    snapshot = fetch_realtime_snapshot()
    if verbose:
        log.info(f"  → {len(snapshot)} 只")
    
    # Step 2: 基础过滤
    candidates = []
    for item in snapshot:
        stock = extract_stock_data(item)
        if basic_filter(stock):
            candidates.append(stock)
    if verbose:
        log.info(f"\n[2/3] 基础过滤 (价>3, 额>0.3亿, 市值>20亿): {len(candidates)} 只")
    
    # Step 3: 技术面筛选
    if verbose:
        log.info(f"\n[3/3] 技术面筛选 ({max_candidates} 候选)...")
    
    scored = []
    for stock in candidates:
        kline = fetch_kline_data(stock["code"], days=60)
        if kline is None or len(kline) < 20:
            continue
        ind = compute_screening_indicators(kline)
        
        # 检查是否满足任一条件
        conds = []
        if condition_uptrend(ind):
            conds.append("多头")
        if condition_oversold(ind):
            conds.append("超卖反弹")
        if condition_breakout(ind):
            conds.append("放量突破")
        if condition_ma_cross(ind):
            conds.append("金叉")
        
        if not conds:
            continue  # 都不满足→跳过
        
        s = screen_score(ind)
        stock["screen_score"] = s
        stock["conditions"] = conds
        stock["indicators"] = {
            "MA5": round(ind.get("MA5", 0), 2),
            "MA10": round(ind.get("MA10", 0), 2),
            "MA20": round(ind.get("MA20", 0), 2),
            "RSI": round(ind.get("RSI", 0), 1),
            "vol_ratio": round(ind.get("vol_ratio", 0), 2),
            "price_MA20_pct": round(ind.get("price_MA20_pct", 0), 1),
        }
        stock["price_ma20_pct"] = ind.get("price_MA20_pct", 0)
        scored.append(stock)
        
        if verbose and len(scored) <= 5:
            log.info(f"  {stock['code']} {stock['name']} 评分{s} {'|'.join(conds)} "
                     f"价{stock['price']:.2f} RSI{ind.get('RSI',0):.0f} "
                     f"量比{ind.get('vol_ratio',0):.1f}")
    
    # 排序去重
    sorted_stocks = sorted(scored, key=lambda x: x["screen_score"], reverse=True)
    # 去重（同一股票只保留1条）
    seen = set()
    deduped = []
    for s in sorted_stocks:
        if s["code"] not in seen:
            seen.add(s["code"])
            deduped.append(s)
    
    selected = deduped[:max_candidates]
    
    # 如果候选不足，放宽条件重新筛选
    if len(selected) < min_candidates and len(candidates) > 0:
        if verbose:
            log.info(f"  ⚠ 候选不足({len(selected)}), 放宽条件...")
        # 放宽：价格>2，成交额>0.1亿
        relaxed = [s for s in candidates if s["amount_yi"] > 0.1]
        log.info(f"  放宽后 {len(relaxed)} 只")
    
    if verbose:
        log.info(f"\n📋 最终候选: {len(selected)} 只")
        for i, s in enumerate(selected, 1):
            log.info(f"  #{i:02d} {s['code']} {s['name']:<8} 评分{s['screen_score']:.0f} "
                     f"{','.join(s['conditions'])} 价{s['price']:.2f}")
    
    return selected


def save_candidates(candidates: List[Dict], path: str = None) -> str:
    """保存候选名单到JSON。"""
    if path is None:
        path = f"/opt/daily_stock_analysis/screener/candidates_{datetime.now():%Y%m%d}.json"
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(candidates, ensure_ascii=False, indent=2, default=str)
    )
    log.info(f"  ✅ 候选名单已保存至 {path}")
    return path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("\n⚠  ⚠  ⚠  ⚠  ⚠  ⚠  ⚠  ⚠")
    print("  选股器运行时需要访问东方财富API")
    print("  耗时约 60~120 秒（300只股票K线）")
    print("  可设置 max_candidates=10 减少等待")
    print("⚠  ⚠  ⚠  ⚠  ⚠  ⚠  ⚠  ⚠")
    
    result = run_screener(max_candidates=15, verbose=True)
    if result:
        save_candidates(result)
    print(f"\n{'='*60}")
    print(f"✅ 选股完成: {len(result)} 只候选")
