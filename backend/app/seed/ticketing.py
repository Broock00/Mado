"""Tickets for the seeded catalogue.

A price on a listing is a label. What makes something buyable is a tier in
``commerce.ticket_types``, and neither seed created one - so every paid listing
in the database showed a price and no way to pay it. The checkout, the provider
adapters and the order flow were all built and none of it could be reached from
the catalogue, which is the kind of gap that reads as a broken feature.

Shared by both seeds rather than written twice, because the interesting part is
not the rows but the shapes they have to produce: a sold-out date, a date that
is nearly gone, and an ordinary one. A fixture where every tier is identical
exercises exactly one branch of the availability code.

**Free listings get no tier on purpose.** The model does treat "registration
required" as a zero-priced ticket, but the listings seeded as free are open-door
things - a park, a market, a viewpoint - and giving them tickets would replace
"just turn up" with a registration step that nobody asked for. The no-tier
fallback already answers free entry correctly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.catalog.models import EventInstance, Experience
from app.domains.commerce.models import TicketType
from app.integrations.payments import major_to_minor

# How the room is divided between tiers, and how much of it is already gone.
# Deterministic from the occurrence's position in the list rather than random:
# a fixture that reshuffles which date is sold out on every seed makes a
# screenshot of a bug impossible to reproduce.
_SOLD_PATTERN = (0.0, 0.35, 0.92, 1.0)


@dataclass(frozen=True, slots=True)
class Tier:
    name: str
    price_major: float
    share: float
    position: int
    description: str | None = None


def tiers_for(price_type: str, price_amount: float | None) -> list[Tier]:
    """The tiers a listing at this price should sell.

    Two for a range, because that is what a range means - a cheaper way in and a
    better seat - and one otherwise. A range listing whose tiers were both the
    same price would render as a fixed price and quietly contradict its own
    price shape.
    """
    if not price_amount or price_type == "free":
        return []
    if price_type == "range":
        return [
            Tier("Standard", price_amount, 0.7, 0, "General entry."),
            Tier("Front section", price_amount * 2, 0.3, 1, "Reserved, nearer the front."),
        ]
    return [Tier("General admission", price_amount, 1.0, 0)]


async def attach_tickets(
    session: AsyncSession,
    *,
    experience: Experience,
    occurrences: list[EventInstance],
    currency: str,
    now: datetime,
) -> int:
    """Create the tiers for one listing's dates. Returns how many were written.

    Replaces rather than adds. ``ticket_types`` carries no foreign key into
    catalog - deliberately, so a sold ticket outlives the listing being
    withdrawn - which means nothing deletes these when a seed recreates its
    occurrences, and reseeding would otherwise leave tiers pointing at dates
    that no longer exist.
    """
    shapes = tiers_for(experience.price_type, float(experience.price_amount or 0) or None)
    if not shapes:
        return 0

    await session.execute(
        delete(TicketType).where(TicketType.experience_id == experience.id)
    )

    written = 0
    for index, occurrence in enumerate(occurrences):
        # A room of unknown size sells tiers of unknown size, which is what
        # `quantity = None` means: defer to whatever the occurrence holds.
        capacity = occurrence.capacity
        sold_share = _SOLD_PATTERN[index % len(_SOLD_PATTERN)]

        for shape in shapes:
            quantity = max(1, int(capacity * shape.share)) if capacity else None
            session.add(
                TicketType(
                    event_instance_id=occurrence.id,
                    experience_id=experience.id,
                    name=shape.name,
                    description=shape.description,
                    price_minor=major_to_minor(shape.price_major, currency),
                    currency=currency,
                    quantity=quantity,
                    sold=int(quantity * sold_share) if quantity else 0,
                    position=shape.position,
                    is_active=True,
                    # Nothing sells after the doors close. Left open at the
                    # start so a date in the past is closed by its own time
                    # rather than needing a second rule.
                    sales_close_at=occurrence.start_time,
                    sales_open_at=None,
                )
            )
            written += 1
    return written


async def attach_tickets_everywhere(
    session: AsyncSession, *, now: datetime, only_demo: bool = False
) -> dict[str, int]:
    """Give every paid listing that has dates something to sell."""
    stmt = select(Experience).where(Experience.price_type != "free")
    if only_demo:
        stmt = stmt.where(Experience.attributes.has_key("demo"))

    counts = {"listings": 0, "tiers": 0}
    for experience in (await session.execute(stmt)).scalars().all():
        occurrences = list(
            (
                await session.execute(
                    select(EventInstance)
                    .where(EventInstance.experience_id == experience.id)
                    .where(EventInstance.start_time > now - timedelta(hours=1))
                    .order_by(EventInstance.start_time)
                )
            )
            .scalars()
            .all()
        )
        if not occurrences:
            continue
        written = await attach_tickets(
            session,
            experience=experience,
            occurrences=occurrences,
            currency=experience.currency or "ETB",
            now=now,
        )
        if written:
            counts["listings"] += 1
            counts["tiers"] += written
    await session.flush()
    return counts
