# 代码审计报告：生产 vs 非生产

> 生成时间: 2026-06-09 14:37 CST
> 审计范围: `/opt/daily_stock_analysis/`
> 入口: `run_and_send.sh` 调用链

---

## 🚨 严重问题：3个脚本缺失！

`run_and_send.sh` 引用了3个脚本文件，但它们**不在 `scripts/` 目录中**（仅存在于 `archive/`）：

| 引用位置 | 期望路径 | 实际存在 |
|----------|----------|----------|
| `run_and_send.sh:65` | `scripts/alert.py` | ❌ 不存在；archive/ 有副本 |
| `run_and_send.sh:71` | `scripts/mx_enrich.py` | ❌ 不存在；archive/ 有副本 |
| `run_and_send.sh:61` | `scripts/intraday_trading.py` | ❌ 不存在；archive/ 有副本 |

**影响**: 当 cron 在特定时段触发这些步骤时会直接失败。`alert.py` 是错误处理的关键组件。

---

## 一、生产调用链

### entry-points（来自 run_and_send.sh）

```
run_and_send.sh
├── run_screener.py                        # 09:00/18:00 选股
├── scripts/intraday_trading.py execute    # 🚨 09:00 信号执行（缺失！）
├── scripts/mx_enrich.py --codes=          # 🚨 09:00/11:30 妙想预取（缺失！）
├── main.py --force-run                    # 每时段分析
├── sentiment_engine/vol_timing            # 早盘+盘后 五因子择时
├── simulated_trading.py                   # 早盘+盘后 模拟交易
├── scripts/daily_import_sim.py            # 模拟交易数据入库SQLite
├── quant_engine/run_quant                 # 18:00 GARCH+HMM+马科维茨
├── sentiment_engine/sentiment_index       # 18:00 情绪指数
├── scripts/alert.py                       # 🚨 步骤失败推送（缺失！）
├── scripts/generate_signal_push.py        # 信号推送
├── scripts/run_backtest.py --scenario=real # 周五回测
└── inline python3 -c ...                  # verify_signal_accuracy / vol报告
```

### 入口 → 依赖完整展开

#### 1. run_screener.py
→ `screener/stock_screener.py` → `data_provider/data_cache.py`
→ `strategies/engine.py`
→ `data_provider/mx_fetcher.py`

#### 2. main.py
→ `src/config.py` (setup_env, get_config, Config)
→ `src/logging_config.py`
→ `src/webui_frontend.py` (prepare assets)
→ `src/core/pipeline.py` → StockAnalysisPipeline → 大量 src/* + data_provider/* + bot/*
→ `src/core/market_review.py` → run_market_review
→ `src/core/trading_calendar.py`
→ `src/feishu_doc.py`
→ `src/services/backtest_service.py`
→ `src/analyzer.py` → GeminiAnalyzer → litellm, src/agent/llm_adapter, src/agent/skills/defaults
→ `src/notification.py` → NotificationService → bot/models, 所有notification_sender/*
→ `src/search_service.py`
→ `src/scheduler.py`
→ `src/agent/events.py`
→ `bot/platforms.py`
→ `data_provider/base.py`

src/core/pipeline.py 的传递依赖:
→ `data_provider/__init__.py` → DataFetcherManager (加载所有fetchers)
→ `data_provider/realtime_types.py`
→ `data_provider/us_index_mapping.py`
→ `src/storage.py` → sqlite
→ `src/data/stock_mapping.py`
→ `src/report_language.py`
→ `src/schemas/report_schema.py`
→ `src/market_context.py`
→ `src/enums.py`
→ `src/stock_analyzer.py`
→ `src/services/social_sentiment_service.py`
→ `src/services/history_loader.py`
→ `src/services/history_service.py`
→ `src/services/history_comparison_service.py`
→ `src/services/report_renderer.py`
→ `src/services/task_queue.py` → `src/services/analysis_service.py`
→ `src/services/portfolio_service.py` → `src/services/portfolio_risk_service.py` → `src/services/portfolio_import_service.py`
→ `src/services/stock_code_utils.py` → `src/services/name_to_code_resolver.py` → `src/services/import_parser.py`
→ `src/services/system_config_service.py`
→ `src/services/image_stock_extractor.py`
→ `src/services/agent_model_service.py`
→ `src/repositories/__init__.py` → analysis_repo, backtest_repo, stock_repo, portfolio_repo
→ `src/agent/` (所有模块: factory, executor, orchestrator, runner, memory, protocols, llm_adapter, conversation, research, agents/*, skills/*, strategies/*, tools/*)
→ `src/core/config_manager.py`
→ `src/core/config_registry.py`
→ `src/core/market_profile.py`
→ `src/core/market_strategy.py`
→ `src/core/backtest_engine.py`
→ `src/data/stock_index_loader.py`
→ `src/formatters.py`
→ `src/market_analyzer.py`
→ `src/md2img.py`
→ `src/notification_sender/*` (all senders)
→ `src/utils/data_processing.py`
→ `src/utils/analysis_metadata.py`
→ `bot/models.py`

#### 3. simulated_trading.py
→ `scripts/trading_calendar.py`
→ `layers/execution_layer/fees.py` → `layers/risk_layer/models.py` (RiskConfig)
→ `risk/market_filter.py`
→ `data_provider/data_cache.py` (via _verify_ai_score)
→ 读取外部JSON: quant_engine/garch_output.json, quant_engine/hmm_output.json, quant_engine/markowitz_output.json, sentiment_engine/vol_timing.json, sentiment_engine/adjustment.json

#### 4. quant_engine/run_quant.py
→ `quant_engine/garch.py` → `quant_engine/_prices.py`
→ `quant_engine/hmm_market.py`
→ `quant_engine/markowitz.py` → `quant_engine/_prices.py`
→ `quant_engine/__init__.py`

#### 5. sentiment_engine/sentiment_index.py
→ 独立模块，仅用标准库，读取 reports/market_review_*.md

#### 6. sentiment_engine/vol_timing.py
→ 独立模块 (numpy, pandas, akshare)

#### 7. scripts/daily_import_sim.py
→ 独立模块 (json, sqlite3)，读取 simulated_trading/*.json

#### 8. scripts/generate_signal_push.py
→ 独立模块，读取 screener/*.json, simulated_trading/state.json, sentiment_engine/sentiment_*.txt

#### 9. scripts/run_backtest.py
→ `scripts/backtest_compare.py` → `layers/execution_layer/fees.py`
→ `scripts/backtest_real.py` → `layers/execution_layer/fees.py` + `layers/risk_layer/models.py` + `data_provider/data_cache.py`
→ `scripts/backtest_hs300_v4_survivorship.py` (独立)

#### 10. patch/
→ `patch/eastmoney_patch.py` → 由 `data_provider/akshare_fetcher.py` 和 `data_provider/efinance_fetcher.py` 导入

---

## 二、全量文件分类

### ✅ PROD — 生产代码（每天跑）

#### 根目录
| 文件 | 原因 |
|------|------|
| main.py | run_and_send.sh 直接调用 |
| run_screener.py | run_and_send.sh 直接调用 |
| simulated_trading.py | run_and_send.sh 直接调用 |

#### scripts/
| 文件 | 原因 |
|------|------|
| daily_import_sim.py | run_and_send.sh 直接调用 |
| generate_signal_push.py | run_and_send.sh 直接调用 |
| run_backtest.py | run_and_send.sh 周五调用 |
| backtest_real.py | run_backtest.py 导入 |
| backtest_compare.py | run_backtest.py 导入 |
| backtest_hs300_v4_survivorship.py | run_backtest.py 导入 |
| trading_calendar.py | simulated_trading.py 导入 |

#### data_provider/
| 文件 | 原因 |
|------|------|
| base.py | main.py 导入 canonical_stock_code；pipeline 导入 normalize_stock_code |
| data_cache.py | simulated_trading.py + screener + backtest 脚本导入 |
| realtime_types.py | pipeline.py 导入 |
| mx_fetcher.py | run_screener.py 导入 |
| us_index_mapping.py | pipeline.py 导入 |
| __init__.py | pipeline.py 导入 DataFetcherManager |
| akshare_fetcher.py | DataFetcherManager 懒加载 |
| baostock_fetcher.py | DataFetcherManager 懒加载 |
| efinance_fetcher.py | DataFetcherManager 懒加载 |
| fallback_provider.py | 被 manager 懒加载 |
| fundamental_adapter.py | 被 manager 懒加载 |
| jqdata_fetcher.py | 被 manager 懒加载 |
| jqdata_fundamental.py | 被 manager 懒加载 |
| mootdx_fetcher.py | 被 manager 懒加载 |
| pytdx_fetcher.py | 被 manager 懒加载 |
| tushare_fetcher.py | 被 manager 懒加载 |

#### layers/ (仅部分被引用)
| 文件 | 原因 |
|------|------|
| execution_layer/fees.py | simulated_trading.py + backtest_*.py 导入 |
| execution_layer/models.py | execution_layer/engine.py 导入（但 engine 未被生产链调用）→ 间接 PROD |
| execution_layer/engine.py | 被 layers/ 内部模块引用（但未被生产链调用）→ 间接 PROD |
| execution_layer/__init__.py | 被 layers/ 内部引用 → 间接 PROD |
| execution_layer/persistence.py | 被 engine 引用 → 间接 PROD |
| execution_layer/portfolio.py | 被 engine 引用 → 间接 PROD |
| risk_layer/models.py | fees.py + backtest_*.py 导入 |
| risk_layer/__init__.py | 模块初始化 → 间接 PROD |
| risk_layer/stop_loss.py | 被 risk_layer/manager.py 引用 |
| risk_layer/drawdown.py | 被 risk_layer/manager.py 引用 |
| risk_layer/kelly.py | 被 risk_layer/manager.py 引用 |
| risk_layer/rebalance.py | 被 risk_layer/manager.py 引用 |
| risk_layer/manager.py | 被 execution_layer/engine.py 引用 |

#### quant_engine/
| 文件 | 原因 |
|------|------|
| run_quant.py | run_and_send.sh 直接调用 |
| garch.py | run_quant.py 导入；simulated_trading.py 读取其输出JSON |
| hmm_market.py | run_quant.py 导入 |
| markowitz.py | run_quant.py 导入；simulated_trading.py 直接导入 normalize_weights_for_positions |
| _prices.py | garch.py + markowitz.py 导入 |
| __init__.py | 模块初始化 |

#### sentiment_engine/
| 文件 | 原因 |
|------|------|
| sentiment_index.py | run_and_send.sh 直接调用；simulated_trading.py 读取其输出 |
| vol_timing.py | run_and_send.sh 直接调用；simulated_trading.py 读取其输出 |
| __init__.py | 模块初始化 |

#### screener/
| 文件 | 原因 |
|------|------|
| stock_screener.py | run_screener.py 导入 |

#### strategies/
| 文件 | 原因 |
|------|------|
| engine.py | run_screener.py 导入 batch_match |

#### risk/
| 文件 | 原因 |
|------|------|
| market_filter.py | simulated_trading.py 导入 get_market_state |

#### patch/
| 文件 | 原因 |
|------|------|
| eastmoney_patch.py | data_provider akshare/efinance fetchers 导入 |
| __init__.py | 模块初始化 |

#### bot/
| 文件 | 原因 |
|------|------|
| models.py | pipeline.py + notification.py + task_service.py 导入 |
| __init__.py | 模块初始化 |
| platforms.py | main.py 导入 |

#### src/ (大量核心模块，全部为 PROD)
| 文件 | 原因 |
|------|------|
| config.py | main.py 导入 |
| logging_config.py | main.py 导入 |
| storage.py | pipeline.py + analyzer.py 导入 |
| analyzer.py | main.py (GeminiAnalyzer) + pipeline.py (fill_chip_structure) 导入 |
| notification.py | main.py (NotificationService) + pipeline.py 导入 |
| search_service.py | main.py + pipeline.py 导入 |
| scheduler.py | main.py 导入 |
| webui_frontend.py | main.py 导入 |
| feishu_doc.py | main.py 导入 |
| enums.py | pipeline.py 导入 |
| stock_analyzer.py | pipeline.py 导入 |
| market_analyzer.py | src/core/market_review.py 导入 |
| formatters.py | 被 src/notification.py/scheduler 等引用 |
| md2img.py | 被 notification.py 导入 |
| report_language.py | pipeline.py + analyzer.py 导入 |
| market_context.py | analyzer.py 导入 |
| data/stock_mapping.py | pipeline.py + analyzer.py 导入 |
| data/stock_index_loader.py | 被 pipeline 间接引用 |
| schemas/__init__.py + report_schema.py | analyzer.py 导入 |
| repositories/__init__.py + all repos | services/ 导入 |
| services/ (全部) | pipeline/notification/task_queue 导入链 |
| utils/data_processing.py | notification.py + history_service.py 导入 |
| utils/analysis_metadata.py | task_queue.py 导入 |
| agent/ (全部模块) | pipeline.py + analyzer.py + main.py 导入链 |
| core/pipeline.py | main.py 导入 |
| core/market_review.py | main.py 导入 |
| core/trading_calendar.py | main.py 导入 |
| core/config_manager.py | main.py ConfigManager 导入 |
| core/config_registry.py | 被 config_manager 间接引用 |
| core/market_profile.py | 被 pipeline 间接引用 |
| core/market_strategy.py | 被 pipeline 间接引用 |
| core/backtest_engine.py | 被 services/backtest_service 导入 |
| notification_sender/* (全部) | notification.py 懒加载 |

---

### ❌ DEAD — 死代码（未被生产链引用）

| 文件 | 原因 |
|------|------|
| webui.py | 独立的 Web 前端入口，不在 cron 调用链中 |

#### layers/ — 未被引用的子模块
| 文件 | 原因 |
|------|------|
| layers/decision_layer/engine.py | 无任何生产代码导入 |
| layers/decision_layer/manager.py | 无任何生产代码导入 |
| layers/decision_layer/models.py | 无任何生产代码导入 |
| layers/decision_layer/scoring.py | 无任何生产代码导入 |
| layers/decision_layer/signals.py | 无任何生产代码导入 |
| layers/decision_layer/strategy.py | 无任何生产代码导入 |
| layers/decision_layer/__init__.py | 无任何生产代码导入 |
| layers/data_layer/cache.py | 无任何生产代码导入（仅被 own _verify.py 测试导入） |
| layers/data_layer/definition.py | 无任何生产代码导入 |
| layers/data_layer/fetchers/akshare.py | 无任何生产代码导入 |
| layers/data_layer/fetchers/base.py | 无任何生产代码导入 |
| layers/data_layer/fetchers/__init__.py | 无任何生产代码导入 |
| layers/data_layer/models.py | 无任何生产代码导入 |
| layers/data_layer/provider.py | 无任何生产代码导入 |
| layers/data_layer/__init__.py | 无任何生产代码导入 |
| layers/data_layer/test_data_layer.py | 测试文件 |
| layers/data_layer/_verify.py | 仅内部验证脚本 |
| layers/__init__.py | 虽被导入但仅做包初始化 |

#### scripts/ — 不在调用链中
| 文件 | 原因 |
|------|------|
| scripts/healthcheck.py | 未被任何脚本导入或调用 |
| scripts/migrate_cache.py | 未被任何脚本导入或调用 |
| scripts/quick_backtest.py | 未被 run_backtest.py 导入（仅 archive 中有 quick_backtest.py） |
| scripts/backtest_full.py | 未被 run_backtest.py 导入（仅存在于 scripts/ 但不在调用链；archive/ 也有副本） |

#### data_provider/ — 实际可能通过 manager 懒加载
| 文件 | 原因 |
|------|------|
| (全部 data_provider 文件已列入 PROD，因为是 DataFetcherManager 的懒加载候选) | — |

---

### 🗄️ ARCHIVE — 归档目录（确认不参与生产链）

`archive/` 全部文件：
| 文件 | 原用途 |
|------|--------|
| alert.py | 旧版告警脚本（scripts/ 需要但缺失） |
| intraday_trading.py | 旧版日内交易（scripts/ 需要但缺失） |
| mx_enrich.py | 旧版妙想预取（scripts/ 需要但缺失） |
| backtest_100.py | 旧版100股回测 |
| backtest_compare.py | 旧版对比回测（scripts/ 有新版） |
| backtest_csi500.py | 旧版中证500回测 |
| backtest_full.py | 旧版全A回测（scripts/ 有新版） |
| backtest_hs300.py | 旧版HS300回测 |
| backtest_hs300_v2_risk.py | 旧版v2风控回测 |
| backtest_hs300_v3_mafilter.py | 旧版v3均线过滤回测 |
| backtest_hs300_v4_survivorship.py | 旧版v4回测（scripts/ 有新版） |
| factor_ic_test.py | IC因子测试 |
| fill_backtest_data.py | 回测数据填充 |
| migrate_cache_to_sqlite.py | 缓存迁移 |
| migrate_to_sqlite.py | 数据迁移 |
| monitor_candidates.py | 候选股监控 |
| prepare_100_stocks.py | 100股数据准备 |
| pull_csi500_data.py | CSI500数据拉取 |
| quick_backtest.py | 旧版快速回测 |
| sentiment_monitor.py | 旧版情绪监控 |

---

### 🧪 TEST — 测试文件
| 文件 | 原因 |
|------|------|
| tests/* | 所有测试文件，不在生产链中 |

### 🔧 SKILLS/TOOLS — 辅助工具
| 文件 | 原因 |
|------|------|
| skills/code-review/scripts/review_once.py | 辅助工具，不在生产链中 |
| sources/dsa_vi/gen_icons.py | 辅助工具，不在生产链中 |

---

## 三、不一致 / 潜在 Bug

### 🔴 严重

1. **scripts/alert.py、scripts/mx_enrich.py、scripts/intraday_trading.py 缺失**
   - 这3个文件被 `run_and_send.sh` 引用但不存在于 `scripts/`
   - 仅存在于 `archive/` 中
   - **影响**: cron 9:00 时段执行 `scripts/intraday_trading.py execute`、`scripts/mx_enrich.py --codes=...` 会直接失败；任何步骤失败调用的 `scripts/alert.py` 也不会执行
   - **修复**: 将 `archive/alert.py`、`archive/intraday_trading.py`、`archive/mx_enrich.py` 复制到 `scripts/`，或从 shell 脚本中移除这些引用

2. **scripts/backtest_full.py 重复**：存在于 `scripts/` 和 `archive/` 两个位置
   - `scripts/backtest_full.py` 导入 layers 模块（使用生产费率）
   - `archive/backtest_full.py` 是旧版
   - `run_backtest.py` 的 `run_full()` 明确表示这个不可用（调用时直接 log warning）
   - **建议**: 确认 scripts/backtest_full.py 是否需要保留，可移至 archive/

### 🟡 中等

3. **layers/decision_layer/ 和 layers/data_layer/ 是完全死代码**
   - 共 17 个 .py 文件 + __init__.py，包含完整的重构版分层架构
   - 无任何生产代码导入它们（仅在 layers/ 目录内部有自引用验证）
   - 这些是计划中的重构模块，但从未接入实际调用链
   - **建议**: 如果确认不再需要，可移至 archive/ 保持清洁

4. **layers/risk_layer/ 仅部分被使用**
   - `risk_layer/models.py`（RiskConfig）被 fees.py 和 backtest_*.py 使用 ✅
   - `risk_layer/stop_loss.py`, `drawdown.py`, `kelly.py`, `rebalance.py`, `manager.py` 仅被 execution_layer/engine.py 间接引用
   - 但 `execution_layer/engine.py`（SimulatedExecutionEngine）**不被 simulated_trading.py 使用** — simulated_trading.py 有自己的 `execute_trades()`, `check_stop_loss()`, `check_account_drawdown()` 实现
   - **结论**: risk_layer 的 stop_loss/kelly/drawdown 逻辑在生产中实际由 simulated_trading.py 内部的函数提供，而非 risk_layer 模块

5. **simulated_trading.py 未使用 layers/risk_layer 的风控函数**
   - `simulated_trading.py` 自己定义了 `check_stop_loss()`, `check_account_drawdown()`, `_kelly_fraction()`, `_compute_kelly_amount()`
   - 这些函数与 `layers/risk_layer/stop_loss.py` 和 `layers/risk_layer/kelly.py` 中的版本是重复实现
   - `layers/risk_layer/` 虽然正确实现了这些逻辑，但生产代码用的是 simulated_trading.py 的内联版本

### 🟢 低风险 / 确认正常

6. **fees.py 费率一致性** ✅
   - `layers/execution_layer/fees.py` 的 DEFAULT_COMMISSION_RATE、DEFAULT_STAMP_TAX_RATE 等模块级常量 ✅
   - `layers/risk_layer/models.py` 的 RiskConfig.commission_rate、stamp_tax_rate 与之对齐 ✅
   - `calc_buy_fees()` / `calc_sell_fees()` 签名支持参数化覆盖 ✅
   - 所有调用方（simulated_trading.py, backtest_compare.py, backtest_real.py）从 fees.py 引用，不自己定义费率 ✅
   - **注意**: `backtest_full.py` 使用模块级常量 DEFAULT_COMMISSION_RATE（注释说 TODO#29 应从 RiskConfig 管理），但也算从 fees.py 引用 ✅

7. **RiskConfig vs simulated_trading.py 参数一致性** ✅
   - `RiskConfig.stop_loss_pct = -8.0` ↔ `simulated_trading.py HARD_STOP_PCT = -8.0` ✅
   - `RiskConfig.take_profit_pct = 25.0` ↔ `simulated_trading.py TAKE_PROFIT_PCT = 25.0` ✅
   - `RiskConfig.max_drawdown_pct = -20.0` ↔ `simulated_trading.py ACCOUNT_DRAWDOWN_LIMIT = -30.0` ⚠️ **不一致**
     - RiskConfig 用的是 -20%，simulated_trading.py 用的是 -30%
     - 但 simulated_trading.py 不通过 RiskConfig 读取此值，所以目前无实际影响

8. **garch.py 参数传递** ✅
   - `estimate_garch(stock_code: Optional[str] = None)` — 入参类型正确
   - `stock_returns(stock_code)` 和 `aggregated_returns()` 分别处理单股和全市场聚合
   - 调用方 (run_quant.py) 调用 `run_garch_and_save()` 不带参数（→ None → 全市场聚合），正确 ✅

9. **backtest 脚本引用生产模块** ✅
   - `scripts/backtest_real.py` ← `data_provider.data_cache` + `layers.execution_layer.fees` + `layers.risk_layer.models` ✅
   - `scripts/backtest_compare.py` ← `layers.execution_layer.fees` ✅
   - `scripts/backtest_full.py` ← `layers.execution_layer.fees` + `layers.risk_layer.models` ✅
   - `scripts/backtest_hs300_v4_survivorship.py` ← 纯 numpy/pandas（不引用 layers，但也不需要） ✅

10. **simulated_trading.py 中的 quant_engine import** ✅
    - `normalize_weights_for_positions` 从 `quant_engine.markowitz` 导入（用于再平衡），正确 ✅

---

## 四、汇总统计

| 分类 | 数量 |
|------|------|
| PROD (生产代码) | ~130+ files |
| DEAD (死代码 - layers/) | ~17 files |
| ARCHIVE (已归档) | 20 files |
| TEST (测试) | ~12 files |
| TOOLS (辅助) | 2 files |
| **需修复的严重问题** | 3 files (缺失脚本) |
| **重复文件** | 4 files (scripts/ 与 archive/ 重复) |

---

## 五、建议

1. **立即修复**: 将 `archive/alert.py`, `archive/intraday_trading.py`, `archive/mx_enrich.py` 复制到 `scripts/`，或修改 `run_and_send.sh` 移除对应引用
2. **清理 layers/**: 将 `layers/decision_layer/` 和 `layers/data_layer/` 移至 `archive/`（它们是完整但未接入的重构尝试）
3. **统一风控逻辑**: simulated_trading.py 内联风控函数与 layers/risk_layer/ 重复——应选择其一作为唯一事实来源
4. **清理重复 backtest 脚本**: scripts/backtest_full.py 在两个位置都存在，确认取舍
5. **ACCOUNT_DRAWDOWN_LIMIT 不一致**: simulated_trading.py (-30%) 和 RiskConfig (-20%) 值不同，应统一
