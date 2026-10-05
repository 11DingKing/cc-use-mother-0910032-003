"""志愿权益兑换 HTTP 接口（纯标准库实现）。

接口一览：
- GET  /health                        健康检查
- POST /batches                       维护权益批次（等级/库存/价格/预约期限）
- GET  /batches/{id}                  批次详情（可用/冻结/消耗库存）
- POST /batches/{id}/status           开放或关闭批次 {"is_open": false}
- POST /accounts                      开户 {"volunteer_id", "level", "points"}
- GET  /accounts/{id}                 账户余额与冻结额
- GET  /accounts/{id}/ledger          每次状态变化的分录与原因
- POST /accounts/{id}/topup           充值 {"points"}
- POST /accounts/{id}/level           资格变化 {"level"}，自动取消失格订单
- POST /redemptions                   提交兑换（双冻结）
- GET  /redemptions/{id}              兑换单详情与分录轨迹
- POST /redemptions/{id}/confirm      履约确认（可带 Idempotency-Key，可部分履约）
- POST /redemptions/{id}/cancel       取消 {"reason": "user_cancel"|"review_rejected"}
- POST /admin/release-expired         过期释放任务（可安全重跑）
- GET  /admin/conservation            补偿分录守恒校验
- GET  /ledger                        全量台账
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import DomainError, ValidationError
from .models import Reason
from .service import RedemptionService

logger = logging.getLogger("redemption.api")

_CANCEL_REASON_MAP = {
    "user_cancel": Reason.USER_CANCEL,
    "review_rejected": Reason.REVIEW_REJECTED,
}


def _make_handler(service: RedemptionService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "RedemptionAPI/1.0"

        # ---------------- 基础工具 ----------------
        def log_message(self, fmt: str, *args: object) -> None:
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, payload: object) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValidationError(f"请求体不是合法 JSON：{exc}") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是 JSON 对象")
            return value

        @staticmethod
        def _require(body: dict, *fields: str) -> None:
            missing = [f for f in fields if f not in body]
            if missing:
                raise ValidationError("缺少字段：" + "、".join(missing))

        # ---------------- 路由 ----------------
        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            try:
                for route_method, pattern, handler_name in _ROUTES:
                    if route_method != method:
                        continue
                    match = pattern.fullmatch(path)
                    if match:
                        body = self._body() if method == "POST" else {}
                        status, payload = getattr(self, handler_name)(body, **match.groupdict())
                        self._send(status, payload)
                        return
                self._send(404, {"error": "not_found", "message": f"路由不存在：{method} {path}"})
            except DomainError as exc:
                self._send(exc.http_status, {"error": exc.code, "message": str(exc)})
            except Exception:  # pragma: no cover - 兜底
                logger.exception("未处理异常")
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        # ---------------- 各端点 ----------------
        def _health(self, body: dict) -> tuple[int, object]:
            return 200, {"status": "ok", "service": "志愿权益兑换"}

        def _create_batch(self, body: dict) -> tuple[int, object]:
            self._require(body, "title", "applicable_levels", "points_price", "reservation_ttl_seconds", "total_stock")
            return 201, service.create_batch(
                title=body["title"],
                applicable_levels=body["applicable_levels"],
                points_price=body["points_price"],
                reservation_ttl_seconds=body["reservation_ttl_seconds"],
                total_stock=body["total_stock"],
                batch_id=body.get("batch_id"),
                is_open=body.get("is_open", True),
            )

        def _get_batch(self, body: dict, batch_id: str) -> tuple[int, object]:
            return 200, service.batch_view(batch_id)

        def _set_batch_status(self, body: dict, batch_id: str) -> tuple[int, object]:
            self._require(body, "is_open")
            return 200, service.set_batch_status(batch_id, body["is_open"])

        def _create_account(self, body: dict) -> tuple[int, object]:
            self._require(body, "volunteer_id", "level")
            return 201, service.create_account(body["volunteer_id"], body["level"], body.get("points", 0))

        def _get_account(self, body: dict, volunteer_id: str) -> tuple[int, object]:
            return 200, service.account_view(volunteer_id)

        def _get_account_ledger(self, body: dict, volunteer_id: str) -> tuple[int, object]:
            return 200, {"volunteer_id": volunteer_id, "entries": service.account_ledger(volunteer_id)}

        def _top_up(self, body: dict, volunteer_id: str) -> tuple[int, object]:
            self._require(body, "points")
            return 200, service.top_up(volunteer_id, body["points"], body.get("detail", ""))

        def _change_level(self, body: dict, volunteer_id: str) -> tuple[int, object]:
            self._require(body, "level")
            return 200, service.change_level(volunteer_id, body["level"])

        def _redeem(self, body: dict) -> tuple[int, object]:
            self._require(body, "volunteer_id", "batch_id")
            return 201, service.redeem(body["volunteer_id"], body["batch_id"], body.get("quantity", 1))

        def _get_order(self, body: dict, order_id: str) -> tuple[int, object]:
            return 200, service.order_view(order_id)

        def _confirm(self, body: dict, order_id: str) -> tuple[int, object]:
            return 200, service.confirm(
                order_id,
                quantity=body.get("quantity"),
                idempotency_key=self.headers.get("Idempotency-Key") or body.get("idempotency_key"),
            )

        def _cancel(self, body: dict, order_id: str) -> tuple[int, object]:
            reason_name = body.get("reason", "user_cancel")
            if reason_name not in _CANCEL_REASON_MAP:
                raise ValidationError(f"不支持的取消原因：{reason_name}（可选：{', '.join(_CANCEL_REASON_MAP)}）")
            return 200, service.cancel(order_id, _CANCEL_REASON_MAP[reason_name], body.get("detail", ""))

        def _release_expired(self, body: dict) -> tuple[int, object]:
            released = service.release_expired(body.get("now"))
            return 200, {"released": released, "count": len(released), "rerun_safe": True}

        def _conservation(self, body: dict) -> tuple[int, object]:
            return 200, service.verify_conservation()

        def _ledger(self, body: dict) -> tuple[int, object]:
            return 200, {"entries": service.ledger_view()}

    return Handler


_ROUTES = [
    ("GET", re.compile(r"/health"), "_health"),
    ("POST", re.compile(r"/batches"), "_create_batch"),
    ("GET", re.compile(r"/batches/(?P<batch_id>[^/]+)"), "_get_batch"),
    ("POST", re.compile(r"/batches/(?P<batch_id>[^/]+)/status"), "_set_batch_status"),
    ("POST", re.compile(r"/accounts"), "_create_account"),
    ("GET", re.compile(r"/accounts/(?P<volunteer_id>[^/]+)"), "_get_account"),
    ("GET", re.compile(r"/accounts/(?P<volunteer_id>[^/]+)/ledger"), "_get_account_ledger"),
    ("POST", re.compile(r"/accounts/(?P<volunteer_id>[^/]+)/topup"), "_top_up"),
    ("POST", re.compile(r"/accounts/(?P<volunteer_id>[^/]+)/level"), "_change_level"),
    ("POST", re.compile(r"/redemptions"), "_redeem"),
    ("GET", re.compile(r"/redemptions/(?P<order_id>[^/]+)"), "_get_order"),
    ("POST", re.compile(r"/redemptions/(?P<order_id>[^/]+)/confirm"), "_confirm"),
    ("POST", re.compile(r"/redemptions/(?P<order_id>[^/]+)/cancel"), "_cancel"),
    ("POST", re.compile(r"/admin/release-expired"), "_release_expired"),
    ("GET", re.compile(r"/admin/conservation"), "_conservation"),
    ("GET", re.compile(r"/ledger"), "_ledger"),
]


def create_server(host: str, port: int, service: RedemptionService | None = None) -> ThreadingHTTPServer:
    """构建线程安全 HTTP 服务。"""
    handler = _make_handler(service or RedemptionService())
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="志愿权益兑换后端服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    server = create_server(args.host, args.port)
    print(f"志愿权益兑换服务已启动：http://{args.host}:{args.port}（Ctrl+C 停止）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
