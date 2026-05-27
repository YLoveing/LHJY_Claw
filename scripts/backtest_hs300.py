#!/usr/bin/env python3
"""
准备沪深300成分股回测数据 + 跑回测
===================================
1. 拉取300只成分股列表
2. 用 fallback 数据源补全K线
3. 用V1评分逻辑跑回测
"""
import sys, os, json, time, logging, math
from pathlib import Path
from datetime import datetime

sys.path.insert(0, "/opt/daily_stock_analysis")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("hs300")

import pandas as pd
import numpy as np

CACHE_DIR = Path("/opt/daily_stock_analysis/data/cache")
HS300_CACHE_FILE = CACHE_DIR / "stock_list" / "hs300.json"
OUTPUT_DIR = Path("/opt/daily_stock_analysis/reports")

MIN_KLINE = 250
INITIAL_CASH = 30000.0
TOP_N = 5


def get_hs300_stocks() -> list:
    """获取沪深300成分股列表"""
    if HS300_CACHE_FILE.exists():
        with open(HS300_CACHE_FILE) as f:
            return json.load(f)
    
    import akshare as ak
    df = ak.index_stock_cons(symbol="000300")
    stocks = df.to_dict('records')
    HS300_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HS300_CACHE_FILE, "w") as f:
        json.dump(stocks, f, ensure_ascii=False)
    logger.info(f"沪深300成分股: {len(stocks)} 只")
    return stocks


def ensure_kline(stocks: list):
    """补全K线数据"""
    from data_provider.data_cache import DataCache
    from data_provider.fallback_provider import get_fallback_provider
    from data_provider.mootdx_fetcher import MootdxFetcher
    
    cache = DataCache()
    provider = get_fallback_provider()
    mootdx = MootdxFetcher()
    
    codes = [s['品种代码'] for s in stocks] if isinstance(stocks[0], dict) else stocks
    
    to_fill = []
    for code in codes:
        meta = cache.kline_meta(code)
        rows = meta.get("rows", 0) if meta else 0
        if rows < MIN_KLINE:
            to_fill.append((code, rows))
    
    if not to_fill:
        logger.info(f"✅ 全部 {len(codes)} 只K线达标")
        return codes
    
    logger.info(f"需要补K线: {len(to_fill)} 只")
    
    # 先用 mootdx 批量拉
    mootdx_ok = 0
    tencent_ok = 0
    fail = 0
    
    for i, (code, old_rows) in enumerate(to_fill):
        logger.info(f"  [{i+1}/{len(to_fill)}] {code} (现有{old_rows}行)...")
        
        # 方法1: mootdx
        try:
            df = mootdx.get_daily_data(code, days=400)
            if df is not None and len(df) > old_rows:
                cache.save_kline(code, df)
                mootdx_ok += 1
                logger.info(f"    ✅ mootdx: {len(df)} 行")
                continue
        except Exception as e:
            logger.debug(f"    mootdx: {e}")
        
        # 方法2: fallback (腾讯)
        try:
            df = provider.get_kline_data(code, days=400)
            if df is not None and len(df) > old_rows:
                cache.save_kline(code, df)
                tencent_ok += 1
                logger.info(f"    ✅ 腾讯: {len(df)} 行")
                continue
        except Exception as e:
            logger.debug(f"    腾讯: {e}")
        
        fail += 1
        logger.warning(f"    ❌ 所有数据源失败")
        
        # 限速
        if i > 0 and i % 10 == 0:
            time.sleep(0.5)
    
    logger.info(f"补全完成: mootdx={mootdx_ok}, 腾讯={tencent_ok}, 失败={fail}")
    return codes


# ── V1 评分函数（已验证 +35.72%） ──

def calc_technicals(df):
    """V1 技术评分（与 backtest_100.py 一致）"""
    if df is None or len(df) < 30:
        return {"score": 0, "ma5": 0, "ma20": 0, "ma60": 0, "rsi": 50}
    
    close = df["close"].values
    volume = df["volume"].values if "volume" in df.columns else None
    ma5 = close[-5:].mean() if len(close) >= 5 else close.mean()
    ma20 = close[-20:].mean() if len(close) >= 20 else close.mean()
    ma60 = close[-60:].mean() if len(close) >= 60 else close.mean()
    price = close[-1]
    score = 50
    
    # 趋势 (40分)
    trend = 0
    if ma5 > ma20:
        trend += 15
    if ma20 > ma60:
        trend += 15
    if price > ma5:
        trend += 10
    elif price < ma20:
        trend -= 10
    score += trend
    
    # 量价 (30分)
    vol_score = 0
    if volume is not None and len(volume) >= 5:
        vol_ma5 = volume[-5:].mean()
        if vol_ma5 > 0:
            vr = volume[-1] / vol_ma5
            if vr > 1.5:
                vol_score += 15
            elif vr > 1.2:
                vol_score += 10
            elif vr > 1.0:
                vol_score += 5
            if vr < 0.5:
                vol_score -= 10
    score += vol_score
    
    # RSI (15分)
    if len(close) >= 15:
        gains = losses = 0
        for i in range(-14, 0):
            diff = close[i] - close[i-1]
            if diff > 0:
                gains += diff
            else:
                losses -= diff
        rsi = 50 if losses == 0 else (gains / (gains + losses) * 100) if gains + losses > 0 else 50
        if rsi > 70:
            score -= 5
        elif rsi < 30:
            score += 5
        else:
            score += 5
    
    # 波动率 (15分)
    if len(close) >= 20:
        rets = [(close[i] - close[i-1]) / close[i-1] for i in range(-19, 0)]
        vol = max(rets) - min(rets)
        if vol < 0.05:
            score += 8
        elif vol > 0.15:
            score -= 5
        else:
            score += 3
    
    return {"score": max(0, min(100, score)), "ma5": ma5, "ma20": ma20, "ma60": ma60}


def load_data(codes: list) -> dict:
    """加载所有K线"""
    from data_provider.data_cache import DataCache
    cache = DataCache()
    
    all_kline = {}
    for code in codes:
        df = cache.get_kline(code)
        if df is not None and len(df) >= MIN_KLINE:
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date")
            all_kline[code] = df
    
    logger.info(f"有效K线: {len(all_kline)}/{len(codes)} 只")
    return all_kline


def run_backtest(all_kline: dict) -> dict:
    """V1回测逻辑：每日Top5评分，次日收盘卖出"""
    # 所有交易日
    all_dates = set()
    for df in all_kline.values():
        all_dates.update(df["date"].dt.strftime("%Y%m%d").values)
    all_dates = sorted(all_dates)
    logger.info(f"交易日: {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)} 天)")
    
    cash = INITIAL_CASH
    position = {}
    trades = []
    equity_curve = []
    peak = INITIAL_CASH
    
    # 跳过前20天
    start_idx = next((i for i, d in enumerate(all_dates) if i >= 20), 20)
    
    for i in range(start_idx, len(all_dates)):
        today = all_dates[i]
        
        # ── 卖出 ──
        if position:
            for code in list(position.keys()):
                kline = all_kline.get(code)
                if kline is None:
                    continue
                td = kline[kline["date"] == pd.Timestamp(today)]
                price = td["close"].iloc[-1] if not td.empty else kline["close"].iloc[-1]
                pos = position[code]
                val = pos["qty"] * price
                fee = val * 0.0005
                cash += val - fee
                pnl = (price - pos["avg_cost"]) * pos["qty"] - fee
                trades.append({"date": today, "code": code, "action": "sell", "qty": pos["qty"], "price": price, "pnl": pnl})
            position = {}
        
        # ── 评分选股 ──
        candidates = []
        for code, kline in all_kline.items():
            td = kline[kline["date"] <= pd.Timestamp(today)]
            if len(td) < 30:
                continue
            tech = calc_technicals(td)
            candidates.append({"code": code, "score": tech["score"], "price": td["close"].iloc[-1]})
        
        candidates.sort(key=lambda x: -x["score"])
        top = candidates[:TOP_N]
        
        # ── 买入 ──
        if top:
            per = cash / len(top)
            for c in top:
                price = c["price"]
                if price <= 0:
                    continue
                qty = max(int(per / price / 100) * 100, int(per / price))
                if qty <= 0:
                    continue
                cost = qty * price
                fee = cost * 0.0005
                total = cost + fee
                if total > cash:
                    continue
                cash -= total
                position[c["code"]] = {"qty": qty, "avg_cost": price}
                trades.append({"date": today, "code": c["code"], "action": "buy", "qty": qty, "price": price, "score": c["score"]})
        
        # ── 权益 ──
        pos_val = 0
        for code, pos in position.items():
            kline = all_kline.get(code)
            if kline is None:
                continue
            td = kline[kline["date"] == pd.Timestamp(today)]
            price = td["close"].iloc[-1] if not td.empty else kline["close"].iloc[-1]
            pos_val += pos["qty"] * price
        eq = cash + pos_val
        equity_curve.append({"date": today, "equity": eq})
        peak = max(peak, eq)
    
    # 最后平仓
    for code, pos in position.items():
        kline = all_kline.get(code)
        if kline is None:
            continue
        price = kline["close"].iloc[-1]
        val = pos["qty"] * price
        cash += val - val * 0.0005
    
    final = cash
    
    # ── 指标 ──
    total_ret = (final - INITIAL_CASH) / INITIAL_CASH * 100
    max_dd = 0
    pk = INITIAL_CASH
    for e in equity_curve:
        pk = max(pk, e["equity"])
        dd = (e["equity"] - pk) / pk * 100
        max_dd = min(max_dd, dd)
    
    win = sum(1 for t in trades if t.get("pnl", 0) > 0)
    loss = sum(1 for t in trades if t.get("pnl", 0) < 0)
    wr = win / (win + loss) * 100 if (win + loss) > 0 else 0
    
    daily_ret = []
    for i in range(1, len(equity_curve)):
        r = (equity_curve[i]["equity"] - equity_curve[i-1]["equity"]) / equity_curve[i-1]["equity"]
        daily_ret.append(r)
    
    sharpe = (np.mean(daily_ret) / np.std(daily_ret) * math.sqrt(244)) if daily_ret and np.std(daily_ret) > 0 else 0
    
    return {
        "strategy": "纯技术评分(V1)",
        "stock_universe": len(all_kline),
        "trading_days": len(equity_curve),
        "total_trades": win + loss,
        "total_return_pct": round(total_ret, 2),
        "final_value": round(final, 2),
        "win_rate_pct": round(wr, 2),
        "max_drawdown_pct": round(abs(max_dd), 2),
        "sharpe_ratio": round(sharpe, 2),
        "wins": win,
        "losses": loss,
        "equity_curve": equity_curve,
        "trades": trades,
    }


def main():
    t0 = time.time()
    logger.info("=" * 60)
    logger.info("沪深300成分股 × 1年 回测")
    logger.info("=" * 60)
    
    # Step 1: 成分股
    stocks = get_hs300_stocks()
    codes = [s['品种代码'] for s in stocks]
    logger.info(f"成分股: {len(codes)} 只")
    
    # Step 2: 补K线
    ensure_kline(codes)
    
    # Step 3: 加载K线
    all_kline = load_data(codes)
    
    if len(all_kline) < 50:
        logger.error(f"有效股票不足50只 ({len(all_kline)})，终止")
        return
    
    # Step 4: 回测
    result = run_backtest(all_kline)
    
    # Step 5: 输出
    print("\n" + "=" * 60)
    print(f"{'策略':<20} {'收益%':>8} {'最终':>10} {'胜率%':>8} {'回撤%':>8} {'夏普':>8} {'交易':>8}")
    print("=" * 60)
    print(f"{result['strategy']:<20} {result['total_return_pct']:>+8.2f} "
          f"{result['final_value']:>10.2f} {result['win_rate_pct']:>8.2f} "
          f"{result['max_drawdown_pct']:>8.2f} {result['sharpe_ratio']:>8.2f} "
          f"{result['total_trades']:>8}")
    print("=" * 60)
    
    # 保存
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / f"backtest_hs300_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(out, "w") as f:
        json.dump({"run_date": datetime.now().isoformat(), "stock_count": len(all_kline),
                    "result": result}, f, ensure_ascii=False, indent=2)
    logger.info(f"结果已保存: {out}")
    logger.info(f"总耗时: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
