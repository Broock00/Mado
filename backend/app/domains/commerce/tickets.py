"""Ticket tiers, and what the page should offer (spec COM-002, 55.05 §20-24).

Two jobs. A publisher describes what admissions exist for a date, and an
explorer's page has to decide what to say about price, availability and what
the button does. The spec spends most of its ticketing section on the second,
because that is where a details page misleads people.

**Codes, not sentences.** Nothing here returns "Almost sold out". It returns
`almost_sold_out`, and the client renders it - the same rule as everywhere else
the API says something a person reads, so there is one place Amharic lives
rather than two that drift. Notifications are the documented exception because
they are written down and read later; a page is neither.

**Never imply a fixed price when only a floor is known** (§21). A tier list with
two prices is a range and says so; an experience whose publisher recorded a
starting price and no ceiling is "from", and rendering that as a single figure
is the difference between information and a promise.

**External ticketing is not a sale** (§64). Where a publisher sells somewhere
else, the button leaves Mado and the platform records that somebody went - not
that they bought. Anything else invents a fact about a transaction on a server
we cannot see.

**Invite-only is absent.** The spec lists it as an availability state and the
platform has no invitations, so there is nothing that could ever produce it.
Emitting a state no code path can reach is worse than omitting one: it puts a
branch in every client for a case that never arrives, and the first real
invite-only feature will want a different shape anyway.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.domains.catalog.models import EventInstance, Experience
from app.domains.commerce.models import TicketType
from app.domains.explorer.reservations import availability_of

logger = get_logger("mado.tickets")

# Availability, from spec 55.05 §20.
AVAILABLE = "tickets_available"
LIMITED = "limited"
ALMOST_SOLD_OUT = "almost_sold_out"
SOLD_OUT = "sold_out"
REGISTRATION_OPEN = "registration_open"
REGISTRATION_CLOSED = "registration_closed"
FREE_ENTRY = "free_entry"
CANCELLED = "cancelled"
ENDED = "ended"
NOT_ON_SALE_YET = "not_on_sale_yet"

# What the primary button does, from §22. One code per behaviour rather than
# per wording: "Reserve" and "Book" are the same action with different labels,
# and letting the server pick between synonyms is letting it write copy.
CTA_GET_TICKETS = "get_tickets"
CTA_REGISTER = "register"
CTA_RESERVE = "reserve"
CTA_EXTERNAL = "external_tickets"
CTA_DIRECTIONS = "directions"
CTA_FIND_SIMILAR = "find_similar"
CTA_NONE = "none"

# Price shapes, from §21.
PRICE_FREE = "free"
PRICE_FIXED = "fixed"
PRICE_FROM = "from"
PRICE_RANGE = "range"

# Below this many of a tier left, an explorer is told to hurry. Deliberately
# tighter than the room-level "limited" in COM-001: a tier running out is a
# smaller, sharper fact than a venue filling up.
ALMOST_GONE = 5

MAX_TIERS = 12
MAX_TIER_NAME = 80


@dataclass(slots=True)
class TierView:
    tier: TicketType
    remaining: int | None
    on_sale: bool
    # Why it is not on sale, when it is not. A greyed-out row with no reason is
    # the thing people email support about.
    reason: str | None = None


@dataclass(slots=True)
class Ticketing:
    """Everything the details page needs to render its action area."""

    availability: str
    cta: str
    price_type: str
    currency: str
    min_price_minor: int | None = None
    max_price_minor: int | None = None
    external_url: str | None = None
    tiers: list[TierView] = field(default_factory=list)
    # The room, not the tier. Kept separate because "12 seats left" and "2 VIP
    # left" are different sentences and the page may show both.
    seats_remaining: int | None = None

    @property
    def is_purchasable(self) -> bool:
        return self.cta in {CTA_GET_TICKETS, CTA_REGISTER}


def sales_window(tier: TicketType, now: datetime) -> tuple[bool, str | None]:
    if not tier.is_active:
        return False, "withdrawn"
    if tier.sales_open_at is not None and now < tier.sales_open_at:
        return False, "not_open_yet"
    if tier.sales_close_at is not None and now >= tier.sales_close_at:
        return False, "closed"
    if tier.remaining is not None and tier.remaining <= 0:
        return False, "sold_out"
    return True, None


def _price_shape(
    experience: Experience, tiers: list[TicketType]
) -> tuple[str, int | None, int | None]:
    """What to say about price, from the tiers if there are any.

    Tiers win over the experience's own price fields when both exist: a tier is
    a thing somebody can actually buy, and the experience price is a summary
    that may predate it.
    """
    sellable = [t for t in tiers if t.is_active]
    if sellable:
        prices = sorted(t.price_minor for t in sellable)
        low, high = prices[0], prices[-1]
        if high == 0:
            return PRICE_FREE, 0, 0
        if low == high:
            return PRICE_FIXED, low, high
        return PRICE_RANGE, low, high

    if experience.price_type == "free":
        return PRICE_FREE, 0, 0

    from app.integrations.payments import major_to_minor

    amount = experience.price_amount
    ceiling = experience.price_max
    if amount is None:
        return PRICE_FREE if experience.price_type == "free" else PRICE_FIXED, None, None

    low = major_to_minor(amount, experience.currency)
    if ceiling is not None:
        high = major_to_minor(ceiling, experience.currency)
        return (PRICE_RANGE if high != low else PRICE_FIXED), low, high
    # A floor and no ceiling. §21 is explicit that this must not read as a
    # fixed price - somebody arriving with 200 birr because the page said 200
    # is the failure this exists to prevent.
    if experience.price_type in {"from", "starting_at", "varies"}:
        return PRICE_FROM, low, None
    return PRICE_FIXED, low, low


def summarise(
    experience: Experience,
    occurrence: EventInstance,
    tiers: list[TicketType],
    *,
    now: datetime | None = None,
) -> Ticketing:
    """Decide the availability state and the primary action for one date."""
    now = now or datetime.now(UTC)
    starts = occurrence.start_time
    if starts.tzinfo is None:
        starts = starts.replace(tzinfo=UTC)

    price_type, low, high = _price_shape(experience, tiers)
    external = (experience.external_ticket_url or "").strip() or None
    room = availability_of(occurrence)

    base = Ticketing(
        availability=AVAILABLE,
        cta=CTA_NONE,
        price_type=price_type,
        currency=experience.currency,
        min_price_minor=low,
        max_price_minor=high,
        external_url=external,
        seats_remaining=room.remaining,
    )

    if occurrence.status == "cancelled":
        base.availability = CANCELLED
        base.cta = CTA_FIND_SIMILAR
        return base

    if starts <= now:
        # §22: an event that has happened offers the next one, not a button
        # that cannot work.
        base.availability = ENDED
        base.cta = CTA_FIND_SIMILAR
        return base

    if external is not None:
        # The publisher sells elsewhere. Mado has no inventory to speak for, so
        # it states neither availability nor a count.
        base.availability = AVAILABLE
        base.cta = CTA_EXTERNAL
        base.tiers = []
        return base

    views = [
        TierView(tier=tier, remaining=tier.remaining, on_sale=ok, reason=why)
        for tier in sorted(tiers, key=lambda t: (t.position, t.price_minor))
        for ok, why in [sales_window(tier, now)]
        if tier.is_active
    ]
    base.tiers = views
    open_tiers = [view for view in views if view.on_sale]

    if not views:
        # No tiers at all. Falls back to what COM-001 already does with the
        # room: a capacity means places can be held, and no capacity on a free
        # listing means walk in.
        if room.status == "full":
            base.availability = SOLD_OUT
            base.cta = CTA_NONE
        elif room.is_unlimited and price_type == PRICE_FREE:
            base.availability = FREE_ENTRY
            base.cta = CTA_DIRECTIONS
        elif room.can_reserve:
            base.availability = LIMITED if room.status == "limited" else AVAILABLE
            base.cta = CTA_RESERVE
        else:
            base.cta = CTA_NONE
        return base

    everything_free = all(view.tier.is_free for view in views)

    if not open_tiers:
        reasons = {view.reason for view in views}
        if reasons == {"not_open_yet"}:
            base.availability = NOT_ON_SALE_YET
        elif "sold_out" in reasons and reasons <= {"sold_out", "withdrawn"}:
            base.availability = SOLD_OUT
        else:
            base.availability = REGISTRATION_CLOSED if everything_free else SOLD_OUT
        base.cta = CTA_NONE
        return base

    # The room can run out before any single tier does.
    if room.status == "full":
        base.availability = SOLD_OUT
        base.cta = CTA_NONE
        return base

    left = [view.remaining for view in open_tiers if view.remaining is not None]
    scarce_tier = bool(left) and min(left) <= ALMOST_GONE and len(left) == len(open_tiers)

    if everything_free:
        base.availability = REGISTRATION_OPEN
        base.cta = CTA_REGISTER
    elif scarce_tier:
        base.availability = ALMOST_SOLD_OUT
        base.cta = CTA_GET_TICKETS
    elif room.status == "limited":
        base.availability = LIMITED
        base.cta = CTA_GET_TICKETS
    else:
        base.availability = AVAILABLE
        base.cta = CTA_GET_TICKETS
    return base


class TicketTypeService:
    """Publisher-side management of tiers."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def for_occurrence(self, occurrence_id: uuid.UUID) -> list[TicketType]:
        result = await self.session.execute(
            select(TicketType)
            .where(TicketType.event_instance_id == occurrence_id)
            .order_by(TicketType.position, TicketType.price_minor)
        )
        return list(result.scalars().all())

    async def create(
        self,
        *,
        occurrence: EventInstance,
        name: str,
        price_minor: int,
        currency: str,
        quantity: int | None = None,
        description: str | None = None,
        position: int | None = None,
        sales_open_at: datetime | None = None,
        sales_close_at: datetime | None = None,
    ) -> TicketType:
        name = (name or "").strip()
        if not name:
            raise ValidationError("Give the ticket a name.", code="TICKET_TYPE_NAME_REQUIRED")
        if price_minor < 0:
            raise ValidationError("A price cannot be negative.", code="TICKET_PRICE_INVALID")
        if quantity is not None and quantity < 0:
            raise ValidationError("A quantity cannot be negative.", code="TICKET_QUANTITY_INVALID")
        if (
            sales_open_at is not None
            and sales_close_at is not None
            and sales_close_at <= sales_open_at
        ):
            raise ValidationError(
                "Sales must close after they open.", code="TICKET_SALES_WINDOW_INVALID"
            )

        existing = await self.for_occurrence(occurrence.id)
        if len(existing) >= MAX_TIERS:
            raise ConflictError(
                f"A date can have at most {MAX_TIERS} ticket types.",
                code="TICKET_TYPE_LIMIT_REACHED",
            )
        if any(tier.name.lower() == name.lower() and tier.is_active for tier in existing):
            raise ConflictError(
                "There is already a ticket with that name on this date.",
                code="TICKET_TYPE_DUPLICATE",
            )

        tier = TicketType(
            event_instance_id=occurrence.id,
            experience_id=occurrence.experience_id,
            name=name[:MAX_TIER_NAME],
            description=(description or None),
            price_minor=price_minor,
            currency=currency.upper(),
            quantity=quantity,
            position=position if position is not None else len(existing),
            sales_open_at=sales_open_at,
            sales_close_at=sales_close_at,
        )
        self.session.add(tier)
        await self.session.flush()
        logger.info(
            "ticket_type_created",
            ticket_type_id=str(tier.id),
            occurrence_id=str(occurrence.id),
            price_minor=price_minor,
            quantity=quantity,
        )
        return tier

    async def withdraw(self, tier_id: uuid.UUID) -> TicketType:
        """Stop selling a tier without deleting what was already sold.

        Nobody can buy it afterwards, and every order that references it still
        reads - which is the whole reason this is a flag and not a `DELETE`.
        """
        tier = await self.session.get(TicketType, tier_id)
        if tier is None:
            raise NotFoundError("Ticket type not found.", code="TICKET_TYPE_NOT_FOUND")
        tier.is_active = False
        await self.session.flush()
        return tier
