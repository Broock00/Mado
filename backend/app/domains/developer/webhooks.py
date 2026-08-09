"""Webhook subscriptions (spec DEV-003, 55.10 §31-54).

Telling somebody else's system that something happened here, so a publisher's
own site does not have to poll for a listing they published themselves.

**An event is queued in the transaction that caused it.** `emit` adds delivery
rows to the caller's session and does not flush or send. If the publish rolls
back, the announcement rolls back with it - the alternative is telling the world
about an experience that does not exist, which no retry policy can take back.
Sending happens later, from the scheduler, where a slow endpoint cannot hold a
request open.

**Delivery is at-least-once, so payloads carry an event id.** A network failure
after the receiver committed is indistinguishable from one before, and the only
safe response is to send again. Consumers deduplicate on `id`; the spec
(§43, §60) asks for exactly this and explicitly does not promise exactly-once.

**The signing secret is stored in the clear, and the API key is not.** They look
alike and are not: an API key is a bearer credential we only ever need to
*recognise*, so a digest is enough, while a signing secret has to be *used* on
every delivery to compute an HMAC. There is no version of this that stores a
hash. It is shown once at creation and can be rotated; the list shows a preview.

**Endpoint URLs are checked for SSRF at subscribe time and again at send time.**
Once is not enough: the host is somebody else's DNS record, and it can start
resolving to 169.254.169.254 an hour after we approved it (§62-63).
"""

from __future__ import annotations

import ipaddress
import re
import secrets
import socket
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.core.database import Base
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.mixins import Timestamps, UUIDPrimaryKey
from app.domains.identity.models import User
from app.domains.publisher.models import SCHEMA

logger = get_logger("mado.webhooks")

SECRET_PREFIX = "whsec_"
SECRET_BYTES = 24

# The event vocabulary. Named after the taxonomy rather than after the spec's
# examples where the two disagree: the spec says `booking.created`, and Mado has
# no bookings - it has reservations, which hold a place and take no money. An
# event type that lies about the domain model is worse than one that differs
# from a document.
EVENT_TYPES: dict[str, str] = {
    "experience.published": "One of your experiences became discoverable.",
    "experience.unpublished": "One of your experiences was taken out of discovery.",
    "experience.updated": "Details of a published experience changed.",
    "event.cancelled": "A date was cancelled.",
    "reservation.created": "Somebody held a place at one of your dates.",
    "reservation.cancelled": "Somebody gave a place back.",
}

STATUS_ACTIVE = "active"
# Repeated failures. Still listed, still yours, but nothing is queued for it
# until somebody looks at why. The spec calls this suspension (§53).
STATUS_SUSPENDED = "suspended"
# Switched off by its owner.
STATUS_PAUSED = "paused"

# Consecutive *deliveries* that exhausted their retries, not consecutive
# attempts. Ten is generous: an endpoint that has failed ten separate events
# through five attempts each is not coming back on its own, and continuing to
# queue for it fills the table with rows nobody will read.
SUSPEND_AFTER_FAILURES = 10

# Waits before attempts 2..5 (§44). The first attempt is immediate. An hour at
# the end covers a deploy or a certificate renewal; beyond that the event is
# stale enough that a receiver would rather fetch current state than replay it.
BACKOFF_SECONDS = (60, 300, 900, 3600)
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1

MAX_ENDPOINTS = 10
MAX_URL = 500

DELIVERY_PENDING = "pending"
DELIVERY_DELIVERED = "delivered"
DELIVERY_RETRYING = "retrying"
DELIVERY_FAILED = "failed"

# How long delivery history is worth keeping. Long enough to debug last week's
# integration, short enough that the table is not a log of every event the
# platform ever emitted.
HISTORY_DAYS = 30


class WebhookEndpoint(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "webhook_endpoints"
    __table_args__ = (
        Index("ix_webhook_endpoints_owner", "owner_user_id"),
        {"schema": SCHEMA},
    )

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("identity.users.id", ondelete="CASCADE"),
        nullable=False,
    )
    url: Mapped[str] = mapped_column(String(MAX_URL), nullable=False)
    events: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    # Readable by design - see the module docstring.
    secret: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(String(16), default=STATUS_ACTIVE, nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)

    @property
    def secret_preview(self) -> str:
        return f"{self.secret[: len(SECRET_PREFIX) + 4]}...{self.secret[-4:]}"


class WebhookDelivery(Base, UUIDPrimaryKey, Timestamps):
    """One event, queued for one endpoint."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        # The scheduler's query: what is due, oldest first.
        Index("ix_webhook_deliveries_due", "status", "next_attempt_at"),
        Index("ix_webhook_deliveries_endpoint", "endpoint_id", "created_at"),
        {"schema": SCHEMA},
    )

    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.webhook_endpoints.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The id the receiver deduplicates on. Stable across every retry of this
    # delivery, which is the entire point of it.
    event_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)

    # The body as it will be sent. Frozen at emit time rather than rebuilt at
    # send time: a retry an hour later must describe the event that happened,
    # not the state of the world when the retry ran.
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    status: Mapped[str] = mapped_column(String(16), default=DELIVERY_PENDING, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    response_status: Mapped[int | None] = mapped_column(Integer, default=None)
    error: Mapped[str | None] = mapped_column(String(300), default=None)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)

    # Marks a delivery the owner asked for from the interface, so a receiver can
    # tell a drill from the real thing (§51).
    is_test: Mapped[bool] = mapped_column(default=False, nullable=False)


# --------------------------------------------------------------- SSRF guard


class UnsafeEndpoint(ValidationError):
    code = "WEBHOOK_INVALID_ENDPOINT"


_HOSTNAME = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9\-.]{0,251}[a-zA-Z0-9])?$")


def _address_is_reachable_publicly(raw: str) -> bool:
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return False
    # `is_global` covers loopback, private ranges, link-local (and with it
    # 169.254.169.254, every cloud's metadata service), multicast, reserved and
    # unspecified in one property - which is safer than a hand-written list that
    # forgets one of them.
    return address.is_global


def _is_metadata_address(raw: str) -> bool:
    """Link-local, where every cloud provider parks its instance metadata.

    Checked separately because it is the one range that stays forbidden even
    when the development escape hatch is open. Wanting a receiver on your own
    machine is ordinary; wanting one at 169.254.169.254 is not a development
    need, and the escape hatch is exactly the setting somebody eventually turns
    on in an environment that has a metadata service.
    """
    try:
        return ipaddress.ip_address(raw).is_link_local
    except ValueError:
        return False


def check_endpoint_url(url: str, *, resolve: bool = True) -> None:
    """Refuse anything that could be pointed back at our own network (§62-63).

    Called when a subscription is created or changed, and again immediately
    before every delivery. Doing it twice is not belt and braces: the second
    check is the only one that sees what the name resolves to *now*.
    """
    settings = get_settings()
    parsed = urlparse(url.strip())

    if parsed.scheme not in {"http", "https"}:
        raise UnsafeEndpoint("A webhook endpoint must be an http or https URL.")
    if parsed.scheme == "http" and settings.environment != "development":
        # Signatures make the body tamper-evident, not private. The payload
        # names your listings and who reserved a place at them.
        raise UnsafeEndpoint("A webhook endpoint must use https.")
    if parsed.username or parsed.password:
        # Credentials in a URL end up in logs, and we would be the one logging
        # them. Use the signature.
        raise UnsafeEndpoint("Put credentials in a header, not in the URL.")

    host = parsed.hostname
    if not host or not _HOSTNAME.match(host.strip("[]")):
        raise UnsafeEndpoint("That does not look like a hostname.")
    if len(url) > MAX_URL:
        raise UnsafeEndpoint("That URL is too long.")

    if not resolve:
        return

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        resolved = socket.getaddrinfo(host, port)
    except OSError:
        if settings.webhook_allow_private_endpoints:
            # A name that does not resolve cannot be reached, so with the hatch
            # open there is nothing to protect against.
            return
        raise UnsafeEndpoint("That hostname does not resolve.") from None

    addresses = {info[4][0] for info in resolved}
    if any(_is_metadata_address(a) for a in addresses):
        # Refused whatever the settings say - see `_is_metadata_address`.
        raise UnsafeEndpoint("That endpoint is not reachable from the public internet.")
    if settings.webhook_allow_private_endpoints:
        # Development only, and off by default. Without it there is no way to
        # test against a receiver on your own machine.
        return
    if not addresses or not all(_address_is_reachable_publicly(a) for a in addresses):
        # Deliberately vague. "10.0.0.5 is private" confirms to somebody probing
        # what your internal network looks like.
        raise UnsafeEndpoint("That endpoint is not reachable from the public internet.")


# ------------------------------------------------------------------ service


class WebhookService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def subscribe(
        self, owner: User, *, url: str, events: list[str]
    ) -> tuple[WebhookEndpoint, str]:
        """Register an endpoint, returning it and its secret in plaintext once."""
        url = url.strip()
        check_endpoint_url(url)
        chosen = self._validate_events(events)

        existing = await self.mine(owner)
        if len([e for e in existing if e.status != STATUS_PAUSED]) >= MAX_ENDPOINTS:
            raise ValidationError(
                f"You already have {MAX_ENDPOINTS} endpoints.", code="TOO_MANY_ENDPOINTS"
            )
        if any(e.url == url for e in existing):
            # Two subscriptions to the same URL means every event arrives twice,
            # which looks like a delivery bug from the far end.
            raise ConflictError("You already send to that URL.", code="ENDPOINT_EXISTS")

        secret = SECRET_PREFIX + secrets.token_urlsafe(SECRET_BYTES)
        endpoint = WebhookEndpoint(
            owner_user_id=owner.id, url=url, events=chosen, secret=secret
        )
        self.session.add(endpoint)
        await self.session.flush()

        logger.info("webhook_subscribed", endpoint_id=str(endpoint.id), events=chosen)
        return endpoint, secret

    async def mine(self, owner: User) -> list[WebhookEndpoint]:
        result = await self.session.execute(
            select(WebhookEndpoint)
            .where(WebhookEndpoint.owner_user_id == owner.id)
            .order_by(WebhookEndpoint.created_at.desc())
        )
        return list(result.scalars().all())

    async def update(
        self,
        owner: User,
        endpoint_id: uuid.UUID,
        *,
        url: str | None = None,
        events: list[str] | None = None,
        status: str | None = None,
    ) -> WebhookEndpoint:
        endpoint = await self._owned(owner, endpoint_id)

        if url is not None and url.strip() != endpoint.url:
            check_endpoint_url(url.strip())
            endpoint.url = url.strip()
        if events is not None:
            endpoint.events = self._validate_events(events)
        if status is not None:
            if status not in {STATUS_ACTIVE, STATUS_PAUSED}:
                raise ValidationError("An endpoint is either active or paused.", code="BAD_STATUS")
            endpoint.status = status
            if status == STATUS_ACTIVE:
                # Resuming clears the failure count, so an endpoint that was
                # suspended and then fixed gets a full run of chances rather
                # than being suspended again by history.
                endpoint.consecutive_failures = 0
                endpoint.last_error = None
        return endpoint

    async def delete(self, owner: User, endpoint_id: uuid.UUID) -> None:
        """Remove a subscription and everything queued for it (§36).

        The one place in the platform that really deletes. Deliveries are
        transient mail addressed to a receiver that no longer wants it, not a
        record of anything - and leaving them queued means posting to a URL its
        owner has explicitly withdrawn.
        """
        endpoint = await self._owned(owner, endpoint_id)
        await self.session.delete(endpoint)
        logger.info("webhook_deleted", endpoint_id=str(endpoint_id))

    async def rotate_secret(
        self, owner: User, endpoint_id: uuid.UUID
    ) -> tuple[WebhookEndpoint, str]:
        """Issue a new signing secret, invalidating the old one immediately.

        No overlap period. One is a real gap for a receiver mid-rotation, and
        the reason to rotate is usually that the old secret is somewhere it
        should not be - in which case keeping it valid for an hour is the
        problem, not the fix.
        """
        endpoint = await self._owned(owner, endpoint_id)
        endpoint.secret = SECRET_PREFIX + secrets.token_urlsafe(SECRET_BYTES)
        logger.info("webhook_secret_rotated", endpoint_id=str(endpoint_id))
        return endpoint, endpoint.secret

    async def deliveries(
        self, owner: User, endpoint_id: uuid.UUID, *, limit: int = 50
    ) -> list[WebhookDelivery]:
        await self._owned(owner, endpoint_id)
        result = await self.session.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.endpoint_id == endpoint_id)
            .order_by(WebhookDelivery.created_at.desc())
            .limit(min(limit, 200))
        )
        return list(result.scalars().all())

    async def retry(
        self, owner: User, endpoint_id: uuid.UUID, delivery_id: uuid.UUID
    ) -> WebhookDelivery:
        """Queue a failed delivery again (§50), keeping its event id.

        Same id, so a receiver that did process it the first time - and merely
        failed to say so - discards the duplicate instead of acting twice.
        """
        await self._owned(owner, endpoint_id)
        delivery = await self.session.get(WebhookDelivery, delivery_id)
        if delivery is None or delivery.endpoint_id != endpoint_id:
            raise NotFoundError("No such delivery.", code="WEBHOOK_DELIVERY_NOT_FOUND")
        if delivery.status == DELIVERY_DELIVERED:
            raise ConflictError("That one already arrived.", code="ALREADY_DELIVERED")

        delivery.status = DELIVERY_PENDING
        delivery.attempts = 0
        delivery.next_attempt_at = datetime.now(UTC)
        delivery.error = None
        return delivery

    async def send_test(self, owner: User, endpoint_id: uuid.UUID) -> WebhookDelivery:
        """Queue an obviously-marked test event (§51)."""
        endpoint = await self._owned(owner, endpoint_id)
        event_id = uuid.uuid4()
        delivery = WebhookDelivery(
            endpoint_id=endpoint.id,
            event_id=event_id,
            event_type="ping",
            payload=envelope(event_id, "ping", {"test": True}),
            next_attempt_at=datetime.now(UTC),
            is_test=True,
        )
        self.session.add(delivery)
        await self.session.flush()
        return delivery

    async def _owned(self, owner: User, endpoint_id: uuid.UUID) -> WebhookEndpoint:
        endpoint = await self.session.get(WebhookEndpoint, endpoint_id)
        if endpoint is None or endpoint.owner_user_id != owner.id:
            raise NotFoundError("No such endpoint.", code="WEBHOOK_NOT_FOUND")
        return endpoint

    @staticmethod
    def _validate_events(events: list[str]) -> list[str]:
        unknown = [e for e in events if e not in EVENT_TYPES]
        if unknown:
            raise ValidationError(
                f"Unknown event type: {', '.join(sorted(unknown))}.",
                code="WEBHOOK_INVALID_EVENT",
            )
        if not events:
            raise ValidationError(
                "Choose at least one event to be told about.", code="WEBHOOK_INVALID_EVENT"
            )
        return sorted(set(events))


# ------------------------------------------------------------------ emitting


def envelope(event_id: uuid.UUID, event_type: str, data: dict) -> dict:
    """The body every receiver parses (§37).

    Versioned from the first delivery. Adding a version field later means every
    existing receiver has to cope with its absence.
    """
    return {
        "id": str(event_id),
        "type": event_type,
        "version": "1.0",
        "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "data": data,
    }


async def emit(
    session: AsyncSession,
    *,
    event_type: str,
    owner_user_id: uuid.UUID,
    data: dict,
) -> int:
    """Queue one domain event for every endpoint of its owner that wants it.

    Returns how many deliveries were queued, which is almost always zero -
    hardly anybody subscribes to webhooks, and this runs on the publish path.
    One indexed query against a table with a handful of rows per publisher.

    Adds rows to the caller's session without flushing, so the announcement
    commits with the thing being announced or not at all. Failures are logged
    and swallowed: a webhook subscription must never be the reason a publisher
    cannot publish.
    """
    try:
        result = await session.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.owner_user_id == owner_user_id,
                WebhookEndpoint.status == STATUS_ACTIVE,
                WebhookEndpoint.events.contains([event_type]),
            )
        )
        endpoints = list(result.scalars().all())
    except Exception as exc:  # noqa: BLE001 - never block the action being announced
        logger.warning("webhook_emit_failed", event_type=event_type, error=str(exc))
        return 0

    if not endpoints:
        return 0

    # One id for the event, shared by every endpoint's copy. Two receivers
    # comparing notes are then talking about the same event rather than two.
    event_id = uuid.uuid4()
    body = envelope(event_id, event_type, data)
    now = datetime.now(UTC)

    for endpoint in endpoints:
        session.add(
            WebhookDelivery(
                endpoint_id=endpoint.id,
                event_id=event_id,
                event_type=event_type,
                payload=body,
                next_attempt_at=now,
            )
        )

    logger.info("webhook_emitted", event_type=event_type, endpoints=len(endpoints))
    return len(endpoints)


def next_attempt(attempts: int) -> datetime | None:
    """When to try again after `attempts` failures, or None once they run out."""
    if attempts >= MAX_ATTEMPTS:
        return None
    return datetime.now(UTC) + timedelta(seconds=BACKOFF_SECONDS[attempts - 1])
