"""The explorer's own summary (spec ANA-001).

A reflection of what somebody has done in the city, shown to that person and
nobody else.

This is the one dashboard in the product with no business purpose. It does not
drive ranking, it is not sold, and no publisher or administrator can read it.
Which makes it the place where the platform's claim - that personalization is
the explorer's, and visible to them - is either true or is not.

Two consequences fall out of that:

**It reads the same signals ranking reads.** The category counts here are drawn
from the same interaction log that decides what appears in the feed. If the
summary says you are mostly interested in music, that is not a separate
guess - it is the actual basis for what you are being shown, which is what makes
looking at it worth anything.

**It stays quiet when it has nothing to say.** A new explorer gets an honest
empty state rather than a wall of zeroes and an invented "0% complete". Nothing
here is gamified; there are no streaks and no badges, because the point is to
show someone their own record, not to get them to come back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.catalog.models import Category, Experience
from app.domains.explorer.learning import ACTION_WEIGHTS
from app.domains.explorer.models import (
    Collection,
    InteractionEvent,
    Itinerary,
    Review,
    SavedItem,
)
from app.domains.identity.models import User

# How far back the "recently" figures look. Long enough to cover a normal
# rhythm of going out, short enough that it describes now rather than a year
# ago.
RECENT_DAYS = 90

# Categories shown. Beyond a handful this stops being a summary and starts
# being a table.
TOP_CATEGORIES = 5


@dataclass(slots=True)
class CategoryCount:
    slug: str
    name: str
    count: int


@dataclass(slots=True)
class ExplorerSummary:
    saved_count: int
    collection_count: int
    plan_count: int
    review_count: int
    # Distinct experiences opened in the window - the closest honest proxy for
    # "places you looked into", which is not the same as places you went.
    explored_count: int
    recent_days: int
    top_categories: list[CategoryCount] = field(default_factory=list)
    member_since: datetime | None = None

    @property
    def is_empty(self) -> bool:
        """Whether there is anything worth showing yet."""
        return not any(
            (
                self.saved_count,
                self.collection_count,
                self.plan_count,
                self.review_count,
                self.explored_count,
            )
        )


class ExplorerSummaryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def summarise(self, user: User) -> ExplorerSummary:
        since = datetime.now(UTC) - timedelta(days=RECENT_DAYS)

        saved = await self.session.scalar(
            select(func.count(SavedItem.id)).where(SavedItem.user_id == user.id)
        )
        collections = await self.session.scalar(
            select(func.count(Collection.id)).where(
                Collection.user_id == user.id, Collection.deleted_at.is_(None)
            )
        )
        plans = await self.session.scalar(
            select(func.count(Itinerary.id)).where(
                Itinerary.user_id == user.id, Itinerary.deleted_at.is_(None)
            )
        )
        reviews = await self.session.scalar(
            select(func.count(Review.id)).where(
                Review.user_id == user.id, Review.deleted_at.is_(None)
            )
        )
        explored = await self.session.scalar(
            select(func.count(func.distinct(InteractionEvent.entity_id))).where(
                InteractionEvent.user_id == user.id,
                InteractionEvent.entity_type == "experience",
                InteractionEvent.action == "open_details",
                InteractionEvent.occurred_at >= since,
            )
        )

        return ExplorerSummary(
            saved_count=int(saved or 0),
            collection_count=int(collections or 0),
            plan_count=int(plans or 0),
            review_count=int(reviews or 0),
            explored_count=int(explored or 0),
            recent_days=RECENT_DAYS,
            top_categories=await self._top_categories(user, since),
            member_since=user.created_at,
        )

    async def _top_categories(self, user: User, since: datetime) -> list[CategoryCount]:
        """What this explorer keeps coming back to.

        Scored with `learning.ACTION_WEIGHTS` - the same weights that decide
        their feed - rather than a plain row count. That is the whole point of
        showing it: if this said "mostly music" on one basis while the feed
        ranked on another, the summary would be decoration rather than an
        explanation.

        Only the positive actions are counted. Dismissals carry negative weight
        in personalization and are excluded here, because "you dismissed nine
        comedy nights" is not an interest and listing it as one would be
        actively misleading.

        The count returned is a rounded score, not a number of events. It is
        surfaced as relative weight in the interface rather than as "9 times",
        because it is not a tally of anything a person did nine of.
        """
        positive = {action: w for action, w in ACTION_WEIGHTS.items() if w > 0}

        score = func.sum(
            case(
                *[(InteractionEvent.action == a, w) for a, w in positive.items()],
                else_=0.0,
            )
        )
        result = await self.session.execute(
            select(Category.slug, Category.name, score)
            .join(Experience, Experience.id == InteractionEvent.entity_id)
            .join(Category, Category.id == Experience.category_id)
            .where(
                InteractionEvent.user_id == user.id,
                InteractionEvent.entity_type == "experience",
                InteractionEvent.action.in_(list(positive)),
                InteractionEvent.occurred_at >= since,
            )
            .group_by(Category.slug, Category.name)
            .order_by(score.desc())
            .limit(TOP_CATEGORIES)
        )
        return [
            CategoryCount(slug=row[0], name=row[1], count=round(float(row[2] or 0)))
            for row in result
        ]
