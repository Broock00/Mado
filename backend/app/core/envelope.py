"""Response envelopes and cursor pagination.

Spec 55.01 s10-12 fixes three shapes:

* single resource  -> ``{"data": {...}, "meta": {...}}``
* collection       -> ``{"data": [...], "pagination": {...}, "meta": {...}}``
* error            -> handled in :mod:`app.core.errors`

Cursor pagination is the default because discovery collections change constantly
and offsets drift under insertion (spec 55.01 s12).
"""

from __future__ import annotations

import base64
import json
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


class Meta(BaseModel):
    request_id: str | None = Field(default=None, alias="requestId")
    timestamp: str | None = None

    model_config = {"populate_by_name": True}


class Pagination(BaseModel):
    next_cursor: str | None = Field(default=None, alias="nextCursor")
    has_more: bool = Field(default=False, alias="hasMore")
    total_count: int | None = Field(default=None, alias="totalCount")

    model_config = {"populate_by_name": True}


class Envelope(BaseModel, Generic[T]):
    data: T
    meta: Meta = Field(default_factory=Meta)


class CollectionEnvelope(BaseModel, Generic[T]):
    data: list[T]
    pagination: Pagination = Field(default_factory=Pagination)
    meta: Meta = Field(default_factory=Meta)


def encode_cursor(payload: dict[str, Any]) -> str:
    """Encode an opaque cursor.

    Base64 keeps the cursor URL-safe and signals to clients that its contents are
    not a stable contract - only the platform interprets it.
    """
    raw = json.dumps(payload, separators=(",", ":"), default=str).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> dict[str, Any]:
    """Decode a cursor, treating anything malformed as "start from the beginning".

    A corrupt cursor is a client problem that should not surface as a 500; degrading
    to the first page is the predictable behaviour.
    """
    if not cursor:
        return {}
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + padding)
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def clamp_limit(limit: int | None) -> int:
    """Bound page size so no caller can request an unpaginated collection."""
    if limit is None:
        return DEFAULT_PAGE_SIZE
    return max(1, min(limit, MAX_PAGE_SIZE))
