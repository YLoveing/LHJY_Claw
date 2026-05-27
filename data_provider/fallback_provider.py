"""
备用数据源模块 (Fallback Data Provider)
======================================
东方财富 push2 挂了之后自动降级：
  东方财富 → mootdx (通达信) → 腾讯财经 → 抛异常

安装: pip install mootdx

Author: 打工马 🐴 + 打工虾 🦐
"""

import logging
import urllib.request
import urllib.error
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

import pandas as pd

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 腾讯财经 API
# ──────────────────────────────────────────────

# 腾讯字段映射 (按位置索引)
# 格式: v_{market}{code}="1~名称~代码~现价~昨收~今开~成交量(手)~外盘~内盘~..."
TENCENT_FIELDS = [
    ('market', 0),     # 0: 市场
    ('name', 1),       # 1: 股票名称
    ('code', 2),       # 2: 股票代码
    ('price', 3),      # 3: 当前价格
    ('last_close', 4), # 4: 昨收
    ('open', 5),       # 5: 今开
    ('volume', 6),     # 6: 成交量(手)
    ('outer_disc', 7), # 7: 外盘
    ('inner_disc', 8), # 8: 内盘
    # 9-29: 买卖五档
    ('datetime', 30),  # 30: 时间戳(YYYYMMDDHHMMSS)
    ('change', 31),    # 31: 涨跌额
    ('change_pct', 32),# 32: 涨跌幅(%)
    ('high', 33),      # 33: 最高
    ('low', 34),       # 34: 最低
    ('price_ratio', 37),# 37: 价格位置(百分比)
    ('turnover_rate', 38),# 38: 换手率(%)
    ('pe', 39),        # 39: 市盈率
    ('amplitude', 43), # 43: 振幅(%)
    ('circulation', 44),# 44: 流通市值
    ('total_mv', 45),  # 45: 总市值
    ('pb', 46),        # 46: 市净率
    ('amount', 49),    # 49: 成交额
]

_TENCENT_CODE_MAP = {
    '6': 'sh',   # 6开头 → 上海
    '0': 'sz',   # 0开头 → 深圳
    '3': 'sz',   # 3开头 → 深圳创业板
    '5': 'sh',   # 5开头 → 上海
    '9': 'sh',   # 9开头 → 上海B股
    '2': 'sz',   # 2开头 → 深圳B股
    '4': 'sz',   # 4开头 → 深圳(三板等)
}


def _code_to_tencent(code: str) -> str:
    """A股代码转腾讯格式: sh600519 / sz000001"""
    code = str(code).strip().zfill(6)
    prefix = code[0]
    market = _TENCENT_CODE_MAP.get(prefix, 'sh')
    return f"{market}{code}"


def _parse_tencent_response(text: str) -> Optional[Dict[str, Any]]:
    """解析腾讯返回的文本格式行情"""
    try:
        match = re.search(r'v_[a-z]{2}\d{6}="(.+)"', text)
        if not match:
            return None
        parts = match.group(1).split('~')
        result = {}
        for name, idx in TENCENT_FIELDS:
            val = parts[idx] if idx < len(parts) else ''
            result[name] = val

        # 类型转换
        for num_field in ['price', 'last_close', 'open', 'high', 'low',
                          'change', 'change_pct', 'turnover_rate', 'pe', 'pb',
                          'amplitude', 'price_ratio']:
            try:
                result[num_field] = float(parts[TENCENT_FIELDS_MAP[num_field]]) \
                    if num_field in TENCENT_FIELDS_MAP else None
            except (ValueError, IndexError):
                result[num_field] = None

        result['volume'] = int(float(parts[6])) if parts[6] else 0
        result['amount'] = float(parts[49]) if len(parts) > 49 and parts[49] else 0.0

        # 解析时间
        try:
            ts = parts[30]
            if len(ts) >= 14:
                result['time'] = datetime(
                    int(ts[:4]), int(ts[4:6]), int(ts[6:8]),
                    int(ts[8:10]), int(ts[10:12]), int(ts[12:14])
                )
        except (ValueError, IndexError):
            result['time'] = None

        return result
    except Exception as e:
        logger.warning(f"解析腾讯行情失败: {e}")
        return None


# 构建查找表
TENCENT_FIELDS_MAP = {name: idx for name, idx in TENCENT_FIELDS}


# ──────────────────────────────────────────────
# Fallback Provider
# ──────────────────────────────────────────────

class FallbackProvider:
    """自动降级数据源，东方财富 -> mootdx -> 腾讯财经"""

    def __init__(self):
        self._mootdx_client = None
        self._tencent_available = True

    def _get_mootdx_client(self):
        """延迟初始化 mootdx"""
        if self._mootdx_client is None:
            try:
                from mootdx.quotes import Quotes
                self._mootdx_client = Quotes.factory(market='std')
                logger.info("mootdx 客户端初始化成功")
            except ImportError:
                logger.warning("mootdx 未安装，该数据源不可用")
                self._mootdx_client = False  # 标记不可用
            except Exception as e:
                logger.warning(f"mootdx 客户端初始化失败: {e}")
                self._mootdx_client = False
        return self._mootdx_client if self._mootdx_client is not False else None

    # ── 实时行情 ──

    def get_realtime_quote(self, code: str) -> Optional[Dict[str, Any]]:
        """
        获取单只股票实时行情 (自动降级)
        返回: dict (统一格式) 或 None
        """
        result = self._try_mootdx_quote(code)
        if result:
            return result

        result = self._try_tencent_quote(code)
        if result:
            return result

        logger.error(f"所有数据源均无法获取 {code} 的实时行情")
        return None

    def _try_mootdx_quote(self, code: str) -> Optional[Dict[str, Any]]:
        """mootdx 获取实时行情"""
        client = self._get_mootdx_client()
        if not client:
            return None
        try:
            code_str = str(code).zfill(6)
            rt = client.quotes(symbol=[code_str])
            if rt is not None and not rt.empty and len(rt) > 0:
                row = rt.iloc[0]
                return {
                    'code': code_str,
                    'name': '',
                    'price': float(row.get('price', 0)),
                    'last_close': float(row.get('last_close', 0)),
                    'open': float(row.get('open', 0)),
                    'high': float(row.get('high', 0)),
                    'low': float(row.get('low', 0)),
                    'volume': int(row.get('active1', 0)),
                    'source': 'mootdx',
                }
        except Exception as e:
            logger.debug(f"mootdx 实时行情失败 ({code}): {e}")
        return None

    def _try_tencent_quote(self, code: str) -> Optional[Dict[str, Any]]:
        """腾讯财经获取实时行情"""
        if not self._tencent_available:
            return None
        try:
            tcode = _code_to_tencent(code)
            url = f"https://qt.gtimg.cn/q={tcode}"
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode('gbk')

            parsed = _parse_tencent_response(raw)
            if parsed:
                parsed['source'] = 'tencent'
                # 统一字段名
                return {
                    'code': code,
                    'name': parsed.get('name', ''),
                    'price': parsed.get('price', 0),
                    'last_close': parsed.get('last_close', 0),
                    'open': parsed.get('open', 0),
                    'high': parsed.get('high', 0),
                    'low': parsed.get('low', 0),
                    'volume': parsed.get('volume', 0),
                    'amount': parsed.get('amount', 0),
                    'change': parsed.get('change', 0),
                    'change_pct': parsed.get('change_pct', 0),
                    'turnover_rate': parsed.get('turnover_rate', 0),
                    'pe': parsed.get('pe', 0),
                    'pb': parsed.get('pb', 0),
                    'amplitude': parsed.get('amplitude', 0),
                    'total_mv': parsed.get('total_mv', 0),
                    'circulation': parsed.get('circulation', 0),
                    'time': parsed.get('time'),
                    'source': 'tencent',
                }
        except urllib.error.HTTPError as e:
            if e.code == 403:
                self._tencent_available = False
                logger.warning("腾讯财经 API 被屏蔽，暂停使用")
            else:
                logger.debug(f"腾讯财经 HTTP {e.code} ({code}): {e}")
        except Exception as e:
            logger.debug(f"腾讯财经行情失败 ({code}): {e}")
        return None

    # ── K线 ──

    def get_kline_data(self, code: str, days: int = 60) -> Optional[pd.DataFrame]:
        """
        获取日K线数据 (自动降级)
        返回: DataFrame 含 open/close/high/low/volume/amount 或 None
        """
        result = self._try_mootdx_kline(code, days)
        if result is not None:
            return result

        result = self._try_tencent_kline(code, days)
        if result is not None:
            return result

        logger.error(f"所有数据源均无法获取 {code} 的 K线数据")
        return None

    def _try_mootdx_kline(self, code: str, days: int = 60) -> Optional[pd.DataFrame]:
        """mootdx 获取日K线"""
        client = self._get_mootdx_client()
        if not client:
            return None
        try:
            code_str = str(code).zfill(6)
            df = client.bars(symbol=code_str, frequency=9, start=0, offset=days + 10)
            if df is not None and not df.empty:
                df = df.copy()
                # 构建日期列
                df['date'] = pd.to_datetime(
                    df['year'].astype(str) + '-' +
                    df['month'].astype(str).str.zfill(2) + '-' +
                    df['day'].astype(str).str.zfill(2),
                    errors='coerce'
                )
                df.drop(columns=['year', 'month', 'day', 'hour'], inplace=True, errors='ignore')
                df.sort_values('date', inplace=True)
                df.reset_index(drop=True, inplace=True)

                # 只保留需要的列并重命名
                result = pd.DataFrame({
                    'date': df['date'],
                    'open': df['open'].astype(float),
                    'high': df['high'].astype(float),
                    'low': df['low'].astype(float),
                    'close': df['close'].astype(float),
                    'volume': df['vol'].astype(float),
                    'amount': df['amount'].astype(float),
                })
                logger.info(f"mootdx K线 {code}: {len(result)} rows")
                return result.tail(days)
        except Exception as e:
            logger.debug(f"mootdx K线失败 ({code}): {e}")
        return None

    def _try_tencent_kline(self, code: str, days: int = 60) -> Optional[pd.DataFrame]:
        """
        腾讯财经获取日K线
        接口: https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days},qfq
        """
        if not self._tencent_available:
            return None
        try:
            tcode = _code_to_tencent(code)
            # 腾讯日K线API
            url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tcode},day,,,{days},qfq"
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0'
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode('utf-8')

            import json
            data = json.loads(raw)

            # 解析 K线 数据
            # 结构: {data: {code: {day: [[date,open,close,high,low,volume], ...], qfqday: ...}}}
            code_key = tcode
            kline_data = None
            try:
                kline_data = data.get('data', {}).get(code_key, {}).get('day', None)
                if not kline_data:
                    kline_data = data.get('data', {}).get(code_key, {}).get('qfqday', None)
            except Exception:
                pass

            if kline_data and len(kline_data) > 0:
                rows = []
                for k in kline_data:
                    if len(k) >= 6:
                        rows.append({
                            'date': k[0],
                            'open': float(k[1]),
                            'close': float(k[2]),
                            'high': float(k[3]),
                            'low': float(k[4]),
                            'volume': float(k[5]),
                        })
                if rows:
                    df = pd.DataFrame(rows)
                    df['date'] = pd.to_datetime(df['date'], errors='coerce')
                    df.sort_values('date', inplace=True)
                    df['amount'] = 0.0  # 腾讯K线不直接提供成交额
                    logger.info(f"腾讯 K线 {code}: {len(df)} rows")
                    return df.tail(days)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                self._tencent_available = False
                logger.warning("腾讯财经 API 被屏蔽，暂停使用")
            else:
                logger.debug(f"腾讯财经 K线 HTTP {e.code} ({code}): {e}")
        except json.JSONDecodeError:
            logger.debug(f"腾讯 K线 JSON解析失败 ({code})")
        except Exception as e:
            logger.debug(f"腾讯 K线失败 ({code}): {e}")
        return None

    # ── 健康检查 ──

    def check_health(self) -> Dict[str, bool]:
        """检查各数据源连通性"""
        status = {'mootdx': False, 'tencent': False}

        # 检查 mootdx
        client = self._get_mootdx_client()
        if client:
            try:
                rt = client.quotes(symbol=['600519'])
                if rt is not None and not rt.empty:
                    status['mootdx'] = True
            except Exception:
                pass

        # 检查腾讯
        if self._tencent_available:
            try:
                url = "https://qt.gtimg.cn/q=sh600519"
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    raw = resp.read().decode('gbk')
                    if '600519' in raw:
                        status['tencent'] = True
                self._tencent_available = True
            except Exception:
                status['tencent'] = False

        return status

    # ── 同花顺热点 ──

    def get_hot_concepts(self, top: int = 20) -> Optional[pd.DataFrame]:
        """
        获取同花顺热门概念板块排行
        top: 返回前N条 (默认20)
        返回: DataFrame [rank, concept_code, concept_name, change_pct, hot_value, hot_tag]
        """
        try:
            from adata.sentiment.hot import Hot
            h = Hot()
            df = h.hot_concept_20_ths()
            if df is not None and not df.empty:
                df = df.head(top)
                logger.info(f"同花顺热点概念: {len(df)} 条")
                return df
        except ImportError:
            logger.warning("adata 未安装，同花顺热点不可用 (pip install adata)")
        except Exception as e:
            logger.warning(f"同花顺热点概念失败: {e}")
        return None

    def get_hot_stocks(self, top: int = 50) -> Optional[pd.DataFrame]:
        """
        获取同花顺热度100排行榜
        top: 返回前N条 (默认50)
        返回: DataFrame [rank, stock_code, short_name, change_pct, hot_value, pop_tag, concept_tag]
        """
        try:
            from adata.sentiment.hot import Hot
            h = Hot()
            df = h.hot_rank_100_ths()
            if df is not None and not df.empty:
                df = df.head(top)
                logger.info(f"同花顺热度排行: {len(df)} 条")
                return df
        except ImportError:
            logger.warning("adata 未安装，同花顺热度不可用")
        except Exception as e:
            logger.warning(f"同花顺热度排行失败: {e}")
        return None

    def get_all_hot_data(self) -> Dict[str, Any]:
        """获取完整的同花顺热点数据（概念+个股）"""
        return {
            'concepts': self.get_hot_concepts(),
            'stocks': self.get_hot_stocks(50),
            'update_time': datetime.now().strftime('%Y-%m-%d %H:%M'),
        }


# ── 单例 ──
_fallback = None


def get_fallback_provider() -> FallbackProvider:
    global _fallback
    if _fallback is None:
        _fallback = FallbackProvider()
    return _fallback


# ── 便捷函数 ──

def get_quote(code: str) -> Optional[Dict[str, Any]]:
    """便捷获取实时行情"""
    return get_fallback_provider().get_realtime_quote(code)


def get_kline(code: str, days: int = 60) -> Optional[pd.DataFrame]:
    """便捷获取K线"""
    return get_fallback_provider().get_kline_data(code, days)


def get_hot_concepts(top: int = 20) -> Optional[pd.DataFrame]:
    """便捷获取同花顺热门概念"""
    return get_fallback_provider().get_hot_concepts(top)


def get_hot_stocks(top: int = 50) -> Optional[pd.DataFrame]:
    """便捷获取同花顺热度排行"""
    return get_fallback_provider().get_hot_stocks(top)


def get_all_hot() -> Dict[str, Any]:
    """便捷获取完整热点数据"""
    return get_fallback_provider().get_all_hot_data()


def check_sources() -> Dict[str, bool]:
    """检查各数据源状态"""
    return get_fallback_provider().check_health()
