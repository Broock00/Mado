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
from sqlalchemy import select

from app.api.deps import CurrentUser, SessionDep
from app.core import rate_limit
from app.core.envelope import CollectionEnvelope, Envelope
from app.core.errors import ValidationError
from app.core.logging import get_request_id
from app.domains.catalog.schemas import CamelModel
from app.domains.commerce.payouts import PayoutService
from app.domains.developer import usage
from app.domains.publisher import plans
from app.domains.publisher.models import Publisher
from app.domains.publisher.subscriptions import SubscriptionService
from app.domains.trust.administration import (
    AdministrationService,
    require_moderator,
)
from app.domains.trust.audit import AuditLog, window_since
from app.domains.trust.flags import FlagService

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


# ------------------------------------------------------- flags and the record


class FlagOut(CamelModel):
    key: str
    description: str
    enabled: bool
    rollout_percentage: int
    updated_at: datetime | None = None


class CreateFlagRequest(CamelModel):
    key: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=1, max_length=500)


class UpdateFlagRequest(CamelModel):
    enabled: bool | None = None
    rollout_percentage: int | None = Field(default=None, ge=0, le=100)
    description: str | None = Field(default=None, max_length=500)


class AuditEntryOut(CamelModel):
    id: uuid.UUID
    actor_label: str
    action: str
    subject_type: str
    subject_id: uuid.UUID | None = None
    subject_label: str | None = None
    reason: str | None = None
    context: dict = {}
    occurred_at: datetime


@router.get(
    "/flags",
    response_model=CollectionEnvelope[FlagOut],
    summary="Every feature flag",
    description="Moderators only. Explorers get resolved values from /me/flags.",
)
async def list_flags(session: SessionDep, user: CurrentUser) -> CollectionEnvelope[FlagOut]:
    require_moderator(user)
    flags = await FlagService(session).all()
    return CollectionEnvelope(
        data=[
            FlagOut(
                key=f.key,
                description=f.description,
                enabled=f.enabled,
                rollout_percentage=f.rollout_percentage,
                updated_at=f.updated_at,
            )
            for f in flags
        ]
    )


@router.post(
    "/flags",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[FlagOut],
    summary="Register a flag",
    description="Created switched off - a flag that arrives on has shipped the feature.",
)
async def create_flag(
    payload: CreateFlagRequest,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
) -> Envelope[FlagOut]:
    require_moderator(user)
    flag = await FlagService(session).create(
        user, payload.key, payload.description, request_id=get_request_id()
    )
    view = FlagOut(
        key=flag.key,
        description=flag.description,
        enabled=flag.enabled,
        rollout_percentage=flag.rollout_percentage,
        updated_at=flag.updated_at,
    )
    await session.commit()
    return Envelope(data=view)


@router.patch(
    "/flags/{key}",
    response_model=Envelope[FlagOut],
    summary="Turn a flag on, off, or partly on",
)
async def update_flag(
    key: str,
    payload: UpdateFlagRequest,
    session: SessionDep,
    user: CurrentUser,
) -> Envelope[FlagOut]:
    require_moderator(user)
    flag = await FlagService(session).set(
        user,
        key,
        enabled=payload.enabled,
        rollout_percentage=payload.rollout_percentage,
        description=payload.description,
        request_id=get_request_id(),
    )
    view = FlagOut(
        key=flag.key,
        description=flag.description,
        enabled=flag.enabled,
        rollout_percentage=flag.rollout_percentage,
        updated_at=flag.updated_at,
    )
    await session.commit()
    return Envelope(data=view)


@router.get(
    "/admin/audit",
    response_model=CollectionEnvelope[AuditEntryOut],
    summary="What administrators have done",
    description=(
        "Every administrative action: suspensions, moderator rights, "
        "verification rulings, moderation decisions and flag changes. Append "
        "only - there is no endpoint that edits or removes an entry.\n\n"
        "This records authority being used, not people being watched. Nothing "
        "an explorer does appears here."
    ),
)
async def audit_trail(
    session: SessionDep,
    user: CurrentUser,
    action: str | None = Query(default=None),
    subject: uuid.UUID | None = Query(default=None),
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=100, ge=1, le=500),
) -> CollectionEnvelope[AuditEntryOut]:
    require_moderator(user)
    entries = await AuditLog(session).recent(
        action=action, subject_id=subject, since=window_since(days), limit=limit
    )
    return CollectionEnvelope(data=[AuditEntryOut.model_validate(e) for e in entries])


# ------------------------------------------------------------------- payouts
#
# Mado sells through its own merchant account, so every ticket payment arrives
# here and the publisher's share is a debt. These three endpoints are how that
# debt is seen, batched and marked settled. None of them moves money: a transfer
# is made by a person, in a bank, and recorded here afterwards.


class OwingOut(CamelModel):
    publisher_id: uuid.UUID
    publisher_name: str | None = None
    currency: str
    net_minor: int
    sales: int


class AdminPayoutOut(CamelModel):
    id: uuid.UUID
    publisher_id: uuid.UUID
    currency: str
    total_minor: int
    entry_count: int
    period_start: datetime
    period_end: datetime
    status: str
    paid_at: datetime | None = None
    reference: str | None = None
    note: str | None = None


class BuildPayoutRequest(CamelModel):
    publisher_id: uuid.UUID
    currency: str = Field(min_length=3, max_length=3)
    # Cuts the batch at a date, so a run on the 1st settles last month without
    # sweeping in this morning's sales.
    up_to: datetime | None = None


class MarkPaidRequest(CamelModel):
    # Required, not optional. The value of the record is that it reconciles
    # against a bank statement, and one with nothing to match is a claim.
    reference: str = Field(min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=300)


@router.get(
    "/admin/payouts/owing",
    response_model=CollectionEnvelope[OwingOut],
    summary="What Mado owes, by publisher and currency",
    description=(
        "Grouped by currency because a business selling in two countries earns "
        "in two, and one figure adding them together would mean nothing."
    ),
)
async def payouts_owing(session: SessionDep, user: CurrentUser) -> CollectionEnvelope[OwingOut]:
    require_moderator(user)
    rows = await PayoutService(session).outstanding()
    names = await _publisher_names(session, [publisher_id for publisher_id, _, _, _ in rows])
    return CollectionEnvelope(
        data=[
            OwingOut(
                publisher_id=publisher_id,
                publisher_name=names.get(publisher_id),
                currency=currency,
                net_minor=total,
                sales=count,
            )
            for publisher_id, currency, total, count in rows
        ]
    )


@router.post(
    "/admin/payouts",
    response_model=Envelope[AdminPayoutOut],
    status_code=status.HTTP_201_CREATED,
    summary="Batch what is owed to one publisher into a payout",
)
async def build_payout(
    payload: BuildPayoutRequest, session: SessionDep, user: CurrentUser
) -> Envelope[AdminPayoutOut]:
    require_moderator(user)
    payout = await PayoutService(session).build(
        payload.publisher_id, payload.currency, up_to=payload.up_to
    )
    view = AdminPayoutOut.model_validate(payout)
    await session.commit()
    return Envelope(data=view)


@router.post(
    "/admin/payouts/{payout_id}/paid",
    response_model=Envelope[AdminPayoutOut],
    summary="Record that a payout was actually sent",
    description=(
        "Mado does not disburse: this marks a transfer somebody made, with "
        "their reference. Marking one paid twice is not an error - two people "
        "confirming the same transfer is ordinary, and the first reference is "
        "the one that matches the bank."
    ),
)
async def mark_payout_paid(
    payout_id: uuid.UUID,
    payload: MarkPaidRequest,
    session: SessionDep,
    user: CurrentUser,
) -> Envelope[AdminPayoutOut]:
    require_moderator(user)
    payout = await PayoutService(session).mark_paid(
        payout_id, reference=payload.reference, note=payload.note
    )
    view = AdminPayoutOut.model_validate(payout)
    await session.commit()
    return Envelope(data=view)


# --------------------------------------------------------- publisher plans
#
# Enterprise is negotiated rather than bought - BUSINESS-06 says so, and
# `plans.py` carries no price for it precisely because a number here would be
# one the sales conversation contradicts. This is how an agreement reached in
# that conversation is actually applied. Without it, enterprise was a tier
# nobody could be put on: a plan that exists in the catalogue, is rendered on the
# upgrade page, and that no code path could ever grant.


class GrantedPlanOut(CamelModel):
    publisher_id: uuid.UUID
    plan: str
    plan_name: str
    status: str
    current_period_end: datetime | None = None


class GrantPlanRequest(CamelModel):
    plan: str
    # Days the agreement runs for. Null leaves no end date, which is what an
    # open-ended arrangement actually is - it ends when somebody ends it, not
    # when a date passes unnoticed.
    days: int | None = Field(default=None, ge=1, le=3650)


@router.put(
    "/admin/publisher-plans/{publisher_id}",
    response_model=Envelope[GrantedPlanOut],
    summary="Put a business on a plan without a payment",
    description=(
        "For negotiated agreements, invoiced outside Mado, and for putting "
        "something right. Nothing is charged here and no invoice is written — "
        "the subscription carries no price, so the invoices and the row cannot "
        "disagree about what was paid."
    ),
)
async def grant_publisher_plan(
    publisher_id: uuid.UUID,
    payload: GrantPlanRequest,
    session: SessionDep,
    user: CurrentUser,
) -> Envelope[GrantedPlanOut]:
    require_moderator(user)
    subscription = await SubscriptionService(session).grant(
        publisher_id, plan_key=payload.plan, days=payload.days
    )
    view = GrantedPlanOut(
        publisher_id=publisher_id,
        plan=subscription.plan,
        plan_name=plans.get(subscription.plan).name,
        status=subscription.status,
        current_period_end=subscription.current_period_end,
    )
    await session.commit()
    return Envelope(data=view)


# --------------------------------------------------------- developer plans
#
# API plans are sold by conversation rather than self-service - BUSINESS-90.01
# §13 has usage-based pricing and enterprise contracts, neither of which is a
# button. This is how an account is actually put on one. Without it `pro` and
# `enterprise` would be plans nobody could be on, which is the same lie as a
# scope nothing enforces.


class DeveloperPlanOut(CamelModel):
    user_id: uuid.UUID
    plan: str
    plan_name: str
    monthly_allowance: int | None = None
    calls_this_month: int


class SetDeveloperPlanRequest(CamelModel):
    plan: str
    # An allowance agreed with one customer, overriding the plan's. Enterprise
    # contracts are negotiated, and encoding each one as a new plan would mean a
    # deploy per customer.
    monthly_calls_override: int | None = Field(default=None, ge=0)


@router.put(
    "/admin/developer-plans/{user_id}",
    response_model=Envelope[DeveloperPlanOut],
    summary="Put an account on an API plan",
)
async def set_developer_plan(
    user_id: uuid.UUID,
    payload: SetDeveloperPlanRequest,
    session: SessionDep,
    user: CurrentUser,
) -> Envelope[DeveloperPlanOut]:
    require_moderator(user)
    if payload.plan not in usage.DEVELOPER_PLANS:
        raise ValidationError(f"There is no {payload.plan} API plan.", code="UNKNOWN_PLAN")

    account = await usage.account_for(session, user_id)
    if account is None:
        account = usage.DeveloperAccount(user_id=user_id, plan=payload.plan)
        session.add(account)
    account.plan = payload.plan
    account.monthly_calls_override = payload.monthly_calls_override
    await session.flush()

    view = DeveloperPlanOut(
        user_id=user_id,
        plan=account.plan,
        plan_name=usage.plan_for(account.plan).name,
        monthly_allowance=await usage.allowance_for(session, user_id),
        calls_this_month=await usage.calls_this_month(session, user_id),
    )
    await session.commit()
    return Envelope(data=view)


async def _publisher_names(
    session: SessionDep, publisher_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """One lookup for the whole page, rather than one per row."""
    if not publisher_ids:
        return {}
    result = await session.execute(
        select(Publisher.id, Publisher.name).where(Publisher.id.in_(set(publisher_ids)))
    )
    return {publisher_id: name for publisher_id, name in result.all()}
