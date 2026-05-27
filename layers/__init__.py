"""
layers/ — 六层架构重构

遵循单向依赖：上层依赖下层，下层不依赖上层。
接口隔离，可独立测试，渐进迁移。

已包含层：
- data_layer/      — 数据获取 + 缓存 (Phase 1)
- risk_layer/      — 风控层 (Phase 2)
- execution_layer/ — 执行层 (Phase 2)
- decision_layer/  — 决策层 (Phase 3)
"""

from . import data_layer
from . import risk_layer
from . import execution_layer
from . import decision_layer

__all__ = [
    "data_layer",
    "risk_layer",
    "execution_layer",
    "decision_layer",
]
