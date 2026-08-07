"""Notifications (spec NOT-001).

The one MVP capability with nothing behind it at all. An explorer can save an
event and plan an evening around it, and the platform will never mention it
again - which makes both features half-useful, because the value of knowing
something is on depends on being reminded before it happens.

Design constraints, in the order they mattered:

**A notification is a promise about the future, so it is scheduled, not sent.**
Rows are created ahead of time with a `deliver_at`, and a worker delivers what is
due. That makes the schedule inspectable and cancellable - if an event is
cancelled or an explorer unsaves it, the pending reminder is withdrawn rather
than racing out anyway.

**Silence is the default for anything not asked for.** Reminders about saved and
planned things are opt-out, because an explorer who saved an event has already
expressed interest in it. Anything else - suggestions, nudges, marketing - is
opt-in, and there is no setting that quietly enables them.

**Nothing arrives in the middle of the night.** A reminder pushed at 03:00 is
worse than no reminder: it wakes someone for something they cannot act on. Quiet
hours shift a delivery rather than dropping it, unless shifting would push it
past the thing it is reminding them about.

**Deduplicated on intent, not on content.** One reminder per explorer per event
per kind, enforced in the database. Two code paths that both decide to remind
someone about the same concert produce one reminder, which is the only way to
make this safe to run repeatedly.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.logging import get_logger
from app.core.mixins import Timestamps, UUIDPrimaryKey

logger = get_logger("mado.notifications")

SCHEMA = "explorer"

# Kinds. Kept as constants rather than an enum column so adding one is a code
# change and not a migration - the set will grow.
KIND_EVENT_REMINDER = "event_reminder"
KIND_PLAN_REMINDER = "plan_reminder"
KIND_MODERATION = "moderation_outcome"
KIND_NEARBY = "nearby_suggestion"

# Which kinds an explorer receives unless they say otherwise. Reminders about
# things they saved or planned are opt-out: saving an event *is* the request.
# Everything else is opt-in and stays off until asked for.
DEFAULT_ON = frozenset({KIND_EVENT_REMINDER, KIND_PLAN_REMINDER, KIND_MODERATION})

STATUS_PENDING = "pending"
STATUS_DELIVERED = "delivered"
STATUS_READ = "read"
STATUS_CANCELLED = "cancelled"

# How long before an event to remind. Far enough ahead to still travel there,
# close enough that it is about tonight rather than something abstract.
REMINDER_LEAD = timedelta(hours=3)

# Nothing is delivered between these hours, in the explorer's own timezone.
QUIET_START = time(22, 0)
QUIET_END = time(7, 30)

# A reminder that has been sitting undelivered this long is stale - the thing it
# was about has almost certainly happened.
MAX_DELIVERY_DELAY = timedelta(hours=12)


class Notification(Base, UUIDPrimaryKey, Timestamps):
    """One thing to tell one explorer, at a particular time."""

    __tablename__ = "notifications"
    __table_args__ = (
        # One reminder per explorer per subject per kind. Two code paths that
        # both decide to remind someone about the same concert must produce one
        # reminder, or a scheduler that runs every half hour produces dozens.
        UniqueConstraint(
            "user_id", "kind", "subject_id", name="uq_notification_user_kind_subject"
        ),
        Index("ix_notifications_delivery", "status", "deliver_at"),
        Index("ix_notifications_user_created", "user_id", "created_at"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("identity.users.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, default=None)
    # What this is about. No foreign key: a notification should survive its
    # subject being withdrawn, so an explorer can still see why they were told
    # something even after the listing is gone.
    subject_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), default=None)
    subject_type: Mapped[str | None] = mapped_column(String(32), default=None)
    # Where tapping it should go.
    link: Mapped[str | None] = mapped_column(String(300), default=None)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_PENDING, nullable=False)
    deliver_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    context: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    @property
    def is_unread(self) -> bool:
        return self.status == STATUS_DELIVERED


@dataclass(slots=True)
class Preferences:
    """Which kinds an explorer wants, resolved from their profile."""

    enabled: frozenset[str]
    timezone: str = "Africa/Addis_Ababa"

    def wants(self, kind: str) -> bool:
        return kind in self.enabled


def resolve_preferences(profile) -> Preferences:
    """Read notification settings off a profile.

    Absent settings mean the defaults, which are opt-out for things the explorer
    already asked for and off for everything else. An explicit false always wins.
    """
    if profile is None:
        return Preferences(enabled=frozenset())

    stored = (profile.preferences or {}).get("notifications") or {}
    enabled = {
        kind
        for kind in (KIND_EVENT_REMINDER, KIND_PLAN_REMINDER, KIND_MODERATION, KIND_NEARBY)
        if stored.get(kind, kind in DEFAULT_ON)
    }
    return Preferences(
        enabled=frozenset(enabled),
        timezone=getattr(profile, "timezone", None) or "Africa/Addis_Ababa",
    )


def respect_quiet_hours(when: datetime, *, timezone: str, deadline: datetime | None) -> datetime:
    """Shift a delivery out of the small hours.

    Moved rather than dropped: the explorer still wants to know. But never past
    ``deadline`` - a reminder that arrives after the concert has started is worse
    than one that arrives at an awkward hour, so if the only quiet-hours-
    respecting slot is too late, it goes out on time instead.
    """
    try:
        zone = ZoneInfo(timezone)
    except Exception:  # noqa: BLE001 - an unknown zone must not lose a reminder
        return when

    local = when.astimezone(zone)
    in_quiet = local.time() >= QUIET_START or local.time() < QUIET_END
    if not in_quiet:
        return when

    # Next morning at the end of quiet hours.
    target = local.replace(
        hour=QUIET_END.hour, minute=QUIET_END.minute, second=0, microsecond=0
    )
    if local.time() >= QUIET_START:
        target += timedelta(days=1)

    shifted = target.astimezone(UTC)
    if deadline is not None and shifted >= deadline:
        # Waiting would miss the thing entirely.
        return when
    return shifted


class NotificationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def schedule(
        self,
        user_id: uuid.UUID,
        *,
        kind: str,
        title: str,
        deliver_at: datetime,
        body: str | None = None,
        subject_id: uuid.UUID | None = None,
        subject_type: str | None = None,
        link: str | None = None,
        preferences: Preferences | None = None,
        deadline: datetime | None = None,
    ) -> Notification | None:
        """Queue a notification, or return None if it should not be sent.

        Returns None rather than raising for the ordinary reasons a notification
        is skipped - the explorer switched that kind off, or one already exists.
        Callers schedule in loops and a skip is not an error.
        """
        if preferences is not None and not preferences.wants(kind):
            return None

        # Never schedule into the past: a worker would deliver it immediately,
        # which turns "remind me before" into "tell me now".
        if deliver_at <= datetime.now(UTC):
            return None

        if preferences is not None:
            deliver_at = respect_quiet_hours(
                deliver_at, timezone=preferences.timezone, deadline=deadline
            )

        existing = await self._find(user_id, kind, subject_id)
        if existing is not None:
            # Re-scheduling an existing pending reminder is allowed - an event
            # moved, so the reminder moves with it. A delivered one is left
            # alone; telling someone twice is worse than telling them late.
            if existing.status == STATUS_PENDING:
                existing.deliver_at = deliver_at
                existing.title = title
                existing.body = body
            return None

        notification = Notification(
            user_id=user_id,
            kind=kind,
            title=title[:200],
            body=body,
            subject_id=subject_id,
            subject_type=subject_type,
            link=link,
            deliver_at=deliver_at,
            status=STATUS_PENDING,
        )
        self.session.add(notification)
        return notification

    async def cancel_for_subject(
        self, subject_id: uuid.UUID, *, user_id: uuid.UUID | None = None
    ) -> int:
        """Withdraw pending notifications about something.

        Called when an event is cancelled or an explorer unsaves it. Only pending
        ones: a delivered notification is a thing that already happened and
        cannot be unsent.
        """
        stmt = select(Notification).where(
            Notification.subject_id == subject_id,
            Notification.status == STATUS_PENDING,
        )
        if user_id is not None:
            stmt = stmt.where(Notification.user_id == user_id)

        pending = list((await self.session.execute(stmt)).scalars().all())
        for notification in pending:
            notification.status = STATUS_CANCELLED
        return len(pending)

    async def due(self, *, now: datetime | None = None, limit: int = 500) -> list[Notification]:
        """Notifications ready to deliver.

        Anything overdue by more than MAX_DELIVERY_DELAY is skipped and marked -
        the thing it was about has happened, and delivering it now is noise that
        teaches an explorer to ignore the next one.
        """
        now = now or datetime.now(UTC)
        result = await self.session.execute(
            select(Notification)
            .where(
                Notification.status == STATUS_PENDING,
                Notification.deliver_at <= now,
            )
            .order_by(Notification.deliver_at)
            .limit(limit)
        )

        ready: list[Notification] = []
        for notification in result.scalars().all():
            deliver_at = notification.deliver_at
            if deliver_at.tzinfo is None:
                deliver_at = deliver_at.replace(tzinfo=UTC)
            if now - deliver_at > MAX_DELIVERY_DELAY:
                notification.status = STATUS_CANCELLED
                continue
            ready.append(notification)
        return ready

    async def mark_delivered(self, notification: Notification) -> None:
        notification.status = STATUS_DELIVERED
        notification.delivered_at = datetime.now(UTC)

    async def inbox(
        self, user_id: uuid.UUID, *, limit: int = 50, unread_only: bool = False
    ) -> list[Notification]:
        stmt = select(Notification).where(
            Notification.user_id == user_id,
            Notification.status.in_(
                (STATUS_DELIVERED, STATUS_READ) if not unread_only else (STATUS_DELIVERED,)
            ),
        )
        result = await self.session.execute(
            stmt.order_by(Notification.delivered_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def unread_count(self, user_id: uuid.UUID) -> int:
        result = await self.session.execute(
            select(Notification).where(
                Notification.user_id == user_id, Notification.status == STATUS_DELIVERED
            )
        )
        return len(list(result.scalars().all()))

    async def mark_read(self, user_id: uuid.UUID, notification_id: uuid.UUID) -> bool:
        notification = await self.session.get(Notification, notification_id)
        if notification is None or notification.user_id != user_id:
            return False
        notification.status = STATUS_READ
        notification.read_at = datetime.now(UTC)
        return True

    async def mark_all_read(self, user_id: uuid.UUID) -> int:
        result = await self.session.execute(
            select(Notification).where(
                Notification.user_id == user_id, Notification.status == STATUS_DELIVERED
            )
        )
        now = datetime.now(UTC)
        rows = list(result.scalars().all())
        for notification in rows:
            notification.status = STATUS_READ
            notification.read_at = now
        return len(rows)

    async def _find(
        self, user_id: uuid.UUID, kind: str, subject_id: uuid.UUID | None
    ) -> Notification | None:
        result = await self.session.execute(
            select(Notification).where(
                Notification.user_id == user_id,
                Notification.kind == kind,
                Notification.subject_id == subject_id,
            )
        )
        return result.scalars().first()
