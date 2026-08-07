"""Explorer domain models (spec 54.01 s11-12).

Everything an Explorer accumulates: saved items, reviews, and the interaction
signals that feed personalization. Saved items are polymorphic over entity type so
collections and itineraries slot in later without a new table per kind.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.mixins import SoftDelete, Timestamps, UUIDPrimaryKey

if TYPE_CHECKING:
    from app.domains.identity.models import User

SCHEMA = "explorer"


class SavedItem(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "saved_items"
    __table_args__ = (
        UniqueConstraint("user_id", "entity_type", "entity_id", name="uq_saved_item"),
        Index("ix_saved_items_user", "user_id", "created_at"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("identity.users.id", ondelete="CASCADE"), index=True
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)  # experience|event|venue
    entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, default=None)


class Review(Base, UUIDPrimaryKey, Timestamps, SoftDelete):
    __tablename__ = "reviews"
    __table_args__ = (
        # One review per explorer per experience keeps rating averages honest.
        UniqueConstraint("user_id", "experience_id", name="uq_review_user_experience"),
        Index("ix_reviews_experience_status", "experience_id", "status"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("identity.users.id", ondelete="CASCADE"), index=True
    )
    experience_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("catalog.experiences.id", ondelete="CASCADE"),
        index=True,
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(String(16), default="approved", nullable=False)
    # True when attendance was confirmed - spec BUSINESS-07 weights these higher
    # in both the displayed average and fraud scoring.
    verified_attendance: Mapped[bool] = mapped_column(default=False, nullable=False)
    # Screening notes, when a review was withheld. Shown to its author so they
    # know why their words are not appearing, rather than leaving them to
    # conclude the platform simply lost them.
    moderation_notes: Mapped[str | None] = mapped_column(Text, default=None)

    # Eager by default: a review is never useful without knowing who wrote it,
    # and a list of twenty would otherwise be twenty extra queries.
    author: Mapped[User] = relationship(lazy="selectin")


class ContentReport(Base, UUIDPrimaryKey, Timestamps):
    """A community report against published content (spec BUSINESS-07).

    Community reporting is the counterweight to open publishing: once anyone can
    post, the people reading are the fastest detector of what should not be there.

    Reports are advisory, never automatic enforcement. Spec BUSINESS-07 reserves
    account suspension and content removal for human judgement, so a report raises
    a signal and can withhold content pending review - it cannot delete anything.
    """

    __tablename__ = "content_reports"
    __table_args__ = (
        # One report per person per item. Without this, a handful of determined
        # users could manufacture a takeover of the moderation queue.
        UniqueConstraint("reporter_user_id", "experience_id", name="uq_report_per_reporter"),
        Index("ix_reports_status_created", "status", "created_at"),
        {"schema": SCHEMA},
    )

    experience_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("catalog.experiences.id", ondelete="CASCADE"),
        index=True,
    )
    reporter_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("identity.users.id", ondelete="SET NULL"),
        default=None,
        index=True,
    )
    # spam | inaccurate | inappropriate | duplicate | scam | other
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    # open | reviewing | upheld | dismissed
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    resolution_note: Mapped[str | None] = mapped_column(Text, default=None)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("identity.users.id", ondelete="SET NULL"), default=None
    )


class InteractionEvent(Base, UUIDPrimaryKey):
    """Behavioural signal feeding personalization (spec 10.01.02 "Progressive Learning").

    Deliberately append-only and cheap to write: this table is on the hot path of
    every card impression, so it carries no foreign keys back into catalog and no
    updated_at. Aggregation happens downstream.
    """

    __tablename__ = "interaction_events"
    __table_args__ = (
        Index("ix_interaction_user_time", "user_id", "occurred_at"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), default=None, index=True
    )
    anonymous_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    # view | save | unsave | open_details | search | dismiss | not_interested | complete
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(32), default=None)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), default=None)
    weight: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    context: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class Itinerary(Base, UUIDPrimaryKey, Timestamps, SoftDelete):
    """A planned sequence of experiences (spec 54.01 s12, taxonomy: Collection ->
    Itinerary -> Journey).

    An Itinerary is a *plan*: ordered, timed and feasible. That is what separates
    it from a Collection, which is an unordered set with no claim about whether
    you could actually do it all. The stored plan keeps its own timings rather
    than recomputing them on read, because the arrival times were computed against
    the travel estimates and event schedules that existed when it was built - and
    a plan that silently reshuffles itself between views is not a plan.
    """

    __tablename__ = "itineraries"
    __table_args__ = (
        Index("ix_itineraries_user_created", "user_id", "created_at"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("identity.users.id", ondelete="CASCADE"),
        default=None,
        index=True,
    )
    # Anonymous explorers can plan too; the plan is keyed to their client id and
    # can be claimed later if they sign up.
    anonymous_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    city_slug: Mapped[str | None] = mapped_column(String(120), default=None)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Total estimated cost in minor-unit-free ETB, matching catalog price fields.
    estimated_cost: Mapped[float | None] = mapped_column(Numeric(10, 2), default=None)
    currency: Mapped[str] = mapped_column(String(3), default="ETB", nullable=False)
    total_travel_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # The request this plan answers, kept so it can be replanned when something
    # falls through without interrogating the explorer again.
    constraints: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # Why this plan looks the way it does, in the explorer's language. Spec
    # PRODUCT-00 principle 5 applies to plans as much as to recommendations.
    rationale: Mapped[str | None] = mapped_column(Text, default=None)

    stops: Mapped[list[ItineraryStop]] = relationship(
        back_populates="itinerary",
        cascade="all, delete-orphan",
        order_by="ItineraryStop.position",
        lazy="selectin",
    )


class ItineraryStop(Base, UUIDPrimaryKey, Timestamps):
    """One stop in a plan.

    Carries a snapshot of the timing rather than pointing at an event instance
    alone, because the arrival time is a property of *this plan* - the same event
    sits at a different hour in a different itinerary.
    """

    __tablename__ = "itinerary_stops"
    __table_args__ = (
        UniqueConstraint("itinerary_id", "position", name="uq_itinerary_stop_position"),
        {"schema": SCHEMA},
    )

    itinerary_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.itineraries.id", ondelete="CASCADE"),
        index=True,
    )
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # No foreign key into catalog: this schema does not read catalog tables
    # directly (modular monolith rule), and a plan should survive a listing being
    # withdrawn rather than cascade-deleting the explorer's evening.
    experience_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    event_instance_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), default=None
    )
    # Denormalised so a plan still renders if the listing later disappears.
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    arrive_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    depart_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Travel from the previous stop, zero for the first.
    travel_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    travel_km: Mapped[float | None] = mapped_column(Numeric(6, 2), default=None)
    estimated_cost: Mapped[float | None] = mapped_column(Numeric(10, 2), default=None)
    # True when the time is dictated by a scheduled event rather than chosen.
    is_fixed_time: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    note: Mapped[str | None] = mapped_column(String(300), default=None)

    itinerary: Mapped[Itinerary] = relationship(back_populates="stops")
