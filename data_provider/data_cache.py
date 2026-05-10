"""
data_cache.py — 数据持久化磁盘缓存层

为 API 获取的基础数据提供磁盘级缓存，避免重复拉取。
每日 K 线等历史数据是不可变的，缓存后永久可用。

缓存结构：
  data/cache/
    kline/
      {stock_code}.parquet       # 该股全量日 K 线（合并增量追加）
      {stock_code}_meta.json     # 最后更新日期等元信息
    screener/
      all_codes.json             # 全 A 股代码列表快照
    fundamentals/
      {stock_code}_fund.json     # 基本面数据快照

使用方式：
  cache = DataCache()
  df = cache.get_kline("600519")
  if df is None:
      df = fetch_from_api(...)
      cache.save_kline("600519", df)
"""

import json
import logging
import os
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Dict, Any

import pandas as pd

logger = logging.getLogger(__name__)

# ── 缓存根目录 ──
DEFAULT_CACHE_ROOT = Path("/opt/daily_stock_analysis/data/cache")


class DataCache:
    """
    磁盘缓存管理器。

    线程安全，支持逐股票追加式缓存 K 线数据。
    """

    def __init__(self, cache_root: Optional[Path] = None):
        self._root = Path(cache_root or DEFAULT_CACHE_ROOT)
        self._kline_dir = self._root / "kline"
        self._screener_dir = self._root / "screener"
        self._fund_dir = self._root / "fundamentals"
        self._stock_list_dir = self._root / "stock_list"

        for d in [self._kline_dir, self._screener_dir, self._fund_dir, self._stock_list_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # 写锁（逐股票粒度）
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    def _lock(self, key: str) -> threading.Lock:
        with self._locks_lock:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    # ──────────── K 线缓存 ────────────

    def _kline_path(self, stock_code: str) -> Path:
        return self._kline_dir / f"{stock_code}.pkl"

    def _kline_meta_path(self, stock_code: str) -> Path:
        return self._kline_dir / f"{stock_code}_meta.json"

    def get_kline(self, stock_code: str) -> Optional[pd.DataFrame]:
        """
        读取缓存的日 K 线数据。
        返回含 'date', 'open', 'high', 'low', 'close', 'volume', 'amount' 等列的 DataFrame。
        如无缓存返回 None。
        """
        fpath = self._kline_path(stock_code)
        if not fpath.exists():
            return None
        try:
            df = pd.read_pickle(fpath)
            if "date" in df.columns:
                df["date"] = df["date"].astype(str)
            logger.debug(f"[DataCache] K线命中: {stock_code} ({len(df)}行)")
            return df
        except Exception as e:
            logger.warning(f"[DataCache] K线读取失败 {stock_code}: {e}")
            # 损坏的缓存文件，删除后重建
            try:
                fpath.unlink(missing_ok=True)
            except Exception:
                pass
            return None

    def save_kline(self, stock_code: str, df: pd.DataFrame) -> None:
        """
        保存日 K 线数据到磁盘缓存。
        如果已有缓存，则按日期合并去重（增量追加）。
        """
        if df is None or df.empty:
            return

        with self._lock(f"kline_{stock_code}"):
            # 确保 date 列存在
            df_to_save = df.copy()
            if "date" not in df_to_save.columns:
                logger.warning(f"[DataCache] 保存K线时无date列: {stock_code}")
                df_to_save["date"] = ""

            fpath = self._kline_path(stock_code)
            existing = self.get_kline(stock_code)

            if existing is not None and not existing.empty:
                # 合并去重
                combined = pd.concat([existing, df_to_save], ignore_index=True)
                if "date" in combined.columns:
                    combined = combined.drop_duplicates(subset=["date"], keep="last")
                    combined = combined.sort_values("date").reset_index(drop=True)
                df_to_save = combined

            try:
                df_to_save.to_pickle(fpath)
                # 写元信息
                meta = {
                    "stock_code": stock_code,
                    "rows": len(df_to_save),
                    "date_range": [
                        str(df_to_save["date"].min()) if "date" in df_to_save.columns else "",
                        str(df_to_save["date"].max()) if "date" in df_to_save.columns else "",
                    ],
                    "updated_at": datetime.now().isoformat(),
                }
                self._kline_meta_path(stock_code).write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2)
                )
                logger.info(
                    f"[DataCache] K线已缓存: {stock_code} ({len(df_to_save)}行, "
                    f"{meta['date_range'][0]} ~ {meta['date_range'][1]})"
                )
            except Exception as e:
                logger.error(f"[DataCache] K线保存失败 {stock_code}: {e}")

    def has_kline(self, stock_code: str) -> bool:
        """检查是否有K线缓存。"""
        return self._kline_path(stock_code).exists()

    def kline_meta(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """获取K线缓存元信息。"""
        mpath = self._kline_meta_path(stock_code)
        if mpath.exists():
            try:
                return json.loads(mpath.read_text())
            except Exception:
                pass
        return None

    # ──────────── 全 A 股代码列表缓存 ────────────

    def get_stock_list(self) -> Optional[list]:
        """读取缓存的 A 股全代码列表。"""
        fpath = self._stock_list_dir / "all_a_codes.json"
        if not fpath.exists():
            return None
        try:
            data = json.loads(fpath.read_text())
            logger.debug(f"[DataCache] 股票列表命中: {len(data)}只")
            return data
        except Exception as e:
            logger.warning(f"[DataCache] 股票列表读取失败: {e}")
            return None

    def save_stock_list(self, stocks: list) -> None:
        """保存 A 股全代码列表。"""
        fpath = self._stock_list_dir / "all_a_codes.json"
        try:
            fpath.write_text(json.dumps(stocks, ensure_ascii=False))
            logger.info(f"[DataCache] 股票列表已缓存: {len(stocks)}只")
        except Exception as e:
            logger.error(f"[DataCache] 股票列表保存失败: {e}")

    # ──────────── 选股器快照缓存 ────────────

    def get_screener_snapshot(self, date_str: str) -> Optional[list]:
        """读取选股器某日快照。"""
        fpath = self._screener_dir / f"candidates_{date_str}.json"
        if not fpath.exists():
            return None
        try:
            return json.loads(fpath.read_text())
        except Exception:
            return None

    def save_screener_snapshot(self, date_str: str, data: list) -> None:
        """保存选股器快照。"""
        fpath = self._screener_dir / f"candidates_{date_str}.json"
        try:
            fpath.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
            logger.info(f"[DataCache] 选股快照已缓存: {date_str}")
        except Exception as e:
            logger.error(f"[DataCache] 选股快照保存失败: {e}")

    # ──────────── 缓存统计 ────────────

    def stats(self) -> Dict[str, Any]:
        """返回缓存统计信息。"""
        result = {"cache_root": str(self._root)}

        # K线缓存统计
        kline_files = list(self._kline_dir.glob("*.pkl"))
        kline_metas = list(self._kline_dir.glob("*_meta.json"))
        total_stocks = len(kline_files)
        total_size_mb = sum(f.stat().st_size for f in kline_files) / 1024 / 1024
        result["kline"] = {
            "stocks": total_stocks,
            "files": len(kline_files),
            "size_mb": round(total_size_mb, 1),
            "metas": len(kline_metas),
        }

        # 股票列表缓存
        sl_path = self._stock_list_dir / "all_a_codes.json"
        result["stock_list"] = {
            "exists": sl_path.exists(),
            "size_mb": round(sl_path.stat().st_size / 1024 / 1024, 2) if sl_path.exists() else 0,
        }

        # 选股快照
        snapshots = list(self._screener_dir.glob("candidates_*.json"))
        result["screener_snapshots"] = len(snapshots)

        # 基本面缓存
        fund_files = list(self._fund_dir.glob("*_fund.json"))
        result["fundamentals"] = len(fund_files)

        return result
