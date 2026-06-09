"""
缓存模块 — 数据持久化磁盘缓存层（源自 data_provider/data_cache.py）

为 API 获取的基础数据提供磁盘级缓存，避免重复拉取。
每日 K 线等历史数据是不可变的，缓存后永久可用。

缓存结构：
  data/cache/
    kline/
      {stock_code}.pkl            # 该股全量日 K 线（合并增量追加）
      {stock_code}_meta.json      # 最后更新日期等元信息
    stock_list/
      all_a_codes.json            # 全 A 股代码列表快照
    fundamentals/
      {stock_code}_fund.json      # 基本面数据快照
    screener/
      candidates_{date}.json      # 选股器快照
"""

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from .definition import CacheProtocol

logger = logging.getLogger(__name__)

# 缓存根目录（可通过环境变量覆盖）
_DEFAULT_CACHE_ROOT = Path("/opt/daily_stock_analysis/data/cache")


class DataCache(CacheProtocol):
    """磁盘缓存管理器。

    线程安全，支持逐股票追加式缓存 K 线数据。
    实现 CacheProtocol 接口。
    """

    def __init__(self, cache_root: Optional[Path] = None):
        self._root = Path(cache_root) if cache_root else _DEFAULT_CACHE_ROOT
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

    def _kline_path(self, stock_code: str, frequency: str = "daily") -> Path:
        """获取 K 线缓存文件路径。

        Args:
            stock_code: 股票代码
            frequency: 频率 daily/weekly/monthly

        保持向后兼容：日线使用 {code}.pkl，其他频率使用 {code}_{freq}.pkl。
        """
        if frequency == "daily":
            return self._kline_dir / f"{stock_code}.pkl"
        return self._kline_dir / f"{stock_code}_{frequency}.pkl"

    def _kline_meta_path(self, stock_code: str, frequency: str = "daily") -> Path:
        if frequency == "daily":
            return self._kline_dir / f"{stock_code}_meta.json"
        return self._kline_dir / f"{stock_code}_{frequency}_meta.json"

    def get_kline(self, code: str, frequency: str = "daily") -> Optional[pd.DataFrame]:
        """读取缓存的 K 线数据。

        Args:
            code: 股票代码
            frequency: 频率 daily/weekly/monthly

        Returns:
            缓存数据的 DataFrame，无缓存时返回 None
        """
        fpath = self._kline_path(code, frequency)
        if not fpath.exists():
            return None
        try:
            df = pd.read_pickle(fpath)
            if "date" in df.columns:
                df["date"] = df["date"].astype(str)
            logger.debug(f"[DataCache] K线命中: {code} (freq={frequency}, {len(df)}行)")
            return df
        except Exception as e:
            logger.warning(f"[DataCache] K线读取失败 {code}: {e}")
            try:
                fpath.unlink(missing_ok=True)
            except Exception:
                pass
            return None

    def save_kline(self, code: str, df: pd.DataFrame, frequency: str = "daily") -> None:
        """保存 K 线数据到磁盘缓存（增量追加 + 日期去重）。"""
        if df is None or df.empty:
            return

        with self._lock(f"kline_{code}_{frequency}"):
            df_to_save = df.copy()
            if "date" not in df_to_save.columns:
                logger.warning(f"[DataCache] 保存K线时无date列: {code}")
                df_to_save["date"] = ""

            existing = self.get_kline(code, frequency)
            if existing is not None and not existing.empty:
                combined = pd.concat([existing, df_to_save], ignore_index=True)
                if "date" in combined.columns:
                    combined = combined.drop_duplicates(subset=["date"], keep="last")
                    combined = combined.sort_values("date").reset_index(drop=True)
                df_to_save = combined

            try:
                df_to_save.to_pickle(self._kline_path(code, frequency))
                meta = {
                    "stock_code": code,
                    "frequency": frequency,
                    "rows": len(df_to_save),
                    "date_range": [
                        str(df_to_save["date"].min()) if "date" in df_to_save.columns else "",
                        str(df_to_save["date"].max()) if "date" in df_to_save.columns else "",
                    ],
                    "updated_at": datetime.now().isoformat(),
                }
                self._kline_meta_path(code, frequency).write_text(json.dumps(meta, ensure_ascii=False, indent=2))
                logger.info(
                    f"[DataCache] K线已缓存: {code} (freq={frequency}, {len(df_to_save)}行, "
                    f"{meta['date_range'][0]} ~ {meta['date_range'][1]})"
                )
            except Exception as e:
                logger.error(f"[DataCache] K线保存失败 {code}: {e}")

    def has_kline(self, code: str, frequency: str = "daily") -> bool:
        return self._kline_path(code, frequency).exists()

    def invalidate(self, code: str, frequency: str = "daily") -> None:
        """使指定股票的缓存失效。

        Args:
            code: 股票代码
            frequency: 频率，为 "all" 时清除所有频率的缓存
        """
        if frequency == "all":
            # 清除该股所有频率的缓存
            for p in self._kline_dir.glob(f"{code}_*.pkl"):
                p.unlink(missing_ok=True)
            for p in self._kline_dir.glob(f"{code}_*_meta.json"):
                p.unlink(missing_ok=True)
            # 也清除不带频率后缀的（日线）
            daily_p = self._kline_path(code, "daily")
            if daily_p.exists():
                daily_p.unlink()
            daily_m = self._kline_meta_path(code, "daily")
            if daily_m.exists():
                daily_m.unlink()
            return

        p = self._kline_path(code, frequency)
        if p.exists():
            p.unlink()
        mp = self._kline_meta_path(code, frequency)
        if mp.exists():
            mp.unlink()

    def kline_meta(self, code: str, frequency: str = "daily") -> Optional[Dict[str, Any]]:
        mpath = self._kline_meta_path(code, frequency)
        if mpath.exists():
            try:
                return json.loads(mpath.read_text())
            except Exception:
                pass
        return None

    # ──────────── 股票列表缓存 ────────────

    def get_stock_list(self) -> Optional[List[dict]]:
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

    def save_stock_list(self, stocks: List[dict]) -> None:
        fpath = self._stock_list_dir / "all_a_codes.json"
        try:
            fpath.write_text(json.dumps(stocks, ensure_ascii=False))
            logger.info(f"[DataCache] 股票列表已缓存: {len(stocks)}只")
        except Exception as e:
            logger.error(f"[DataCache] 股票列表保存失败: {e}")

    # ──────────── 选股器快照 ────────────

    def get_screener_snapshot(self, date_str: str) -> Optional[List[dict]]:
        fpath = self._screener_dir / f"candidates_{date_str}.json"
        if not fpath.exists():
            return None
        try:
            return json.loads(fpath.read_text())
        except Exception:
            return None

    def save_screener_snapshot(self, date_str: str, data: list) -> None:
        fpath = self._screener_dir / f"candidates_{date_str}.json"
        try:
            fpath.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
            logger.info(f"[DataCache] 选股快照已缓存: {date_str}")
        except Exception as e:
            logger.error(f"[DataCache] 选股快照保存失败: {e}")

    # ──────────── 缓存统计 ────────────

    def stats(self) -> Dict[str, object]:
        result: Dict[str, object] = {"cache_root": str(self._root)}

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

        sl_path = self._stock_list_dir / "all_a_codes.json"
        result["stock_list"] = {
            "exists": sl_path.exists(),
            "size_mb": round(sl_path.stat().st_size / 1024 / 1024, 2) if sl_path.exists() else 0,
        }

        snapshots = list(self._screener_dir.glob("candidates_*.json"))
        result["screener_snapshots"] = len(snapshots)

        fund_files = list(self._fund_dir.glob("*_fund.json"))
        result["fundamentals"] = len(fund_files)

        return result
