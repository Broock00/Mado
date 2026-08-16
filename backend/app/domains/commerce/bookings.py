"""What one date has actually sold, for the publisher who is running it.

Two different questions, answered together because a publisher asks them
together: how is it selling, and who is coming.

**Pending orders are counted apart from paid ones, never folded in.** A pending
order is holding a seat and may still become money or may lapse in twenty
minutes. Adding it to revenue would report income that does not exist; leaving it
out of the seat count would tell a publisher there is room they cannot actually
sell. So both numbers are reported and named.

**Buyers are named; nothing else about them is included.** Someone who bought a
ticket has deliberately told this publisher they are coming and expects to be
found on a list at the door - the same consent the reservations door list rests
on. Their email, their other bookings and anything inferred about them are not
the publisher's business.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domains.commerce.models import (
    ORDER_PAID,
    ORDER_PENDING,
    Order,
    TicketType,
)
from app.domains.identity.models import UserProfile


@dataclass(slots=True)
class TierSales:
    ticket_type_id: uuid.UUID
    name: str
    price_minor: int
    quantity: int | None
    sold: int
    remaining: int | None
    revenue_minor: int


@dataclass(slots=True)
class Buyer:
    order_id: uuid.UUID
    reference: str
    name: str
    quantity: int
    amount_minor: int
    status: str
    tiers: list[str]
    ordered_at: datetime
    paid_at: datetime | None


@dataclass(slots=True)
class Bookings:
    currency: str
    capacity: int | None
    # Seats held by anything that has not lapsed - paid and pending together.
    # This is the number that decides whether the room is full.
    seats_taken: int
    tickets_paid: int
    tickets_pending: int
    revenue_minor: int
    pending_minor: int
    orders_paid: int
    orders_pending: int
    orders_failed: int
    tiers: list[TierSales] = field(default_factory=list)
    buyers: list[Buyer] = field(default_factory=list)


def summarise(
    orders,
    tiers,
    *,
    capacity: int | None,
    currency: str,
    names: dict[uuid.UUID, str] | None = None,
) -> Bookings:
    """Aggregate orders and tiers into the publisher's view.

    Pure, and separate from the fetching, so the money rules can be tested with
    ordinary objects rather than a database - the same split
    :func:`app.domains.commerce.tickets.summarise` uses, and for the same
    reason: these rules are worth asserting directly.
    """
    names = names or {}

    # Revenue per tier comes from the order lines rather than from
    # `sold * price`: a tier's price can change after somebody has bought
    # one, and the line carries what was actually charged.
    revenue_by_tier: dict[uuid.UUID, int] = {}
    for order in orders:
        if order.status != ORDER_PAID:
            continue
        for line in order.lines:
            revenue_by_tier[line.ticket_type_id] = revenue_by_tier.get(
                line.ticket_type_id, 0
            ) + (line.unit_price_minor * line.quantity)

    report = Bookings(
        currency=currency,
        capacity=capacity,
        seats_taken=0,
        tickets_paid=0,
        tickets_pending=0,
        revenue_minor=0,
        pending_minor=0,
        orders_paid=0,
        orders_pending=0,
        orders_failed=0,
        tiers=[
            TierSales(
                ticket_type_id=tier.id,
                name=tier.name,
                price_minor=tier.price_minor,
                quantity=tier.quantity,
                sold=tier.sold,
                remaining=tier.remaining,
                revenue_minor=revenue_by_tier.get(tier.id, 0),
            )
            for tier in tiers
        ],
    )

    for order in orders:
        if order.status == ORDER_PAID:
            report.orders_paid += 1
            report.tickets_paid += order.quantity
            report.revenue_minor += order.amount_minor
        elif order.status == ORDER_PENDING:
            report.orders_pending += 1
            report.tickets_pending += order.quantity
            report.pending_minor += order.amount_minor
        else:
            report.orders_failed += 1
            # Nothing is held and nothing was paid, so it appears in the
            # count of what went wrong and nowhere else.
            continue

        report.seats_taken += order.quantity
        report.buyers.append(
            Buyer(
                order_id=order.id,
                reference=order.reference,
                name=names.get(order.user_id, "Someone"),
                quantity=order.quantity,
                amount_minor=order.amount_minor,
                status=order.status,
                tiers=[f"{line.quantity} x {line.ticket_type_name}" for line in order.lines],
                ordered_at=order.created_at,
                paid_at=order.paid_at,
            )
        )

    return report


async def sold_for_experience(session: AsyncSession, experience_id: uuid.UUID) -> int:
    """How many tickets across this listing are somebody's, right now.

    Paid and pending both count. A pending order is holding a seat and may still
    become money, and deleting the listing out from under it is the same
    unpleasant surprise either way.

    Exists so the publishing routes can ask without reading this domain's tables
    themselves.
    """
    return (
        await session.execute(
            select(func.coalesce(func.sum(Order.quantity), 0))
            .where(Order.experience_id == experience_id)
            .where(Order.status.in_((ORDER_PAID, ORDER_PENDING)))
        )
    ).scalar() or 0


class BookingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def for_occurrence(
        self, occurrence_id: uuid.UUID, *, capacity: int | None, currency: str
    ) -> Bookings:
        orders = list(
            (
                await self.session.execute(
                    select(Order)
                    .where(Order.event_instance_id == occurrence_id)
                    .options(selectinload(Order.lines))
                    .order_by(Order.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        tiers = list(
            (
                await self.session.execute(
                    select(TicketType)
                    .where(TicketType.event_instance_id == occurrence_id)
                    .order_by(TicketType.position, TicketType.price_minor)
                )
            )
            .scalars()
            .all()
        )
        return summarise(
            orders,
            tiers,
            capacity=capacity,
            currency=currency,
            names=await self._names({order.user_id for order in orders}),
        )

    async def _names(self, user_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not user_ids:
            return {}
        return {
            profile.user_id: profile.display_name
            for profile in (
                await self.session.execute(
                    select(UserProfile).where(UserProfile.user_id.in_(user_ids))
                )
            )
            .scalars()
            .all()
        }
