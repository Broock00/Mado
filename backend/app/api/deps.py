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

from app.core.database import get_session
from app.core.errors import AuthenticationError
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
    if user is None or user.deleted_at is not None or user.status != "active":
        return None
    return user


async def current_user(
    user: Annotated[User | None, Depends(optional_current_user)],
) -> User:
    if user is None:
        raise AuthenticationError()
    return user


CurrentUser = Annotated[User, Depends(current_user)]
OptionalUser = Annotated[User | None, Depends(optional_current_user)]


def get_anonymous_id(request: Request) -> str | None:
    """Client-supplied stable id for pre-registration personalization.

    Lets an anonymous explorer keep a concierge thread and accumulate interaction
    signals that migrate to the account on registration (spec 10.01.01 "Guest Mode").
    """
    return request.headers.get("X-Mado-Anonymous-Id")


AnonymousId = Annotated[str | None, Depends(get_anonymous_id)]
