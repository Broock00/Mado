"""Shared FastAPI dependencies.

Spec 10.01.01 requires that explorers can browse anonymously and are only asked to
authenticate when it unlocks real value. Two dependencies express that split:

* :func:`current_user`          - authentication required
* :func:`optional_current_user` - personalize if signed in, still serve if not
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_session
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.core.security import decode_access_token
from app.domains.identity.models import User

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


async def optional_current_user(
    session: SessionDep,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> User | None:
    """Resolve the caller if a valid token is present, otherwise return None.

    A malformed or expired token is treated as "anonymous" rather than an error, so
    a stale token in a long-open browser tab degrades to public browsing instead of
    a wall of 401s on the discovery feed.
    """
    token = _extract_bearer(authorization)
    if not token:
        return None
    try:
        payload = decode_access_token(token)
    except AuthenticationError:
        return None

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        return None

    user = await session.get(User, user_id)
    if user is None or user.deleted_at is not None:
        return None

    if user.status != "active":
        # Treated as anonymous on public paths - a suspended account browsing the
        # city is harmless, and their personalization should not follow them.
        #
        # The reason is recorded on the request so `current_user` can say what
        # actually happened. Without it a suspended explorer is told
        # "authentication required", tries signing in again, succeeds, and is
        # told the same thing - which is a confusing way to learn you have been
        # suspended.
        request.state.auth_refusal = "suspended"
        return None
    return user


async def current_user(
    request: Request,
    user: Annotated[User | None, Depends(optional_current_user)],
) -> User:
    """Require a signed-in, active explorer.

    Suspension is enforced in `optional_current_user`, which drops a suspended
    account to anonymous. This only translates that into an honest message: the
    account exists and the password is right, so "authentication required" would
    send them round a loop they cannot escape.
    """
    if user is not None:
        return user

    if getattr(request.state, "auth_refusal", None) == "suspended":
        raise PermissionDeniedError(
            "This account is suspended. Contact support if you think that is wrong.",
            code="ACCOUNT_SUSPENDED",
        )
    raise AuthenticationError()


CurrentUserDep = Annotated[User, Depends(current_user)]


async def verified_publisher(user: CurrentUserDep) -> User:
    """Require a confirmed email address before anything reaches the city.

    Applied to publishing, not to drafting - someone should be able to write
    while they wait for the email. And not to reading, saving or planning, which
    cost nobody anything if the account turns out to be disposable.

    An unverified address is an unlimited supply of publishing accounts, which
    makes it the cheapest spam vector a platform has. Behind a setting because
    the gate only means anything once mail actually sends: with the log sender
    it would lock out every publisher for no security gain at all.
    """
    if not get_settings().require_verified_email_to_publish:
        return user
    if user.is_verified:
        return user
    raise PermissionDeniedError(
        "Confirm your email address before publishing. We sent you a link when "
        "you signed up - ask for another from your settings.",
        code="EMAIL_NOT_VERIFIED",
    )


CurrentUser = CurrentUserDep
VerifiedPublisher = Annotated[User, Depends(verified_publisher)]
OptionalUser = Annotated[User | None, Depends(optional_current_user)]


def get_anonymous_id(request: Request) -> str | None:
    """Client-supplied stable id for pre-registration personalization.

    Lets an anonymous explorer keep a concierge thread and accumulate interaction
    signals that migrate to the account on registration (spec 10.01.01 "Guest Mode").
    """
    return request.headers.get("X-Mado-Anonymous-Id")


AnonymousId = Annotated[str | None, Depends(get_anonymous_id)]
