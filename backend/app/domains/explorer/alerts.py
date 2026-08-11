"""Telling explorers when something they were counting on changes (spec NOT-003).

The rest of the notification system is about things that are *going to happen*:
a reminder three hours before, a suggestion for the weekend. This is the other
direction - something an explorer arranged their evening around is no longer
happening, and the value of saying so decays to nothing at the moment they set
off.

Three groups have a claim on a date, in descending order of how much it costs
them to find out late:

* **Reserved** - they hold places. They may have arranged the evening, the taxi
  and the people around it, and their places are being given back here.
* **Planned** - it is a stop in an itinerary, so the rest of that plan is now
  wrong too.
* **Saved** - an intention, not a commitment.

Each person is told once, using the strongest claim they have. Somebody who
reserved *and* saved is a reserver; sending both would read as two separate
cancellations of the same thing.

**Withdrawing comes before sending.** The pending "starts tonight" reminder for
this date has to be cancelled, and :meth:`NotificationService.cancel_for_subject`
cancels everything pending about a subject regardless of kind - including this
alert, which shares the subject. Schedule first and the alert cancels itself,
silently, and the explorer hears nothing at all. Order is load-bearing; the
tests pin it.

Nothing here is inferred or scheduled speculatively: a publisher cancelled a
date, which is a fact, and these are the people who asked to be told about that
date. That keeps it on the right side of BUSINESS-07 - a human decided, the
platform only carried the news.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging import get_logger
from app.core.messages import translate
from app.domains.explorer.models import Itinerary, ItineraryStop, SavedItem
from app.domains.explorer.notifications import (
    KIND_TRAVEL_ALERT,
    REMINDER_LEAD,
    NotificationService,
    Preferences,
    resolve_preferences,
)
from app.domains.explorer.reservations import ReservationService
from app.domains.identity.models import User

logger = get_logger("mado.alerts")

# How an explorer is connected to the date, strongest first. The order is the
# tie-break when somebody appears in more than one group.
RESERVED = "reserved"
PLANNED = "planned"
SAVED = "saved"
CLAIMS = (RESERVED, PLANNED, SAVED)

# How close a plan stop with no occurrence recorded has to be to the cancelled
# start before it counts as the same sitting. Half a day: wide enough to catch a
# plan whose times drifted while it was being edited, narrow enough that the
# Tuesday of a weekly event is not the Wednesday.
NEAR_STOP = timedelta(hours=12)


@dataclass(slots=True)
class Told:
    """What one announcement did. Returned for logging and for the tests."""

    reserved: int = 0
    planned: int = 0
    saved: int = 0
    withdrawn: int = 0
    places_released: int = 0
    skipped: int = 0

    @property
    def notified(self) -> int:
        return self.reserved + self.planned + self.saved


def _local_when(when: datetime, preferences: Preferences) -> str:
    """The date and time in the explorer's own zone.

    Reminders print only a time, because a reminder is always about tonight. A
    cancellation is not: a weekly night has four dates and only one of them is
    off, so the day has to be in there or the explorer cannot tell which.

    English gets a written weekday and month. Amharic gets numbers, because the
    alternative is `strftime` splicing "Tue" and "Aug" into an Amharic sentence
    - half-translated is worse than plain. Gregorian either way, matching the
    rest of the platform rather than introducing a second calendar in a
    notification.
    """
    try:
        local = when.astimezone(ZoneInfo(preferences.timezone))
    except Exception:  # noqa: BLE001 - an unknown zone must not lose an alert
        local = when.astimezone(UTC)
    return local.strftime("%d/%m, %H:%M" if preferences.language == "am" else "%a %d %b, %H:%M")


def _body(claim: str, *, preferences: Preferences, when: str, reason: str | None) -> str:
    sentence = translate(f"alert.cancelled.{claim}", preferences.language, when=when)
    # Stripped before the test, not after: a reason of spaces is a publisher who
    # left the box alone, and it would otherwise print "The organiser said:"
    # followed by nothing.
    reason = (reason or "").strip()
    if not reason:
        return sentence
    because = translate("alert.cancelled.because", preferences.language, reason=reason)
    return f"{sentence} {because}"


async def _reserved(session: AsyncSession, occurrence_id: uuid.UUID) -> tuple[set[uuid.UUID], int]:
    """Cancel the reservations on this date and report who held them."""
    released = await ReservationService(session).release_for_occurrence(occurrence_id)
    return {r.user_id for r in released}, sum(r.party_size for r in released)


async def _planned(
    session: AsyncSession,
    *,
    occurrence_id: uuid.UUID,
    experience_id: uuid.UUID,
    starts_at: datetime,
) -> set[uuid.UUID]:
    """Explorers with this date as a stop in a plan.

    Matched on the occurrence where a stop records one. A stop that does not -
    planned around the experience rather than a specific sitting - counts only
    if the plan puts the explorer there within ``NEAR_STOP`` of the cancelled
    start. Without that window, cancelling one night of a weekly event would
    alert everybody who ever planned around any night of it, and being told
    about the wrong date is how somebody stops reading these.
    """
    result = await session.execute(
        select(Itinerary.user_id)
        .join(ItineraryStop, ItineraryStop.itinerary_id == Itinerary.id)
        .where(
            Itinerary.user_id.is_not(None),
            Itinerary.deleted_at.is_(None),
            (ItineraryStop.event_instance_id == occurrence_id)
            | (
                ItineraryStop.event_instance_id.is_(None)
                & (ItineraryStop.experience_id == experience_id)
                & (ItineraryStop.arrive_at >= starts_at - NEAR_STOP)
                & (ItineraryStop.arrive_at <= starts_at + NEAR_STOP)
            ),
        )
        .distinct()
    )
    return {row[0] for row in result}


async def _saved(session: AsyncSession, experience_id: uuid.UUID) -> set[uuid.UUID]:
    result = await session.execute(
        select(SavedItem.user_id)
        .where(SavedItem.entity_type == "experience", SavedItem.entity_id == experience_id)
        .distinct()
    )
    return {row[0] for row in result}


async def announce_cancellation(
    session: AsyncSession,
    *,
    experience_id: uuid.UUID,
    occurrence_id: uuid.UUID,
    title: str,
    starts_at: datetime,
    reason: str | None = None,
    now: datetime | None = None,
) -> Told:
    """Tell everybody who was counting on a date that it is off.

    Takes plain values rather than catalog rows on purpose: this is the explorer
    domain, the caller is the publisher domain, and passing the four facts that
    matter keeps the boundary at the call rather than inside a lazy-loaded
    relationship.
    """
    now = now or datetime.now(UTC)
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=UTC)

    service = NotificationService(session)
    told = Told()

    # First, before anything is scheduled - see the module docstring.
    told.withdrawn = await service.cancel_for_subject(occurrence_id)

    holders, told.places_released = await _reserved(session, occurrence_id)

    if starts_at <= now:
        # The date has already come and gone. Places are still handed back and
        # the stale reminder is still withdrawn, but there is nobody left to
        # warn: "do not travel" about last Tuesday is noise, and noise is how an
        # explorer learns to swipe the next one away unread.
        logger.info(
            "cancellation_not_announced",
            occurrence_id=str(occurrence_id),
            reason="already_started",
            withdrawn=told.withdrawn,
        )
        return told

    planners = await _planned(
        session,
        occurrence_id=occurrence_id,
        experience_id=experience_id,
        starts_at=starts_at,
    )
    savers = await _saved(session, experience_id)

    # One entry per person, strongest claim wins.
    claims: dict[uuid.UUID, str] = {}
    for claim, group in ((RESERVED, holders), (PLANNED, planners), (SAVED, savers)):
        for user_id in group:
            claims.setdefault(user_id, claim)

    if not claims:
        return told

    users = {
        user.id: user
        for user in (
            await session.execute(
                select(User).where(User.id.in_(claims)).options(selectinload(User.profile))
            )
        ).scalars()
    }

    for user_id, claim in claims.items():
        user = users.get(user_id)
        if user is None:
            told.skipped += 1
            continue

        preferences = resolve_preferences(getattr(user, "profile", None))
        notification = await service.schedule(
            user_id,
            kind=KIND_TRAVEL_ALERT,
            title=translate("alert.cancelled.title", preferences.language, title=title),
            body=_body(
                claim,
                preferences=preferences,
                when=_local_when(starts_at, preferences),
                reason=reason,
            ),
            # Now, not at some point before the event: this is the one kind of
            # notification whose whole value is arriving before the explorer
            # acts on stale information.
            deliver_at=now,
            immediate=True,
            subject_id=occurrence_id,
            subject_type="event_instance",
            link=f"/experiences/{experience_id}",
            preferences=preferences,
            # Quiet hours may hold this until morning, but never past the point
            # where the reminder would have gone out - after that it is arriving
            # while they are getting ready to leave.
            deadline=starts_at - REMINDER_LEAD,
        )
        if notification is None:
            told.skipped += 1
            continue
        setattr(told, claim, getattr(told, claim) + 1)

    logger.info(
        "cancellation_announced",
        occurrence_id=str(occurrence_id),
        experience_id=str(experience_id),
        reserved=told.reserved,
        planned=told.planned,
        saved=told.saved,
        skipped=told.skipped,
        withdrawn=told.withdrawn,
        places_released=told.places_released,
    )
    return told
