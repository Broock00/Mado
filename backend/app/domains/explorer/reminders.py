"""Building event reminders from what explorers saved and planned.

Separated from :mod:`app.domains.explorer.notifications`, which owns *what a
notification is*. This owns *which ones are worth creating* - a product question
rather than a mechanical one, and the part that changes most.

Two sources, both meaning "I intend to be there":

* **Saved experiences** with an upcoming occurrence.
* **Kept itineraries** — an evening outing gets one advance ping; a multi-day
  trip gets an evening-before notice plus a morning ping for each day that has
  stops.

Run repeatedly and idempotently: the unique constraint on
(user, kind, subject) means a scheduler firing every half hour produces one
reminder per subject, not one per run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging import get_logger
from app.core.messages import translate
from app.domains.catalog.models import EventInstance
from app.domains.explorer.models import Itinerary, SavedItem
from app.domains.explorer.notifications import (
    KIND_EVENT_REMINDER,
    KIND_PLAN_DAY_REMINDER,
    KIND_PLAN_REMINDER,
    REMINDER_LEAD,
    NotificationService,
    resolve_preferences,
)
from app.domains.explorer.planning import ACTIVE_DAY_START_HOUR, MAX_TRIP_DAYS
from app.domains.identity.models import User

logger = get_logger("mado.reminders")

# Kept plans only — drafts are workspaces and must not ping anyone.
_STATUS_KEPT = "kept"

# How far ahead to look. Anything beyond this will be picked up by a later run,
# and scheduling months out means holding rows for things that will change.
HORIZON = timedelta(days=7)

# Local hour for the "your trip is tomorrow" notice. Quiet hours end at 07:30,
# so 18:00 is safely outside them in every timezone we ship.
TRIP_ADVANCE_HOUR = 18


def _local_time(when: datetime, timezone: str) -> str:
    try:
        return when.astimezone(ZoneInfo(timezone)).strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return when.strftime("%H:%M")


def _local_date_label(when: datetime, timezone: str) -> str:
    try:
        local = when.astimezone(ZoneInfo(timezone))
    except Exception:  # noqa: BLE001
        local = when
    # Avoid %-d (unsupported on Windows); strip a leading zero by hand.
    return f"{local.strftime('%a')} {local.day} {local.strftime('%b')}"


def plan_day_subject_id(itinerary_id: uuid.UUID, day_index: int) -> uuid.UUID:
    """Stable subject for a day's reminder — independent of which stop is first.

    uuid5 so rescheduling the same day after a reorder still hits the same row
    under the (user, kind, subject) unique constraint.
    """
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"mado:itinerary:{itinerary_id}:day:{day_index}",
    )


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def _is_trip(itinerary: Itinerary) -> bool:
    constraints = itinerary.constraints or {}
    if constraints.get("kind") == "trip":
        return True
    if any(int(getattr(s, "day_index", 0) or 0) > 0 for s in (itinerary.stops or [])):
        return True
    tz = _zone(constraints.get("timezone") or "UTC")
    start = itinerary.starts_at
    end = itinerary.ends_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return start.astimezone(tz).date() < end.astimezone(tz).date()


def _evening_before_start(starts_at: datetime, timezone: str) -> datetime:
    """18:00 local on the calendar day before the trip opens."""
    tz = _zone(timezone)
    local = starts_at.astimezone(tz)
    prev = local.date() - timedelta(days=1)
    return datetime(
        prev.year, prev.month, prev.day, TRIP_ADVANCE_HOUR, 0, 0, tzinfo=tz
    ).astimezone(UTC)


def _day_start(itinerary: Itinerary, day_index: int, timezone: str) -> datetime:
    """Local morning of that trip day — when the day itself reaches the explorer."""
    tz = _zone(timezone)
    base = itinerary.starts_at
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    local0 = base.astimezone(tz)
    day_date = local0.date() + timedelta(days=day_index)
    morning = datetime(
        day_date.year,
        day_date.month,
        day_date.day,
        ACTIVE_DAY_START_HOUR,
        0,
        0,
        tzinfo=tz,
    ).astimezone(UTC)
    # Never fire before the trip window opens.
    if morning < base:
        return base
    return morning


def _first_stop_arrive(itinerary: Itinerary, day_index: int) -> datetime | None:
    day_stops = [
        s
        for s in (itinerary.stops or [])
        if int(getattr(s, "day_index", 0) or 0) == day_index
    ]
    if not day_stops:
        return None
    earliest = min(s.arrive_at for s in day_stops)
    if earliest.tzinfo is None:
        earliest = earliest.replace(tzinfo=UTC)
    return earliest


def _first_stop_title(itinerary: Itinerary, day_index: int | None = None) -> str | None:
    stops = list(itinerary.stops or [])
    if day_index is not None:
        stops = [s for s in stops if int(getattr(s, "day_index", 0) or 0) == day_index]
    if not stops:
        return None
    stops.sort(key=lambda s: (int(getattr(s, "day_index", 0) or 0), s.position))
    return stops[0].title


async def schedule_event_reminders(
    session: AsyncSession, *, now: datetime | None = None
) -> dict[str, int]:
    """Create reminders for saved experiences that are happening soon."""
    now = now or datetime.now(UTC)
    horizon = now + HORIZON
    service = NotificationService(session)
    created = skipped = 0

    result = await session.execute(
        select(SavedItem).where(SavedItem.entity_type == "experience")
    )
    saved = list(result.scalars().all())
    if not saved:
        return {"created": 0, "skipped": 0}

    # Load the users and their preferences once rather than per saved item.
    user_ids = {item.user_id for item in saved}
    users = {
        user.id: user
        for user in (
            await session.execute(
                select(User)
                .where(User.id.in_(user_ids))
                .options(selectinload(User.profile))
            )
        ).scalars()
    }

    experience_ids = {item.entity_id for item in saved}
    occurrences = list(
        (
            await session.execute(
                select(EventInstance)
                .where(
                    EventInstance.experience_id.in_(experience_ids),
                    EventInstance.status == "scheduled",
                    EventInstance.start_time > now,
                    EventInstance.start_time <= horizon,
                )
                .options(selectinload(EventInstance.experience))
            )
        ).scalars()
    )

    # Only the next occurrence of each experience. Reminding someone about all
    # four dates of a weekly night is four notifications for one intention.
    next_by_experience: dict = {}
    for occurrence in occurrences:
        current = next_by_experience.get(occurrence.experience_id)
        if current is None or occurrence.start_time < current.start_time:
            next_by_experience[occurrence.experience_id] = occurrence

    for item in saved:
        occurrence = next_by_experience.get(item.entity_id)
        if occurrence is None:
            continue
        user = users.get(item.user_id)
        if user is None:
            continue

        preferences = resolve_preferences(getattr(user, "profile", None))
        starts = occurrence.start_time
        if starts.tzinfo is None:
            starts = starts.replace(tzinfo=UTC)

        experience = occurrence.experience
        notification = await service.schedule(
            user.id,
            kind=KIND_EVENT_REMINDER,
            title=translate(
                "reminder.event.title", preferences.language, title=experience.title
            ),
            # Two templates rather than one plus a concatenated clause. Gluing
            # " at {venue}" onto the end assumes the venue belongs at the end of
            # the sentence, which is true in English and not in Amharic - the
            # verb goes last there, so the phrase has to be built as a whole.
            body=translate(
                "reminder.event.body_at_venue" if experience.venue else "reminder.event.body",
                preferences.language,
                time=_local_time(starts, preferences.timezone),
                venue=experience.venue.name if experience.venue else "",
            ),
            deliver_at=starts - REMINDER_LEAD,
            subject_id=occurrence.id,
            subject_type="event_instance",
            link=f"/experiences/{experience.id}",
            preferences=preferences,
            # Never shift a reminder past the thing it is about.
            deadline=starts,
        )
        if notification is None:
            skipped += 1
        else:
            created += 1

    await session.flush()
    logger.info("event_reminders_scheduled", created=created, skipped=skipped)
    return {"created": created, "skipped": skipped}


async def schedule_plan_reminders(
    session: AsyncSession, *, now: datetime | None = None
) -> dict[str, int]:
    """Remind explorers about kept plans — advance, and per-day for trips.

    Outings keep a single ping a few hours before ``starts_at``. Multi-day trips
    get an evening-before notice plus one morning reminder for each day that has
    stops, so somebody mid-stay still hears about tomorrow without being
    re-told about yesterday.
    """
    now = now or datetime.now(UTC)
    horizon = now + HORIZON
    service = NotificationService(session)
    created = skipped = 0

    result = await session.execute(
        select(Itinerary)
        .where(
            Itinerary.deleted_at.is_(None),
            Itinerary.user_id.is_not(None),
            Itinerary.status == _STATUS_KEPT,
            # Still relevant: not finished, and has started or will start soon.
            Itinerary.ends_at > now,
            Itinerary.starts_at <= horizon,
        )
        .options(selectinload(Itinerary.stops))
    )
    itineraries = list(result.scalars().unique().all())
    if not itineraries:
        return {"created": 0, "skipped": 0}

    users = {
        user.id: user
        for user in (
            await session.execute(
                select(User)
                .where(User.id.in_({i.user_id for i in itineraries}))
                .options(selectinload(User.profile))
            )
        ).scalars()
    }

    for itinerary in itineraries:
        user = users.get(itinerary.user_id)
        if user is None:
            continue
        if not itinerary.stops:
            continue

        preferences = resolve_preferences(getattr(user, "profile", None))
        starts = itinerary.starts_at
        if starts.tzinfo is None:
            starts = starts.replace(tzinfo=UTC)

        constraints = itinerary.constraints or {}
        timezone = constraints.get("timezone") or preferences.timezone
        link = f"/plans/{itinerary.id}"

        if _is_trip(itinerary):
            c, s = await _schedule_trip_reminders(
                service,
                itinerary=itinerary,
                user_id=user.id,
                preferences=preferences,
                starts=starts,
                timezone=timezone,
                link=link,
                now=now,
                horizon=horizon,
            )
        else:
            c, s = await _schedule_outing_reminder(
                service,
                itinerary=itinerary,
                user_id=user.id,
                preferences=preferences,
                starts=starts,
                timezone=timezone,
                link=link,
                now=now,
            )
        created += c
        skipped += s

    await session.flush()
    logger.info("plan_reminders_scheduled", created=created, skipped=skipped)
    return {"created": created, "skipped": skipped}


async def _schedule_outing_reminder(
    service: NotificationService,
    *,
    itinerary: Itinerary,
    user_id: uuid.UUID,
    preferences,
    starts: datetime,
    timezone: str,
    link: str,
    now: datetime,
) -> tuple[int, int]:
    if starts <= now:
        return 0, 1
    first = _first_stop_title(itinerary)
    notification = await service.schedule(
        user_id,
        kind=KIND_PLAN_REMINDER,
        title=translate("reminder.plan.title", preferences.language, title=itinerary.title),
        body=(
            translate(
                "reminder.plan.body",
                preferences.language,
                stop=first,
                time=_local_time(starts, timezone),
            )
            if first
            else None
        ),
        deliver_at=starts - REMINDER_LEAD,
        subject_id=itinerary.id,
        subject_type="itinerary",
        link=link,
        preferences=preferences,
        deadline=starts,
    )
    return (1, 0) if notification is not None else (0, 1)


async def _schedule_trip_reminders(
    service: NotificationService,
    *,
    itinerary: Itinerary,
    user_id: uuid.UUID,
    preferences,
    starts: datetime,
    timezone: str,
    link: str,
    now: datetime,
    horizon: datetime,
) -> tuple[int, int]:
    created = skipped = 0

    # Evening-before: only while the trip has not started yet.
    if starts > now:
        advance_at = _evening_before_start(starts, timezone)
        if now < advance_at <= horizon:
            notification = await service.schedule(
                user_id,
                kind=KIND_PLAN_REMINDER,
                title=translate(
                    "reminder.plan.tomorrow_title",
                    preferences.language,
                    title=itinerary.title,
                ),
                body=translate(
                    "reminder.plan.tomorrow_body",
                    preferences.language,
                    date=_local_date_label(starts, timezone),
                ),
                deliver_at=advance_at,
                subject_id=itinerary.id,
                subject_type="itinerary",
                link=link,
                preferences=preferences,
                deadline=starts,
            )
            if notification is None:
                skipped += 1
            else:
                created += 1
        elif advance_at <= now < starts:
            # Too late for evening-before; fall back to the outing-style lead so
            # a trip kept the morning of day one still gets a heads-up.
            lead_at = starts - REMINDER_LEAD
            if lead_at > now:
                first = _first_stop_title(itinerary, day_index=0)
                notification = await service.schedule(
                    user_id,
                    kind=KIND_PLAN_REMINDER,
                    title=translate(
                        "reminder.plan.title",
                        preferences.language,
                        title=itinerary.title,
                    ),
                    body=(
                        translate(
                            "reminder.plan.body",
                            preferences.language,
                            stop=first,
                            time=_local_time(starts, timezone),
                        )
                        if first
                        else None
                    ),
                    deliver_at=lead_at,
                    subject_id=itinerary.id,
                    subject_type="itinerary",
                    link=link,
                    preferences=preferences,
                    deadline=starts,
                )
                if notification is None:
                    skipped += 1
                else:
                    created += 1

    days_with_stops = sorted(
        {int(getattr(s, "day_index", 0) or 0) for s in itinerary.stops}
    )
    for day_i in days_with_stops:
        if day_i < 0 or day_i >= MAX_TRIP_DAYS:
            continue
        day_at = _day_start(itinerary, day_i, timezone)
        if day_at <= now or day_at > horizon:
            continue
        first = _first_stop_title(itinerary, day_index=day_i)
        first_arrive = _first_stop_arrive(itinerary, day_i)
        stop_time = _local_time(first_arrive or day_at, timezone)
        day_number = day_i + 1
        notification = await service.schedule(
            user_id,
            kind=KIND_PLAN_DAY_REMINDER,
            title=translate(
                "reminder.plan.day_title",
                preferences.language,
                title=itinerary.title,
                day=day_number,
            ),
            body=(
                translate(
                    "reminder.plan.day_body",
                    preferences.language,
                    stop=first,
                    time=stop_time,
                    day=day_number,
                )
                if first
                else translate(
                    "reminder.plan.day_body_bare",
                    preferences.language,
                    day=day_number,
                    time=stop_time,
                )
            ),
            deliver_at=day_at,
            subject_id=plan_day_subject_id(itinerary.id, day_i),
            subject_type="itinerary_day",
            link=link,
            preferences=preferences,
            deadline=(first_arrive or day_at) + timedelta(hours=2),
        )
        if notification is None:
            skipped += 1
        else:
            created += 1

    return created, skipped


async def cancel_plan_reminders(
    session: AsyncSession, itinerary_id: uuid.UUID
) -> int:
    """Withdraw pending advance + day reminders for a deleted or emptied plan."""
    service = NotificationService(session)
    cancelled = await service.cancel_for_subject(itinerary_id)
    for day_i in range(MAX_TRIP_DAYS):
        cancelled += await service.cancel_for_subject(
            plan_day_subject_id(itinerary_id, day_i)
        )
    return cancelled


async def deliver_due(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Mark everything due as delivered.

    Delivery is in-app for now: a notification becomes visible in the explorer's
    inbox. Push and email are transport concerns that belong behind this same
    call, which is why the status transition is separated from the decision to
    send at all.
    """
    service = NotificationService(session)
    due = await service.due(now=now)
    for notification in due:
        await service.mark_delivered(notification)
    await session.flush()
    if due:
        logger.info("notifications_delivered", count=len(due))
    return len(due)
