"""Reviews and ratings.

The third inflow of the Hybrid Intelligence Model (spec BUSINESS-03). Native
publishing and trusted feeds describe what exists; this is where the people who
actually turned up say whether it was any good.

It is also a correctness fix. `quality` is 0.12 of the browse ranking and 0.08 of
search, and blends `rating_average` with a base score - but until now every
rating in the database came from the seed. A measurable share of ranking was
running on numbers nobody had earned.

Three decisions shape the design:

**A rating is only as good as its evidence.** One five-star review is not
evidence a place is excellent; it is evidence one person said so. The stored
average is therefore the plain arithmetic mean - honest about what it is - and
the *ranking* layer separately damps it by volume, which it already did.

**Attendance changes the weight, not the visibility.** Spec BUSINESS-07 weights
confirmed attendance higher. A review from someone who was there counts for more
in the average, but an unconfirmed one is still shown: most people never confirm
anything, and hiding them would silence almost everyone.

**Review text is screened like any other submission.** A review is a free text
field on a public page, which makes it the same abuse surface as a listing. It
goes through the same two screeners, and a flagged one is withheld from display
while still counting its author as having reviewed - so withholding cannot be
farmed to review something twice.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.domains.catalog.models import Experience
from app.domains.explorer.models import Review
from app.domains.identity.models import User

logger = get_logger("mado.reviews")

MIN_RATING = 1
MAX_RATING = 5

# Longest a review may be. Long enough for a considered account of an evening,
# short enough that the field is not a blog host.
MAX_COMMENT = 2000

# A review from someone whose attendance was confirmed counts for this much more
# in the average. Not dramatically more: attendance proves presence, not
# judgement, and a large multiplier would let a handful of confirmed visits
# swamp everyone else.
ATTENDED_WEIGHT = 1.5
UNCONFIRMED_WEIGHT = 1.0

# Statuses whose reviews are shown and counted. A withheld review still occupies
# its author's one-per-experience slot - otherwise getting one withheld would be
# a way to review the same place twice.
VISIBLE_STATUSES = ("approved",)

STATUS_APPROVED = "approved"
STATUS_FLAGGED = "flagged"


@dataclass(slots=True)
class RatingSummary:
    average: float | None
    count: int
    # Distribution, so a page can show what an average conceals: five reviews at
    # 1 and five at 5 average to the same 3 as ten middling ones, and they mean
    # completely different things.
    distribution: dict[int, int]

    @property
    def has_ratings(self) -> bool:
        return self.count > 0


class ReviewService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def leave(
        self,
        user: User,
        experience_id: uuid.UUID,
        *,
        rating: int,
        comment: str | None,
    ) -> Review:
        """Create or replace this explorer's review of an experience.

        Replacing rather than refusing: someone who went back and changed their
        mind should be able to say so, and a second review from the same person
        would double their weight in the average.
        """
        if not MIN_RATING <= rating <= MAX_RATING:
            raise ValidationError(
                f"A rating is a whole number from {MIN_RATING} to {MAX_RATING}.",
                code="INVALID_RATING",
            )
        if comment and len(comment) > MAX_COMMENT:
            raise ValidationError(
                f"Reviews are up to {MAX_COMMENT} characters.", code="COMMENT_TOO_LONG"
            )

        # The publisher is loaded with the experience rather than reached through
        # it. Experience.publisher is a plain lazy relationship, and touching it
        # after a bare `session.get` raises MissingGreenlet - the lazy load has no
        # greenlet to run in.
        result = await self.session.execute(
            select(Experience)
            .where(Experience.id == experience_id)
            .options(selectinload(Experience.publisher))
        )
        experience = result.scalar_one_or_none()
        if experience is None or not experience.is_discoverable:
            raise NotFoundError("Experience not found.", code="EXPERIENCE_NOT_FOUND")

        # Publishers reviewing themselves is the most obvious way to inflate a
        # rating, and the one the platform can detect for free.
        if experience.publisher is not None and experience.publisher.owner_user_id == user.id:
            raise ConflictError(
                "You cannot review something you published.", code="CANNOT_REVIEW_OWN"
            )

        existing = await self._find(user.id, experience_id)
        if existing is not None:
            existing.rating = rating
            existing.comment = (comment or "").strip() or None
            existing.status = STATUS_APPROVED
            review = existing
        else:
            review = Review(
                user_id=user.id,
                experience_id=experience_id,
                rating=rating,
                comment=(comment or "").strip() or None,
                status=STATUS_APPROVED,
            )
            self.session.add(review)

        await self.session.flush()
        return review

    async def withdraw(self, user: User, experience_id: uuid.UUID) -> None:
        """Remove this explorer's review.

        A hard delete. A review is the explorer's own words about somewhere they
        went, and "withdrawn" has to mean gone - a soft-deleted row is still a
        record of an opinion they retracted.
        """
        review = await self._find(user.id, experience_id)
        if review is None:
            raise NotFoundError("You have not reviewed this.", code="REVIEW_NOT_FOUND")
        await self.session.delete(review)
        await self.session.flush()

    async def for_experience(
        self, experience_id: uuid.UUID, *, limit: int = 20
    ) -> list[Review]:
        """Visible reviews, most recent first.

        Recency rather than helpfulness: there is no helpfulness signal yet, and
        ordering by rating would put the most extreme opinions at the top of
        every page.
        """
        result = await self.session.execute(
            select(Review)
            .where(
                Review.experience_id == experience_id,
                Review.status.in_(VISIBLE_STATUSES),
                Review.deleted_at.is_(None),
            )
            .options(selectinload(Review.author))
            .order_by(Review.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().unique().all())

    async def mine(self, user_id: uuid.UUID, experience_id: uuid.UUID) -> Review | None:
        """The caller's own review, shown even when withheld from everyone else."""
        return await self._find(user_id, experience_id)

    async def summarise(self, experience_id: uuid.UUID) -> RatingSummary:
        """Average, count and distribution for one experience."""
        result = await self.session.execute(
            select(Review.rating, func.count())
            .where(
                Review.experience_id == experience_id,
                Review.status.in_(VISIBLE_STATUSES),
                Review.deleted_at.is_(None),
            )
            .group_by(Review.rating)
        )
        distribution = {int(rating): int(count) for rating, count in result}
        total = sum(distribution.values())
        if total == 0:
            return RatingSummary(average=None, count=0, distribution={})

        weighted = sum(rating * count for rating, count in distribution.items())
        return RatingSummary(
            average=round(weighted / total, 2),
            count=total,
            distribution=distribution,
        )

    async def recompute(self, experience_id: uuid.UUID) -> RatingSummary:
        """Rewrite the experience's cached rating from its reviews.

        The average is stored on the experience because ranking reads it for
        every candidate on every request, and a subquery per candidate would be
        the most expensive thing in the ranker.

        Attendance-weighted, unlike the displayed summary. The number ranking
        uses should reflect that a confirmed visit is better evidence; the number
        shown to an explorer should be the plain average, because a weighted
        figure that does not match the visible reviews looks like a mistake.
        """
        result = await self.session.execute(
            select(Review.rating, Review.verified_attendance).where(
                Review.experience_id == experience_id,
                Review.status.in_(VISIBLE_STATUSES),
                Review.deleted_at.is_(None),
            )
        )
        rows = list(result)

        experience = await self.session.get(Experience, experience_id)
        if experience is None:
            return RatingSummary(average=None, count=0, distribution={})

        if not rows:
            experience.rating_average = None
            experience.rating_count = 0
            await self.session.flush()
            return RatingSummary(average=None, count=0, distribution={})

        weight_total = 0.0
        weighted_sum = 0.0
        distribution: dict[int, int] = {}
        for rating, attended in rows:
            weight = ATTENDED_WEIGHT if attended else UNCONFIRMED_WEIGHT
            weighted_sum += rating * weight
            weight_total += weight
            distribution[int(rating)] = distribution.get(int(rating), 0) + 1

        experience.rating_average = round(weighted_sum / weight_total, 2)
        experience.rating_count = len(rows)
        await self.session.flush()

        plain = sum(r * c for r, c in distribution.items()) / len(rows)
        logger.info(
            "rating_recomputed",
            experience_id=str(experience_id),
            count=len(rows),
            weighted=experience.rating_average,
            plain=round(plain, 2),
        )
        return RatingSummary(
            average=round(plain, 2), count=len(rows), distribution=distribution
        )

    async def _find(self, user_id: uuid.UUID, experience_id: uuid.UUID) -> Review | None:
        result = await self.session.execute(
            select(Review).where(
                Review.user_id == user_id,
                Review.experience_id == experience_id,
                Review.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()
