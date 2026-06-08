# 🔍 全面代码审查报告 — daily_stock_analysis

**审查日期:** 2026-06-09
**审查范围:** P0–P1 核心文件（全项目重审）
**审查框架:** 五维分类法（数学逻辑 → 数据流 → 边界条件 → 性能 → 可维护性）

---

## 目录

1. [🔴 CRITICAL 问题（必修）](#-critical-问题)
2. [🟡 WARNING 问题（应修）](#-warning-问题)
3. [🟢 INFO 建议（记录）](#-info-建议)
4. [汇总统计](#汇总统计)
5. [修复路线图](#修复路线图)

---

## 🔴 CRITICAL 问题

### 1. `quant_engine/markowitz.py` — 最大夏普梯度上升忽略 λ（数学逻辑）

**位置:** `_solve_max_sharpe()` 第 ~180 行

**问题:** 内层梯度上升完全忽略风险厌恶系数 λ：

```python
grad = 2.0 * sigma @ w - mu  # -∇(μ - λΣw) 的负方向
w = w + 0.01 * (mu - 2.0 * sigma @ w)  # 梯度上升
```

实际的优化目标应为 `max w^T μ - λ · w^T Σ w`，梯度应为 `μ - 2λ · Σ · w`。当前代码等效于固定 `λ=1` 的优化，使外层 λ 二分搜索完全失效。最大夏普比率的计算结果不可靠。

**后果:** 马科维茨最优权重可能偏离真实有效前沿，组合优化结果有系统性偏差。

**修复建议:** 梯度应改为 `mu - 2.0 * λ * sigma @ w`，或将 λ 作为参数传入内层循环。

---

### 2. `quant_engine/hmm_market.py` — 观测特征退化为 1 维（数据流）

**位置:** `_extract_market_features()` 第 ~145-200 行

**问题:** 振幅和成交额变化特征始终硬编码为 `0.0`：

```python
features.append(np.array([
    np.mean(daily_rets),
    0.0,  # 振幅(暂无)
    0.0,  # 成交额(暂无)
]))
```

报告解析代码尝试提取真实振幅/成交额数据，但提取的值从未被赋值回 features 数组。HMM 实际只用 1 维特征运行，4 状态 × 3 观测维度的模型设计名存实亡。

**后果:** 市场状态检测失去多维度信息，状态区分度大幅降低。高波动/震荡/多空状态可能无法正确区分。

**修复建议:** 将振幅和成交额提取逻辑正确接入 features 构建，或在报告格式演进前降为 1 维观测 HMM（改 `N_DIMS = 1`）。

---

### 3. `layers/risk_layer/models.py` vs `simulated_trading.py` — 止损/凯利参数不一致（数据流/数学逻辑）

**位置:**
- `RiskConfig.stop_loss_pct` 默认 `-15.0`
- `simulated_trading.py` `HARD_STOP_PCT = -8.0`
- `RiskConfig.kelly_b()` 返回 `abs(25/-15) = 1.67`
- `simulated_trading.py` `KELLY_B = abs(10/-8) = 1.25`

**问题:** 迁移后的 risk_layer 使用不同于 simulated_trading.py 的默认参数。当 `check_stop_loss` 使用 `config=None`（即 `RiskConfig()` 默认值）时，止盈/止损阈值和凯利赔率与原始逻辑完全不同。

**后果:** 如果调用方不显式传参加以 override，止损阈值在 -8% 和 -15% 之间不一致，可能延迟止损导致更大亏损，或错误触发止损；凯利仓位计算也会不同。

**修复建议:**
1. 统一默认参数到一处配置（建议保留 `-8%` 止损 + `10%` 一级止盈的较新参数）
2. `RiskConfig` 应该更新默认值为：`stop_loss_pct=-8.0, take_profit_pct=25.0`
3. 在 `RiskConfig` 中增加注释说明与哪个版本的参数对齐

---

### 4. `simulated_trading.py` / `layers/risk_layer/rebalance.py` — 浮亏-3% 加仓保护失效（数学逻辑）

**位置:**
- `simulated_trading.py` `_try_rebalance()` 加仓分支（~1200行）
- `layers/risk_layer/rebalance.py` `_apply_rebalance_buy()`（缺失该检查）

**问题 A (simulated_trading.py):** 加仓前先修改了 `pos["avg_cost"]`：
```python
pos["avg_cost"] = total_invested / total_qty  # ← 先修改均价
pos["quantity"] = total_qty
pos_pnl = (price - pos["avg_cost"]) / pos["avg_cost"] * 100  # ← 用新均价算
if pos_pnl < -3:
    # 跳过加仓 — 但新均价≈价格，pos_pnl≈0，永不会触发！
```

**问题 B (layers/risk_layer/rebalance.py):** `_apply_rebalance_buy()` 完全缺少浮亏保护，可对亏损持仓加仓。

**后果:** 对已亏损持仓继续加仓（摊低成本但加大风险敞口），违反风控原则。

**修复建议:**
1. 在计算 avg_cost 前保存旧均价，用旧均价计算浮亏百分比进行判断
2. 将检查移至 `_apply_rebalance_buy` 开头
3. 补回 `layers/risk_layer/rebalance.py` 中缺失的检查

---

### 5. `layers/risk_layer/stop_loss.py` — check_stop_loss 使用错误默认参数（数据流）

**位置:** `check_stop_loss()` 第 38 行

```python
cfg = config or _DEFAULT_CONFIG  # _DEFAULT_CONFIG = RiskConfig()
# → RiskConfig() 默认 stop_loss_pct = -15.0
```

但 `simulated_trading.py` 使用的是 `HARD_STOP_PCT = -8.0`。

**问题:** 当调用方从 `manager.py` → `check_stop_loss_dict()` 时，默认使用 -15% 止损，而 `simulated_trading.py` 的 `check_stop_loss` 使用 -8%。两者行为不同。

**后果:** 迁移后的止损比原版宽松近一倍（-15% vs -8%），风险敞口更大。

**修复建议:** 统一 `RiskConfig` 默认 `stop_loss_pct` 为 `-8.0`。

---

### 6. `data_provider/base.py` — 缓存日期比较在 pandas 2.x 下可能静默失败（数据流）

**位置:** 第 ~930-933 行

```python
cache_dates = cached["date"]  # get_kline 返回 .astype(str) 的字符串
req_start = start_date or cache_dates.min()  # start_date 可能是任意格式
req_end = end_date or datetime.now().strftime("%Y-%m-%d")
if cache_dates.min() <= req_start and cache_dates.max() >= req_end:
```

**问题:**
1. `start_date` 的类型不确定（可能是 `str`, `datetime`, `Timestamp`, `None`）
2. 如果 `start_date` 传入的是 `datetime` 对象，`cache_dates.min()`（字符串）与 `datetime` 比较在 pandas 2.x 中可能 `TypeError`
3. 如果 `start_date` 是 `"20260501"` (YYYYMMDD)，字符串比较 `"2026-05-01" < "20260501"` → `False`（因为 "-" > "6" in ASCII），导致缓存"命中"判断错误

**后果:**
- 最坏情况：缓存误判为命中，跳过数据拉取，报告使用过期数据
- 次坏情况：每次跳过缓存重新拉取，网络请求量翻倍

**修复建议:**
1. 统一 date 列格式为 `datetime64[ns]` 或统一为同一种字符串格式
2. 在比较前强制转换：`pd.Timestamp(cache_dates.min()) <= pd.Timestamp(req_start)`

---

### 7. `simulated_trading.py` — total_pnl 计算逻辑不一致（数据流/数学逻辑）

**位置:** `execute_trades()` 第 ~960 行

```python
state["total_pnl"] = round(total_equity - INITIAL_CAPITAL, 2)
```

但在风控卖出（stop_loss/take_profit/low_score_sell）时，`state["total_pnl"]` 没有被增量更新：
```python
state["cash"] += proceeds
state["total_fee"] += fee
# state["total_pnl"] 没有 += pnl ！
```

**问题:** 最终用 `total_equity - INITIAL_CAPITAL` 计算总 PnL 是正确的（因为包含了所有现金变动），但中间如果有代码读取 `state["total_pnl"]`（如在风控卖出的同时），会读到错误值。虽然在当前流程中 `total_pnl` 只在最后被写入，但这构成了潜在的时序依赖 bug。

**后果:** 如果在卖出和最终 PnL 计算之间有任何代码读取 `state["total_pnl"]`，会得到偏高的值（少计了卖出的 PnL 损失）。

**修复建议:** 在每个卖出行增加 `state["total_pnl"] += pnl` 或添加注释说明不依赖中间值。

---

## 🟡 WARNING 问题

### 8. `quant_engine/garch.py` — α+β 约束过紧

**位置:** 第 68、83 行

```python
if alpha + beta >= 0.995:
    return 1e12
```

使用 0.995 而非 1.0 约束。对于趋势性强的市场（α+β 接近 1，如 0.997），会被错误惩罚。

**建议:** 放宽到 `>= 0.999` 或 `>= 1 - 1e-4`。

---

### 9. `quant_engine/garch.py` — BFGS Hessian 求逆无正则化

**位置:** `_finite_diff_hessian()` + `estimate_garch()` 第 ~230 行

```python
se = np.sqrt(np.abs(np.diag(np.linalg.inv(H))))
```

当 Hessian 接近奇异时（参数高度相关时常见），`np.linalg.inv(H)` 产生无法解释的大标准误。虽有 try/except 兜底，但异常时返回 NaN，不如用伪逆或加 L2 正则化。

**建议:** 使用 `np.linalg.pinv(H, rcond=1e-6)` 或加对角正则化 `H + np.eye(3) * 1e-6`。

---

### 10. `quant_engine/garch.py` — `_garch_llh` 和 `_garch_llh_raw` 代码重复

**位置:** 第 63-100 行

两个函数逻辑几乎完全相同。`_garch_llh_raw` 可以调用 `_garch_llh` 并传入反变换参数。

**建议:** 合并为单一函数，减少维护负担。

---

### 11. `quant_engine/hmm_market.py` — 前向算法对数空间缺少归一化防御

**位置:** `_forward_log()` 第 ~110 行

```python
la[t, j] = np.max(la[t-1]) + np.log(np.sum(...) + 1e-300) + ...
```

每次迭代的 `np.max(la[t-1])` 做了 log-sum-exp 的减最大值操作。但连续多日数据后，`la` 累积值可能接近边界，如果 `max(la[t-1])` 本身是 `-inf`（从初始化时），会导致 NaN。

**建议:** 增加 `np.max(la[t-1])` 为 `-inf` 时的兜底处理。

---

### 12. `quant_engine/hmm_market.py` — Baum-Welch M-step 转移矩阵未严格归一化

**位置:** `fit()` 第 ~175 行

```python
self.transmat_[i, j] = (xi[:, i, j].sum() + 1e-10) / denom
```

分子加了 `1e-10` 平滑项，但未在循环后重新归一化行和为 1。

**建议:** M-step 完成后添加 `self.transmat_ /= self.transmat_.sum(axis=1, keepdims=True)`。

---

### 13. `quant_engine/markowitz.py` — 收缩估计公式精度

**位置:** `_estimate_parameters()` 第 ~240 行

```python
shrinkage = max(0.0, min(1.0, (N + 2) / (r + N + 2)))
```

这是 Ledoit-Wolf 类收缩的简化近似。当 `N`（股票数）接近 `r`（样本数）时，收缩强度可能不够。

**建议:** 考虑使用 `sklearn.covariance.LedoitWolf` 或 OAS 估计器。

---

### 14. `quant_engine/markowitz.py` — λ 搜索范围可能不足

**位置:** `_solve_max_sharpe()` 第 ~175 行

```python
λ_low, λ_high = 0.0, 50.0
```

对于极端数据集（低波动 + 高收益差），最优 λ 可能 > 50。

**建议:** 动态扩展搜索范围：如果 `λ_mid == λ_high` 收敛到边界，向外延伸。

---

### 15. `quant_engine/_prices.py` — `extract_price_matrix` 全日期交集过滤过于严格

**位置:** 第 ~100 行

```python
if None not in series:
    matrix.append(series)
    valid_codes.append(code)
```

只要一只股票缺少任意一天数据就完全排除。在大样本（2000+只）下，日期对齐后可能只有几十只通过。

**建议:** 使用前向填充（`ffill`）或插值处理缺失日期，或只要求 N% 的日期覆盖即可。

---

### 16. `quant_engine/_prices.py` — 价格提取正则可能误匹配

**位置:** `_find_current_price()` 第 ~155-175 行

```python
nums = re.findall(r'[\d.]+', lines[i + 2])
for n in nums:
    v = float(n)
    if 0.5 < v < 10000:
        return v
```

可能匹配到日期（如 "2026.06.09"）、百分比（"5.25%"）、或其他非价格数字。

**建议:** 增加更精确的上下文匹配（如 "当前价：XX.XX" 格式）作为优先方法。

---

### 17. `src/core/backtest_engine.py` — 中文否定词匹配过于简单

**位置:** `_is_negated()` 第 ~75 行

```python
_NEGATION_PATTERNS = (..., "不", "别", "勿", "没有", ...)
```

"不" 可能匹配到非否定上下文（如"不同"、"不断"），"没有" 可能匹配到"没有风险"（否定风险=利好）。

**建议:** 中英文否定应使用更精确的边界匹配或 NLP 分词。

---

### 18. `src/core/backtest_engine.py` — stop-loss/take-profit 同时触发时偏向悲观

**位置:** `_evaluate_targets()` 第 ~424 行

```python
if stop_hit and tp_hit:
    first_hit = "ambiguous"
    exit_price = stop_loss  # 假设止损先触发
```

**建议:** 同时触发时标记为 ambiguous 但不假设止损价格，或用中间价。

---

### 19. `risk/market_filter.py` — MA60 不可用时静默回退

**位置:** `get_market_scale()` + `get_market_state()` 第 ~120 行

```python
above_ma60 = close > ma60 if ma60 > 0 else True
```

当 MA60 计算值为 0 时（数据不足 60 天），静默返回 `True`（视为站上 MA60），可能导致满仓。

**建议:** 当 MA60/MA120 不可用时，应返回保守仓位（如 0.25）并 logger.warning。

---

### 20. `layers/risk_layer/drawdown.py` — 函数修改传入的 state dict

**位置:** `check_account_drawdown()` 第 ~42 行

```python
if current_equity > peak:
    state["peak_equity"] = current_equity  # ← 修改调用方的 dict
```

**建议:** 改为返回新的 state 副本或在返回结果中包含新 peak_equity，让调用方决定是否更新。

---

### 21. `simulated_trading.py` — `_verify_ai_score` RSI 使用简单平均而非 Wilder 平滑

**位置:** `_verify_ai_score()` 第 ~1550 行

```python
avg_gain = sum(gains[-14:]) / 14
avg_loss = sum(losses[-14:]) / 14
```

标准的 Wilder RSI 使用指数平滑（SM+EMA 混合），而非简单平均。

**建议:** 使用 pandas-ta 或手动实现 Wilder 平滑算法。

---

### 22. `simulated_trading.py` — rebalance 加仓缺少止盈/止损位置跟踪后的处理

**位置:** `_try_rebalance()` 加仓分支

加仓后 `pos["tp_level"]` 和 `entry_date` 不更新，导致止盈级数判定可能错误（第一次买入已经触发一级止盈，加仓后不应重置止盈计数器）。

**建议:** 加仓后保留 `tp_level` 或使用加权平均的止盈目标。

---

### 23. `quant_engine/markowitz.py` — `_estimate_parameters` 使用固定 10% 年化先验

**位置:** 第 ~250 行

```python
prior = np.ones(N) * 0.10  # 合理长期权益收益率
```

**问题:**
- 对于 A 股市场，10% 年化期望偏高
- 对于不同市场环境（牛市/熊市），固定先验不调整
- 小样本下（r < 60），先验权重过大，所有股票期望收益趋向 10%，导致优化结果退化为等权

**建议:** 将先验设为无风险利率（如 LPR 或国债收益率），或使用 Black-Litterman 框架。

---

### 24. `simulated_trading.py` — 卖出价格获取失败时使用原位价

**位置:** `execute_trades()` 第 ~660 行

```python
price = extract_stock_price(code, report_date_str) or pos["current_price"]
```

如果 `extract_stock_price` 返回 None（报告格式变化等），会使用存储在 state 中的旧价格。这可能导致：
- 止损信号基于过期价格，不反映真实风险
- 以错误价格执行卖出

**建议:** 如果无法获取最新价格，应 logger.warning 并保持持仓不操作（而非用旧价格卖出）。

---

## 🟢 INFO 建议

### 25. `quant_engine/garch.py` — 网格搜索可优化
- 当前三重循环 8×10×10=800 次 LLH 评估，每次 O(T)。可考虑先粗后细的两阶段网格，或使用更高效的初始点。

### 26. `quant_engine/hmm_market.py` — `_extract_market_features` 有代码块从不执行
- 振幅/成交额提取的 for 循环（第 ~170-190 行）提取了值但从未赋值给 features。建议要么移除要么正确接入。

### 27. `quant_engine/_prices.py` — 缺少数据缓存
- `extract_all_prices()` 每次调用都重新解析所有报告。如果在同一次运行中多次调用，浪费大量 I/O。建议加内存缓存。

### 28. `src/core/pipeline.py` — 函数过长
- `StockAnalysisPipeline` 的 `analyze_stock` 和 `fetch_and_save_stock_data` 方法过长（200+行），建议拆分为更小的子方法。

### 29. `layers/execution_layer/fees.py` — 费率硬编码
- 费率参数硬编码为默认值。虽然函数签名支持参数化，但实际调用方从不传参。建议改为从配置文件读取。

### 30. `simulated_trading.py` — 文件过长（1627行）
- 单文件包含报告解析、策略执行、风控、再平衡、绩效统计、信号验证、输出格式等。建议按现有 layers 架构继续拆分，simulated_trading.py 作为薄编排层。

### 31. `risk/market_filter.py` — 无北京时间处理
- `datetime.now()` 使用系统时间，未显式转换时区。在 UTC 系统上可能凌晨 3 点就刷新缓存（实际北京时间上午 11 点就过期）。

### 32. `layers/risk_layer/rebalance.py` vs `simulated_trading.py` — 重复代码
- rebalance.py 有独立的 rebalance 实现，但 simulated_trading.py 中 `_try_rebalance` 函数是重复的（未使用 risk_layer 的版本）。两套实现存在分歧风险。

### 33. `layers/risk_layer/models.py` — `kelly_b()` 类型提示错误
- `kelly_b()` 返回 `abs(take_profit / stop_loss)`。当 `stop_loss_pct = -15.0` 时，`abs(25.0 / -15.0)` = 1.67。但当 `stop_loss_pct = 0` 时返回 1.67（硬编码常量）。`stop_loss_pct` 应为负值，但类型为 `float` 不强制。

### 34. `quant_engine/markowitz.py` — 有效前沿去重可能删除有效点
- 去重逻辑：
  ```python
  if not clean or p["vol"] > clean[-1]["vol"] + 1e-8:
      clean.append(p)
  ```
  如果 lambda 扫描跳过了一个区间（因为梯度投影不收敛），会缺少有效前沿上的点。

### 35. 全局硬编码路径
- 多个文件有 `/opt/daily_stock_analysis` 硬编码路径（garch.py、hmm_market.py、markowitz.py、market_filter.py、_prices.py、simulated_trading.py、rebalance.py、portfolio.py 等）。建议统一为环境变量或配置文件。

---

## 汇总统计

| 严重度 | 数量 | 类别 |
|--------|------|------|
| 🔴 CRITICAL | 7 | 数学逻辑 ×3 / 数据流 ×3 / 数学逻辑+数据流 ×1 |
| 🟡 WARNING | 17 | 数学逻辑 ×5 / 数据流 ×4 / 边界条件 ×2 / 可维护性 ×6 |
| 🟢 INFO | 11 | 性能 ×3 / 可维护性 ×8 |

### 🔴 CRITICAL 问题列表（按严重性排序）

| # | 文件 | 类型 | 简述 |
|---|------|------|------|
| 1 | `markowitz.py` | 数学逻辑 | 最大夏普梯度上升忽略 λ，结果不可靠 |
| 2 | `hmm_market.py` | 数据流 | 观测特征退化为 1 维，HMM 名存实亡 |
| 3 | `models.py/stop_loss.py` | 数据流/数学 | 止损参数迁移后不一致（-15% vs -8%） |
| 4 | `simulated_trading.py` | 数学逻辑 | 加仓-3%浮亏保护因计算顺序失效 |
| 5 | `stop_loss.py` | 数据流 | check_stop_loss 使用错误默认参数 |
| 6 | `base.py` | 数据流 | 缓存日期类型混合比较，pandas 2.x 可能崩溃 |
| 7 | `simulated_trading.py` | 数据流 | total_pnl 中间值不可靠，有竞态风险 |

---

## 修复路线图

### 第一优先级（立即修，可独立提交）

1. **修复 markowitz.py 梯度 bug (CRITICAL #1)**
   - 修改 `_solve_max_sharpe` 内层梯度为 `mu - 2.0 * λ * sigma @ w`
   - 预计影响：修正后最大夏普权重可能变化

2. **统一止损参数 (CRITICAL #3, #5)**
   - 将 `RiskConfig.stop_loss_pct` 默认改为 `-8.0`
   - 将 `RiskConfig.kelly_b()` 的赔率计算参数更新
   - 确保 `simulated_trading.py` 与 `layers/risk_layer/` 行为一致

3. **修复 base.py 日期比较 (CRITICAL #6)**
   - 在比较前统一转换为 `pd.Timestamp`
   - 统一 cache_dates 的数据类型

4. **修复加仓保护 (CRITICAL #4)**
   - 在 `simulated_trading.py` `_try_rebalance()` 中，用旧均价做浮亏判断
   - 在 `layers/risk_layer/rebalance.py` 中补回浮亏保护

### 第二优先级（讨论后修，可能有业务影响）

5. **修复 HMM 特征退化 (CRITICAL #2)**
   - 需要确认报告格式中振幅/成交额数据是否已可用
   - 如果不可用，降级为 N_DIMS=1 或增加其他可用特征

6. **修复 total_pnl 时序 (CRITICAL #7)**
   - 在每个卖出行增加 `state["total_pnl"] += pnl`

7. **修复 kelly/kelly_b 参数一致性问题 (WARNING #21-style)**
   - 重新审核 simulated_trading.py vs risk_layer 所有参数一致性

### 第三优先级（改善，可选）

8. GARCH 约束放宽、Hessian 正则化 (WARNING #8, #9)
9. 代码去重：garch.py _llh_raw, simulated_trading.py vs rebalance.py
10. 硬编码路径参数化 (INFO #35)
11. RSI 算法改用 Wilder 平滑 (WARNING #21)

---

**审查工具:** 五维分类法（数学逻辑 → 数据流 → 边界条件 → 性能 → 可维护性）
**审查人:** 打工虾 🦐 (OpenClaw subagent)
**参考:** `skills/code-review/references/numerical-pitfalls.md`, `skills/code-review/references/checklist.md`
