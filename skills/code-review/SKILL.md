---
name: code-review
description: Systematic code review of math-logic, data-flow, edge-cases, numerical-stability, and maintainability. Trigger when user asks for code review, audit, or verification of analysis code. Applies to Python and Shell in quant-finance and data-analysis contexts.
---

# Code Review — 结构化代码审查

## 审查方法论

按五维分类法逐层扫描，避免遗漏：

### 1. 数学逻辑错误（最严重）
- 公式实现与原始数学定义是否一致（符号、指数、对数）
- 数值方法是否正确（梯度方向、Hessian、收敛判据）
- 统计方法是否有误（似然函数符号、自由度、标准化）
- 约束条件是否真正满足（α+β<1, Σw=1, w≥0）

### 2. 数据流错误
- 数据提取路径是否实际可行（文件存在？列索引正确？）
- 占位符/缺失值的处理（`------` 回退？NaNFill 策略？）
- 数据对齐是否正确（多股票多日期的日期交集）
- 单位换算是否一致（年化因子、百分比 vs 小数）

### 3. 边界条件
- 空数组/空字典 → 不会崩溃
- 单元素序列 → 方差为0、相关矩阵退化
- 极值输入 → log(0)、x/0、sqrt(负数)
- 数据不足 → n<2, n<样本维度
- 收敛失败 → BFGS不收敛、矩阵奇异

### 4. 性能与开销
- 嵌套循环层数（O(n³)预警）
- 文件 I/O 频率（每次调用都读全量？）
- 重复计算（同样的值算多次？）
- 资源释放（DB连接、文件句柄）

### 5. 可维护性
- 硬编码路径（改为配置文件或环境变量）
- 异常捕获粒度（裸 `except:` 吞错误）
- 日志可追溯性（关键中间值是否打印）
- 函数职责单一性

## 审查流程

```
收到审查请求
  ↓
① 理解代码功能: 读 docstring + 算哪个公式
② 看输入: 数据源长什么样？有什么坑？
③ 逐层审查: 数学逻辑 → 数据流 → 边界条件 → 性能 → 维护性
    每层发现的问题标注: 🔴严重 / 🟡关注 / 🟢注释
④ 输出审查报告
```

## 严重度分级

| 级别 | 标签 | 定义 | 响应 |
|------|------|------|------|
| 🔴 严重 | `CRITICAL` | 公式错误/代码必崩/数据必错 | 必须立即修 |
| 🟡 关注 | `WARNING` | 特定条件下会出问题/非最优 | 建议修 |
| 🟢 注释 | `INFO` | 未来风险/可读性/风格 | 记录即可 |

## 参考资源

- 常见数值陷阱：参考 `references/numerical-pitfalls.md`
- 审查清单：参考 `references/checklist.md`

## 工具脚本

`scripts/review_once.py` — 对指定文件执行一次结构化审查，输出 JSON 格式问题清单。适合 CI 集成或批量审查。
