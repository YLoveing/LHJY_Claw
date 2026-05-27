#!/usr/bin/env python3
"""
批量准备100只股票的回测数据：K线补齐 + 基本面缓存
=============================================
1. 从现有缓存中选100只数据最全的股票
2. 确保每只都有 >= 250 条日K线（~1年）
3. 预拉取基本面数据并缓存到磁盘
4. 输出股票列表供回测使用
"""
import sys, os, json, time, logging
from pathlib import Path
from datetime import datetime

sys.path.insert(0, "/opt/daily_stock_analysis")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("prepare_100")

TARGET_COUNT = 100
MIN_KLINE = 250  # 至少250条日K（~1年交易日）
CACHE_DIR = Path("/opt/daily_stock_analysis/data/cache")
KLINECACHE_FILE = CACHE_DIR / "fundamentals" / "backtest_100_fundamental_cache.json"


def select_top_100() -> list:
    """从缓存中选100只数据最全的股票"""
    from data_provider.data_cache import DataCache
    cache = DataCache()
    stats = cache.stats()
    kline_dir = Path(stats["cache_root"]) / "kline"
    
    stocks = []
    for f in os.listdir(kline_dir):
        if not f.endswith("_meta.json"):
            continue
        code = f.replace("_meta.json", "")
        try:
            with open(kline_dir / f) as fp:
                meta = json.load(fp)
            rows = meta.get("rows", 0)
            stocks.append((code, rows))
        except:
            pass
    
    stocks.sort(key=lambda x: -x[1])
    logger.info(f"缓存中共 {len(stocks)} 只股票，取前 {TARGET_COUNT} 只")
    return [s[0] for s in stocks[:TARGET_COUNT]]


def ensure_kline(stocks: list):
    """确保所有股票K线数据足够，不足的从 fallback 补"""
    from data_provider.data_cache import DataCache
    from data_provider.fallback_provider import get_fallback_provider
    
    cache = DataCache()
    provider = get_fallback_provider()
    
    need_fill = []
    for code in stocks:
        meta = cache.kline_meta(code)
        rows = meta.get("rows", 0) if meta else 0
        if rows < MIN_KLINE:
            need_fill.append((code, rows))
    
    if need_fill:
        logger.warning(f"需要补充K线的股票: {len(need_fill)} 只")
        for code, rows in need_fill:
            logger.info(f"  {code}: {rows}行 → 拉取中...")
            try:
                df = provider.get_kline_data(code, days=400)
                if df is not None and len(df) > rows:
                    cache.save_kline(code, df)
                    logger.info(f"    ✅ {code}: 已保存 {len(df)} 行")
            except Exception as e:
                logger.error(f"    ❌ {code}: {e}")
    else:
        logger.info(f"✅ 全部 {TARGET_COUNT} 只股票K线数据达标 (≥{MIN_KLINE}行)")


def load_or_fetch_fundamentals(stocks: list) -> dict:
    """加载或拉取基本面缓存"""
    KLINECACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    # 尝试加载已有缓存
    if KLINECACHE_FILE.exists():
        with open(KLINECACHE_FILE) as f:
            cached = json.load(f)
        logger.info(f"加载基本面缓存: {len(cached)} 只")
    else:
        cached = {}
    
    # 找出需要拉取的股票
    to_fetch = [c for c in stocks if c not in cached]
    if not to_fetch:
        logger.info("✅ 全部股票基本面数据已缓存")
        return cached
    
    logger.info(f"需要拉取基本面: {len(to_fetch)} 只股票")
    
    # 用 akshare 批量拉基本面数据
    import akshare as ak
    
    total = len(to_fetch)
    for i, code in enumerate(to_fetch):
        try:
            code6 = code.zfill(6)
            fund = {}
            
            # 1. 财务指标 (ROE, 营收/利润增速)
            try:
                df = ak.stock_financial_abstract_ths(symbol=code6)
                if df is not None and not df.empty:
                    latest = df.iloc[0]
                    fund["roe"] = float(latest.get("净资产收益率", 0) or 0)
                    fund["revenue_yoy"] = float(latest.get("营业收入同比增长", 0) or 0)
                    fund["profit_yoy"] = float(latest.get("净利润同比增长", 0) or 0)
                    fund["gross_margin"] = float(latest.get("毛利率", 0) or 0)
            except Exception:
                pass
            
            # 2. 估值指标 (PE, PB)
            try:
                df2 = ak.stock_a_lg_indicator(symbol=code6)
                if df2 is not None and not df2.empty:
                    latest2 = df2.iloc[0]
                    fund["pe"] = float(latest2.get("pe", 0) or 0)
                    fund["pb"] = float(latest2.get("pb", 0) or 0)
                    fund["market_cap"] = float(latest2.get("market_cap", 0) or 0)
            except Exception:
                pass
            
            cached[code] = fund
            logger.info(f"  [{i+1}/{total}] {code} ✅ ROE={fund.get('roe', 'N/A')}")
            
            # 限速：每秒最多5次请求
            if i > 0 and i % 5 == 0:
                time.sleep(0.5)
                
        except Exception as e:
            cached[code] = {}
            logger.warning(f"  [{i+1}/{total}] {code} ❌ {e}")
    
    # 保存缓存
    with open(KLINECACHE_FILE, "w") as f:
        json.dump(cached, f)
    logger.info(f"基本面缓存已保存: {KLINECACHE_FILE}")
    
    return cached


def save_stock_list(stocks: list):
    """保存100只股票列表供回测使用"""
    out = CACHE_DIR / "stock_list" / "backtest_100.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    
    # 顺便从 K线缓存取每只股票的名字
    from data_provider.data_cache import DataCache
    cache = DataCache()
    
    stock_info = []
    for code in stocks:
        kdata = cache.get_kline(code)
        name = ""
        stock_info.append({"code": code, "name": name})
    
    with open(out, "w") as f:
        json.dump({"count": len(stocks), "stocks": stock_info, "updated_at": datetime.now().isoformat()}, f, indent=2)
    logger.info(f"股票列表已保存: {out}")


def main():
    logger.info("=" * 60)
    logger.info("开始准备100只股票回测数据")
    logger.info("=" * 60)
    
    t0 = time.time()
    
    # Step 1: 选100只
    stocks = select_top_100()
    logger.info(f"\n📌 Step 1: 选定 {len(stocks)} 只股票")
    
    # Step 2: 补K线
    logger.info(f"\n📌 Step 2: 检查K线数据")
    ensure_kline(stocks)
    
    # Step 3: 拉基本面
    logger.info(f"\n📌 Step 3: 拉取基本面数据")
    fundamentals = load_or_fetch_fundamentals(stocks)
    
    # 统计基本面覆盖率
    has_fund = sum(1 for v in fundamentals.values() if v and v.get("roe", 0) != 0)
    logger.info(f"基本面覆盖: {has_fund}/{len(stocks)} 只")
    
    # Step 4: 保存列表
    save_stock_list(stocks)
    
    elapsed = time.time() - t0
    logger.info(f"\n✅ 全部完成! 耗时: {elapsed:.1f}s")


if __name__ == "__main__":
    main()
