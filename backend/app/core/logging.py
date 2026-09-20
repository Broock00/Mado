"""Structured logging.

Spec 80.02 s13 requires each line to carry timestamp, request id, service, severity
and correlation data - and requires that secrets never appear. structlog renders
JSON in deployed environments and a readable console format locally.
"""

from __future__ import annotations

import logging
import re
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
    "r2_secret_access_key",
    "secret_access_key",
}

# Key-name redaction is not enough on its own. A secret passed as a URL query
# parameter reappears inside error *messages* - httpx renders the full request URL
# into every transport error - so it arrives as an ordinary string in a field
# named something innocuous like "error". These patterns catch it there.
_SECRET_PATTERNS = (
    # `?key=...`, `&api_key=...`, `token=...` in any URL or message.
    re.compile(
        r"(?i)\b(key|api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)"
        r"=([^\s&\"'<>]+)"
    ),
    # Google API keys, which are recognisable on their own and may appear bare.
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),
    # `Authorization: Bearer <jwt>` and friends.
    re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._\-+/=]{8,})"),
)


def redact(text: str) -> str:
    """Strip anything that looks like a credential out of a free-text string.

    Used on error text before logging. Deliberately over-eager: a redacted value
    that turns out to be harmless costs a debugging round-trip, a leaked key costs
    a rotation.
    """
    if not text:
        return text
    result = _SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)}=[redacted]", text)
    result = _SECRET_PATTERNS[1].sub("[redacted]", result)
    return _SECRET_PATTERNS[2].sub(lambda m: f"{m.group(1)} [redacted]", result)


def _add_request_id(_logger, _method, event_dict):
    request_id = get_request_id()
    if request_id:
        event_dict["request_id"] = request_id
    return event_dict


def _redact(_logger, _method, event_dict):
    """Strip secrets before anything reaches a sink.

    Two passes, because they catch different failures. Key-name matching handles
    the case where a secret is logged deliberately under an obvious name; value
    scanning handles the case where one is embedded in a string nobody expected to
    contain it - which is the one that actually leaks in practice.
    """
    for key in list(event_dict):
        if key.lower() in SENSITIVE_KEYS:
            event_dict[key] = "[redacted]"
        elif isinstance(event_dict[key], str):
            event_dict[key] = redact(event_dict[key])
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
