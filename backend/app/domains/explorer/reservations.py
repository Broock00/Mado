"""Reservations (spec COM-001).

Holding a place at something that has a limited number of them.

**This is not a payment.** Secure payments are COM-003 and deliberately not
built. A reservation here is a promise to turn up and a place held in return -
which is what most of what happens in this city actually needs. A supper club
with twelve seats, a workshop with fifteen. Bolting money onto it before that
works would be building the hard part first.

**Overselling is the whole problem.** Everything else is bookkeeping. Two people
taking the last seat at the same instant is not a rare race - it is what happens
whenever something is nearly full, which is exactly when the count matters. So
the capacity check and the decrement happen in one statement the database
serialises, not in a read-then-write that two requests can interleave:

    UPDATE event_instances
       SET remaining = remaining - :party
     WHERE id = :id AND remaining >= :party

Postgres takes a row lock for the duration. The second caller waits, re-reads,
finds the seats gone and is refused. Checking `remaining` in Python and writing
it back would let both callers pass the check before either wrote.

**Capacity is optional.** Most listings do not have a fixed number of places -
a market, a public square. `remaining IS NULL` means unlimited, and reserving
against it still records the intent without pretending to count anything.

**A cancelled reservation returns its seats.** Immediately and by the same
mechanism, because a held seat nobody will use is worse for the publisher than
an empty one they knew about.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.mixins import Timestamps, UUIDPrimaryKey
from app.domains.catalog.models import EventInstance, Experience
from app.domains.explorer.models import SCHEMA
from app.domains.identity.models import User
from app.domains.publisher.models import Publisher

logger = get_logger("mado.reservations")

STATUS_CONFIRMED = "confirmed"
STATUS_CANCELLED = "cancelled"
STATUS_ATTENDED = "attended"
STATUS_NO_SHOW = "no_show"

# Statuses that still occupy a seat.
HOLDING_STATUSES = (STATUS_CONFIRMED, STATUS_ATTENDED)

# The most one person may take in a single reservation. Not a technical limit:
# somebody booking forty places at a twelve-seat supper club is either mistaken
# or blocking everybody else, and both are better caught here than at the door.
MAX_PARTY_SIZE = 10

# How close to the start an explorer may still cancel and free the seat.
# Cancelling as the doors open does the publisher no good - they have already
# bought the food - so the seat is released but the reservation is kept and the
# publisher still sees it.
CANCELLATION_CUTOFF = timedelta(hours=2)

# When "limited" starts. See `_scarcity_threshold`.
SCARCE_FLOOR = 3
SCARCE_CEILING = 20


class Reservation(Base, UUIDPrimaryKey, Timestamps):
    """A held place at one occurrence.

    Keyed to the occurrence rather than the experience: a weekly supper club is
    one Experience with many `event_instances`, and a place at next Tuesday's is
    not a place at the one after.
    """

    __tablename__ = "reservations"
    __table_args__ = (
        # One live reservation per explorer per occurrence. Changing the party
        # size updates that row rather than adding another, so the count cannot
        # drift from what the explorer thinks they booked.
        UniqueConstraint("user_id", "event_instance_id", name="uq_reservation_user_occurrence"),
        Index("ix_reservations_occurrence", "event_instance_id", "status"),
        Index("ix_reservations_user", "user_id", "created_at"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("identity.users.id", ondelete="CASCADE"), index=True
    )
    # No foreign key into catalog: this schema does not read catalog tables
    # directly (modular monolith rule), and a reservation should outlive a
    # listing being withdrawn rather than vanishing from the explorer's history.
    event_instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    experience_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # Denormalised so a reservation still renders after a listing disappears.
    experience_title: Mapped[str] = mapped_column(String(300), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    party_size: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=STATUS_CONFIRMED, server_default=STATUS_CONFIRMED, nullable=False
    )
    # Anything the explorer wants the publisher to know: a dietary restriction,
    # arriving late. Read by a person, so it is free text rather than a form.
    note: Mapped[str | None] = mapped_column(Text, default=None)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    @property
    def is_holding(self) -> bool:
        return self.status in HOLDING_STATUSES


@dataclass(slots=True)
class Availability:
    """What an explorer is told about how full something is."""

    status: str
    capacity: int | None
    remaining: int | None
    # True when the publisher set no capacity at all.
    is_unlimited: bool

    @property
    def can_reserve(self) -> bool:
        return self.status in {"available", "limited"}


def _scarcity_threshold(capacity: int) -> int:
    """How few places left counts as "limited".

    A tenth of capacity, but never more than :data:`SCARCE_CEILING`. Without the
    ceiling a five-thousand-seat hall reads as limited with four hundred seats
    free, which is not scarcity - it is a fifth of a stadium. Urgency is about
    how many places remain, not what fraction of the room they are.

    The floor matters at the other end: a twelve-seat supper club is genuinely
    nearly gone at three, even though three is a quarter of it.
    """
    return max(SCARCE_FLOOR, min(capacity // 10, SCARCE_CEILING))


def availability_of(occurrence: EventInstance) -> Availability:
    """Describe how full an occurrence is (spec 55.05 §33).

    The states come from the spec. "limited" exists because "3 left" and "300
    left" should not read the same, and somebody deciding whether to hurry is
    the whole audience for this field.
    """
    if occurrence.status == "cancelled":
        return Availability("cancelled", occurrence.capacity, 0, is_unlimited=False)

    if occurrence.remaining is None:
        # No capacity set. Most things in a city do not have one, and inventing
        # a number so the field is populated would be inventing a fact.
        return Availability("available", None, None, is_unlimited=True)

    if occurrence.remaining <= 0:
        return Availability("full", occurrence.capacity, 0, is_unlimited=False)

    scarce = occurrence.capacity is not None and occurrence.remaining <= _scarcity_threshold(
        occurrence.capacity
    )
    return Availability(
        "limited" if scarce else "available",
        occurrence.capacity,
        occurrence.remaining,
        is_unlimited=False,
    )


class ReservationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def reserve(
        self,
        user: User,
        occurrence_id: uuid.UUID,
        *,
        party_size: int = 1,
        note: str | None = None,
    ) -> Reservation:
        """Hold places, or fail without holding any."""
        if party_size < 1:
            raise ValidationError(
                "A reservation is for at least one person.", code="PARTY_TOO_SMALL"
            )
        if party_size > MAX_PARTY_SIZE:
            raise ValidationError(
                f"You can reserve for at most {MAX_PARTY_SIZE} people at once. "
                "Contact the publisher for a larger group.",
                code="PARTY_TOO_LARGE",
            )

        occurrence = await self._occurrence(occurrence_id)
        if occurrence.status == "cancelled":
            raise ConflictError("That date has been cancelled.", code="OCCURRENCE_CANCELLED")
        if occurrence.start_time <= datetime.now(UTC):
            raise ConflictError("That has already started.", code="OCCURRENCE_PAST")

        existing = await self._existing(user.id, occurrence_id)
        if existing is not None and existing.is_holding:
            raise ConflictError(
                "You already have a place at this. Change the number of people instead.",
                code="ALREADY_RESERVED",
            )

        await self._take_seats(occurrence, party_size)

        experience = await self.session.get(Experience, occurrence.experience_id)
        if existing is not None:
            # Reusing the cancelled row keeps the unique constraint satisfied
            # and preserves the fact that they booked, cancelled and came back.
            existing.status = STATUS_CONFIRMED
            existing.party_size = party_size
            existing.note = (note or "").strip() or None
            existing.cancelled_at = None
            reservation = existing
        else:
            reservation = Reservation(
                user_id=user.id,
                event_instance_id=occurrence_id,
                experience_id=occurrence.experience_id,
                experience_title=experience.title if experience else "An experience",
                starts_at=occurrence.start_time,
                party_size=party_size,
                note=(note or "").strip() or None,
            )
            self.session.add(reservation)

        await self.session.flush()
        await self._announce("reservation.created", reservation)
        logger.info(
            "reservation_made",
            reservation_id=str(reservation.id),
            occurrence_id=str(occurrence_id),
            party_size=party_size,
        )
        return reservation

    async def change_party_size(
        self, user: User, reservation_id: uuid.UUID, party_size: int
    ) -> Reservation:
        """Take or return the difference, never the whole amount.

        Releasing every seat and re-taking the new number would briefly free
        places somebody else could claim, so an explorer changing from three to
        four could lose all three.
        """
        if party_size < 1 or party_size > MAX_PARTY_SIZE:
            raise ValidationError(
                "That is not a valid number of people.", code="INVALID_PARTY_SIZE"
            )

        reservation = await self._owned(user, reservation_id)
        if not reservation.is_holding:
            raise ConflictError("That reservation is not active.", code="RESERVATION_INACTIVE")

        difference = party_size - reservation.party_size
        if difference == 0:
            return reservation

        occurrence = await self._occurrence(reservation.event_instance_id)
        if difference > 0:
            await self._take_seats(occurrence, difference)
        else:
            await self._return_seats(occurrence, -difference)

        reservation.party_size = party_size
        return reservation

    async def cancel(self, user: User, reservation_id: uuid.UUID) -> Reservation:
        """Give the places back."""
        reservation = await self._owned(user, reservation_id)
        if reservation.status == STATUS_CANCELLED:
            return reservation

        occurrence = await self._occurrence(reservation.event_instance_id)
        await self._return_seats(occurrence, reservation.party_size)

        reservation.status = STATUS_CANCELLED
        reservation.cancelled_at = datetime.now(UTC)
        await self._announce("reservation.cancelled", reservation)
        logger.info(
            "reservation_cancelled",
            reservation_id=str(reservation.id),
            late=datetime.now(UTC) > reservation.starts_at - CANCELLATION_CUTOFF,
        )
        return reservation

    async def _announce(self, event_type: str, reservation: Reservation) -> None:
        """Tell the publisher's systems, if they asked to be told (spec DEV-003).

        Addressed to the owner of the listing rather than to the explorer who
        reserved: a webhook goes to the party running the event, and the person
        turning up has notifications instead.

        No name and no note. Both are things the explorer told this publisher
        directly, and a webhook is a copy sent to whatever server they pointed
        us at - which is a different audience from the door list. A receiver
        that wants names calls the attendees endpoint with a scoped key.
        """
        from app.domains.developer.webhooks import emit

        experience = await self.session.get(Experience, reservation.experience_id)
        if experience is None:
            return
        publisher = await self.session.get(Publisher, experience.publisher_id)
        if publisher is None or publisher.owner_user_id is None:
            return

        await emit(
            self.session,
            event_type=event_type,
            owner_user_id=publisher.owner_user_id,
            data={
                "reservationId": str(reservation.id),
                "experienceId": str(reservation.experience_id),
                "eventInstanceId": str(reservation.event_instance_id),
                "startsAt": reservation.starts_at.isoformat(),
                "partySize": reservation.party_size,
            },
        )

    async def mine(self, user: User, *, upcoming_only: bool = True) -> list[Reservation]:
        stmt = select(Reservation).where(Reservation.user_id == user.id)
        if upcoming_only:
            stmt = stmt.where(
                Reservation.starts_at >= datetime.now(UTC),
                Reservation.status != STATUS_CANCELLED,
            )
        result = await self.session.execute(stmt.order_by(Reservation.starts_at))
        return list(result.scalars().all())

    async def for_occurrence(self, occurrence_id: uuid.UUID) -> list[Reservation]:
        """Who is coming. For the publisher of that listing only.

        Unlike the analytics dashboards, which never name a person, this
        deliberately does: the explorer made a commitment to this publisher and
        expects to be found on a list at the door. Ownership is checked by the
        caller before this runs.
        """
        result = await self.session.execute(
            select(Reservation)
            .where(
                Reservation.event_instance_id == occurrence_id,
                Reservation.status.in_(HOLDING_STATUSES),
            )
            .order_by(Reservation.created_at)
        )
        return list(result.scalars().all())

    async def release_for_occurrence(self, occurrence_id: uuid.UUID) -> list[Reservation]:
        """Cancel every reservation on a date the publisher has called off.

        Not the explorer changing their mind, so no cancellation cutoff and no
        lateness to record - they did nothing. Returns the reservations as they
        were, because the caller has to tell those people (see
        :mod:`app.domains.explorer.alerts`) and after this runs there is nothing
        left in the table to say who they were.

        Without this the reservation stays ``confirmed`` against a cancelled
        date, so the explorer's own trips list goes on promising them a place at
        something that is not happening - and it would say that on the same
        screen as the alert saying it is cancelled.

        No ``reservation.cancelled`` webhook per person: the publisher's systems
        are already being sent ``event.cancelled``, and following it with forty
        individual cancellations describes one decision forty-one times.
        """
        reservations = await self.for_occurrence(occurrence_id)
        if not reservations:
            return []

        # Seats are handed back even though the date is off. `remaining` is
        # meant to be capacity minus places held, and a number that is only
        # correct while nothing unusual happens is one nobody can later trust
        # for a report or a refund.
        occurrence = await self._occurrence(occurrence_id)
        for reservation in reservations:
            await self._return_seats(occurrence, reservation.party_size)
            reservation.status = STATUS_CANCELLED
            reservation.cancelled_at = datetime.now(UTC)

        logger.info(
            "reservations_released",
            occurrence_id=str(occurrence_id),
            reservations=len(reservations),
            places=sum(r.party_size for r in reservations),
        )
        return reservations

    # ----------------------------------------------------------- internals

    async def _take_seats(self, occurrence: EventInstance, count: int) -> None:
        """Claim places, or refuse. One statement, so two callers cannot both win.

        The guard is in the WHERE clause rather than in Python. Reading
        `remaining`, deciding, then writing would let two requests both read 1,
        both decide yes, and both write 0 - selling the same seat twice.
        """
        if occurrence.remaining is None:
            return  # Unlimited: nothing to count down.

        result = await self.session.execute(
            update(EventInstance)
            .where(
                EventInstance.id == occurrence.id,
                EventInstance.remaining >= count,
            )
            .values(remaining=EventInstance.remaining - count)
        )

        if result.rowcount == 0:
            # The row did not match, which can only mean there were not enough
            # places by the time the database got to us.
            raise ConflictError(
                "There are not enough places left." if count > 1 else "That is fully booked.",
                code="NOT_ENOUGH_PLACES",
            )

        # Refreshed, never adjusted by hand. Subtracting `count` from the loaded
        # attribute as well as in SQL took the seats twice - the ORM had already
        # picked up the new value, so a party of two removed four places. The
        # statement above is the only thing that may change this number.
        await self.session.refresh(occurrence, ["remaining"])

    async def _return_seats(self, occurrence: EventInstance, count: int) -> None:
        if occurrence.remaining is None:
            return

        await self.session.execute(
            update(EventInstance)
            .where(EventInstance.id == occurrence.id)
            .values(
                # Clamped at capacity so repeated cancellations cannot inflate a
                # listing beyond the number of places it actually has.
                remaining=EventInstance.remaining + count
                if occurrence.capacity is None
                else func.least(EventInstance.remaining + count, occurrence.capacity)
            )
        )
        await self.session.refresh(occurrence, ["remaining"])

    async def _occurrence(self, occurrence_id: uuid.UUID) -> EventInstance:
        occurrence = await self.session.get(EventInstance, occurrence_id)
        if occurrence is None:
            raise NotFoundError("That date does not exist.", code="OCCURRENCE_NOT_FOUND")
        return occurrence

    async def _existing(self, user_id: uuid.UUID, occurrence_id: uuid.UUID) -> Reservation | None:
        result = await self.session.execute(
            select(Reservation).where(
                Reservation.user_id == user_id,
                Reservation.event_instance_id == occurrence_id,
            )
        )
        return result.scalars().first()

    async def _owned(self, user: User, reservation_id: uuid.UUID) -> Reservation:
        reservation = await self.session.get(Reservation, reservation_id)
        if reservation is None or reservation.user_id != user.id:
            # 404 rather than 403: a stranger should not be able to tell
            # somebody else's reservation apart from one that does not exist.
            raise NotFoundError("Reservation not found.", code="RESERVATION_NOT_FOUND")
        return reservation
