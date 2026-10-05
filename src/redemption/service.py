"""志愿权益兑换核心服务。

不变量（对应 domain/contract.json 的 invariants）：
- 积分库存双冻结：提交兑换在同一事务内冻结积分与库存，任一侧不足整体回滚。
- 兑换履约状态机：冻结中 -> 已履约/部分履约/已取消/已驳回/已过期，终态不可逆。
- 补偿分录守恒：每个订单每个维度满足 冻结 = 扣减 + 释放。
- 超时释放幂等：过期任务按状态守卫更新，可并发、可重跑。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from .models import (
    BATCH_FLOW,
    REDEEMABLE_BATCH_STATUSES,
    BatchStatus,
    Dimension,
    EntryKind,
    OrderStatus,
)
from .store import Store


class DomainError(Exception):
    """业务规则冲突，携带稳定错误码供接口映射。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _check_int(field: str, value: object, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DomainError("INVALID_FIELD", f"字段 {field} 必须是不小于 {minimum} 的整数")


class RedemptionService:
    """权益批次、兑换订单、积分账户与补偿分录的核心服务。"""

    def __init__(self, store: Store | None = None, clock: Callable[[], float] | None = None) -> None:
        self.store = store or Store()
        self.clock = clock or time.time

    # ------------------------------------------------------------------
    # 权益批次
    # ------------------------------------------------------------------
    def create_batch(
        self,
        *,
        name: str,
        required_level: int,
        total_stock: int,
        points_price: int,
        reservation_ttl_seconds: int,
        actor: str = "system",
    ) -> dict:
        if not isinstance(name, str) or not name.strip():
            raise DomainError("INVALID_FIELD", "批次名称不能为空")
        _check_int("required_level", required_level, 0)
        _check_int("total_stock", total_stock, 0)
        _check_int("points_price", points_price, 0)
        _check_int("reservation_ttl_seconds", reservation_ttl_seconds, 1)
        batch_id = _new_id("B")
        now = self.clock()
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO benefit_batch (batch_id,name,required_level,total_stock,available_stock,"
                "frozen_stock,points_price,reservation_ttl_seconds,status,created_at,updated_at)"
                " VALUES (?,?,?,?,?,0,?,?,?,?,?)",
                (
                    batch_id,
                    name.strip(),
                    required_level,
                    total_stock,
                    total_stock,
                    points_price,
                    reservation_ttl_seconds,
                    BatchStatus.DRAFT,
                    now,
                    now,
                ),
            )
            self._transition(conn, "BATCH", batch_id, None, BatchStatus.DRAFT, "创建权益批次", actor, now)
        return self.get_batch(batch_id)

    def transition_batch(self, batch_id: str, to_status: str, *, reason: str, actor: str = "system") -> dict:
        try:
            target = BatchStatus(to_status)
        except ValueError:
            raise DomainError("INVALID_TRANSITION", f"未知批次状态：{to_status}") from None
        if not reason:
            raise DomainError("REASON_REQUIRED", "状态变更必须填写原因")
        now = self.clock()
        with self.store.transaction() as conn:
            batch = self._get_batch_row(conn, batch_id)
            current = BatchStatus(batch["status"])
            if target not in BATCH_FLOW.get(current, set()):
                raise DomainError("INVALID_TRANSITION", f"批次状态不能从「{current}」变为「{target}」")
            if target == BatchStatus.ARCHIVED and batch["frozen_stock"] > 0:
                raise DomainError("BATCH_HAS_FROZEN_STOCK", "仍有冻结库存未结算，不能归档")
            conn.execute("UPDATE benefit_batch SET status=?, updated_at=? WHERE batch_id=?", (target, now, batch_id))
            self._transition(conn, "BATCH", batch_id, current, target, reason, actor, now)
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> dict:
        with self.store.transaction() as conn:
            return self._batch_view(conn, batch_id)

    def list_batches(self) -> list[dict]:
        with self.store.transaction() as conn:
            rows = conn.execute("SELECT batch_id FROM benefit_batch ORDER BY created_at, batch_id").fetchall()
            return [self._batch_view(conn, row["batch_id"], with_transitions=False) for row in rows]

    # ------------------------------------------------------------------
    # 志愿者账户
    # ------------------------------------------------------------------
    def create_volunteer(self, volunteer_id: str, *, level: int = 0, points: int = 0) -> dict:
        if not isinstance(volunteer_id, str) or not volunteer_id.strip():
            raise DomainError("INVALID_FIELD", "volunteer_id 不能为空")
        volunteer_id = volunteer_id.strip()
        _check_int("level", level, 0)
        _check_int("points", points, 0)
        now = self.clock()
        with self.store.transaction() as conn:
            try:
                conn.execute(
                    "INSERT INTO volunteer_account (volunteer_id,level,points_balance,points_frozen,created_at,updated_at)"
                    " VALUES (?,?,?,0,?,?)",
                    (volunteer_id, level, points, now, now),
                )
            except sqlite3.IntegrityError:
                raise DomainError("VOLUNTEER_EXISTS", f"志愿者账户已存在：{volunteer_id}") from None
            self._transition(conn, "ACCOUNT", volunteer_id, None, "开户", f"开立账户：等级{level}，初始积分{points}", "system", now)
        return self.get_account(volunteer_id)

    def adjust_points(self, volunteer_id: str, delta: int, *, reason: str = "运营调整", actor: str = "system") -> dict:
        if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0:
            raise DomainError("INVALID_FIELD", "delta 必须是非零整数")
        now = self.clock()
        with self.store.transaction() as conn:
            self._get_account_row(conn, volunteer_id)
            if delta > 0:
                conn.execute(
                    "UPDATE volunteer_account SET points_balance=points_balance+?, updated_at=? WHERE volunteer_id=?",
                    (delta, now, volunteer_id),
                )
            else:
                self._guard(
                    conn.execute(
                        "UPDATE volunteer_account SET points_balance=points_balance+?, updated_at=?"
                        " WHERE volunteer_id=? AND points_balance>=?",
                        (delta, now, volunteer_id, -delta),
                    ),
                    "INSUFFICIENT_POINTS",
                    "积分余额不足，无法扣减",
                )
            direction = "增加" if delta > 0 else "扣减"
            self._transition(conn, "ACCOUNT", volunteer_id, None, "积分变动", f"{reason}（{direction}{abs(delta)}分）", actor, now)
        return self.get_account(volunteer_id)

    def change_level(self, volunteer_id: str, new_level: int, *, reason: str, actor: str = "system") -> dict:
        """调整等级；等级低于批次要求时，补偿释放其冻结中的订单。"""
        _check_int("new_level", new_level, 0)
        if not reason:
            raise DomainError("REASON_REQUIRED", "等级变更必须填写原因")
        now = self.clock()
        with self.store.transaction() as conn:
            acct = self._get_account_row(conn, volunteer_id)
            old_level = acct["level"]
            conn.execute("UPDATE volunteer_account SET level=?, updated_at=? WHERE volunteer_id=?", (new_level, now, volunteer_id))
            self._transition(conn, "ACCOUNT", volunteer_id, f"等级{old_level}", f"等级{new_level}", reason, actor, now)
        with self.store.transaction() as conn:
            rows = conn.execute(
                "SELECT o.order_id, b.required_level FROM redemption_order o"
                " JOIN benefit_batch b ON b.batch_id = o.batch_id"
                " WHERE o.volunteer_id=? AND o.status=? AND b.required_level>?",
                (volunteer_id, OrderStatus.RESERVED, new_level),
            ).fetchall()
        cancelled = []
        for row in rows:
            try:
                self._release_all(
                    row["order_id"],
                    OrderStatus.CANCELLED,
                    reason=f"资格变化：等级{old_level}→{new_level}（{reason}），低于批次要求等级{row['required_level']}，补偿释放",
                    actor=actor,
                    action="eligibility-cancel",
                )
                cancelled.append(row["order_id"])
            except DomainError as exc:
                if exc.code != "ORDER_NOT_RESERVED":
                    raise
        return {
            "volunteer_id": volunteer_id,
            "old_level": old_level,
            "new_level": new_level,
            "cancelled_order_ids": cancelled,
        }

    def get_account(self, volunteer_id: str) -> dict:
        with self.store.transaction() as conn:
            acct = self._get_account_row(conn, volunteer_id)
            return {
                "volunteer_id": acct["volunteer_id"],
                "level": acct["level"],
                "points_balance": acct["points_balance"],
                "points_frozen": acct["points_frozen"],
                "points_total": acct["points_balance"] + acct["points_frozen"],
                "recent_events": self._transitions(conn, "ACCOUNT", volunteer_id)[-20:],
            }

    # ------------------------------------------------------------------
    # 兑换：提交（双冻结）
    # ------------------------------------------------------------------
    def submit_redemption(
        self,
        *,
        volunteer_id: str,
        batch_id: str,
        quantity: int,
        idempotency_key: str,
        actor: str | None = None,
    ) -> dict:
        _check_int("quantity", quantity, 1)
        if not idempotency_key:
            raise DomainError("INVALID_FIELD", "idempotency_key 不能为空")
        actor = actor or f"volunteer:{volunteer_id}"
        now = self.clock()
        with self.store.transaction() as conn:
            dup = conn.execute(
                "SELECT order_id FROM redemption_order WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if dup:
                view = self._order_view(conn, dup["order_id"])
                view["idempotent_replay"] = True
                return view
            batch = self._get_batch_row(conn, batch_id)
            if BatchStatus(batch["status"]) not in REDEEMABLE_BATCH_STATUSES:
                raise DomainError("BATCH_NOT_OPEN", f"批次当前状态为「{batch['status']}」，未开放兑换")
            acct = self._get_account_row(conn, volunteer_id)
            if acct["level"] < batch["required_level"]:
                raise DomainError(
                    "LEVEL_TOO_LOW", f"志愿者等级{acct['level']}低于批次要求等级{batch['required_level']}"
                )
            amount = quantity * batch["points_price"]
            # 双冻结：同事务、条件更新，任一侧不足整体回滚，杜绝超卖与超额冻结
            self._guard(
                conn.execute(
                    "UPDATE benefit_batch SET available_stock=available_stock-?, frozen_stock=frozen_stock+?,"
                    " updated_at=? WHERE batch_id=? AND available_stock>=?",
                    (quantity, quantity, now, batch_id, quantity),
                ),
                "INSUFFICIENT_STOCK",
                "权益库存不足",
            )
            self._guard(
                conn.execute(
                    "UPDATE volunteer_account SET points_balance=points_balance-?, points_frozen=points_frozen+?,"
                    " updated_at=? WHERE volunteer_id=? AND points_balance>=?",
                    (amount, amount, now, volunteer_id, amount),
                ),
                "INSUFFICIENT_POINTS",
                "积分余额不足",
            )
            order_id = _new_id("R")
            expire_at = now + batch["reservation_ttl_seconds"]
            conn.execute(
                "INSERT INTO redemption_order (order_id,idempotency_key,volunteer_id,batch_id,quantity,"
                "points_amount,confirmed_quantity,status,created_at,expire_at,closed_at)"
                " VALUES (?,?,?,?,?,?,0,?,?,?,NULL)",
                (order_id, idempotency_key, volunteer_id, batch_id, quantity, amount, OrderStatus.RESERVED, now, expire_at),
            )
            self._ledger(conn, order_id, Dimension.POINTS, EntryKind.FREEZE, amount, "提交兑换，冻结积分", now)
            self._ledger(conn, order_id, Dimension.STOCK, EntryKind.FREEZE, quantity, "提交兑换，冻结库存", now)
            self._transition(
                conn,
                "ORDER",
                order_id,
                None,
                OrderStatus.RESERVED,
                f"提交兑换：冻结积分{amount}、库存{quantity}，预约期限{batch['reservation_ttl_seconds']}秒",
                actor,
                now,
            )
            view = self._order_view(conn, order_id)
            view["idempotent_replay"] = False
            return view

    # ------------------------------------------------------------------
    # 兑换：结算（履约 / 取消 / 驳回）
    # ------------------------------------------------------------------
    def confirm_redemption(
        self,
        order_id: str,
        *,
        confirmed_quantity: int,
        reason: str | None = None,
        actor: str = "system",
        callback_id: str | None = None,
    ) -> dict:
        """履约确认：确认部分正式扣减，剩余部分补偿释放。过期任务未跑前迟到确认仍受理，先到先得。"""
        now = self.clock()
        with self.store.transaction() as conn:
            if callback_id is not None:
                hit = self._callback_hit(conn, callback_id)
                if hit:
                    return hit
            order = self._get_order_row(conn, order_id)
            self._require_reserved(order)
            quantity = order["quantity"]
            _check_int("confirmed_quantity", confirmed_quantity, 0)
            if confirmed_quantity > quantity:
                raise DomainError("INVALID_QUANTITY", f"履约数量不能超过预约数量{quantity}")
            unit_price = order["points_amount"] // quantity
            points_deduct = unit_price * confirmed_quantity
            points_release = order["points_amount"] - points_deduct
            stock_release = quantity - confirmed_quantity
            self._deduct_frozen(conn, order, points=points_deduct, stock=confirmed_quantity, reason="履约确认，正式扣减", now=now)
            if stock_release:
                release_reason = "部分履约补偿，释放剩余冻结额度" if confirmed_quantity else "履约数量为零，全额释放冻结额度"
                self._release_frozen(conn, order, points=points_release, stock=stock_release, reason=release_reason, now=now)
            if stock_release == 0:
                new_status = OrderStatus.FULFILLED
                default_reason = f"履约确认：{confirmed_quantity}件全部交付，扣减积分{points_deduct}、库存{confirmed_quantity}"
            elif confirmed_quantity == 0:
                new_status = OrderStatus.CANCELLED
                default_reason = "履约数量为零，全额释放冻结积分与库存"
            else:
                new_status = OrderStatus.PARTIAL
                default_reason = (
                    f"部分履约：交付{confirmed_quantity}件扣减积分{points_deduct}，"
                    f"剩余{stock_release}件补偿释放积分{points_release}"
                )
            self._close_order(conn, order_id, new_status, confirmed_quantity, now)
            self._transition(conn, "ORDER", order_id, OrderStatus.RESERVED, new_status, reason or default_reason, actor, now)
            view = self._order_view(conn, order_id)
            if callback_id is not None:
                self._record_callback(conn, callback_id, order_id, "confirm", view, now)
            return view

    def cancel_redemption(
        self,
        order_id: str,
        *,
        reason: str | None = None,
        actor: str | None = None,
        callback_id: str | None = None,
    ) -> dict:
        return self._release_all(
            order_id, OrderStatus.CANCELLED, reason=reason or "用户取消预约", actor=actor, action="cancel", callback_id=callback_id
        )

    def reject_redemption(
        self,
        order_id: str,
        *,
        reason: str,
        actor: str = "auditor",
        callback_id: str | None = None,
    ) -> dict:
        if not reason:
            raise DomainError("REASON_REQUIRED", "审核驳回必须填写原因")
        return self._release_all(
            order_id, OrderStatus.REJECTED, reason=f"审核驳回：{reason}", actor=actor, action="reject", callback_id=callback_id
        )

    def _release_all(
        self,
        order_id: str,
        to_status: OrderStatus,
        *,
        reason: str,
        actor: str | None,
        action: str,
        callback_id: str | None = None,
    ) -> dict:
        """终态释放：冻结的积分与库存全部补偿回滚。"""
        now = self.clock()
        with self.store.transaction() as conn:
            if callback_id is not None:
                hit = self._callback_hit(conn, callback_id)
                if hit:
                    return hit
            order = self._get_order_row(conn, order_id)
            self._require_reserved(order)
            actor = actor or (f"volunteer:{order['volunteer_id']}" if action == "cancel" else "system")
            self._release_frozen(conn, order, points=order["points_amount"], stock=order["quantity"], reason=reason, now=now)
            self._close_order(conn, order_id, to_status, 0, now)
            self._transition(conn, "ORDER", order_id, OrderStatus.RESERVED, to_status, reason, actor, now)
            view = self._order_view(conn, order_id)
            if callback_id is not None:
                self._record_callback(conn, callback_id, order_id, action, view, now)
            return view

    # ------------------------------------------------------------------
    # 超时释放任务（可安全重跑）
    # ------------------------------------------------------------------
    def release_expired(self, *, now: float | None = None, actor: str = "system:expiry-task") -> dict:
        """释放所有已过期未履约的预约。每个订单独立事务 + 状态守卫，重跑/并发均为幂等。"""
        now = self.clock() if now is None else now
        with self.store.transaction() as conn:
            rows = conn.execute(
                "SELECT order_id FROM redemption_order WHERE status=? AND expire_at<=? ORDER BY expire_at, order_id",
                (OrderStatus.RESERVED, now),
            ).fetchall()
        released = []
        for row in rows:
            try:
                self._release_all(
                    row["order_id"],
                    OrderStatus.EXPIRED,
                    reason="预约超期未履约，到期自动释放冻结积分与库存",
                    actor=actor,
                    action="expire",
                )
                released.append(row["order_id"])
            except DomainError as exc:
                # 并发跑批或迟到确认已结算该订单：跳过即可
                if exc.code != "ORDER_NOT_RESERVED":
                    raise
        return {"released_order_ids": released, "released_count": len(released), "ran_at": _iso(now)}

    # ------------------------------------------------------------------
    # 查询与守恒校验
    # ------------------------------------------------------------------
    def get_order(self, order_id: str) -> dict:
        with self.store.transaction() as conn:
            return self._order_view(conn, order_id)

    def verify_conservation(self, order_id: str | None = None) -> dict:
        """分录守恒校验：终态订单 冻结 = 扣减 + 释放；冻结中订单不得有任何扣减/释放。"""
        with self.store.transaction() as conn:
            if order_id is not None:
                self._get_order_row(conn, order_id)
                order_rows = conn.execute(
                    "SELECT order_id,status FROM redemption_order WHERE order_id=?", (order_id,)
                ).fetchall()
            else:
                order_rows = conn.execute("SELECT order_id,status FROM redemption_order").fetchall()
            status_by_order = {row["order_id"]: row["status"] for row in order_rows}
            if order_rows:
                placeholders = ",".join("?" * len(order_rows))
                ledger_rows = conn.execute(
                    f"SELECT order_id,dimension,kind,amount FROM ledger_entry WHERE order_id IN ({placeholders})",
                    tuple(status_by_order),
                ).fetchall()
            else:
                ledger_rows = []
        orders: dict[str, dict[str, dict]] = {oid: {} for oid in status_by_order}
        for row in ledger_rows:
            dims = orders.setdefault(row["order_id"], {})
            bucket = dims.setdefault(row["dimension"], {"frozen": 0, "deducted": 0, "released": 0})
            if row["kind"] == EntryKind.FREEZE:
                bucket["frozen"] += row["amount"]
            elif row["kind"] == EntryKind.DEDUCT:
                bucket["deducted"] += row["amount"]
            else:
                bucket["released"] += row["amount"]
        ok = True
        for oid, dims in orders.items():
            reserved = status_by_order[oid] == OrderStatus.RESERVED
            for bucket in dims.values():
                outstanding = bucket["frozen"] - bucket["deducted"] - bucket["released"]
                bucket["outstanding"] = outstanding
                bucket["balanced"] = outstanding == bucket["frozen"] if reserved else outstanding == 0
                ok = ok and bucket["balanced"]
        return {"ok": ok, "orders": orders}

    # ------------------------------------------------------------------
    # 内部：行读取 / 守卫 / 分录 / 状态流转
    # ------------------------------------------------------------------
    @staticmethod
    def _guard(cursor: sqlite3.Cursor, code: str, message: str) -> None:
        if cursor.rowcount != 1:
            raise DomainError(code, message)

    def _get_batch_row(self, conn: sqlite3.Connection, batch_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM benefit_batch WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise DomainError("BATCH_NOT_FOUND", f"权益批次不存在：{batch_id}")
        return row

    def _get_account_row(self, conn: sqlite3.Connection, volunteer_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM volunteer_account WHERE volunteer_id=?", (volunteer_id,)).fetchone()
        if row is None:
            raise DomainError("VOLUNTEER_NOT_FOUND", f"志愿者账户不存在：{volunteer_id}")
        return row

    def _get_order_row(self, conn: sqlite3.Connection, order_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM redemption_order WHERE order_id=?", (order_id,)).fetchone()
        if row is None:
            raise DomainError("ORDER_NOT_FOUND", f"兑换订单不存在：{order_id}")
        return row

    @staticmethod
    def _require_reserved(order: sqlite3.Row) -> None:
        if order["status"] != OrderStatus.RESERVED:
            raise DomainError("ORDER_NOT_RESERVED", f"订单当前状态为「{order['status']}」，仅冻结中的订单可结算")

    def _close_order(self, conn: sqlite3.Connection, order_id: str, to_status: OrderStatus, confirmed: int, now: float) -> None:
        # 状态守卫更新：并发/重跑时仅第一个写者生效
        self._guard(
            conn.execute(
                "UPDATE redemption_order SET status=?, confirmed_quantity=?, closed_at=?"
                " WHERE order_id=? AND status=?",
                (to_status, confirmed, now, order_id, OrderStatus.RESERVED),
            ),
            "ORDER_NOT_RESERVED",
            "订单已被并发结算，本次操作无效",
        )

    def _deduct_frozen(self, conn, order, *, points: int, stock: int, reason: str, now: float) -> None:
        """履约确认：冻结额正式扣减（积分销账、库存出库）。"""
        if points > 0:
            self._guard(
                conn.execute(
                    "UPDATE volunteer_account SET points_frozen=points_frozen-?, updated_at=?"
                    " WHERE volunteer_id=? AND points_frozen>=?",
                    (points, now, order["volunteer_id"], points),
                ),
                "ACCOUNT_FROZEN_MISMATCH",
                "账户冻结积分不足，数据不一致",
            )
            self._ledger(conn, order["order_id"], Dimension.POINTS, EntryKind.DEDUCT, points, reason, now)
        if stock > 0:
            self._guard(
                conn.execute(
                    "UPDATE benefit_batch SET frozen_stock=frozen_stock-?, updated_at=?"
                    " WHERE batch_id=? AND frozen_stock>=?",
                    (stock, now, order["batch_id"], stock),
                ),
                "BATCH_FROZEN_MISMATCH",
                "批次冻结库存不足，数据不一致",
            )
            self._ledger(conn, order["order_id"], Dimension.STOCK, EntryKind.DEDUCT, stock, reason, now)

    def _release_frozen(self, conn, order, *, points: int, stock: int, reason: str, now: float) -> None:
        """补偿分录：冻结额回滚为可用（积分回余额、库存回可兑）。"""
        if points > 0:
            self._guard(
                conn.execute(
                    "UPDATE volunteer_account SET points_frozen=points_frozen-?, points_balance=points_balance+?,"
                    " updated_at=? WHERE volunteer_id=? AND points_frozen>=?",
                    (points, points, now, order["volunteer_id"], points),
                ),
                "ACCOUNT_FROZEN_MISMATCH",
                "账户冻结积分不足，数据不一致",
            )
            self._ledger(conn, order["order_id"], Dimension.POINTS, EntryKind.RELEASE, points, reason, now)
        if stock > 0:
            self._guard(
                conn.execute(
                    "UPDATE benefit_batch SET frozen_stock=frozen_stock-?, available_stock=available_stock+?,"
                    " updated_at=? WHERE batch_id=? AND frozen_stock>=?",
                    (stock, stock, now, order["batch_id"], stock),
                ),
                "BATCH_FROZEN_MISMATCH",
                "批次冻结库存不足，数据不一致",
            )
            self._ledger(conn, order["order_id"], Dimension.STOCK, EntryKind.RELEASE, stock, reason, now)

    def _ledger(self, conn, order_id: str, dimension: Dimension, kind: EntryKind, amount: int, reason: str, now: float) -> None:
        if amount <= 0:
            return
        conn.execute(
            "INSERT INTO ledger_entry (order_id,dimension,kind,amount,reason,created_at) VALUES (?,?,?,?,?,?)",
            (order_id, dimension, kind, amount, reason, now),
        )

    def _transition(self, conn, subject_type: str, subject_id: str, from_status, to_status, reason: str, actor: str, now: float) -> None:
        conn.execute(
            "INSERT INTO state_transition (subject_type,subject_id,from_status,to_status,reason,actor,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (subject_type, subject_id, from_status, to_status, reason, actor, now),
        )

    def _callback_hit(self, conn, callback_id: str) -> dict | None:
        row = conn.execute("SELECT result_json FROM processed_callback WHERE callback_id=?", (callback_id,)).fetchone()
        if row is None:
            return None
        result = json.loads(row["result_json"])
        result["duplicate_callback"] = True
        return result

    def _record_callback(self, conn, callback_id: str, order_id: str, action: str, result: dict, now: float) -> None:
        conn.execute(
            "INSERT INTO processed_callback (callback_id,order_id,action,result_json,created_at) VALUES (?,?,?,?,?)",
            (callback_id, order_id, action, json.dumps(result, ensure_ascii=False), now),
        )

    def _transitions(self, conn, subject_type: str, subject_id: str) -> list[dict]:
        rows = conn.execute(
            "SELECT from_status,to_status,reason,actor,created_at FROM state_transition"
            " WHERE subject_type=? AND subject_id=? ORDER BY transition_id",
            (subject_type, subject_id),
        ).fetchall()
        return [
            {"from": r["from_status"], "to": r["to_status"], "reason": r["reason"], "actor": r["actor"], "at": _iso(r["created_at"])}
            for r in rows
        ]

    def _batch_view(self, conn, batch_id: str, *, with_transitions: bool = True) -> dict:
        batch = self._get_batch_row(conn, batch_id)
        view = {
            "batch_id": batch["batch_id"],
            "name": batch["name"],
            "required_level": batch["required_level"],
            "total_stock": batch["total_stock"],
            "available_stock": batch["available_stock"],
            "frozen_stock": batch["frozen_stock"],
            "deducted_stock": batch["total_stock"] - batch["available_stock"] - batch["frozen_stock"],
            "points_price": batch["points_price"],
            "reservation_ttl_seconds": batch["reservation_ttl_seconds"],
            "status": batch["status"],
            "created_at": _iso(batch["created_at"]),
            "updated_at": _iso(batch["updated_at"]),
        }
        if with_transitions:
            view["transitions"] = self._transitions(conn, "BATCH", batch_id)
        return view

    def _order_view(self, conn, order_id: str) -> dict:
        order = self._get_order_row(conn, order_id)
        ledger_rows = conn.execute(
            "SELECT dimension,kind,amount,reason,created_at FROM ledger_entry WHERE order_id=? ORDER BY entry_id",
            (order_id,),
        ).fetchall()
        return {
            "order_id": order["order_id"],
            "idempotency_key": order["idempotency_key"],
            "volunteer_id": order["volunteer_id"],
            "batch_id": order["batch_id"],
            "quantity": order["quantity"],
            "points_amount": order["points_amount"],
            "confirmed_quantity": order["confirmed_quantity"],
            "status": order["status"],
            "created_at": _iso(order["created_at"]),
            "expire_at": _iso(order["expire_at"]),
            "closed_at": _iso(order["closed_at"]) if order["closed_at"] is not None else None,
            "transitions": self._transitions(conn, "ORDER", order_id),
            "ledger": [
                {"dimension": r["dimension"], "kind": r["kind"], "amount": r["amount"], "reason": r["reason"], "at": _iso(r["created_at"])}
                for r in ledger_rows
            ],
        }
