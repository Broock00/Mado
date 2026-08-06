"""Publisher domain models (spec 54.04, BUSINESS-07).

**A publisher is a person, not an abstraction.** Anyone with an account can post,
the same way anyone can post on a social platform. The first time an explorer
publishes, a *personal* publisher is created for them automatically from their
profile - they never fill in an organization form.

Organizations still exist, because a hotel or a museum genuinely is one and needs
several people posting under one identity. But they are the second case, not the
entry requirement.

Spec BUSINESS-07 already anticipated this: its Level 0 tier is the "Community
Publisher - basic account, suitable for small community events and informal
groups". What changes here is that Level 0 becomes the default path rather than
the exception, and verification is a badge earned on top rather than a gate
standing in front.

Accountability is preserved either way: spec BUSINESS-07 requires every published
experience to have an accountable owner, and a personal publisher is exactly that.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
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

# An individual posting under their own name, vs. an organization with members.
TYPE_INDIVIDUAL = "individual"
TYPE_ORGANIZATION = "organization"


class Publisher(Base, UUIDPrimaryKey, Timestamps, SoftDelete):
    __tablename__ = "publishers"
    __table_args__ = (
        # One personal publisher per explorer. Organizations are unconstrained
        # here because a person may own several.
        UniqueConstraint("owner_user_id", "type", name="uq_personal_publisher_per_user"),
        {"schema": SCHEMA},
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), unique=True, nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(32), default=TYPE_INDIVIDUAL, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    industry: Mapped[str | None] = mapped_column(String(80), default=None)
    website: Mapped[str | None] = mapped_column(Text, default=None)
    logo_url: Mapped[str | None] = mapped_column(Text, default=None)

    # "unverified" is an honest resting state for a personal account. "pending"
    # would imply a review is queued, which is untrue unless verification was
    # actually requested.
    verification_status: Mapped[str] = mapped_column(
        String(32), default="unverified", nullable=False
    )
    trust_level: Mapped[int] = mapped_column(Integer, default=TRUST_LEVEL_COMMUNITY, nullable=False)
    # Dynamic 0-1 score from accuracy, cancellation rate, metadata completeness and
    # satisfaction (spec BUSINESS-07). Influences ranking; never shown raw to users.
    quality_score: Mapped[float] = mapped_column(Numeric(4, 3), default=0.500, nullable=False)

    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("identity.users.id", ondelete="SET NULL"),
        default=None,
        index=True,
    )
    contact: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    venues: Mapped[list[Venue]] = relationship(back_populates="publisher")
    experiences: Mapped[list[Experience]] = relationship(back_populates="publisher")

    @property
    def is_verified(self) -> bool:
        return self.verification_status == "verified"

    @property
    def is_personal(self) -> bool:
        return self.type == TYPE_INDIVIDUAL
