"""志愿权益兑换核心服务：双冻结、履约状态机、补偿分录与幂等释放。

不变量（对应 domain/contract.json）：
- 积分库存双冻结：提交兑换时在同一临界区内同时冻结积分与库存，杜绝超卖；
- 兑换履约状态机：待履约 → 已履约 / 部分履约 / 已取消 / 已过期，终态不可逆；
- 补偿分录守恒：取消、部分履约、资格变化、超时均通过补偿分录回冲，台账可重放核对；
- 超时释放幂等：过期释放任务按状态守卫 + 幂等键，可任意重跑、可并发执行。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone

from .errors import (
    BatchClosedError,
    ConservationError,
    InsufficientPointsError,
    InsufficientStockError,
    InvalidStateError,
    LevelNotEligibleError,
    NotFoundError,
    ValidationError,
)
from .models import (
    Account,
    BenefitBatch,
    EntryType,
    LedgerEntry,
    OrderState,
    Reason,
    RedemptionOrder,
)

_CANCEL_REASONS = frozenset({Reason.USER_CANCEL, Reason.REVIEW_REJECTED, Reason.QUALIFICATION_LOST})


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class RedemptionService:
    """志愿权益兑换服务（内存实现，线程安全）。"""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._batches: dict[str, BenefitBatch] = {}
        self._accounts: dict[str, Account] = {}
        self._orders: dict[str, RedemptionOrder] = {}
        self._ledger: list[LedgerEntry] = []
        self._callbacks: dict[str, str] = {}  # 幂等键 -> order_id
        self._entry_seq = 0
        self._order_seq = 0
        self._batch_seq = 0

    # ------------------------------------------------------------------
    # 权益批次
    # ------------------------------------------------------------------
    def create_batch(
        self,
        *,
        title: str,
        applicable_levels: list[str],
        points_price: int,
        reservation_ttl_seconds: int,
        total_stock: int,
        batch_id: str | None = None,
        is_open: bool = True,
    ) -> dict:
        """维护权益批次：适用等级、库存、积分价格、预约期限。"""
        with self._lock:
            if not title:
                raise ValidationError("批次标题不能为空")
            levels = frozenset(applicable_levels or [])
            if not levels:
                raise ValidationError("适用等级不能为空")
            if points_price < 0:
                raise ValidationError("积分价格不能为负")
            if reservation_ttl_seconds < 1:
                raise ValidationError("预约期限必须为正数秒")
            if total_stock < 1:
                raise ValidationError("库存必须为正数")
            if batch_id is None:
                self._batch_seq += 1
                batch_id = f"B{self._batch_seq:04d}"
            if batch_id in self._batches:
                raise ValidationError(f"批次已存在：{batch_id}")
            batch = BenefitBatch(
                batch_id=batch_id,
                title=title,
                applicable_levels=levels,
                points_price=points_price,
                reservation_ttl_seconds=reservation_ttl_seconds,
                total_stock=total_stock,
                available_stock=total_stock,
                is_open=is_open,
                created_at=self._clock(),
            )
            self._batches[batch_id] = batch
            return self._batch_view(batch)

    def set_batch_status(self, batch_id: str, is_open: bool) -> dict:
        with self._lock:
            batch = self._get_batch(batch_id)
            batch.is_open = bool(is_open)
            return self._batch_view(batch)

    def batch_view(self, batch_id: str) -> dict:
        with self._lock:
            return self._batch_view(self._get_batch(batch_id))

    # ------------------------------------------------------------------
    # 志愿者账户
    # ------------------------------------------------------------------
    def create_account(self, volunteer_id: str, level: str, points: int = 0) -> dict:
        with self._lock:
            if not volunteer_id:
                raise ValidationError("志愿者编号不能为空")
            if not level:
                raise ValidationError("等级不能为空")
            if points < 0:
                raise ValidationError("积分不能为负")
            if volunteer_id in self._accounts:
                raise ValidationError(f"账户已存在：{volunteer_id}")
            account = Account(volunteer_id=volunteer_id, level=level, balance=0)
            self._accounts[volunteer_id] = account
            if points:
                self._append_entry(
                    entry_type=EntryType.TOPUP,
                    reason=Reason.TOPUP.value,
                    volunteer_id=volunteer_id,
                    points_available_delta=points,
                    detail=f"开户充值 {points} 积分",
                )
                account.balance += points
            return self._account_view(account)

    def top_up(self, volunteer_id: str, points: int, detail: str = "") -> dict:
        with self._lock:
            if points < 1:
                raise ValidationError("充值积分必须为正数")
            account = self._get_account(volunteer_id)
            account.balance += points
            self._append_entry(
                entry_type=EntryType.TOPUP,
                reason=Reason.TOPUP.value,
                volunteer_id=volunteer_id,
                points_available_delta=points,
                detail=detail or f"充值 {points} 积分",
            )
            return self._account_view(account)

    def change_level(self, volunteer_id: str, new_level: str) -> dict:
        """资格变化：等级调整后，不再适用批次的待履约订单自动取消并补偿释放。"""
        with self._lock:
            if not new_level:
                raise ValidationError("等级不能为空")
            account = self._get_account(volunteer_id)
            old_level = account.level
            account.level = new_level
            auto_cancelled: list[str] = []
            for order in self._orders.values():
                if order.volunteer_id != volunteer_id or order.state is not OrderState.PENDING:
                    continue
                batch = self._get_batch(order.batch_id)
                if new_level not in batch.applicable_levels:
                    order.state = OrderState.CANCELLED
                    order.last_reason = Reason.QUALIFICATION_LOST.value
                    order.version += 1
                    self._release_remaining(
                        order,
                        Reason.QUALIFICATION_LOST.value,
                        f"等级 {old_level} → {new_level}，不再适用批次 {batch.batch_id}",
                    )
                    auto_cancelled.append(order.order_id)
            return {"account": self._account_view(account), "auto_cancelled": auto_cancelled}

    def account_view(self, volunteer_id: str) -> dict:
        """账户视图：可用余额、冻结额、总额。"""
        with self._lock:
            return self._account_view(self._get_account(volunteer_id))

    def account_ledger(self, volunteer_id: str) -> list[dict]:
        """账户台账：每次状态变化及原因。"""
        with self._lock:
            self._get_account(volunteer_id)
            return [self._entry_view(e) for e in self._ledger if e.volunteer_id == volunteer_id]

    # ------------------------------------------------------------------
    # 兑换：提交（双冻结）
    # ------------------------------------------------------------------
    def redeem(self, volunteer_id: str, batch_id: str, quantity: int = 1) -> dict:
        """提交兑换：同一临界区内同时冻结积分与库存。"""
        with self._lock:
            if not isinstance(quantity, int) or quantity < 1:
                raise ValidationError("兑换数量必须为正整数")
            account = self._get_account(volunteer_id)
            batch = self._get_batch(batch_id)
            if not batch.is_open:
                raise BatchClosedError(f"批次未开放：{batch_id}")
            if account.level not in batch.applicable_levels:
                raise LevelNotEligibleError(
                    f"等级 {account.level} 不适用批次 {batch_id}（适用：{'、'.join(sorted(batch.applicable_levels))}）"
                )
            if batch.available_stock < quantity:
                raise InsufficientStockError(f"批次 {batch_id} 库存不足：剩余 {batch.available_stock}")
            cost = quantity * batch.points_price
            if account.balance < cost:
                raise InsufficientPointsError(f"积分不足：需 {cost}，可用 {account.balance}")

            now = self._clock()
            account.balance -= cost
            account.frozen += cost
            batch.available_stock -= quantity
            batch.frozen_stock += quantity
            self._order_seq += 1
            order = RedemptionOrder(
                order_id=f"R{self._order_seq:06d}",
                volunteer_id=volunteer_id,
                batch_id=batch_id,
                quantity=quantity,
                unit_price=batch.points_price,
                state=OrderState.PENDING,
                created_at=now,
                expires_at=now + batch.reservation_ttl_seconds,
                last_reason=Reason.REDEEM.value,
            )
            self._orders[order.order_id] = order
            self._append_entry(
                entry_type=EntryType.FREEZE,
                reason=Reason.REDEEM.value,
                volunteer_id=volunteer_id,
                batch_id=batch_id,
                order_id=order.order_id,
                order_state_after=order.state.value,
                points_available_delta=-cost,
                points_frozen_delta=cost,
                stock_available_delta=-quantity,
                stock_frozen_delta=quantity,
                detail=f"冻结 {quantity} 件库存与 {cost} 积分",
            )
            return self._order_view(order)

    # ------------------------------------------------------------------
    # 兑换：履约确认（正式扣减，重复回调安全）
    # ------------------------------------------------------------------
    def confirm(self, order_id: str, quantity: int | None = None, idempotency_key: str | None = None) -> dict:
        """履约确认：正式扣减冻结额度；支持部分履约与重复回调去重。"""
        with self._lock:
            key = idempotency_key or f"confirm:{order_id}"
            if key in self._callbacks:
                order = self._orders[self._callbacks[key]]
                self._append_entry(
                    entry_type=EntryType.DUPLICATE_IGNORED,
                    reason=Reason.DUPLICATE_CALLBACK.value,
                    volunteer_id=order.volunteer_id,
                    batch_id=order.batch_id,
                    order_id=order.order_id,
                    order_state_after=order.state.value,
                    idempotency_key=key,
                    detail="幂等键已处理，忽略重复回调",
                )
                return {**self._order_view(order), "duplicate": True}

            order = self._get_order(order_id)
            if order.state is not OrderState.PENDING:
                # 迟到的回调（已履约/已取消/已过期）：记录零增量分录，不再变动额度
                self._callbacks[key] = order.order_id
                self._append_entry(
                    entry_type=EntryType.DUPLICATE_IGNORED,
                    reason=Reason.DUPLICATE_CALLBACK.value,
                    volunteer_id=order.volunteer_id,
                    batch_id=order.batch_id,
                    order_id=order.order_id,
                    order_state_after=order.state.value,
                    idempotency_key=key,
                    detail=f"订单已处于终态：{order.state.value}",
                )
                return {**self._order_view(order), "duplicate": True}

            remaining = order.remaining_quantity
            count = remaining if quantity is None else quantity
            if not isinstance(count, int) or count < 1 or count > remaining:
                raise ValidationError(f"履约数量必须在 1 到 {remaining} 之间")

            account = self._get_account(order.volunteer_id)
            batch = self._get_batch(order.batch_id)
            cost = count * order.unit_price
            # 正式扣减：冻结额转为消耗
            account.frozen -= cost
            batch.frozen_stock -= count
            batch.consumed_stock += count
            order.fulfilled_quantity += count
            order.version += 1

            leftover = order.remaining_quantity
            if leftover:
                order.state = OrderState.PARTIALLY_FULFILLED
                order.last_reason = Reason.PARTIAL_FULFILL.value
            else:
                order.state = OrderState.FULFILLED
                order.last_reason = Reason.FULFILL.value

            self._append_entry(
                entry_type=EntryType.DEDUCT,
                reason=order.last_reason,
                volunteer_id=order.volunteer_id,
                batch_id=order.batch_id,
                order_id=order.order_id,
                order_state_after=order.state.value,
                points_frozen_delta=-cost,
                stock_frozen_delta=-count,
                stock_consumed_delta=count,
                idempotency_key=key,
                detail=f"履约 {count} 件，扣减 {cost} 积分",
            )
            if leftover:
                self._release_remaining(
                    order,
                    Reason.PARTIAL_REMAINDER.value,
                    f"部分履约 {count}/{order.quantity} 件，剩余 {leftover} 件补偿释放",
                )
            self._callbacks[key] = order.order_id
            return {**self._order_view(order), "duplicate": False}

    # ------------------------------------------------------------------
    # 兑换：取消（用户取消 / 审核驳回）
    # ------------------------------------------------------------------
    def cancel(self, order_id: str, reason: Reason = Reason.USER_CANCEL, detail: str = "") -> dict:
        with self._lock:
            if isinstance(reason, str):
                reason = Reason(reason)
            if reason not in _CANCEL_REASONS:
                raise ValidationError(f"不支持的取消原因：{reason.value}")
            order = self._get_order(order_id)
            if order.state is OrderState.CANCELLED:
                return {**self._order_view(order), "duplicate": True}
            if order.state is not OrderState.PENDING:
                raise InvalidStateError(f"订单状态为「{order.state.value}」，不能取消")
            order.state = OrderState.CANCELLED
            order.last_reason = reason.value
            order.version += 1
            self._release_remaining(order, reason.value, detail or reason.value)
            return {**self._order_view(order), "duplicate": False}

    # ------------------------------------------------------------------
    # 过期释放（可安全重跑）
    # ------------------------------------------------------------------
    def release_expired(self, now: float | None = None) -> list[str]:
        """释放所有已过预约期限的待履约订单；状态守卫 + 幂等键保证可重跑。"""
        with self._lock:
            moment = self._clock() if now is None else now
            released: list[str] = []
            for order in self._orders.values():
                if order.state is OrderState.PENDING and order.expires_at <= moment:
                    order.state = OrderState.EXPIRED
                    order.last_reason = Reason.EXPIRED.value
                    order.version += 1
                    self._release_remaining(
                        order,
                        Reason.EXPIRED.value,
                        "预约期限已过，释放冻结的积分与库存",
                        idempotency_key=f"expire:{order.order_id}",
                    )
                    released.append(order.order_id)
            return released

    # ------------------------------------------------------------------
    # 查询与守恒校验
    # ------------------------------------------------------------------
    def order_view(self, order_id: str) -> dict:
        with self._lock:
            return self._order_view(self._get_order(order_id))

    def ledger_view(self) -> list[dict]:
        with self._lock:
            return [self._entry_view(e) for e in self._ledger]

    def verify_conservation(self) -> dict:
        """补偿分录守恒校验：从分录重放出的额度必须与实体状态一致。"""
        with self._lock:
            points_avail: dict[str, int] = defaultdict(int)
            points_frozen: dict[str, int] = defaultdict(int)
            stock_avail: dict[str, int] = defaultdict(int)
            stock_frozen: dict[str, int] = defaultdict(int)
            stock_consumed: dict[str, int] = defaultdict(int)
            for entry in self._ledger:
                if entry.volunteer_id:
                    points_avail[entry.volunteer_id] += entry.points_available_delta
                    points_frozen[entry.volunteer_id] += entry.points_frozen_delta
                if entry.batch_id:
                    stock_avail[entry.batch_id] += entry.stock_available_delta
                    stock_frozen[entry.batch_id] += entry.stock_frozen_delta
                    stock_consumed[entry.batch_id] += entry.stock_consumed_delta

            problems: list[str] = []
            for vid, account in self._accounts.items():
                if account.balance != points_avail[vid] or account.frozen != points_frozen[vid]:
                    problems.append(
                        f"账户 {vid} 台账不符：余额 {account.balance}/{points_avail[vid]}，"
                        f"冻结 {account.frozen}/{points_frozen[vid]}"
                    )
                if account.balance < 0 or account.frozen < 0:
                    problems.append(f"账户 {vid} 出现负额")
                expected_frozen = sum(
                    o.remaining_quantity * o.unit_price
                    for o in self._orders.values()
                    if o.volunteer_id == vid and o.state is OrderState.PENDING
                )
                if account.frozen != expected_frozen:
                    problems.append(f"账户 {vid} 冻结额 {account.frozen} 与待履约订单合计 {expected_frozen} 不符")

            for bid, batch in self._batches.items():
                if batch.available_stock != batch.total_stock + stock_avail[bid]:
                    problems.append(f"批次 {bid} 可用库存与台账不符")
                if batch.frozen_stock != stock_frozen[bid] or batch.consumed_stock != stock_consumed[bid]:
                    problems.append(f"批次 {bid} 冻结/消耗库存与台账不符")
                if batch.available_stock + batch.frozen_stock + batch.consumed_stock != batch.total_stock:
                    problems.append(f"批次 {bid} 库存不守恒")
                if min(batch.available_stock, batch.frozen_stock, batch.consumed_stock) < 0:
                    problems.append(f"批次 {bid} 出现负库存")
                expected_frozen = sum(
                    o.remaining_quantity
                    for o in self._orders.values()
                    if o.batch_id == bid and o.state is OrderState.PENDING
                )
                if batch.frozen_stock != expected_frozen:
                    problems.append(f"批次 {bid} 冻结库存 {batch.frozen_stock} 与待履约订单合计 {expected_frozen} 不符")

            if problems:
                raise ConservationError("；".join(problems))
            return {
                "ok": True,
                "accounts": len(self._accounts),
                "batches": len(self._batches),
                "orders": len(self._orders),
                "entries": len(self._ledger),
            }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _get_batch(self, batch_id: str) -> BenefitBatch:
        batch = self._batches.get(batch_id)
        if batch is None:
            raise NotFoundError(f"批次不存在：{batch_id}")
        return batch

    def _get_account(self, volunteer_id: str) -> Account:
        account = self._accounts.get(volunteer_id)
        if account is None:
            raise NotFoundError(f"志愿者账户不存在：{volunteer_id}")
        return account

    def _get_order(self, order_id: str) -> RedemptionOrder:
        order = self._orders.get(order_id)
        if order is None:
            raise NotFoundError(f"兑换单不存在：{order_id}")
        return order

    def _release_remaining(
        self,
        order: RedemptionOrder,
        reason: str,
        detail: str,
        idempotency_key: str | None = None,
    ) -> None:
        """补偿分录：将订单剩余冻结额度完整回冲到可用额。"""
        account = self._get_account(order.volunteer_id)
        batch = self._get_batch(order.batch_id)
        count = order.remaining_quantity
        cost = count * order.unit_price
        account.frozen -= cost
        account.balance += cost
        batch.frozen_stock -= count
        batch.available_stock += count
        self._append_entry(
            entry_type=EntryType.RELEASE,
            reason=reason,
            volunteer_id=order.volunteer_id,
            batch_id=order.batch_id,
            order_id=order.order_id,
            order_state_after=order.state.value,
            points_available_delta=cost,
            points_frozen_delta=-cost,
            stock_available_delta=count,
            stock_frozen_delta=-count,
            idempotency_key=idempotency_key,
            detail=detail,
        )

    def _append_entry(
        self,
        *,
        entry_type: EntryType,
        reason: str,
        volunteer_id: str | None = None,
        batch_id: str | None = None,
        order_id: str | None = None,
        order_state_after: str | None = None,
        points_available_delta: int = 0,
        points_frozen_delta: int = 0,
        stock_available_delta: int = 0,
        stock_frozen_delta: int = 0,
        stock_consumed_delta: int = 0,
        idempotency_key: str | None = None,
        detail: str = "",
    ) -> LedgerEntry:
        self._entry_seq += 1
        entry = LedgerEntry(
            entry_id=f"E{self._entry_seq:06d}",
            seq=self._entry_seq,
            entry_type=entry_type,
            reason=reason,
            volunteer_id=volunteer_id,
            batch_id=batch_id,
            order_id=order_id,
            order_state_after=order_state_after,
            points_available_delta=points_available_delta,
            points_frozen_delta=points_frozen_delta,
            stock_available_delta=stock_available_delta,
            stock_frozen_delta=stock_frozen_delta,
            stock_consumed_delta=stock_consumed_delta,
            idempotency_key=idempotency_key,
            created_at=self._clock(),
            detail=detail,
        )
        self._ledger.append(entry)
        return entry

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------
    @staticmethod
    def _batch_view(batch: BenefitBatch) -> dict:
        return {
            "batch_id": batch.batch_id,
            "title": batch.title,
            "applicable_levels": sorted(batch.applicable_levels),
            "points_price": batch.points_price,
            "reservation_ttl_seconds": batch.reservation_ttl_seconds,
            "total_stock": batch.total_stock,
            "available_stock": batch.available_stock,
            "frozen_stock": batch.frozen_stock,
            "consumed_stock": batch.consumed_stock,
            "is_open": batch.is_open,
            "created_at": batch.created_at,
            "created_at_iso": _iso(batch.created_at),
        }

    @staticmethod
    def _account_view(account: Account) -> dict:
        return {
            "volunteer_id": account.volunteer_id,
            "level": account.level,
            "balance": account.balance,
            "frozen": account.frozen,
            "total_points": account.balance + account.frozen,
        }

    def _order_view(self, order: RedemptionOrder) -> dict:
        entries = [self._entry_view(e) for e in self._ledger if e.order_id == order.order_id]
        return {
            "order_id": order.order_id,
            "volunteer_id": order.volunteer_id,
            "batch_id": order.batch_id,
            "quantity": order.quantity,
            "fulfilled_quantity": order.fulfilled_quantity,
            "remaining_quantity": order.remaining_quantity,
            "unit_price": order.unit_price,
            "points_amount": order.points_amount,
            "state": order.state.value,
            "terminal": order.state.is_terminal,
            "last_reason": order.last_reason,
            "version": order.version,
            "created_at": order.created_at,
            "created_at_iso": _iso(order.created_at),
            "expires_at": order.expires_at,
            "expires_at_iso": _iso(order.expires_at),
            "entries": entries,
        }

    @staticmethod
    def _entry_view(entry: LedgerEntry) -> dict:
        return {
            "entry_id": entry.entry_id,
            "seq": entry.seq,
            "entry_type": entry.entry_type.value,
            "reason": entry.reason,
            "volunteer_id": entry.volunteer_id,
            "batch_id": entry.batch_id,
            "order_id": entry.order_id,
            "order_state_after": entry.order_state_after,
            "points_available_delta": entry.points_available_delta,
            "points_frozen_delta": entry.points_frozen_delta,
            "stock_available_delta": entry.stock_available_delta,
            "stock_frozen_delta": entry.stock_frozen_delta,
            "stock_consumed_delta": entry.stock_consumed_delta,
            "idempotency_key": entry.idempotency_key,
            "created_at": entry.created_at,
            "created_at_iso": _iso(entry.created_at),
            "detail": entry.detail,
        }
