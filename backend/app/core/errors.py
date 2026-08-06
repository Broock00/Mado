"""Structured platform errors.

Spec 55.01 s15 fixes the wire shape of every failure::

    {"error": {"code", "message", "details", "requestId"}}

and spec 80.02 s12 requires that errors never leak internal implementation detail.
Domain code raises :class:`PlatformError`; the handlers in ``app.main`` render it.
"""

from __future__ import annotations

from typing import Any


class PlatformError(Exception):
    """Base class for every error that is safe to surface to a client."""

    status_code: int = 500
    code: str = "INTERNAL_ERROR"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details or {}
        super().__init__(self.message)


class ValidationError(PlatformError):
    status_code = 422
    code = "VALIDATION_FAILED"
    message = "The request failed validation."


class BadRequestError(PlatformError):
    status_code = 400
    code = "BAD_REQUEST"
    message = "The request was invalid."


class AuthenticationError(PlatformError):
    status_code = 401
    code = "AUTHENTICATION_REQUIRED"
    message = "Authentication is required."


class PermissionDeniedError(PlatformError):
    status_code = 403
    code = "PERMISSION_DENIED"
    message = "You do not have permission to perform this action."


class NotFoundError(PlatformError):
    status_code = 404
    code = "RESOURCE_NOT_FOUND"
    message = "The requested resource was not found."


class ConflictError(PlatformError):
    status_code = 409
    code = "RESOURCE_CONFLICT"
    message = "The request conflicts with the current state of the resource."


class RateLimitError(PlatformError):
    status_code = 429
    code = "RATE_LIMIT_EXCEEDED"
    message = "Too many requests."


class ServiceUnavailableError(PlatformError):
    status_code = 503
    code = "SERVICE_UNAVAILABLE"
    message = "The service is temporarily unavailable."


class ExperienceNotFoundError(NotFoundError):
    code = "EXPERIENCE_NOT_FOUND"
    message = "The requested experience was not found."


class EventNotFoundError(NotFoundError):
    code = "EVENT_NOT_FOUND"
    message = "The requested event was not found."


class CityNotFoundError(NotFoundError):
    code = "CITY_NOT_FOUND"
    message = "The requested city was not found."
