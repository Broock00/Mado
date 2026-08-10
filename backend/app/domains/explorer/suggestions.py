"""Suggestions nobody asked for (spec NOT-002, AI-005).

Everything else in Mado answers a question somebody put to it. This is the one
place the platform speaks first, which makes it the one place where being wrong
is not a bad answer but an interruption - so most of this file is about not
sending things.

**"Nearby" here means their city and the categories they actually like, not
their coordinates.** Mado does not store where an explorer is: the privacy page
says so in as many words, and the ranking pipeline takes a position per request
and throws it away. A notification job runs hours later with nobody present, so
the only honest location it can use is the one they told us - their home city -
and the only honest personalisation is the affinity already derived from what
they opened and saved. A suggestion that said "200m from you" would be a
sentence the platform cannot support without quietly starting to keep a
location history.

**Opted in, and off by default.** `nearby_suggestion` is the one notification
kind that defaults off, because nobody asked for it. This job existed as a
preference with nothing behind it until now, which is its own small dishonesty -
a switch that promises something and delivers silence.

**At most one a week, and only when there is something worth saying.** The
failure mode of proactive anything is that it becomes noise and gets muted, and
a muted channel cannot deliver the one message that mattered. So: a hard
frequency cap, a quality floor on what is worth interrupting somebody for, and
nothing at all if they have been in the app recently - somebody who was here
this morning does not need telling what is on.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging import get_logger
from app.core.messages import translate
from app.domains.catalog.models import STATUS_PUBLISHED, EventInstance, Experience
from app.domains.explorer.learning import infer_preferences
from app.domains.explorer.models import InteractionEvent, SavedItem
from app.domains.explorer.notifications import (
    KIND_NEARBY,
    NotificationService,
    Preferences,
    resolve_preferences,
)
from app.domains.identity.models import User, UserProfile

logger = get_logger("mado.suggestions")

# At most one every seven days. Chosen as the interval at which an unasked-for
# message still reads as thoughtful rather than as a campaign.
COOLDOWN = timedelta(days=7)

# Somebody who opened Mado in the last two days is already looking. Telling them
# what is on is not a service, it is a second copy of the page they just closed.
RECENTLY_ACTIVE = timedelta(days=2)

# How far ahead to look for something to mention. Far enough that they can act
# on it, near enough that it is still "on soon".
HORIZON = timedelta(days=3)

# The bar for interrupting somebody. An experience has to be well liked *and*
# have enough ratings for that to mean anything - suggesting a place with one
# five-star review is the platform passing off a coincidence as a
# recommendation.
MIN_RATING = 4.2
MIN_RATINGS = 8

# How strongly they have to like a category before it is used. Below this the
# affinity is noise and the suggestion is a guess wearing a personalisation
# badge.
MIN_AFFINITY = 0.35

# A suggestion is queued a minute out rather than for "now".
#
# `NotificationService.schedule` refuses anything dated now or earlier, and it
# is right to: that guard stops a reminder being delivered the instant it is
# created, which would turn "remind me three hours before" into "tell me now".
# A suggestion has no event it must precede - it is an offer, and the right
# moment is the next delivery sweep. Asking for one minute ahead expresses that
# without weakening a guard the reminders depend on.
LEAD = timedelta(minutes=1)


@dataclass(slots=True)
class Suggestion:
    user_id: uuid.UUID
    experience: Experience
    occurrence: EventInstance | None
    category_slug: str | None
    preferences: Preferences


async def _recently_active(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> bool:
    seen = await session.scalar(
        select(func.count())
        .select_from(InteractionEvent)
        .where(
            InteractionEvent.user_id == user_id,
            InteractionEvent.occurred_at >= now - RECENTLY_ACTIVE,
        )
    )
    return bool(seen)


async def _already_suggested(
    service: NotificationService, user_id: uuid.UUID, now: datetime
) -> bool:
    return await service.sent_since(user_id, KIND_NEARBY, since=now - COOLDOWN)


async def _candidates(
    session: AsyncSession, city_slug: str, now: datetime
) -> list[tuple[Experience, EventInstance | None]]:
    """Things on soon in their city that are actually worth mentioning."""
    horizon = now + HORIZON

    result = await session.execute(
        select(EventInstance, Experience)
        .join(Experience, Experience.id == EventInstance.experience_id)
        .options(selectinload(Experience.category), selectinload(Experience.venue))
        .join(Experience.city)
        .where(
            EventInstance.start_time >= now,
            EventInstance.start_time <= horizon,
            EventInstance.status != "cancelled",
            Experience.status == STATUS_PUBLISHED,
            Experience.deleted_at.is_(None),
            Experience.rating_average.is_not(None),
            Experience.rating_average >= MIN_RATING,
            Experience.rating_count >= MIN_RATINGS,
        )
        .order_by(EventInstance.start_time)
        .limit(200)
    )

    seen: set[uuid.UUID] = set()
    candidates: list[tuple[Experience, EventInstance | None]] = []
    for occurrence, experience in result.all():
        # One entry per experience. A weekly night with five upcoming dates
        # should not crowd out everything else in the city.
        if experience.id in seen:
            continue
        if experience.city is None or experience.city.slug != city_slug:
            continue
        seen.add(experience.id)
        candidates.append((experience, occurrence))
    return candidates


async def _pick(
    session: AsyncSession,
    user: User,
    profile: UserProfile,
    candidates: list[tuple[Experience, EventInstance | None]],
    saved_ids: set[uuid.UUID],
    now: datetime,
) -> tuple[Experience, EventInstance | None, str] | None:
    """The one thing worth telling this explorer about, or nothing."""
    inferred = await infer_preferences(
        session, user_id=user.id, privacy=profile.privacy, now=now
    )
    liked = {slug: score for slug, score in inferred.categories.items() if score >= MIN_AFFINITY}
    if not liked:
        # No evidence of what they like. Sending the city's most popular thing
        # to everybody is a mailing list, not a suggestion.
        return None

    best: tuple[float, Experience, EventInstance | None, str] | None = None
    for experience, occurrence in candidates:
        if experience.id in saved_ids:
            # They already know about it. Telling somebody about the thing they
            # saved is the platform demonstrating it does not read its own data.
            continue
        category = experience.category.slug if experience.category else None
        if category is None or category in inferred.disliked_categories:
            continue
        affinity = liked.get(category)
        if affinity is None:
            continue

        # Affinity first, rating as the tie-break. Between two things they like
        # equally, the better-reviewed one is the safer interruption.
        score = affinity + float(experience.rating_average or 0) / 100
        if best is None or score > best[0]:
            best = (score, experience, occurrence, category)

    if best is None:
        return None
    return best[1], best[2], best[3]


async def for_explorer(
    session: AsyncSession,
    user: User,
    profile: UserProfile,
    *,
    now: datetime | None = None,
) -> tuple[Experience, EventInstance | None] | None:
    """The one thing worth showing this explorer, or nothing.

    Shared by the notification job and the concierge's opening state on purpose.
    Two separate definitions of "something you would like" would drift, and the
    day they disagree the platform is telling somebody two different things
    about their own taste.

    What is *not* shared is permission to interrupt. The cooldown and the
    recently-active check live in `suggest_nearby`, because they are about
    whether it is reasonable to send an unasked-for message - and showing
    something in a panel the explorer just opened is not an interruption at all.
    """
    now = now or datetime.now(UTC)
    city_slug = profile.home_city_slug
    if not city_slug:
        return None

    candidates = await _candidates(session, city_slug, now)
    if not candidates:
        return None

    saved_ids = {
        row
        for row in (
            await session.execute(select(SavedItem.entity_id).where(SavedItem.user_id == user.id))
        ).scalars()
    }
    picked = await _pick(session, user, profile, candidates, saved_ids, now)
    if picked is None:
        return None
    experience, occurrence, _category = picked
    return experience, occurrence


async def suggest_nearby(session: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """Find explorers worth telling something to, and tell them.

    Returns counts rather than notifications: this runs from the scheduler and
    the only interesting question afterwards is how many went out.
    """
    now = now or datetime.now(UTC)
    service = NotificationService(session)

    profiles = list(
        (
            await session.execute(
                select(UserProfile)
                .join(User, User.id == UserProfile.user_id)
                .where(User.status == "active", User.deleted_at.is_(None))
                .options(selectinload(UserProfile.user))
            )
        ).scalars()
    )

    considered = sent = 0

    for profile in profiles:
        preferences = resolve_preferences(profile)
        if not preferences.wants(KIND_NEARBY):
            continue

        city_slug = profile.home_city_slug
        if not city_slug:
            # No stated city and no stored location, so there is no honest way
            # to say "near you". Skipped rather than guessed at.
            continue

        user = profile.user
        if user is None:
            continue

        considered += 1
        if await _recently_active(session, user.id, now):
            continue
        if await _already_suggested(service, user.id, now):
            continue

        picked = await for_explorer(session, user, profile, now=now)
        if picked is None:
            continue

        experience, occurrence = picked
        notification = await service.schedule(
            user.id,
            kind=KIND_NEARBY,
            title=translate(
                "suggestion.nearby.title", preferences.language, title=experience.title
            ),
            body=translate(
                "suggestion.nearby.body_at_venue"
                if experience.venue
                else "suggestion.nearby.body",
                preferences.language,
                venue=experience.venue.name if experience.venue else "",
                city=city_slug.replace("-", " ").title(),
            ),
            # On the next sweep rather than timed to the event: this is "you
            # might like this", not a reminder. Quiet hours are still applied
            # inside `schedule`, so an overnight run waits until morning.
            deliver_at=now + LEAD,
            subject_id=experience.id,
            subject_type="experience",
            link=f"/experiences/{experience.id}",
            preferences=preferences,
            deadline=occurrence.start_time if occurrence else None,
        )
        if notification is not None:
            sent += 1

    logger.info("nearby_suggestions", considered=considered, sent=sent)
    return {"considered": considered, "sent": sent}
