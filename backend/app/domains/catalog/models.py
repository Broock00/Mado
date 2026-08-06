"""Catalog domain models (spec 54.01 and 54.03).

The taxonomy in spec PRODUCT-01 is binding and slightly counter-intuitive, so it is
worth restating: **Experience is the primary object**. An Event is a *time-bound*
Experience, represented here as an ``EventInstance`` hanging off an Experience -
not as a sibling table. A concert Experience with three showings is one row in
``experiences`` and three in ``event_instances``.

City -> Neighborhood -> Venue -> Experience -> EventInstance is the content
hierarchy that both breadcrumbs and search facets are built from.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from geoalchemy2 import Geography
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Column,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.mixins import SoftDelete, Timestamps, UUIDPrimaryKey

if TYPE_CHECKING:
    from app.domains.publisher.models import Publisher

SCHEMA = "catalog"

# Experience lifecycle (spec 54.03 s5).
STATUS_DRAFT = "draft"
STATUS_REVIEW = "review"
STATUS_PUBLISHED = "published"
STATUS_ARCHIVED = "archived"

# Experience kinds (spec 54.03 s6).
TYPE_PLACE = "place"
TYPE_EVENT = "event"
TYPE_ACTIVITY = "activity"

# Embedding width for the Gemini text-embedding family; the vector column is sized
# once here so a model swap is a single deliberate migration rather than a surprise.
EMBEDDING_DIM = 768


experience_tags = Table(
    "experience_tags",
    Base.metadata,
    Column(
        "experience_id",
        PGUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.experiences.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "tag_id",
        PGUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.tags.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    schema=SCHEMA,
)


class City(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "cities"
    __table_args__ = {"schema": SCHEMA}

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    country: Mapped[str] = mapped_column(String(120), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    languages: Mapped[list[str]] = mapped_column(ARRAY(String(12)), default=list, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    # Whether the city has passed the spec BUSINESS-08 launch readiness checklist.
    is_live: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    neighborhoods: Mapped[list[Neighborhood]] = relationship(back_populates="city")
    venues: Mapped[list[Venue]] = relationship(back_populates="city")


class Neighborhood(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "neighborhoods"
    __table_args__ = (
        UniqueConstraint("city_id", "slug", name="uq_neighborhood_city_slug"),
        {"schema": SCHEMA},
    )

    city_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.cities.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)

    city: Mapped[City] = relationship(back_populates="neighborhoods")
    venues: Mapped[list[Venue]] = relationship(back_populates="neighborhood")


class Venue(Base, UUIDPrimaryKey, Timestamps, SoftDelete):
    __tablename__ = "venues"
    __table_args__ = (
        # GiST over the generated geography column powers "near me" radius search.
        Index("ix_venues_geo", "geo", postgresql_using="gist"),
        {"schema": SCHEMA},
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(220), unique=True, nullable=False, index=True)
    publisher_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("publisher.publishers.id", ondelete="SET NULL"),
        default=None,
        index=True,
    )
    city_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.cities.id", ondelete="RESTRICT"), index=True
    )
    neighborhood_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.neighborhoods.id", ondelete="SET NULL"),
        default=None,
        index=True,
    )

    address: Mapped[str] = mapped_column(Text, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    # Derived by Postgres from latitude/longitude, so the two can never drift
    # apart. Declaring it Computed also tells SQLAlchemy to omit the column from
    # INSERT and UPDATE - without that the ORM would try to write NULL into it and
    # Postgres would reject the row.
    #
    # spatial_index=False because GeoAlchemy2 would otherwise emit its own index
    # alongside the named one in __table_args__, leaving every venue with two
    # identical GiST indexes to maintain on write.
    geo: Mapped[object | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False),
        Computed("ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography", persisted=True),
        nullable=True,
    )

    capacity: Mapped[int | None] = mapped_column(Integer, default=None)
    facilities: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list, nullable=False)
    # Structured accessibility metadata - spec PRODUCT-00 principle 10 treats this
    # as a requirement, so it is a first-class column rather than a loose tag.
    accessibility: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    opening_hours: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    contact: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    city: Mapped[City] = relationship(back_populates="venues")
    neighborhood: Mapped[Neighborhood | None] = relationship(back_populates="venues")
    publisher: Mapped[Publisher | None] = relationship(back_populates="venues")
    experiences: Mapped[list[Experience]] = relationship(back_populates="venue")


class Category(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "categories"
    __table_args__ = {"schema": SCHEMA}

    name: Mapped[str] = mapped_column(String(80), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.categories.id", ondelete="SET NULL"),
        default=None,
    )
    icon: Mapped[str | None] = mapped_column(String(64), default=None)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    experiences: Mapped[list[Experience]] = relationship(back_populates="category")


class Tag(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "tags"
    __table_args__ = {"schema": SCHEMA}

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)

    experiences: Mapped[list[Experience]] = relationship(
        secondary=experience_tags, back_populates="tags"
    )


class Experience(Base, UUIDPrimaryKey, Timestamps, SoftDelete):
    __tablename__ = "experiences"
    __table_args__ = (
        Index("ix_experiences_city_status", "city_id", "status"),
        Index("ix_experiences_status_type", "status", "type"),
        {"schema": SCHEMA},
    )

    publisher_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("publisher.publishers.id", ondelete="CASCADE"),
        index=True,
    )
    venue_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.venues.id", ondelete="SET NULL"),
        default=None,
        index=True,
    )
    city_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.cities.id", ondelete="RESTRICT"), index=True
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.categories.id", ondelete="SET NULL"),
        default=None,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(240), nullable=False)
    slug: Mapped[str] = mapped_column(String(260), unique=True, nullable=False, index=True)
    summary: Mapped[str | None] = mapped_column(String(400), default=None)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    type: Mapped[str] = mapped_column(String(16), default=TYPE_PLACE, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_DRAFT, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    price_type: Mapped[str] = mapped_column(String(16), default="free", nullable=False)
    price_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), default=None)
    price_max: Mapped[float | None] = mapped_column(Numeric(12, 2), default=None)
    currency: Mapped[str] = mapped_column(String(3), default="ETB", nullable=False)

    duration_minutes: Mapped[int | None] = mapped_column(Integer, default=None)
    is_indoor: Mapped[bool | None] = mapped_column(Boolean, default=None)
    accessibility: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # --- Discovery signals (spec 54.03 s15) ---------------------------------
    # Owned by the platform, never by a publisher, and never accepted from input.
    popularity_score: Mapped[float] = mapped_column(Numeric(5, 4), default=0.0, nullable=False)
    quality_score: Mapped[float] = mapped_column(Numeric(5, 4), default=0.5, nullable=False)
    trend_score: Mapped[float] = mapped_column(Numeric(5, 4), default=0.0, nullable=False)
    rating_average: Mapped[float | None] = mapped_column(Numeric(3, 2), default=None)
    rating_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Nullable so an experience is usable before the embedding worker reaches it.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), default=None)

    publisher: Mapped[Publisher] = relationship(back_populates="experiences")
    venue: Mapped[Venue | None] = relationship(back_populates="experiences", lazy="selectin")
    city: Mapped[City] = relationship(lazy="selectin")
    category: Mapped[Category | None] = relationship(back_populates="experiences", lazy="selectin")
    tags: Mapped[list[Tag]] = relationship(
        secondary=experience_tags, back_populates="experiences", lazy="selectin"
    )
    media: Mapped[list[Media]] = relationship(
        back_populates="experience", cascade="all, delete-orphan", lazy="selectin"
    )
    events: Mapped[list[EventInstance]] = relationship(
        back_populates="experience", cascade="all, delete-orphan"
    )

    @property
    def is_published(self) -> bool:
        return self.status == STATUS_PUBLISHED and self.deleted_at is None


class EventInstance(Base, UUIDPrimaryKey, Timestamps):
    """A single scheduled occurrence of an Experience (spec 54.03 s8)."""

    __tablename__ = "event_instances"
    __table_args__ = (
        Index("ix_event_instances_start", "start_time"),
        Index("ix_event_instances_experience_start", "experience_id", "start_time"),
        {"schema": SCHEMA},
    )

    experience_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.experiences.id", ondelete="CASCADE"),
        index=True,
    )
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    status: Mapped[str] = mapped_column(String(16), default="scheduled", nullable=False)
    capacity: Mapped[int | None] = mapped_column(Integer, default=None)
    remaining: Mapped[int | None] = mapped_column(Integer, default=None)
    # Populated when a publisher reschedules, so the UI can show "moved from ...".
    rescheduled_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, default=None)

    experience: Mapped[Experience] = relationship(back_populates="events", lazy="selectin")


class Media(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "media"
    __table_args__ = {"schema": SCHEMA}

    experience_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.experiences.id", ondelete="CASCADE"),
        index=True,
    )
    type: Mapped[str] = mapped_column(String(16), default="image", nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    alt_text: Mapped[str | None] = mapped_column(String(400), default=None)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    experience: Mapped[Experience] = relationship(back_populates="media")
