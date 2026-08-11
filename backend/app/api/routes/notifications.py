"""Notification inbox and preferences (spec NOT-001).

Delivery is in-app: a notification becomes visible here when it is due. Push and
email are transports that sit behind the same scheduling, so adding one changes
the delivery step and nothing an explorer sees.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Query, status
from pydantic import Field

from app.api.deps import CurrentUser, SessionDep
from app.core.envelope import Envelope
from app.core.errors import NotFoundError
from app.domains.catalog.schemas import CamelModel
from app.domains.explorer.notifications import (
    ALL_KINDS,
    DEFAULT_ON,
    NotificationService,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationOut(CamelModel):
    id: uuid.UUID
    kind: str
    title: str
    body: str | None = None
    link: str | None = None
    is_unread: bool
    delivered_at: datetime | None = None


class InboxOut(CamelModel):
    notifications: list[NotificationOut]
    unread: int


class PreferencesOut(CamelModel):
    # Every kind with its current state, rather than only the ones set. A client
    # cannot render a settings screen from a partial map without duplicating the
    # defaults, and two copies of a default is one too many.
    kinds: dict[str, bool]


class PreferencesRequest(CamelModel):
    kinds: dict[str, bool] = Field(default_factory=dict)


def _to_out(notification) -> NotificationOut:
    return NotificationOut(
        id=notification.id,
        kind=notification.kind,
        title=notification.title,
        body=notification.body,
        link=notification.link,
        is_unread=notification.is_unread,
        delivered_at=notification.delivered_at,
    )


@router.get("", response_model=Envelope[InboxOut], summary="Your notifications")
async def inbox(
    session: SessionDep,
    user: CurrentUser,
    unread_only: bool = Query(default=False, alias="unreadOnly"),
    limit: int = Query(default=50, ge=1, le=100),
) -> Envelope[InboxOut]:
    service = NotificationService(session)
    items = await service.inbox(user.id, limit=limit, unread_only=unread_only)
    return Envelope(
        data=InboxOut(
            notifications=[_to_out(n) for n in items],
            unread=await service.unread_count(user.id),
        )
    )


@router.post(
    "/{notification_id}/read",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark one as read",
)
async def mark_read(
    notification_id: uuid.UUID, session: SessionDep, user: CurrentUser
) -> None:
    if not await NotificationService(session).mark_read(user.id, notification_id):
        raise NotFoundError("Notification not found.", code="NOTIFICATION_NOT_FOUND")
    await session.commit()


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT, summary="Mark all as read")
async def mark_all_read(session: SessionDep, user: CurrentUser) -> None:
    await NotificationService(session).mark_all_read(user.id)
    await session.commit()


@router.get(
    "/preferences",
    response_model=Envelope[PreferencesOut],
    summary="Which notifications you receive",
)
async def get_preferences(user: CurrentUser) -> Envelope[PreferencesOut]:
    stored = ((user.profile.preferences if user.profile else None) or {}).get(
        "notifications"
    ) or {}
    return Envelope(
        data=PreferencesOut(
            kinds={kind: stored.get(kind, kind in DEFAULT_ON) for kind in ALL_KINDS}
        )
    )


@router.patch(
    "/preferences",
    response_model=Envelope[PreferencesOut],
    summary="Change which notifications you receive",
)
async def update_preferences(
    payload: PreferencesRequest, session: SessionDep, user: CurrentUser
) -> Envelope[PreferencesOut]:
    profile = user.profile
    preferences = dict(profile.preferences or {})
    stored = dict(preferences.get("notifications") or {})

    # Unknown kinds are ignored rather than stored. A client sending a typo
    # should not silently create a setting that nothing ever reads.
    for kind, wanted in payload.kinds.items():
        if kind in ALL_KINDS:
            stored[kind] = bool(wanted)

    preferences["notifications"] = stored
    profile.preferences = preferences
    await session.commit()

    return Envelope(
        data=PreferencesOut(
            kinds={kind: stored.get(kind, kind in DEFAULT_ON) for kind in ALL_KINDS}
        )
    )
