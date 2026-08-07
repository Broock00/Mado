"""Administration and publisher verification (spec ADM-001, TRST-001).

Both were MVP-priority with nothing behind them. Moderator rights were granted
by a CLI command - meaning shell access to the production host was the only way
to appoint one - and verification_status was a column the seed wrote with no way
to request or grant it.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, Query, Request, status
from pydantic import Field

from app.api.deps import CurrentUser, SessionDep
from app.core import rate_limit
from app.core.envelope import CollectionEnvelope, Envelope
from app.domains.catalog.schemas import CamelModel
from app.domains.trust.administration import (
    AdministrationService,
    require_moderator,
)

router = APIRouter(tags=["administration"])


class AccountOut(CamelModel):
    id: uuid.UUID
    display_name: str
    email: str | None = None
    status: str
    is_moderator: bool
    is_verified: bool
    created_at: datetime
    # The pair an administrator actually weighs. Ten posts and no reports is a
    # contributor; two posts and nine reports is a problem. Neither alone says
    # which.
    published_count: int
    reported_count: int


class SuspendRequest(CamelModel):
    suspended: bool
    reason: str | None = Field(default=None, max_length=500)


class ModeratorRequest(CamelModel):
    moderator: bool


class VerificationRequestIn(CamelModel):
    note: str | None = Field(default=None, max_length=1000)


class PublisherVerificationOut(CamelModel):
    id: uuid.UUID
    name: str
    slug: str
    verification_status: str
    verification_note: str | None = None
    verification_requested_at: datetime | None = None
    trust_level: int


@router.get(
    "/admin/accounts",
    response_model=CollectionEnvelope[AccountOut],
    summary="Find accounts",
    description=(
        "Search by name or email. Returns what a decision needs and nothing "
        "more - an admin console is not a surveillance surface, so nothing about "
        "what someone searched for, planned or was recommended appears here."
    ),
)
async def list_accounts(
    session: SessionDep,
    user: CurrentUser,
    q: str | None = Query(default=None, max_length=200),
    account_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
) -> CollectionEnvelope[AccountOut]:
    require_moderator(user)
    accounts = await AdministrationService(session).list_accounts(
        query=q, status=account_status, limit=limit
    )
    return CollectionEnvelope(data=[AccountOut(**asdict(a)) for a in accounts])


@router.post(
    "/admin/accounts/{user_id}/suspend",
    response_model=Envelope[AccountOut],
    summary="Suspend or restore an account",
    description=(
        "Withholds rather than deletes. The account keeps its posts, saved list "
        "and history; it loses the ability to publish and to be seen. Reversible."
    ),
)
async def suspend_account(
    user_id: uuid.UUID,
    payload: SuspendRequest,
    session: SessionDep,
    user: CurrentUser,
) -> Envelope[AccountOut]:
    require_moderator(user)
    service = AdministrationService(session)
    await service.set_suspended(
        user, user_id, suspended=payload.suspended, reason=payload.reason
    )
    await session.commit()
    return Envelope(data=AccountOut(**asdict(await service.summarise(user_id))))


@router.post(
    "/admin/accounts/{user_id}/moderator",
    response_model=Envelope[AccountOut],
    summary="Grant or remove moderator rights",
)
async def set_moderator(
    user_id: uuid.UUID,
    payload: ModeratorRequest,
    session: SessionDep,
    user: CurrentUser,
) -> Envelope[AccountOut]:
    require_moderator(user)
    service = AdministrationService(session)
    await service.set_moderator(user, user_id, moderator=payload.moderator)
    await session.commit()
    return Envelope(data=AccountOut(**asdict(await service.summarise(user_id))))


# --------------------------------------------------------------- verification


@router.post(
    "/posts/verification",
    response_model=Envelope[PublisherVerificationOut],
    status_code=status.HTTP_201_CREATED,
    summary="Ask to be verified",
    description=(
        "Anyone may ask. A person decides - there is no automatic path to a "
        "verified badge, which is what makes it mean anything."
    ),
)
async def request_verification(
    payload: VerificationRequestIn,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
) -> Envelope[PublisherVerificationOut]:
    await rate_limit.check(
        rate_limit.identify(request, str(user.id)), rate_limit.VERIFICATION_LIMIT
    )
    publisher = await AdministrationService(session).request_verification(
        user, note=payload.note
    )
    await session.commit()
    return Envelope(data=PublisherVerificationOut.model_validate(publisher))


@router.get(
    "/posts/verification",
    response_model=Envelope[PublisherVerificationOut],
    summary="Where your verification request stands",
    description=(
        "The publisher's own view of it. Separate from asking, because a "
        "publisher who has already asked needs to be told they are waiting "
        "rather than shown the button again."
    ),
)
async def my_verification(
    session: SessionDep, user: CurrentUser
) -> Envelope[PublisherVerificationOut]:
    publisher = await AdministrationService(session).my_verification(user)
    return Envelope(data=PublisherVerificationOut.model_validate(publisher))


@router.get(
    "/moderation/verifications",
    response_model=CollectionEnvelope[PublisherVerificationOut],
    summary="Publishers waiting on verification",
)
async def pending_verifications(
    session: SessionDep, user: CurrentUser
) -> CollectionEnvelope[PublisherVerificationOut]:
    require_moderator(user)
    publishers = await AdministrationService(session).pending_verifications()
    return CollectionEnvelope(
        data=[PublisherVerificationOut.model_validate(p) for p in publishers]
    )


class VerificationDecision(CamelModel):
    approve: bool
    note: str | None = Field(default=None, max_length=500)


@router.post(
    "/moderation/verifications/{publisher_id}",
    response_model=Envelope[PublisherVerificationOut],
    summary="Decide a verification request",
    description=(
        "Refusal returns the publisher to unverified rather than a terminal "
        "state, so someone refused for thin evidence can come back with better."
    ),
)
async def decide_verification(
    publisher_id: uuid.UUID,
    payload: VerificationDecision,
    session: SessionDep,
    user: CurrentUser,
) -> Envelope[PublisherVerificationOut]:
    require_moderator(user)
    publisher = await AdministrationService(session).decide_verification(
        user, publisher_id, approve=payload.approve, note=payload.note
    )
    await session.commit()
    return Envelope(data=PublisherVerificationOut.model_validate(publisher))
