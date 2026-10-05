"""志愿权益兑换领域模型。"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class OrderState(str, enum.Enum):
    """兑换履约状态机：待履约为唯一非终态。"""

    PENDING = "待履约"
    FULFILLED = "已履约"
    PARTIALLY_FULFILLED = "部分履约"
    CANCELLED = "已取消"
    EXPIRED = "已过期"

    @property
    def is_terminal(self) -> bool:
        return self is not OrderState.PENDING


class EntryType(str, enum.Enum):
    """台账分录类型。"""

    TOPUP = "积分充值"
    FREEZE = "双冻结"
    DEDUCT = "履约扣减"
    RELEASE = "补偿释放"
    DUPLICATE_IGNORED = "重复回调忽略"


class Reason(str, enum.Enum):
    """每次状态变化的原因。"""

    TOPUP = "积分充值"
    REDEEM = "提交兑换"
    FULFILL = "履约确认"
    PARTIAL_FULFILL = "部分履约"
    PARTIAL_REMAINDER = "部分履约剩余释放"
    USER_CANCEL = "用户取消"
    REVIEW_REJECTED = "审核驳回"
    QUALIFICATION_LOST = "资格变化"
    EXPIRED = "预约超时"
    DUPLICATE_CALLBACK = "重复回调"


@dataclass
class BenefitBatch:
    """权益批次：适用等级、库存、积分价格与预约期限。"""

    batch_id: str
    title: str
    applicable_levels: frozenset[str]
    points_price: int
    reservation_ttl_seconds: int
    total_stock: int
    available_stock: int
    frozen_stock: int = 0
    consumed_stock: int = 0
    is_open: bool = True
    created_at: float = 0.0


@dataclass
class Account:
    """志愿者积分账户：可用余额与冻结额分离。"""

    volunteer_id: str
    level: str
    balance: int
    frozen: int = 0


@dataclass
class RedemptionOrder:
    """兑换单：冻结形成预约，履约确认后正式扣减。"""

    order_id: str
    volunteer_id: str
    batch_id: str
    quantity: int
    unit_price: int
    state: OrderState
    created_at: float
    expires_at: float
    fulfilled_quantity: int = 0
    last_reason: str = ""
    version: int = 0

    @property
    def remaining_quantity(self) -> int:
        return self.quantity - self.fulfilled_quantity

    @property
    def points_amount(self) -> int:
        return self.quantity * self.unit_price


@dataclass(frozen=True)
class LedgerEntry:
    """台账分录：所有积分/库存变动的唯一事实来源，补偿分录保证守恒。"""

    entry_id: str
    seq: int
    entry_type: EntryType
    reason: str
    volunteer_id: str | None
    batch_id: str | None
    order_id: str | None
    order_state_after: str | None
    points_available_delta: int = 0
    points_frozen_delta: int = 0
    stock_available_delta: int = 0
    stock_frozen_delta: int = 0
    stock_consumed_delta: int = 0
    idempotency_key: str | None = None
    created_at: float = 0.0
    detail: str = ""
