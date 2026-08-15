"""The tickets a listing sells, defined once and applied to every date.

Tiers are stored per occurrence and that is not going to change: a weekly night
is a different room every week, last Friday sold out and next Friday has not, so
inventory has to hang off the date. But *authoring* them per date was a
different claim, and a wrong one - the publisher of a run of six nights was
asked to type "VIP, 800, what it includes" six times, and any night they forgot
quietly sold nothing but general admission.

So this is the plan: the set of tiers a listing sells, derived by grouping the
per-date rows by name, and applied outwards to every future date at once. The
name is the key, because it is what a publisher already treats as the identity
of a tier - two dates both selling "VIP" are selling the same thing.

Three operations, and a fourth that matters more than it looks:

* :func:`plan_for` - what this listing sells, as one list
* :func:`apply_tier` - add or change a tier on every future date
* :func:`withdraw_tier` - stop selling it, everywhere, without deleting it
* :func:`materialise_for` - give a newly added date the tiers the listing
  already sells, which is what stops "define once" quietly becoming "define
  once, then remember forever"
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.catalog.models import EventInstance
from app.domains.commerce.models import TicketType


@dataclass(slots=True)
class PlanEntry:
    """One tier as the publisher thinks of it, across all the dates that sell it."""

    name: str
    description: str | None
    price_minor: int
    currency: str
    quantity: int | None
    # How many dates carry this tier, and how it is doing across all of them.
    dates: int
    sold: int
    remaining: int | None
    is_active: bool
    # Set when the dates disagree, so the interface can say so rather than
    # silently showing one of them. A publisher who changed one date by hand
    # deserves to know this plan is no longer the whole truth.
    varies: bool = False
    ticket_type_ids: list[uuid.UUID] = field(default_factory=list)


async def _future_occurrences(
    session: AsyncSession, experience_id: uuid.UUID, *, now: datetime | None = None
) -> list[EventInstance]:
    """Dates a ticket could still be sold for.

    A past date is left alone by everything here. Adding a tier to a night that
    has happened would put something on sale that cannot be attended, and
    changing the price of one would rewrite what people already paid.
    """
    now = now or datetime.now(UTC)
    return list(
        (
            await session.execute(
                select(EventInstance)
                .where(EventInstance.experience_id == experience_id)
                .where(EventInstance.start_time > now)
                .order_by(EventInstance.start_time)
            )
        )
        .scalars()
        .all()
    )


async def _tiers_of(session: AsyncSession, experience_id: uuid.UUID) -> list[TicketType]:
    return list(
        (
            await session.execute(
                select(TicketType)
                .where(TicketType.experience_id == experience_id)
                .order_by(TicketType.position, TicketType.price_minor)
            )
        )
        .scalars()
        .all()
    )


async def plan_for(
    session: AsyncSession, experience_id: uuid.UUID, *, now: datetime | None = None
) -> list[PlanEntry]:
    """What this listing sells, one row per tier rather than per tier per date."""
    now = now or datetime.now(UTC)
    upcoming = {occurrence.id for occurrence in await _future_occurrences(
        session, experience_id, now=now
    )}

    grouped: dict[str, list[TicketType]] = {}
    for tier in await _tiers_of(session, experience_id):
        # Only what is still sellable shapes the plan. A tier left on a date
        # that has passed is history, not an offer.
        if tier.event_instance_id in upcoming:
            grouped.setdefault(tier.name, []).append(tier)

    entries: list[PlanEntry] = []
    for name, tiers in grouped.items():
        # Withdrawn everywhere means no longer sold, so it leaves the plan. The
        # rows stay in the table - an order that bought one still has to read -
        # but this list answers "what does this listing sell", and the answer
        # stopped including it.
        if not any(tier.is_active for tier in tiers):
            continue
        first = tiers[0]
        quantities = {tier.quantity for tier in tiers}
        entries.append(
            PlanEntry(
                name=name,
                description=first.description,
                price_minor=first.price_minor,
                currency=first.currency,
                quantity=first.quantity,
                dates=len(tiers),
                sold=sum(tier.sold for tier in tiers),
                remaining=(
                    None
                    if any(tier.remaining is None for tier in tiers)
                    else sum(tier.remaining or 0 for tier in tiers)
                ),
                is_active=any(tier.is_active for tier in tiers),
                varies=(
                    len({tier.price_minor for tier in tiers}) > 1
                    or len(quantities) > 1
                    or len({tier.description for tier in tiers}) > 1
                ),
                ticket_type_ids=[tier.id for tier in tiers],
            )
        )

    entries.sort(key=lambda entry: (entry.price_minor, entry.name))
    return entries


async def apply_tier(
    session: AsyncSession,
    *,
    experience_id: uuid.UUID,
    name: str,
    price_minor: int,
    currency: str,
    quantity: int | None,
    description: str | None,
    now: datetime | None = None,
) -> int:
    """Put this tier on every future date. Returns how many dates it reached.

    Updates where the name already exists rather than adding a second row, so
    applying the plan twice is the same as applying it once - a publisher fixing
    a typo in a price is doing exactly that.

    ``sold`` is never touched. It is a record of what happened, and a price
    change is not a reason to forget that eleven people already bought one.
    """
    occurrences = await _future_occurrences(session, experience_id, now=now)
    if not occurrences:
        return 0

    existing = {
        (tier.event_instance_id, tier.name): tier
        for tier in await _tiers_of(session, experience_id)
    }
    position = len({tier.name for tier in existing.values()})

    for occurrence in occurrences:
        found = existing.get((occurrence.id, name))
        if found is None:
            session.add(
                TicketType(
                    event_instance_id=occurrence.id,
                    experience_id=experience_id,
                    name=name,
                    description=description,
                    price_minor=price_minor,
                    currency=currency,
                    quantity=quantity,
                    sold=0,
                    position=position,
                    is_active=True,
                    sales_close_at=occurrence.start_time,
                )
            )
            continue

        found.description = description
        found.price_minor = price_minor
        found.currency = currency
        found.quantity = quantity
        # Re-applying a withdrawn tier puts it back on sale, which is what a
        # publisher adding it again plainly means.
        found.is_active = True

    await session.flush()
    return len(occurrences)


async def withdraw_tier(
    session: AsyncSession,
    *,
    experience_id: uuid.UUID,
    name: str,
    now: datetime | None = None,
) -> int:
    """Stop selling this tier on every future date. Returns how many it touched.

    Deactivated rather than deleted, everywhere else in this domain's spirit: an
    order that bought one still has to read.
    """
    upcoming = {
        occurrence.id
        for occurrence in await _future_occurrences(session, experience_id, now=now)
    }
    touched = 0
    for tier in await _tiers_of(session, experience_id):
        if tier.name == name and tier.event_instance_id in upcoming and tier.is_active:
            tier.is_active = False
            touched += 1
    await session.flush()
    return touched


async def materialise_for(
    session: AsyncSession, *, experience_id: uuid.UUID, occurrence: EventInstance
) -> int:
    """Give a newly added date whatever the listing already sells.

    Without this, "define the tickets once" holds only until the publisher adds
    another night - and the night they added would go on sale with nothing on
    it, which is the failure this whole module exists to remove.
    """
    plan = await plan_for(session, experience_id)
    for entry in plan:
        if not entry.is_active:
            continue
        session.add(
            TicketType(
                event_instance_id=occurrence.id,
                experience_id=experience_id,
                name=entry.name,
                description=entry.description,
                price_minor=entry.price_minor,
                currency=entry.currency,
                quantity=entry.quantity,
                sold=0,
                position=0,
                is_active=True,
                sales_close_at=occurrence.start_time,
            )
        )
    await session.flush()
    return len(plan)
