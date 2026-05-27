#!/usr/bin/env python3
"""
拉取中证500成分股 + 估值数据缓存（同花顺接口）
==================================================
1. 成分股列表 → data/cache/stock_list/csi500.json (已完成)
2. PE/PB/ROE/营收增速 → data/cache/fundamentals/csi500_cache.json

使用 stock_financial_abstract_ths（同花顺接口，已验证可用）
+ 实时价格从 mootdx 拿（计算PE/PB）
"""
import sys, json, time, logging
from pathlib import Path

sys.path.insert(0, "/opt/daily_stock_analysis")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("pull_csi500")

CACHE_DIR = Path("/opt/daily_stock_analysis/data/cache")
STOCK_LIST_FILE = CACHE_DIR / "stock_list" / "csi500.json"
FUND_CACHE_FILE = CACHE_DIR / "fundamentals" / "csi500_cache.json"


def _safe_pct(v):
    """'12.34%' → 12.34，None/False → None"""
    if v is None or v is False:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().rstrip('%')
    try:
        return float(s)
    except:
        return None


def load_stock_list() -> list:
    """加载已保存的成分股列表"""
    stocks = json.load(open(STOCK_LIST_FILE))
    codes = [s['品种代码'] for s in stocks]
    logger.info(f"成分股: {len(codes)} 只")
    return codes


def get_prices_from_cache(codes: list) -> dict:
    """从K线缓存拿最新收盘价"""
    from data_provider.data_cache import DataCache
    cache = DataCache()
    prices = {}
    for code in codes:
        df = cache.get_kline(code)
        if df is not None and not df.empty:
            prices[code] = float(df["close"].iloc[-1])
    logger.info(f"有K线数据的: {len(prices)}/{len(codes)}")
    return prices


def pull_financial_data(codes: list, prices: dict) -> dict:
    """用同花顺接口拉财务+估值数据"""
    import akshare as ak

    # 加载已有缓存
    if FUND_CACHE_FILE.exists():
        with open(FUND_CACHE_FILE) as f:
            cached = json.load(f)
        logger.info(f"已有缓存: {len(cached)} 只")
    else:
        cached = {}

    to_fetch = [c for c in codes if c not in cached]
    if not to_fetch:
        logger.info("✅ 全部已缓存")
        return cached

    total = len(to_fetch)
    success = 0

    for i, code in enumerate(to_fetch):
        fund = {}

        # Step 1: 同花顺财务数据
        try:
            code6 = code.zfill(6)
            df = ak.stock_financial_abstract_ths(symbol=code6)
            if df is not None and not df.empty:
                latest = df.iloc[-1]  # 取最新
                fund["roe"] = _safe_pct(latest.get("净资产收益率"))
                fund["revenue_yoy"] = _safe_pct(latest.get("营业总收入同比增长率"))
                fund["profit_yoy"] = _safe_pct(latest.get("净利润同比增长率"))
                fund["gross_margin"] = _safe_pct(latest.get("销售毛利率"))
                fund["net_margin"] = _safe_pct(latest.get("销售净利率"))
                fund["eps"] = latest.get("基本每股收益", None)  # 每股收益（元）
                fund["bps"] = latest.get("每股净资产", None)    # 每股净资产（元）
                # 数字处理
                try:
                    fund["eps"] = float(fund["eps"]) if fund["eps"] and fund["eps"] is not False else None
                except:
                    fund["eps"] = None
                try:
                    fund["bps"] = float(fund["bps"]) if fund["bps"] and fund["bps"] is not False else None
                except:
                    fund["bps"] = None
        except Exception as e:
            logger.debug(f"{code} ths失败: {e}")

        # Step 2: 计算PE/PB
        price = prices.get(code)
        if price and price > 0:
            if fund.get("eps"):
                fund["pe"] = round(price / fund["eps"], 2)
            if fund.get("bps") and fund["bps"] > 0:
                fund["pb"] = round(price / fund["bps"], 2)
        fund["price"] = price

        cached[code] = fund

        if fund.get("roe") is not None or fund.get("pe") is not None:
            success += 1
            pe_str = f"PE={fund['pe']}" if fund.get('pe') else "PE=?"
            logger.info(f"  [{i+1}/{total}] {code} ✅ {pe_str} ROE={fund.get('roe')}%")
        else:
            logger.warning(f"  [{i+1}/{total}] {code} ❌ 无数据")

        # 限速：同花顺接口限制较严
        time.sleep(0.3)

    # 保存
    with open(FUND_CACHE_FILE, "w") as f:
        json.dump(cached, f)
    logger.info(f"✅ 缓存完成: {success}/{total} 成功, 共 {len(cached)} 只")
    return cached


def main():
    t0 = time.time()
    logger.info("=" * 50)
    logger.info("中证500估值数据（同花顺接口）")
    logger.info("=" * 50)

    if not STOCK_LIST_FILE.exists():
        logger.error(f"成分股列表不存在，请先运行 pull_constituents")
        return

    codes = load_stock_list()
    prices = get_prices_from_cache(codes)
    fund_data = pull_financial_data(codes, prices)

    # 统计
    has_roe = sum(1 for v in fund_data.values() if v and v.get("roe") is not None)
    has_pe = sum(1 for v in fund_data.values() if v and v.get("pe") is not None)
    has_pb = sum(1 for v in fund_data.values() if v and v.get("pb") is not None)

    print("\n" + "=" * 50)
    print("📊 中证500数据统计")
    print("=" * 50)
    print(f"  成分股: {len(codes)} 只")
    print(f"  有ROE: {has_roe} 只")
    print(f"  有PE:  {has_pe} 只")
    print(f"  有PB:  {has_pb} 只")
    print(f"  缓存:  {FUND_CACHE_FILE}")
    print(f"  耗时:  {time.time()-t0:.1f}s")
    print("=" * 50)

    # 显示PE最低前5
    valid = [(c, v) for c, v in fund_data.items() if v and v.get("pe")]
    valid.sort(key=lambda x: x[1]["pe"])
    print("\nPE最低 TOP 5:")
    for c, v in valid[:5]:
        print(f"  {c}: PE={v['pe']} PB={v.get('pb')} ROE={v.get('roe')}%")


if __name__ == "__main__":
    main()
