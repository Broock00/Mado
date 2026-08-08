"""Account administration and publisher verification (spec ADM-001, TRST-001).

Both were listed as MVP and neither existed. Moderator rights were granted by a
CLI command, which means the only way to appoint one was shell access to the
production host; and `verification_status` was a column the seed wrote, with no
way for a publisher to ask for verification or for anyone to grant it.

The governing rule is the same one that shapes moderation: **automated systems
detect, humans decide** (spec BUSINESS-07). Nothing here is automatic. Every
action recorded below is one a person took, and every one is reversible.

Three things the design is careful about:

**Suspension withholds, it does not delete.** A suspended account keeps its
posts, its saved list and its history; what it loses is the ability to publish
and to be seen. Deleting someone's work because they behaved badly punishes the
people who found that work useful.

**Verification is a claim about identity, not quality.** A verified publisher is
one whose identity somebody checked. It says nothing about whether their events
are good, and the ranking weight it carries is deliberately small.

**Privilege is never self-granted.** Moderator status is set by another
moderator and recorded with who did it. The check reads a dedicated column that
no API path lets an explorer write.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.core.logging import get_logger
from app.domains.identity.models import User, UserProfile
from app.domains.publisher.models import Publisher
from app.domains.trust import audit

logger = get_logger("mado.administration")

STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"

VERIFICATION_UNVERIFIED = "unverified"
VERIFICATION_REQUESTED = "requested"
VERIFICATION_VERIFIED = "verified"
VERIFICATION_REFUSED = "refused"

# Trust granted on verification. Deliberately modest: verification is a claim
# about identity, not about quality, and `trust` is only 0.04 of ranking anyway.
# A verified publisher who posts badly should not outrank a good anonymous one.
TRUST_ON_VERIFICATION = 3


@dataclass(slots=True)
class AccountSummary:
    """What an administrator needs to make a decision, and nothing more.

    Notably absent: the email address is included because identifying an account
    is the whole job here, but nothing about what the person searched for,
    planned or was recommended. An admin console is not a surveillance surface.
    """

    id: uuid.UUID
    display_name: str
    email: str | None
    status: str
    is_moderator: bool
    is_verified: bool
    created_at: datetime
    published_count: int
    reported_count: int


class AdministrationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------- accounts

    async def list_accounts(
        self,
        *,
        query: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[AccountSummary]:
        """Find accounts, by name or email.

        Deliberately requires a filter to be useful rather than paginating the
        whole user table: an admin looking for a specific person should search,
        and browsing everyone is not a workflow worth supporting.
        """
        stmt = (
            select(User)
            .join(UserProfile, UserProfile.user_id == User.id)
            .options(selectinload(User.profile))
        )
        if query:
            needle = f"%{query.strip().lower()}%"
            stmt = stmt.where(
                func.lower(UserProfile.display_name).like(needle)
                | func.lower(UserProfile.email).like(needle)
            )
        if status:
            stmt = stmt.where(User.status == status)

        users = list(
            (await self.session.execute(stmt.order_by(User.created_at.desc()).limit(limit)))
            .scalars()
            .unique()
        )
        if not users:
            return []

        counts = await self._publishing_counts({user.id for user in users})
        return [
            AccountSummary(
                id=user.id,
                display_name=user.profile.display_name if user.profile else "Unknown",
                email=user.profile.email if user.profile else None,
                status=user.status,
                is_moderator=user.is_moderator,
                is_verified=user.is_verified,
                created_at=user.created_at,
                published_count=counts.get(user.id, (0, 0))[0],
                reported_count=counts.get(user.id, (0, 0))[1],
            )
            for user in users
        ]

    async def summarise(self, user_id: uuid.UUID) -> AccountSummary:
        """One account, in the same shape the list returns."""
        user = await self._load(user_id)
        published, reported = (await self._publishing_counts({user_id})).get(user_id, (0, 0))
        return AccountSummary(
            id=user.id,
            display_name=user.profile.display_name if user.profile else "Unknown",
            email=user.profile.email if user.profile else None,
            status=user.status,
            is_moderator=user.is_moderator,
            is_verified=user.is_verified,
            created_at=user.created_at,
            published_count=published,
            reported_count=reported,
        )

    async def _publishing_counts(self, user_ids: set[uuid.UUID]) -> dict:
        """How much each account published, and how much of it drew reports.

        The pair is what an administrator actually weighs: ten posts and no
        reports is a contributor, two posts and nine reports is a problem, and
        neither number alone says which.
        """
        from app.domains.catalog.models import Experience

        result = await self.session.execute(
            select(
                Publisher.owner_user_id,
                func.count(Experience.id),
                func.coalesce(func.sum(Experience.report_count), 0),
            )
            .join(Experience, Experience.publisher_id == Publisher.id)
            .where(Publisher.owner_user_id.in_(user_ids))
            .group_by(Publisher.owner_user_id)
        )
        return {row[0]: (int(row[1]), int(row[2])) for row in result}

    async def set_suspended(
        self, actor: User, user_id: uuid.UUID, *, suspended: bool, reason: str | None = None
    ) -> User:
        """Suspend or restore an account.

        Withholds rather than deletes: the account keeps its posts, its saved
        list and its history, and loses the ability to publish and be seen.
        Fully reversible, because a suspension made in error should cost the
        person nothing once it is lifted.
        """
        user = await self._load(user_id)

        if user.id == actor.id:
            # Not a safety property so much as a usability one: an administrator
            # who suspends themselves cannot undo it.
            raise ConflictError(
                "You cannot suspend your own account.", code="CANNOT_SUSPEND_SELF"
            )
        if user.is_moderator and suspended:
            # A moderator has to be demoted first, deliberately, rather than
            # having their privileges removed as a side effect.
            raise ConflictError(
                "Remove moderator rights before suspending this account.",
                code="MODERATOR_MUST_BE_DEMOTED",
            )

        user.status = STATUS_SUSPENDED if suspended else STATUS_ACTIVE

        # In the same transaction as the change, not fire-and-forget. If the
        # record cannot be written the suspension does not happen either: a
        # moderator action nobody can account for is worse than one that failed.
        audit.AuditLog(self.session).record(
            actor=actor,
            action=audit.ACCOUNT_SUSPENDED if suspended else audit.ACCOUNT_RESTORED,
            subject_type="user",
            subject_id=user.id,
            subject_label=user.profile.display_name if user.profile else None,
            reason=reason,
        )
        logger.info(
            "account_status_changed",
            user_id=str(user.id),
            actor_id=str(actor.id),
            status=user.status,
            reason=reason,
        )
        return user

    async def set_moderator(self, actor: User, user_id: uuid.UUID, *, moderator: bool) -> User:
        """Grant or remove moderator rights.

        Only an existing moderator may do this, and the check reads a dedicated
        column that no explorer-facing path can write - the flag was previously
        kept in a JSONB field the explorer themselves could edit.
        """
        user = await self._load(user_id)

        if user.id == actor.id and not moderator:
            raise ConflictError(
                "You cannot remove your own moderator rights.", code="CANNOT_DEMOTE_SELF"
            )
        if user.status == STATUS_SUSPENDED and moderator:
            raise ConflictError(
                "A suspended account cannot be made a moderator.", code="ACCOUNT_SUSPENDED"
            )

        user.is_moderator = moderator
        audit.AuditLog(self.session).record(
            actor=actor,
            action=audit.MODERATOR_GRANTED if moderator else audit.MODERATOR_REVOKED,
            subject_type="user",
            subject_id=user.id,
            subject_label=user.profile.display_name if user.profile else None,
        )
        logger.info(
            "moderator_rights_changed",
            user_id=str(user.id),
            actor_id=str(actor.id),
            moderator=moderator,
        )
        return user

    # --------------------------------------------------------- verification

    async def request_verification(self, user: User, *, note: str | None = None) -> Publisher:
        """A publisher asks to be verified.

        Anyone may ask. The request carries whatever they offer as evidence, and
        a person decides - there is no automatic path to a verified badge, which
        is the entire point of it meaning anything.
        """
        publisher = await self._publisher_for(user)

        if publisher.verification_status == VERIFICATION_VERIFIED:
            raise ConflictError("You are already verified.", code="ALREADY_VERIFIED")
        if publisher.verification_status == VERIFICATION_REQUESTED:
            raise ConflictError(
                "Your request is already waiting for review.", code="ALREADY_REQUESTED"
            )

        publisher.verification_status = VERIFICATION_REQUESTED
        publisher.verification_note = (note or "").strip()[:1000] or None
        publisher.verification_requested_at = datetime.now(UTC)

        logger.info("verification_requested", publisher_id=str(publisher.id))
        return publisher

    async def my_verification(self, user: User) -> Publisher:
        """Where a publisher's own request stands.

        Separate from asking, because someone who already asked needs to be told
        they are waiting rather than shown the button a second time.
        """
        return await self._publisher_for(user)

    async def pending_verifications(self, *, limit: int = 50) -> list[Publisher]:
        result = await self.session.execute(
            select(Publisher)
            .where(Publisher.verification_status == VERIFICATION_REQUESTED)
            .order_by(Publisher.created_at)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def decide_verification(
        self,
        actor: User,
        publisher_id: uuid.UUID,
        *,
        approve: bool,
        note: str | None = None,
    ) -> Publisher:
        """Grant or refuse verification.

        Refusal is not permanent: a publisher who was refused for supplying poor
        evidence should be able to come back with better. It returns them to
        unverified rather than marking them rejected forever.
        """
        publisher = await self.session.get(Publisher, publisher_id)
        if publisher is None:
            raise NotFoundError("Publisher not found.", code="PUBLISHER_NOT_FOUND")

        publisher.verification_decided_at = datetime.now(UTC)
        if approve:
            publisher.verification_status = VERIFICATION_VERIFIED
            publisher.trust_level = max(publisher.trust_level, TRUST_ON_VERIFICATION)
        else:
            # Back to unverified, not to a terminal "refused" state, so trying
            # again is possible without an administrator having to reset it.
            publisher.verification_status = VERIFICATION_UNVERIFIED

        audit.AuditLog(self.session).record(
            actor=actor,
            action=audit.VERIFICATION_APPROVED if approve else audit.VERIFICATION_REFUSED,
            subject_type="publisher",
            subject_id=publisher.id,
            subject_label=publisher.name,
            reason=note,
        )
        logger.info(
            "verification_decided",
            publisher_id=str(publisher.id),
            actor_id=str(actor.id),
            approved=approve,
            note=note,
        )
        return publisher

    # ------------------------------------------------------------- internals

    async def _load(self, user_id: uuid.UUID) -> User:
        result = await self.session.execute(
            select(User).where(User.id == user_id).options(selectinload(User.profile))
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise NotFoundError("Account not found.", code="ACCOUNT_NOT_FOUND")
        return user

    async def _publisher_for(self, user: User) -> Publisher:
        result = await self.session.execute(
            select(Publisher).where(Publisher.owner_user_id == user.id)
        )
        publisher = result.scalars().first()
        if publisher is None:
            raise NotFoundError(
                "Publish something first - verification applies to a publisher.",
                code="NO_PUBLISHER",
            )
        return publisher


def require_moderator(user: User) -> User:
    """Gate an administrative action.

    Reads the dedicated column rather than anything an explorer can write. Kept
    here as well as in the trust routes so there is one definition of the check
    rather than two that can drift.
    """
    if not user.is_moderator:
        raise PermissionDeniedError(
            "This is restricted to platform moderators.", code="NOT_A_MODERATOR"
        )
    return user
