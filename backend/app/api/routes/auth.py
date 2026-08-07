"""Authentication endpoints (spec 55.02)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, status

from app.api.deps import CurrentUser, SessionDep
from app.core import rate_limit
from app.core.envelope import CollectionEnvelope, Envelope
from app.domains.identity.schemas import (
    AuthResponse,
    ChangePasswordRequest,
    ConfirmTokenRequest,
    EmailRequest,
    LoginRequest,
    MeOut,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    SessionOut,
    TokenPair,
)
from app.domains.identity.service import IdentityService

router = APIRouter(prefix="/auth", tags=["authentication"])


def _device_from(request: Request) -> dict:
    """Minimal device fingerprint for the session list (spec 10.01.01)."""
    return {
        "userAgent": request.headers.get("user-agent", "")[:300],
        "platform": request.headers.get("x-mado-platform", "web"),
    }


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[AuthResponse],
    summary="Create an explorer account",
)
async def register(
    payload: RegisterRequest, session: SessionDep, request: Request
) -> Envelope[AuthResponse]:
    # Keyed to the client address: there is no account yet to key to.
    await rate_limit.check(rate_limit.identify(request), rate_limit.REGISTER_LIMIT)
    service = IdentityService(session)
    user, tokens = await service.register(
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        language=payload.language,
        # Recorded from the first session onward. Without it the session list
        # shows a column of blanks, which is exactly as useful as not having one
        # - the whole point is recognising a device you do not own.
        device=_device_from(request),
    )
    # Sent now rather than waiting to be asked. Confirming an address is the one
    # step people skip if it is optional, and an account that never confirms is
    # an account with no way back if the password is lost.
    await service.send_verification(user)
    # Commit before responding: the client may use these tokens immediately.
    view = AuthResponse(user=MeOut.model_validate(user), tokens=tokens)
    await session.commit()
    return Envelope(data=view)


@router.post("/login", response_model=Envelope[AuthResponse], summary="Sign in")
async def login(
    payload: LoginRequest, session: SessionDep, request: Request
) -> Envelope[AuthResponse]:
    # The tightest limit in the system: this is the credential-stuffing surface.
    await rate_limit.check(rate_limit.identify(request), rate_limit.LOGIN_LIMIT)
    service = IdentityService(session)
    user, tokens = await service.authenticate(
        email=payload.email, password=payload.password, device=_device_from(request)
    )
    view = AuthResponse(user=MeOut.model_validate(user), tokens=tokens)
    await session.commit()
    return Envelope(data=view)


@router.post("/refresh", response_model=Envelope[TokenPair], summary="Rotate tokens")
async def refresh(payload: RefreshRequest, session: SessionDep) -> Envelope[TokenPair]:
    service = IdentityService(session)
    _, tokens = await service.refresh(payload.refresh_token)
    # The old token is revoked in this same transaction; it must be durable
    # before the new pair leaves the process.
    await session.commit()
    return Envelope(data=tokens)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the current refresh token",
)
async def logout(payload: RefreshRequest, session: SessionDep) -> None:
    await IdentityService(session).revoke(payload.refresh_token)
    await session.commit()


@router.post("/logout-all", summary="Revoke every session for the signed-in explorer")
async def logout_all(user: CurrentUser, session: SessionDep) -> Envelope[dict]:
    revoked = await IdentityService(session).revoke_all(user.id)
    await session.commit()
    return Envelope(data={"revokedSessions": revoked})


@router.get("/session", response_model=Envelope[MeOut], summary="Resolve the current session")
async def current_session(user: CurrentUser) -> Envelope[MeOut]:
    return Envelope(data=MeOut.model_validate(user))


# ------------------------------------------------- email and password flows


@router.post(
    "/verify-email/send",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send yourself a confirmation link",
)
async def send_verification(
    user: CurrentUser, session: SessionDep, request: Request
) -> Envelope[dict]:
    await rate_limit.check(
        rate_limit.identify(request, str(user.id)), rate_limit.EMAIL_SEND_LIMIT
    )
    await IdentityService(session).send_verification(user)
    await session.commit()
    # Accepted, not sent: the mail relay is asked after the response goes out,
    # and claiming delivery we have not confirmed would be a lie.
    return Envelope(data={"sent": True})


@router.post(
    "/verify-email/confirm",
    response_model=Envelope[MeOut],
    summary="Confirm an email address",
)
async def confirm_email(
    payload: ConfirmTokenRequest, session: SessionDep, request: Request
) -> Envelope[MeOut]:
    await rate_limit.check(rate_limit.identify(request), rate_limit.TOKEN_CONFIRM_LIMIT)
    user = await IdentityService(session).confirm_email(payload.token)
    view = MeOut.model_validate(user)
    await session.commit()
    return Envelope(data=view)


@router.post(
    "/password/forgot",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask for a password reset link",
    description=(
        "Always answers the same way, whether or not the address is registered. "
        "Anything else would turn this into a way to find out who has an account."
    ),
)
async def forgot_password(
    payload: EmailRequest, session: SessionDep, request: Request
) -> Envelope[dict]:
    # Keyed on the address rather than the caller: the point is to protect the
    # inbox of whoever was typed in, and rotating IPs must not defeat that.
    await rate_limit.check(
        rate_limit.identify(request, payload.email.lower()), rate_limit.EMAIL_SEND_LIMIT
    )
    await IdentityService(session).request_password_reset(payload.email)
    await session.commit()
    return Envelope(data={"sent": True})


@router.post(
    "/password/reset",
    response_model=Envelope[AuthResponse],
    summary="Set a new password from a reset link",
    description=(
        "Signs every other device out. Someone resetting a password may be doing "
        "it because another person is in the account."
    ),
)
async def reset_password(
    payload: ResetPasswordRequest, session: SessionDep, request: Request
) -> Envelope[AuthResponse]:
    await rate_limit.check(rate_limit.identify(request), rate_limit.TOKEN_CONFIRM_LIMIT)
    service = IdentityService(session)
    user = await service.reset_password(payload.token, payload.password)
    # A fresh session for the device that completed the reset, so the explorer is
    # signed in rather than bounced to a login form seconds after proving who
    # they are. Every previously issued session was revoked a moment ago.
    tokens = await service.issue_tokens(user, device=_device_from(request))
    view = AuthResponse(user=MeOut.model_validate(user), tokens=tokens)
    await session.commit()
    return Envelope(data=view)


@router.post(
    "/password/change",
    response_model=Envelope[TokenPair],
    summary="Change your password",
)
async def change_password(
    payload: ChangePasswordRequest,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
) -> Envelope[TokenPair]:
    # Same limit as signing in: this endpoint takes the current password, so it
    # is a credential-guessing surface in exactly the way login is.
    await rate_limit.check(
        rate_limit.identify(request, str(user.id)), rate_limit.LOGIN_LIMIT
    )
    service = IdentityService(session)
    await service.change_password(
        user, current=payload.current_password, new=payload.password
    )
    tokens = await service.issue_tokens(user, device=_device_from(request))
    await session.commit()
    return Envelope(data=tokens)


# --------------------------------------------------------------- sessions


@router.get(
    "/sessions",
    response_model=CollectionEnvelope[SessionOut],
    summary="Devices signed in to this account",
    description=(
        "Spec SECURITY-01 s6: explorers should be able to review and terminate "
        "active sessions. Seeing an unfamiliar device is how most people find "
        "out their account has been taken."
    ),
)
async def list_sessions(
    user: CurrentUser, session: SessionDep, request: Request
) -> CollectionEnvelope[SessionOut]:
    rows = await IdentityService(session).list_sessions(user.id)

    # The caller holds a refresh token, not a session id, and asking for it back
    # on a GET would mean putting a credential in a query string. Matching on the
    # device instead marks the likely current row - a hint for the human reading
    # the list, never an authorization decision.
    device = _device_from(request)
    return CollectionEnvelope(
        data=[
            SessionOut(
                id=row.id,
                created_at=row.created_at,
                expires_at=row.expires_at,
                user_agent=row.device.get("userAgent"),
                platform=row.device.get("platform"),
                is_current=row.device.get("userAgent") == device["userAgent"],
            )
            for row in rows
        ]
    )


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out one device",
)
async def revoke_session(
    session_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> None:
    await IdentityService(session).revoke_session(user.id, session_id)
    await session.commit()
