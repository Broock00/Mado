"""What a publisher has earned, and what Mado still owes them.

The ledger in `fees.py` records each sale. This turns those rows into the two
questions anybody actually asks: *what has this business taken*, and *what is
still sitting with Mado*.

**A payout records a transfer; it never makes one.** Disbursing needs a verified
bank account per publisher and a provider payout API, and Mado has neither. So
`build` groups what is owed and `mark_paid` records that somebody sent it, with
their reference. A status that advanced itself would tell a publisher they had
been paid when nothing had moved - the same failure the integration stubs exist
to prevent, wearing an accounting hat.

**Totals are computed, never stored on the publisher.** A cached balance is one
missed update away from being permanently wrong, with nothing to notice - the
same reason `repost_count` is recomputed from its rows rather than incremented.
These are sums over an indexed partial, which is cheap and cannot drift.

Currency groups everything. A business selling in Addis and in Nairobi has two
balances, and a single figure adding birr to shillings is a number that means
nothing at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.domains.commerce.models import (
    PAYOUT_OWING,
    PAYOUT_PAID,
    LedgerEntry,
    Payout,
)

logger = get_logger("mado.payouts")


@dataclass(frozen=True, slots=True)
class CurrencyTotals:
    """One currency's worth of a publisher's earnings."""

    currency: str
    gross_minor: int
    fee_minor: int
    net_minor: int
    # The part of `net_minor` not yet swept into a payout. Reported separately
    # rather than as its own total, because "you have earned X, of which Y is
    # still to come" is the sentence a publisher wants and two unrelated numbers
    # are not.
    owing_minor: int
    sales: int


class PayoutService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------ reading

    async def totals_for(
        self,
        publisher_id: uuid.UUID,
        *,
        since: datetime | None = None,
    ) -> list[CurrencyTotals]:
        """What one publisher has earned, one row per currency.

        `since` narrows the earnings window without narrowing what is owed:
        money earned before the window is still owed, and hiding it would make
        the outstanding figure disagree with the payout that eventually arrives.
        """
        stmt = (
            select(
                LedgerEntry.currency,
                func.coalesce(func.sum(LedgerEntry.gross_minor), 0),
                func.coalesce(func.sum(LedgerEntry.fee_minor), 0),
                func.coalesce(func.sum(LedgerEntry.net_minor), 0),
                func.coalesce(
                    func.sum(LedgerEntry.net_minor).filter(LedgerEntry.payout_id.is_(None)), 0
                ),
                func.count(),
            )
            .where(LedgerEntry.publisher_id == publisher_id)
            .group_by(LedgerEntry.currency)
            .order_by(func.sum(LedgerEntry.net_minor).desc())
        )
        if since is not None:
            stmt = stmt.where(LedgerEntry.earned_at >= since)

        rows = (await self.session.execute(stmt)).all()
        return [
            CurrencyTotals(
                currency=currency,
                gross_minor=int(gross),
                fee_minor=int(fee),
                net_minor=int(net),
                owing_minor=int(owing),
                sales=int(sales),
            )
            for currency, gross, fee, net, owing, sales in rows
        ]

    async def history_for(self, publisher_id: uuid.UUID, *, limit: int = 24) -> list[Payout]:
        result = await self.session.execute(
            select(Payout)
            .where(Payout.publisher_id == publisher_id)
            .order_by(Payout.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def outstanding(self, *, limit: int = 200) -> list[tuple[uuid.UUID, str, int, int]]:
        """Everything Mado owes, across every publisher.

        The administrator's queue: publisher, currency, total, number of sales.
        Ordered by size, because a payout run pays the largest debts first if it
        pays only some of them.
        """
        result = await self.session.execute(
            select(
                LedgerEntry.publisher_id,
                LedgerEntry.currency,
                func.sum(LedgerEntry.net_minor),
                func.count(),
            )
            .where(LedgerEntry.payout_id.is_(None))
            .group_by(LedgerEntry.publisher_id, LedgerEntry.currency)
            .order_by(func.sum(LedgerEntry.net_minor).desc())
            .limit(limit)
        )
        return [
            (publisher_id, currency, int(total), int(count))
            for publisher_id, currency, total, count in result.all()
        ]

    async def get(self, payout_id: uuid.UUID) -> Payout:
        payout = await self.session.get(Payout, payout_id)
        if payout is None:
            raise NotFoundError("Payout not found.", code="PAYOUT_NOT_FOUND")
        return payout

    # ------------------------------------------------------------ writing

    async def build(
        self,
        publisher_id: uuid.UUID,
        currency: str,
        *,
        up_to: datetime | None = None,
        now: datetime | None = None,
    ) -> Payout:
        """Group everything owed to one publisher into one payout.

        `up_to` cuts the batch at a date, so a run on the 1st can settle last
        month without also sweeping in this morning's sales.

        The entries are claimed by a single conditional UPDATE keyed on
        `payout_id IS NULL`, not read then written. Two administrators pressing
        the button at once would otherwise both read the same unpaid rows and
        create two payouts for the same money.
        """
        now = now or datetime.now(UTC)
        currency = currency.upper()

        claimable = select(LedgerEntry.id).where(
            LedgerEntry.publisher_id == publisher_id,
            LedgerEntry.currency == currency,
            LedgerEntry.payout_id.is_(None),
        )
        if up_to is not None:
            claimable = claimable.where(LedgerEntry.earned_at <= up_to)

        # The payout has to exist before entries can point at it, and its totals
        # are only known after they do - so it is written empty and corrected
        # from what the claim actually took. Sizing it from a SELECT first would
        # be the read-then-write race this exists to avoid.
        payout = Payout(
            publisher_id=publisher_id,
            currency=currency,
            total_minor=0,
            entry_count=0,
            period_start=now,
            period_end=up_to or now,
            status=PAYOUT_OWING,
        )
        self.session.add(payout)
        await self.session.flush()

        claimed = (
            await self.session.execute(
                update(LedgerEntry)
                .where(LedgerEntry.id.in_(claimable))
                .values(payout_id=payout.id)
                .returning(LedgerEntry.net_minor, LedgerEntry.earned_at)
            )
        ).all()

        if not claimed:
            # Nothing was owed. The empty payout is deleted rather than left as
            # a zero row: a list of payouts that includes runs which paid
            # nothing makes the real ones harder to find.
            await self.session.delete(payout)
            await self.session.flush()
            raise ConflictError(
                "There is nothing owed to that publisher in that currency.",
                code="NOTHING_OWING",
            )

        payout.total_minor = sum(int(net) for net, _ in claimed)
        payout.entry_count = len(claimed)
        payout.period_start = min(earned for _, earned in claimed)
        payout.period_end = max(earned for _, earned in claimed)
        await self.session.flush()

        logger.info(
            "payout_built",
            payout_id=str(payout.id),
            publisher_id=str(publisher_id),
            currency=currency,
            total_minor=payout.total_minor,
            entries=payout.entry_count,
        )
        return payout

    async def mark_paid(
        self,
        payout_id: uuid.UUID,
        *,
        reference: str,
        note: str | None = None,
        now: datetime | None = None,
    ) -> Payout:
        """Record that the transfer was actually sent.

        A reference is required. The whole value of this row is that it can be
        reconciled against a bank statement, and one marked paid with nothing to
        match it against is a claim rather than a record.
        """
        payout = await self.get(payout_id)
        if payout.status == PAYOUT_PAID:
            # Not an error. Two people confirming the same transfer is ordinary,
            # and the first reference is the one that matches the bank.
            return payout

        reference = reference.strip()
        if not reference:
            raise ConflictError(
                "Record the transfer reference, so this can be reconciled later.",
                code="REFERENCE_REQUIRED",
            )

        payout.status = PAYOUT_PAID
        payout.paid_at = now or datetime.now(UTC)
        payout.reference = reference[:120]
        payout.note = (note or None) and note.strip()[:300]
        await self.session.flush()

        logger.info(
            "payout_marked_paid",
            payout_id=str(payout.id),
            publisher_id=str(payout.publisher_id),
            total_minor=payout.total_minor,
            currency=payout.currency,
        )
        return payout


__all__ = ["CurrencyTotals", "PayoutService"]
