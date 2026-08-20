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
from app.domains.catalog.models import MODERATION_APPROVED, MODERATION_REJECTED

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


# --- Reposts ------------------------------------------------------------------
#
# Sharing somebody else's listing with your own audience.
#
# Likes and comments lived here too and were removed: Mado already has reviews,
# which are a rating and a written opinion, one per person per listing. A like
# is a weaker version of the rating and a comment is a weaker version of the
# opinion, and carrying both meant two places to say the same thing, two things
# to moderate, and a reader having to look in two places to learn what people
# thought.
#
# A repost is not a weaker review. It says "other people should see this", which
# a review does not say and cannot, so it stays.
#
# It hangs off an experience rather than off a "post". Mado has no separate post
# entity - an experience *is* what a publisher posted - and inventing one so the
# social layer had something familiar to attach to would duplicate the catalogue.


class Repost(Base, UUIDPrimaryKey, Timestamps):
    """Somebody putting a listing in front of their own audience.

    The optional note is a caption, not a verdict - "the coffee here is the
    reason to go". Anything longer or more considered belongs in a review, which
    carries a rating and counts towards the listing's average; this does not.
    """

    __tablename__ = "reposts"
    __table_args__ = (
        UniqueConstraint("user_id", "experience_id", name="uq_repost_user_experience"),
        Index("ix_reposts_experience", "experience_id"),
        Index("ix_reposts_user_created", "user_id", "created_at"),
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
    note: Mapped[str | None] = mapped_column(Text, default=None)

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


# ------------------------------------------------------------------ collections
#
# Taxonomy (spec PRODUCT-01): a Collection groups experiences around a theme -
# "best coffee shops", "rainy-day ideas". An Itinerary is an ordered sequence
# across time. The distinction is not pedantry: an itinerary claims you could
# actually do all of it in the order given, and a collection claims nothing of
# the sort. Conflating them would mean either promising feasibility a themed list
# cannot deliver, or dropping the timing an itinerary exists for.

VISIBILITY_PRIVATE = "private"
VISIBILITY_UNLISTED = "unlisted"
VISIBILITY_PUBLIC = "public"

# Where the collection came from. Only "user" is written today; the others are
# in the taxonomy and get a value here so adding them later is a seed, not a
# migration.
SOURCE_USER = "user"
SOURCE_AI = "ai"
SOURCE_EDITORIAL = "editorial"
SOURCE_PUBLISHER = "publisher"


class Collection(Base, UUIDPrimaryKey, Timestamps, SoftDelete):
    """A themed set of experiences.

    Three visibilities, and each earns its place:

    * **private** - nobody but the owner.
    * **unlisted** - anyone holding the link. This is what "share" usually means:
      sending a friend your coffee list. Requiring a moderator to approve that
      before you could text it to one person would be absurd.
    * **public** - listed, and screened on the way there, because at that point
      it is a page the platform is putting in front of strangers.

    Soft-deleted rather than removed. A shared link that turns into a 404 the
    moment the owner tidies up is worse than one that says the collection is
    gone, and an owner who deletes by accident should be recoverable.
    """

    __tablename__ = "collections"
    __table_args__ = (
        Index("ix_collections_user", "user_id", "created_at"),
        # Public browsing reads this pair; private ones must never appear.
        Index("ix_collections_visibility", "visibility", "moderation_status"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("identity.users.id", ondelete="CASCADE"),
        default=None,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # Unique and stable, so a shared link keeps working after a rename. Derived
    # from the title once, at creation, and never regenerated for that reason.
    slug: Mapped[str] = mapped_column(String(240), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    city_slug: Mapped[str | None] = mapped_column(String(120), default=None, index=True)
    visibility: Mapped[str] = mapped_column(
        String(16), default=VISIBILITY_PRIVATE, server_default=VISIBILITY_PRIVATE, nullable=False
    )
    source: Mapped[str] = mapped_column(
        String(16), default=SOURCE_USER, server_default=SOURCE_USER, nullable=False
    )
    # Screening state, mirroring how a published listing is handled. A private
    # collection is never screened - reading someone's private notes to check
    # them for policy violations is not moderation, it is surveillance.
    moderation_status: Mapped[str] = mapped_column(
        String(16), default="approved", server_default="approved", nullable=False
    )
    moderation_notes: Mapped[str | None] = mapped_column(Text, default=None)
    report_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    items: Mapped[list[CollectionItem]] = relationship(
        back_populates="collection",
        cascade="all, delete-orphan",
        order_by="CollectionItem.position",
        lazy="selectin",
    )

    @property
    def is_shareable(self) -> bool:
        """Whether someone holding the link should be shown it."""
        return (
            self.deleted_at is None
            and self.visibility in {VISIBILITY_UNLISTED, VISIBILITY_PUBLIC}
            and self.moderation_status != MODERATION_REJECTED
        )

    @property
    def is_discoverable(self) -> bool:
        """Whether it belongs in a public listing.

        Stricter than shareable: a collection awaiting review is reachable by
        anyone the owner already sent the link to, but is not put in front of
        people who did not ask for it.
        """
        return (
            self.deleted_at is None
            and self.visibility == VISIBILITY_PUBLIC
            and self.moderation_status == MODERATION_APPROVED
        )


class CollectionItem(Base, UUIDPrimaryKey, Timestamps):
    """One experience in a collection.

    `position` is display order and nothing more. It carries no claim about time,
    sequence or feasibility - that is what an Itinerary is for. Naming it
    `position` rather than `order` is deliberate for the same reason.
    """

    __tablename__ = "collection_items"
    __table_args__ = (
        # The same experience twice in one themed list is always a mistake.
        UniqueConstraint("collection_id", "experience_id", name="uq_collection_item"),
        Index("ix_collection_items_collection", "collection_id", "position"),
        {"schema": SCHEMA},
    )

    collection_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.collections.id", ondelete="CASCADE"),
        nullable=False,
    )
    experience_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("catalog.experiences.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Why this one is in the list, in the curator's words. The single most useful
    # thing a shared collection carries and the reason it beats a bare list.
    note: Mapped[str | None] = mapped_column(Text, default=None)

    collection: Mapped[Collection] = relationship(back_populates="items")
