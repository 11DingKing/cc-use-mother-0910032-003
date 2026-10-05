"""志愿权益兑换核心服务测试：双冻结、状态机、补偿分录、幂等释放。"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from redemption import (  # noqa: E402
    BatchClosedError,
    InsufficientPointsError,
    InsufficientStockError,
    InvalidStateError,
    LevelNotEligibleError,
    NotFoundError,
    RedemptionService,
    Reason,
    ValidationError,
)


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.service = RedemptionService(clock=self.clock)
        self.service.create_batch(
            batch_id="B-MUSEUM",
            title="文博中心讲解权益",
            applicable_levels=["silver", "gold"],
            points_price=100,
            reservation_ttl_seconds=3600,
            total_stock=10,
        )
        self.service.create_account("V-001", "gold", points=1000)
        self.service.create_account("V-002", "silver", points=500)
        self.service.create_account("V-003", "bronze", points=2000)


class FreezeTest(ServiceTestBase):
    def test_redeem_freezes_points_and_stock_atomically(self) -> None:
        order = self.service.redeem("V-001", "B-MUSEUM", 2)
        self.assertEqual(order["state"], "待履约")
        self.assertEqual(order["points_amount"], 200)
        self.assertEqual(order["expires_at"], self.clock.t + 3600)

        account = self.service.account_view("V-001")
        self.assertEqual((account["balance"], account["frozen"]), (800, 200))
        batch = self.service.batch_view("B-MUSEUM")
        self.assertEqual(
            (batch["available_stock"], batch["frozen_stock"], batch["consumed_stock"]),
            (8, 2, 0),
        )
        freeze_entries = [e for e in order["entries"] if e["entry_type"] == "双冻结"]
        self.assertEqual(len(freeze_entries), 1)
        self.assertEqual(freeze_entries[0]["reason"], "提交兑换")
        self.service.verify_conservation()

    def test_redeem_rejects_when_stock_short(self) -> None:
        with self.assertRaises(InsufficientStockError):
            self.service.redeem("V-001", "B-MUSEUM", 11)
        self.assertEqual(self.service.account_view("V-001")["frozen"], 0)

    def test_redeem_rejects_when_points_short(self) -> None:
        self.service.create_account("V-POOR", "gold", points=50)
        with self.assertRaises(InsufficientPointsError):
            self.service.redeem("V-POOR", "B-MUSEUM", 1)
        self.assertEqual(self.service.batch_view("B-MUSEUM")["frozen_stock"], 0)

    def test_redeem_rejects_ineligible_level(self) -> None:
        with self.assertRaises(LevelNotEligibleError):
            self.service.redeem("V-003", "B-MUSEUM", 1)

    def test_redeem_rejects_closed_batch_and_unknown_ids(self) -> None:
        self.service.set_batch_status("B-MUSEUM", False)
        with self.assertRaises(BatchClosedError):
            self.service.redeem("V-001", "B-MUSEUM", 1)
        with self.assertRaises(NotFoundError):
            self.service.redeem("V-001", "B-NOPE", 1)
        with self.assertRaises(NotFoundError):
            self.service.redeem("V-NOPE", "B-MUSEUM", 1)
        with self.assertRaises(ValidationError):
            self.service.redeem("V-001", "B-MUSEUM", 0)


class ConfirmTest(ServiceTestBase):
    def test_full_confirm_deducts_formally(self) -> None:
        order = self.service.redeem("V-001", "B-MUSEUM", 2)
        result = self.service.confirm(order["order_id"])
        self.assertFalse(result["duplicate"])
        self.assertEqual(result["state"], "已履约")

        account = self.service.account_view("V-001")
        self.assertEqual((account["balance"], account["frozen"]), (800, 0))
        batch = self.service.batch_view("B-MUSEUM")
        self.assertEqual(
            (batch["available_stock"], batch["frozen_stock"], batch["consumed_stock"]),
            (8, 0, 2),
        )
        types = [e["entry_type"] for e in result["entries"]]
        self.assertEqual(types, ["双冻结", "履约扣减"])
        self.service.verify_conservation()

    def test_partial_confirm_releases_remainder_via_compensation(self) -> None:
        order = self.service.redeem("V-001", "B-MUSEUM", 4)
        result = self.service.confirm(order["order_id"], quantity=1)
        self.assertEqual(result["state"], "部分履约")
        self.assertEqual(result["fulfilled_quantity"], 1)

        account = self.service.account_view("V-001")
        self.assertEqual((account["balance"], account["frozen"]), (900, 0))
        batch = self.service.batch_view("B-MUSEUM")
        self.assertEqual(
            (batch["available_stock"], batch["frozen_stock"], batch["consumed_stock"]),
            (9, 0, 1),
        )
        release = [e for e in result["entries"] if e["entry_type"] == "补偿释放"]
        self.assertEqual(len(release), 1)
        self.assertEqual(release[0]["reason"], "部分履约剩余释放")
        self.assertEqual(release[0]["points_available_delta"], 300)
        self.service.verify_conservation()

    def test_duplicate_confirm_same_idempotency_key_deducts_once(self) -> None:
        order = self.service.redeem("V-001", "B-MUSEUM", 2)
        first = self.service.confirm(order["order_id"], idempotency_key="cb-123")
        second = self.service.confirm(order["order_id"], idempotency_key="cb-123")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])

        account = self.service.account_view("V-001")
        self.assertEqual((account["balance"], account["frozen"]), (800, 0))
        batch = self.service.batch_view("B-MUSEUM")
        self.assertEqual(batch["consumed_stock"], 2)
        ignored = [e for e in second["entries"] if e["entry_type"] == "重复回调忽略"]
        self.assertEqual(len(ignored), 1)
        self.assertEqual(ignored[0]["reason"], "重复回调")
        self.service.verify_conservation()

    def test_late_confirm_on_terminal_order_is_ignored(self) -> None:
        order = self.service.redeem("V-001", "B-MUSEUM", 2)
        self.service.cancel(order["order_id"], Reason.USER_CANCEL)
        result = self.service.confirm(order["order_id"], idempotency_key="cb-late")
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["state"], "已取消")
        self.assertEqual(self.service.batch_view("B-MUSEUM")["consumed_stock"], 0)
        self.assertEqual(self.service.account_view("V-001")["balance"], 1000)
        self.service.verify_conservation()

    def test_confirm_validates_quantity(self) -> None:
        order = self.service.redeem("V-001", "B-MUSEUM", 2)
        with self.assertRaises(ValidationError):
            self.service.confirm(order["order_id"], quantity=3)
        with self.assertRaises(NotFoundError):
            self.service.confirm("R-NOPE")


class CancelTest(ServiceTestBase):
    def test_user_cancel_releases_freeze(self) -> None:
        order = self.service.redeem("V-001", "B-MUSEUM", 3)
        result = self.service.cancel(order["order_id"], Reason.USER_CANCEL)
        self.assertEqual(result["state"], "已取消")
        self.assertEqual(self.service.account_view("V-001")["balance"], 1000)
        self.assertEqual(self.service.batch_view("B-MUSEUM")["available_stock"], 10)
        release = [e for e in result["entries"] if e["entry_type"] == "补偿释放"]
        self.assertEqual(release[0]["reason"], "用户取消")
        self.service.verify_conservation()

    def test_review_reject_releases_with_reason(self) -> None:
        order = self.service.redeem("V-002", "B-MUSEUM", 1)
        result = self.service.cancel(order["order_id"], Reason.REVIEW_REJECTED)
        release = [e for e in result["entries"] if e["entry_type"] == "补偿释放"]
        self.assertEqual(release[0]["reason"], "审核驳回")
        self.assertEqual(self.service.account_view("V-002")["frozen"], 0)
        self.service.verify_conservation()

    def test_cancel_is_idempotent_for_cancelled_order(self) -> None:
        order = self.service.redeem("V-001", "B-MUSEUM", 1)
        self.service.cancel(order["order_id"])
        again = self.service.cancel(order["order_id"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(self.service.account_view("V-001")["balance"], 1000)

    def test_cancel_rejects_fulfilled_order(self) -> None:
        order = self.service.redeem("V-001", "B-MUSEUM", 1)
        self.service.confirm(order["order_id"])
        with self.assertRaises(InvalidStateError):
            self.service.cancel(order["order_id"])


class QualificationTest(ServiceTestBase):
    def test_level_change_auto_cancels_ineligible_orders(self) -> None:
        order = self.service.redeem("V-001", "B-MUSEUM", 2)
        result = self.service.change_level("V-001", "bronze")
        self.assertEqual(result["auto_cancelled"], [order["order_id"]])
        self.assertEqual(result["account"]["level"], "bronze")

        view = self.service.order_view(order["order_id"])
        self.assertEqual(view["state"], "已取消")
        self.assertEqual(view["last_reason"], "资格变化")
        self.assertEqual(self.service.account_view("V-001")["balance"], 1000)
        self.assertEqual(self.service.batch_view("B-MUSEUM")["frozen_stock"], 0)
        self.service.verify_conservation()

    def test_level_change_keeps_eligible_orders(self) -> None:
        order = self.service.redeem("V-001", "B-MUSEUM", 2)
        result = self.service.change_level("V-001", "silver")
        self.assertEqual(result["auto_cancelled"], [])
        self.assertEqual(self.service.order_view(order["order_id"])["state"], "待履约")
        self.service.verify_conservation()


class ExpiryTest(ServiceTestBase):
    def test_expired_orders_are_released(self) -> None:
        order = self.service.redeem("V-001", "B-MUSEUM", 2)
        self.clock.advance(3601)
        released = self.service.release_expired()
        self.assertEqual(released, [order["order_id"]])

        view = self.service.order_view(order["order_id"])
        self.assertEqual(view["state"], "已过期")
        release = [e for e in view["entries"] if e["entry_type"] == "补偿释放"]
        self.assertEqual(release[0]["reason"], "预约超时")
        self.assertEqual(release[0]["idempotency_key"], f"expire:{order['order_id']}")
        self.assertEqual(self.service.account_view("V-001")["balance"], 1000)
        self.assertEqual(self.service.batch_view("B-MUSEUM")["available_stock"], 10)
        self.service.verify_conservation()

    def test_sweeper_is_safe_to_rerun(self) -> None:
        order = self.service.redeem("V-001", "B-MUSEUM", 2)
        self.clock.advance(7200)
        first = self.service.release_expired()
        second = self.service.release_expired()
        third = self.service.release_expired(self.clock.t + 10_000)
        self.assertEqual(first, [order["order_id"]])
        self.assertEqual(second, [])
        self.assertEqual(third, [])
        self.assertEqual(self.service.account_view("V-001")["balance"], 1000)
        entries = self.service.account_ledger("V-001")
        self.assertEqual(len([e for e in entries if e["entry_type"] == "补偿释放"]), 1)
        self.service.verify_conservation()

    def test_unexpired_orders_survive_sweeper(self) -> None:
        order = self.service.redeem("V-001", "B-MUSEUM", 1)
        self.clock.advance(1800)
        self.assertEqual(self.service.release_expired(), [])
        self.assertEqual(self.service.order_view(order["order_id"])["state"], "待履约")

    def test_confirm_after_expiry_is_ignored_without_double_effect(self) -> None:
        order = self.service.redeem("V-001", "B-MUSEUM", 2)
        self.clock.advance(3601)
        self.service.release_expired()
        result = self.service.confirm(order["order_id"], idempotency_key="cb-too-late")
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["state"], "已过期")
        self.assertEqual(self.service.batch_view("B-MUSEUM")["consumed_stock"], 0)
        self.assertEqual(self.service.account_view("V-001")["balance"], 1000)
        self.service.verify_conservation()


class ConcurrencyTest(ServiceTestBase):
    def test_no_oversell_when_volunteers_race(self) -> None:
        barrier = threading.Barrier(20)
        outcomes: list[str] = []
        lock = threading.Lock()

        def attempt(idx: int) -> None:
            vid = f"V-RACE-{idx:02d}"
            self.service.create_account(vid, "gold", points=100)
            barrier.wait()
            try:
                self.service.redeem(vid, "B-MUSEUM", 1)
                outcome = "ok"
            except InsufficientStockError:
                outcome = "stock"
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(outcomes.count("ok"), 10)
        self.assertEqual(outcomes.count("stock"), 10)
        batch = self.service.batch_view("B-MUSEUM")
        self.assertEqual((batch["available_stock"], batch["frozen_stock"]), (0, 10))
        self.service.verify_conservation()

    def test_no_points_overdraw_when_racing(self) -> None:
        self.service.create_batch(
            batch_id="B-BIG",
            title="大额库存批次",
            applicable_levels=["gold"],
            points_price=100,
            reservation_ttl_seconds=3600,
            total_stock=100,
        )
        barrier = threading.Barrier(8)
        outcomes: list[str] = []
        lock = threading.Lock()

        def attempt(_: int) -> None:
            barrier.wait()
            try:
                self.service.redeem("V-001", "B-BIG", 1)
                outcome = "ok"
            except InsufficientPointsError:
                outcome = "points"
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # V-001 有 1000 积分，单价 100，最多成功 10 笔；8 线程全部成功且不超支
        self.assertEqual(outcomes.count("ok"), 8)
        account = self.service.account_view("V-001")
        self.assertEqual((account["balance"], account["frozen"]), (200, 800))
        self.service.verify_conservation()


class ConservationTest(ServiceTestBase):
    def test_mixed_flow_keeps_ledger_consistent(self) -> None:
        o1 = self.service.redeem("V-001", "B-MUSEUM", 3)
        o2 = self.service.redeem("V-002", "B-MUSEUM", 2)
        o3 = self.service.redeem("V-001", "B-MUSEUM", 1)
        self.service.confirm(o1["order_id"], quantity=1)          # 部分履约
        self.service.cancel(o2["order_id"], Reason.REVIEW_REJECTED)  # 审核驳回
        self.service.top_up("V-002", 50)
        self.service.change_level("V-001", "bronze")                 # 资格变化取消 o3
        self.clock.advance(3601)
        self.service.release_expired()
        self.service.release_expired()                               # 重跑安全

        report = self.service.verify_conservation()
        self.assertTrue(report["ok"])

        account = self.service.account_view("V-001")
        # 1000 - 100（履约扣减）= 900；o3 的 100 已补偿释放
        self.assertEqual((account["balance"], account["frozen"]), (900, 0))
        batch = self.service.batch_view("B-MUSEUM")
        self.assertEqual(
            (batch["available_stock"], batch["frozen_stock"], batch["consumed_stock"]),
            (9, 0, 1),
        )
        # 台账完整记录每次状态变化的原因
        reasons = [e["reason"] for e in self.service.account_ledger("V-001")]
        self.assertIn("提交兑换", reasons)
        self.assertIn("部分履约", reasons)
        self.assertIn("部分履约剩余释放", reasons)
        self.assertIn("资格变化", reasons)


if __name__ == "__main__":
    unittest.main()
