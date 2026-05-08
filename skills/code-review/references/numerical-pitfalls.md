# 常见数值陷阱 — Numerical Pitfalls

## 浮点运算

- `x / y` 当 y 接近 0 → 加 epsilon: `x / (y + 1e-10)`
- `log(x)` 当 x ≤ 0 → 加 epsilon: `log(x + 1e-300)`
- `sqrt(x)` 当 x < 0 → `sqrt(max(x, 0))`
- `exp(x)` 当 x 过大 → 用 log-sum-exp trick
- 浮点比较勿用 `==` → 用 `abs(a-b) < 1e-8`

## 线性代数陷阱

- 矩阵奇异 → 用 `np.linalg.eigvalsh` 检测最小特征值，小于阈值时正则化
- 协方差矩阵非正定 → 加 λI 或用收缩估计 (shrinkage)
- 矩阵求逆 → n 大时优先用 `np.linalg.solve` 而非 `inv`
- numpy 2D vs 1D 混淆 → `shape` 检查: `(n,)` 不是 `(n,1)`

## 统计陷阱

- 小样本年化 → n<20 时×252 会极度放大噪声
- 收益率顺序 → 对数收益率可加，百分比收益率不可
- 方差无偏估计 → `np.var(x, ddof=1)`
- 批量标准化 → 用同一组 mean/std 训练验证，勿用全数据

## 优化陷阱

- 梯度下降固定步长 → 用 line search 或自适应步长
- 无约束优化忽略约束 → 变换参数空间（如 sigmoid 约束 α∈(0,1)）
- 迭代不检查收敛 → 应同时检查梯度范数和步长
