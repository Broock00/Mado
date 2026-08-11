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
from app.core.logging import get_logger
from app.core.security import decode_access_token
from app.domains.developer.keys import ANY_SCOPE
from app.domains.identity.models import User, UserProfile

logger = get_logger("mado.deps")

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


async def api_key_caller(
    session: SessionDep,
    request: Request,
    x_mado_api_key: Annotated[str | None, Header()] = None,
) -> User:
    """Authenticate a machine caller by API key (spec DEV-001).

    Its own header rather than `Authorization: Bearer`. The two credentials have
    different lifetimes, different revocation stories and different limits, and
    a single header that might contain either produces exactly one bug: a
    caller sends a key, the token path rejects it as malformed JWT, and the
    error says "sign in".

    The scopes the key carries are put on the request rather than checked here,
    because what a key may do depends on the endpoint it is calling. Routes
    declare that with :func:`caller_with_scope`.
    """
    from app.core.rate_limit import API_KEY_LIMIT, check
    from app.domains.developer.keys import ApiKeyService

    if not x_mado_api_key:
        raise AuthenticationError(
            "Send your key in the X-Mado-Api-Key header.", code="API_KEY_REQUIRED"
        )

    resolved = await ApiKeyService(session).authenticate(x_mado_api_key.strip())
    if resolved is None:
        # One message for unknown, revoked, expired and suspended-owner. The
        # distinctions are real; telling the caller which one applies helps
        # somebody probing with keys they were not given.
        raise AuthenticationError("That key is not valid.", code="API_KEY_INVALID")

    key, owner = resolved

    # `authenticate` stamped last_used_at, and dependencies run before the
    # handler - so the session holds that and nothing else, and committing it
    # here cannot commit anything a handler was building. Handlers own their own
    # transaction (see `get_session`), and a read-only endpoint should not have
    # to commit merely to record that a key was used.
    #
    # Committed even when the request goes on to fail: a rejected call still
    # proves the key is live, which is the question "when was this last used"
    # is asked in order to answer.
    if session.is_modified(key, include_collections=False):
        try:
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - a timestamp must not fail a request
            logger.warning("api_key_touch_failed", error=str(exc))
            await session.rollback()

    # Limited per key, not per account: an integration that runs away should
    # exhaust its own allowance rather than lock its owner out of the website.
    await check(f"key:{key.id}", API_KEY_LIMIT)

    request.state.api_key = key
    return owner


# Callable by any valid key, whatever it is scoped for. Tagged like the scoped
# dependencies below so the developer surface can see it: `whoami` is the first
# thing a partner calls to check their key works, and an SDK that omitted it
# would leave them debugging their setup against an endpoint they had to find
# in the documentation.
api_key_caller.mado_scope = ANY_SCOPE  # type: ignore[attr-defined]

ApiKeyUser = Annotated[User, Depends(api_key_caller)]


def caller_with_scope(scope: str):
    """A route usable by a signed-in person *or* by a key that carries `scope`.

    Both, on the same route, because the alternative is a parallel `/api/v1/x`
    for browsers and `/partner/x` for scripts - two implementations of one rule
    that drift apart, and the drift is always an authorisation bug.

    A session is unrestricted: somebody signed in is acting as themselves with
    everything that implies, and scopes exist to give a *script* less than its
    owner has. A key is checked, because a key issued for reading listings must
    not be able to publish one.

    Scope is checked here rather than in `api_key_caller` because a key is
    issued once and used against many endpoints: "who are you" has one answer,
    "may you do this" has one per route.
    """

    async def resolve(
        session: SessionDep,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_mado_api_key: Annotated[str | None, Header()] = None,
    ) -> User:
        if x_mado_api_key:
            user = await api_key_caller(session, request, x_mado_api_key)
            key = request.state.api_key
            if scope not in key.scopes:
                raise PermissionDeniedError(
                    f"This key does not have the '{scope}' scope.", code="SCOPE_REQUIRED"
                )
            return user

        session_user = await optional_current_user(session, request, authorization)
        return await current_user(request, session_user)

    # Left on the function so the scoped surface can be read back off the app
    # rather than written down a second time. The SDK generator and the OpenAPI
    # document both need "which endpoints can a key call, and with what scope",
    # and a hand-maintained list of that answers correctly right up until
    # somebody adds a route.
    resolve.mado_scope = scope  # type: ignore[attr-defined]
    return Annotated[User, Depends(resolve)]


# Named aliases rather than inline calls, so the set of scoped surfaces is
# visible in one place and a new route cannot quietly invent a fourth scope.
ExperienceReader = caller_with_scope("experiences:read")
ExperienceWriter = caller_with_scope("experiences:write")
ReservationReader = caller_with_scope("reservations:read")


async def verified_experience_writer(user: ExperienceWriter) -> User:
    """`experiences:write`, plus the confirmed-address gate on publishing.

    Composed rather than duplicated: an unverified address is an unlimited
    supply of publishing accounts whether the request arrives from a browser or
    from a script, and a key that skipped the check would be the easy way round
    it.
    """
    return await verified_publisher(user)


VerifiedExperienceWriter = Annotated[User, Depends(verified_experience_writer)]


async def current_language(
    request: Request,
    user: OptionalUser,
    session: SessionDep,
    accept_language: Annotated[str | None, Header()] = None,
) -> str:
    """The language to answer this request in (spec 11.07).

    A stated preference beats a browser header: somebody who chose Amharic in
    their settings means it on a borrowed laptop that asks for English.

    Put on the request as well as returned, so the middleware can set
    `Content-Language` without resolving it a second time - and so a caches or
    a proxy in front of this knows the response varied by language.
    """
    from app.core.language import resolve

    stated = None
    if user is not None:
        profile = await session.get(UserProfile, user.id)
        stated = getattr(profile, "language", None)

    language = resolve(stated, accept_language)
    request.state.language = language
    return language


Language = Annotated[str, Depends(current_language)]


def get_anonymous_id(request: Request) -> str | None:
    """Client-supplied stable id for pre-registration personalization.

    Lets an anonymous explorer keep a concierge thread and accumulate interaction
    signals that migrate to the account on registration (spec 10.01.01 "Guest Mode").
    """
    return request.headers.get("X-Mado-Anonymous-Id")


AnonymousId = Annotated[str | None, Depends(get_anonymous_id)]
