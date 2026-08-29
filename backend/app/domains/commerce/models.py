"""What a ticket is, and what an order is (spec COM-002, COM-003).

Its own schema, because the integration architecture asks that payment
processing stay isolated from business logic and because money has different
rules from everything else in the platform: rows here are financial records
first. Nothing in this schema is ever deleted, amounts never change once
written, and a state only moves forwards.

**Inventory lives in two places, on purpose.** `EventInstance.remaining` is the
seat count COM-001 already defends with a single conditional UPDATE, and
ticketing does not get its own parallel counter to drift against it - an order
takes from the same column by the same mechanism. What is per-tier here is how
many of *that tier* exist, which is a different question from how many people
fit in the room.

**Money is integers in minor units.** Santim, not birr. `Numeric` would also be
exact, but it invites a float somewhere in the middle, and the place that
surfaces is somebody's total.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.mixins import Timestamps, UUIDPrimaryKey

SCHEMA = "commerce"

# Order states. Forwards only: a paid order is never returned to pending, and
# an expired one is never revived - a second attempt is a second order, which
# keeps every attempt in the record rather than overwriting the interesting one.
ORDER_PENDING = "pending"
ORDER_PAID = "paid"
ORDER_FAILED = "failed"
ORDER_EXPIRED = "expired"
ORDER_CANCELLED = "cancelled"
ORDER_TERMINAL = (ORDER_PAID, ORDER_FAILED, ORDER_EXPIRED, ORDER_CANCELLED)
# States that still hold seats out of the room.
ORDER_HOLDING = (ORDER_PENDING, ORDER_PAID)

TICKET_ISSUED = "issued"
TICKET_CHECKED_IN = "checked_in"
TICKET_VOID = "void"

MAX_PER_ORDER = 10


class TicketType(Base, UUIDPrimaryKey, Timestamps):
    """One tier on one date - "General admission", "VIP" (spec 55.05 §21).

    Attached to an occurrence rather than to the experience. A weekly night is
    a different room every week: last Friday sold out and next Friday has not,
    so a tier shared across dates would have to carry a quantity per date
    anyway, which is this table with an extra join.

    Free tiers are allowed and are not a special case. "Registration required"
    in the spec's price list is a zero-priced ticket, and modelling it as one
    means the door list, the capacity and the confirmation are the same code as
    a paid event rather than a parallel path that gets less attention.
    """

    __tablename__ = "ticket_types"
    __table_args__ = (
        CheckConstraint("price_minor >= 0", name="ck_ticket_type_price_not_negative"),
        CheckConstraint("quantity IS NULL OR quantity >= 0", name="ck_ticket_type_quantity"),
        Index("ix_ticket_types_occurrence", "event_instance_id", "position"),
        {"schema": SCHEMA},
    )

    # No foreign key into catalog for the same reason reservations has none: a
    # sold ticket must outlive the listing being withdrawn, and a cascade here
    # would delete the evidence that somebody paid.
    event_instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    experience_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(String(300), default=None)
    price_minor: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="ETB", nullable=False)
    # How many of this tier exist. NULL means "as many as the room holds", which
    # defers entirely to the occurrence's own capacity.
    quantity: Mapped[int | None] = mapped_column(Integer, default=None)
    sold: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Order shown to an explorer. Cheapest first is not always right - a
    # publisher may want the tier they are pushing at the top.
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Withdrawn rather than deleted, so an order that references it still reads.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sales_open_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    sales_close_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    @property
    def is_free(self) -> bool:
        return self.price_minor == 0

    @property
    def remaining(self) -> int | None:
        if self.quantity is None:
            return None
        return max(0, self.quantity - self.sold)


class Order(Base, UUIDPrimaryKey, Timestamps):
    """One explorer buying tickets for one date.

    The reference is ours and is generated before the provider is called, which
    is what makes the whole flow idempotent: starting the same order twice sends
    the same reference, and a provider that sees it treats the second as the
    same transaction rather than a second charge.

    `amount_minor` is written once from the tiers' prices at the moment of
    checkout and never recalculated. A publisher raising a price must not change
    what somebody already agreed to pay, and reading the price back from the
    tier at settlement time would do exactly that.
    """

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("reference", name="uq_order_reference"),
        CheckConstraint("amount_minor >= 0", name="ck_order_amount_not_negative"),
        # The fee is a deduction from what the buyer paid, never an addition to
        # it. A fee larger than the order would make the publisher's net
        # negative, which is not a discount - it is a bug that bills somebody.
        CheckConstraint(
            "platform_fee_minor >= 0 AND platform_fee_minor <= amount_minor",
            name="ck_order_fee_within_amount",
        ),
        Index("ix_orders_user_created", "user_id", "created_at"),
        Index("ix_orders_occurrence", "event_instance_id"),
        # Finding holds to expire. Partial, because pending is a small minority
        # of the table after a month and a full index would mostly be paid rows.
        Index(
            "ix_orders_pending_expiry",
            "expires_at",
            postgresql_where="status = 'pending'",
        ),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("identity.users.id", ondelete="RESTRICT"), index=True
    )
    event_instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    experience_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # Denormalised, like the reservation's: a receipt has to keep reading after
    # the listing is withdrawn.
    experience_title: Mapped[str] = mapped_column(String(300), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    reference: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=ORDER_PENDING, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="ETB", nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Who sold this. Denormalised deliberately: a payout must not depend on
    # reading the experience back, which can be withdrawn, retitled or moved to
    # a different publisher long before the money is sent on.
    publisher_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), default=None, index=True
    )
    # The commission, in basis points, copied at checkout for the same reason
    # `amount_minor` is: changing the platform's rate must not change what a
    # publisher was already owed on a sale made last month.
    fee_rate_bps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # What that rate came to on this order. Stored rather than recomputed, so a
    # rounding change can never restate a settled figure.
    platform_fee_minor: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    provider: Mapped[str | None] = mapped_column(String(32), default=None)
    provider_reference: Mapped[str | None] = mapped_column(String(120), default=None)
    checkout_url: Mapped[str | None] = mapped_column(Text, default=None)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Why it ended the way it did, in words meant for the explorer.
    outcome_reason: Mapped[str | None] = mapped_column(String(300), default=None)

    lines: Mapped[list[OrderLine]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )
    tickets: Mapped[list[Ticket]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def is_settled(self) -> bool:
        return self.status in ORDER_TERMINAL

    @property
    def is_free(self) -> bool:
        return self.amount_minor == 0

    @property
    def net_minor(self) -> int:
        """What the publisher is owed out of this order."""
        return self.amount_minor - self.platform_fee_minor


class OrderLine(Base, UUIDPrimaryKey, Timestamps):
    """How many of one tier, at the price agreed when the order was made."""

    __tablename__ = "order_lines"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_line_quantity_positive"),
        {"schema": SCHEMA},
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orders.id", ondelete="CASCADE"), index=True
    )
    ticket_type_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # Copied, so a receipt still reads after the tier is renamed or withdrawn.
    ticket_type_name: Mapped[str] = mapped_column(String(80), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)

    order: Mapped[Order] = relationship(back_populates="lines")

    @property
    def total_minor(self) -> int:
        return self.unit_price_minor * self.quantity


class Ticket(Base, UUIDPrimaryKey, Timestamps):
    """One admission. Issued only after money actually arrived.

    One row per person rather than a quantity on the order, because a ticket is
    checked in individually and a party of four turning up separately is
    ordinary. The code is what gets scanned at the door.
    """

    __tablename__ = "tickets"
    __table_args__ = (
        UniqueConstraint("code", name="uq_ticket_code"),
        Index("ix_tickets_occurrence", "event_instance_id", "status"),
        {"schema": SCHEMA},
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orders.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    event_instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    ticket_type_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    ticket_type_name: Mapped[str] = mapped_column(String(80), nullable=False)

    # Random, not sequential. A sequential code tells anybody holding one how
    # many were sold and lets them guess the next.
    code: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=TICKET_ISSUED, nullable=False)
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    order: Mapped[Order] = relationship(back_populates="tickets")


class PaymentEvent(Base, UUIDPrimaryKey, Timestamps):
    """Every settlement signal we acted on, kept so we act on it once.

    A provider will deliver the same webhook more than once - that is normal and
    documented behaviour, not a fault - and both the webhook and the explorer's
    return can arrive for the same payment. Without this table, whichever
    arrives second issues a second set of tickets.

    The unique constraint is the mechanism, not a `SELECT` first: two deliveries
    can race, and only the database can settle which one wins.
    """

    __tablename__ = "payment_events"
    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_payment_event_external"),
        {"schema": SCHEMA},
    )

    order_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orders.id", ondelete="SET NULL"), default=None
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # The provider's identifier for this signal. Where one is not supplied, the
    # source and reference stand in - see `checkout.settle`.
    external_id: Mapped[str] = mapped_column(String(160), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # webhook | return | sweep
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_minor: Mapped[int | None] = mapped_column(BigInteger, default=None)
    currency: Mapped[str | None] = mapped_column(String(3), default=None)
    # What the provider actually said, for the morning after.
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


PAYOUT_OWING = "owing"
PAYOUT_PAID = "paid"


class LedgerEntry(Base, UUIDPrimaryKey, Timestamps):
    """What one paid order came to: gross in, fee kept, net owed.

    Mado sells through its own merchant account - one set of Chapa and Stripe
    keys, no per-publisher connected accounts - so every santim of every ticket
    arrives here and the publisher's share is a debt, not a transfer that
    already happened. Before this table there was no record of that debt at all:
    the platform was holding the whole of every sale with nothing saying whose
    it was.

    One row per paid order, written when the money is confirmed and never
    afterwards. The three amounts are stored rather than derived because a later
    change to the rate, or to how it rounds, must not restate what a publisher
    was told they had earned.

    An unpaid order has no row. A hold that expired, an order that failed
    verification, and a free ticket all represent no money received, and a
    ledger that records intentions rather than receipts cannot be reconciled
    against a bank statement.
    """

    __tablename__ = "ledger_entries"
    __table_args__ = (
        # One order, one entry. The mechanism rather than a check first: settle
        # races itself between a webhook and a return, and only the database can
        # decide which one wrote.
        UniqueConstraint("order_id", name="uq_ledger_entry_order"),
        CheckConstraint(
            "gross_minor >= 0 AND fee_minor >= 0 AND net_minor >= 0",
            name="ck_ledger_amounts_not_negative",
        ),
        CheckConstraint("fee_minor + net_minor = gross_minor", name="ck_ledger_balances"),
        # Building a payout: everything owing for one publisher in one currency.
        Index(
            "ix_ledger_entries_unpaid",
            "publisher_id",
            "currency",
            postgresql_where="payout_id IS NULL",
        ),
        {"schema": SCHEMA},
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orders.id", ondelete="RESTRICT"), nullable=False
    )
    publisher_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    gross_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fee_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    net_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fee_rate_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    # When the money arrived, not when this row was written. A payout covers a
    # period, and the period a sale belongs to is the day it was paid for.
    earned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # NULL until the entry is swept into a payout. That is the whole of "is this
    # still owed", which keeps the question a single index lookup rather than a
    # sum over two tables.
    payout_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.payouts.id", ondelete="SET NULL"),
        default=None,
    )


class Payout(Base, UUIDPrimaryKey, Timestamps):
    """A batch of ledger entries settled to one publisher, in one currency.

    **A payout records a transfer; it does not make one.** Sending money needs a
    verified bank account per publisher and a provider disbursement API, neither
    of which exists yet, so the row is marked paid by whoever actually sent it
    and carries their reference. A status that moved itself to `paid` would be a
    stub inventing a result - the publisher would read "paid" and have no money.

    Currency is per payout rather than per publisher: a business selling in two
    cities earns in two currencies, and one row summing them would be a number
    that means nothing.
    """

    __tablename__ = "payouts"
    __table_args__ = (
        CheckConstraint("total_minor >= 0", name="ck_payout_total_not_negative"),
        Index("ix_payouts_publisher", "publisher_id", "created_at"),
        {"schema": SCHEMA},
    )

    publisher_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    entry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # The window the entries were drawn from, kept so a publisher can be told
    # what a figure covers rather than just what it is.
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[str] = mapped_column(String(16), default=PAYOUT_OWING, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # The bank or provider reference for the transfer somebody actually sent.
    reference: Mapped[str | None] = mapped_column(String(120), default=None)
    note: Mapped[str | None] = mapped_column(String(300), default=None)
