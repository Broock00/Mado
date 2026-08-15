"""Admitting somebody at the door (spec COM-003).

One scan, one verdict. The interesting cases are not the valid ticket - they are
everything else, and each one needs its own answer because the person on the
door has to do something different about each.

**A second scan of the same ticket must fail.** That is the entire point of
scanning: a screenshot forwarded to three friends is one ticket, and if the
second scan reported success the other two walk in free. So a check-in is not
idempotent in the "repeat it safely" sense - the first is an admission and the
rest are refusals that say when the first happened, which is what lets a door
tell a duplicate from a genuine re-entry.

**A real ticket for another date is not the same as no ticket at all.** Somebody
holding next Friday's ticket tonight has made a mistake, not an attempt; the
door needs to say which. Rejecting both as "invalid" turns a two-second
correction into an argument.

**A ticket for another date carries no details back.** The verdict says it is
not for this door and stops there. Whoever is scanning publishes *this* event,
and a code they happen to hold is not permission to read a stranger's booking.

The QR carries the code and nothing else. A ticket that carried its own claims
would have to be signed or be forgeable, and a ticket that carried the holder's
name would print it for anybody who photographs the screen. The code is looked
up here, and the database is what answers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.commerce.models import (
    TICKET_CHECKED_IN,
    TICKET_ISSUED,
    Order,
    Ticket,
)
from app.domains.identity.models import UserProfile

# What the door was told. Codes rather than sentences, like every other verdict
# the platform sends a client - the wording belongs to whoever is reading it.
ADMITTED = "admitted"
ALREADY_ADMITTED = "already_admitted"
WRONG_EVENT = "wrong_event"
VOID = "void"
UNKNOWN = "unknown"


@dataclass(slots=True)
class Scan:
    verdict: str
    # Present only when the ticket is for this door. Nothing about somebody
    # else's booking travels back on a wrong_event or an unknown code.
    name: str | None = None
    ticket_type_name: str | None = None
    reference: str | None = None
    checked_in_at: datetime | None = None
    # Where this door stands after the scan, so a phone can show "42 in" without
    # a second request.
    admitted_count: int = 0
    issued_count: int = 0


class DoorService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def scan(
        self,
        *,
        occurrence_id: uuid.UUID,
        code: str,
        now: datetime | None = None,
        admit: bool = True,
    ) -> Scan:
        """Look up a scanned code and, unless only peeking, admit it.

        ``admit=False`` reads without spending the ticket, which is what a
        publisher checking a code by hand wants - looking somebody up should not
        silently use their admission.
        """
        now = now or datetime.now(UTC)
        cleaned = code.strip().upper()

        ticket = (
            await self.session.execute(select(Ticket).where(Ticket.code == cleaned))
        ).scalars().first()

        if ticket is None:
            return await self._with_counts(Scan(verdict=UNKNOWN), occurrence_id)

        if ticket.event_instance_id != occurrence_id:
            # Deliberately bare. It is a real ticket, and it is none of this
            # door's business whose.
            return await self._with_counts(Scan(verdict=WRONG_EVENT), occurrence_id)

        details = await self._details(ticket)

        if ticket.status not in (TICKET_ISSUED, TICKET_CHECKED_IN):
            details.verdict = VOID
            return await self._with_counts(details, occurrence_id)

        if ticket.status == TICKET_CHECKED_IN or ticket.checked_in_at is not None:
            details.verdict = ALREADY_ADMITTED
            details.checked_in_at = ticket.checked_in_at
            return await self._with_counts(details, occurrence_id)

        if admit:
            ticket.status = TICKET_CHECKED_IN
            ticket.checked_in_at = now
            await self.session.flush()
            details.checked_in_at = now

        details.verdict = ADMITTED
        return await self._with_counts(details, occurrence_id)

    async def _details(self, ticket: Ticket) -> Scan:
        order = await self.session.get(Order, ticket.order_id)
        profile = (
            await self.session.execute(
                select(UserProfile).where(UserProfile.user_id == ticket.user_id)
            )
        ).scalars().first()
        return Scan(
            verdict=UNKNOWN,
            name=profile.display_name if profile else "Someone",
            ticket_type_name=ticket.ticket_type_name,
            reference=order.reference if order else None,
        )

    async def _with_counts(self, scan: Scan, occurrence_id: uuid.UUID) -> Scan:
        rows = (
            await self.session.execute(
                select(Ticket.status).where(Ticket.event_instance_id == occurrence_id)
            )
        ).scalars().all()
        live = (TICKET_ISSUED, TICKET_CHECKED_IN)
        scan.issued_count = sum(1 for status in rows if status in live)
        scan.admitted_count = sum(1 for status in rows if status == TICKET_CHECKED_IN)
        return scan
