#!/usr/bin/env python3
"""
A股交易日历工具 — 判断当天是否是交易日

用法：
  from trading_calendar import is_trading_day
  if is_trading_day():
      ...
"""

from datetime import datetime

# 2026年 A股法定节假日（非交易日）
# 数据来源：国务院办公厅发布的2026年放假安排
HOLIDAYS_2026 = {
    # 元旦
    "20260101", "20260102", "20260103",
    # 春节
    "20260217", "20260218", "20260219", "20260220", "20260221", "20260222", "20260223",
    # 清明节
    "20260404", "20260405", "20260406",
    # 劳动节
    "20260501", "20260502", "20260503", "20260504", "20260505",
    # 端午节
    "20260619", "20260620", "20260621",
    # 中秋节+国庆节
    "20261001", "20261002", "20261003", "20261004", "20261005", "20261006", "20261007", "20261008",
}

# 调休工作日（周末补班，是交易日）
WORKDAYS_2026 = {
    # 春节前
    "20260215", "20260216",
    # 其他调休日可根据国务院通知补充
}


def is_trading_day(dt=None) -> bool:
    """
    判断指定日期是否为A股交易日。
    
    规则：
    1. 周末（周六日）不是交易日
    2. 法定节假日不是交易日
    3. 调休补班日是交易日
    
    Args:
        dt: datetime对象，默认为当前时间
    
    Returns:
        bool: 是否为交易日
    """
    if dt is None:
        dt = datetime.now()

    # 周末
    if dt.weekday() >= 5:
        return dt.strftime("%Y%m%d") in WORKDAYS_2026

    date_str = dt.strftime("%Y%m%d")

    # 法定节假日
    if date_str in HOLIDAYS_2026:
        return False

    # 调休补班
    if date_str in WORKDAYS_2026:
        return True

    return True


def is_market_open(dt=None) -> bool:
    """
    判断当前是否在A股交易时段内（开盘时间）。
    交易日 09:30-11:30 + 13:00-15:00 为交易时段。
    """
    if dt is None:
        dt = datetime.now()

    if not is_trading_day(dt):
        return False

    t = dt.strftime("%H%M")
    # 上午：09:30-11:30 (0930-1130)
    if "0930" <= t <= "1130":
        return True
    # 下午：13:00-15:00 (1300-1500)
    if "1300" <= t <= "1500":
        return True
    return False


def eastmoney_secid(code: str) -> str:
    """
    将股票代码转换为东方财富 secid 格式。
    
    规则：
    - 沪市股票（6开头）→ 1.{code}
    - 沪市ETF（5开头，如510300）→ 1.{code}
    - 深市股票（0/3开头）→ 0.{code}
    
    Args:
        code: 6位股票代码，如 "510300"
    
    Returns:
        str: secid，如 "1.510300"
    """
    code = str(code).strip()
    # 上交所: 6xxxxx(股票)/5xxxxx(ETF)/000xxx(指数)
    # 深交所: 0xxxxx/3xxxxx(股票)/001xxx/002xxx/003xxx/399xxx(指数)
    if code.startswith("6") or code.startswith("5") or code.startswith("000"):
        return f"1.{code}"
    return f"0.{code}"


if __name__ == "__main__":
    now = datetime.now()
    print(f"当前时间: {now:%Y-%m-%d %H:%M %A}")
    print(f"交易日: {'是' if is_trading_day() else '否'}")
    print(f"交易时段: {'是' if is_market_open() else '否'}")
