"""Trust and safety endpoints (spec BUSINESS-07).

Reporting is available to anyone who can see the content, including signed-out
explorers - the person best placed to spot a fake listing is often the one who
turned up and found nothing there, and requiring an account first loses that
signal.

Moderation decisions require a moderator. There is no self-service takedown.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request, status

from app.api.deps import CurrentUser, OptionalUser, SessionDep
from app.core import rate_limit
from app.core.envelope import CollectionEnvelope, Envelope
from app.core.errors import PermissionDeniedError
from app.domains.catalog.models import Experience
from app.domains.discovery.indexer import index_experience, remove_experience
from app.domains.identity.models import User
from app.domains.publisher.schemas import (
    ModerationDecisionRequest,
    ModerationItemOut,
    ReportOut,
    ReportRequest,
)
from app.domains.trust.service import TrustService

router = APIRouter(tags=["trust & safety"])


def _require_moderator(user: User) -> User:
    """Gate moderation actions.

    Reads a dedicated column rather than anything the explorer can write. No API
    grants it; it is set out of band. Spec 54.02 defines a fuller role model, and
    when that lands this stays the single place that changes.
    """
    if not user.is_moderator:
        raise PermissionDeniedError(
            "Moderation is restricted to platform moderators.", code="NOT_A_MODERATOR"
        )
    return user


@router.post(
    "/experiences/{experience_id}/report",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[ReportOut],
    summary="Report content",
    description=(
        "Flags an experience for review. Reports are advisory: they can withhold "
        "content pending human review but never delete it or suspend an account."
    ),
)
async def report_experience(
    experience_id: uuid.UUID,
    payload: ReportRequest,
    session: SessionDep,
    user: OptionalUser,
    request: Request,
) -> Envelope[ReportOut]:
    await rate_limit.check(
        rate_limit.identify(request, str(user.id) if user else None), rate_limit.REPORT_LIMIT
    )
    trust = TrustService(session)
    report = await trust.report(
        experience_id=experience_id,
        reporter=user,
        reason=payload.reason,
        detail=payload.detail,
    )

    # Drop it from the search index immediately if the report withheld it, so a
    # flagged listing stops appearing while it waits for review.
    experience = await session.get(Experience, experience_id)
    if experience is not None and not experience.is_discoverable:
        await remove_experience(str(experience_id))

    view = ReportOut.model_validate(report)
    await session.commit()
    return Envelope(data=view)


@router.get(
    "/moderation/queue",
    response_model=CollectionEnvelope[ModerationItemOut],
    summary="Moderation queue",
    description="Content awaiting a human decision, most-reported first.",
)
async def moderation_queue(
    user: CurrentUser,
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> CollectionEnvelope[ModerationItemOut]:
    _require_moderator(user)
    items = await TrustService(session).queue(limit=limit)
    return CollectionEnvelope(
        data=[
            ModerationItemOut(
                id=item.id,
                title=item.title,
                summary=item.summary,
                publisher_name=item.publisher.name if item.publisher else None,
                moderation_status=item.moderation_status,
                moderation_notes=item.moderation_notes,
                report_count=item.report_count or 0,
                risk_score=float(item.risk_score or 0),
                city_slug=item.city.slug if item.city else None,
                created_at=item.created_at,
            )
            for item in items
        ]
    )


@router.post(
    "/moderation/{experience_id}/decide",
    response_model=Envelope[ModerationItemOut],
    summary="Record a moderation decision",
    description=(
        "Approving or rejecting locks the outcome so automated screening cannot "
        "later overturn a person's judgement."
    ),
)
async def decide(
    experience_id: uuid.UUID,
    payload: ModerationDecisionRequest,
    user: CurrentUser,
    session: SessionDep,
) -> Envelope[ModerationItemOut]:
    moderator = _require_moderator(user)
    experience = await TrustService(session).decide(
        experience_id=experience_id,
        moderator=moderator,
        approve=payload.approve,
        note=payload.note,
    )

    if experience.is_discoverable:
        await index_experience(experience)
    else:
        await remove_experience(str(experience_id))

    await session.commit()
    return Envelope(
        data=ModerationItemOut(
            id=experience.id,
            title=experience.title,
            summary=experience.summary,
            publisher_name=experience.publisher.name if experience.publisher else None,
            moderation_status=experience.moderation_status,
            moderation_notes=experience.moderation_notes,
            report_count=experience.report_count or 0,
            risk_score=float(experience.risk_score or 0),
            city_slug=experience.city.slug if experience.city else None,
            created_at=experience.created_at,
        )
    )
