"""The audit log (spec ADM-004, SECURITY-01 §12).

A record of authority being used: who did what to whom, and when.

**This logs power, not people.** Every entry describes an administrative action -
an account suspended, moderator rights granted, a listing withheld, a flag
switched. It does not record explorers browsing, searching, saving or planning.
That distinction is the whole design. A log of what administrators do is
accountability; a log of what everybody does is surveillance, and the platform
already declines to build the second one (see `app/domains/explorer/summary.py`,
where the interaction log is shown to the person it is about and to nobody else).

**Append-only.** There is no update and no delete, and that is not an oversight
to be tidied up later - a record somebody with power can edit is not evidence.
The service exposes `record` and two read methods. Retention and off-host
shipping are operational concerns that belong above this layer.

**Written in the same transaction as the action.** Not fire-and-forget. If the
audit row cannot be written, the suspension does not happen either, because a
moderator action nobody can account for is worse than one that failed loudly.
This is the opposite of the choice made for analytics and email, where a failed
write must never block the user - and the difference is that those describe
things, while this one authorises them.

**The subject is stored as an id and a label.** The id is what an investigation
follows; the label is what the row still says after the account is deleted.
Storing only the id gives you a page of UUIDs, and storing only the name loses
the trail.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.logging import get_logger
from app.core.mixins import UUIDPrimaryKey
from app.domains.identity.models import User

logger = get_logger("mado.audit")

SCHEMA = "identity"

# Actions worth a permanent record. A closed set: an unrecognised action is
# still written, but naming the ones that exist keeps the log greppable and
# stops the vocabulary drifting into free text.
ACCOUNT_SUSPENDED = "account.suspended"
ACCOUNT_RESTORED = "account.restored"
MODERATOR_GRANTED = "moderator.granted"
MODERATOR_REVOKED = "moderator.revoked"
VERIFICATION_APPROVED = "verification.approved"
VERIFICATION_REFUSED = "verification.refused"
CONTENT_APPROVED = "content.approved"
CONTENT_REJECTED = "content.rejected"
FLAG_CHANGED = "flag.changed"

KNOWN_ACTIONS = frozenset(
    {
        ACCOUNT_SUSPENDED,
        ACCOUNT_RESTORED,
        MODERATOR_GRANTED,
        MODERATOR_REVOKED,
        VERIFICATION_APPROVED,
        VERIFICATION_REFUSED,
        CONTENT_APPROVED,
        CONTENT_REJECTED,
        FLAG_CHANGED,
    }
)

MAX_REASON = 1000


class AuditEntry(Base, UUIDPrimaryKey):
    """One administrative action. Never updated, never deleted.

    No `Timestamps` mixin: that carries `updated_at`, and a column implying an
    audit row can be modified is a column somebody will eventually modify.
    """

    __tablename__ = "audit_entries"
    __table_args__ = (
        Index("ix_audit_occurred", "occurred_at"),
        Index("ix_audit_actor", "actor_user_id", "occurred_at"),
        Index("ix_audit_subject", "subject_id", "occurred_at"),
        {"schema": SCHEMA},
    )

    # Nullable so a system action - a scheduled job withholding content - is
    # recorded rather than dropped for want of a person to blame.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("identity.users.id", ondelete="SET NULL"), default=None
    )
    # Kept even if the actor's account is later deleted. A trail that empties
    # itself when somebody leaves is not a trail.
    actor_label: Mapped[str] = mapped_column(String(200), nullable=False)

    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # What was acted on. No foreign key: subjects live in several domains, and
    # the record must outlive the thing it describes.
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), default=None)
    subject_label: Mapped[str | None] = mapped_column(String(300), default=None)

    # Why, in the actor's own words, where they gave one.
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    # Anything structured worth keeping: the before and after of a flag, the
    # risk score that led to a withholding.
    context: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # Ties an entry to the request that produced it, so a log line and an audit
    # row can be put side by side during an investigation.
    request_id: Mapped[str | None] = mapped_column(String(64), default=None)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class AuditLog:
    """Write and read the record. There is deliberately no way to change it."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def record(
        self,
        *,
        actor: User | None,
        action: str,
        subject_type: str,
        subject_id: uuid.UUID | None = None,
        subject_label: str | None = None,
        reason: str | None = None,
        context: dict | None = None,
        request_id: str | None = None,
    ) -> AuditEntry:
        """Add an entry to the caller's transaction.

        Not async and does not flush: the entry joins whatever transaction the
        action is already in, so the two commit together or neither does. A
        caller who forgets to commit loses the action as well as the record,
        which is the correct pairing.
        """
        if action not in KNOWN_ACTIONS:
            # Written anyway. Refusing to record an action because its name is
            # unfamiliar would lose exactly the unusual event most worth having.
            logger.warning("audit_unknown_action", action=action)

        entry = AuditEntry(
            actor_user_id=actor.id if actor else None,
            actor_label=_label_for(actor),
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            subject_label=(subject_label or "")[:300] or None,
            reason=(reason or "").strip()[:MAX_REASON] or None,
            context=context or {},
            request_id=request_id,
            occurred_at=datetime.now(UTC),
        )
        self.session.add(entry)

        logger.info(
            "audit",
            action=action,
            actor=str(actor.id) if actor else "system",
            subject=str(subject_id) if subject_id else subject_type,
        )
        return entry

    async def recent(
        self,
        *,
        action: str | None = None,
        actor_id: uuid.UUID | None = None,
        subject_id: uuid.UUID | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        """Newest first, because an investigation starts from what just happened."""
        stmt = select(AuditEntry)
        if action:
            stmt = stmt.where(AuditEntry.action == action)
        if actor_id:
            stmt = stmt.where(AuditEntry.actor_user_id == actor_id)
        if subject_id:
            stmt = stmt.where(AuditEntry.subject_id == subject_id)
        if since:
            stmt = stmt.where(AuditEntry.occurred_at >= since)

        result = await self.session.execute(
            stmt.order_by(AuditEntry.occurred_at.desc()).limit(min(limit, 500))
        )
        return list(result.scalars().all())

    async def for_subject(self, subject_id: uuid.UUID, *, limit: int = 50) -> list[AuditEntry]:
        """Everything ever done to one account or listing.

        The question an investigation actually asks - "what happened to this
        person" - which is why `subject_id` has its own index.
        """
        return await self.recent(subject_id=subject_id, limit=limit)


def _label_for(actor: User | None) -> str:
    if actor is None:
        return "system"
    profile = getattr(actor, "profile", None)
    name = getattr(profile, "display_name", None)
    return (name or str(actor.id))[:200]


def window_since(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)
