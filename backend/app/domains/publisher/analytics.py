"""Publisher analytics (spec PUB-006, ANA-002).

What a publisher gets to know about how their listings are doing, and - equally
deliberately - what they do not.

**Only what is actually recorded.** The spec's metric list runs to impressions,
booking clicks, bookings and attendance. Mado records none of those: there is no
booking flow yet, and impressions would mean a database write for every card
scrolled past, which the interaction log explicitly declines to do. Reporting
them as zero would read as "nobody is interested" rather than "we do not count
this", so they are absent instead. A dashboard that invents numbers is worse
than one that admits its edges.

**Counts, not rates, until a rate means something.** Two saves from three views
is not a 67% save rate, it is three views. Ratios computed over tiny denominators
are the fastest way to make a dashboard lie, so a rate appears only once the
denominator clears :data:`MIN_RATE_SAMPLE`.

**Aggregates, never people.** A publisher learns that forty explorers opened
their listing. They never learn which forty. The queries here return counts and
nothing that could be joined back to a person, and the route layer has no field
to put an identity in even if a future query produced one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Select, String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.domains.catalog.models import Experience
from app.domains.explorer.models import InteractionEvent, Review
from app.domains.publisher.models import Publisher

logger = get_logger("mado.publisher_analytics")

# Actions the interaction log records that mean something to a publisher.
# `open_details` is the closest thing to a view: somebody opened the listing
# itself rather than scrolling past a card.
ACTION_VIEW = "open_details"
ACTION_SAVE = "save"
ACTION_UNSAVE = "unsave"

# Windows offered. Anything longer starts to blur a listing's current form with
# what it said three months ago.
WINDOWS = (7, 30, 90)
DEFAULT_WINDOW = 30

# Below this many views, a save rate is noise dressed as a number.
MIN_RATE_SAMPLE = 20


def _identity():
    """The closest thing to "a person" this platform will commit to.

    The signed-in id where there is one, the client-generated anonymous id
    otherwise. Two devices belonging to the same signed-out explorer count
    twice, and that is the intended trade: stitching them together would mean
    tracking people across devices, which Mado deliberately does not do.
    """
    return func.coalesce(cast(InteractionEvent.user_id, String), InteractionEvent.anonymous_id)


@dataclass(slots=True)
class DayPoint:
    day: date
    views: int
    saves: int


@dataclass(slots=True)
class ExperienceMetrics:
    """One listing's numbers."""

    experience_id: uuid.UUID
    title: str
    status: str
    moderation_status: str
    views: int
    unique_viewers: int
    saves: int
    # Net of unsaves. A listing saved forty times and unsaved thirty-nine is not
    # doing well, and showing only the forty would say it was.
    net_saves: int
    rating_average: float | None
    rating_count: int
    report_count: int

    @property
    def save_rate(self) -> float | None:
        """Saves per view, or None when there is not enough to divide by."""
        if self.views < MIN_RATE_SAMPLE:
            return None
        return round(self.net_saves / self.views, 3)


@dataclass(slots=True)
class PublisherOverview:
    window_days: int
    total_views: int
    unique_viewers: int
    total_saves: int
    net_saves: int
    published_count: int
    draft_count: int
    withheld_count: int
    review_count: int
    rating_average: float | None
    report_count: int
    series: list[DayPoint] = field(default_factory=list)
    experiences: list[ExperienceMetrics] = field(default_factory=list)

    @property
    def save_rate(self) -> float | None:
        if self.total_views < MIN_RATE_SAMPLE:
            return None
        return round(self.net_saves / self.total_views, 3)


class PublisherAnalyticsService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def overview(
        self, publisher: Publisher, *, window_days: int = DEFAULT_WINDOW
    ) -> PublisherOverview:
        window_days = window_days if window_days in WINDOWS else DEFAULT_WINDOW
        since = datetime.now(UTC) - timedelta(days=window_days)

        experiences = list(
            (
                await self.session.execute(
                    select(Experience).where(
                        Experience.publisher_id == publisher.id,
                        Experience.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .unique()
        )
        if not experiences:
            return PublisherOverview(
                window_days=window_days,
                total_views=0,
                unique_viewers=0,
                total_saves=0,
                net_saves=0,
                published_count=0,
                draft_count=0,
                withheld_count=0,
                review_count=0,
                rating_average=None,
                report_count=0,
            )

        ids = [experience.id for experience in experiences]
        counts = await self._action_counts(ids, since)
        uniques = await self._unique_viewers(ids, since)
        series = await self._daily(ids, since, window_days)
        reviews = await self._recent_reviews(ids, since)

        per_experience = [
            ExperienceMetrics(
                experience_id=experience.id,
                title=experience.title,
                status=experience.status,
                moderation_status=experience.moderation_status,
                views=counts.get((experience.id, ACTION_VIEW), 0),
                unique_viewers=uniques.get(experience.id, 0),
                saves=counts.get((experience.id, ACTION_SAVE), 0),
                net_saves=counts.get((experience.id, ACTION_SAVE), 0)
                - counts.get((experience.id, ACTION_UNSAVE), 0),
                rating_average=float(experience.rating_average)
                if experience.rating_average is not None
                else None,
                rating_count=experience.rating_count,
                report_count=experience.report_count,
            )
            for experience in experiences
        ]
        # Busiest first: a publisher opening this wants to know what is working,
        # not to read an alphabetical list.
        per_experience.sort(key=lambda m: (m.views, m.net_saves), reverse=True)

        total_views = sum(m.views for m in per_experience)
        total_saves = sum(m.saves for m in per_experience)
        net_saves = sum(m.net_saves for m in per_experience)

        return PublisherOverview(
            window_days=window_days,
            total_views=total_views,
            # Distinct across the whole window and every listing, so it is not
            # the sum of the per-listing figures - one person who read three of
            # your posts is one person.
            unique_viewers=await self._unique_viewers_total(ids, since),
            total_saves=total_saves,
            net_saves=net_saves,
            published_count=sum(1 for e in experiences if e.status == "published"),
            draft_count=sum(1 for e in experiences if e.status == "draft"),
            withheld_count=sum(
                1 for e in experiences if e.moderation_status in {"flagged", "rejected"}
            ),
            review_count=reviews,
            rating_average=weighted_rating(experiences),
            report_count=sum(e.report_count for e in experiences),
            series=series,
            experiences=per_experience,
        )

    # ----------------------------------------------------------- internals

    def _events(self, ids: list[uuid.UUID], since: datetime) -> Select:
        return select(InteractionEvent).where(
            InteractionEvent.entity_type == "experience",
            InteractionEvent.entity_id.in_(ids),
            InteractionEvent.occurred_at >= since,
        )

    async def _action_counts(
        self, ids: list[uuid.UUID], since: datetime
    ) -> dict[tuple[uuid.UUID, str], int]:
        result = await self.session.execute(
            select(
                InteractionEvent.entity_id,
                InteractionEvent.action,
                func.count(InteractionEvent.id),
            )
            .where(
                InteractionEvent.entity_type == "experience",
                InteractionEvent.entity_id.in_(ids),
                InteractionEvent.occurred_at >= since,
                InteractionEvent.action.in_([ACTION_VIEW, ACTION_SAVE, ACTION_UNSAVE]),
            )
            .group_by(InteractionEvent.entity_id, InteractionEvent.action)
        )
        return {(row[0], row[1]): int(row[2]) for row in result}

    async def _unique_viewers(
        self, ids: list[uuid.UUID], since: datetime
    ) -> dict[uuid.UUID, int]:
        """Distinct people per listing.

        Counted over the signed-in id where there is one and the anonymous id
        otherwise, which is the closest this platform gets to "a person" without
        starting to track people across devices - something it deliberately does
        not do.
        """
        result = await self.session.execute(
            select(InteractionEvent.entity_id, func.count(func.distinct(_identity())))
            .where(
                InteractionEvent.entity_type == "experience",
                InteractionEvent.entity_id.in_(ids),
                InteractionEvent.occurred_at >= since,
                InteractionEvent.action == ACTION_VIEW,
            )
            .group_by(InteractionEvent.entity_id)
        )
        return {row[0]: int(row[1]) for row in result}

    async def _unique_viewers_total(self, ids: list[uuid.UUID], since: datetime) -> int:
        total = await self.session.scalar(
            select(func.count(func.distinct(_identity()))).where(
                InteractionEvent.entity_type == "experience",
                InteractionEvent.entity_id.in_(ids),
                InteractionEvent.occurred_at >= since,
                InteractionEvent.action == ACTION_VIEW,
            )
        )
        return int(total or 0)

    async def _daily(
        self, ids: list[uuid.UUID], since: datetime, window_days: int
    ) -> list[DayPoint]:
        """Views and saves per day, with the empty days filled in.

        Filling matters: a chart that silently omits the days nothing happened
        draws a busy line through a quiet fortnight.
        """
        bucket = func.date_trunc("day", InteractionEvent.occurred_at)
        result = await self.session.execute(
            select(
                bucket,
                InteractionEvent.action,
                func.count(InteractionEvent.id),
            )
            .where(
                InteractionEvent.entity_type == "experience",
                InteractionEvent.entity_id.in_(ids),
                InteractionEvent.occurred_at >= since,
                InteractionEvent.action.in_([ACTION_VIEW, ACTION_SAVE]),
            )
            .group_by(bucket, InteractionEvent.action)
        )
        counts: dict[tuple[date, str], int] = {
            (row[0].date(), row[1]): int(row[2]) for row in result
        }

        today = datetime.now(UTC).date()
        return [
            DayPoint(
                day=day,
                views=counts.get((day, ACTION_VIEW), 0),
                saves=counts.get((day, ACTION_SAVE), 0),
            )
            for day in (
                today - timedelta(days=offset) for offset in range(window_days - 1, -1, -1)
            )
        ]

    async def _recent_reviews(self, ids: list[uuid.UUID], since: datetime) -> int:
        """How many reviews were written in the window.

        Windowed on purpose: "three reviews this month" is news in a way that a
        lifetime total is not.
        """
        count = await self.session.scalar(
            select(func.count(Review.id)).where(
                Review.experience_id.in_(ids),
                Review.created_at >= since,
                Review.deleted_at.is_(None),
                Review.status == "approved",
            )
        )
        return int(count or 0)


def weighted_rating(experiences: list[Experience]) -> float | None:
    """The publisher's overall rating, weighted by how many people rated each listing.

    Read from the listings' own aggregates rather than recomputed from review
    rows, because those aggregates are what every card in the product displays.
    Two different numbers for "your rating" - one on the dashboard, one on the
    card - is the kind of discrepancy that makes a publisher stop believing
    either.

    Weighted rather than a mean of means: a listing rated by two hundred people
    should not count the same as one rated by three.
    """
    rated = [e for e in experiences if e.rating_average is not None and e.rating_count]
    if not rated:
        return None
    total = sum(e.rating_count for e in rated)
    return round(sum(float(e.rating_average) * e.rating_count for e in rated) / total, 2)
