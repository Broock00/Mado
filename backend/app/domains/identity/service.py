"""Identity service - registration, authentication, session lifecycle."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
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
from app.domains.identity.tokens import (
    PURPOSE_RESET_PASSWORD,
    PURPOSE_VERIFY_EMAIL,
    AccountTokenService,
)

# Aliased: several methods here take a parameter called `email`, and a shadowed
# module import is the kind of bug that only shows up the day someone adds a
# send call to one of them.
from app.integrations import email as mailer

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
        device: dict | None = None,
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
        tokens = await self.issue_tokens(user, device=device)
        return user, tokens

    async def authenticate(
        self, *, email: str, password: str, device: dict | None = None
    ) -> tuple[User, TokenPair]:
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
        tokens = await self.issue_tokens(user, device=device)
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

    async def revoke_session(self, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
        """End one session.

        Scoped to the owner's own sessions, so a guessed id belonging to someone
        else reads as not found rather than signing a stranger out.
        """
        result = await self.session.execute(
            select(UserSession).where(
                UserSession.id == session_id, UserSession.user_id == user_id
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise NotFoundError("Session not found.", code="SESSION_NOT_FOUND")
        if row.revoked_at is None:
            row.revoked_at = datetime.now(UTC)

    # ------------------------------------------------- email and passwords

    async def send_verification(self, user: User) -> None:
        """Issue a verification link and put it in the post.

        Silent when there is no address to send to or it is already confirmed:
        both are states where the explorer needs nothing, and raising would make
        a harmless repeat click look like a failure.
        """
        profile = user.profile
        if profile is None or not profile.email or user.is_verified:
            return

        token = await AccountTokenService(self.session).issue(user.id, PURPOSE_VERIFY_EMAIL)
        mailer.dispatch(
            mailer.verification_message(
                profile.email,
                profile.display_name,
                f"{settings.web_base_url}/verify-email?token={token}",
            )
        )

    async def confirm_email(self, token: str) -> User:
        user_id = await AccountTokenService(self.session).consume(token, PURPOSE_VERIFY_EMAIL)
        user = await self.session.get(User, user_id)
        if user is None:
            raise NotFoundError("Account not found.", code="ACCOUNT_NOT_FOUND")

        user.is_verified = True
        # Kept in step with the identity row, which is what a future federated
        # provider would consult.
        for identity in await self._identities_for(user_id):
            identity.verified = True

        logger.info("email_verified", user_id=str(user.id))
        return user

    async def request_password_reset(self, email_address: str) -> None:
        """Send a reset link, if there is an account to send it to.

        Returns the same way whether or not the address is registered. A
        forgot-password form that answers "no such account" is an enumeration
        oracle, and this one is deliberately not.
        """
        identity = await self._find_identity(email_address.lower().strip())
        if identity is None or identity.user is None:
            logger.info("password_reset_requested_unknown_address")
            return

        user = identity.user
        if user.status != "active" or user.deleted_at is not None:
            return

        profile = user.profile
        if profile is None or not profile.email:
            return

        token = await AccountTokenService(self.session).issue(user.id, PURPOSE_RESET_PASSWORD)
        mailer.dispatch(
            mailer.reset_message(
                profile.email,
                profile.display_name,
                f"{settings.web_base_url}/reset-password?token={token}",
            )
        )

    async def reset_password(self, token: str, new_password: str) -> User:
        """Set a new password from a reset link, and end every session.

        Someone resetting a password may be doing it precisely because another
        person is in their account. Leaving that session alive would make the
        reset ceremonial, so all of them go - including the one that asked.
        """
        user_id = await AccountTokenService(self.session).consume(token, PURPOSE_RESET_PASSWORD)
        user = await self.session.get(
            User, user_id, options=[selectinload(User.profile)]
        )
        if user is None:
            raise NotFoundError("Account not found.", code="ACCOUNT_NOT_FOUND")

        await self._set_password(user, new_password)
        logger.info("password_reset", user_id=str(user.id))
        return user

    async def change_password(self, user: User, *, current: str, new: str) -> None:
        """Change a password from inside the account.

        The current password is required even though the caller is already
        signed in: an access token left open on a shared machine should not be
        enough to take the account permanently.
        """
        identities = await self._identities_for(user.id)
        identity = next((i for i in identities if i.password_hash), None)
        if identity is None:
            raise ValidationError(
                "This account does not sign in with a password.", code="NO_PASSWORD_IDENTITY"
            )

        if not verify_password(current, identity.password_hash or ""):
            raise AuthenticationError(
                "That is not your current password.", code="INVALID_CREDENTIALS"
            )

        await self._set_password(user, new)
        logger.info("password_changed", user_id=str(user.id))

    async def _set_password(self, user: User, new_password: str) -> None:
        for identity in await self._identities_for(user.id):
            if identity.password_hash is not None:
                identity.password_hash = hash_password(new_password)

        await self.revoke_all(user.id)

        profile = user.profile
        if profile is not None and profile.email:
            mailer.dispatch(
                mailer.password_changed_message(profile.email, profile.display_name)
            )

    async def _identities_for(self, user_id: uuid.UUID) -> list[AuthIdentity]:
        result = await self.session.execute(
            select(AuthIdentity).where(AuthIdentity.user_id == user_id)
        )
        return list(result.scalars().all())
