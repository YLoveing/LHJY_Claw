"""
layers/ — 生产层架构

遵循单向依赖：上层依赖下层，下层不依赖上层。
接口隔离，可独立测试。

已包含层：
- risk_layer/      — 风控层
- execution_layer/ — 执行层

注意：
- data_layer/   → 已移入 archive/（不再使用）
- decision_layer/ → 已移入 archive/（不再使用）
"""

from . import execution_layer, risk_layer

__all__ = [
    "risk_layer",
    "execution_layer",
]
