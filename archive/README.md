# archive/ — 历史归档脚本

这些脚本经审计后判定为 **实验性/一次性的轮子**，不再用于日常流水线。

## 仍在内部但已过时的回测变体
（被 `scripts/backtest_hs300_v4_survivorship.py` 取代）
- `backtest_hs300.py` → v1 初始版本
- `backtest_hs300_v2_risk.py` → v2 自适应回撤降仓
- `backtest_hs300_v3_mafilter.py` → v3 均线过滤
- `backtest_100.py` → 100只小样本（被 full 取代）
- `backtest_csi500.py` → 中证500版本

## 辅助/探索类
- `alert.py` — 旧版告警（被 generate_signal_push.py 替代）
- `fill_backtest_data.py` — 历史数据填充
- `factor_ic_test.py` — 因子IC测试
- `migrate_cache_to_sqlite.py` — 缓存→SQLite迁移（一次性）
- `migrate_to_sqlite.py` — CSV→SQLite迁移（一次性）
- `monitor_candidates.py` — 候选股监控（实验性）
- `mx_enrich.py` — 妙想数据丰富（实验性）
- `intraday_trading.py` — 日内交易模型（搁置）
- `prepare_100_stocks.py` — 100只股票数据准备
- `pull_csi500_data.py` — CSI500数据拉取
- `sentiment_monitor.py` — 情绪监控（被 sentiment_engine 取代）

## 说明
- 这些脚本**不需要也建议不要**移回 `scripts/`
- 如需回测请使用 `scripts/run_backtest.py` 统一入口
