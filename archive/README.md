# Archive — 实验性脚本归档

本目录包含日常流水线中不再使用的实验性/一次性脚本，保留以备考量。

## 归档内容概览

### 回测系列（9个）
| 文件 | 说明 |
|------|------|
| `backtest_100.py` | 100只股票批量回测 |
| `backtest_compare.py` | 回测结果对比分析 |
| `backtest_csi500.py` | 中证500指数成分股回测 |
| `backtest_full.py` | 全市场回测（最完整版） |
| `backtest_hs300.py` | 沪深300成分股回测（v1基础版） |
| `backtest_hs300_v2_risk.py` | v2：加入风控模块 |
| `backtest_hs300_v3_mafilter.py` | v3：加入均线过滤 |
| `backtest_hs300_v4_survivorship.py` | v4：加入存活偏差修正 |
| `quick_backtest.py` | 快速回测原型 |

### 数据准备（3个）
| 文件 | 说明 |
|------|------|
| `prepare_100_stocks.py` | 候选池100只股票数据准备 |
| `pull_csi500_data.py` | 拉取中证500数据 |
| `fill_backtest_data.py` | 回测数据填充/补全 |

### 实验功能（5个）
| 文件 | 说明 |
|------|------|
| `intraday_trading.py` | 日内交易策略实验 |
| `sentiment_monitor.py` | 舆情盘中监控（已迁移至 sentiment_engine/） |
| `alert.py` | 早期预警模块 |
| `monitor_candidates.py` | 候选池实时监控 |
| `mx_enrich.py` | 妙想数据增强（已集成到主流程） |

### 一次性迁移工具（3个）
| 文件 | 说明 |
|------|------|
| `factor_ic_test.py` | 因子IC测试（一次性分析） |
| `migrate_cache_to_sqlite.py` | 缓存迁移到SQLite |
| `migrate_to_sqlite.py` | 全量数据迁移到SQLite |

## 保留在 scripts/ 的日常脚本
- `daily_import_sim.py` — 每日模拟交易导入
- `generate_signal_push.py` — 信号生成与推送
- `healthcheck.py` — 系统健康检查
- `migrate_cache.py` — 缓存迁移（维护用）
- `trading_calendar.py` — 交易日历

---

_归档日期：2026-06-09 | 方案B：代码流程升级_
