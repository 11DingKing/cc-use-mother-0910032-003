"""志愿权益兑换领域错误。"""
from __future__ import annotations


class DomainError(Exception):
    """领域错误基类，携带接口错误码与 HTTP 状态。"""

    code = "domain_error"
    http_status = 400


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class ValidationError(DomainError):
    code = "validation_error"
    http_status = 400


class BatchClosedError(DomainError):
    code = "batch_closed"
    http_status = 409


class LevelNotEligibleError(DomainError):
    code = "level_not_eligible"
    http_status = 403


class InsufficientStockError(DomainError):
    code = "insufficient_stock"
    http_status = 409


class InsufficientPointsError(DomainError):
    code = "insufficient_points"
    http_status = 409


class InvalidStateError(DomainError):
    code = "invalid_state"
    http_status = 409


class ConservationError(DomainError):
    """补偿分录守恒校验失败。"""

    code = "conservation_violated"
    http_status = 500
