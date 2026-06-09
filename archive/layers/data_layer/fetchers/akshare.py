"""
AkshareFetcher — data_layer 适配层

包装 data_provider/akshare_fetcher.py 中的原始 AkshareFetcher，
将其返回类型转换为 data_layer 定义的模型（RealtimeQuote, ChipDistribution,
FundamentalContext 等）。

关键设计：
- **不修改 data_provider/ 目录任何文件**，保持向后兼容
- 继承 fetchers/base.py 的 BaseFetcher，复用其 get_kline / _clean_kline /
  _calculate_indicators 等标准化流程
- _fetch_raw_kline / _normalize_kline 委托给旧 AkshareFetcher 的已有逻辑
- get_realtime_quote / get_chip_distribution 做类型转换
- get_fundamental_context 桥接到 AkshareFundamentalAdapter
- get_market_overview 桥接到旧 AkshareFetcher 的 get_main_indices + get_market_stats
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ..definition import FetcherProtocol
from ..models import (
    ChipDistribution,
    FundamentalContext,
    KLineRequest,
    MarketOverview,
    RealtimeQuote,
    safe_float,
    safe_int,
)
from .base import BaseFetcher

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════
# 借用 data_provider 侧的辅助判断函数（不再导入全局 akshare，避免提前加载）
# ════════════════════════════════════════════════════════════════


def _is_hk_code(stock_code: str) -> bool:
    """判断代码是否为港股。"""
    code = stock_code.strip().lower()
    if code.endswith(".hk"):
        numeric_part = code[:-3]
        return numeric_part.isdigit() and 1 <= len(numeric_part) <= 5
    if code.startswith("hk"):
        numeric_part = code[2:]
        return numeric_part.isdigit() and 1 <= len(numeric_part) <= 5
    return code.isdigit() and len(code) == 5


def _is_us_code(stock_code: str) -> bool:
    """判断代码是否为美股。"""
    try:
        from data_provider.akshare_fetcher import _is_us_code as _old

        return _old(stock_code)
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════
# AkshareFetcher
# ════════════════════════════════════════════════════════════════


class AkshareFetcher(BaseFetcher):
    """Akshare 数据源适配器。

    封装 data_provider/akshare_fetcher.py 中旧 AkshareFetcher 的所有能力，
    将原始数据转换为 data_layer 的标准化模型。

    支持的接口：
    - K 线（日/周/月）：委托旧 fetcher，支持美股/港股/ETF/普通A股
    - 实时行情：委托旧 fetcher，支持多数据源自动降级
    - 筹码分布：委托旧 fetcher
    - 基本面：桥接到 AkshareFundamentalAdapter
    - 大盘概览：委托旧 fetcher 的 get_main_indices + get_market_stats
    """

    name: str = "AkshareFetcher"
    priority: int = 1  # 主数据源，优先级最高

    def __init__(self, sleep_min: float = 2.0, sleep_max: float = 5.0):
        # 延迟导入旧 AkshareFetcher，避免 import 时加载 akshare
        from data_provider.akshare_fetcher import AkshareFetcher as _OldFetcher

        self._inner = _OldFetcher(sleep_min=sleep_min, sleep_max=sleep_max)
        self._fund_adapter = None  # 延迟初始化

    def _get_fund_adapter(self):
        """延迟初始化基本面适配器。"""
        if self._fund_adapter is None:
            from data_provider.fundamental_adapter import AkshareFundamentalAdapter

            self._fund_adapter = AkshareFundamentalAdapter()
        return self._fund_adapter

    # ──────────── K 线：子类必须实现的抽象方法 ────────────

    def _fetch_raw_kline(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取原始 K 线数据（委托旧 fetcher）。

        注意一下传参：旧 AkshareFetcher._fetch_raw_data 仅支持日线，
        周线/月线通过 akshare 的 period 参数直接调用，我们在 _fetch_* 的
        直接 API 层处理。
        """
        return self._inner._fetch_raw_data(stock_code, start_date, end_date)

    def _normalize_kline(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """标准化 K 线列名（委托旧 fetcher）。"""
        return self._inner._normalize_data(df, stock_code)

    # ──────────── K 线入口（覆盖基类以支持 frequency）────────────

    def get_kline(self, req: KLineRequest) -> pd.DataFrame:
        """获取 K 线数据，支持多频率。

        日线：委托 BaseFetcher.get_kline 走 _fetch_raw_kline。
        周线/月线：直接调用 ak.stock_zh_a_hist(period=...) 或其他对应的 akshare API。
        """
        from datetime import datetime, timedelta

        end_date = req.end_date or datetime.now().strftime("%Y-%m-%d")
        if req.start_date:
            start_date = req.start_date
        else:
            start_dt = datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=req.days * 2)
            start_date = start_dt.strftime("%Y-%m-%d")

        if req.frequency == "daily":
            # 日线走基类的标准流程
            return super().get_kline(req)

        # 周线/月线：直接调用 akshare API，走自定义流程
        return self._fetch_frequency_kline(
            stock_code=req.code,
            start_date=start_date,
            end_date=end_date,
            frequency=req.frequency,
        )

    def _fetch_frequency_kline(self, stock_code: str, start_date: str, end_date: str, frequency: str) -> pd.DataFrame:
        """获取周线或月线 K 线数据。

        akshare 的 stock_zh_a_hist 支持 period="weekly" / "monthly"，
        但仅适用于 A 股。港股/美股/ETF 走相应的专用接口（日线 + 聚合）。

        当 API 直接请求失败时，降级为从日线数据聚合（resample）。
        确保在反爬或网络抖动时仍能返回数据。
        """
        import akshare as ak

        code = stock_code.upper()

        # 尝试直接通过 akshare API 获取指定周期数据
        try:
            result = self._try_fetch_period_kline(stock_code, start_date, end_date, frequency)
            if result is not None and not result.empty:
                df = self._normalize_kline(result, stock_code)
                df = self._clean_kline(df)
                df = self._calculate_indicators(df)
                return df
        except Exception as e:
            logger.warning(f"[Akshare] {frequency} 直接 API 失败: {e}，降级为日线聚合")

        # 降级：获取日线数据并聚合
        logger.info(f"[Akshare] {stock_code} {frequency} 降级: 从日线 resample")
        df = self._fetch_raw_kline(stock_code, start_date, end_date)
        if df is None or df.empty:
            return pd.DataFrame()
        df = self._normalize_kline(df, stock_code)
        return self._resample_kline(df, frequency)

    def _try_fetch_period_kline(
        self, stock_code: str, start_date: str, end_date: str, frequency: str
    ) -> Optional[pd.DataFrame]:
        """尝试通过 akshare 直接获取指定周期的 K 线数据。"""
        import akshare as ak

        period_map = {"weekly": "weekly", "monthly": "monthly"}
        period = period_map.get(frequency, "daily")

        # A 股
        if not (_is_us_code(stock_code) or _is_hk_code(stock_code)):
            _is_etf_local = False
            try:
                from data_provider.akshare_fetcher import _is_etf_code

                _is_etf_local = _is_etf_code(stock_code)
            except Exception:
                pass

            if _is_etf_local:
                logger.info(f"[API调用] ak.fund_etf_hist_em(symbol={stock_code}, " f"period={frequency}, ...)")
                self._inner._enforce_rate_limit()
                return ak.fund_etf_hist_em(
                    symbol=stock_code,
                    period=frequency,
                    start_date=start_date.replace("-", ""),
                    end_date=end_date.replace("-", ""),
                    adjust="qfq",
                )
            else:
                logger.info(f"[API调用] ak.stock_zh_a_hist(symbol={stock_code}, " f"period={period}, ...)")
                self._inner._enforce_rate_limit()
                return ak.stock_zh_a_hist(
                    symbol=stock_code,
                    period=period,
                    start_date=start_date.replace("-", ""),
                    end_date=end_date.replace("-", ""),
                    adjust="qfq",
                )

        # 美股：不支持周线/月线，返回 None 触发降级
        if _is_us_code(stock_code):
            return None

        # 港股：使用 stock_hk_hist（仅日线），返回 None 触发降级
        if _is_hk_code(stock_code):
            return None

        return None

    @staticmethod
    def _resample_kline(df: pd.DataFrame, frequency: str) -> pd.DataFrame:
        """将日线 K 线聚合为周线或月线。

        仅作为 akshare 不直接支持周期时的降级方案。
        """
        if df.empty:
            return df

        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])

        # 确定聚合标签
        rule = "W" if frequency == "weekly" else "M"
        label_col = df["date"].dt.strftime("%Y-%W" if frequency == "weekly" else "%Y-%m")
        df["_period"] = label_col

        agg = {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "amount": "sum",
        }
        # 只聚合存在的列
        agg = {k: v for k, v in agg.items() if k in df.columns}

        df_resampled = df.groupby("_period", sort=False).agg(agg).reset_index()
        # 用该周期最后一天作为日期
        last_dates = df.groupby("_period")["date"].last().reset_index()
        df_resampled["date"] = last_dates["date"]
        df_resampled = df_resampled.drop(columns=["_period"])
        df_resampled = df_resampled.sort_values("date").reset_index(drop=True)
        df_resampled["code"] = df["code"].iloc[0] if "code" in df.columns else ""

        # 计算涨跌幅
        if "close" in df_resampled.columns:
            df_resampled["pct_chg"] = (df_resampled["close"].pct_change() * 100).fillna(0)

        # 标准化日期格式
        df_resampled["date"] = df_resampled["date"].dt.strftime("%Y-%m-%d")

        return df_resampled

    # ──────────── 实时行情 ────────────

    def get_realtime_quote(self, code: str) -> Optional[RealtimeQuote]:
        """获取实时行情，将旧 fetcher 的 UnifiedRealtimeQuote 转换为 data_layer 的 RealtimeQuote。"""
        quote = self._inner.get_realtime_quote(code)
        if quote is None:
            return None

        return RealtimeQuote(
            code=quote.code,
            name=quote.name,
            source=getattr(quote.source, "value", str(quote.source)),
            price=safe_float(quote.price),
            change_pct=safe_float(quote.change_pct),
            change_amount=safe_float(quote.change_amount),
            volume=safe_int(quote.volume),
            amount=safe_float(quote.amount),
            volume_ratio=safe_float(quote.volume_ratio),
            turnover_rate=safe_float(quote.turnover_rate),
            amplitude=safe_float(quote.amplitude),
            open_price=safe_float(quote.open_price),
            high=safe_float(quote.high),
            low=safe_float(quote.low),
            pre_close=safe_float(quote.pre_close),
            pe_ratio=safe_float(quote.pe_ratio),
            pb_ratio=safe_float(quote.pb_ratio),
            total_mv=safe_float(quote.total_mv),
            circ_mv=safe_float(quote.circ_mv),
            change_60d=safe_float(quote.change_60d),
            high_52w=safe_float(quote.high_52w),
            low_52w=safe_float(quote.low_52w),
        )

    # ──────────── 筹码分布 ────────────

    def get_chip_distribution(self, code: str) -> Optional[ChipDistribution]:
        """获取筹码分布，将旧 ChipDistribution 转换为 data_layer 的 ChipDistribution。"""
        chip = self._inner.get_chip_distribution(code)
        if chip is None:
            return None

        return ChipDistribution(
            code=chip.code,
            date=chip.date,
            source=chip.source,
            profit_ratio=safe_float(chip.profit_ratio, 0.0),
            avg_cost=safe_float(chip.avg_cost, 0.0),
            cost_90_low=safe_float(chip.cost_90_low, 0.0),
            cost_90_high=safe_float(chip.cost_90_high, 0.0),
            concentration_90=safe_float(chip.concentration_90, 0.0),
            cost_70_low=safe_float(chip.cost_70_low, 0.0),
            cost_70_high=safe_float(chip.cost_70_high, 0.0),
            concentration_70=safe_float(chip.concentration_70, 0.0),
        )

    # ──────────── 基本面 ────────────

    def get_fundamental_context(self, code: str) -> FundamentalContext:
        """获取基本面上下文，桥接到 AkshareFundamentalAdapter。"""
        try:
            adapter = self._get_fund_adapter()
            bundle = adapter.get_fundamental_bundle(code)

            ctx = FundamentalContext(code=code, source_chain=["akshare"])

            # 从 bundle 中提取核心指标
            finances = bundle.get("finances", {})
            if finances:
                ctx.industry = _safe_str(finances.get("industry"))
                ctx.market_cap = safe_float(finances.get("market_cap") or finances.get("总市值"))
                ctx.pe_ttm = safe_float(finances.get("pe_ttm") or finances.get("pe"))
                ctx.pb = safe_float(finances.get("pb") or finances.get("pb_ratio"))
                ctx.roe = safe_float(finances.get("roe"))

            # 增长率
            if finances:
                ctx.revenue_growth = safe_float(finances.get("revenue_growth") or finances.get("营业总收入同比增长率"))
                ctx.profit_growth = safe_float(finances.get("profit_growth") or finances.get("净利润同比增长率"))

            # 板块归属
            belong_boards = bundle.get("belong_boards", [])
            if isinstance(belong_boards, list):
                ctx.belong_boards = belong_boards

            # 覆盖情况
            coverage = bundle.get("coverage", {})
            if isinstance(coverage, dict):
                ctx.coverage = coverage

            # 状态判断
            status = bundle.get("status", "not_supported")
            if status in ("ok", "partial"):
                ctx.status = status
            elif any(v is not None for v in [ctx.pe_ttm, ctx.pb, ctx.roe, ctx.market_cap]):
                ctx.status = "partial"
            else:
                ctx.status = status

            return ctx

        except Exception as e:
            logger.warning(f"[Akshare] 获取 {code} 基本面数据失败: {e}", exc_info=True)
            return FundamentalContext(code=code, status="failed")

    # ──────────── 大盘概览 ────────────

    def get_market_overview(self, region: str = "cn") -> MarketOverview:
        """获取大盘概览。"""
        overview = MarketOverview(region=region)

        try:
            # 指数行情
            indices = self._inner.get_main_indices(region=region)
            if indices:
                for idx in indices:
                    overview.indices.append(
                        {
                            "code": str(idx.get("code", "")),
                            "name": str(idx.get("name", "")),
                            "current": safe_float(idx.get("current")),
                            "change_pct": safe_float(idx.get("change_pct")),
                            "change": safe_float(idx.get("change")),
                            "open": safe_float(idx.get("open")),
                            "high": safe_float(idx.get("high")),
                            "low": safe_float(idx.get("low")),
                            "volume": safe_float(idx.get("volume")),
                            "amount": safe_float(idx.get("amount")),
                        }
                    )
        except Exception as e:
            logger.warning(f"[Akshare] 获取指数行情失败: {e}")

        try:
            # 市场涨跌统计
            stats = self._inner.get_market_stats()
            if stats:
                overview.up_count = safe_int(stats.get("up_count"), 0)
                overview.down_count = safe_int(stats.get("down_count"), 0)
                overview.flat_count = safe_int(stats.get("flat_count"), 0)
                overview.limit_up = safe_int(stats.get("limit_up_count"), 0)
                overview.limit_down = safe_int(stats.get("limit_down_count"), 0)
                overview.total_amount = safe_float(stats.get("total_amount"), 0.0)
        except Exception as e:
            logger.warning(f"[Akshare] 获取市场统计失败: {e}")

        try:
            # 行业板块排名
            rankings = self._inner.get_sector_rankings(n=5)
            if rankings:
                top, bottom = rankings
                if top:
                    overview.sector_rankings = top + bottom
        except Exception as e:
            logger.warning(f"[Akshare] 获取行业板块排名失败: {e}")

        return overview

    # ──────────── 批量预取 ────────────

    def prefetch_quotes(self, codes: List[str]) -> int:
        """批量预取实时行情。

        利用旧 fetcher 的全量缓存机制，预填充 _realtime_cache。
        """
        success = 0
        for code in codes:
            try:
                quote = self.get_realtime_quote(code)
                if quote is not None:
                    success += 1
            except Exception:
                continue
        logger.info(f"[Akshare] 批量预取行情: {success}/{len(codes)} 成功")
        return success

    # ──────────── 股票名称 ────────────

    def get_stock_name(self, code: str) -> str:
        """获取股票名称。

        优先从实时行情获取，失败时尝试从 akshare 的 stock_info_a_code_name 获取。
        """
        try:
            quote = self.get_realtime_quote(code)
            if quote and quote.name:
                return quote.name
        except Exception:
            pass

        try:
            import akshare as ak

            self._inner._enforce_rate_limit()
            df = ak.stock_info_a_code_name()
            if df is not None and not df.empty:
                row = df[df["code"] == code]
                if not row.empty:
                    return str(row.iloc[0].get("name", ""))
        except Exception as e:
            logger.debug(f"[Akshare] 获取 {code} 名称失败: {e}")

        return ""


# ════════════════════════════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════════════════════════════


def _safe_str(val: Any, default: str = "") -> str:
    if val is None:
        return default
    return str(val).strip()
