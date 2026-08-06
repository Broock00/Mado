"""Publisher domain models (spec 54.04).

A Publisher is a verified organization that owns Venues and publishes Experiences.
Trust level maps to spec BUSINESS-07's four tiers, and drives both ranking weight
and how much moderation scrutiny a submission receives.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.mixins import SoftDelete, Timestamps, UUIDPrimaryKey

if TYPE_CHECKING:
    from app.domains.catalog.models import Experience, Venue

SCHEMA = "publisher"

# Spec BUSINESS-07: community -> verified -> trusted organization -> strategic partner.
TRUST_LEVEL_COMMUNITY = 0
TRUST_LEVEL_VERIFIED = 1
TRUST_LEVEL_TRUSTED_ORG = 2
TRUST_LEVEL_STRATEGIC_PARTNER = 3


class Publisher(Base, UUIDPrimaryKey, Timestamps, SoftDelete):
    __tablename__ = "publishers"
    __table_args__ = {"schema": SCHEMA}

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), unique=True, nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(32), default="publisher", nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    industry: Mapped[str | None] = mapped_column(String(80), default=None)
    website: Mapped[str | None] = mapped_column(Text, default=None)
    logo_url: Mapped[str | None] = mapped_column(Text, default=None)

    verification_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    trust_level: Mapped[int] = mapped_column(Integer, default=TRUST_LEVEL_COMMUNITY, nullable=False)
    # Dynamic 0-1 score from accuracy, cancellation rate, metadata completeness and
    # satisfaction (spec BUSINESS-07). Influences ranking; never shown raw to users.
    quality_score: Mapped[float] = mapped_column(Numeric(4, 3), default=0.500, nullable=False)

    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("identity.users.id", ondelete="SET NULL"), default=None
    )
    contact: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    venues: Mapped[list[Venue]] = relationship(back_populates="publisher")
    experiences: Mapped[list[Experience]] = relationship(back_populates="publisher")

    @property
    def is_verified(self) -> bool:
        return self.verification_status == "verified"
