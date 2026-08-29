"""A listing a business paid to have shown, and what that placement did.

BUSINESS-90.01 §7 and BUSINESS-06 both allow sponsored discovery, and both put a
condition on it that this schema is shaped around: *sponsored content should
never compromise recommendation integrity*. What follows is the smaller of the
two things that phrase could mean.

**A flat fee for a fixed run, not an auction.** BUSINESS-90.01 §5 puts "featured
listings" in the first phase and sponsored recommendations in the second, and
this is the first: a business pays once and its listing is eligible for one
labelled slot for a number of days. There is no bidding, no cost per click and
no budget being spent down, so there is nothing whose exhaustion could change
what an explorer is shown mid-session, and no incentive to serve an impression
to somebody it does not suit.

Impressions and clicks are still counted, because a publisher who paid deserves
to know what they got. They are **reporting, not pricing** - nothing about what
is shown depends on them, which is what stops the counter becoming a reason to
show something.

**Its own schema, not `commerce`.** Commerce holds what explorers pay for and
what publishers are owed; this is the platform selling its own inventory, which
is the other direction entirely. Putting a campaign in `commerce.orders` would
have meant a ticket order with no event and no seats.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.mixins import Timestamps, UUIDPrimaryKey

SCHEMA = "promotion"

# Bought, and waiting on the provider. Shows nothing: a promotion that ran while
# the payment page was open would be free advertising for anybody who opened it.
STATUS_PENDING = "pending_payment"
STATUS_ACTIVE = "active"
# The run finished. Kept rather than deleted - a publisher asking what their
# money bought needs the record, and a campaign that vanishes when it ends
# cannot be reported on.
STATUS_ENDED = "ended"
# Refused before it ran, with the payment refunded or never taken. A listing
# withdrawn or rejected by moderation between purchase and start lands here.
STATUS_REFUSED = "refused"

# How far a point-and-radius promotion reaches by default. A neighbourhood
# rather than a city: somebody buying a slot for a cafe in Bole is buying the
# people who might walk there.
DEFAULT_RADIUS_KM = 8.0

MIN_DAYS = 1
MAX_DAYS = 90


class Promotion(Base, UUIDPrimaryKey, Timestamps):
    """One listing, promoted in one place, for one stretch of days."""

    __tablename__ = "promotions"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ck_promotion_runs_forwards"),
        CheckConstraint("amount_minor >= 0", name="ck_promotion_amount_not_negative"),
        CheckConstraint(
            "radius_km IS NULL OR radius_km > 0", name="ck_promotion_radius_positive"
        ),
        UniqueConstraint("reference", name="uq_promotion_reference"),
        # Finding what is eligible right now, which is the query every search
        # runs. Partial, because an ended campaign is most of the table within a
        # season and none of them can ever match.
        Index(
            "ix_promotions_running",
            "starts_at",
            "ends_at",
            postgresql_where="status = 'active'",
        ),
        Index("ix_promotions_publisher", "publisher_id", "created_at"),
        {"schema": SCHEMA},
    )

    publisher_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # No foreign key into catalog, for the reason commerce has none: the record
    # of what was paid for must outlive the listing being withdrawn.
    experience_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    # Where it reaches. A city slug, or a point and a radius - the same two
    # shapes `Area` already understands, minus the country code: nobody buys a
    # slot in a country, and letting them would put one business in front of a
    # continent.
    city_slug: Mapped[str | None] = mapped_column(String(120), default=None, index=True)
    latitude: Mapped[float | None] = mapped_column(Float, default=None)
    longitude: Mapped[float | None] = mapped_column(Float, default=None)
    radius_km: Mapped[float | None] = mapped_column(Float, default=None)

    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[str] = mapped_column(String(20), default=STATUS_PENDING, nullable=False)

    amount_minor: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="ETB", nullable=False)

    reference: Mapped[str | None] = mapped_column(String(64), default=None)
    provider: Mapped[str | None] = mapped_column(String(32), default=None)
    provider_reference: Mapped[str | None] = mapped_column(String(120), default=None)
    checkout_url: Mapped[str | None] = mapped_column(String(1000), default=None)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Why it was refused, in words meant for the publisher.
    outcome_reason: Mapped[str | None] = mapped_column(String(300), default=None)


class PromotionDay(Base, UUIDPrimaryKey, Timestamps):
    """What one promotion did on one day.

    A daily rollup rather than a row per impression, for the reason
    `developer/usage.py` gives: a row per event is a firehose nobody reads, and
    the question is "how did this campaign do", which a counter answers exactly
    as well.

    **Nothing about serving depends on these numbers.** They are shown to the
    publisher and used for nothing else - a counter that fed back into placement
    would be a reason to show a listing to somebody it does not suit.
    """

    __tablename__ = "promotion_days"
    __table_args__ = (
        UniqueConstraint("promotion_id", "day", name="uq_promotion_day"),
        {"schema": SCHEMA},
    )

    promotion_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.promotions.id", ondelete="CASCADE"),
        nullable=False,
    )
    day: Mapped[Date] = mapped_column(Date, nullable=False)
    impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
