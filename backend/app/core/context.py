"""Per-request correlation context.

Spec 55.01 s23 requires an ``X-Request-ID`` that propagates through every log line
and internal call. A ContextVar carries it without threading an argument through
every function signature.
"""

from __future__ import annotations

from contextvars import ContextVar

_request_id: ContextVar[str | None] = ContextVar("mado_request_id", default=None)


def set_request_id(value: str) -> None:
    _request_id.set(value)


def get_request_id() -> str | None:
    return _request_id.get()
