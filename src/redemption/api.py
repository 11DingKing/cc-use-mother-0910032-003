"""志愿权益兑换 HTTP 接口（标准库实现，JSON 入出）。"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .service import DomainError, RedemptionService

ERROR_STATUS = {
    "NOT_FOUND": 404,
    "BATCH_NOT_FOUND": 404,
    "ORDER_NOT_FOUND": 404,
    "VOLUNTEER_NOT_FOUND": 404,
    "MISSING_FIELD": 400,
    "INVALID_FIELD": 400,
    "INVALID_QUANTITY": 400,
    "REASON_REQUIRED": 400,
    "BAD_JSON": 400,
}


def _req(body: dict, field: str):
    value = body.get(field)
    if value is None:
        raise DomainError("MISSING_FIELD", f"缺少必填字段：{field}")
    return value


class RedemptionApi:
    """路由表 + 处理器，便于脱离 HTTP 层直接测试。"""

    def __init__(self, service: RedemptionService) -> None:
        self.service = service
        self.routes = [
            ("GET", re.compile(r"^/health$"), self.health),
            ("POST", re.compile(r"^/batches$"), self.create_batch),
            ("GET", re.compile(r"^/batches$"), self.list_batches),
            ("GET", re.compile(r"^/batches/(?P<batch_id>[^/]+)$"), self.get_batch),
            ("POST", re.compile(r"^/batches/(?P<batch_id>[^/]+)/transition$"), self.transition_batch),
            ("POST", re.compile(r"^/volunteers$"), self.create_volunteer),
            ("GET", re.compile(r"^/volunteers/(?P<volunteer_id>[^/]+)/account$"), self.get_account),
            ("POST", re.compile(r"^/volunteers/(?P<volunteer_id>[^/]+)/points$"), self.adjust_points),
            ("POST", re.compile(r"^/volunteers/(?P<volunteer_id>[^/]+)/level$"), self.change_level),
            ("POST", re.compile(r"^/redemptions$"), self.submit_redemption),
            ("GET", re.compile(r"^/redemptions/(?P<order_id>[^/]+)$"), self.get_order),
            ("POST", re.compile(r"^/redemptions/(?P<order_id>[^/]+)/confirm$"), self.confirm),
            ("POST", re.compile(r"^/redemptions/(?P<order_id>[^/]+)/cancel$"), self.cancel),
            ("POST", re.compile(r"^/redemptions/(?P<order_id>[^/]+)/reject$"), self.reject),
            ("GET", re.compile(r"^/redemptions/(?P<order_id>[^/]+)/conservation$"), self.conservation),
            ("POST", re.compile(r"^/tasks/release-expired$"), self.release_expired),
        ]

    def dispatch(self, method: str, path: str, body: dict) -> tuple[int, dict | list]:
        for route_method, pattern, handler in self.routes:
            if route_method != method:
                continue
            match = pattern.match(path)
            if match:
                return handler(body, **match.groupdict())
        raise DomainError("NOT_FOUND", f"接口不存在：{method} {path}")

    # ------------------------------------------------------------------
    def health(self, body: dict) -> tuple[int, dict]:
        return 200, {"ok": True, "service": "benefit-redemption"}

    def create_batch(self, body: dict) -> tuple[int, dict]:
        batch = self.service.create_batch(
            name=_req(body, "name"),
            required_level=_req(body, "required_level"),
            total_stock=_req(body, "total_stock"),
            points_price=_req(body, "points_price"),
            reservation_ttl_seconds=_req(body, "reservation_ttl_seconds"),
            actor=body.get("actor", "system"),
        )
        return 201, batch

    def list_batches(self, body: dict) -> tuple[int, list]:
        return 200, self.service.list_batches()

    def get_batch(self, body: dict, batch_id: str) -> tuple[int, dict]:
        return 200, self.service.get_batch(batch_id)

    def transition_batch(self, body: dict, batch_id: str) -> tuple[int, dict]:
        return 200, self.service.transition_batch(
            batch_id,
            _req(body, "to_status"),
            reason=_req(body, "reason"),
            actor=body.get("actor", "system"),
        )

    def create_volunteer(self, body: dict) -> tuple[int, dict]:
        return 201, self.service.create_volunteer(
            _req(body, "volunteer_id"),
            level=body.get("level", 0),
            points=body.get("points", 0),
        )

    def get_account(self, body: dict, volunteer_id: str) -> tuple[int, dict]:
        return 200, self.service.get_account(volunteer_id)

    def adjust_points(self, body: dict, volunteer_id: str) -> tuple[int, dict]:
        return 200, self.service.adjust_points(
            volunteer_id,
            _req(body, "delta"),
            reason=body.get("reason", "运营调整"),
            actor=body.get("actor", "system"),
        )

    def change_level(self, body: dict, volunteer_id: str) -> tuple[int, dict]:
        return 200, self.service.change_level(
            volunteer_id,
            _req(body, "new_level"),
            reason=_req(body, "reason"),
            actor=body.get("actor", "system"),
        )

    def submit_redemption(self, body: dict) -> tuple[int, dict]:
        view = self.service.submit_redemption(
            volunteer_id=_req(body, "volunteer_id"),
            batch_id=_req(body, "batch_id"),
            quantity=_req(body, "quantity"),
            idempotency_key=_req(body, "idempotency_key"),
            actor=body.get("actor"),
        )
        return (200 if view.get("idempotent_replay") else 201), view

    def get_order(self, body: dict, order_id: str) -> tuple[int, dict]:
        return 200, self.service.get_order(order_id)

    def confirm(self, body: dict, order_id: str) -> tuple[int, dict]:
        return 200, self.service.confirm_redemption(
            order_id,
            confirmed_quantity=_req(body, "confirmed_quantity"),
            reason=body.get("reason"),
            actor=body.get("actor", "system"),
            callback_id=body.get("callback_id"),
        )

    def cancel(self, body: dict, order_id: str) -> tuple[int, dict]:
        return 200, self.service.cancel_redemption(
            order_id,
            reason=body.get("reason"),
            actor=body.get("actor"),
            callback_id=body.get("callback_id"),
        )

    def reject(self, body: dict, order_id: str) -> tuple[int, dict]:
        return 200, self.service.reject_redemption(
            order_id,
            reason=_req(body, "reason"),
            actor=body.get("actor", "auditor"),
            callback_id=body.get("callback_id"),
        )

    def conservation(self, body: dict, order_id: str) -> tuple[int, dict]:
        return 200, self.service.verify_conservation(order_id)

    def release_expired(self, body: dict) -> tuple[int, dict]:
        return 200, self.service.release_expired(now=body.get("now"))


def make_handler(api: RedemptionApi):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def _handle(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body: dict = {}
            if raw:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = None
                if not isinstance(parsed, dict):
                    self._send(400, {"error": {"code": "BAD_JSON", "message": "请求体必须是 JSON 对象"}})
                    return
                body = parsed
            path = self.path.split("?", 1)[0]
            try:
                status, payload = api.dispatch(method, path, body)
            except DomainError as exc:
                status = ERROR_STATUS.get(exc.code, 409)
                payload = {"error": {"code": exc.code, "message": exc.message}}
            except Exception as exc:  # 兜底，避免连接悬挂
                status = 500
                payload = {"error": {"code": "INTERNAL", "message": str(exc)}}
            self._send(status, payload)

        def _send(self, status: int, payload) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args) -> None:  # 静默访问日志
            pass

    return Handler


def serve(service: RedemptionService | None = None, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    service = service or RedemptionService()
    server = ThreadingHTTPServer((host, port), make_handler(RedemptionApi(service)))
    server.daemon_threads = True
    return server
