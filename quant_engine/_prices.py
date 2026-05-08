#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一价格数据提取 — 供 GARCH / HMM / 马科维茨共用"""

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

_REPORTS_DIR = Path("/opt/daily_stock_analysis/reports")


def extract_all_prices() -> Dict[str, List[Tuple[str, float]]]:
    """
    从所有历史报告提取全部股票的日收盘价序列。

    Returns:
        {"code": [("20260505", 4.82), ("20260506", 4.90), ...]}
    """
    prices: Dict[str, List[Tuple[str, float]]] = {}

    for report_file in sorted(_REPORTS_DIR.glob("report_*.md")):
        report_date = report_file.stem.replace("report_", "")
        content = report_file.read_text(encoding="utf-8")

        sections = re.split(r'\n## ', content)
        for sec in sections:
            code_match = re.search(r'\((\d{6})\)', sec.split('\n')[0])
            if not code_match:
                continue
            code = code_match.group(1)

            price = _find_current_price(sec)
            if price is not None:
                prices.setdefault(code, []).append((report_date, price))

    # 按日期排序
    for code in prices:
        prices[code].sort(key=lambda x: x[0])

    return prices


def extract_price_matrix() -> Tuple[np.ndarray, List[str], List[str]]:
    """
    提取对齐的价格矩阵。

    Returns:
        (price_matrix, stock_codes, date_labels)
        price_matrix: (n_dates, n_stocks) float64
    """
    all_prices = extract_all_prices()
    if not all_prices:
        return np.array([]), [], []

    # 收集所有日期
    all_dates: set = set()
    for code, series in all_prices.items():
        for d, _ in series:
            all_dates.add(d)
    sorted_dates = sorted(all_dates)
    codes = sorted(all_prices.keys())

    matrix: List[List[float]] = []
    valid_codes: List[str] = []
    for code in codes:
        lookup = dict(all_prices[code])
        series = [lookup.get(d) for d in sorted_dates]
        if None not in series:
            matrix.append(series)
            valid_codes.append(code)

    if not matrix:
        return np.array([]), [], sorted_dates

    return np.array(matrix, dtype=np.float64).T, valid_codes, sorted_dates


def stock_returns(stock_code: str) -> np.ndarray:
    """返回单只股票的对数收益率序列。"""
    prices = extract_all_prices()
    series = prices.get(stock_code)
    if not series or len(series) < 2:
        return np.array([])
    vals = np.array([p[1] for p in series])
    return np.diff(np.log(vals))


def aggregated_returns() -> np.ndarray:
    """所有股票日收益率取均值。"""
    prices = extract_all_prices()
    if not prices:
        return np.array([])
    all_ret: List[np.ndarray] = []
    for code, series in prices.items():
        vals = np.array([p[1] for p in series])
        if len(vals) >= 2:
            all_ret.append(np.diff(np.log(vals)))
    if not all_ret:
        return np.array([])
    # 截取到最小长度
    min_len = min(len(r) for r in all_ret)
    if min_len < 2:
        return np.array([])
    clipped = np.array([r[-min_len:] for r in all_ret])
    return np.mean(clipped, axis=0)


def _find_current_price(section_text: str) -> Optional[float]:
    """从报告的一个股票段落中提取当前价。"""
    lines = section_text.split('\n')

    # 方法一：从 "当前价" 标记行后第二行提取数字
    for i, line in enumerate(lines):
        if '当前价' in line and i + 2 < len(lines):
            nums = re.findall(r'[\d.]+', lines[i + 2])
            for n in nums:
                try:
                    v = float(n)
                    if 0.5 < v < 10000:
                        return v
                except ValueError:
                    pass
            break

    # 方法二：从表格行 "| 当前价 | X.XX |"
    for line in lines:
        if '当前价' in line:
            cells = [c.strip() for c in line.split('|') if c.strip()]
            for cell in cells:
                m = re.search(r'[\d.]+', cell.replace(',', ''))
                if m:
                    try:
                        v = float(m.group())
                        if 0.5 < v < 10000:
                            return v
                    except ValueError:
                        pass

    # 方法三：从 "收盘" 表格行取第一列
    for i, line in enumerate(lines):
        if ('| 收盘 |' in line or line.strip().startswith('| 收盘 ')) and i + 1 < len(lines):
            cells = [c.strip() for c in lines[i + 1].split('|') if c.strip()]
            if cells:
                try:
                    v = float(cells[0].replace(',', ''))
                    if 0.5 < v < 10000:
                        return v
                except ValueError:
                    pass
            break

    return None


if __name__ == "__main__":
    print("=== 价格提取测试 ===")
    p = extract_all_prices()
    for code, series in sorted(p.items()):
        print(f"  {code}: {[f'{x[0]}={x[1]}' for x in series]}")

    mat, codes, dates = extract_price_matrix()
    if mat.size > 0:
        print(f"\n矩阵: {mat.shape} = {len(dates)}天 × {len(codes)}只")
        print(f"  日期: {dates}")
        print(f"  代码: {codes}")
        print(mat)
