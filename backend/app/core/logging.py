"""Structured logging.

Spec 80.02 s13 requires each line to carry timestamp, request id, service, severity
and correlation data - and requires that secrets never appear. structlog renders
JSON in deployed environments and a readable console format locally.
"""

from __future__ import annotations

import logging
import sys

import structlog

from app.core.config import get_settings
from app.core.context import get_request_id

SENSITIVE_KEYS = {
    "password",
    "password_hash",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "api_key",
    "secret",
    "jwt_secret",
    "gemini_api_key",
}


def _add_request_id(_logger, _method, event_dict):
    request_id = get_request_id()
    if request_id:
        event_dict["request_id"] = request_id
    return event_dict


def _redact(_logger, _method, event_dict):
    """Drop known-sensitive keys before anything reaches a sink."""
    for key in list(event_dict):
        if key.lower() in SENSITIVE_KEYS:
            event_dict[key] = "[redacted]"
    return event_dict


def configure_logging() -> None:
    settings = get_settings()
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)

    renderer = (
        structlog.dev.ConsoleRenderer()
        if settings.environment == "development"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_request_id,
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "mado"):
    return structlog.get_logger(name)
