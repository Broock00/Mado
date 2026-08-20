"""Publishing service.

Anyone with an account can post. The first time an explorer publishes, a personal
publisher is created for them from their profile - no organization form, no
approval step, no waiting.

Three rules hold throughout:

* **Ownership is checked on every mutation.** Being signed in is not authority
  over someone else's post.
* **Platform-owned fields are never accepted from input.** Popularity, quality,
  trend, ratings and moderation state are computed by the platform (spec 54.03
  s15); a publisher who could set their own ranking score would have found the
  cheapest possible growth hack.
* **Publishing is reversible.** Unpublish and archive exist so an author is never
  stuck with something they regret.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.domains.catalog import cities as catalog_cities
from app.domains.catalog import suitability as suitability_vocab
from app.domains.catalog.models import (
    MODERATION_APPROVED,
    MODERATION_REJECTED,
    STATUS_ARCHIVED,
    STATUS_DRAFT,
    STATUS_PUBLISHED,
    TYPE_EVENT,
    Category,
    City,
    EventInstance,
    Experience,
    Media,
    Tag,
    Venue,
)
from app.domains.identity.models import (
    ACCOUNT_BUSINESS,
    ACCOUNT_INDIVIDUAL,
    User,
    UserProfile,
)
from app.domains.publisher import business as business_vocab
from app.domains.publisher import permissions
from app.domains.publisher.models import (
    INVITATION_TTL_DAYS,
    MEMBERSHIP_ACTIVE,
    MEMBERSHIP_DECLINED,
    MEMBERSHIP_INVITED,
    MEMBERSHIP_REMOVED,
    TRUST_LEVEL_COMMUNITY,
    TYPE_INDIVIDUAL,
    TYPE_ORGANIZATION,
    Publisher,
    PublisherMember,
)

logger = get_logger("mado.publishing")

MAX_MEDIA_PER_EXPERIENCE = 10
MAX_EVENTS_PER_EXPERIENCE = 60


def slugify(value: str, *, max_length: int = 60) -> str:
    """URL-safe slug from arbitrary user text.

    Amharic and other non-Latin titles normalise away to nothing, so the caller
    must be prepared for an empty result and fall back to an opaque id rather than
    producing a slug like "--".
    """
    normalised = unicodedata.normalize("NFKD", value)
    ascii_only = normalised.encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    return slug[:max_length].strip("-")


class PublishingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------- publishers

    async def personal_publisher(self, user: User) -> Publisher:
        """Return the explorer's personal publisher, creating it on first use.

        This is what makes publishing feel like posting: the identity is derived
        from the profile the explorer already has.
        """
        result = await self.session.execute(
            select(Publisher).where(
                Publisher.owner_user_id == user.id,
                Publisher.type == TYPE_INDIVIDUAL,
                Publisher.deleted_at.is_(None),
            )
        )
        publisher = result.scalar_one_or_none()
        if publisher is not None:
            return publisher

        display_name = user.profile.display_name if user.profile else "Explorer"
        publisher = Publisher(
            name=display_name,
            slug=await self._unique_publisher_slug(display_name, user.id),
            type=TYPE_INDIVIDUAL,
            verification_status="unverified",
            trust_level=TRUST_LEVEL_COMMUNITY,
            owner_user_id=user.id,
            logo_url=user.profile.avatar_url if user.profile else None,
        )
        self.session.add(publisher)
        await self.session.flush()
        logger.info("personal_publisher_created", user_id=str(user.id))
        return publisher

    async def _unique_publisher_slug(self, name: str, user_id: uuid.UUID) -> str:
        base = slugify(name) or f"explorer-{user_id.hex[:8]}"
        candidate = base
        for suffix in range(0, 50):
            if suffix:
                candidate = f"{base}-{suffix}"
            exists = await self.session.scalar(
                select(func.count()).select_from(Publisher).where(Publisher.slug == candidate)
            )
            if not exists:
                return candidate
        # Collision-proof fallback rather than looping forever on a popular name.
        return f"{base}-{uuid.uuid4().hex[:6]}"

    # ------------------------------------------------------------- businesses

    async def business_of(self, user: User) -> Publisher | None:
        """The business this account *is*, or None for an individual.

        One per account, and the database says so: the unique constraint on
        `(owner_user_id, type)` already made a second organization impossible.
        """
        result = await self.session.execute(
            select(Publisher).where(
                Publisher.owner_user_id == user.id,
                Publisher.type == TYPE_ORGANIZATION,
                Publisher.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def publisher_for(self, user: User) -> Publisher:
        """The identity this account publishes under.

        The single place that answers "whose name goes on this post". A business
        account posts as the business; an individual posts as themselves. There
        is no choice to make at publishing time, because the account already is
        one thing or the other - which is why the composer has no "posting as"
        control and the API takes no publisher id from the client.
        """
        if user.is_business:
            business = await self.business_of(user)
            if business is not None:
                return business
            # A business account with no business row should not exist -
            # `become_business` creates both in one transaction. If it somehow
            # does, falling through to a personal publisher would quietly
            # publish under a person's name, so this refuses instead.
            raise ConflictError(
                "This business account has no business profile yet.",
                code="BUSINESS_PROFILE_MISSING",
            )
        return await self.personal_publisher(user)

    async def publishing_identities(self, user: User) -> list[tuple[Publisher, bool]]:
        """Everything this account may post as, and which one is the default.

        Usually one entry, and then the composer shows no choice at all: an
        individual posts as themselves, a business posts as itself.

        A second entry appears only for somebody who was **invited to another
        business** - the person who runs the front desk at a hotel and also has
        their own account. They genuinely do have two identities to choose
        between, and guessing on their behalf would put a hotel's programme out
        under their own name or vice versa.
        """
        default = await self.publisher_for(user)
        identities: list[tuple[Publisher, bool]] = [(default, True)]

        for membership in await self.memberships_of(user):
            publisher = membership.publisher
            if publisher.id == default.id:
                continue
            # Only where the role can actually create something. Offering an
            # analyst a business they cannot post for is offering them a button
            # that returns 403.
            if permissions.CONTENT_CREATE in permissions.granted_to(membership.role):
                identities.append((publisher, False))

        return identities

    async def become_business(
        self,
        user: User,
        *,
        name: str,
        business_type: str | None = None,
        description: str | None = None,
        website: str | None = None,
        contact: dict | None = None,
        social: dict | None = None,
        logo_url: str | None = None,
        cover_url: str | None = None,
    ) -> Publisher:
        """Turn this account into a business account. One way, once.

        **The account becomes the business.** It does not gain a business
        alongside a personal profile - there is one identity, and from here it
        is the business: the profile page, the name on every post, and what
        other explorers see.

        One way on purpose. Turning back would leave published listings, reviews
        of them and any tickets sold attributed to a business that no longer
        exists, and the honest way to undo it is a conversation with support
        rather than a button that quietly orphans other people's purchases.

        The personal publisher is *not* deleted if one already exists. Anything
        they posted as themselves before converting stays theirs and stays
        published; rewriting history to claim the business wrote it would be a
        lie about who was accountable at the time. New posts go to the business.

        No verification is granted here. A new business is `unverified`, which is
        honest - nobody has checked - and the badge is requested separately
        through the flow that already exists for it.
        """
        cleaned_name = name.strip()
        if not cleaned_name:
            raise ValidationError("A business needs a name.", code="BUSINESS_NAME_REQUIRED")

        if user.is_business:
            raise ConflictError(
                "This account is already a business.", code="ALREADY_A_BUSINESS"
            )

        existing = await self.business_of(user)
        if existing is not None:
            raise ConflictError(
                "This account already has a business profile.",
                code="BUSINESS_ALREADY_EXISTS",
            )

        publisher = Publisher(
            name=cleaned_name,
            slug=await self._unique_publisher_slug(cleaned_name, user.id),
            type=TYPE_ORGANIZATION,
            business_type=business_vocab.normalise_type(business_type),
            description=(description or "").strip() or None,
            website=(website or "").strip() or None,
            contact=contact or {},
            social=business_vocab.normalise_social(social),
            logo_url=(logo_url or "").strip() or None,
            cover_url=(cover_url or "").strip() or None,
            # Unverified, not pending: "pending" implies a review is queued, and
            # nobody has asked for one yet.
            verification_status="unverified",
            trust_level=TRUST_LEVEL_COMMUNITY,
            owner_user_id=user.id,
        )
        self.session.add(publisher)

        # The account and its business change together or not at all. A flush
        # that created the publisher and left `account_type` as individual would
        # leave somebody owning a business their account does not know about,
        # and `publisher_for` would keep publishing under their own name.
        user.account_type = ACCOUNT_BUSINESS
        user.account_type_chosen_at = datetime.now(UTC)

        await self.session.flush()
        logger.info(
            "account_became_business",
            publisher_id=str(publisher.id),
            owner_user_id=str(user.id),
            business_type=publisher.business_type,
        )
        return publisher

    async def choose_individual(self, user: User) -> User:
        """Record that this account is a person, and stop asking.

        Nothing else happens - an individual account is what every account
        already was. This exists so "they answered individual" and "they have
        not answered" stay distinguishable, which is the difference between
        prompting once and prompting forever.
        """
        if user.is_business:
            raise ConflictError(
                "This account is already a business.", code="ALREADY_A_BUSINESS"
            )
        user.account_type = ACCOUNT_INDIVIDUAL
        user.account_type_chosen_at = datetime.now(UTC)
        await self.session.flush()
        return user

    async def update_business(
        self, user: User, publisher_id: uuid.UUID, changes: dict
    ) -> Publisher:
        """Edit a business the caller is entitled to manage."""
        publisher = await self.assert_can_manage(
            user, publisher_id, permission=permissions.PROFILE_EDIT
        )

        simple = {"description", "industry", "website", "logo_url", "cover_url"}
        for field, value in changes.items():
            if field in simple and value is not None:
                setattr(publisher, field, (value or "").strip() or None)

        if changes.get("name"):
            publisher.name = changes["name"].strip()
        if "business_type" in changes:
            publisher.business_type = business_vocab.normalise_type(changes["business_type"])
        if changes.get("contact") is not None:
            publisher.contact = changes["contact"] or {}
        if changes.get("social") is not None:
            # Re-cleaned rather than trusted: the schema validated it, and the
            # seeder and importer reach this method without passing through one.
            publisher.social = business_vocab.normalise_social(changes["social"])

        await self.session.flush()
        logger.info("business_updated", publisher_id=str(publisher.id), by=str(user.id))
        return publisher

    async def membership_of(
        self, user: User, publisher_id: uuid.UUID
    ) -> PublisherMember | None:
        """The caller's active membership of a business, if any.

        Only `active` counts. An invitation nobody has accepted, a declined one
        and a removed one all grant nothing, and returning any of them here would
        make every caller responsible for remembering that.
        """
        result = await self.session.execute(
            select(PublisherMember).where(
                PublisherMember.publisher_id == publisher_id,
                PublisherMember.user_id == user.id,
                PublisherMember.status == MEMBERSHIP_ACTIVE,
            )
        )
        return result.scalar_one_or_none()

    async def permissions_for(self, user: User, publisher_id: uuid.UUID) -> frozenset[str]:
        """Everything this account may do to this business.

        Empty when they may do nothing, which is the common case and is not an
        error - a caller asking "what can I do here" for somewhere they have no
        relationship with deserves an answer rather than an exception.
        """
        publisher = await self.session.get(Publisher, publisher_id)
        if publisher is None or publisher.deleted_at is not None:
            return frozenset()
        if publisher.owner_user_id == user.id:
            return permissions.granted_to(permissions.ROLE_OWNER)
        membership = await self.membership_of(user, publisher_id)
        if membership is None:
            return frozenset()
        return permissions.granted_to(membership.role)

    async def assert_can_manage(
        self, user: User, publisher_id: uuid.UUID, *, permission: str
    ) -> Publisher:
        """Resolve a business the caller may perform `permission` on.

        The one gate. Every business action goes through it rather than each
        route deciding for itself, because the failure mode of a scattered check
        is not a visible bug - it is one endpoint that forgot, which nobody finds
        until somebody uses it.

        A caller with no relationship to the business gets 404, not 403.
        Confirming that a business exists to somebody who cannot see it is a
        disclosure, and the same reasoning already governs `_load_owned`.
        """
        publisher = await self.session.get(Publisher, publisher_id)
        if publisher is None or publisher.deleted_at is not None:
            raise NotFoundError("Business not found.", code="PUBLISHER_NOT_FOUND")

        held = await self.permissions_for(user, publisher_id)
        if not held:
            raise NotFoundError("Business not found.", code="PUBLISHER_NOT_FOUND")
        if permission not in held:
            # They can see it, so naming the refusal is not a disclosure and is
            # far more useful than a 404 they cannot act on.
            raise PermissionDeniedError(
                "Your role does not allow this: "
                + permissions.PERMISSIONS.get(permission, permission),
                code="INSUFFICIENT_BUSINESS_PERMISSION",
                details={"permission": permission},
            )
        return publisher

    # ------------------------------------------------------------------- team

    async def list_members(self, user: User, publisher_id: uuid.UUID) -> list[PublisherMember]:
        """Everyone with access or a pending invitation, for the team screen."""
        await self.assert_can_manage(user, publisher_id, permission=permissions.TEAM_MANAGE)
        result = await self.session.execute(
            select(PublisherMember)
            .where(
                PublisherMember.publisher_id == publisher_id,
                # Removed rows are kept for the audit trail and are not the team.
                PublisherMember.status.in_([MEMBERSHIP_INVITED, MEMBERSHIP_ACTIVE]),
            )
            .order_by(PublisherMember.created_at)
        )
        return list(result.scalars().all())

    async def invite_member(
        self, user: User, publisher_id: uuid.UUID, *, email: str, role: str
    ) -> PublisherMember:
        """Invite somebody to help manage a business.

        Invitations are addressed to an email rather than to a user id, so a
        business can invite the person who runs their front desk before that
        person has ever opened Mado. The row waits with no `user_id` until
        somebody with that address accepts.
        """
        publisher = await self.assert_can_manage(
            user, publisher_id, permission=permissions.TEAM_MANAGE
        )

        if not permissions.is_assignable(role):
            raise ValidationError(
                f"'{role}' is not a role you can assign.",
                code="INVALID_ROLE",
                details={"validRoles": list(permissions.ASSIGNABLE_ROLES)},
            )

        address = (email or "").strip().lower()
        if not address or "@" not in address:
            raise ValidationError("That is not an email address.", code="INVALID_EMAIL")

        # Inviting the owner is a no-op that would create a membership claiming
        # to grant what they already hold by owning the place.
        owner_email = await self._email_of(publisher.owner_user_id)
        if owner_email and owner_email == address:
            raise ValidationError(
                "They already own this business.", code="ALREADY_OWNER"
            )

        invitee = await self._user_by_email(address)
        existing = await self._membership_for(publisher_id, address, invitee)

        now = datetime.now(UTC)
        expires = now + timedelta(days=INVITATION_TTL_DAYS)

        if existing is not None:
            if existing.status == MEMBERSHIP_ACTIVE:
                raise ConflictError(
                    "They are already on this team.", code="ALREADY_A_MEMBER"
                )
            # Re-inviting somebody who declined, was removed, or let an
            # invitation lapse refreshes the same row. A second row would look
            # like two grants, and revoking one would leave the other standing.
            existing.role = role
            existing.status = MEMBERSHIP_INVITED
            existing.invited_by_user_id = user.id
            existing.invited_at = now
            existing.expires_at = expires
            existing.responded_at = None
            existing.removed_at = None
            member = existing
        else:
            member = PublisherMember(
                publisher_id=publisher_id,
                user_id=invitee.id if invitee else None,
                invited_email=address,
                role=role,
                status=MEMBERSHIP_INVITED,
                invited_by_user_id=user.id,
                invited_at=now,
                expires_at=expires,
            )
            self.session.add(member)

        await self.session.flush()
        logger.info(
            "business_member_invited",
            publisher_id=str(publisher_id),
            role=role,
            by=str(user.id),
            known_account=invitee is not None,
        )
        return member

    async def accept_invitation(self, user: User, member_id: uuid.UUID) -> PublisherMember:
        """Accept an invitation addressed to this account.

        The check is against the invited address, not against a token in a URL:
        the invitation grants access to somebody's business, and a link that
        anybody who receives it can redeem is a link that gets forwarded.
        """
        member = await self.session.get(PublisherMember, member_id)
        if member is None or member.status != MEMBERSHIP_INVITED:
            raise NotFoundError("Invitation not found.", code="INVITATION_NOT_FOUND")

        address = (user.profile.email or "").strip().lower() if user.profile else ""
        addressed_to_them = (member.user_id == user.id) or (
            bool(address) and member.invited_email == address
        )
        if not addressed_to_them:
            raise NotFoundError("Invitation not found.", code="INVITATION_NOT_FOUND")

        now = datetime.now(UTC)
        if member.expires_at is not None and member.expires_at < now:
            raise ConflictError(
                "That invitation has expired. Ask them to send a new one.",
                code="INVITATION_EXPIRED",
            )

        member.user_id = user.id
        member.status = MEMBERSHIP_ACTIVE
        member.responded_at = now
        await self.session.flush()
        logger.info(
            "business_member_accepted",
            publisher_id=str(member.publisher_id),
            user_id=str(user.id),
            role=member.role,
        )
        return member

    async def decline_invitation(self, user: User, member_id: uuid.UUID) -> None:
        member = await self.session.get(PublisherMember, member_id)
        if member is None or member.status != MEMBERSHIP_INVITED:
            raise NotFoundError("Invitation not found.", code="INVITATION_NOT_FOUND")

        address = (user.profile.email or "").strip().lower() if user.profile else ""
        if member.user_id != user.id and not (address and member.invited_email == address):
            raise NotFoundError("Invitation not found.", code="INVITATION_NOT_FOUND")

        member.status = MEMBERSHIP_DECLINED
        member.responded_at = datetime.now(UTC)
        await self.session.flush()

    async def change_member_role(
        self, user: User, publisher_id: uuid.UUID, member_id: uuid.UUID, *, role: str
    ) -> PublisherMember:
        await self.assert_can_manage(user, publisher_id, permission=permissions.TEAM_MANAGE)

        if not permissions.is_assignable(role):
            raise ValidationError(
                f"'{role}' is not a role you can assign.",
                code="INVALID_ROLE",
                details={"validRoles": list(permissions.ASSIGNABLE_ROLES)},
            )

        member = await self._member_of(publisher_id, member_id)
        # Nobody may promote themselves. An administrator holds `team:manage`,
        # so without this they could grant themselves any role the table allows -
        # which is the whole point of having roles.
        if member.user_id == user.id:
            raise PermissionDeniedError(
                "You cannot change your own role.", code="CANNOT_CHANGE_OWN_ROLE"
            )

        member.role = role
        await self.session.flush()
        logger.info(
            "business_member_role_changed",
            publisher_id=str(publisher_id),
            member_id=str(member_id),
            role=role,
            by=str(user.id),
        )
        return member

    async def remove_member(
        self, user: User, publisher_id: uuid.UUID, member_id: uuid.UUID
    ) -> None:
        """Remove somebody, or revoke an invitation they never answered."""
        await self.assert_can_manage(user, publisher_id, permission=permissions.TEAM_MANAGE)
        member = await self._member_of(publisher_id, member_id)

        member.status = MEMBERSHIP_REMOVED
        member.removed_at = datetime.now(UTC)
        await self.session.flush()
        logger.info(
            "business_member_removed",
            publisher_id=str(publisher_id),
            member_id=str(member_id),
            by=str(user.id),
        )

    async def pending_invitations(self, user: User) -> list[PublisherMember]:
        """Invitations waiting for this account to answer."""
        address = (user.profile.email or "").strip().lower() if user.profile else ""
        conditions = [PublisherMember.user_id == user.id]
        if address:
            conditions.append(PublisherMember.invited_email == address)

        result = await self.session.execute(
            select(PublisherMember)
            .where(
                PublisherMember.status == MEMBERSHIP_INVITED,
                or_(*conditions),
            )
            .options(selectinload(PublisherMember.publisher))
            .order_by(PublisherMember.invited_at.desc())
        )
        now = datetime.now(UTC)
        # Expired ones are filtered rather than shown greyed out: an invitation
        # you cannot accept is not something to offer somebody.
        return [
            member
            for member in result.scalars().all()
            if member.expires_at is None or member.expires_at >= now
        ]

    async def memberships_of(self, user: User) -> list[PublisherMember]:
        """Businesses this account helps manage, not counting ones they own."""
        result = await self.session.execute(
            select(PublisherMember)
            .where(
                PublisherMember.user_id == user.id,
                PublisherMember.status == MEMBERSHIP_ACTIVE,
            )
            .options(selectinload(PublisherMember.publisher))
            .order_by(PublisherMember.created_at.desc())
        )
        return [m for m in result.scalars().all() if m.publisher.deleted_at is None]

    async def display_names_for(
        self, user_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        """Names for a set of accounts, in one query.

        The team screen shows a name per member; fetching them one at a time is
        the N+1 the loader options elsewhere in this file exist to avoid.
        """
        wanted = [uid for uid in user_ids if uid is not None]
        if not wanted:
            return {}
        result = await self.session.execute(
            select(UserProfile.user_id, UserProfile.display_name).where(
                UserProfile.user_id.in_(wanted)
            )
        )
        return {row.user_id: row.display_name for row in result}

    async def business_by_slug(self, slug: str) -> Publisher | None:
        """A public business profile by slug, or None.

        Organizations only. A personal publisher has a slug too, and returning
        one here would put an explorer's own profile behind a business page they
        never asked to have.
        """
        result = await self.session.execute(
            select(Publisher).where(
                Publisher.slug == slug,
                Publisher.type == TYPE_ORGANIZATION,
                Publisher.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def published_listings_of(
        self, publisher_id: uuid.UUID, *, limit: int = 50
    ) -> list[Experience]:
        """What this business currently has published.

        Built on `catalog.repository.published_experiences`, which is the same
        query every discovery surface uses - so a listing withheld by moderation
        disappears from a business page at the same moment it disappears from
        search, rather than lingering on the one surface that wrote its own
        filter.
        """
        from app.domains.catalog.repository import published_experiences

        result = await self.session.execute(
            published_experiences()
            .where(Experience.publisher_id == publisher_id)
            .order_by(Experience.published_at.desc().nullslast())
            .limit(limit)
        )
        return list(result.scalars().unique().all())

    async def _member_of(
        self, publisher_id: uuid.UUID, member_id: uuid.UUID
    ) -> PublisherMember:
        """Load a membership, refusing one that belongs to a different business.

        The `publisher_id` check is what makes cross-business tampering
        impossible rather than merely unlikely: without it, an administrator of
        one business could pass another business's member id and have the
        permission check pass against their own.
        """
        member = await self.session.get(PublisherMember, member_id)
        if member is None or member.publisher_id != publisher_id:
            raise NotFoundError("Team member not found.", code="MEMBER_NOT_FOUND")
        if member.status == MEMBERSHIP_REMOVED:
            raise NotFoundError("Team member not found.", code="MEMBER_NOT_FOUND")
        return member

    async def _membership_for(
        self, publisher_id: uuid.UUID, address: str, invitee: User | None
    ) -> PublisherMember | None:
        conditions = [PublisherMember.invited_email == address]
        if invitee is not None:
            conditions.append(PublisherMember.user_id == invitee.id)
        result = await self.session.execute(
            select(PublisherMember).where(
                PublisherMember.publisher_id == publisher_id, or_(*conditions)
            )
        )
        return result.scalars().first()

    async def _user_by_email(self, address: str) -> User | None:
        result = await self.session.execute(
            select(User)
            .join(UserProfile, UserProfile.user_id == User.id)
            .where(func.lower(UserProfile.email) == address, User.deleted_at.is_(None))
        )
        return result.scalars().first()

    async def _email_of(self, user_id: uuid.UUID | None) -> str | None:
        if user_id is None:
            return None
        address = await self.session.scalar(
            select(UserProfile.email).where(UserProfile.user_id == user_id)
        )
        return (address or "").strip().lower() or None

    async def assert_can_publish_as(self, user: User, publisher_id: uuid.UUID) -> Publisher:
        """Resolve a publisher the explorer is entitled to post as.

        Now membership-aware. It used to compare `owner_user_id` and nothing
        else, which is exactly right while a publisher is one person and becomes
        wrong the moment a business has staff - an editor hired to post for a
        hotel could not post for it.
        """
        publisher = await self.session.get(Publisher, publisher_id)
        if publisher is None or publisher.deleted_at is not None:
            raise NotFoundError("Publisher not found.", code="PUBLISHER_NOT_FOUND")

        held = await self.permissions_for(user, publisher_id)
        if permissions.CONTENT_CREATE not in held:
            raise PermissionDeniedError(
                "You cannot post as this publisher.", code="NOT_PUBLISHER_OWNER"
            )
        return publisher

    # ------------------------------------------------------------ experiences

    async def _load_owned(
        self,
        user: User,
        experience_id: uuid.UUID,
        *,
        permission: str = permissions.CONTENT_EDIT,
    ) -> Experience:
        result = await self.session.execute(
            select(Experience)
            .where(Experience.id == experience_id, Experience.deleted_at.is_(None))
            .options(
                selectinload(Experience.publisher),
                selectinload(Experience.media),
                selectinload(Experience.events),
                selectinload(Experience.tags),
                selectinload(Experience.venue).selectinload(Venue.neighborhood),
                selectinload(Experience.category),
                selectinload(Experience.city),
            )
        )
        experience = result.scalar_one_or_none()
        if experience is None:
            raise NotFoundError("Experience not found.", code="EXPERIENCE_NOT_FOUND")

        publisher = experience.publisher
        if publisher is None:
            raise NotFoundError("Experience not found.", code="EXPERIENCE_NOT_FOUND")

        # Membership-aware, so a business's editor can open its drafts. Still
        # deliberately 404 rather than 403: confirming that someone else's draft
        # exists is itself a disclosure.
        #
        # `permission` defaults to editing because that is what every caller of
        # this loader goes on to do. A read-only role therefore cannot reach a
        # draft through here, which is correct - an analyst was given sight of
        # the numbers, not of unpublished work.
        held = await self.permissions_for(user, publisher.id)
        if permission not in held:
            raise NotFoundError("Experience not found.", code="EXPERIENCE_NOT_FOUND")
        return experience

    async def _unique_experience_slug(self, title: str) -> str:
        base = slugify(title) or f"experience-{uuid.uuid4().hex[:8]}"
        candidate = base
        for suffix in range(0, 50):
            if suffix:
                candidate = f"{base}-{suffix}"
            exists = await self.session.scalar(
                select(func.count()).select_from(Experience).where(Experience.slug == candidate)
            )
            if not exists:
                return candidate
        return f"{base}-{uuid.uuid4().hex[:6]}"

    async def _resolve_city(self, city_slug: str) -> City:
        result = await self.session.execute(select(City).where(City.slug == city_slug))
        city = result.scalar_one_or_none()
        if city is None:
            raise BadRequestError(
                f"Unknown city '{city_slug}'.",
                code="CITY_NOT_FOUND",
                details={"citySlug": city_slug},
            )
        return city

    async def _city_for(self, city_slug: str | None, venue: Venue | None) -> City:
        """Which city a post belongs to.

        An explicit slug wins, because a caller that named one meant it. Failing
        that the venue decides - it was itself filed under the city its
        coordinates are in, so a post and its location can never disagree, which
        they could when both were chosen independently on a form.
        """
        if city_slug:
            return await self._resolve_city(city_slug)
        if venue is not None:
            return await self.session.get(City, venue.city_id)
        raise ValidationError(
            "Add a location, or say which city this is in.",
            code="CITY_REQUIRED",
        )

    async def _city_at(self, latitude: float, longitude: float) -> City:
        """The city these coordinates are in, materialised if it is new.

        Raises rather than inventing a placeholder when the point cannot be
        named: `venues.city_id` is NOT NULL, and a row filed under "Unknown"
        would be a real venue nobody could ever find by place. Reaching this
        means the place provider is unreachable or the pin is in open ocean, and
        both are worth saying out loud.
        """
        city = await catalog_cities.city_for_point(self.session, latitude, longitude)
        if city is None:
            raise BadRequestError(
                "We could not work out which city that location is in. "
                "Try moving the pin, or search for the place by name.",
                code="CITY_UNRESOLVED",
                details={"latitude": latitude, "longitude": longitude},
            )
        return city

    async def _resolve_category(self, slug: str | None) -> Category | None:
        if not slug:
            return None
        result = await self.session.execute(select(Category).where(Category.slug == slug))
        category = result.scalar_one_or_none()
        if category is None:
            raise BadRequestError(f"Unknown category '{slug}'.", code="CATEGORY_NOT_FOUND")
        return category

    async def _resolve_tags(self, slugs: list[str] | None) -> list[Tag]:
        """Resolve tag slugs, silently ignoring unknown ones.

        Tags are a soft classification. Rejecting a whole post because one tag was
        misspelled would be a poor trade for the author.
        """
        if not slugs:
            return []
        result = await self.session.execute(select(Tag).where(Tag.slug.in_(slugs)))
        return list(result.scalars().all())

    async def create_experience(
        self,
        user: User,
        *,
        title: str,
        description: str,
        experience_type: str,
        city_slug: str | None = None,
        summary: str | None = None,
        category_slug: str | None = None,
        venue_id: uuid.UUID | None = None,
        tags: list[str] | None = None,
        price_type: str = "free",
        price_amount: float | None = None,
        price_max: float | None = None,
        currency: str | None = None,
        duration_minutes: int | None = None,
        is_indoor: bool | None = None,
        accessibility: dict | None = None,
        suitability: list[str] | None = None,
        external_ticket_url: str | None = None,
        publisher_id: uuid.UUID | None = None,
    ) -> Experience:
        """Create a draft. Nothing is visible until the author publishes it.

        `city_slug` is optional because a post's city is a property of where it
        is, and where it is is the venue. Supplying one is for the callers that
        genuinely know better - the seeder, the feed importer - and for a post
        with no venue at all, which is the only case where the city cannot be
        derived from anything else.
        """
        # Whose name goes on this. An explicit `publisher_id` is still honoured,
        # because a team member posts for the business they were invited to and
        # that business is not their own account's identity. With none given,
        # `publisher_for` answers from the account itself - the business for a
        # business account, the person for an individual - so the ordinary case
        # needs no choice and offers none.
        publisher = (
            await self.assert_can_publish_as(user, publisher_id)
            if publisher_id
            else await self.publisher_for(user)
        )
        category = await self._resolve_category(category_slug)

        venue = None
        if venue_id is not None:
            venue = await self.session.get(Venue, venue_id)
            if venue is None or venue.deleted_at is not None:
                raise BadRequestError("Unknown venue.", code="VENUE_NOT_FOUND")

        city = await self._city_for(city_slug, venue)

        if price_type != "free" and price_amount is None:
            raise ValidationError(
                "A price is required unless the experience is free.",
                code="PRICE_REQUIRED",
            )

        experience = Experience(
            publisher_id=publisher.id,
            city_id=city.id,
            venue_id=venue.id if venue else None,
            category_id=category.id if category else None,
            title=title.strip(),
            slug=await self._unique_experience_slug(title),
            summary=(summary or "").strip() or None,
            description=description.strip(),
            type=experience_type,
            status=STATUS_DRAFT,
            price_type=price_type,
            price_amount=price_amount,
            price_max=price_max,
            currency=(currency or city.currency).upper(),
            duration_minutes=duration_minutes,
            is_indoor=is_indoor,
            accessibility=accessibility or {},
            # Normalised again here rather than trusted from the schema: the
            # seeder and the feed importer call this service directly and never
            # pass through a request model.
            suitability=suitability_vocab.normalise(suitability),
            external_ticket_url=(external_ticket_url or None),
            attributes={},
            tags=await self._resolve_tags(tags),
            media=[],
            events=[],
        )
        self.session.add(experience)
        await self.session.flush()
        logger.info(
            "experience_created", experience_id=str(experience.id), publisher_id=str(publisher.id)
        )
        # Re-load with relations eagerly attached. A freshly constructed instance
        # has none of them loaded, and serializing it would trigger a lazy load -
        # which raises MissingGreenlet under an async session.
        return await self._load_owned(user, experience.id)

    async def update_experience(
        self, user: User, experience_id: uuid.UUID, changes: dict
    ) -> Experience:
        experience = await self._load_owned(user, experience_id)
        if experience.status == STATUS_ARCHIVED:
            raise ConflictError(
                "Restore this experience before editing it.", code="EXPERIENCE_ARCHIVED"
            )

        simple_fields = {
            "title",
            "summary",
            "description",
            "type",
            "price_type",
            "price_amount",
            "price_max",
            "currency",
            "duration_minutes",
            "is_indoor",
            "accessibility",
            "external_ticket_url",
        }
        for field, value in changes.items():
            if field in simple_fields and value is not None:
                setattr(experience, field, value)

        # Not in `simple_fields` because it needs normalising, and because an
        # empty list is a meaningful update here - a publisher withdrawing a claim
        # they can no longer honour must be able to, and `value is not None` above
        # would let [] through while the vocabulary check would not have run.
        if changes.get("suitability") is not None:
            experience.suitability = suitability_vocab.normalise(changes["suitability"])

        if changes.get("city_slug"):
            experience.city_id = (await self._resolve_city(changes["city_slug"])).id
        if "category_slug" in changes:
            category = await self._resolve_category(changes["category_slug"])
            experience.category_id = category.id if category else None
        if "tags" in changes and changes["tags"] is not None:
            experience.tags = await self._resolve_tags(changes["tags"])
        if "venue_id" in changes:
            venue_id = changes["venue_id"]
            if venue_id is None:
                experience.venue_id = None
            else:
                venue = await self.session.get(Venue, venue_id)
                if venue is None or venue.deleted_at is not None:
                    raise BadRequestError("Unknown venue.", code="VENUE_NOT_FOUND")
                experience.venue_id = venue.id

        if experience.price_type != "free" and experience.price_amount is None:
            raise ValidationError(
                "A price is required unless the experience is free.", code="PRICE_REQUIRED"
            )

        await self.session.flush()

        # Only for something already discoverable. Every keystroke in the
        # composer is an edit, and a subscriber does not want three hundred
        # deliveries about a draft nobody can see - the announcement a
        # receiver's cache cares about is that live content changed.
        if experience.status == STATUS_PUBLISHED:
            await self._announce("experience.updated", user, experience)
        return experience

    async def publish(self, user: User, experience_id: uuid.UUID) -> Experience:
        """Make a draft discoverable, after checking it is actually usable."""
        experience = await self._load_owned(
            user, experience_id, permission=permissions.CONTENT_PUBLISH
        )

        if experience.moderation_status == MODERATION_REJECTED:
            raise PermissionDeniedError(
                "This experience was removed by moderation and cannot be republished.",
                code="MODERATION_REJECTED",
            )

        problems = self.readiness_problems(experience)
        if problems:
            raise ValidationError(
                "This experience is not ready to publish yet.",
                code="EXPERIENCE_INCOMPLETE",
                details={"problems": problems},
            )

        experience.status = STATUS_PUBLISHED
        if experience.published_at is None:
            experience.published_at = datetime.now(UTC)
        await self.session.flush()
        await self._announce("experience.published", user, experience)
        logger.info("experience_published", experience_id=str(experience.id))
        return experience

    async def unpublish(self, user: User, experience_id: uuid.UUID) -> Experience:
        experience = await self._load_owned(
            user, experience_id, permission=permissions.CONTENT_PUBLISH
        )
        experience.status = STATUS_DRAFT
        await self.session.flush()
        await self._announce("experience.unpublished", user, experience)
        return experience

    async def _announce(self, event_type: str, user: User, experience: Experience) -> None:
        """Queue a webhook for anyone subscribed (spec DEV-003).

        In this transaction rather than after it, so an announcement cannot
        survive a rollback of the thing it announces. Nothing is sent from here;
        the scheduler drains the queue.

        The payload is identifiers and the title - enough for a receiver to know
        what changed and fetch the rest. Sending the whole record would put a
        copy of the catalogue in somebody's logs and go stale the moment it
        left.
        """
        from app.domains.developer.webhooks import emit

        await emit(
            self.session,
            event_type=event_type,
            owner_user_id=user.id,
            data={
                "experienceId": str(experience.id),
                "slug": experience.slug,
                "title": experience.title,
                "status": experience.status,
            },
        )

    async def archive(self, user: User, experience_id: uuid.UUID) -> Experience:
        experience = await self._load_owned(
            user, experience_id, permission=permissions.CONTENT_DELETE
        )
        experience.status = STATUS_ARCHIVED
        await self.session.flush()
        return experience

    async def delete(self, user: User, experience_id: uuid.UUID) -> Experience:
        """Remove a listing for good, as far as anybody can see.

        Soft, because `deleted_at` is already what the rest of the platform
        reads: the author's own list and `is_discoverable` both exclude it, so
        setting it removes the listing from every surface at once. Keeping the
        row is not sentimentality - an order carries the title and the time it
        was bought for, and a ticket that outlives the listing has to keep
        reading at the door.

        Refusing when something has been sold is the caller's job rather than
        this method's, because the count lives in another domain. Deciding it
        here would mean the publisher domain reading commerce's tables.
        """
        experience = await self._load_owned(
            user, experience_id, permission=permissions.CONTENT_DELETE
        )
        experience.deleted_at = datetime.now(UTC)
        experience.status = STATUS_ARCHIVED
        await self.session.flush()
        logger.info("experience_deleted", experience_id=str(experience.id))
        return experience

    async def restore(self, user: User, experience_id: uuid.UUID) -> Experience:
        experience = await self._load_owned(
            user, experience_id, permission=permissions.CONTENT_DELETE
        )
        if experience.status != STATUS_ARCHIVED:
            raise ConflictError("That experience is not archived.", code="NOT_ARCHIVED")
        # Back to draft, never straight to published: the author decides when it
        # goes live again.
        experience.status = STATUS_DRAFT
        await self.session.flush()
        return experience

    @staticmethod
    def readiness_problems(experience: Experience) -> list[str]:
        """Human-readable reasons an experience cannot go live yet.

        Phrased for the author rather than as validation codes, because this list
        is shown directly in the composer.
        """
        problems: list[str] = []
        if len(experience.title.strip()) < 4:
            problems.append("Give it a title of at least 4 characters.")
        if len(experience.description.strip()) < 40:
            problems.append("Add a description of at least 40 characters.")
        if experience.category_id is None:
            problems.append("Choose a category so people can find it.")
        if experience.venue_id is None:
            problems.append("Add a location.")
        if experience.type == TYPE_EVENT and not [
            event for event in (experience.events or []) if event.status != "cancelled"
        ]:
            problems.append("Add at least one date and time.")
        if experience.price_type != "free" and experience.price_amount is None:
            problems.append("Set a price, or mark it as free.")
        return problems

    # ----------------------------------------------------------------- media

    async def add_media(
        self, user: User, experience_id: uuid.UUID, *, url: str, alt_text: str | None = None
    ) -> Media:
        experience = await self._load_owned(user, experience_id)
        if len(experience.media or []) >= MAX_MEDIA_PER_EXPERIENCE:
            raise ConflictError(
                f"An experience can have at most {MAX_MEDIA_PER_EXPERIENCE} images.",
                code="MEDIA_LIMIT_REACHED",
            )
        # Two legitimate shapes: an absolute http(s) URL, or a path under /media/
        # produced by our own upload endpoint. The second is deliberately narrow -
        # accepting arbitrary relative paths would let a caller point a listing at
        # any route on this host, and "/media/" is the only one we serve files from.
        if not url.startswith(("https://", "http://", "/media/")):
            raise ValidationError(
                "Image URL must be http(s), or an uploaded image.",
                code="INVALID_MEDIA_URL",
            )
        if url.startswith("/media/") and ".." in url:
            raise ValidationError("Invalid image path.", code="INVALID_MEDIA_URL")

        media = Media(
            experience_id=experience.id,
            type="image",
            url=url,
            alt_text=alt_text,
            sort_order=len(experience.media or []),
        )
        self.session.add(media)
        await self.session.flush()
        return media

    async def remove_media(self, user: User, experience_id: uuid.UUID, media_id: uuid.UUID) -> None:
        experience = await self._load_owned(user, experience_id)
        media = next((m for m in (experience.media or []) if m.id == media_id), None)
        if media is None:
            raise NotFoundError("Image not found.", code="MEDIA_NOT_FOUND")
        await self.session.delete(media)
        await self.session.flush()

    # ---------------------------------------------------------------- events

    async def add_event(
        self,
        user: User,
        experience_id: uuid.UUID,
        *,
        start_time: datetime,
        end_time: datetime | None = None,
        capacity: int | None = None,
    ) -> EventInstance:
        experience = await self._load_owned(
            user, experience_id, permission=permissions.EVENTS_MANAGE
        )

        if len(experience.events or []) >= MAX_EVENTS_PER_EXPERIENCE:
            raise ConflictError(
                f"An experience can have at most {MAX_EVENTS_PER_EXPERIENCE} dates.",
                code="EVENT_LIMIT_REACHED",
            )
        if end_time is not None and end_time <= start_time:
            raise ValidationError(
                "The end time must be after the start time.", code="INVALID_EVENT_WINDOW"
            )
        if start_time < datetime.now(UTC):
            raise ValidationError("That start time is in the past.", code="EVENT_IN_PAST")

        event = EventInstance(
            experience_id=experience.id,
            start_time=start_time,
            end_time=end_time,
            status="scheduled",
            capacity=capacity,
            remaining=capacity,
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def cancel_event(
        self,
        user: User,
        experience_id: uuid.UUID,
        event_id: uuid.UUID,
        *,
        reason: str | None = None,
    ) -> EventInstance:
        experience = await self._load_owned(
            user, experience_id, permission=permissions.EVENTS_MANAGE
        )
        event = next((e for e in (experience.events or []) if e.id == event_id), None)
        if event is None:
            raise NotFoundError("Date not found.", code="EVENT_NOT_FOUND")
        # Cancelled rather than deleted: people may have planned around it, and
        # spec 57.03 s18 requires cancellation to be visible rather than silent.
        event.status = "cancelled"
        event.cancellation_reason = reason
        await self.session.flush()

        # The one event type a receiver most needs, because it is the one that
        # invalidates something they already showed somebody.
        from app.domains.developer.webhooks import emit

        await emit(
            self.session,
            event_type="event.cancelled",
            owner_user_id=user.id,
            data={
                "experienceId": str(experience.id),
                "eventInstanceId": str(event.id),
                "startsAt": event.start_time.isoformat(),
                # Carried because a receiver relaying this to their own audience
                # needs to say why, and "cancelled" with no reason reads as a
                # glitch rather than a decision.
                "reason": reason,
            },
        )

        # And the people who were going. A webhook reaches the publisher's own
        # systems; it reaches nobody who bought into this date. Until this was
        # wired up, cancelling was silent to every explorer involved - and worse
        # than silent, because the "starts tonight" reminder stayed queued and
        # would still have gone out.
        from app.domains.explorer.alerts import announce_cancellation

        await announce_cancellation(
            self.session,
            experience_id=experience.id,
            occurrence_id=event.id,
            title=experience.title,
            starts_at=event.start_time,
            reason=reason,
        )
        return event

    async def delete_event(self, user: User, experience_id: uuid.UUID, event_id: uuid.UUID) -> None:
        experience = await self._load_owned(
            user, experience_id, permission=permissions.EVENTS_MANAGE
        )
        event = next((e for e in (experience.events or []) if e.id == event_id), None)
        if event is None:
            raise NotFoundError("Date not found.", code="EVENT_NOT_FOUND")
        if experience.status == STATUS_PUBLISHED:
            raise ConflictError(
                "Cancel this date instead - it is live and people may be relying on it.",
                code="CANCEL_INSTEAD_OF_DELETE",
            )

        # Only a draft's dates reach here, but a draft can have been published
        # once, and the reminder job does not check publication state - so a
        # reminder about this date may already be queued. Deleting the row it
        # points at would leave it to fire about nothing.
        from app.domains.explorer.notifications import NotificationService

        await NotificationService(self.session).cancel_for_subject(event.id)

        await self.session.delete(event)
        await self.session.flush()

    # ------------------------------------------------------------- listing

    async def list_own_experiences(
        self, user: User, *, status: str | None = None
    ) -> list[Experience]:
        """Every experience across all publishers the explorer owns."""
        publisher_ids = (
            (
                await self.session.execute(
                    select(Publisher.id).where(
                        Publisher.owner_user_id == user.id, Publisher.deleted_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )

        if not publisher_ids:
            return []

        stmt = (
            select(Experience)
            .where(
                Experience.publisher_id.in_(list(publisher_ids)),
                Experience.deleted_at.is_(None),
            )
            .options(
                selectinload(Experience.publisher),
                selectinload(Experience.media),
                selectinload(Experience.events),
                selectinload(Experience.tags),
                selectinload(Experience.venue).selectinload(Venue.neighborhood),
                selectinload(Experience.category),
                selectinload(Experience.city),
            )
            .order_by(Experience.updated_at.desc())
        )
        if status:
            stmt = stmt.where(Experience.status == status)

        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())

    async def get_own_experience(self, user: User, experience_id: uuid.UUID) -> Experience:
        return await self._load_owned(
            user, experience_id, permission=permissions.PROFILE_VIEW
        )

    # ------------------------------------------------------------- venues

    async def create_venue(
        self,
        user: User,
        *,
        name: str,
        address: str,
        latitude: float,
        longitude: float,
        city_slug: str | None = None,
        neighborhood_id: uuid.UUID | None = None,
        accessibility: dict | None = None,
        facilities: list[str] | None = None,
        publisher_id: uuid.UUID | None = None,
        place_id: str | None = None,
    ) -> Venue:
        """Create a location to attach experiences to.

        Explorers can add places that are not in the catalog yet - which is how a
        long tail of small venues gets in at all - but coordinates are validated so
        a typo cannot drop a café into the Gulf of Guinea.

        **The city is derived from the coordinates, not supplied.** `city_slug` is
        accepted for callers that already know it - the seeder, the feed importer -
        but a venue anywhere on earth can be created without one, and the row is
        materialised from wherever the pin actually is. Requiring a curated city
        was what limited this platform to the ten somebody had typed in.

        `place_id` records which place the coordinates came from when they were
        chosen from a search rather than dropped on a map. It is stored and not
        otherwise used: the coordinates remain the location, and a venue with no
        identifier is a perfectly ordinary venue.
        """
        publisher = (
            await self.assert_can_publish_as(user, publisher_id)
            if publisher_id
            else await self.personal_publisher(user)
        )

        # Validated before anything is resolved: a bad coordinate should be
        # refused outright rather than after a reverse geocode has been spent
        # asking what is at latitude 400.
        if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
            raise ValidationError("Those coordinates are not valid.", code="INVALID_COORDINATES")

        city = (
            await self._resolve_city(city_slug)
            if city_slug
            else await self._city_at(latitude, longitude)
        )

        venue = Venue(
            name=name.strip(),
            slug=await self._unique_venue_slug(name),
            publisher_id=publisher.id,
            city_id=city.id,
            neighborhood_id=neighborhood_id,
            address=address.strip(),
            latitude=latitude,
            longitude=longitude,
            place_id=(place_id or "").strip() or None,
            facilities=suitability_vocab.normalise(facilities),
            accessibility=accessibility or {},
            opening_hours={},
            contact={},
        )
        self.session.add(venue)
        await self.session.flush()
        return venue

    async def _unique_venue_slug(self, name: str) -> str:
        base = slugify(name) or f"venue-{uuid.uuid4().hex[:8]}"
        candidate = base
        for suffix in range(0, 50):
            if suffix:
                candidate = f"{base}-{suffix}"
            exists = await self.session.scalar(
                select(func.count()).select_from(Venue).where(Venue.slug == candidate)
            )
            if not exists:
                return candidate
        return f"{base}-{uuid.uuid4().hex[:6]}"


__all__ = ["PublishingService", "slugify", "MODERATION_APPROVED"]
