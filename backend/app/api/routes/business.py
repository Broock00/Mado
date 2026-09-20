"""Business accounts and their teams.

**An account is a person or a business, never both.** Registration is unchanged;
shortly afterwards the account answers which it is. Choosing business asks for
the details and converts the account - from then on it *is* the business: the
profile page, the name on everything it posts, and what other explorers see.
There is no personal profile alongside it and no second login, because the same
credentials sign in to the same account.

That is why there is no "create a business" here and no list of businesses you
own. There is one account, and `/me/account-type` says what it is.

Conversion is one way. Turning back would leave published listings, reviews of
them and any tickets sold attributed to a business that no longer exists.

Every route below goes through `assert_can_manage`, which is the one gate. Routes
do not decide authorization for themselves, because the failure mode of a
scattered check is not a visible bug - it is the one endpoint that forgot.

The explorer-facing half is at the bottom: a public profile by slug, deliberately
a different payload from what the team sees. An owner needs verification state
and contact details; somebody deciding where to have dinner needs the pictures
and what is on.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, File, Form, Request, UploadFile, status
from pydantic import Field
from sqlalchemy import select

from app.api.deps import CurrentUser, OptionalUser, SessionDep
from app.core import rate_limit
from app.core.config import get_settings
from app.core.envelope import CollectionEnvelope, Envelope
from app.core.errors import NotFoundError, PermissionDeniedError, ValidationError
from app.domains.catalog.schemas import CamelModel, ExperienceSummary
from app.domains.catalog.serializers import to_summary
from app.domains.commerce import fees
from app.domains.commerce.payouts import PayoutService
from app.domains.promotion import models as promotion_models
from app.domains.promotion import service as promotion_service
from app.domains.promotion.service import PromotionService
from app.domains.publisher import business as business_vocab
from app.domains.publisher import entitlements, permissions, plans
from app.domains.publisher.models import (
    GALLERY_IMAGE,
    GALLERY_VIDEO,
    GalleryItem,
    Publisher,
    PublisherMember,
)
from app.domains.publisher.schemas import (
    AccountTypeOut,
    AddGalleryItemRequest,
    BusinessOut,
    ChangeRoleRequest,
    CreateBusinessRequest,
    GalleryItemOut,
    InvitationOut,
    InviteMemberRequest,
    MemberOut,
    PublishingIdentityOut,
    RoleOut,
    UpdateBusinessRequest,
)
from app.domains.publisher.service import PublishingService
from app.domains.publisher.subscriptions import SubscriptionService
from app.integrations import media_storage, payments

router = APIRouter(tags=["business"])


def _view(publisher: Publisher) -> BusinessOut:
    return BusinessOut(
        id=publisher.id,
        name=publisher.name,
        slug=publisher.slug,
        type=publisher.type,
        business_type=publisher.business_type,
        business_type_label=(
            business_vocab.label(publisher.business_type) if publisher.business_type else None
        ),
        description=publisher.description,
        industry=publisher.industry,
        website=publisher.website,
        contact=publisher.contact or {},
        social=publisher.social or {},
        logo_url=publisher.logo_url,
        cover_url=publisher.cover_url,
        verification_status=publisher.verification_status,
        trust_level=publisher.trust_level,
        created_at=publisher.created_at,
    )


def _gallery_view(item: GalleryItem) -> GalleryItemOut:
    return GalleryItemOut(
        id=item.id,
        kind=item.kind,
        url=item.url,
        caption=item.caption,
        sort_order=item.sort_order,
        width=item.width,
        height=item.height,
        content_type=item.content_type,
        created_at=item.created_at,
    )


def _member_view(member: PublisherMember, *, display_name: str | None = None) -> MemberOut:
    return MemberOut(
        id=member.id,
        role=member.role,
        role_label=permissions.label(member.role),
        status=member.status,
        user_id=member.user_id,
        display_name=display_name,
        invited_email=member.invited_email,
        invited_at=member.invited_at,
        expires_at=member.expires_at,
        permissions=sorted(permissions.granted_to(member.role)),
    )


# ------------------------------------------------------------------ businesses


@router.get(
    "/businesses/roles",
    response_model=CollectionEnvelope[RoleOut],
    summary="Roles that can be assigned to a team member",
    description=(
        "Owner is absent on purpose: ownership is a property of the business, not "
        "a role, and cannot be granted by inviting somebody."
    ),
)
async def list_roles() -> CollectionEnvelope[RoleOut]:
    return CollectionEnvelope(
        data=[
            RoleOut(
                value=role,
                label=permissions.label(role),
                permissions=sorted(permissions.granted_to(role)),
            )
            for role in permissions.ASSIGNABLE_ROLES
        ]
    )


@router.get(
    "/me/account-type",
    response_model=Envelope[AccountTypeOut],
    summary="Whether this account is a person or a business",
    description=(
        "`chosen` is false when the account has never answered - it predates the "
        "question, or has just registered. The interface asks once on that basis; "
        "the type itself defaults to individual so nothing is ever in limbo."
    ),
)
async def my_account_type(
    user: CurrentUser, session: SessionDep
) -> Envelope[AccountTypeOut]:
    business = await PublishingService(session).business_of(user)
    return Envelope(
        data=AccountTypeOut(
            account_type=user.account_type,
            chosen=user.account_type_chosen,
            business=_view(business) if business else None,
        )
    )


@router.post(
    "/me/account-type/individual",
    response_model=Envelope[AccountTypeOut],
    summary="Say this account is a person",
    description="Records the answer so you are not asked again. Nothing else changes.",
)
async def choose_individual(
    user: CurrentUser, session: SessionDep
) -> Envelope[AccountTypeOut]:
    await PublishingService(session).choose_individual(user)
    view = AccountTypeOut(
        account_type=user.account_type, chosen=True, business=None
    )
    await session.commit()
    return Envelope(data=view)


@router.post(
    "/me/account-type/business",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[AccountTypeOut],
    summary="Turn this account into a business",
    description=(
        "One way, and once. From here the account **is** the business: its profile, "
        "the name on everything it posts, and what other explorers see. There is no "
        "personal profile left alongside it, and no second login - the same "
        "credentials sign in to the same account.\n\n"
        "Anything posted as a person before converting stays attributed to that "
        "person. Rewriting it to claim the business wrote it would misstate who was "
        "accountable at the time."
    ),
)
async def become_business(
    payload: CreateBusinessRequest, user: CurrentUser, session: SessionDep
) -> Envelope[AccountTypeOut]:
    publisher = await PublishingService(session).become_business(
        user,
        name=payload.name,
        business_type=payload.business_type,
        description=payload.description,
        website=payload.website,
        contact=payload.contact,
        social=payload.social,
        logo_url=payload.logo_url,
        cover_url=payload.cover_url,
    )
    view = AccountTypeOut(
        account_type=user.account_type, chosen=True, business=_view(publisher)
    )
    await session.commit()
    return Envelope(data=view)


@router.get(
    "/me/publishing-identities",
    response_model=CollectionEnvelope[PublishingIdentityOut],
    summary="What you may post as",
    description=(
        "Usually one entry, and the composer then offers no choice - an individual "
        "posts as themselves, a business as itself. A second appears only for "
        "somebody invited to another business, who genuinely does have two."
    ),
)
async def publishing_identities(
    user: CurrentUser, session: SessionDep
) -> CollectionEnvelope[PublishingIdentityOut]:
    found = await PublishingService(session).publishing_identities(user)
    return CollectionEnvelope(
        data=[
            PublishingIdentityOut(
                id=publisher.id,
                name=publisher.name,
                type=publisher.type,
                logo_url=publisher.logo_url,
                is_default=is_default,
            )
            for publisher, is_default in found
        ]
    )


@router.get(
    "/businesses/{business_id}",
    response_model=Envelope[BusinessOut],
    summary="Read a business you manage",
)
async def get_business(
    business_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Envelope[BusinessOut]:
    service = PublishingService(session)
    publisher = await service.assert_can_manage(
        user, business_id, permission=permissions.PROFILE_VIEW
    )
    return Envelope(data=_view(publisher))


@router.patch(
    "/businesses/{business_id}",
    response_model=Envelope[BusinessOut],
    summary="Edit a business",
)
async def update_business(
    business_id: uuid.UUID,
    payload: UpdateBusinessRequest,
    user: CurrentUser,
    session: SessionDep,
) -> Envelope[BusinessOut]:
    changes = payload.model_dump(exclude_unset=True)
    publisher = await PublishingService(session).update_business(user, business_id, changes)
    view = _view(publisher)
    await session.commit()
    return Envelope(data=view)


@router.get(
    "/businesses/{business_id}/permissions",
    response_model=Envelope[list[str]],
    summary="What you may do to this business",
    description=(
        "So the interface can hide what the caller cannot do. This is a "
        "convenience, never the enforcement - every action is checked again "
        "server-side when it is attempted."
    ),
)
async def my_permissions(
    business_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Envelope[list[str]]:
    held = await PublishingService(session).permissions_for(user, business_id)
    if not held:
        raise NotFoundError("Business not found.", code="PUBLISHER_NOT_FOUND")
    return Envelope(data=sorted(held))


# ----------------------------------------------------------------------- plans


class EntitlementsOut(CamelModel):
    """Null means no limit, and never zero — see `plans.py`."""

    max_live_listings: int | None = None
    max_team_members: int | None = None
    analytics_window_days: int
    ai_assistant: bool
    api_access: bool


class PlanOut(CamelModel):
    key: str
    name: str
    tagline: str
    entitlements: EntitlementsOut
    # What this plan costs in the currency asked for, or null where it is not
    # sold in that currency. Never a converted figure: a price arrived at
    # through this morning's exchange rate is one nobody decided to charge.
    price_minor: int | None = None
    currency: str | None = None
    purchasable: bool


class SubscriptionOut(CamelModel):
    # What is in force. Not what was last clicked: an account buying an upgrade
    # keeps the plan it is still paying for until the new one is paid.
    plan: str
    plan_name: str
    status: str
    current_period_end: datetime | None = None
    # What is being bought, while a purchase is waiting on the provider.
    pending_plan: str | None = None
    # Where to go to pay. Cleared the moment the purchase settles, so a stale
    # link cannot be followed into a second charge.
    checkout_url: str | None = None


class PlansOut(CamelModel):
    current: SubscriptionOut
    plans: list[PlanOut]
    # What these prices are quoted in, resolved from the business's own city
    # unless it asked for something else.
    currency: str
    # Every currency a plan is priced in, so the interface can offer a change
    # without hard-coding a list that drifts from the price lists.
    sold_in: list[str]
    # Who takes the money in that currency. See `PromotionPricingOut.provider`.
    provider: str


class StartSubscriptionRequest(CamelModel):
    plan: str
    # Omitted means "whatever you quoted me", which is resolved from the
    # business's city - so a client that forgets cannot silently buy in birr.
    currency: str | None = Field(default=None, min_length=3, max_length=3)


async def _currency_for(session, publisher_id: uuid.UUID, asked: str | None) -> str:
    """What to quote this business in.

    Its own city decides, not the client and not a constant. CLAUDE.md already
    records this going wrong once - "every plan used to quote ETB in every
    city" - and it went wrong the same way here, because the client sent a
    default of ETB and the server believed it. A business in London seeing birr
    is being shown a price it cannot act on.

    The city comes from where the business actually operates: its venues first,
    then its listings. A brand new business has neither, and falls back to the
    deployment's default city rather than guessing from an IP address - a wrong
    guess here quotes somebody a price in a currency they do not use, which is
    worse than a familiar default they can change.

    An explicit `?currency=` still wins, so a business selling across a border
    can ask. It is honoured only where a plan is actually priced in it: the
    alternative is quoting a converted figure, which is a number nobody decided
    to charge.
    """
    if asked:
        wanted = asked.upper()
        if wanted in plans.sold_in():
            return wanted

    from app.domains.catalog.models import City, Experience, Venue

    for model in (Venue, Experience):
        found = (
            await session.execute(
                select(City.currency)
                .join(model, model.city_id == City.id)
                .where(model.publisher_id == publisher_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if found and found.upper() in plans.sold_in():
            return found.upper()

    fallback = (
        await session.execute(
            select(City.currency).where(City.slug == get_settings().default_city_slug)
        )
    ).scalar_one_or_none()
    return (fallback or "ETB").upper()


def _plan_out(plan: plans.Plan, currency: str) -> PlanOut:
    return PlanOut(
        key=plan.key,
        name=plan.name,
        tagline=plan.tagline,
        entitlements=EntitlementsOut(**asdict(plan.entitlements)),
        price_minor=plans.price_for(plan.key, currency),
        currency=currency if plan.prices_minor else None,
        purchasable=plans.is_purchasable(plan.key),
    )


def _subscription_out(subscription, effective: plans.Plan) -> SubscriptionOut:
    """What the account is on **now**, which is not always what the row says.

    `effective` comes from `entitlements.plan_of`, which reads through the
    period end - so an account whose month ran out yesterday is shown as free
    today without anything having had to run on a timer to say so. When the two
    disagree the status is reported as active, because free is not `past_due`:
    the row's status describes the subscription that lapsed, and repeating it
    against the free plan would say something untrue about the free plan.
    """
    if subscription is None:
        return SubscriptionOut(
            plan=effective.key, plan_name=effective.name, status=plans.STATUS_ACTIVE
        )
    return SubscriptionOut(
        plan=effective.key,
        plan_name=effective.name,
        status=(
            subscription.status
            if effective.key == subscription.plan
            else plans.STATUS_ACTIVE
        ),
        current_period_end=(
            subscription.current_period_end if effective.key == subscription.plan else None
        ),
        pending_plan=subscription.pending_plan,
        checkout_url=subscription.checkout_url,
    )


@router.get(
    "/businesses/{business_id}/plans",
    response_model=Envelope[PlansOut],
    summary="What this business is on, and what it could be on",
    description=(
        "Asks the provider what happened when a purchase is still waiting, so "
        "somebody who has just come back from paying gets an answer rather than "
        "a spinner waiting on a webhook — the same thing `GET /orders/{id}` "
        "does for a ticket."
    ),
)
async def business_plans(
    business_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    currency: str | None = None,
) -> Envelope[PlansOut]:
    await PublishingService(session).assert_can_manage(
        user, business_id, permission=permissions.PROFILE_VIEW
    )
    service = SubscriptionService(session)
    subscription = await service.of(business_id)

    # Only while something is actually being bought. Verifying on every read
    # would spend a provider call each time anybody opened the dashboard, and
    # `settle` returns immediately when there is no pending purchase anyway -
    # this guard is about not paying for the round trip, not about correctness.
    #
    # Here rather than left to the client calling `/plans/settle`: a webhook
    # cannot reach a development machine, so the return leg is the only path,
    # and a separate endpoint the interface has to remember to call is one it
    # eventually does not - which is exactly how this shipped stuck at pending.
    if subscription is not None and subscription.pending_plan is not None:
        subscription = await service.settle(subscription)
        await session.commit()

    effective = await entitlements.plan_of(session, business_id)
    quoted = await _currency_for(session, business_id, currency)
    return Envelope(
        data=PlansOut(
            current=_subscription_out(subscription, effective),
            plans=[_plan_out(plans.PLANS[key], quoted) for key in plans.PLAN_ORDER],
            currency=quoted,
            sold_in=plans.sold_in(),
            provider=payments.provider_for(quoted).name,
        )
    )


@router.post(
    "/businesses/{business_id}/plans",
    response_model=Envelope[SubscriptionOut],
    status_code=status.HTTP_201_CREATED,
    summary="Buy a plan",
    description=(
        "Returns a checkout URL. Nothing is granted until the payment provider "
        "confirms the money arrived — a browser returning with `?status=success` "
        "proves only that it can follow a link.\n\n"
        "A period is prepaid and does not renew itself. Nothing in this "
        "deployment can charge a second time, and a subscription that renewed "
        "into a period nobody paid for would read as active while no money ever "
        "moved."
    ),
)
async def start_subscription(
    business_id: uuid.UUID,
    payload: StartSubscriptionRequest,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
) -> Envelope[SubscriptionOut]:
    # Buying is spending the business's money, so it takes the same permission
    # as seeing what it earned rather than the one for editing the profile.
    publisher = await PublishingService(session).assert_can_manage(
        user, business_id, permission=permissions.FINANCE_VIEW
    )
    settings = get_settings()
    service = SubscriptionService(session)
    subscription = await service.start(
        user,
        publisher,
        plan_key=payload.plan,
        currency=await _currency_for(session, business_id, payload.currency),
        return_url=f"{settings.web_base_url.rstrip('/')}/businesses/{business_id}/manage",
        # Resolved from the request rather than from a setting, the same way
        # checkout does it: the app can sit behind a proxy on a different host
        # from the one the publisher is looking at.
        callback_url=str(request.url_for("payment_callback")),
    )
    effective = await entitlements.plan_of(session, business_id)
    view = _subscription_out(subscription, effective)
    await session.commit()
    return Envelope(data=view)


@router.post(
    "/businesses/{business_id}/plans/settle",
    response_model=Envelope[SubscriptionOut],
    summary="Check whether a plan payment came through",
    description=(
        "The return leg, kept because a webhook can be minutes late and "
        "somebody staring at a spinner deserves an answer. It verifies with the "
        "provider rather than believing the browser."
    ),
)
async def settle_subscription(
    business_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Envelope[SubscriptionOut]:
    await PublishingService(session).assert_can_manage(
        user, business_id, permission=permissions.FINANCE_VIEW
    )
    service = SubscriptionService(session)
    subscription = await service.of(business_id)
    if subscription is None:
        raise NotFoundError("There is nothing to settle.", code="NO_SUBSCRIPTION")
    subscription = await service.settle(subscription)
    effective = await entitlements.plan_of(session, business_id)
    view = _subscription_out(subscription, effective)
    await session.commit()
    return Envelope(data=view)


@router.post(
    "/businesses/{business_id}/plans/simulate",
    response_model=Envelope[SubscriptionOut],
    summary="Settle a stub plan payment (development only)",
    description=(
        "The counterpart of `/payments/simulate/{reference}` for subscriptions, "
        "and it exists for the same reason: the stub provider refuses to invent "
        "a payment, so development has to say one happened. Refused outright "
        "unless the stub is the configured provider."
    ),
)
async def simulate_subscription_payment(
    business_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Envelope[SubscriptionOut]:
    await PublishingService(session).assert_can_manage(
        user, business_id, permission=permissions.FINANCE_VIEW
    )
    service = SubscriptionService(session)
    subscription = await service.of(business_id)
    if subscription is None or subscription.reference is None:
        raise NotFoundError("There is nothing to settle.", code="NO_SUBSCRIPTION")

    provider = payments.provider_named(subscription.provider)
    if not isinstance(provider, payments.StubPayments):
        raise PermissionDeniedError(
            "Payments are handled by a real provider here.", code="NOT_SIMULATED"
        )
    try:
        provider.settle(subscription.reference, paid=True)
    except payments.PaymentError as exc:
        raise ValidationError(str(exc), code="NO_STUB_PAYMENT") from exc

    subscription = await service.settle(subscription)
    effective = await entitlements.plan_of(session, business_id)
    view = _subscription_out(subscription, effective)
    await session.commit()
    return Envelope(data=view)


@router.delete(
    "/businesses/{business_id}/plans",
    response_model=Envelope[SubscriptionOut],
    summary="Cancel a plan",
    description=(
        "The period already paid for is not cut short. Keeping the money and "
        "withdrawing the thing it bought would be theft with extra steps."
    ),
)
async def cancel_subscription(
    business_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Envelope[SubscriptionOut]:
    await PublishingService(session).assert_can_manage(
        user, business_id, permission=permissions.FINANCE_VIEW
    )
    subscription = await SubscriptionService(session).cancel(business_id)
    effective = await entitlements.plan_of(session, business_id)
    view = _subscription_out(subscription, effective)
    await session.commit()
    return Envelope(data=view)


# ------------------------------------------------------------------ promotions


class PromotionOut(CamelModel):
    id: uuid.UUID
    experience_id: uuid.UUID
    experience_title: str | None = None
    city_slug: str | None = None
    starts_at: datetime
    ends_at: datetime
    status: str
    amount_minor: int
    currency: str
    checkout_url: str | None = None
    # Reporting only. Nothing about what is shown depends on these, which is
    # what stops the counter becoming a reason to show something.
    impressions: int = 0
    clicks: int = 0


class StartPromotionRequest(CamelModel):
    experience_id: uuid.UUID
    days: int = Field(ge=promotion_models.MIN_DAYS, le=promotion_models.MAX_DAYS)
    currency: str = Field(default="ETB", min_length=3, max_length=3)
    # Where it reaches: a city, or a point and a radius. One of the two is
    # required - a promotion with no place would either reach nowhere or reach
    # everywhere, and the second is one business standing in front of a planet.
    city_slug: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    radius_km: float | None = Field(default=None, gt=0, le=200)


class PromotionPricingOut(CamelModel):
    currency: str
    # Null where promotions are not sold in this currency. Never a converted
    # figure - the same rule the plan prices follow.
    daily_minor: int | None = None
    min_days: int
    max_days: int
    # Every currency a promotion is priced in, so a switcher is never a stale
    # list written beside the prices it is meant to describe.
    sold_in: list[str]
    # Who will actually take the money, which currency decides: Chapa settles
    # birr and Stripe settles the rest. Named by the server because
    # `provider_for` is the one thing that knows, and a client reimplementing
    # that rule would eventually disagree with it - telling somebody they are
    # paying by card and then sending them to Chapa.
    provider: str


@router.get(
    "/businesses/{business_id}/promotions/pricing",
    response_model=Envelope[PromotionPricingOut],
    summary="What a promoted slot costs this business",
    description=(
        "So the dialog can show a total before anybody is sent to pay. A price "
        "somebody discovers on the payment page is a price they did not agree "
        "to.\n\n"
        "Quoted in the business's own city currency, the same way a plan is — "
        "an account should never be shown two prices in two different monies."
    ),
)
async def promotion_pricing(
    business_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    currency: str | None = None,
) -> Envelope[PromotionPricingOut]:
    # Under the business rather than beside it, and behind the same gate. As a
    # bare `/promotions/pricing?businessId=`, the id never bound at all -
    # FastAPI reads a query parameter by its declared name, and the camelCase
    # aliasing that makes request *bodies* work does not apply - so every
    # business silently got the fallback currency while the plans page beside it
    # quoted them correctly.
    await PublishingService(session).assert_can_manage(
        user, business_id, permission=permissions.PROFILE_VIEW
    )
    quoted = await _currency_for(session, business_id, currency)
    return Envelope(
        data=PromotionPricingOut(
            currency=quoted,
            daily_minor=promotion_service.DAILY_PRICE_MINOR.get(quoted),
            min_days=promotion_models.MIN_DAYS,
            max_days=promotion_models.MAX_DAYS,
            sold_in=promotion_service.sold_in(),
            provider=payments.provider_for(quoted).name,
        )
    )


@router.get(
    "/businesses/{business_id}/promotions",
    response_model=CollectionEnvelope[PromotionOut],
    summary="Promotions this business has bought",
)
async def list_promotions(
    business_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> CollectionEnvelope[PromotionOut]:
    await PublishingService(session).assert_can_manage(
        user, business_id, permission=permissions.FINANCE_VIEW
    )
    service = PromotionService(session)
    rows = await service.for_publisher(business_id)
    titles = await _experience_titles(session, [row.experience_id for row in rows])

    out = []
    for row in rows:
        impressions, clicks = await service.performance(row.id)
        out.append(
            PromotionOut(
                id=row.id,
                experience_id=row.experience_id,
                experience_title=titles.get(row.experience_id),
                city_slug=row.city_slug,
                starts_at=row.starts_at,
                ends_at=row.ends_at,
                status=row.status,
                amount_minor=row.amount_minor,
                currency=row.currency,
                checkout_url=row.checkout_url,
                impressions=impressions,
                clicks=clicks,
            )
        )
    return CollectionEnvelope(data=out)


@router.post(
    "/businesses/{business_id}/promotions",
    response_model=Envelope[PromotionOut],
    status_code=status.HTTP_201_CREATED,
    summary="Promote one of this business's posts",
    description=(
        "Buys one clearly-labelled slot in discovery for a number of days. It "
        "does not change how anything is ranked: the promoted card is inserted "
        "second, never first, and only where the listing would have been "
        "allowed to appear anyway — it still has to match the place, the "
        "filters and every accessibility or dietary requirement the explorer "
        "asked for. Nothing runs until the payment provider confirms."
    ),
)
async def start_promotion(
    business_id: uuid.UUID,
    payload: StartPromotionRequest,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
) -> Envelope[PromotionOut]:
    publisher = await PublishingService(session).assert_can_manage(
        user, business_id, permission=permissions.FINANCE_VIEW
    )
    settings = get_settings()
    promotion = await PromotionService(session).start(
        user,
        publisher,
        experience_id=payload.experience_id,
        days=payload.days,
        currency=payload.currency,
        city_slug=payload.city_slug,
        latitude=payload.latitude,
        longitude=payload.longitude,
        radius_km=payload.radius_km,
        return_url=f"{settings.web_base_url.rstrip('/')}/businesses/{business_id}/manage",
        callback_url=str(request.url_for("payment_callback")),
    )
    view = PromotionOut(
        id=promotion.id,
        experience_id=promotion.experience_id,
        city_slug=promotion.city_slug,
        starts_at=promotion.starts_at,
        ends_at=promotion.ends_at,
        status=promotion.status,
        amount_minor=promotion.amount_minor,
        currency=promotion.currency,
        checkout_url=promotion.checkout_url,
    )
    await session.commit()
    return Envelope(data=view)


@router.post(
    "/businesses/{business_id}/promotions/{promotion_id}/simulate",
    response_model=Envelope[PromotionOut],
    summary="Settle a stub promotion payment (development only)",
)
async def simulate_promotion_payment(
    business_id: uuid.UUID,
    promotion_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
) -> Envelope[PromotionOut]:
    await PublishingService(session).assert_can_manage(
        user, business_id, permission=permissions.FINANCE_VIEW
    )
    service = PromotionService(session)
    promotion = await session.get(promotion_models.Promotion, promotion_id)
    if promotion is None or promotion.publisher_id != business_id or not promotion.reference:
        raise NotFoundError("Promotion not found.", code="PROMOTION_NOT_FOUND")

    provider = payments.provider_named(promotion.provider)
    if not isinstance(provider, payments.StubPayments):
        raise PermissionDeniedError(
            "Payments are handled by a real provider here.", code="NOT_SIMULATED"
        )
    try:
        provider.settle(promotion.reference, paid=True)
    except payments.PaymentError as exc:
        raise ValidationError(str(exc), code="NO_STUB_PAYMENT") from exc

    promotion = await service.settle(promotion)
    view = PromotionOut(
        id=promotion.id,
        experience_id=promotion.experience_id,
        city_slug=promotion.city_slug,
        starts_at=promotion.starts_at,
        ends_at=promotion.ends_at,
        status=promotion.status,
        amount_minor=promotion.amount_minor,
        currency=promotion.currency,
        checkout_url=promotion.checkout_url,
    )
    await session.commit()
    return Envelope(data=view)


async def _experience_titles(
    session: SessionDep, ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """One lookup for the page, rather than one per promotion."""
    if not ids:
        return {}
    from sqlalchemy import select

    from app.domains.catalog.models import Experience

    result = await session.execute(
        select(Experience.id, Experience.title).where(Experience.id.in_(set(ids)))
    )
    return {experience_id: title for experience_id, title in result.all()}


# -------------------------------------------------------------------- earnings


class EarningsLineOut(CamelModel):
    """One currency's worth of what a business has taken."""

    currency: str
    gross_minor: int
    fee_minor: int
    net_minor: int
    owing_minor: int
    sales: int


class PayoutOut(CamelModel):
    id: uuid.UUID
    currency: str
    total_minor: int
    entry_count: int
    period_start: datetime
    period_end: datetime
    status: str
    paid_at: datetime | None = None
    reference: str | None = None


class EarningsOut(CamelModel):
    # The commission this business pays, so the dashboard can state it rather
    # than leaving the difference between gross and net to be inferred from
    # arithmetic. A fee nobody can find is indistinguishable from a mistake.
    fee_rate_bps: int
    totals: list[EarningsLineOut]
    payouts: list[PayoutOut]


@router.get(
    "/businesses/{business_id}/earnings",
    response_model=Envelope[EarningsOut],
    summary="What this business has earned",
    description=(
        "Gross is what explorers paid, fee is Mado's commission, net is what "
        "the business is owed, and owing is the part not yet paid out. Money "
        "is not analytics: this needs `finance:view`, which an analyst does "
        "not hold."
    ),
)
async def business_earnings(
    business_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
) -> Envelope[EarningsOut]:
    publisher = await PublishingService(session).assert_can_manage(
        user, business_id, permission=permissions.FINANCE_VIEW
    )
    service = PayoutService(session)
    totals = await service.totals_for(business_id)
    history = await service.history_for(business_id)
    return Envelope(
        data=EarningsOut(
            fee_rate_bps=fees.rate_for(publisher),
            totals=[EarningsLineOut(**asdict(line)) for line in totals],
            payouts=[PayoutOut.model_validate(payout) for payout in history],
        )
    )


# ------------------------------------------------------------------------ team


@router.get(
    "/businesses/{business_id}/members",
    response_model=CollectionEnvelope[MemberOut],
    summary="List team members and pending invitations",
)
async def list_members(
    business_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> CollectionEnvelope[MemberOut]:
    service = PublishingService(session)
    members = await service.list_members(user, business_id)
    names = await service.display_names_for(
        [member.user_id for member in members if member.user_id]
    )
    return CollectionEnvelope(
        data=[
            _member_view(member, display_name=names.get(member.user_id))
            for member in members
        ]
    )


@router.post(
    "/businesses/{business_id}/members",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[MemberOut],
    summary="Invite somebody to help manage this business",
)
async def invite_member(
    business_id: uuid.UUID,
    payload: InviteMemberRequest,
    user: CurrentUser,
    session: SessionDep,
) -> Envelope[MemberOut]:
    member = await PublishingService(session).invite_member(
        user, business_id, email=payload.email, role=payload.role
    )
    view = _member_view(member)
    await session.commit()
    return Envelope(data=view)


@router.patch(
    "/businesses/{business_id}/members/{member_id}",
    response_model=Envelope[MemberOut],
    summary="Change a team member's role",
)
async def change_role(
    business_id: uuid.UUID,
    member_id: uuid.UUID,
    payload: ChangeRoleRequest,
    user: CurrentUser,
    session: SessionDep,
) -> Envelope[MemberOut]:
    member = await PublishingService(session).change_member_role(
        user, business_id, member_id, role=payload.role
    )
    view = _member_view(member)
    await session.commit()
    return Envelope(data=view)


@router.delete(
    "/businesses/{business_id}/members/{member_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member, or revoke their invitation",
)
async def remove_member(
    business_id: uuid.UUID,
    member_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
) -> None:
    await PublishingService(session).remove_member(user, business_id, member_id)
    await session.commit()


# --------------------------------------------------------------------- gallery


@router.get(
    "/businesses/{business_id}/gallery",
    response_model=CollectionEnvelope[GalleryItemOut],
    summary="Photos and videos on this business's profile",
)
async def list_gallery(
    business_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> CollectionEnvelope[GalleryItemOut]:
    items = await PublishingService(session).list_gallery(user, business_id)
    return CollectionEnvelope(data=[_gallery_view(item) for item in items])


@router.post(
    "/businesses/{business_id}/gallery/upload",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[GalleryItemOut],
    summary="Upload a photo or a video to this business's profile",
    description=(
        "One endpoint for both, because the uploader is one file picker and "
        "making the client decide which endpoint to call would mean it deciding "
        "what the file is - which is the thing this route does not trust it "
        "about.\n\n"
        "An image is decoded and re-encoded, which proves it is an image, strips "
        "EXIF including any GPS coordinates, and resizes it for serving. A video "
        "is identified from its container's own magic bytes; MP4 and WebM are "
        "accepted, and the stored extension comes from what was recognised "
        "rather than from the uploaded filename."
    ),
)
async def upload_gallery_item(
    business_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    file: UploadFile = File(...),
    caption: str | None = Form(default=None),
) -> Envelope[GalleryItemOut]:
    await rate_limit.check(rate_limit.identify(request, str(user.id)), rate_limit.UPLOAD_LIMIT)

    service = PublishingService(session)
    # Before a byte is read. An upload route that processes the file first does
    # the expensive work for anybody who asks, whether or not they may store it.
    await service.assert_can_manage(user, business_id, permission=permissions.PROFILE_EDIT)

    # The file's own opening bytes choose the verifier, not its Content-Type or
    # its name - both are written by the client, and `photo.jpg` renamed to
    # `.mp4` would otherwise pick the path that cannot catch it. The declared
    # type is consulted only when the bytes are unrecognisable, and then purely
    # so the refusal talks about what the uploader thought they were sending.
    head = await file.read(media_storage.SNIFF_BYTES)
    declared = (file.content_type or "").lower()

    if media_storage.looks_like_video(head) or declared.startswith("video/"):
        with media_storage.VideoUpload() as upload:
            upload.feed(head)
            # A megabyte at a time from here. Reading a hundred-megabyte video
            # whole before the size check makes the size check decorative, and
            # doing it per concurrent request is how the API runs out of memory.
            while chunk := await file.read(1024 * 1024):
                upload.feed(chunk)
            stored_video = await asyncio.to_thread(upload.finish, owner_id=user.id)

        item = await service.add_gallery_item(
            user,
            business_id,
            kind=GALLERY_VIDEO,
            url=stored_video.url,
            caption=caption,
            content_type=stored_video.content_type,
        )
    else:
        # `head` was already taken off the stream, so the rest is read on top of
        # it rather than instead of it - the cap still bounds the whole file.
        data = head + await file.read(media_storage.MAX_UPLOAD_BYTES + 1 - len(head))
        if len(data) > media_storage.MAX_UPLOAD_BYTES:
            raise ValidationError(
                f"Images must be under {media_storage.MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                code="UPLOAD_TOO_LARGE",
            )
        stored = await asyncio.to_thread(media_storage.store, data, owner_id=user.id)
        item = await service.add_gallery_item(
            user,
            business_id,
            kind=GALLERY_IMAGE,
            url=stored.url,
            caption=caption,
            width=stored.width,
            height=stored.height,
            content_type="image/webp",
        )

    view = _gallery_view(item)
    await session.commit()
    return Envelope(data=view)


@router.post(
    "/businesses/{business_id}/gallery",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[GalleryItemOut],
    summary="Add media already hosted somewhere else",
    description=(
        "For a business whose pictures already live on their own site, and for "
        "the seed. Uploading is the path in the interface."
    ),
)
async def add_gallery_item(
    business_id: uuid.UUID,
    payload: AddGalleryItemRequest,
    user: CurrentUser,
    session: SessionDep,
) -> Envelope[GalleryItemOut]:
    item = await PublishingService(session).add_gallery_item(
        user,
        business_id,
        kind=payload.kind,
        url=payload.url,
        caption=payload.caption,
    )
    view = _gallery_view(item)
    await session.commit()
    return Envelope(data=view)


@router.delete(
    "/businesses/{business_id}/gallery/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Take a photo or video off the profile",
)
async def remove_gallery_item(
    business_id: uuid.UUID,
    item_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
) -> None:
    await PublishingService(session).remove_gallery_item(user, business_id, item_id)
    await session.commit()


# ----------------------------------------------------------------- invitations


@router.get(
    "/me/business-invitations",
    response_model=CollectionEnvelope[InvitationOut],
    summary="Invitations waiting for you",
)
async def my_invitations(
    user: CurrentUser, session: SessionDep
) -> CollectionEnvelope[InvitationOut]:
    pending = await PublishingService(session).pending_invitations(user)
    return CollectionEnvelope(
        data=[
            InvitationOut(
                id=member.id,
                role=member.role,
                role_label=permissions.label(member.role),
                business_id=member.publisher.id,
                business_name=member.publisher.name,
                business_slug=member.publisher.slug,
                invited_at=member.invited_at,
                expires_at=member.expires_at,
            )
            for member in pending
        ]
    )


@router.post(
    "/me/business-invitations/{member_id}/accept",
    response_model=Envelope[MemberOut],
    summary="Accept an invitation",
)
async def accept_invitation(
    member_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Envelope[MemberOut]:
    member = await PublishingService(session).accept_invitation(user, member_id)
    view = _member_view(member)
    await session.commit()
    return Envelope(data=view)


@router.post(
    "/me/business-invitations/{member_id}/decline",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Decline an invitation",
)
async def decline_invitation(
    member_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> None:
    await PublishingService(session).decline_invitation(user, member_id)
    await session.commit()


# --------------------------------------------------------------- public profile


class PublicBusinessOut(BusinessOut):
    """What an explorer sees.

    A different payload from the team's on purpose. Contact details and social
    links are things a business chose to publish, so they stay; verification
    state stays because it is exactly what an explorer wants to know about a
    stranger's claim. What is dropped is everything only the owner has a reason
    to see.
    """

    listings: list[ExperienceSummary] = []
    # Sent with the profile rather than fetched when the tab is opened. It is one
    # ordered list of rows already keyed by this publisher, and a second round
    # trip to get it would make the tab blank for as long as that request takes -
    # for a gallery, which is the part of the page people came to look at.
    gallery: list[GalleryItemOut] = []


@router.get(
    "/businesses/by-slug/{slug}",
    response_model=Envelope[PublicBusinessOut],
    summary="A business profile, as an explorer sees it",
    description=(
        "Public. Returns the business and what it currently has published - "
        "nothing in draft, nothing withheld by moderation, because this is the "
        "same catalogue read every other discovery surface uses."
    ),
)
async def public_business(
    slug: str, session: SessionDep, user: OptionalUser
) -> Envelope[PublicBusinessOut]:
    service = PublishingService(session)
    publisher = await service.business_by_slug(slug)
    if publisher is None:
        raise NotFoundError("Business not found.", code="PUBLISHER_NOT_FOUND")

    listings = await service.published_listings_of(publisher.id)
    gallery = await service.gallery_of(publisher.id)
    view = PublicBusinessOut(
        **_view(publisher).model_dump(by_alias=False),
        listings=[to_summary(experience) for experience in listings],
        gallery=[_gallery_view(item) for item in gallery],
    )
    return Envelope(data=view)
