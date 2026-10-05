"""志愿权益兑换 HTTP 接口冒烟测试。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from redemption.api import create_server  # noqa: E402
from redemption.service import RedemptionService  # noqa: E402


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.clock = FakeClock()
        cls.service = RedemptionService(clock=cls.clock)
        cls.server = create_server("127.0.0.1", 0, cls.service)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _req(self, method: str, path: str, payload: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base + path, data=body, method=method)
        request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _make_batch(self, batch_id: str, stock: int = 3, price: int = 120, ttl: int = 600) -> None:
        status, _ = self._req("POST", "/batches", {
            "batch_id": batch_id,
            "title": f"批次{batch_id}",
            "applicable_levels": ["gold"],
            "points_price": price,
            "reservation_ttl_seconds": ttl,
            "total_stock": stock,
        })
        self.assertEqual(status, 201)

    def _make_account(self, volunteer_id: str, points: int = 500) -> None:
        status, _ = self._req("POST", "/accounts", {
            "volunteer_id": volunteer_id, "level": "gold", "points": points,
        })
        self.assertEqual(status, 201)

    def test_full_redemption_journey(self) -> None:
        status, health = self._req("GET", "/health")
        self.assertEqual((status, health["status"]), (200, "ok"))

        self._make_batch("B-API")
        self._make_account("V-API")

        # 提交兑换：双冻结
        status, order = self._req("POST", "/redemptions", {
            "volunteer_id": "V-API", "batch_id": "B-API", "quantity": 2,
        })
        self.assertEqual(status, 201)
        self.assertEqual(order["state"], "待履约")
        order_id = order["order_id"]

        # 接口展示余额与冻结额
        status, account = self._req("GET", "/accounts/V-API")
        self.assertEqual((account["balance"], account["frozen"], account["total_points"]), (260, 240, 500))

        # 部分履约
        status, confirmed = self._req("POST", f"/redemptions/{order_id}/confirm", {"quantity": 1})
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["state"], "部分履约")

        # 重复回调：同一幂等键不再扣减
        status, dup = self._req(
            "POST", f"/redemptions/{order_id}/confirm", {"quantity": 1},
            headers={"Idempotency-Key": "cb-1"},
        )
        self.assertTrue(dup["duplicate"])
        status, dup2 = self._req(
            "POST", f"/redemptions/{order_id}/confirm", {"quantity": 1},
            headers={"Idempotency-Key": "cb-1"},
        )
        self.assertTrue(dup2["duplicate"])
        status, account = self._req("GET", "/accounts/V-API")
        self.assertEqual((account["balance"], account["frozen"]), (380, 0))

        # 台账展示每次状态变化的原因
        status, ledger = self._req("GET", "/accounts/V-API/ledger")
        reasons = [e["reason"] for e in ledger["entries"]]
        self.assertIn("提交兑换", reasons)
        self.assertIn("部分履约", reasons)
        self.assertIn("部分履约剩余释放", reasons)
        self.assertIn("重复回调", reasons)

        status, report = self._req("GET", "/admin/conservation")
        self.assertTrue(report["ok"])

    def test_expiry_release_endpoint_is_rerunnable(self) -> None:
        self._make_batch("B-EXP", stock=2, price=100, ttl=60)
        self._make_account("V-EXP", points=300)
        _, order = self._req("POST", "/redemptions", {"volunteer_id": "V-EXP", "batch_id": "B-EXP"})

        self.clock.advance(120)
        status, first = self._req("POST", "/admin/release-expired", {})
        self.assertEqual(first["released"], [order["order_id"]])
        status, second = self._req("POST", "/admin/release-expired", {})
        self.assertEqual(second["released"], [])

        _, account = self._req("GET", "/accounts/V-EXP")
        self.assertEqual((account["balance"], account["frozen"]), (300, 0))
        _, view = self._req("GET", f"/redemptions/{order['order_id']}")
        self.assertEqual(view["state"], "已过期")
        self.assertEqual(view["last_reason"], "预约超时")

    def test_qualification_change_endpoint(self) -> None:
        self._make_batch("B-LVL", stock=2, price=100)
        self._make_account("V-LVL", points=300)
        _, order = self._req("POST", "/redemptions", {"volunteer_id": "V-LVL", "batch_id": "B-LVL"})
        status, result = self._req("POST", "/accounts/V-LVL/level", {"level": "bronze"})
        self.assertEqual(status, 200)
        self.assertEqual(result["auto_cancelled"], [order["order_id"]])
        _, account = self._req("GET", "/accounts/V-LVL")
        self.assertEqual((account["balance"], account["frozen"]), (300, 0))

    def test_error_shapes(self) -> None:
        self._make_batch("B-ERR", stock=1)
        self._make_account("V-ERR")

        status, err = self._req("GET", "/accounts/V-NOPE")
        self.assertEqual((status, err["error"]), (404, "not_found"))
        status, err = self._req("POST", "/redemptions", {"volunteer_id": "V-ERR"})
        self.assertEqual((status, err["error"]), (400, "validation_error"))
        status, err = self._req("POST", "/redemptions", {
            "volunteer_id": "V-ERR", "batch_id": "B-ERR", "quantity": 99,
        })
        self.assertEqual((status, err["error"]), (409, "insufficient_stock"))
        status, err = self._req("GET", "/no-such-route")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
