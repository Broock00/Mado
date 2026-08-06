"""Explorer profile, preferences, privacy and saved items (spec 55.03)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, status

from app.api.deps import CurrentUser, SessionDep
from app.core.envelope import CollectionEnvelope, Envelope
from app.domains.explorer.service import ExplorerService
from app.domains.identity.schemas import (
    MeOut,
    PreferencesRequest,
    PrivacyRequest,
    ProfileOut,
    SavedItemOut,
    UpdateProfileRequest,
)
from app.domains.identity.service import IdentityService

router = APIRouter(prefix="/me", tags=["explorer"])


@router.get("", response_model=Envelope[MeOut], summary="Get the signed-in explorer")
async def get_me(user: CurrentUser) -> Envelope[MeOut]:
    return Envelope(data=MeOut.model_validate(user))


@router.patch("", response_model=Envelope[ProfileOut], summary="Update profile")
async def update_me(
    payload: UpdateProfileRequest, user: CurrentUser, session: SessionDep
) -> Envelope[ProfileOut]:
    profile = await IdentityService(session).get_profile(user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(profile, field, value)
    await session.commit()
    return Envelope(data=ProfileOut.model_validate(profile))


@router.get("/preferences", response_model=Envelope[dict], summary="Get preferences")
async def get_preferences(user: CurrentUser, session: SessionDep) -> Envelope[dict]:
    profile = await IdentityService(session).get_profile(user)
    return Envelope(data=profile.preferences or {})


@router.patch("/preferences", response_model=Envelope[dict], summary="Update preferences")
async def update_preferences(
    payload: PreferencesRequest, user: CurrentUser, session: SessionDep
) -> Envelope[dict]:
    profile = await IdentityService(session).get_profile(user)
    updated = await ExplorerService(session).update_preferences(
        profile, payload.model_dump(exclude_unset=True, by_alias=True)
    )
    await session.commit()
    return Envelope(data=updated.preferences)


@router.get("/privacy", response_model=Envelope[dict], summary="Get privacy controls")
async def get_privacy(user: CurrentUser, session: SessionDep) -> Envelope[dict]:
    profile = await IdentityService(session).get_profile(user)
    return Envelope(data=profile.privacy or {})


@router.patch("/privacy", response_model=Envelope[dict], summary="Update privacy controls")
async def update_privacy(
    payload: PrivacyRequest, user: CurrentUser, session: SessionDep
) -> Envelope[dict]:
    profile = await IdentityService(session).get_profile(user)
    updated = await ExplorerService(session).update_privacy(
        profile, payload.model_dump(exclude_unset=True, by_alias=True)
    )
    await session.commit()
    return Envelope(data=updated.privacy)


@router.get(
    "/saved",
    response_model=CollectionEnvelope[SavedItemOut],
    summary="List saved items",
)
async def list_saved(
    user: CurrentUser,
    session: SessionDep,
    entity_type: str | None = Query(default=None, alias="entityType"),
) -> CollectionEnvelope[SavedItemOut]:
    items = await ExplorerService(session).list_saved(user.id, entity_type=entity_type)
    return CollectionEnvelope(data=[SavedItemOut.model_validate(item) for item in items])


@router.post(
    "/saved/{entity_type}/{entity_id}",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[SavedItemOut],
    summary="Save an item",
)
async def save_item(
    entity_type: str, entity_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Envelope[SavedItemOut]:
    item = await ExplorerService(session).save(user, entity_type=entity_type, entity_id=entity_id)
    await session.commit()
    return Envelope(data=SavedItemOut.model_validate(item))


@router.delete(
    "/saved/{entity_type}/{entity_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a saved item",
)
async def unsave_item(
    entity_type: str, entity_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> None:
    await ExplorerService(session).unsave(user, entity_type=entity_type, entity_id=entity_id)
    await session.commit()


@router.post(
    "/export",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=Envelope[dict],
    summary="Request a personal data export",
)
async def export_data(user: CurrentUser) -> Envelope[dict]:
    """Spec 10.01.01 requires explorers be able to download their data.

    Returns 202 with a job handle per spec 55.01 s27; the worker that fulfils it is
    not part of this milestone, so the job stays queued rather than pretending to
    have produced a file.
    """
    return Envelope(
        data={
            "jobId": f"job_{uuid.uuid4().hex[:16]}",
            "status": "queued",
            "userId": str(user.id),
        }
    )
