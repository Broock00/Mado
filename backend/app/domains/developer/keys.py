"""API keys (spec DEV-001).

A credential a publisher can put in a script, so that syncing a season of events
from their own system does not mean driving a browser or storing somebody's
password.

**Only a digest is stored**, exactly as refresh tokens and account tokens are
handled (`app/domains/identity/tokens.py`). The plaintext exists once, in the
response to the request that created it, and nowhere else. Somebody who reads
this table cannot call the API with what they find there, and we cannot show a
key again after the fact - which is a real cost, paid deliberately.

**Keys are revoked, never deleted.** Spec BUSINESS-07 restated for credentials:
a deleted key leaves no answer to "what was that key doing on the fourteenth".
A revoked one stays in the list, greyed out, with the date it stopped working.

**Scopes are narrow and few.** Three, each naming something a publisher
plausibly automates. A key that can do everything its owner can do is a password
with extra steps, and the whole point of issuing one is that it can be handed to
a script without handing over the account.

**Not audited.** `app/domains/trust/audit.py` records administrative authority
over other people. A publisher minting their own key is not that, and putting it
in the same log would start the slide from "what administrators did" towards
"what everybody did".
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, ForeignKey, Index, String, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.mixins import Timestamps, UUIDPrimaryKey
from app.domains.identity.models import User
from app.domains.publisher.models import SCHEMA

logger = get_logger("mado.api_keys")

# `mado_sk_` marks the string as a Mado secret key wherever it turns up - in a
# log, a screenshot, a public repository. Secret-scanning tools work on exactly
# this kind of prefix, and a key that looks like anonymous base64 gets committed
# and never noticed.
KEY_PREFIX = "mado_sk_"
KEY_BYTES = 24

SCOPE_EXPERIENCES_READ = "experiences:read"
SCOPE_EXPERIENCES_WRITE = "experiences:write"
SCOPE_RESERVATIONS_READ = "reservations:read"

# Not a scope anybody can be granted, and deliberately absent from SCOPES below
# so it can never be ticked or requested. It marks the handful of endpoints any
# valid key may call - checking who the key belongs to, and little else - and
# exists so that "which endpoints can a key reach" has one answer rather than
# one for scoped routes and another for these.
ANY_SCOPE = "*"

# What each scope actually permits, in the words shown next to its checkbox.
#
# Every one of these gates a route that exists. A scope nobody enforces is worse
# than no scope at all: it tells the person ticking the box that they have
# restricted something when they have not. There is deliberately no scope for
# the public catalogue - reading what anybody can read needs no permission.
SCOPES: dict[str, str] = {
    SCOPE_EXPERIENCES_READ: "Read your own listings, including drafts and their dates.",
    SCOPE_EXPERIENCES_WRITE: "Create, edit, publish and withdraw your listings.",
    SCOPE_RESERVATIONS_READ: "See who has reserved a place at your events.",
}

MAX_KEYS = 20
MAX_NAME = 80

# The longest a key may live. Not a security boundary on its own - a leaked key
# is dangerous today, not in a year - but a key with no end date outlives the
# integration it was made for, and then outlives the person who made it.
MAX_TTL_DAYS = 365

# `last_used_at` would otherwise be a database write on every single API call.
# A minute's resolution is plenty for the question it answers, which is "is this
# key still in use, or can I revoke it".
LAST_USED_RESOLUTION = timedelta(minutes=1)


class ApiKey(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "api_keys"
    __table_args__ = (
        Index("ix_api_keys_owner", "owner_user_id"),
        {"schema": SCHEMA},
    )

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("identity.users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # What it is for, in the owner's words. Required: an unlabelled key is one
    # nobody dares revoke, because nobody remembers what would break.
    name: Mapped[str] = mapped_column(String(MAX_NAME), nullable=False)

    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    # Enough of the key to recognise it in this list and in a log line, and far
    # too little to use. The middle is what is secret.
    preview: Mapped[str] = mapped_column(String(32), nullable=False)

    scopes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    @property
    def is_live(self) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > datetime.now(UTC)

    @property
    def state(self) -> str:
        """One word for the interface, so it never has to compute this itself."""
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at is not None and self.expires_at <= datetime.now(UTC):
            return "expired"
        return "active"


def digest(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def _preview(plaintext: str) -> str:
    return f"{plaintext[: len(KEY_PREFIX) + 4]}...{plaintext[-4:]}"


class ApiKeyService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def issue(
        self,
        owner: User,
        *,
        name: str,
        scopes: list[str],
        expires_in_days: int | None = None,
    ) -> tuple[ApiKey, str]:
        """Mint a key, returning the row and the plaintext exactly once."""
        name = name.strip()
        if not name:
            raise ValidationError("Give the key a name you will recognise.", code="NAME_REQUIRED")

        unknown = [scope for scope in scopes if scope not in SCOPES]
        if unknown:
            raise ValidationError(
                f"Unknown scope: {', '.join(sorted(unknown))}.", code="UNKNOWN_SCOPE"
            )
        if not scopes:
            # A key with no scopes authenticates and then may do nothing, which
            # reads as a broken key rather than as a careful one.
            raise ValidationError("Choose at least one thing the key may do.", code="NO_SCOPES")

        live = [key for key in await self.mine(owner) if key.is_live]
        if len(live) >= MAX_KEYS:
            raise ValidationError(
                f"You already have {MAX_KEYS} active keys. Revoke one first.",
                code="TOO_MANY_KEYS",
            )

        expires_at = None
        if expires_in_days is not None:
            if not 1 <= expires_in_days <= MAX_TTL_DAYS:
                raise ValidationError(
                    f"A key can last between a day and {MAX_TTL_DAYS} days.",
                    code="INVALID_EXPIRY",
                )
            expires_at = datetime.now(UTC) + timedelta(days=expires_in_days)

        plaintext = KEY_PREFIX + secrets.token_urlsafe(KEY_BYTES)
        key = ApiKey(
            owner_user_id=owner.id,
            name=name[:MAX_NAME],
            token_hash=digest(plaintext),
            preview=_preview(plaintext),
            # Sorted and de-duplicated so two keys with the same permissions
            # compare equal, and the list does not depend on checkbox order.
            scopes=sorted(set(scopes)),
            expires_at=expires_at,
        )
        self.session.add(key)
        await self.session.flush()

        logger.info("api_key_issued", key_id=str(key.id), owner=str(owner.id), scopes=key.scopes)
        return key, plaintext

    async def mine(self, owner: User) -> list[ApiKey]:
        result = await self.session.execute(
            select(ApiKey)
            .where(ApiKey.owner_user_id == owner.id)
            .order_by(ApiKey.created_at.desc())
        )
        return list(result.scalars().all())

    async def revoke(self, owner: User, key_id: uuid.UUID) -> ApiKey:
        """Stop a key working. Irreversible on purpose.

        Un-revoking would mean a key that leaked, was revoked, and then works
        again - so a compromised credential could be brought back by whoever
        compromised the account. Making a new key costs nothing.
        """
        key = await self.session.get(ApiKey, key_id)
        if key is None or key.owner_user_id != owner.id:
            # Not "this key belongs to somebody else": that confirms the id
            # exists to whoever is guessing.
            raise NotFoundError("No such key.", code="API_KEY_NOT_FOUND")
        if key.revoked_at is None:
            key.revoked_at = datetime.now(UTC)
            logger.info("api_key_revoked", key_id=str(key.id), owner=str(owner.id))
        return key

    async def authenticate(self, plaintext: str) -> tuple[ApiKey, User] | None:
        """Resolve a presented key, or None.

        None rather than an exception, and one shape for every failure - unknown,
        revoked, expired, owner suspended. The caller turns that into a single
        401, because distinguishing them tells somebody probing with keys they do
        not have which of their guesses was closest.
        """
        if not plaintext.startswith(KEY_PREFIX):
            return None

        result = await self.session.execute(
            select(ApiKey).where(ApiKey.token_hash == digest(plaintext))
        )
        key = result.scalar_one_or_none()
        if key is None or not key.is_live:
            return None

        owner = await self.session.get(User, key.owner_user_id)
        if owner is None or owner.deleted_at is not None or owner.status != "active":
            # A suspended account's keys stop working with it. Otherwise
            # suspension is a formality anybody with a script can step around.
            return None

        self._touch(key)
        return key, owner

    @staticmethod
    def _touch(key: ApiKey) -> None:
        now = datetime.now(UTC)
        if key.last_used_at is None or now - key.last_used_at >= LAST_USED_RESOLUTION:
            key.last_used_at = now
