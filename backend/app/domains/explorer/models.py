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
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.mixins import SoftDelete, Timestamps, UUIDPrimaryKey

if TYPE_CHECKING:
    pass

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
