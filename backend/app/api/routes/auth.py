"""Authentication endpoints (spec 55.02)."""

from __future__ import annotations

from fastapi import APIRouter, Request, status

from app.api.deps import CurrentUser, SessionDep
from app.core.envelope import Envelope
from app.domains.identity.schemas import (
    AuthResponse,
    LoginRequest,
    MeOut,
    RefreshRequest,
    RegisterRequest,
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
    service = IdentityService(session)
    user, tokens = await service.register(
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        language=payload.language,
    )
    # Commit before responding: the client may use these tokens immediately.
    await session.commit()
    return Envelope(data=AuthResponse(user=MeOut.model_validate(user), tokens=tokens))


@router.post("/login", response_model=Envelope[AuthResponse], summary="Sign in")
async def login(
    payload: LoginRequest, session: SessionDep, request: Request
) -> Envelope[AuthResponse]:
    service = IdentityService(session)
    user, tokens = await service.authenticate(email=payload.email, password=payload.password)
    await session.commit()
    return Envelope(data=AuthResponse(user=MeOut.model_validate(user), tokens=tokens))


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
