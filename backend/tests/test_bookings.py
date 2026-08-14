"""What the publisher is told about their own date.

The rule worth testing here is the one that is tempting to get wrong: a pending
order is holding a seat and may still lapse. It has to count against the room
and must not count as money. Folding it into revenue reports income that does
not exist; leaving it out of the seat count offers places that are already held.

Tested against ordinary objects rather than a database, which is why the
aggregation is a pure function - the same split ticketing.summarise uses.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.domains.commerce.bookings import summarise

NOW = datetime(2026, 8, 15, 18, 0, tzinfo=UTC)


@dataclass
class FakeLine:
    ticket_type_id: uuid.UUID
    ticket_type_name: str
    quantity: int
    unit_price_minor: int


@dataclass
class FakeOrder:
    status: str
    quantity: int
    amount_minor: int
    user_id: uuid.UUID = field(default_factory=uuid.uuid4)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    reference: str = "mado-test"
    created_at: datetime = NOW
    paid_at: datetime | None = None
    lines: list[FakeLine] = field(default_factory=list)


@dataclass
class FakeTier:
    name: str
    price_minor: int
    sold: int = 0
    quantity: int | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    @property
    def remaining(self) -> int | None:
        return None if self.quantity is None else max(0, self.quantity - self.sold)


def report(orders=(), tiers=(), capacity=None, names=None):
    return summarise(
        list(orders), list(tiers), capacity=capacity, currency="ETB", names=names or {}
    )


class TestPendingMoneyIsNotMoney:
    def test_a_pending_order_is_not_revenue(self):
        got = report([FakeOrder(status="pending", quantity=2, amount_minor=60000)])
        assert got.revenue_minor == 0
        assert got.pending_minor == 60000

    def test_a_pending_order_still_holds_its_seats(self):
        """It has to, or the publisher is told there is room that is already
        spoken for and sells it twice."""
        got = report([FakeOrder(status="pending", quantity=2, amount_minor=60000)])
        assert got.seats_taken == 2

    def test_paid_and_pending_are_counted_separately(self):
        got = report(
            [
                FakeOrder(status="paid", quantity=1, amount_minor=30000),
                FakeOrder(status="pending", quantity=3, amount_minor=90000),
            ]
        )
        assert (got.revenue_minor, got.pending_minor) == (30000, 90000)
        assert (got.tickets_paid, got.tickets_pending) == (1, 3)
        assert got.seats_taken == 4


class TestOrdersThatWentNowhere:
    def test_a_failed_order_holds_nothing_and_is_worth_nothing(self):
        got = report(
            [
                FakeOrder(status="failed", quantity=2, amount_minor=60000),
                FakeOrder(status="expired", quantity=1, amount_minor=30000),
                FakeOrder(status="cancelled", quantity=1, amount_minor=30000),
            ]
        )
        assert got.seats_taken == 0
        assert got.revenue_minor == 0
        assert got.pending_minor == 0
        assert got.orders_failed == 3

    def test_a_failed_order_is_not_on_the_door_list(self):
        """Somebody whose payment failed is not coming, and printing their name
        at the door is how they get let in for nothing."""
        got = report([FakeOrder(status="failed", quantity=2, amount_minor=60000)])
        assert got.buyers == []


class TestPerTierRevenue:
    def test_revenue_follows_what_was_charged_not_the_current_price(self):
        """A publisher raising a price must not retrospectively increase what
        earlier buyers appear to have paid. The line carries the real number."""
        vip = FakeTier(name="VIP", price_minor=100_000, sold=1)
        got = report(
            orders=[
                FakeOrder(
                    status="paid",
                    quantity=1,
                    amount_minor=40000,
                    lines=[
                        FakeLine(
                            ticket_type_id=vip.id,
                            ticket_type_name="VIP",
                            quantity=1,
                            unit_price_minor=40000,
                        )
                    ],
                )
            ],
            tiers=[vip],
        )
        assert got.tiers[0].revenue_minor == 40000

    def test_a_pending_line_contributes_no_tier_revenue(self):
        vip = FakeTier(name="VIP", price_minor=40000, sold=1)
        got = report(
            orders=[
                FakeOrder(
                    status="pending",
                    quantity=1,
                    amount_minor=40000,
                    lines=[
                        FakeLine(
                            ticket_type_id=vip.id,
                            ticket_type_name="VIP",
                            quantity=1,
                            unit_price_minor=40000,
                        )
                    ],
                )
            ],
            tiers=[vip],
        )
        assert got.tiers[0].revenue_minor == 0


class TestTheDoorList:
    def test_a_buyer_is_named_and_carries_what_they_bought(self):
        who = uuid.uuid4()
        got = report(
            [
                FakeOrder(
                    status="paid",
                    quantity=3,
                    amount_minor=120000,
                    user_id=who,
                    lines=[
                        FakeLine(uuid.uuid4(), "VVIP", 1, 60000),
                        FakeLine(uuid.uuid4(), "General admission", 2, 30000),
                    ],
                )
            ],
            names={who: "Sara"},
        )
        assert [b.name for b in got.buyers] == ["Sara"]
        assert got.buyers[0].tiers == ["1 x VVIP", "2 x General admission"]

    def test_an_unknown_buyer_is_not_left_blank(self):
        got = report([FakeOrder(status="paid", quantity=1, amount_minor=30000)])
        assert got.buyers[0].name == "Someone"
