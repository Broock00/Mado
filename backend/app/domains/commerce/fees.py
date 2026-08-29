"""The platform's cut, and the record of what is owed because of it.

Mado's marketplace is the revenue stream BUSINESS-09 sizes at 20% of the
long-term mix, and it is the one that was already collecting money: checkout
sends every buyer to Mado's own Chapa or Stripe account, so the whole of every
sale arrives here. What was missing was any statement of whose it was. This
module is that statement.

**The fee is absorbed, not added.** The buyer pays the price the publisher set;
the commission comes out of the publisher's share. The alternative - a service
charge added at checkout - changes the agreed price after the explorer has
chosen, which is the exact thing `checkout.py` copies prices to prevent, and it
makes the ticket cost more here than at the door. BUSINESS-90.01 §9 lists
service charges as a possible source; taking one would be a deliberate pricing
decision, not a default.

**Basis points, not a percentage float.** 250 bps is 2.5%, exactly, in an
integer. A rate held as 0.025 invites the multiplication to happen in floating
point, and the santim that goes missing there is somebody's money.

**Rounding favours the publisher.** The fee floors, so a fraction of a santim is
never rounded up into Mado's pocket. Over a large number of small orders that is
a rounding difference of a few birr; the direction it errs in is the part worth
being deliberate about.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.domains.commerce.models import (
    ORDER_PAID,
    LedgerEntry,
    Order,
)
from app.domains.publisher.models import Publisher

logger = get_logger("mado.fees")

BPS_DENOMINATOR = 10_000
# A rate above this is refused rather than stored. Not a policy about what Mado
# should charge - it is a guard against a typo in an admin field taking half of
# somebody's takings because 2500 was meant to be 250.
MAX_FEE_BPS = 3_000


def fee_for(amount_minor: int, rate_bps: int) -> int:
    """The commission on one order, in minor units.

    Floors, and is clamped to the order: a rate that somehow exceeded 100% must
    not produce a negative net, because every consumer of that number - the
    dashboard, the payout, the check constraint - treats it as money owed.
    """
    if amount_minor <= 0 or rate_bps <= 0:
        return 0
    fee = amount_minor * rate_bps // BPS_DENOMINATOR
    return min(fee, amount_minor)


def rate_for(publisher: Publisher | None) -> int:
    """What this publisher pays, in basis points.

    A publisher with no negotiated rate pays the platform default. Read at
    checkout and then copied onto the order, so changing either one never
    restates a sale that already happened.
    """
    negotiated = getattr(publisher, "fee_bps", None) if publisher is not None else None
    rate = get_settings().platform_fee_bps if negotiated is None else negotiated
    # Defensive on both sides. A negative rate would pay the publisher more than
    # the buyer paid; an absurd one would be a typo nobody noticed.
    return max(0, min(int(rate), MAX_FEE_BPS))


async def rate_for_publisher(session: AsyncSession, publisher_id: uuid.UUID | None) -> int:
    if publisher_id is None:
        return get_settings().platform_fee_bps
    publisher = await session.get(Publisher, publisher_id)
    return rate_for(publisher)


async def record_sale(session: AsyncSession, order: Order) -> LedgerEntry | None:
    """Write what this paid order earned, once.

    Called from `Checkout._mark_paid`, which is the only place an order becomes
    paid, so the entry cannot exist for money that did not arrive. It returns
    None rather than raising for the cases that legitimately earn nothing - a
    free ticket, an order with no publisher recorded - because those are not
    failures and the caller has nothing useful to do about them.

    **Writing the ledger must never fail a settlement.** By the time this runs
    the explorer has paid and the tickets are being issued; refusing the whole
    transaction because an accounting row would not write would take money and
    give nothing back. A conflict means another delivery of the same webhook won
    the race and the entry is already there, which is the correct outcome
    arrived at by the other path.
    """
    if order.status != ORDER_PAID:
        # A guard against a future caller, not a case that happens today. An
        # entry for an unpaid order is a debt against money nobody has.
        raise ValueError("A ledger entry may only be written for a paid order.")
    if order.amount_minor <= 0 or order.publisher_id is None:
        return None

    entry = LedgerEntry(
        order_id=order.id,
        publisher_id=order.publisher_id,
        gross_minor=order.amount_minor,
        fee_minor=order.platform_fee_minor,
        net_minor=order.net_minor,
        fee_rate_bps=order.fee_rate_bps,
        currency=order.currency,
        earned_at=order.paid_at or datetime.now(UTC),
    )
    try:
        async with session.begin_nested():
            session.add(entry)
    except IntegrityError:
        logger.info("ledger_entry_already_written", order_id=str(order.id))
        return None

    logger.info(
        "ledger_entry_written",
        order_id=str(order.id),
        publisher_id=str(order.publisher_id),
        gross_minor=entry.gross_minor,
        fee_minor=entry.fee_minor,
        currency=entry.currency,
    )
    return entry


async def earnings_for(
    session: AsyncSession,
    publisher_id: uuid.UUID,
    *,
    since: datetime | None = None,
) -> list[LedgerEntry]:
    """Every entry for one publisher, newest first.

    Deliberately returns the rows rather than a total. A publisher asking what
    they earned wants to see the sales it came from, and a caller that only
    needs a sum can add these up - whereas a caller given only a sum cannot
    recover the sales.
    """
    stmt = select(LedgerEntry).where(LedgerEntry.publisher_id == publisher_id)
    if since is not None:
        stmt = stmt.where(LedgerEntry.earned_at >= since)
    result = await session.execute(stmt.order_by(LedgerEntry.earned_at.desc()))
    return list(result.scalars().all())


__all__ = [
    "BPS_DENOMINATOR",
    "MAX_FEE_BPS",
    "earnings_for",
    "fee_for",
    "rate_for",
    "rate_for_publisher",
    "record_sale",
]
