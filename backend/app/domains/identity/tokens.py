"""One-time account tokens: email verification and password reset.

Both flows are the same mechanism pointed at different problems, so they share
one table and one set of rules. What those rules protect against:

**The database is not a set of skeleton keys.** Only a SHA-256 digest is stored,
exactly as refresh tokens are handled. Someone who reads the table cannot mint a
password reset from it. The plaintext exists once, in the email, and nowhere
else - which is also why a lost link cannot be recovered, only reissued.

**A link is single-use and short-lived.** Reset links reach an inbox, and inboxes
get forwarded, backed up and breached. An hour is long enough for a person who
asked for one and short enough that a stale mailbox is not a standing key.

**Asking says nothing about who exists.** `request_password_reset` behaves
identically for a registered address and an unknown one. Anything else turns the
forgot-password form into an account enumeration oracle - the exact leak
`authenticate` already goes out of its way to avoid.

**Resetting a password ends every session.** Someone resetting a password may be
doing it because somebody else has their account. Leaving the intruder's session
alive would make the reset theatre.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, ForeignKey, String, select
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.core.mixins import Timestamps, UUIDPrimaryKey
from app.domains.identity.models import SCHEMA

logger = get_logger("mado.account_tokens")

PURPOSE_VERIFY_EMAIL = "verify_email"
PURPOSE_RESET_PASSWORD = "reset_password"

# Reset links are the more dangerous of the two, so they live the shorter life.
# A verification link is only worth as much as the address it was sent to, and
# giving someone a day to find the email in a cluttered inbox costs nothing.
RESET_TTL = timedelta(hours=1)
VERIFY_TTL = timedelta(days=1)

TTLS = {PURPOSE_RESET_PASSWORD: RESET_TTL, PURPOSE_VERIFY_EMAIL: VERIFY_TTL}


class AccountToken(Base, UUIDPrimaryKey, Timestamps):
    """A single-use secret sent to an email address.

    Deliberately not a JWT. A JWT cannot be revoked without keeping a list of the
    ones you revoked, at which point the list is the source of truth and the JWT
    is decoration. A row that can be marked used is simpler and actually
    enforces single use.
    """

    __tablename__ = "account_tokens"
    __table_args__ = {"schema": SCHEMA}

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    # The digest, never the token. Same treatment as refresh tokens.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AccountTokenService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def issue(self, user_id: uuid.UUID, purpose: str) -> str:
        """Mint a token, returning the plaintext exactly once.

        Any outstanding token for the same purpose is spent first. Two live
        reset links mean two chances for an old one to be found in a mailbox,
        and someone who asked twice is telling you the first one did not arrive.
        """
        await self._spend_outstanding(user_id, purpose)

        plaintext = secrets.token_urlsafe(32)
        self.session.add(
            AccountToken(
                user_id=user_id,
                purpose=purpose,
                token_hash=_digest(plaintext),
                expires_at=datetime.now(UTC) + TTLS[purpose],
            )
        )
        await self.session.flush()
        logger.info("account_token_issued", user_id=str(user_id), purpose=purpose)
        return plaintext

    async def consume(self, token: str, purpose: str) -> uuid.UUID:
        """Spend a token, returning whose it was.

        Every failure - unknown, wrong purpose, already spent, expired - raises
        the same error. The distinctions are real but telling the caller which
        one applies only helps someone probing with tokens they did not receive.
        """
        result = await self.session.execute(
            select(AccountToken).where(AccountToken.token_hash == _digest(token))
        )
        row = result.scalar_one_or_none()

        now = datetime.now(UTC)
        if (
            row is None
            or row.purpose != purpose
            or row.used_at is not None
            or row.expires_at <= now
        ):
            raise ValidationError(
                "This link is no longer valid. Ask for a new one.",
                code="TOKEN_INVALID",
            )

        row.used_at = now
        logger.info("account_token_consumed", user_id=str(row.user_id), purpose=purpose)
        return row.user_id

    async def _spend_outstanding(self, user_id: uuid.UUID, purpose: str) -> None:
        result = await self.session.execute(
            select(AccountToken).where(
                AccountToken.user_id == user_id,
                AccountToken.purpose == purpose,
                AccountToken.used_at.is_(None),
            )
        )
        now = datetime.now(UTC)
        for row in result.scalars():
            row.used_at = now
