"""Reviews and ratings.

The third content inflow (spec BUSINESS-03): the people who turned up saying
whether it was worth turning up for. Also the fix for a real defect - `quality`
is a weighted ranking signal that blends `rating_average`, and until these routes
existed every rating in the database came from the seed.

Reviews are screened exactly as listings are. A review is free text on a public
page, so it is the same abuse surface, and a trusted-looking channel is precisely
where unscreened text does the most damage.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Query, Request, status
from pydantic import Field

from app.api.deps import CurrentUser, OptionalUser, SessionDep
from app.core import rate_limit
from app.core.envelope import Envelope
from app.core.logging import get_logger
from app.domains.catalog import repository as catalog_repo
from app.domains.catalog.schemas import CamelModel
from app.domains.discovery.indexer import index_experience
from app.domains.explorer.reviews import (
    MAX_COMMENT,
    MAX_RATING,
    MIN_RATING,
    STATUS_APPROVED,
    STATUS_FLAGGED,
    ReviewService,
)
from app.domains.trust.service import TrustService

router = APIRouter(tags=["reviews"])
logger = get_logger("mado.reviews.api")


class ReviewRequest(CamelModel):
    rating: int = Field(ge=MIN_RATING, le=MAX_RATING)
    comment: str | None = Field(default=None, max_length=MAX_COMMENT)


class ReviewOut(CamelModel):
    id: uuid.UUID
    rating: int
    comment: str | None = None
    author_name: str
    verified_attendance: bool
    created_at: datetime
    # Only ever set on the caller's own review. An explorer whose words were
    # withheld should know, rather than concluding the platform lost them.
    status: str | None = None
    moderation_notes: str | None = None


class RatingSummaryOut(CamelModel):
    average: float | None = None
    count: int
    # The spread behind the average. Five 1s and five 5s average to the same 3
    # as ten 3s, and they describe completely different places.
    distribution: dict[int, int] = Field(default_factory=dict)


class ReviewsOut(CamelModel):
    summary: RatingSummaryOut
    reviews: list[ReviewOut]
    # The caller's own, whatever its status, so the interface can offer to edit
    # rather than showing an empty form over a review they already wrote.
    mine: ReviewOut | None = None


def _to_out(review, *, include_status: bool = False, author=None) -> ReviewOut:
    """Shape a review for the client.

    ``author`` is passed explicitly when the caller already has the user - which
    it does immediately after writing a review. Reaching for `review.author`
    there would lazy-load a relationship on a freshly constructed object, and a
    lazy load during response serialisation raises MissingGreenlet with no
    application frame in the traceback to point at.
    """
    author = author or getattr(review, "author", None)
    profile = getattr(author, "profile", None)
    return ReviewOut(
        id=review.id,
        rating=review.rating,
        comment=review.comment,
        author_name=(profile.display_name if profile else None) or "An explorer",
        verified_attendance=review.verified_attendance,
        created_at=review.created_at,
        status=review.status if include_status else None,
        moderation_notes=review.moderation_notes if include_status else None,
    )


@router.get(
    "/experiences/{experience_id}/reviews",
    response_model=Envelope[ReviewsOut],
    summary="Reviews for an experience",
)
async def list_reviews(
    experience_id: uuid.UUID,
    session: SessionDep,
    user: OptionalUser,
    limit: int = Query(default=20, ge=1, le=100),
) -> Envelope[ReviewsOut]:
    service = ReviewService(session)
    reviews = await service.for_experience(experience_id, limit=limit)
    summary = await service.summarise(experience_id)

    mine = await service.mine(user.id, experience_id) if user else None
    return Envelope(
        data=ReviewsOut(
            summary=RatingSummaryOut(
                average=summary.average,
                count=summary.count,
                distribution=summary.distribution,
            ),
            # The caller's own is shown separately, so it is not listed twice.
            reviews=[_to_out(r) for r in reviews if not mine or r.id != mine.id],
            mine=_to_out(mine, include_status=True) if mine else None,
        )
    )


@router.put(
    "/experiences/{experience_id}/reviews",
    response_model=Envelope[ReviewOut],
    status_code=status.HTTP_201_CREATED,
    summary="Leave or update a review",
    description=(
        "One review per explorer per experience. Sending a second replaces the "
        "first rather than being refused - someone who went back and changed "
        "their mind should be able to say so."
    ),
)
async def leave_review(
    experience_id: uuid.UUID,
    payload: ReviewRequest,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
) -> Envelope[ReviewOut]:
    await rate_limit.check(rate_limit.identify(request, str(user.id)), rate_limit.REVIEW_LIMIT)

    service = ReviewService(session)
    review = await service.leave(
        user, experience_id, rating=payload.rating, comment=payload.comment
    )

    # Screened like any other submission. Withheld text still occupies the
    # author's one-review slot, so getting one flagged is not a way to review
    # the same place twice.
    if review.comment:
        result = await TrustService(session).screen_review(review)
        if result.needs_review:
            review.status = STATUS_FLAGGED
            review.moderation_notes = result.as_note()
        else:
            review.status = STATUS_APPROVED

    summary = await service.recompute(experience_id)

    # The rating is a search-ranking input, so the index has to hear about it.
    #
    # Loaded through the repository, not with a bare session.get: to_document
    # reads the venue, city, category, tags, publisher and events, and every one
    # of those is a lazy relationship that raises MissingGreenlet when touched on
    # an object that was not loaded with them.
    experience = await catalog_repo.get_experience(session, experience_id)
    if experience is not None and experience.is_discoverable:
        await index_experience(experience)

    await session.commit()
    logger.info(
        "review_left",
        experience_id=str(experience_id),
        rating=payload.rating,
        status=review.status,
        new_average=summary.average,
    )
    return Envelope(data=_to_out(review, include_status=True, author=user))


@router.delete(
    "/experiences/{experience_id}/reviews",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Withdraw your review",
)
async def withdraw_review(
    experience_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUser,
) -> None:
    service = ReviewService(session)
    await service.withdraw(user, experience_id)
    await service.recompute(experience_id)
    await session.commit()
