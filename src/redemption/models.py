"""领域枚举与状态机定义。

状态取值沿用 domain/contract.json 的中文领域语言，接口与分录直接展示。
"""
from __future__ import annotations

from enum import StrEnum


class BatchStatus(StrEnum):
    """权益批次生命周期（与领域契约 states 对齐）。"""

    DRAFT = "草拟"
    PENDING_REVIEW = "待核验"
    CONFIRMED = "已确认"
    ACTIVE = "执行中"
    ARCHIVED = "已归档"


class OrderStatus(StrEnum):
    """兑换履约状态机。"""

    RESERVED = "冻结中"
    FULFILLED = "已履约"
    PARTIAL = "部分履约"
    CANCELLED = "已取消"
    REJECTED = "已驳回"
    EXPIRED = "已过期"


TERMINAL_ORDER_STATUSES = {
    OrderStatus.FULFILLED,
    OrderStatus.PARTIAL,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
}


class Dimension(StrEnum):
    """分录维度。"""

    POINTS = "积分"
    STOCK = "库存"


class EntryKind(StrEnum):
    """分录类型：冻结 / 扣减 / 释放（补偿）。"""

    FREEZE = "冻结"
    DEDUCT = "扣减"
    RELEASE = "释放"


# 开放兑换的批次状态
REDEEMABLE_BATCH_STATUSES = {BatchStatus.CONFIRMED, BatchStatus.ACTIVE}

# 批次状态机：草拟 -> 待核验 -> 已确认 -> 执行中 -> 已归档；待核验可退回草拟
BATCH_FLOW = {
    BatchStatus.DRAFT: {BatchStatus.PENDING_REVIEW},
    BatchStatus.PENDING_REVIEW: {BatchStatus.CONFIRMED, BatchStatus.DRAFT},
    BatchStatus.CONFIRMED: {BatchStatus.ACTIVE},
    BatchStatus.ACTIVE: {BatchStatus.ARCHIVED},
    BatchStatus.ARCHIVED: set(),
}
