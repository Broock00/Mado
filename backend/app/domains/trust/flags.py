"""Feature flags (spec ADM-003).

Turning a piece of the product on or off without a deploy, and turning it on for
some people before everybody.

**Flags are not settings.** `app/core/config.py` already holds things like
`rate_limit_enabled` and `require_verified_email_to_publish`. Those are operator
configuration: they describe the environment, they are set at deploy time, and
getting one wrong is an outage. A flag is a *product* decision - is this feature
ready for explorers - changeable at runtime by a moderator without shipping
anything. Keeping the two apart matters because the moment product decisions
live in environment variables, changing one needs a release, and the moment
infrastructure lives in a database somebody switches off rate limiting from a
web page.

**An unknown flag is off.** Flags exist to turn new things on, so absence means
the behaviour that existed before - which is the state that has actually been
tested. The alternative, defaulting on, means a typo in a flag name silently
ships an unfinished feature.

**Rollout is sticky.** A percentage rollout that re-rolled per request would
show an explorer a feature on one page and not the next, which is worse than
either state. Bucketing is a hash of the flag key and the explorer's id, so the
same person gets the same answer every time, and a flag at 10% covers a stable
tenth rather than a random tenth each call.

**Every change is audited.** Flags alter what people can see, which makes them
authority, and authority is recorded (spec ADM-004).
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Boolean, Integer, String, Text, select
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.mixins import Timestamps, UUIDPrimaryKey
from app.domains.identity.models import User
from app.domains.trust import audit

logger = get_logger("mado.flags")

SCHEMA = "identity"

# A flag key looks like `concierge.voice` - a surface and a capability. Enforced
# so the list stays readable once there are thirty of them.
KEY_PATTERN = re.compile(r"^[a-z][a-z0-9]*(\.[a-z0-9]+)+$")

MAX_DESCRIPTION = 500


class FeatureFlag(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "feature_flags"
    __table_args__ = {"schema": SCHEMA}

    key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    # What this turns on, in a sentence. Required, because a flag nobody can
    # explain is a flag nobody dares delete.
    description: Mapped[str] = mapped_column(Text, nullable=False)

    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 0-100. Only consulted when `enabled` is true, so switching a flag off is
    # one action rather than "off and also set the percentage to zero".
    rollout_percentage: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), default=None
    )


@dataclass(slots=True)
class FlagState:
    key: str
    description: str
    enabled: bool
    rollout_percentage: int
    updated_at: datetime | None = None


def bucket_of(key: str, identity: str) -> int:
    """Which 0-99 bucket an explorer falls in for one flag.

    Hashed with the flag key as well as the identity, so somebody unlucky in one
    rollout is not systematically last for every future one - without the key in
    the hash, the same tenth of explorers would receive every gradual rollout
    the platform ever does.
    """
    digest = hashlib.sha256(f"{key}:{identity}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 100


def is_enabled_for(flag: FlagState | None, identity: str | None) -> bool:
    """Whether this flag is on for this explorer.

    An absent flag is off: flags turn new things on, so not having one means the
    behaviour that existed before.
    """
    if flag is None or not flag.enabled:
        return False
    if flag.rollout_percentage >= 100:
        return True
    if flag.rollout_percentage <= 0:
        return False
    if identity is None:
        # Nobody to bucket - an anonymous visitor on a partial rollout. Treated
        # as outside it, because a signed-out explorer who then signs in would
        # otherwise see the feature appear and disappear.
        return False
    return bucket_of(flag.key, identity) < flag.rollout_percentage


# Flags that gate something in the codebase. Named here so `app.seed` can
# register them and the admin console lists them from a fresh database - a lever
# nobody can see is a lever nobody pulls, and a flag that only springs into
# existence the first time somebody guesses its key is worse than no flag.
GATED: dict[str, str] = {
    "publisher.assistant": (
        "The AI writing assistant in the composer. The most expensive thing a "
        "publisher can press, so it has a switch."
    ),
}


async def flag_enabled(
    session: AsyncSession, key: str, user: User | None, anonymous_id: str | None = None
) -> bool:
    """One flag, resolved for one explorer.

    A convenience for route code, which otherwise reaches into the service to
    ask a yes-or-no question. Reads the whole (tiny) table for one answer, which
    is the same query `evaluate` runs and is not worth optimising: there are a
    handful of flags and this is not a hot path.
    """
    identity = str(user.id) if user else anonymous_id
    for flag in await FlagService(session).all():
        if flag.key == key:
            return is_enabled_for(flag, identity)
    return False


class FlagService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def all(self) -> list[FlagState]:
        result = await self.session.execute(select(FeatureFlag).order_by(FeatureFlag.key))
        return [_state(flag) for flag in result.scalars()]

    async def evaluate(self, user: User | None, anonymous_id: str | None = None) -> dict[str, bool]:
        """Every flag, resolved for one explorer.

        Returned as a whole map rather than one at a time: the client needs them
        all to render, and asking per flag would be a request per feature.
        """
        identity = str(user.id) if user else anonymous_id
        return {flag.key: is_enabled_for(flag, identity) for flag in await self.all()}

    async def set(
        self,
        actor: User,
        key: str,
        *,
        enabled: bool | None = None,
        rollout_percentage: int | None = None,
        description: str | None = None,
        request_id: str | None = None,
    ) -> FeatureFlag:
        """Change a flag, recording who did it and what it was before."""
        flag = await self._get(key)
        before = {"enabled": flag.enabled, "rolloutPercentage": flag.rollout_percentage}

        if enabled is not None:
            flag.enabled = enabled
        if rollout_percentage is not None:
            if not 0 <= rollout_percentage <= 100:
                raise ValidationError(
                    "A rollout is a percentage between 0 and 100.", code="INVALID_ROLLOUT"
                )
            flag.rollout_percentage = rollout_percentage
        if description is not None:
            flag.description = description.strip()[:MAX_DESCRIPTION] or flag.description

        flag.updated_by_user_id = actor.id

        after = {"enabled": flag.enabled, "rolloutPercentage": flag.rollout_percentage}
        # Recorded even when nothing moved. "Somebody opened this and left it
        # alone" is a fact an investigation may want, and deciding it is not
        # worth keeping is a judgement the log should not be making.
        audit.AuditLog(self.session).record(
            actor=actor,
            action=audit.FLAG_CHANGED,
            subject_type="feature_flag",
            subject_id=flag.id,
            subject_label=flag.key,
            context={"before": before, "after": after},
            request_id=request_id,
        )
        return flag

    async def create(
        self, actor: User, key: str, description: str, *, request_id: str | None = None
    ) -> FeatureFlag:
        """Register a flag. Off, at full rollout, until somebody turns it on."""
        if not KEY_PATTERN.match(key):
            raise ValidationError(
                "A flag key looks like 'concierge.voice' - lowercase, dot separated.",
                code="INVALID_FLAG_KEY",
            )
        if not description.strip():
            raise ValidationError(
                "Describe what the flag turns on.", code="DESCRIPTION_REQUIRED"
            )

        existing = await self.session.execute(
            select(FeatureFlag).where(FeatureFlag.key == key)
        )
        if existing.scalars().first() is not None:
            raise ValidationError("That flag already exists.", code="FLAG_EXISTS")

        flag = FeatureFlag(
            key=key,
            description=description.strip()[:MAX_DESCRIPTION],
            # Created off. A flag that arrives switched on has shipped a feature
            # by being created, which is the opposite of the point.
            enabled=False,
            rollout_percentage=100,
            updated_by_user_id=actor.id,
        )
        self.session.add(flag)
        await self.session.flush()

        audit.AuditLog(self.session).record(
            actor=actor,
            action=audit.FLAG_CHANGED,
            subject_type="feature_flag",
            subject_id=flag.id,
            subject_label=flag.key,
            context={"created": True, "after": {"enabled": False, "rolloutPercentage": 100}},
            request_id=request_id,
        )
        return flag

    async def _get(self, key: str) -> FeatureFlag:
        result = await self.session.execute(select(FeatureFlag).where(FeatureFlag.key == key))
        flag = result.scalars().first()
        if flag is None:
            raise NotFoundError(f"No flag called '{key}'.", code="FLAG_NOT_FOUND")
        return flag


def _state(flag: FeatureFlag) -> FlagState:
    return FlagState(
        key=flag.key,
        description=flag.description,
        enabled=flag.enabled,
        rollout_percentage=flag.rollout_percentage,
        updated_at=getattr(flag, "updated_at", None),
    )
