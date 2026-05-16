# -*- coding: utf-8 -*-
"""
mx_fetcher.py — 妙想金融数据抓取模块

包装妙想 Skills，补充现有系统缺少的数据能力：
- mx-search：金融资讯搜索（新闻、公告、研报）
- mx-xuangu：智能选股（多条件筛选）
- mx-data：财务数据查询（营收、利润、PE/PB等基本面）

注：实时行情和K线仍使用现有的 push2.eastmoney.com API（更稳定、结构简单）。
妙想在这些场景优势不明显，但在深度财务数据和智能搜索上补了短板。
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

_HOME = Path.home()
SCRIPTS_DIR = _HOME / ".openclaw/workspace/skills"
# 妙想输出目录（可通过 MX_OUTPUT_DIR 环境变量覆盖，P2）
MX_OUTPUT_ENV = os.environ.get("MX_OUTPUT_DIR", "")
if MX_OUTPUT_ENV:
    OUTPUT_DIR = Path(MX_OUTPUT_ENV)
else:
    OUTPUT_DIR = _HOME / ".openclaw/workspace/mx_data/output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

SCRIPTS = {
    "mx_data": SCRIPTS_DIR / "mx-data/mx_data.py",
    "mx_search": SCRIPTS_DIR / "mx-search/mx_search.py",
    "mx_xuangu": SCRIPTS_DIR / "mx-xuangu/mx_xuangu.py",
}

# 缓存
_CACHE: Dict[str, Tuple[float, Any]] = {}
CACHE_TTL = 300  # 5 分钟


# ── 内部工具 ──

def _get_apikey() -> str:
    key = os.environ.get("MX_APIKEY", "")
    if not key:
        bashrc = Path.home() / ".bashrc"
        if bashrc.exists():
            try:
                for line in bashrc.read_text().splitlines():
                    m = re.search(r'export MX_APIKEY="([^"]+)"', line)
                    if m:
                        key = m.group(1)
                        break
            except (PermissionError, OSError) as e:
                logger.warning(f"无法读取 ~/.bashrc 获取 MX_APIKEY: {e}")
    return key


def _run_script(script_key: str, query: str) -> bool:
    """执行妙想脚本，成功返回 True"""
    script = SCRIPTS.get(script_key)
    if not script or not script.exists():
        logger.error(f"脚本不存在: {script}")
        return False

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.run(
            [sys.executable, str(script), query],
            cwd=str(script.parent),
            capture_output=True,
            text=True,
            env={"MX_APIKEY": _get_apikey(), **os.environ},
            timeout=60,
        )
        return True
    except subprocess.TimeoutExpired:
        logger.warning(f"脚本超时: {script_key}")
    except Exception as e:
        logger.error(f"脚本执行失败: {script_key}: {e}")
    return False


def _find_latest(prefix: str, suffix: str) -> Optional[Path]:
    """在输出目录中找最新的匹配文件"""
    files = sorted(OUTPUT_DIR.glob(f"{prefix}*{suffix}"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _cache_get(key: str) -> Any:
    item = _CACHE.get(key)
    if item and time.time() - item[0] < CACHE_TTL:
        return item[1]
    if key in _CACHE:
        del _CACHE[key]
    return None


def _cache_set(key: str, value: Any):
    _CACHE[key] = (time.time(), value)
    if len(_CACHE) > 100:
        old_keys = sorted(_CACHE.keys(), key=lambda k: _CACHE[k][0])[:20]
        for k in old_keys:
            del _CACHE[k]


# ── 1. 资讯搜索（mx-search） ──

def search_news(keyword: str, count: int = 10) -> List[Dict[str, str]]:
    """
    搜索金融资讯（新闻、公告、研报）。
    mx_search 只输出格式化文本到 stdout，需解析。
    
    Args:
        keyword: 搜索关键词
        count: 返回条数
        
    Returns:
        [{"title": ..., "content": ..., "date": ..., "source": ..., "type": ...}]
    """
    cache_key = f"news:{keyword}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached[:count]

    script = SCRIPTS.get("mx_search")
    if not script or not script.exists():
        return []

    try:
        result = subprocess.run(
            [sys.executable, str(script), keyword],
            cwd=str(script.parent),
            capture_output=True, text=True, timeout=60,
            env={"MX_APIKEY": _get_apikey(), **os.environ},
        )
    except Exception:
        return []

    results = []
    current = {}
    for line in result.stdout.split("\n"):
        if line.startswith("--- ") and " ---" in line:
            if current.get("title"):
                results.append(current)
            current = {"title": "", "content": "", "date": "", "source": "", "type": "资讯"}
            # Extract title from --- N. title ---
            title_part = line.split(" ---", 1)[0].split(". ", 1)
            if len(title_part) > 1:
                current["title"] = title_part[1].strip()
        elif line.startswith("机构:") or line.startswith("来源:"):
            current["source"] = line.split(":", 1)[1].strip()
        elif line.startswith("日期:"):
            current["date"] = line.split(":", 1)[1].strip()
        elif line.startswith("类型:"):
            current["type"] = line.split(":", 1)[1].strip()
        elif current.get("title") and not line.startswith("---"):
            current["content"] += line.strip() + " "

    if current.get("title"):
        results.append(current)

    _cache_set(cache_key, results)
    return results[:count]


# ── 2. 智能选股（mx-xuangu） ──

def _map_xuangu_cols(df: pd.DataFrame) -> pd.DataFrame:
    """映射选股CSV的中文列名为英文"""
    mapping = {}
    for col in df.columns:
        col_s = str(col)
        if "市场代码简称" in col_s: mapping[col] = "exchange"
        elif "代码" in col_s: mapping[col] = "code"
        elif "名称" in col_s: mapping[col] = "name"
        elif "最新价" in col_s: mapping[col] = "price"
        elif "涨跌幅" in col_s: mapping[col] = "change_pct"
        elif "涨跌额" in col_s: mapping[col] = "change"
        elif "最高" in col_s: mapping[col] = "high"
        elif "最低" in col_s: mapping[col] = "low"
        elif "换手率" in col_s: mapping[col] = "turnover_rate"
        elif "量比" in col_s: mapping[col] = "volume_ratio"
        elif "成交量" in col_s: mapping[col] = "volume"
        elif "成交额" in col_s: mapping[col] = "amount"
        elif "市盈率" in col_s: mapping[col] = "pe"
        elif "市净率" in col_s: mapping[col] = "pb"
        elif "总市值" in col_s: mapping[col] = "market_cap"
        elif "流通市值" in col_s: mapping[col] = "float_market_cap"
        elif "序号" in col_s: mapping[col] = "rank"
    if mapping:
        df = df.rename(columns=mapping)
        # 只保留映射过的列
        keep = [c for c in df.columns if c in mapping.values()]
        if keep:
            df = df[keep]
    return df


def screen_stocks(condition: str) -> List[Dict[str, Any]]:
    """
    智能选股。
    
    Args:
        condition: 自然语言条件
        
    Returns:
        [{"code": str, "name": str, "price": float, ...}, ...]
    """
    cache_key = f"xuangu:{condition}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if not _run_script("mx_xuangu", condition):
        return []

    csv_file = _find_latest("mx_xuangu_", ".csv")
    if csv_file:
        try:
            df = pd.read_csv(csv_file, encoding="utf-8")
            df = _map_xuangu_cols(df)
            # 数值列转float
            for col in ["price", "change_pct", "turnover_rate", "volume_ratio", "pe", "pb", "market_cap"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            results = df.fillna("").to_dict(orient="records")
            _cache_set(cache_key, results)
            return results
        except Exception as e:
            logger.error(f"CSV读取失败: {e}")

    return []


# ── 3. 财务数据查询（mx-data） ──

def get_financial(code: str, indicators: str = "收盘价 涨跌幅 成交量") -> Optional[pd.DataFrame]:
    """
    查询个股财务/行情数据（利用妙想deep检索能力）。
    
    Args:
        code: 6位股票代码
        indicators: 查询指标描述，"净利润 营业收入 每股收益 近三年"
        
    Returns:
        DataFrame 或无数据返回 None
    """
    query = f"{code} {indicators}"
    cache_key = f"fin:{code}:{indicators}"
    
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if not _run_script("mx_data", query):
        return None

    # 读取 xlsx
    xlsx_file = _find_latest("mx_data_", ".xlsx")
    if not xlsx_file:
        return None

    try:
        sheets = pd.read_excel(xlsx_file, sheet_name=None)
    except Exception as e:
        logger.error(f"xlsx读取失败: {e}")
        return None

    if not sheets:
        return None

    # 选最佳sheet：优先选包含收盘/开盘/营收/利润的
    df_result = None
    best_score = -1
    sheet_names = list(sheets.keys())
    
    for name, df in sheets.items():
        cols = [str(c).lower() for c in df.columns]
        score = 0
        score += 10 if any("收盘" in c for c in cols) else 0
        score += 8 if any("开盘" in c for c in cols) else 0
        score += 5 if any("净利润" in c or "营收" in c or "收入" in c for c in cols) else 0
        score += 3 if any("成交量" in c for c in cols) else 0
        score += 2 if any("每股" in c for c in cols) else 0
        score += 2 if any("涨跌幅" in c for c in cols) else 0
        if score > best_score:
            best_score = score
            df_result = df

    if df_result is None:
        df_result = list(sheets.values())[0]

    if df_result is None or len(df_result) == 0:
        return None

    # 清理：日期列去 "(日)" 后缀
    df_result = df_result.rename(columns=_normalize_col)
    for col in df_result.columns:
        if "date" in col.lower():
            df_result[col] = df_result[col].astype(str).str.replace(r"\(日\)", "", regex=True)
            df_result[col] = df_result[col].str.replace(r"至.*", "", regex=True)

    _cache_set(cache_key, df_result)
    return df_result


# ── 4. 个股实时财务指标（mx-data 包装） ──

def get_realtime_fundamentals(code: str) -> Dict[str, Any]:
    """
    获取个股实时基本面数据（PE、PB、ROE等）。
    这是 mx-data 特有的价值——现有push2 API不提供全面的财务指标。
    
    Args:
        code: 6位股票代码
        
    Returns:
        dict 含 pe, pb, roe, revenue, net_profit 等
    """
    df = get_financial(code, "最新市盈率 市净率 每股收益 净资产收益率")
    if df is None or len(df) == 0:
        return {}

    # 取最新一行
    row = df.iloc[0]
    result = {}
    col_map = _REALTIME_FIELD_MAP
    for col in df.columns:
        eng = col_map.get(str(col), str(col))
        val = row[col]
        try:
            val = float(str(val).replace("%", "").replace("亿元", "").replace("元", "").strip())
        except (ValueError, TypeError):
            pass
        result[eng] = val
    return result


# ── 列名映射 ──

def _normalize_col(name: str) -> str:
    """中文字段名→标准英文"""
    name = str(name).strip()
    mapping = {
        "date": "date",
        "日期": "date",
        "收盘价": "close",
        "收盘": "close",
        "开盘价": "open",
        "开盘": "open",
        "最高价": "high",
        "最低价": "low",
        "涨跌幅": "change_pct",
        "复权单位净值增长率": "change_pct",
        "涨跌额": "change",
        "成交量": "volume",
        "成交额": "amount",
        "成交金额": "amount",
        "最新价": "price",
        "昨收": "prev_close",
        "换手率": "turnover_rate",
        "区间换手率": "turnover_rate",
        "市盈率": "pe",
        "市净率": "pb",
        "总市值": "market_cap",
        "流通市值": "float_market_cap",
        "量比": "volume_ratio",
        "振幅": "amplitude",
        "净利润": "net_profit",
        "营业收入": "revenue",
        "每股收益": "eps",
        "净资产收益率": "roe",
        "毛利率": "gross_margin",
        "资产负债率": "debt_ratio",
        "每股净资产": "bps",
        "名称": "name",
        "代码": "code",
    }
    return mapping.get(name, name)


_REALTIME_FIELD_MAP = {v: v for v in [
    "price", "open", "high", "low", "close", "change_pct", "change",
    "volume", "amount", "pe", "pb", "eps", "roe", "revenue", "net_profit",
    "market_cap", "float_market_cap", "turnover_rate", "volume_ratio", "bps",
]}


# ── 缓存管理 ──

def clear_cache():
    _CACHE.clear()


def cache_stats() -> Dict:
    now = time.time()
    alive = sum(1 for v in _CACHE.values() if now - v[0] < CACHE_TTL)
    return {"total_entries": len(_CACHE), "alive_entries": alive, "ttl_seconds": CACHE_TTL}


# ── CLI ──

def main():
    import argparse
    parser = argparse.ArgumentParser(description="妙想数据查询")
    parser.add_argument("type", choices=["news", "screener", "financial", "fundamentals", "test"],
                        help="查询类型")
    parser.add_argument("--code", help="股票代码")
    parser.add_argument("--keyword", help="搜索关键词")
    parser.add_argument("--condition", help="选股条件")
    parser.add_argument("--indicators", default="收盘价 涨跌幅 成交量",
                        help="财务指标")
    parser.add_argument("--count", type=int, default=10, help="返回条数")

    args = parser.parse_args()

    os.environ["MX_APIKEY"] = _get_apikey()

    if args.type == "test":
        print("🔍 妙想接口测试")
        print(f"  MX_APIKEY: {'已设置' if _get_apikey() else '未设置'}")
        for name, path in SCRIPTS.items():
            print(f"  {name}: {'✅' if path.exists() else '❌'} {path}")
        print(f"  输入目录: {OUTPUT_DIR}")
        return

    if args.type == "news" and args.keyword:
        results = search_news(args.keyword, args.count)
        print(f"🔍 找到 {len(results)} 条资讯:")
        for r in results[:5]:
            print(f"  📰 {r['title']} ({r['date']})")
        return

    if args.type == "screener" and args.condition:
        results = screen_stocks(args.condition)
        print(f"🔍 选股结果: {len(results)} 只")
        for r in results[:5]:
            print(f"  {r}")
        return

    if args.type == "financial" and args.code:
        df = get_financial(args.code, args.indicators)
        if df is not None and len(df) > 0:
            print(df.head(10).to_string())
        else:
            print("无数据")
        return

    if args.type == "fundamentals" and args.code:
        data = get_realtime_fundamentals(args.code)
        if data:
            import json
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print("无数据")
        return


if __name__ == "__main__":
    main()
