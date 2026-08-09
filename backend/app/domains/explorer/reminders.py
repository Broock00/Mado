"""Building event reminders from what explorers saved and planned.

Separated from :mod:`app.domains.explorer.notifications`, which owns *what a
notification is*. This owns *which ones are worth creating* - a product question
rather than a mechanical one, and the part that changes most.

Two sources, both meaning "I intend to be there":

* **Saved experiences** with an upcoming occurrence.
* **Itinerary stops** that are time-bound.

Run repeatedly and idempotently: the unique constraint on
(user, kind, subject) means a scheduler firing every half hour produces one
reminder per event, not one per run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging import get_logger
from app.core.messages import translate
from app.domains.catalog.models import EventInstance
from app.domains.explorer.models import Itinerary, SavedItem
from app.domains.explorer.notifications import (
    KIND_EVENT_REMINDER,
    KIND_PLAN_REMINDER,
    REMINDER_LEAD,
    NotificationService,
    resolve_preferences,
)
from app.domains.identity.models import User

logger = get_logger("mado.reminders")

# How far ahead to look. Anything beyond this will be picked up by a later run,
# and scheduling months out means holding rows for things that will change.
HORIZON = timedelta(days=7)


def _local_time(when: datetime, timezone: str) -> str:
    from zoneinfo import ZoneInfo

    try:
        return when.astimezone(ZoneInfo(timezone)).strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return when.strftime("%H:%M")


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
    """Remind an explorer about a kept itinerary before it starts.

    One reminder for the whole plan rather than one per stop: an explorer who
    planned an evening wants to be told the evening is starting, not pinged four
    times as they move through it.
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
            Itinerary.starts_at > now,
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

        preferences = resolve_preferences(getattr(user, "profile", None))
        starts = itinerary.starts_at
        if starts.tzinfo is None:
            starts = starts.replace(tzinfo=UTC)

        first = itinerary.stops[0] if itinerary.stops else None
        notification = await service.schedule(
            user.id,
            kind=KIND_PLAN_REMINDER,
            title=translate("reminder.plan.title", preferences.language, title=itinerary.title),
            body=(
                translate(
                    "reminder.plan.body",
                    preferences.language,
                    stop=first.title,
                    time=_local_time(starts, preferences.timezone),
                )
                if first
                else None
            ),
            deliver_at=starts - REMINDER_LEAD,
            subject_id=itinerary.id,
            subject_type="itinerary",
            link=f"/plans/{itinerary.id}",
            preferences=preferences,
            deadline=starts,
        )
        if notification is None:
            skipped += 1
        else:
            created += 1

    await session.flush()
    logger.info("plan_reminders_scheduled", created=created, skipped=skipped)
    return {"created": created, "skipped": skipped}


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
