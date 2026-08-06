"""Identity domain models (spec 54.02).

The model deliberately separates three concerns that most codebases fuse:

* ``User``          - the durable principal and its lifecycle status
* ``AuthIdentity``  - one row per authentication method, so a person can hold an
                      email login and a Google login without duplicate accounts
* ``UserProfile``   - the presentational and preference surface

That split is what lets spec 10.01.01's requirement "support social sign-in
providers without requiring duplicate accounts" hold without schema surgery.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
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
    pass

SCHEMA = "identity"


class User(Base, UUIDPrimaryKey, Timestamps, SoftDelete):
    __tablename__ = "users"
    __table_args__ = {"schema": SCHEMA}

    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # A real column, not a key in the preferences JSONB. Preferences are written by
    # the explorer themselves; keeping a privilege flag there would mean one
    # careless schema change turns into privilege escalation. No API sets this -
    # it is granted out of band (see `python -m app.seed --make-moderator`).
    is_moderator: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    profile: Mapped[UserProfile] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )
    identities: Mapped[list[AuthIdentity]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AuthIdentity(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "auth_identities"
    __table_args__ = (
        # A provider account maps to at most one Mado user.
        UniqueConstraint("provider", "provider_id", name="uq_auth_identity_provider"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # email|google|apple|phone
    provider_id: Mapped[str] = mapped_column(String(320), nullable=False)
    # Populated only for the "email" provider; social identities carry no secret.
    password_hash: Mapped[str | None] = mapped_column(Text, default=None)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped[User] = relationship(back_populates="identities")


class UserProfile(Base, Timestamps):
    __tablename__ = "user_profiles"
    __table_args__ = {"schema": SCHEMA}

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), default=None, index=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, default=None)
    bio: Mapped[str | None] = mapped_column(Text, default=None)
    language: Mapped[str] = mapped_column(String(12), default="en", nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Africa/Addis_Ababa", nullable=False)
    home_city_slug: Mapped[str | None] = mapped_column(String(120), default=None)

    # The Living Explorer Profile (spec 10.01.02). Each attribute carries a value,
    # a confidence score and a source so personalization stays explainable and the
    # UI can distinguish stated preferences from inferred ones.
    preferences: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    privacy: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    user: Mapped[User] = relationship(back_populates="profile")


class UserSession(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_user_active", "user_id", "revoked_at"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE")
    )
    # SHA-256 of the refresh token; the plaintext never touches the database.
    refresh_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    device: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    user: Mapped[User] = relationship(back_populates="sessions")

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None
