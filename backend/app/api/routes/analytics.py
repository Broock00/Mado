"""Analytics (spec PUB-006, ANA-001, ANA-002).

Two dashboards, and both only ever show you your own data: a publisher sees how
their listings are doing, an explorer sees a summary of their own activity.
There is no endpoint here that lets one account learn about another.

The spec's analytics section also describes an ingestion pipeline - a collector,
a queue, a processor - behind `POST /analytics/events`. That is not built. Mado
already records the behaviour these dashboards read from, synchronously and
cheaply, through the interaction log; adding a second write path and a queue in
front of it would be infrastructure with nothing yet to justify it. When
client-side product events (impressions, scroll depth) are actually wanted, that
is the point to build the collector, and this module is where it would attach.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import CurrentUser, SessionDep
from app.core.envelope import Envelope
from app.core.errors import NotFoundError
from app.domains.catalog.schemas import CamelModel
from app.domains.explorer.summary import ExplorerSummaryService
from app.domains.publisher.analytics import (
    DEFAULT_WINDOW,
    MIN_RATE_SAMPLE,
    PublisherAnalyticsService,
)
from app.domains.publisher.models import Publisher
from app.domains.trust.reputation import for_publisher

router = APIRouter(tags=["analytics"])


class DayPointOut(CamelModel):
    day: date
    views: int
    saves: int


class ExperienceMetricsOut(CamelModel):
    experience_id: uuid.UUID
    title: str
    status: str
    moderation_status: str
    views: int
    unique_viewers: int
    saves: int
    net_saves: int
    # Null when there were too few views for a ratio to mean anything, rather
    # than 0.0 - which would read as "nobody saves this".
    save_rate: float | None = None
    rating_average: float | None = None
    rating_count: int
    report_count: int


class PublisherAnalyticsOut(CamelModel):
    window_days: int
    total_views: int
    unique_viewers: int
    total_saves: int
    net_saves: int
    save_rate: float | None = None
    published_count: int
    draft_count: int
    withheld_count: int
    review_count: int
    rating_average: float | None = None
    report_count: int
    series: list[DayPointOut]
    experiences: list[ExperienceMetricsOut]
    # Stated in the payload rather than hardcoded in the client, so the number
    # the interface explains is always the one the server applied.
    min_rate_sample: int = MIN_RATE_SAMPLE


@router.get(
    "/analytics/publisher",
    response_model=Envelope[PublisherAnalyticsOut],
    summary="How your listings are doing",
    description=(
        "Aggregates only. A publisher learns how many explorers opened a "
        "listing, never which ones. Impressions, booking clicks and attendance "
        "appear in the specification but are absent here because Mado does not "
        "record them - reporting them as zero would read as 'nobody is "
        "interested' rather than 'this is not counted'."
    ),
)
async def publisher_analytics(
    session: SessionDep,
    user: CurrentUser,
    window: int = Query(default=DEFAULT_WINDOW, description="Days: 7, 30 or 90."),
) -> Envelope[PublisherAnalyticsOut]:
    # Looked up, never created. `personal_publisher` makes one on first call,
    # which is right when someone is about to post and wrong as a side effect of
    # opening a dashboard.
    publisher = (
        await session.execute(select(Publisher).where(Publisher.owner_user_id == user.id))
    ).scalars().first()
    if publisher is None:
        raise NotFoundError(
            "Publish something first - there is nothing to report on yet.",
            code="NO_PUBLISHER",
        )

    overview = await PublisherAnalyticsService(session).overview(
        publisher, window_days=window
    )
    return Envelope(
        data=PublisherAnalyticsOut(
            window_days=overview.window_days,
            total_views=overview.total_views,
            unique_viewers=overview.unique_viewers,
            total_saves=overview.total_saves,
            net_saves=overview.net_saves,
            save_rate=overview.save_rate,
            published_count=overview.published_count,
            draft_count=overview.draft_count,
            withheld_count=overview.withheld_count,
            review_count=overview.review_count,
            rating_average=overview.rating_average,
            report_count=overview.report_count,
            series=[
                DayPointOut(day=p.day, views=p.views, saves=p.saves) for p in overview.series
            ],
            experiences=[
                ExperienceMetricsOut(
                    experience_id=m.experience_id,
                    title=m.title,
                    status=m.status,
                    moderation_status=m.moderation_status,
                    views=m.views,
                    unique_viewers=m.unique_viewers,
                    saves=m.saves,
                    net_saves=m.net_saves,
                    save_rate=m.save_rate,
                    rating_average=m.rating_average,
                    rating_count=m.rating_count,
                    report_count=m.report_count,
                )
                for m in overview.experiences
            ],
        )
    )


# ------------------------------------------------------------------ explorer


class CategoryCountOut(CamelModel):
    slug: str
    name: str
    # A weighted score, not a tally of events. Named `weight` rather than
    # `count` so nothing renders it as "9 times" - the explorer did not do
    # anything nine times.
    weight: int


class ExplorerSummaryOut(CamelModel):
    saved_count: int
    collection_count: int
    plan_count: int
    review_count: int
    explored_count: int
    recent_days: int
    top_categories: list[CategoryCountOut]
    member_since: datetime | None = None
    is_empty: bool


@router.get(
    "/analytics/me",
    response_model=Envelope[ExplorerSummaryOut],
    summary="Your own summary",
    description=(
        "Visible to you and nobody else. The categories are scored with the "
        "same weights that decide your feed, so this is an explanation of what "
        "you are being shown rather than a separate calculation about you."
    ),
)
async def explorer_summary(session: SessionDep, user: CurrentUser) -> Envelope[ExplorerSummaryOut]:
    summary = await ExplorerSummaryService(session).summarise(user)
    return Envelope(
        data=ExplorerSummaryOut(
            saved_count=summary.saved_count,
            collection_count=summary.collection_count,
            plan_count=summary.plan_count,
            review_count=summary.review_count,
            explored_count=summary.explored_count,
            recent_days=summary.recent_days,
            top_categories=[
                CategoryCountOut(slug=c.slug, name=c.name, weight=c.count)
                for c in summary.top_categories
            ],
            member_since=summary.member_since,
            is_empty=summary.is_empty,
        )
    )


class ReputationSignalOut(CamelModel):
    key: str
    label: str
    """-1 to 1. Negative pulled the score down."""
    direction: float
    detail: str


class ReputationOut(CamelModel):
    score: float
    """excellent | good | mixed | poor | provisional"""
    band: str
    is_provisional: bool
    completed_dates: int
    cancelled_dates: int
    ratings: int
    reports: int
    withheld_listings: int
    signals: list[ReputationSignalOut]
    computed_at: datetime | None = None


@router.get(
    "/analytics/reputation",
    response_model=Envelope[ReputationOut],
    summary="Your standing on Mado",
    description=(
        "Yours and nobody else's. There is no endpoint that returns another "
        "publisher's score, because a number explorers cannot interpret and "
        "competitors can watch is worse than no number: it invites working out "
        "what moves it rather than doing the thing it measures.\n\n"
        "Computed rather than assigned. Verification says a moderator confirmed "
        "who you are once; this says what you have done since (spec TRST-004). "
        "It affects where your listings rank and nothing else - it cannot "
        "withhold, suspend or reject anything, which stays with the moderation "
        "queue and a person reading it.\n\n"
        "Recent behaviour counts for more than old, so a bad patch fades. That "
        "is deliberate: a record with no way back gives nobody a reason to "
        "improve."
    ),
)
async def my_reputation(session: SessionDep, user: CurrentUser) -> Envelope[ReputationOut]:
    publisher = (
        await session.execute(select(Publisher).where(Publisher.owner_user_id == user.id))
    ).scalars().first()
    if publisher is None:
        raise NotFoundError(
            "Publish something first - there is nothing to judge yet.",
            code="NO_PUBLISHER",
        )

    # Computed live rather than read from the stored copy. A publisher looking
    # at their own standing after fixing something should see the fix, not the
    # figure from the last scheduled run up to an hour ago. Ranking reads the
    # stored one, which is what the hourly job is for.
    reputation = await for_publisher(session, publisher)

    return Envelope(
        data=ReputationOut(
            score=round(reputation.score, 3),
            band=reputation.band,
            is_provisional=reputation.is_provisional,
            completed_dates=reputation.completed_dates,
            cancelled_dates=reputation.cancelled_dates,
            ratings=reputation.ratings,
            reports=reputation.reports,
            withheld_listings=reputation.withheld_listings,
            signals=[
                ReputationSignalOut(
                    key=s.key, label=s.label, direction=s.direction, detail=s.detail
                )
                for s in reputation.signals
            ],
            computed_at=publisher.reputation_computed_at,
        )
    )
