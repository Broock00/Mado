"""Password hashing and token issuance.

Spec 10.01.01 requires Argon2-class hashing and short-lived access tokens paired
with rotatable refresh tokens. Refresh tokens are opaque and stored hashed, so a
database leak cannot be replayed against the API.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.core.config import get_settings
from app.core.errors import AuthenticationError

settings = get_settings()
_hasher = PasswordHasher()

ACCESS_TOKEN_TYPE = "access"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    return True


def needs_rehash(password_hash: str) -> bool:
    """Report whether a stored hash predates the current Argon2 parameters."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def create_access_token(user_id: uuid.UUID, *, roles: list[str] | None = None) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "type": ACCESS_TOKEN_TYPE,
        "roles": roles or ["explorer"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_ttl_minutes)).timestamp()),
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Session expired.", code="TOKEN_EXPIRED") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Invalid token.", code="TOKEN_INVALID") from exc

    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise AuthenticationError("Invalid token type.", code="TOKEN_INVALID")
    return payload


def generate_refresh_token() -> tuple[str, str]:
    """Return ``(plaintext, sha256_digest)``.

    Only the digest is persisted. The plaintext exists solely in the response body,
    so a stolen session row cannot be exchanged for an access token.
    """
    plaintext = secrets.token_urlsafe(48)
    return plaintext, hash_refresh_token(plaintext)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def refresh_token_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days)
