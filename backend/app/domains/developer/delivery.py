"""Sending webhooks (spec DEV-003, 55.10 §39-47).

The half that runs on a timer rather than on a request. `webhooks.emit` writes
rows; this drains them.

**Signed, not authenticated.** A receiver cannot tell a POST from Mado from a
POST from anybody who learned the URL, so every delivery carries an HMAC of the
timestamp and the exact bytes of the body. A receiver that checks the signature
knows two things: we sent it, and nothing changed in transit. A receiver that
skips the check has an open webhook endpoint on the internet, which is why the
verification recipe is in the interface next to the secret.

**The timestamp is inside the signed material.** Signing the body alone makes
every delivery replayable forever by anyone who captured one. With the timestamp
signed, a receiver can reject anything older than its tolerance and the
signature cannot be moved to a fresh one (§41-42).

**A slow endpoint must not slow anything else** (§65). Deliveries are claimed in
small batches with `SKIP LOCKED`, each has a hard timeout, and the batch runs
concurrently - so one receiver that takes ten seconds to answer costs ten
seconds once, not ten seconds per event behind it.

**Failure is expected and bounded.** Five attempts over roughly seventy minutes,
then the delivery is failed and left in the history for its owner to look at or
retry by hand. An endpoint that fails ten deliveries that way is suspended,
because at that point we are not experiencing a blip, we are hammering somebody
who is not listening.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.logging import get_logger
from app.domains.developer.webhooks import (
    DELIVERY_DELIVERED,
    DELIVERY_FAILED,
    DELIVERY_PENDING,
    DELIVERY_RETRYING,
    STATUS_ACTIVE,
    STATUS_SUSPENDED,
    SUSPEND_AFTER_FAILURES,
    UnsafeEndpoint,
    WebhookDelivery,
    WebhookEndpoint,
    check_endpoint_url,
    next_attempt,
)

logger = get_logger("mado.webhook_delivery")

# Long enough for a receiver on another continent that writes to its own
# database before answering; short enough that a hung endpoint releases the
# worker within a batch interval.
TIMEOUT_SECONDS = 10.0

# How many are attempted per run. Bounded so one publisher with a burst of
# events cannot monopolise a cycle, and so a run finishes well inside its
# interval.
BATCH = 20

# Receivers answering this are saying the endpoint is gone for good. Retrying it
# is pointless and continuing to queue for it is rude (§46).
GONE = 410

HEADER_EVENT = "X-Mado-Event"
HEADER_DELIVERY = "X-Mado-Delivery"
HEADER_TIMESTAMP = "X-Mado-Timestamp"
HEADER_SIGNATURE = "X-Mado-Signature"


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """`t.body`, HMAC-SHA256, hex.

    The separator matters. Without it, a timestamp of 12 and a body starting "3"
    signs the same material as a timestamp of 123 and a body starting with the
    rest - which is a real, if narrow, forgery. A dot cannot appear in the
    decimal timestamp, so the split is unambiguous.
    """
    material = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()


def signature_header(secret: str, timestamp: int, body: bytes) -> str:
    """`t=...,v1=...` - the scheme carries its own version.

    A bare hex string leaves no way to change algorithm later without every
    receiver breaking on the same day.
    """
    return f"t={timestamp},v1={sign(secret, timestamp, body)}"


def verify(secret: str, header: str, body: bytes, *, tolerance_seconds: int = 300) -> bool:
    """The check a receiver performs. Here so it can be tested, and so the
    documentation shown to publishers is generated from working code rather
    than written once and left to drift."""
    parts = dict(
        piece.split("=", 1) for piece in header.split(",") if "=" in piece
    )
    try:
        timestamp = int(parts.get("t", ""))
    except ValueError:
        return False
    if abs(time.time() - timestamp) > tolerance_seconds:
        return False
    # Constant time: a fast reject on the first wrong byte leaks the signature
    # one byte at a time to anybody willing to measure.
    return hmac.compare_digest(parts.get("v1", ""), sign(secret, timestamp, body))


def _body_of(delivery: WebhookDelivery) -> bytes:
    """The exact bytes that get signed and sent.

    Serialised once, deterministically. Re-serialising for the signature and
    again for the request would eventually produce two different byte strings -
    a different key order is enough - and every signature would fail for reasons
    nobody could reproduce.
    """
    return json.dumps(delivery.payload, separators=(",", ":"), sort_keys=True).encode()


async def _attempt(client: httpx.AsyncClient, endpoint: WebhookEndpoint, delivery: WebhookDelivery):
    """One POST. Returns (status_code | None, error | None, milliseconds)."""
    body = _body_of(delivery)
    timestamp = int(time.time())
    started = time.perf_counter()

    try:
        # Re-checked here, not only at subscribe time: DNS is the receiver's to
        # change, and a name that resolved to a public address last week can
        # resolve to a metadata endpoint today.
        check_endpoint_url(endpoint.url)
    except UnsafeEndpoint as exc:
        return None, str(exc), 0

    try:
        response = await client.post(
            endpoint.url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mado-Webhooks/1.0",
                HEADER_EVENT: delivery.event_type,
                HEADER_DELIVERY: str(delivery.event_id),
                HEADER_TIMESTAMP: str(timestamp),
                HEADER_SIGNATURE: signature_header(endpoint.secret, timestamp, body),
            },
        )
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"[:300], int(
            (time.perf_counter() - started) * 1000
        )

    return response.status_code, None, int((time.perf_counter() - started) * 1000)


def _record(
    endpoint: WebhookEndpoint,
    delivery: WebhookDelivery,
    status_code: int | None,
    error: str | None,
    duration_ms: int,
) -> None:
    """Apply the outcome of one attempt to both rows."""
    now = datetime.now(UTC)
    delivery.attempts += 1
    delivery.response_status = status_code
    delivery.duration_ms = duration_ms

    # Counted separately from `dependency_calls`, because the far end belongs to
    # a publisher rather than to us: a rise here is somebody else's outage, and
    # filing it under our dependencies would send the wrong team looking.
    metrics.webhook_deliveries.inc(
        "delivered"
        if status_code is not None and 200 <= status_code < 300
        else "failed"
    )

    if status_code is not None and 200 <= status_code < 300:
        delivery.status = DELIVERY_DELIVERED
        delivery.delivered_at = now
        delivery.error = None
        endpoint.consecutive_failures = 0
        endpoint.last_success_at = now
        endpoint.last_error = None
        return

    delivery.error = error or f"HTTP {status_code}"
    endpoint.last_failure_at = now
    endpoint.last_error = delivery.error

    if status_code == GONE:
        # Believe them the first time.
        delivery.status = DELIVERY_FAILED
        endpoint.status = STATUS_SUSPENDED
        endpoint.last_error = "The endpoint reported it is gone (410)."
        logger.info("webhook_endpoint_gone", endpoint_id=str(endpoint.id))
        return

    upcoming = next_attempt(delivery.attempts)
    if upcoming is None:
        delivery.status = DELIVERY_FAILED
        endpoint.consecutive_failures += 1
        if endpoint.consecutive_failures >= SUSPEND_AFTER_FAILURES:
            endpoint.status = STATUS_SUSPENDED
            logger.warning(
                "webhook_endpoint_suspended",
                endpoint_id=str(endpoint.id),
                failures=endpoint.consecutive_failures,
            )
        return

    delivery.status = DELIVERY_RETRYING
    delivery.next_attempt_at = upcoming


async def deliver_due(session: AsyncSession, *, batch: int = BATCH) -> dict[str, int]:
    """Send everything that is due, and record what happened.

    `SKIP LOCKED` so two workers can run this at the same time without either
    waiting or both sending the same delivery. The scheduler also elects a
    single runner, which makes this redundant - and it is here anyway, because
    the day somebody runs a second process the failure would be duplicate
    webhooks arriving at customers.
    """
    now = datetime.now(UTC)
    result = await session.execute(
        select(WebhookDelivery, WebhookEndpoint)
        .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id)
        .where(
            WebhookDelivery.status.in_((DELIVERY_PENDING, DELIVERY_RETRYING)),
            WebhookDelivery.next_attempt_at <= now,
            WebhookEndpoint.status == STATUS_ACTIVE,
        )
        .order_by(WebhookDelivery.next_attempt_at)
        .limit(batch)
        .with_for_update(of=WebhookDelivery, skip_locked=True)
    )
    pairs = list(result.all())
    if not pairs:
        return {"attempted": 0, "delivered": 0, "failed": 0}

    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
        # Concurrently, so the batch costs as long as its slowest endpoint
        # rather than the sum of all of them.
        outcomes = await asyncio.gather(
            *(_attempt(client, endpoint, delivery) for delivery, endpoint in pairs)
        )

    delivered = failed = 0
    for (delivery, endpoint), (status_code, error, duration) in zip(pairs, outcomes, strict=True):
        _record(endpoint, delivery, status_code, error, duration)
        if delivery.status == DELIVERY_DELIVERED:
            delivered += 1
        elif delivery.status == DELIVERY_FAILED:
            failed += 1

    return {"attempted": len(pairs), "delivered": delivered, "failed": failed}


# Redirects are refused rather than followed. A 302 is the cheapest way to turn
# an endpoint we validated into one we did not, which would undo the SSRF check
# entirely - so `follow_redirects=False` above is load-bearing, not a default.
