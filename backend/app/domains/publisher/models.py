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
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
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

    # --- Business profile (see publisher/business.py) -----------------------
    #
    # What kind of business this is - hotel, museum, cafe. A label on the
    # publisher rather than a subclass or a parallel entity: everything that
    # actually differs between them is already modelled elsewhere. Location and
    # opening hours belong to `catalog.venues`, what they put on belongs to
    # `catalog.experiences`, and what they are suitable for belongs to the
    # suitability vocabulary. Null for a person, and for an organization that has
    # not said.
    business_type: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    # The wide image at the top of a business profile. `logo_url` is the small
    # square one and already existed; a cover is a different picture doing a
    # different job, not a bigger version of the same one.
    cover_url: Mapped[str | None] = mapped_column(Text, default=None)
    # Platform -> URL, over the fixed set in `business.SOCIAL_PLATFORMS`.
    social: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)

    # "unverified" is an honest resting state for a personal account. "pending"
    # would imply a review is queued, which is untrue unless verification was
    # actually requested.
    verification_status: Mapped[str] = mapped_column(
        String(32), default="unverified", nullable=False
    )
    # What the publisher offered as evidence when asking to be verified, and
    # when they asked. Real columns rather than a JSONB bag: a moderator reads
    # both on every decision, and they are the record of why the badge was given.
    verification_note: Mapped[str | None] = mapped_column(Text, default=None)
    verification_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    verification_decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    trust_level: Mapped[int] = mapped_column(Integer, default=TRUST_LEVEL_COMMUNITY, nullable=False)
    # Dynamic 0-1 score from accuracy, cancellation rate, metadata completeness and
    # satisfaction (spec BUSINESS-07). Influences ranking; never shown raw to users.
    quality_score: Mapped[float] = mapped_column(Numeric(4, 3), default=0.500, nullable=False)

    # Earned reputation (spec TRST-004). Separate from `trust_level`, which a
    # moderator sets once at verification and nothing changes afterwards: this
    # is the half the spec calls "continuously recalculated using historical
    # behavior". Recomputed on a timer, not per request.
    #
    # 0.5 is neutral rather than bad. A new publisher is unknown, and starting
    # everybody at zero would make a first listing impossible to get seen.
    reputation_score: Mapped[float] = mapped_column(
        Numeric(4, 3), default=0.500, nullable=False
    )
    # The signals that produced the score, so the dashboard and the moderation
    # console explain it from the same evidence rather than each deriving their
    # own account and disagreeing.
    reputation_signals: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    reputation_computed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("identity.users.id", ondelete="SET NULL"),
        default=None,
        index=True,
    )
    contact: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    venues: Mapped[list[Venue]] = relationship(back_populates="publisher")
    experiences: Mapped[list[Experience]] = relationship(back_populates="publisher")

    members: Mapped[list[PublisherMember]] = relationship(
        back_populates="publisher", cascade="all, delete-orphan"
    )
    gallery: Mapped[list[GalleryItem]] = relationship(
        back_populates="publisher",
        cascade="all, delete-orphan",
        order_by="GalleryItem.sort_order",
    )

    @property
    def is_verified(self) -> bool:
        return self.verification_status == "verified"

    @property
    def is_personal(self) -> bool:
        return self.type == TYPE_INDIVIDUAL


# A membership is offered, then accepted or declined. `removed` is kept rather
# than deleted so "who had access in March" is answerable - which is the first
# question asked after something is published that should not have been.
MEMBERSHIP_INVITED = "invited"
MEMBERSHIP_ACTIVE = "active"
MEMBERSHIP_DECLINED = "declined"
MEMBERSHIP_REMOVED = "removed"

# How long an unanswered invitation stays usable, in days. An invitation is a
# standing grant of access to somebody else's business; one that never expires
# is a key left under a mat for years.
INVITATION_TTL_DAYS = 14


class PublisherMember(Base, UUIDPrimaryKey, Timestamps):
    """Somebody other than the owner who may act for a business.

    The owner is **not** a row here. Ownership is `publishers.owner_user_id`,
    which every authorization check already keyed on before this table existed;
    duplicating it as a membership would create a second answer to "who owns
    this" that could disagree with the first.

    A membership is scoped to exactly one publisher, which is what makes
    cross-business access impossible to express rather than merely forbidden.
    """

    __tablename__ = "publisher_members"
    __table_args__ = (
        # One membership per person per business. Re-inviting somebody updates
        # the existing row rather than accumulating a history of grants that
        # each look current.
        UniqueConstraint("publisher_id", "user_id", name="uq_publisher_member"),
        Index("ix_publisher_members_user", "user_id", "status"),
        {"schema": SCHEMA},
    )

    publisher_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.publishers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Null until the invitation is accepted: somebody can be invited by email
    # before they have an account, and the row is what the signup then attaches
    # to. Not nullable-forever - an active membership always has a user.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("identity.users.id", ondelete="CASCADE"),
        default=None,
        index=True,
    )
    # Who it was sent to, lower-cased. Kept after acceptance as the record of
    # what was offered and to whom.
    invited_email: Mapped[str | None] = mapped_column(String(320), default=None, index=True)

    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=MEMBERSHIP_INVITED, server_default=MEMBERSHIP_INVITED, nullable=False
    )

    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("identity.users.id", ondelete="SET NULL"),
        default=None,
    )
    invited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # When the invitation stops being usable. Checked on accept rather than
    # swept by a job: an expired row that nobody has tried to use is harmless,
    # and a sweeper is another thing that can fail silently.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    publisher: Mapped[Publisher] = relationship(back_populates="members")

    @property
    def is_active(self) -> bool:
        return self.status == MEMBERSHIP_ACTIVE and self.user_id is not None


GALLERY_IMAGE = "image"
GALLERY_VIDEO = "video"

# How much a business may show on its own profile. A ceiling rather than a
# quota: the tab is a gallery somebody scrolls, not an archive, and the
# hundredth photograph of a lobby is not what anybody came for.
MAX_GALLERY_ITEMS = 60


class GalleryItem(Base, UUIDPrimaryKey, Timestamps):
    """A photograph or video a business put on its own profile.

    Distinct from `catalog.media`, which belongs to an *experience* and is
    editorial: it illustrates one thing that is on, and disappears when that
    listing does. This belongs to the business itself and outlives any listing -
    the rooms, the dining room, the view - which is exactly what somebody
    deciding whether to go wants and what a post history cannot show them.

    Merging the two was the obvious alternative and does not work: a `Media` row
    requires an experience id, so a business photo would need a hidden listing to
    hang off, and that listing would then have to be excluded by hand from
    search, ranking, the feed and every count. A second table is cheaper than a
    phantom row everything else has to remember to ignore.

    Nothing here is discoverable. A gallery is not indexed, not ranked and never
    reaches the concierge: it is what the business says about itself, and the
    platform's opinions are formed from listings and reviews.
    """

    __tablename__ = "gallery_items"
    __table_args__ = (
        Index("ix_gallery_items_publisher_order", "publisher_id", "sort_order"),
        {"schema": SCHEMA},
    )

    # No `index=True`: the composite above leads with this column, so a
    # single-column index would be a second copy of the same b-tree prefix,
    # written on every insert and read by nothing.
    publisher_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.publishers.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), default=GALLERY_IMAGE, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    # One field for both the alternative text of an image and the caption under a
    # video, because publishers write one sentence about a picture and asking for
    # two produces one of them filled in and the other left blank.
    caption: Mapped[str | None] = mapped_column(String(400), default=None)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Known for an image because Pillow decoded it, and null for a video because
    # nothing here decodes video. Null means "not known", never "square" - the
    # client reserves space from these and guessing 16:9 would make every
    # vertical phone video jump on load.
    width: Mapped[int | None] = mapped_column(Integer, default=None)
    height: Mapped[int | None] = mapped_column(Integer, default=None)
    # `video/mp4` or `video/webm`, from what the container sniff recognised.
    # Stored so the player can be told rather than left to sniff the URL.
    content_type: Mapped[str | None] = mapped_column(String(64), default=None)

    publisher: Mapped[Publisher] = relationship(back_populates="gallery")

    @property
    def is_video(self) -> bool:
        return self.kind == GALLERY_VIDEO
