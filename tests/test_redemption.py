"""志愿权益兑换后端回归测试：双冻结、履约结算、补偿分录、幂等与并发。"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from redemption import DomainError, RedemptionService
from redemption.api import serve


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [1_000_000.0]
        self.service = RedemptionService(clock=lambda: self.now[0])
        self.service.create_volunteer("v1", level=3, points=1000)
        self.service.create_volunteer("v2", level=1, points=50)
        batch = self.service.create_batch(
            name="博物馆讲解器借用",
            required_level=2,
            total_stock=5,
            points_price=30,
            reservation_ttl_seconds=600,
            actor="ops",
        )
        self.bid = batch["batch_id"]
        self.service.transition_batch(self.bid, "待核验", reason="提交场馆负责人核验", actor="ops")
        self.service.transition_batch(self.bid, "已确认", reason="核验通过，开放兑换", actor="场馆负责人")

    def submit(self, volunteer="v1", qty=1, key="k1", batch=None):
        return self.service.submit_redemption(
            volunteer_id=volunteer, batch_id=batch or self.bid, quantity=qty, idempotency_key=key
        )

    def assertConservation(self, order_id=None):
        report = self.service.verify_conservation(order_id)
        self.assertTrue(report["ok"], f"分录不守恒：{report}")
        return report


class FreezeTest(ServiceTestCase):
    def test_submit_freezes_points_and_stock(self) -> None:
        view = self.submit(qty=2)
        self.assertEqual(view["status"], "冻结中")
        self.assertEqual(view["points_amount"], 60)
        self.assertFalse(view["idempotent_replay"])

        account = self.service.get_account("v1")
        self.assertEqual(account["points_balance"], 940)
        self.assertEqual(account["points_frozen"], 60)
        self.assertEqual(account["points_total"], 1000)

        batch = self.service.get_batch(self.bid)
        self.assertEqual(batch["available_stock"], 3)
        self.assertEqual(batch["frozen_stock"], 2)
        self.assertEqual(batch["deducted_stock"], 0)

        kinds = {(e["dimension"], e["kind"]): e["amount"] for e in view["ledger"]}
        self.assertEqual(kinds, {("积分", "冻结"): 60, ("库存", "冻结"): 2})
        self.assertEqual(view["transitions"][0]["from"], None)
        self.assertEqual(view["transitions"][0]["to"], "冻结中")
        self.assertIn("冻结", view["transitions"][0]["reason"])

    def test_insufficient_points_rolls_back_stock(self) -> None:
        self.service.create_volunteer("v3", level=3, points=50)  # 等级够、积分不够
        self.submit(qty=1, key="k1")  # v1 冻结 1 件
        with self.assertRaises(DomainError) as ctx:
            self.service.submit_redemption(volunteer_id="v3", batch_id=self.bid, quantity=2, idempotency_key="k2")
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_POINTS")
        # 库存未被积分失败的订单占用
        self.assertEqual(self.service.get_batch(self.bid)["available_stock"], 4)
        self.assertEqual(self.service.get_account("v3")["points_frozen"], 0)

    def test_level_too_low(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            self.submit(volunteer="v2", key="k3")
        self.assertEqual(ctx.exception.code, "LEVEL_TOO_LOW")

    def test_batch_not_open(self) -> None:
        draft = self.service.create_batch(
            name="未开放批次", required_level=0, total_stock=3, points_price=10, reservation_ttl_seconds=60
        )
        with self.assertRaises(DomainError) as ctx:
            self.submit(key="k4", batch=draft["batch_id"])
        self.assertEqual(ctx.exception.code, "BATCH_NOT_OPEN")

    def test_idempotency_key_replays_without_double_freeze(self) -> None:
        first = self.submit(qty=1, key="dup")
        second = self.submit(qty=1, key="dup")
        self.assertEqual(first["order_id"], second["order_id"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(self.service.get_account("v1")["points_frozen"], 30)
        self.assertEqual(self.service.get_batch(self.bid)["frozen_stock"], 1)


class SettlementTest(ServiceTestCase):
    def test_confirm_full_deducts_formally(self) -> None:
        order = self.submit(qty=2)
        view = self.service.confirm_redemption(order["order_id"], confirmed_quantity=2, actor="场馆负责人")
        self.assertEqual(view["status"], "已履约")
        self.assertEqual(view["confirmed_quantity"], 2)
        account = self.service.get_account("v1")
        self.assertEqual(account["points_balance"], 940)
        self.assertEqual(account["points_frozen"], 0)
        batch = self.service.get_batch(self.bid)
        self.assertEqual(batch["frozen_stock"], 0)
        self.assertEqual(batch["deducted_stock"], 2)
        self.assertConservation(order["order_id"])

    def test_partial_confirm_releases_remainder_via_compensation(self) -> None:
        order = self.submit(qty=4)
        view = self.service.confirm_redemption(order["order_id"], confirmed_quantity=1)
        self.assertEqual(view["status"], "部分履约")
        account = self.service.get_account("v1")
        self.assertEqual(account["points_balance"], 970)  # 1000 - 120 + 90
        self.assertEqual(account["points_frozen"], 0)
        batch = self.service.get_batch(self.bid)
        self.assertEqual(batch["available_stock"], 4)  # 释放 3 件回池
        self.assertEqual(batch["frozen_stock"], 0)
        self.assertEqual(batch["deducted_stock"], 1)
        release_entries = [e for e in view["ledger"] if e["kind"] == "释放"]
        self.assertEqual(len(release_entries), 2)
        self.assertTrue(all("补偿" in e["reason"] for e in release_entries))
        self.assertConservation(order["order_id"])

    def test_confirm_zero_releases_everything(self) -> None:
        order = self.submit(qty=1)
        view = self.service.confirm_redemption(order["order_id"], confirmed_quantity=0)
        self.assertEqual(view["status"], "已取消")
        self.assertEqual(self.service.get_account("v1")["points_balance"], 1000)
        self.assertEqual(self.service.get_batch(self.bid)["available_stock"], 5)
        self.assertConservation(order["order_id"])

    def test_cancel_releases_everything(self) -> None:
        order = self.submit(qty=2)
        view = self.service.cancel_redemption(order["order_id"], reason="行程变更")
        self.assertEqual(view["status"], "已取消")
        self.assertEqual(self.service.get_account("v1")["points_balance"], 1000)
        self.assertEqual(self.service.get_batch(self.bid)["available_stock"], 5)
        self.assertIn("行程变更", view["transitions"][-1]["reason"])
        self.assertConservation(order["order_id"])

    def test_reject_requires_reason_and_releases(self) -> None:
        order = self.submit(qty=1)
        with self.assertRaises(DomainError) as ctx:
            self.service.reject_redemption(order["order_id"], reason="")
        self.assertEqual(ctx.exception.code, "REASON_REQUIRED")
        view = self.service.reject_redemption(order["order_id"], reason="重复提交", actor="审核员A")
        self.assertEqual(view["status"], "已驳回")
        self.assertIn("审核驳回：重复提交", view["transitions"][-1]["reason"])
        self.assertEqual(self.service.get_account("v1")["points_balance"], 1000)
        self.assertConservation(order["order_id"])

    def test_settled_order_cannot_be_settled_again(self) -> None:
        order = self.submit(qty=1)
        self.service.cancel_redemption(order["order_id"], reason="取消")
        with self.assertRaises(DomainError) as ctx:
            self.service.confirm_redemption(order["order_id"], confirmed_quantity=1)
        self.assertEqual(ctx.exception.code, "ORDER_NOT_RESERVED")

    def test_duplicate_callback_returns_first_result_without_double_effect(self) -> None:
        order = self.submit(qty=2)
        first = self.service.confirm_redemption(order["order_id"], confirmed_quantity=2, callback_id="cb-1")
        second = self.service.confirm_redemption(order["order_id"], confirmed_quantity=2, callback_id="cb-1")
        self.assertTrue(second["duplicate_callback"])
        self.assertEqual(first["order_id"], second["order_id"])
        self.assertEqual(self.service.get_account("v1")["points_balance"], 940)  # 只扣一次
        self.assertConservation(order["order_id"])

    def test_duplicate_callback_on_cancel(self) -> None:
        order = self.submit(qty=1)
        self.service.cancel_redemption(order["order_id"], reason="取消", callback_id="cb-2")
        again = self.service.cancel_redemption(order["order_id"], reason="取消", callback_id="cb-2")
        self.assertTrue(again["duplicate_callback"])
        self.assertEqual(self.service.get_account("v1")["points_balance"], 1000)


class ExpiryTest(ServiceTestCase):
    def test_expired_reservation_is_released(self) -> None:
        order = self.submit(qty=2)
        self.now[0] += 601
        result = self.service.release_expired()
        self.assertEqual(result["released_order_ids"], [order["order_id"]])
        view = self.service.get_order(order["order_id"])
        self.assertEqual(view["status"], "已过期")
        self.assertIn("超期", view["transitions"][-1]["reason"])
        self.assertEqual(self.service.get_account("v1")["points_balance"], 1000)
        self.assertEqual(self.service.get_batch(self.bid)["available_stock"], 5)
        self.assertConservation(order["order_id"])

    def test_release_task_is_safely_rerunnable(self) -> None:
        self.submit(qty=1, key="k1")
        self.submit(qty=1, key="k2")
        self.now[0] += 601
        first = self.service.release_expired()
        self.assertEqual(first["released_count"], 2)
        second = self.service.release_expired()
        self.assertEqual(second["released_count"], 0)
        self.assertTrue(self.service.verify_conservation()["ok"])

    def test_concurrent_release_runs_release_exactly_once(self) -> None:
        order = self.submit(qty=1)
        self.now[0] += 601
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.service.release_expired())) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        total = sum(r["released_count"] for r in results)
        self.assertEqual(total, 1)
        self.assertEqual(self.service.get_order(order["order_id"])["status"], "已过期")
        self.assertEqual(self.service.get_account("v1")["points_balance"], 1000)

    def test_unexpired_reservation_is_untouched(self) -> None:
        self.submit(qty=1)
        self.now[0] += 599
        self.assertEqual(self.service.release_expired()["released_count"], 0)


class EligibilityTest(ServiceTestCase):
    def test_level_drop_cancels_only_ineligible_reservations(self) -> None:
        low_batch = self.service.create_batch(
            name="低门槛权益", required_level=1, total_stock=2, points_price=10, reservation_ttl_seconds=600
        )
        for status in ("待核验", "已确认"):
            self.service.transition_batch(low_batch["batch_id"], status, reason="推进")
        high_order = self.submit(qty=1, key="high")  # 要求等级 2
        low_order = self.submit(qty=1, key="low", batch=low_batch["batch_id"])  # 要求等级 1

        result = self.service.change_level("v1", 1, reason="年度复核降级", actor="ops")
        self.assertEqual(result["cancelled_order_ids"], [high_order["order_id"]])

        high_view = self.service.get_order(high_order["order_id"])
        self.assertEqual(high_view["status"], "已取消")
        self.assertIn("资格变化", high_view["transitions"][-1]["reason"])
        self.assertEqual(self.service.get_order(low_order["order_id"])["status"], "冻结中")

        account = self.service.get_account("v1")
        self.assertEqual(account["points_balance"], 990)  # 高门槛订单 30 分已释放
        self.assertEqual(account["points_frozen"], 10)
        self.assertConservation(high_order["order_id"])


class ConcurrencyTest(ServiceTestCase):
    def test_no_oversell_under_concurrent_redemptions(self) -> None:
        successes, failures = [], []

        def worker(i: int) -> None:
            try:
                self.service.submit_redemption(
                    volunteer_id="v1", batch_id=self.bid, quantity=1, idempotency_key=f"c{i}"
                )
                successes.append(i)
            except DomainError as exc:
                failures.append(exc.code)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(successes), 5)
        self.assertEqual(failures, ["INSUFFICIENT_STOCK"] * 15)
        batch = self.service.get_batch(self.bid)
        self.assertEqual(batch["available_stock"], 0)
        self.assertEqual(batch["frozen_stock"], 5)
        self.assertTrue(self.service.verify_conservation()["ok"])


class BatchLifecycleTest(ServiceTestCase):
    def test_invalid_transition_rejected(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            self.service.transition_batch(self.bid, "已归档", reason="跳过执行中")
        self.assertEqual(ctx.exception.code, "INVALID_TRANSITION")

    def test_transition_requires_reason(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            self.service.transition_batch(self.bid, "执行中", reason="")
        self.assertEqual(ctx.exception.code, "REASON_REQUIRED")

    def test_archive_blocked_while_stock_frozen(self) -> None:
        self.service.transition_batch(self.bid, "执行中", reason="排期开始")
        order = self.submit(qty=1)
        with self.assertRaises(DomainError) as ctx:
            self.service.transition_batch(self.bid, "已归档", reason="活动结束")
        self.assertEqual(ctx.exception.code, "BATCH_HAS_FROZEN_STOCK")
        self.service.confirm_redemption(order["order_id"], confirmed_quantity=1)
        archived = self.service.transition_batch(self.bid, "已归档", reason="活动结束")
        self.assertEqual(archived["status"], "已归档")
        reasons = [t["reason"] for t in archived["transitions"]]
        self.assertEqual(len(reasons), len(archived["transitions"]))
        self.assertTrue(all(reasons))


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = serve(RedemptionService(), port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def request(self, method: str, path: str, payload: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        body = json.dumps(payload) if payload is not None else None
        conn.request(method, path, body, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def test_full_flow_over_http(self) -> None:
        status, _ = self.request("GET", "/health")
        self.assertEqual(status, 200)

        status, _ = self.request("POST", "/volunteers", {"volunteer_id": "v9", "level": 2, "points": 200})
        self.assertEqual(status, 201)

        status, batch = self.request(
            "POST",
            "/batches",
            {"name": "文创徽章", "required_level": 1, "total_stock": 2, "points_price": 10, "reservation_ttl_seconds": 60},
        )
        self.assertEqual(status, 201)
        bid = batch["batch_id"]
        for target in ("待核验", "已确认"):
            status, _ = self.request("POST", f"/batches/{bid}/transition", {"to_status": target, "reason": "推进"})
            self.assertEqual(status, 200)

        status, order = self.request(
            "POST", "/redemptions", {"volunteer_id": "v9", "batch_id": bid, "quantity": 1, "idempotency_key": "api-1"}
        )
        self.assertEqual(status, 201)
        oid = order["order_id"]

        status, account = self.request("GET", "/volunteers/v9/account")
        self.assertEqual((status, account["points_balance"], account["points_frozen"]), (200, 190, 10))

        status, confirmed = self.request(
            "POST", f"/redemptions/{oid}/confirm", {"confirmed_quantity": 1, "callback_id": "cb-api-1"}
        )
        self.assertEqual((status, confirmed["status"]), (200, "已履约"))

        status, duplicate = self.request(
            "POST", f"/redemptions/{oid}/confirm", {"confirmed_quantity": 1, "callback_id": "cb-api-1"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(duplicate["duplicate_callback"])

        status, account = self.request("GET", "/volunteers/v9/account")
        self.assertEqual((account["points_balance"], account["points_frozen"]), (190, 0))

        status, conservation = self.request("GET", f"/redemptions/{oid}/conservation")
        self.assertTrue(conservation["ok"])

        status, detail = self.request("GET", f"/redemptions/{oid}")
        self.assertTrue(all(t["reason"] for t in detail["transitions"]))

    def test_unknown_route_and_domain_error_mapping(self) -> None:
        status, body = self.request("GET", "/nope")
        self.assertEqual((status, body["error"]["code"]), (404, "NOT_FOUND"))
        status, body = self.request("GET", "/redemptions/R-missing")
        self.assertEqual((status, body["error"]["code"]), (404, "ORDER_NOT_FOUND"))
        status, body = self.request("POST", "/redemptions", {"volunteer_id": "v9"})
        self.assertEqual((status, body["error"]["code"]), (400, "MISSING_FIELD"))


if __name__ == "__main__":
    unittest.main()
