"""Reservation tests (spec COM-001).

Overselling is the failure that matters. Everything else here is bookkeeping
that fails loudly; selling the same seat twice fails quietly and somebody is
turned away at the door.

The concurrency guarantee itself lives in SQL - a conditional UPDATE the
database serialises - so it cannot be proved in-process. It is verified against
a running server instead: ten simultaneous claims on three seats yield exactly
three. What is asserted here is the arithmetic and the states around it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.domains.explorer.reservations import (
    CANCELLATION_CUTOFF,
    HOLDING_STATUSES,
    MAX_PARTY_SIZE,
    STATUS_ATTENDED,
    STATUS_CANCELLED,
    STATUS_CONFIRMED,
    STATUS_NO_SHOW,
    availability_of,
)

pytestmark = pytest.mark.anyio


def occurrence(**overrides):
    return SimpleNamespace(
        **{
            "status": "scheduled",
            "capacity": 100,
            "remaining": 100,
            "start_time": datetime.now(UTC) + timedelta(days=2),
            **overrides,
        }
    )


class TestAvailability:
    def test_plenty_of_room_reads_as_available(self):
        assert availability_of(occurrence(remaining=80)).status == "available"

    def test_nearly_full_reads_as_limited(self):
        """"3 left" and "300 left" should not look the same to somebody
        deciding whether to hurry."""
        assert availability_of(occurrence(capacity=100, remaining=6)).status == "limited"

    def test_no_places_left_reads_as_full(self):
        state = availability_of(occurrence(remaining=0))
        assert state.status == "full"
        assert not state.can_reserve

    def test_a_cancelled_date_is_not_reservable(self):
        state = availability_of(occurrence(status="cancelled"))
        assert state.status == "cancelled"
        assert not state.can_reserve

    def test_no_capacity_means_unlimited_not_zero(self):
        """Most things in a city have no fixed number of places. Reporting a
        count for them would be inventing a fact."""
        state = availability_of(occurrence(capacity=None, remaining=None))
        assert state.is_unlimited
        assert state.remaining is None
        assert state.can_reserve

    def test_a_small_venue_goes_limited_before_it_is_nearly_empty(self):
        """A twelve-seat supper club with three left is genuinely scarce, even
        though three is a small fraction of nothing."""
        assert availability_of(occurrence(capacity=12, remaining=3)).status == "limited"

    def test_a_large_venue_does_not_cry_scarcity_at_the_same_number(self):
        assert availability_of(occurrence(capacity=5000, remaining=400)).status == "available"

    def test_both_limited_and_available_allow_reserving(self):
        assert availability_of(occurrence(remaining=80)).can_reserve
        assert availability_of(occurrence(capacity=100, remaining=2)).can_reserve


class TestSeatAccounting:
    """The arithmetic that a live run showed getting wrong.

    Reserving a party of two once removed four places: the SQL UPDATE did the
    decrement and the code then subtracted from the loaded attribute as well,
    which the ORM had already refreshed. The statement is now the only thing
    that changes the number.
    """

    def test_only_holding_statuses_occupy_a_seat(self):
        assert STATUS_CONFIRMED in HOLDING_STATUSES
        assert STATUS_ATTENDED in HOLDING_STATUSES
        assert STATUS_CANCELLED not in HOLDING_STATUSES
        assert STATUS_NO_SHOW not in HOLDING_STATUSES

    def test_a_no_show_frees_the_seat_for_the_count(self):
        """Somebody who did not turn up did not occupy a place. Keeping the
        seat held would understate what the publisher could have sold."""
        assert STATUS_NO_SHOW not in HOLDING_STATUSES


class TestLimits:
    def test_a_party_ceiling_exists_and_is_small(self):
        """Forty places at a twelve-seat supper club is a mistake or a block,
        and both are better caught here than at the door."""
        assert 2 <= MAX_PARTY_SIZE <= 20

    def test_the_late_cancellation_window_is_hours_not_minutes(self):
        assert timedelta(minutes=30) <= CANCELLATION_CUTOFF <= timedelta(days=1)


class TestNoPaymentIsImplied:
    def test_a_reservation_carries_no_money(self):
        """Payments are COM-003 and not built. A field here would imply a
        transaction that never happens."""
        from app.domains.explorer.reservations import Reservation

        columns = set(Reservation.__table__.c.keys())
        assert not columns & {"amount", "price", "currency", "paid", "payment_id", "total"}

    def test_it_records_intent_and_nothing_more(self):
        from app.domains.explorer.reservations import Reservation

        columns = set(Reservation.__table__.c.keys())
        assert {"party_size", "status", "note"} <= columns


class TestOneReservationPerOccurrence:
    def test_the_constraint_is_on_the_occurrence_not_the_experience(self):
        """A weekly supper club is one Experience with many dates. A place at
        next Tuesday's is not a place at the one after."""
        from app.domains.explorer.reservations import Reservation

        constraint = next(
            c
            for c in Reservation.__table__.constraints
            if getattr(c, "name", "") == "uq_reservation_user_occurrence"
        )
        assert {c.name for c in constraint.columns} == {"user_id", "event_instance_id"}

    def test_a_reservation_outlives_its_listing(self):
        """No foreign key into catalog: withdrawing a listing must not erase
        somebody's record of having booked it."""
        from app.domains.explorer.reservations import Reservation

        assert not Reservation.__table__.c.event_instance_id.foreign_keys
        assert not Reservation.__table__.c.experience_id.foreign_keys

    def test_the_title_is_denormalised_so_it_still_renders(self):
        from app.domains.explorer.reservations import Reservation

        assert "experience_title" in Reservation.__table__.c
