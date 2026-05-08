# -*- coding: utf-8 -*-
"""
A股市场情绪指数引擎 (Market Sentiment Index)

职责：
1. 每日收盘后计算全市场情绪指数 (0-100)
2. 分解情绪子维度：涨跌比情绪、北向资金情绪、主力资金情绪、涨停跌停比情绪
3. 生成情绪标签：极度恐慌(<20) / 恐慌(20-39) / 中性(40-59) / 贪婪(60-79) / 极度贪婪(≥80)
4. 提供情绪历史记录和趋势判断
"""

from .sentiment_index import (
    SentimentResult,
    SentimentLabel,
    SENTIMENT_COLORS,
    SENTIMENT_LABELS_ZH,
    SENTIMENT_LABELS_EN,
    SENTIMENT_ADJUSTMENT,
    compute_sentiment_index,
    get_sentiment_report,
    get_sentiment_adjustment,
    get_sentiment_history,
    print_sentiment_curve,
)
