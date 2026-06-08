"""
费用计算 — 从 simulated_trading.py 的 calc_buy_fees / calc_sell_fees 迁移。

A股真实费率（与原值对齐）：
- 佣金：万2.5（买卖均收，最低5元）
- 印花税：万5（仅卖出时收）
- 过户费：万0.1（买卖均收）
"""

import logging
from typing import Optional

logger = logging.getLogger("execution_layer")

# ── 默认费率参数（与原 simulated_trading.py 一致）──
# TODO(#29): 费率应从 RiskConfig / 配置文件集中管理，
# 而不是硬编码为模块级常量。当前各调用方直接依赖这些默认值。
DEFAULT_COMMISSION_RATE = 0.00025  # 佣金万2.5
DEFAULT_STAMP_TAX_RATE = 0.0005  # 印花税万5
DEFAULT_TRANSFER_FEE_RATE = 0.00001  # 过户费万0.1
DEFAULT_MIN_COMMISSION = 5.0  # 佣金最低收费5元


def calc_buy_fees(
    cost: float,
    commission_rate: Optional[float] = None,
    transfer_fee_rate: Optional[float] = None,
    min_commission: Optional[float] = None,
) -> float:
    """计算买入费用 = 佣金（最低5元）+ 过户费。

    从 simulated_trading.py 原样迁移，签名扩展了参数化支持。

    Args:
        cost: 买入金额（股数 × 价格）
        commission_rate: 佣金费率，None 则使用默认值万2.5
        transfer_fee_rate: 过户费率，None 则使用默认值万0.1
        min_commission: 最低佣金，None 则使用默认值5.0

    Returns:
        买入总费用
    """
    rate = commission_rate if commission_rate is not None else DEFAULT_COMMISSION_RATE
    tf_rate = transfer_fee_rate if transfer_fee_rate is not None else DEFAULT_TRANSFER_FEE_RATE
    min_c = min_commission if min_commission is not None else DEFAULT_MIN_COMMISSION

    commission = max(cost * rate, min_c)
    transfer_fee = cost * tf_rate
    return commission + transfer_fee


def calc_sell_fees(
    proceeds_before_fees: float,
    commission_rate: Optional[float] = None,
    stamp_tax_rate: Optional[float] = None,
    transfer_fee_rate: Optional[float] = None,
    min_commission: Optional[float] = None,
) -> float:
    """计算卖出费用 = 佣金（最低5元）+ 过户费 + 印花税。

    从 simulated_trading.py 原样迁移，签名扩展了参数化支持。

    Args:
        proceeds_before_fees: 卖出前收入（股数 × 价格）
        commission_rate: 佣金费率，None 则使用默认值万2.5
        stamp_tax_rate: 印花税费率，None 则使用默认值万5
        transfer_fee_rate: 过户费率，None 则使用默认值万0.1
        min_commission: 最低佣金，None 则使用默认值5.0

    Returns:
        卖出总费用
    """
    rate = commission_rate if commission_rate is not None else DEFAULT_COMMISSION_RATE
    st_rate = stamp_tax_rate if stamp_tax_rate is not None else DEFAULT_STAMP_TAX_RATE
    tf_rate = transfer_fee_rate if transfer_fee_rate is not None else DEFAULT_TRANSFER_FEE_RATE
    min_c = min_commission if min_commission is not None else DEFAULT_MIN_COMMISSION

    commission = max(proceeds_before_fees * rate, min_c)
    transfer_fee = proceeds_before_fees * tf_rate
    stamp_tax = proceeds_before_fees * st_rate
    return commission + transfer_fee + stamp_tax
