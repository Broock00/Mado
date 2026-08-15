"""Ticketing and checkout (spec COM-002, COM-003).

Three audiences, and the split between them is the security boundary.

A **publisher** manages the tiers on their own dates. An **explorer** sees what
is on sale, starts a checkout and reads their own tickets. A **payment
provider** posts a callback, authenticated by a signature rather than by a
session - it is not a person, it has no account, and treating it like one is
how a webhook endpoint ends up open.

The callback route is the only unauthenticated write in the platform. It
verifies an HMAC over the exact bytes received, and then ignores everything the
body claims about the outcome: the amount and the state are taken from a
verification call made in the other direction. A signature proves the message
came from the provider; it does not prove the message is current, and a replayed
one would otherwise be a free ticket.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response, status
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, OptionalUser, SessionDep
from app.core.config import get_settings
from app.core.envelope import CollectionEnvelope, Envelope
from app.core.errors import NotFoundError, PermissionDeniedError, ValidationError
from app.core.logging import get_logger
from app.domains.catalog.models import EventInstance, Experience
from app.domains.catalog.schemas import CamelModel
from app.domains.commerce import plan
from app.domains.commerce import tickets as ticketing
from app.domains.commerce.bookings import BookingService
from app.domains.commerce.checkout import CheckoutService, Line
from app.domains.commerce.door import DoorService
from app.domains.commerce.models import Order, TicketType
from app.domains.commerce.tickets import TicketTypeService
from app.domains.publisher.models import Publisher
from app.integrations import payments

logger = get_logger("mado.commerce.routes")

router = APIRouter(tags=["commerce"])


# ------------------------------------------------------------------- shapes


class TicketTypeOut(CamelModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    price_minor: int
    currency: str
    quantity: int | None = None
    remaining: int | None = None
    on_sale: bool
    # Why not, when not: `withdrawn`, `not_open_yet`, `closed`, `sold_out`.
    unavailable_reason: str | None = None
    position: int


class TicketingOut(CamelModel):
    """Codes, not sentences - the client renders the words."""

    availability: str
    cta: str
    price_type: str
    currency: str
    min_price_minor: int | None = None
    max_price_minor: int | None = None
    external_url: str | None = None
    seats_remaining: int | None = None
    ticket_types: list[TicketTypeOut] = Field(default_factory=list)


class CreateTicketTypeRequest(CamelModel):
    name: str = Field(max_length=ticketing.MAX_TIER_NAME)
    price_minor: int = Field(ge=0)
    currency: str = Field(default="ETB", min_length=3, max_length=3)
    quantity: int | None = Field(default=None, ge=0)
    description: str | None = Field(default=None, max_length=300)
    sales_open_at: datetime | None = None
    sales_close_at: datetime | None = None


class ScanRequest(CamelModel):
    code: str = Field(min_length=1, max_length=64)
    # False looks the ticket up without spending it. Checking a code by hand
    # should not quietly use somebody's admission.
    admit: bool = True


class ScanOut(CamelModel):
    """What the door was told.

    A code, not a sentence - the wording belongs to whoever is reading it, and
    a door in Addis reads it in Amharic.
    """

    # admitted | already_admitted | wrong_event | void | unknown
    verdict: str
    # Absent unless the ticket is for this door. A code somebody happens to hold
    # is not permission to read a stranger's booking.
    name: str | None = None
    ticket_type_name: str | None = None
    reference: str | None = None
    checked_in_at: datetime | None = None
    admitted_count: int = 0
    issued_count: int = 0


class PlanEntryOut(CamelModel):
    """One tier as the publisher thinks of it: across every date, not on one."""

    name: str
    description: str | None = None
    price_minor: int
    currency: str
    quantity: int | None = None
    # How many dates carry it, and how it is doing across all of them.
    dates: int
    sold: int
    remaining: int | None = None
    is_active: bool
    # True when the dates disagree - somebody edited one by hand. Said out loud
    # rather than papered over by showing whichever one came first.
    varies: bool = False


class PlanTierRequest(CamelModel):
    name: str = Field(max_length=ticketing.MAX_TIER_NAME)
    price_minor: int = Field(ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    quantity: int | None = Field(default=None, ge=0)
    description: str | None = Field(default=None, max_length=300)


class WithdrawTierRequest(CamelModel):
    name: str = Field(max_length=ticketing.MAX_TIER_NAME)


class TierSalesOut(CamelModel):
    ticket_type_id: uuid.UUID
    name: str
    price_minor: int
    quantity: int | None = None
    sold: int
    remaining: int | None = None
    revenue_minor: int


class BuyerOut(CamelModel):
    """One booking, as the publisher sees it.

    Named, like the reservations door list and for the same reason: somebody
    who bought a ticket has told this publisher they are coming. Nothing else
    about them belongs here - not their email, not what else they have booked.
    """

    order_id: uuid.UUID
    reference: str
    name: str
    quantity: int
    amount_minor: int
    status: str
    tiers: list[str] = Field(default_factory=list)
    ordered_at: datetime
    paid_at: datetime | None = None


class BookingsOut(CamelModel):
    currency: str
    capacity: int | None = None
    # Paid and pending together, because that is what decides whether the room
    # is full. The two are also reported apart, below, because only one of them
    # is money.
    seats_taken: int
    seats_remaining: int | None = None
    tickets_paid: int
    tickets_pending: int
    revenue_minor: int
    pending_minor: int
    orders_paid: int
    orders_pending: int
    orders_failed: int
    tiers: list[TierSalesOut] = Field(default_factory=list)
    buyers: list[BuyerOut] = Field(default_factory=list)


class OrderLineOut(CamelModel):
    ticket_type_id: uuid.UUID
    ticket_type_name: str
    quantity: int
    unit_price_minor: int
    total_minor: int


class TicketOut(CamelModel):
    id: uuid.UUID
    code: str
    ticket_type_name: str
    status: str
    checked_in_at: datetime | None = None


class OrderOut(CamelModel):
    id: uuid.UUID
    reference: str
    status: str
    experience_id: uuid.UUID
    experience_title: str
    event_instance_id: uuid.UUID
    starts_at: datetime
    amount_minor: int
    currency: str
    quantity: int
    # Where to send the explorer next. Absent for a free order, which has
    # nothing to pay and is complete already.
    checkout_url: str | None = None
    expires_at: datetime | None = None
    paid_at: datetime | None = None
    outcome_reason: str | None = None
    lines: list[OrderLineOut] = Field(default_factory=list)
    tickets: list[TicketOut] = Field(default_factory=list)


class BuyLine(CamelModel):
    ticket_type_id: uuid.UUID
    quantity: int = Field(ge=1)


class StartCheckoutRequest(CamelModel):
    lines: list[BuyLine]


# --------------------------------------------------------------- explorer


@router.get(
    "/experiences/{experience_id}/events/{occurrence_id}/ticketing",
    response_model=Envelope[TicketingOut],
    summary="What this date costs and whether it is on sale",
    description=(
        "Availability, price shape and the primary action, as codes rather than "
        "words - the client owns the wording so there is one place Amharic "
        "lives. Where the organiser sells elsewhere, this returns the outward "
        "link and no availability at all: Mado cannot count inventory it does "
        "not hold."
    ),
)
async def ticketing_for(
    experience_id: uuid.UUID,
    occurrence_id: uuid.UUID,
    session: SessionDep,
    _user: OptionalUser,
) -> Envelope[TicketingOut]:
    occurrence, experience = await _occurrence_of(session, experience_id, occurrence_id)
    tiers = await TicketTypeService(session).for_occurrence(occurrence_id)
    return Envelope(data=_ticketing_out(ticketing.summarise(experience, occurrence, tiers)))


@router.post(
    "/experiences/{experience_id}/events/{occurrence_id}/orders",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[OrderOut],
    summary="Start buying tickets",
    description=(
        "Holds the places immediately and returns somewhere to pay. The hold "
        "expires if the payment is not completed, and nothing is issued until "
        "the provider confirms the money independently - the browser coming "
        "back is not evidence. A free order needs no provider and is complete "
        "when this returns."
    ),
)
async def start_order(
    experience_id: uuid.UUID,
    occurrence_id: uuid.UUID,
    payload: StartCheckoutRequest,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
) -> Envelope[OrderOut]:
    await _occurrence_of(session, experience_id, occurrence_id)
    settings = get_settings()

    order = await CheckoutService(session).start(
        user,
        occurrence_id=occurrence_id,
        lines=[
            Line(ticket_type_id=item.ticket_type_id, quantity=item.quantity)
            for item in payload.lines
        ],
        return_url_base=f"{settings.web_base_url.rstrip('/')}/orders",
        callback_url=str(request.url_for("payment_callback")),
    )
    view = _order_out(order)
    await session.commit()
    return Envelope(data=view)


@router.get(
    "/orders",
    response_model=CollectionEnvelope[OrderOut],
    summary="Tickets you hold",
)
async def my_orders(session: SessionDep, user: CurrentUser) -> CollectionEnvelope[OrderOut]:
    orders = await CheckoutService(session).mine(user)
    return CollectionEnvelope(data=[_order_out(order) for order in orders])


@router.get(
    "/orders/{order_id}",
    response_model=Envelope[OrderOut],
    summary="One order",
    description=(
        "Asks the provider what happened when the order is still pending, so "
        "somebody who has just come back from paying gets an answer rather "
        "than a spinner waiting on a webhook."
    ),
)
async def read_order(
    order_id: uuid.UUID, session: SessionDep, user: CurrentUser
) -> Envelope[OrderOut]:
    service = CheckoutService(session)
    order = await service.owned(user, order_id)
    if order.status == "pending":
        order = await service.settle(order, source="return")
        await session.commit()
    return Envelope(data=_order_out(order))


@router.post(
    "/orders/{order_id}/cancel",
    response_model=Envelope[OrderOut],
    summary="Give up on an unpaid order",
    description="Releases the places at once instead of waiting for the hold to lapse.",
)
async def cancel_order(
    order_id: uuid.UUID, session: SessionDep, user: CurrentUser
) -> Envelope[OrderOut]:
    order = await CheckoutService(session).cancel(user, order_id)
    view = _order_out(order)
    await session.commit()
    return Envelope(data=view)


# -------------------------------------------------------------- publisher


@router.post(
    "/posts/{experience_id}/events/{occurrence_id}/scan",
    response_model=Envelope[ScanOut],
    summary="Admit somebody at the door",
    description=(
        "Scoped to one date on purpose: a ticket for another night must not "
        "read as valid here, and the scanner is the only thing standing between "
        "those two outcomes. The second scan of the same ticket is refused and "
        "says when the first happened - a screenshot forwarded to three friends "
        "is still one admission."
    ),
)
async def scan_ticket(
    experience_id: uuid.UUID,
    occurrence_id: uuid.UUID,
    payload: ScanRequest,
    session: SessionDep,
    user: CurrentUser,
) -> Envelope[ScanOut]:
    occurrence, _ = await _owned_occurrence(session, user, experience_id, occurrence_id)
    result = await DoorService(session).scan(
        occurrence_id=occurrence.id, code=payload.code, admit=payload.admit
    )
    view = ScanOut(
        verdict=result.verdict,
        name=result.name,
        ticket_type_name=result.ticket_type_name,
        reference=result.reference,
        checked_in_at=result.checked_in_at,
        admitted_count=result.admitted_count,
        issued_count=result.issued_count,
    )
    await session.commit()
    return Envelope(data=view)


@router.get(
    "/posts/{experience_id}/tickets",
    response_model=CollectionEnvelope[PlanEntryOut],
    summary="The tickets this listing sells",
    description=(
        "One row per tier rather than per tier per date. Tiers are still stored "
        "against a date, because inventory is - but a publisher thinks in terms "
        "of what the event sells, not what each night sells."
    ),
)
async def ticket_plan(
    experience_id: uuid.UUID, session: SessionDep, user: CurrentUser
) -> CollectionEnvelope[PlanEntryOut]:
    await _owned_experience(session, user, experience_id)
    return CollectionEnvelope(
        data=[_plan_out(entry) for entry in await plan.plan_for(session, experience_id)]
    )


@router.put(
    "/posts/{experience_id}/tickets",
    response_model=CollectionEnvelope[PlanEntryOut],
    summary="Sell this ticket on every date",
    description=(
        "Adds the tier where it is missing and updates it where it is not, on "
        "every date still to come. PUT rather than POST because applying the "
        "same tier twice has to mean the same as applying it once - a publisher "
        "correcting a price is doing exactly that."
    ),
)
async def put_ticket_plan(
    experience_id: uuid.UUID,
    payload: PlanTierRequest,
    session: SessionDep,
    user: CurrentUser,
) -> CollectionEnvelope[PlanEntryOut]:
    experience = await _owned_experience(session, user, experience_id)
    reached = await plan.apply_tier(
        session,
        experience_id=experience_id,
        name=payload.name.strip(),
        price_minor=payload.price_minor,
        currency=(payload.currency or experience.currency or "ETB").upper(),
        quantity=payload.quantity,
        description=payload.description,
    )
    if reached == 0:
        raise ValidationError(
            "Add a date first - a ticket is sold for a date, so there is nothing "
            "to put this on yet.",
            code="NO_DATES_TO_SELL",
        )
    entries = [_plan_out(entry) for entry in await plan.plan_for(session, experience_id)]
    await session.commit()
    return CollectionEnvelope(data=entries)


@router.post(
    "/posts/{experience_id}/tickets/withdraw",
    response_model=CollectionEnvelope[PlanEntryOut],
    summary="Stop selling one of them",
    description=(
        "Withdrawn on every date still to come, and deleted nowhere: an order "
        "that bought one has to keep reading. The name is sent in the body "
        "rather than the path because a tier may be called anything, including "
        "something with a slash in it."
    ),
)
async def withdraw_from_plan(
    experience_id: uuid.UUID,
    payload: WithdrawTierRequest,
    session: SessionDep,
    user: CurrentUser,
) -> CollectionEnvelope[PlanEntryOut]:
    await _owned_experience(session, user, experience_id)
    await plan.withdraw_tier(session, experience_id=experience_id, name=payload.name)
    entries = [_plan_out(entry) for entry in await plan.plan_for(session, experience_id)]
    await session.commit()
    return CollectionEnvelope(data=entries)


@router.get(
    "/posts/{experience_id}/events/{occurrence_id}/ticket-types",
    response_model=CollectionEnvelope[TicketTypeOut],
    summary="The tiers on one of your dates",
)
async def list_ticket_types(
    experience_id: uuid.UUID,
    occurrence_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUser,
) -> CollectionEnvelope[TicketTypeOut]:
    occurrence, _ = await _owned_occurrence(session, user, experience_id, occurrence_id)
    tiers = await TicketTypeService(session).for_occurrence(occurrence.id)
    return CollectionEnvelope(data=[_tier_out(tier) for tier in tiers])


@router.post(
    "/posts/{experience_id}/events/{occurrence_id}/ticket-types",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[TicketTypeOut],
    summary="Add a tier",
    description=(
        "Prices are in minor units - santim, not birr - because a price that "
        "goes near a float eventually acquires a rounding error, and the place "
        "it surfaces is somebody's total."
    ),
)
async def create_ticket_type(
    experience_id: uuid.UUID,
    occurrence_id: uuid.UUID,
    payload: CreateTicketTypeRequest,
    session: SessionDep,
    user: CurrentUser,
) -> Envelope[TicketTypeOut]:
    occurrence, _ = await _owned_occurrence(session, user, experience_id, occurrence_id)
    tier = await TicketTypeService(session).create(
        occurrence=occurrence,
        name=payload.name,
        price_minor=payload.price_minor,
        currency=payload.currency,
        quantity=payload.quantity,
        description=payload.description,
        sales_open_at=payload.sales_open_at,
        sales_close_at=payload.sales_close_at,
    )
    view = _tier_out(tier)
    await session.commit()
    return Envelope(data=view)


@router.get(
    "/posts/{experience_id}/events/{occurrence_id}/bookings",
    response_model=Envelope[BookingsOut],
    summary="How this date is selling, and who is coming",
    description=(
        "The publisher of the listing only. Pending money is reported "
        "separately from taken money rather than added to it: a pending order "
        "is holding a seat and may still lapse, so folding it into revenue "
        "reports income that does not exist."
    ),
)
async def bookings(
    experience_id: uuid.UUID,
    occurrence_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUser,
) -> Envelope[BookingsOut]:
    occurrence, experience = await _owned_occurrence(
        session, user, experience_id, occurrence_id
    )
    report = await BookingService(session).for_occurrence(
        occurrence.id,
        capacity=occurrence.capacity,
        currency=experience.currency or "ETB",
    )
    return Envelope(
        data=BookingsOut(
            currency=report.currency,
            capacity=report.capacity,
            seats_taken=report.seats_taken,
            seats_remaining=(
                max(0, report.capacity - report.seats_taken)
                if report.capacity is not None
                else None
            ),
            tickets_paid=report.tickets_paid,
            tickets_pending=report.tickets_pending,
            revenue_minor=report.revenue_minor,
            pending_minor=report.pending_minor,
            orders_paid=report.orders_paid,
            orders_pending=report.orders_pending,
            orders_failed=report.orders_failed,
            tiers=[
                TierSalesOut(
                    ticket_type_id=tier.ticket_type_id,
                    name=tier.name,
                    price_minor=tier.price_minor,
                    quantity=tier.quantity,
                    sold=tier.sold,
                    remaining=tier.remaining,
                    revenue_minor=tier.revenue_minor,
                )
                for tier in report.tiers
            ],
            buyers=[
                BuyerOut(
                    order_id=buyer.order_id,
                    reference=buyer.reference,
                    name=buyer.name,
                    quantity=buyer.quantity,
                    amount_minor=buyer.amount_minor,
                    status=buyer.status,
                    tiers=buyer.tiers,
                    ordered_at=buyer.ordered_at,
                    paid_at=buyer.paid_at,
                )
                for buyer in report.buyers
            ],
        )
    )


@router.delete(
    "/posts/{experience_id}/events/{occurrence_id}/ticket-types/{ticket_type_id}",
    response_model=Envelope[TicketTypeOut],
    summary="Stop selling a tier",
    description=(
        "Withdrawn, not deleted. Every order that bought one still reads, "
        "which is the whole reason this is not a delete."
    ),
)
async def withdraw_ticket_type(
    experience_id: uuid.UUID,
    occurrence_id: uuid.UUID,
    ticket_type_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUser,
) -> Envelope[TicketTypeOut]:
    await _owned_occurrence(session, user, experience_id, occurrence_id)
    tier = await TicketTypeService(session).withdraw(ticket_type_id)
    if tier.event_instance_id != occurrence_id:
        raise NotFoundError("Ticket type not found.", code="TICKET_TYPE_NOT_FOUND")
    view = _tier_out(tier)
    await session.commit()
    return Envelope(data=view)


# --------------------------------------------------------------- provider


@router.post(
    "/payments/callback",
    name="payment_callback",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Where the payment provider posts",
    description=(
        "Not a person and not a session: authenticated by an HMAC over the "
        "exact bytes received. What the body claims about the outcome is "
        "ignored - the amount and state come from a verification call made in "
        "the other direction, because a signature proves who sent a message "
        "and not that the message is current."
    ),
)
async def payment_callback(request: Request, session: SessionDep) -> Response:
    body = await request.body()

    # Which provider is calling, decided by the header it signs with. Both
    # providers post here and each has its own secret and its own scheme, so
    # verifying with the wrong one would refuse every genuine callback.
    stripe_signature = request.headers.get("stripe-signature")
    if stripe_signature:
        provider = payments.provider_named("stripe")
        signature = stripe_signature
    else:
        provider = payments.provider_named("chapa")
        signature = (
            request.headers.get("chapa-signature")
            or request.headers.get("x-chapa-signature")
            or request.headers.get("x-mado-signature")
        )

    if not provider.signature_is_valid(body=body, signature=signature):
        logger.warning("payment_callback_rejected", reason="bad_signature", provider=provider.name)
        # 204 either way. A different status for a bad signature tells somebody
        # probing which of their guesses was closer.
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    try:
        payload = await request.json()
    except ValueError:
        payload = {}

    # Stripe wraps the interesting part in an event envelope and carries our own
    # reference as `client_reference_id`; Chapa posts the transaction directly.
    inner = ((payload.get("data") or {}).get("object") or {}) if isinstance(payload, dict) else {}
    reference = str(
        payload.get("tx_ref")
        or payload.get("reference")
        or inner.get("client_reference_id")
        or ""
    ).strip()
    if not reference:
        logger.warning("payment_callback_rejected", reason="no_reference")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    service = CheckoutService(session)
    order = await service.by_reference(reference)
    if order is None:
        logger.warning("payment_callback_unknown_order", reference=reference)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    await service.settle(
        order,
        source="webhook",
        # Stripe's event id is the natural idempotency key and is unique per
        # delivery attempt group; Chapa supplies neither, so `settle` falls back
        # to one built from the reference.
        external_id=str(payload.get("event_id") or payload.get("id") or "") or None,
        payload=payload if isinstance(payload, dict) else {},
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/payments/simulate/{reference}",
    response_model=Envelope[OrderOut],
    summary="Settle a stub payment (development only)",
    description=(
        "Exists because the stub provider refuses to invent a payment. A stub "
        "that reports success on its own is indistinguishable from a working "
        "integration until the money does not arrive, so development settles "
        "one explicitly. Refused outright unless the stub is the configured "
        "provider."
    ),
)
async def simulate_payment(
    reference: str,
    session: SessionDep,
    user: CurrentUser,
    paid: bool = True,
) -> Envelope[OrderOut]:
    provider = payments.get_provider()
    if not isinstance(provider, payments.StubPayments):
        raise PermissionDeniedError(
            "Payments are handled by a real provider here.", code="NOT_SIMULATED"
        )

    service = CheckoutService(session)
    order = await service.by_reference(reference)
    if order is None or order.user_id != user.id:
        raise NotFoundError("Order not found.", code="ORDER_NOT_FOUND")

    try:
        provider.settle(reference, paid=paid)
    except payments.PaymentError as exc:
        raise ValidationError(str(exc), code="NO_STUB_PAYMENT") from exc

    order = await service.settle(order, source="return")
    view = _order_out(order)
    await session.commit()
    return Envelope(data=view)


# ------------------------------------------------------------------ mapping


def _plan_out(entry: plan.PlanEntry) -> PlanEntryOut:
    return PlanEntryOut(
        name=entry.name,
        description=entry.description,
        price_minor=entry.price_minor,
        currency=entry.currency,
        quantity=entry.quantity,
        dates=entry.dates,
        sold=entry.sold,
        remaining=entry.remaining,
        is_active=entry.is_active,
        varies=entry.varies,
    )


def _tier_out(tier: TicketType, *, now: datetime | None = None) -> TicketTypeOut:
    on_sale, reason = ticketing.sales_window(tier, now or datetime.now(UTC))
    return TicketTypeOut(
        id=tier.id,
        name=tier.name,
        description=tier.description,
        price_minor=tier.price_minor,
        currency=tier.currency,
        quantity=tier.quantity,
        remaining=tier.remaining,
        on_sale=on_sale,
        unavailable_reason=reason,
        position=tier.position,
    )


def _ticketing_out(summary: ticketing.Ticketing) -> TicketingOut:
    return TicketingOut(
        availability=summary.availability,
        cta=summary.cta,
        price_type=summary.price_type,
        currency=summary.currency,
        min_price_minor=summary.min_price_minor,
        max_price_minor=summary.max_price_minor,
        external_url=summary.external_url,
        seats_remaining=summary.seats_remaining,
        ticket_types=[
            TicketTypeOut(
                id=view.tier.id,
                name=view.tier.name,
                description=view.tier.description,
                price_minor=view.tier.price_minor,
                currency=view.tier.currency,
                quantity=view.tier.quantity,
                remaining=view.remaining,
                on_sale=view.on_sale,
                unavailable_reason=view.reason,
                position=view.tier.position,
            )
            for view in summary.tiers
        ],
    )


def _order_out(order: Order) -> OrderOut:
    return OrderOut(
        id=order.id,
        reference=order.reference,
        status=order.status,
        experience_id=order.experience_id,
        experience_title=order.experience_title,
        event_instance_id=order.event_instance_id,
        starts_at=order.starts_at,
        amount_minor=order.amount_minor,
        currency=order.currency,
        quantity=order.quantity,
        checkout_url=order.checkout_url,
        expires_at=order.expires_at,
        paid_at=order.paid_at,
        outcome_reason=order.outcome_reason,
        lines=[
            OrderLineOut(
                ticket_type_id=line.ticket_type_id,
                ticket_type_name=line.ticket_type_name,
                quantity=line.quantity,
                unit_price_minor=line.unit_price_minor,
                total_minor=line.total_minor,
            )
            for line in order.lines
        ],
        # Only ever populated for a paid order, because tickets are only issued
        # then. A pending order showing codes would be a ticket somebody could
        # screenshot before paying.
        tickets=[
            TicketOut(
                id=ticket.id,
                code=ticket.code,
                ticket_type_name=ticket.ticket_type_name,
                status=ticket.status,
                checked_in_at=ticket.checked_in_at,
            )
            for ticket in order.tickets
        ],
    )


async def _occurrence_of(
    session, experience_id: uuid.UUID, occurrence_id: uuid.UUID
) -> tuple[EventInstance, Experience]:
    occurrence = await session.get(
        EventInstance, occurrence_id, options=[selectinload(EventInstance.experience)]
    )
    if occurrence is None or occurrence.experience_id != experience_id:
        raise NotFoundError("That date does not exist.", code="OCCURRENCE_NOT_FOUND")
    experience = occurrence.experience
    if experience is None:
        raise NotFoundError("That listing does not exist.", code="EXPERIENCE_NOT_FOUND")
    return occurrence, experience


async def _owned_experience(session, user, experience_id: uuid.UUID) -> Experience:
    """The listing, if this person publishes it.

    The same check as :func:`_owned_occurrence` without needing a date, because
    the ticket plan belongs to the listing rather than to any one of its nights.
    """
    experience = await session.get(Experience, experience_id)
    if experience is None:
        raise NotFoundError("That listing does not exist.", code="EXPERIENCE_NOT_FOUND")
    publisher = (
        await session.execute(select(Publisher).where(Publisher.id == experience.publisher_id))
    ).scalars().first()
    if publisher is None or publisher.owner_user_id != user.id:
        raise PermissionDeniedError(
            "Only the publisher of this listing can manage its tickets.",
            code="NOT_THE_PUBLISHER",
        )
    return experience


async def _owned_occurrence(
    session, user, experience_id: uuid.UUID, occurrence_id: uuid.UUID
) -> tuple[EventInstance, Experience]:
    """The same ownership check the attendee list uses - a publisher is a person."""
    occurrence, experience = await _occurrence_of(session, experience_id, occurrence_id)
    publisher = (
        await session.execute(select(Publisher).where(Publisher.id == experience.publisher_id))
    ).scalars().first()
    if publisher is None or publisher.owner_user_id != user.id:
        raise PermissionDeniedError(
            "Only the publisher of this listing can manage its tickets.",
            code="NOT_THE_PUBLISHER",
        )
    return occurrence, experience
