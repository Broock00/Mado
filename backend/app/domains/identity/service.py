"""Identity service - registration, authentication, session lifecycle."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.errors import AuthenticationError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    needs_rehash,
    refresh_token_expiry,
    verify_password,
)
from app.domains.identity.models import AuthIdentity, User, UserProfile, UserSession
from app.domains.identity.schemas import TokenPair

logger = get_logger("mado.identity")
settings = get_settings()

PROVIDER_EMAIL = "email"

DEFAULT_PRIVACY = {
    # Spec PRODUCT-00 principle 9: personalization is opt-in and revocable.
    "personalizationEnabled": True,
    "locationEnabled": False,
    "aiMemoryEnabled": True,
    "analyticsEnabled": True,
}


class IdentityService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _find_identity(self, email: str) -> AuthIdentity | None:
        result = await self.session.execute(
            select(AuthIdentity)
            .where(
                AuthIdentity.provider == PROVIDER_EMAIL,
                AuthIdentity.provider_id == email.lower(),
            )
            .options(selectinload(AuthIdentity.user).selectinload(User.profile))
        )
        return result.scalar_one_or_none()

    async def register(
        self,
        *,
        email: str,
        password: str,
        display_name: str,
        language: str = "en",
    ) -> tuple[User, TokenPair]:
        normalized = email.lower().strip()
        if await self._find_identity(normalized) is not None:
            # Deliberately explicit. Registration cannot hide that an address is
            # taken without breaking the flow, and the address is already known to
            # whoever submitted it.
            raise ConflictError(
                "An account already exists for this email.", code="EMAIL_ALREADY_REGISTERED"
            )

        user = User(status="active", is_verified=False)
        user.profile = UserProfile(
            display_name=display_name.strip(),
            email=normalized,
            language=language,
            preferences={},
            privacy=dict(DEFAULT_PRIVACY),
        )
        user.identities = [
            AuthIdentity(
                provider=PROVIDER_EMAIL,
                provider_id=normalized,
                password_hash=hash_password(password),
                verified=False,
            )
        ]
        self.session.add(user)
        await self.session.flush()

        logger.info("user_registered", user_id=str(user.id))
        tokens = await self.issue_tokens(user)
        return user, tokens

    async def authenticate(self, *, email: str, password: str) -> tuple[User, TokenPair]:
        identity = await self._find_identity(email.lower().strip())

        # Same error and comparable work regardless of whether the account exists,
        # so response timing does not enumerate registered addresses.
        if identity is None or identity.password_hash is None:
            hash_password(password)
            raise AuthenticationError("Invalid email or password.", code="INVALID_CREDENTIALS")

        if not verify_password(password, identity.password_hash):
            raise AuthenticationError("Invalid email or password.", code="INVALID_CREDENTIALS")

        user = identity.user
        if user.status != "active" or user.deleted_at is not None:
            raise AuthenticationError("This account is not active.", code="ACCOUNT_INACTIVE")

        # Transparently upgrade the stored hash when Argon2 parameters move on.
        if needs_rehash(identity.password_hash):
            identity.password_hash = hash_password(password)

        user.last_login_at = datetime.now(UTC)
        tokens = await self.issue_tokens(user)
        logger.info("user_authenticated", user_id=str(user.id))
        return user, tokens

    async def issue_tokens(self, user: User, *, device: dict | None = None) -> TokenPair:
        plaintext, digest = generate_refresh_token()
        self.session.add(
            UserSession(
                user_id=user.id,
                refresh_token_hash=digest,
                device=device or {},
                expires_at=refresh_token_expiry(),
            )
        )
        return TokenPair(
            access_token=create_access_token(user.id),
            refresh_token=plaintext,
            expires_in=settings.access_token_ttl_minutes * 60,
        )

    async def refresh(self, refresh_token: str) -> tuple[User, TokenPair]:
        """Exchange a refresh token, rotating it in the process.

        Rotation means a stolen token is usable at most once, and the theft becomes
        visible when the legitimate client's next refresh fails.
        """
        digest = hash_refresh_token(refresh_token)
        result = await self.session.execute(
            select(UserSession)
            .where(UserSession.refresh_token_hash == digest)
            .options(selectinload(UserSession.user).selectinload(User.profile))
        )
        session_row = result.scalar_one_or_none()

        if session_row is None or session_row.revoked_at is not None:
            raise AuthenticationError("Invalid refresh token.", code="REFRESH_TOKEN_INVALID")
        if session_row.expires_at <= datetime.now(UTC):
            raise AuthenticationError("Refresh token expired.", code="REFRESH_TOKEN_EXPIRED")

        user = session_row.user
        if user is None or user.status != "active" or user.deleted_at is not None:
            raise AuthenticationError("This account is not active.", code="ACCOUNT_INACTIVE")

        session_row.revoked_at = datetime.now(UTC)
        return user, await self.issue_tokens(user, device=session_row.device)

    async def revoke(self, refresh_token: str) -> None:
        digest = hash_refresh_token(refresh_token)
        result = await self.session.execute(
            select(UserSession).where(UserSession.refresh_token_hash == digest)
        )
        session_row = result.scalar_one_or_none()
        if session_row is not None and session_row.revoked_at is None:
            session_row.revoked_at = datetime.now(UTC)

    async def revoke_all(self, user_id: uuid.UUID) -> int:
        result = await self.session.execute(
            select(UserSession).where(
                UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
            )
        )
        sessions = list(result.scalars().all())
        now = datetime.now(UTC)
        for row in sessions:
            row.revoked_at = now
        return len(sessions)

    async def get_profile(self, user: User) -> UserProfile:
        if user.profile is None:
            raise NotFoundError("Profile not found.", code="PROFILE_NOT_FOUND")
        return user.profile

    async def list_sessions(self, user_id: uuid.UUID) -> list[UserSession]:
        result = await self.session.execute(
            select(UserSession)
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .order_by(UserSession.created_at.desc())
        )
        return list(result.scalars().all())
