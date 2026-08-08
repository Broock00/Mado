"""Reservations (spec COM-001).

Holding a place, cancelling it, and - for the publisher - seeing who is coming.

No money changes hands here. Secure payments are COM-003 and not built; a
reservation is a promise to turn up and a place held in return.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Request, status
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, OptionalUser, SessionDep
from app.core import rate_limit
from app.core.envelope import CollectionEnvelope, Envelope
from app.core.errors import NotFoundError, PermissionDeniedError
from app.domains.catalog.models import EventInstance, Experience
from app.domains.catalog.schemas import CamelModel
from app.domains.explorer.reservations import (
    MAX_PARTY_SIZE,
    ReservationService,
    availability_of,
)
from app.domains.identity.models import UserProfile
from app.domains.publisher.models import Publisher

router = APIRouter(tags=["reservations"])


class AvailabilityOut(CamelModel):
    # available | limited | full | cancelled (spec 55.05 §33)
    status: str
    capacity: int | None = None
    remaining: int | None = None
    is_unlimited: bool
    can_reserve: bool


class ReservationOut(CamelModel):
    id: uuid.UUID
    event_instance_id: uuid.UUID
    experience_id: uuid.UUID
    experience_title: str
    starts_at: datetime
    party_size: int
    status: str
    note: str | None = None


class AttendeeOut(CamelModel):
    """One reservation, as the publisher sees it.

    Named, unlike anything in the analytics dashboards. The distinction is
    consent: an explorer who reserves a place has deliberately told this
    publisher they are coming and expects to be found on a list at the door.
    Nothing else about them is included.
    """

    reservation_id: uuid.UUID
    name: str
    party_size: int
    note: str | None = None
    reserved_at: datetime


class ReserveRequest(CamelModel):
    party_size: int = Field(default=1, ge=1, le=MAX_PARTY_SIZE)
    note: str | None = Field(default=None, max_length=500)


class PartySizeRequest(CamelModel):
    party_size: int = Field(ge=1, le=MAX_PARTY_SIZE)


@router.get(
    "/events/{occurrence_id}/availability",
    response_model=Envelope[AvailabilityOut],
    summary="How full a date is",
    description=(
        "Anyone may ask - somebody deciding whether to hurry needs this before "
        "they have an account. A listing with no capacity set reads as "
        "unlimited rather than inventing a number."
    ),
)
async def availability(
    occurrence_id: uuid.UUID, session: SessionDep, user: OptionalUser
) -> Envelope[AvailabilityOut]:
    occurrence = await session.get(EventInstance, occurrence_id)
    if occurrence is None:
        raise NotFoundError("That date does not exist.", code="OCCURRENCE_NOT_FOUND")

    state = availability_of(occurrence)
    return Envelope(
        data=AvailabilityOut(
            status=state.status,
            capacity=state.capacity,
            remaining=state.remaining,
            is_unlimited=state.is_unlimited,
            can_reserve=state.can_reserve,
        )
    )


@router.post(
    "/events/{occurrence_id}/reserve",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[ReservationOut],
    summary="Hold a place",
)
async def reserve(
    occurrence_id: uuid.UUID,
    payload: ReserveRequest,
    session: SessionDep,
    user: CurrentUser,
    request: Request,
) -> Envelope[ReservationOut]:
    await rate_limit.check(rate_limit.identify(request, str(user.id)), rate_limit.RESERVE_LIMIT)
    reservation = await ReservationService(session).reserve(
        user, occurrence_id, party_size=payload.party_size, note=payload.note
    )
    view = ReservationOut.model_validate(reservation)
    await session.commit()
    return Envelope(data=view)


@router.get(
    "/reservations",
    response_model=CollectionEnvelope[ReservationOut],
    summary="Places you are holding",
)
async def my_reservations(
    session: SessionDep, user: CurrentUser
) -> CollectionEnvelope[ReservationOut]:
    reservations = await ReservationService(session).mine(user)
    return CollectionEnvelope(data=[ReservationOut.model_validate(r) for r in reservations])


@router.patch(
    "/reservations/{reservation_id}",
    response_model=Envelope[ReservationOut],
    summary="Change how many people are coming",
)
async def change_party_size(
    reservation_id: uuid.UUID,
    payload: PartySizeRequest,
    session: SessionDep,
    user: CurrentUser,
) -> Envelope[ReservationOut]:
    reservation = await ReservationService(session).change_party_size(
        user, reservation_id, payload.party_size
    )
    view = ReservationOut.model_validate(reservation)
    await session.commit()
    return Envelope(data=view)


@router.delete(
    "/reservations/{reservation_id}",
    response_model=Envelope[ReservationOut],
    summary="Give the place back",
    description=(
        "The places return to the pool immediately. A held seat nobody will use "
        "is worse for the publisher than an empty one they knew about."
    ),
)
async def cancel(
    reservation_id: uuid.UUID, session: SessionDep, user: CurrentUser
) -> Envelope[ReservationOut]:
    reservation = await ReservationService(session).cancel(user, reservation_id)
    view = ReservationOut.model_validate(reservation)
    await session.commit()
    return Envelope(data=view)


@router.get(
    "/posts/events/{occurrence_id}/attendees",
    response_model=CollectionEnvelope[AttendeeOut],
    summary="Who is coming",
    description="The publisher of the listing only.",
)
async def attendees(
    occurrence_id: uuid.UUID, session: SessionDep, user: CurrentUser
) -> CollectionEnvelope[AttendeeOut]:
    occurrence = await session.get(
        EventInstance, occurrence_id, options=[selectinload(EventInstance.experience)]
    )
    if occurrence is None:
        raise NotFoundError("That date does not exist.", code="OCCURRENCE_NOT_FOUND")

    # Ownership, checked against the publisher rather than the listing: a
    # publisher is a person here, and the owner of the listing is the only one
    # entitled to a list of names.
    experience = await session.get(Experience, occurrence.experience_id)
    publisher = (
        await session.execute(select(Publisher).where(Publisher.id == experience.publisher_id))
    ).scalars().first()
    if publisher is None or publisher.owner_user_id != user.id:
        raise PermissionDeniedError(
            "Only the publisher of this listing can see who is coming.",
            code="NOT_THE_PUBLISHER",
        )

    reservations = await ReservationService(session).for_occurrence(occurrence_id)
    if not reservations:
        return CollectionEnvelope(data=[])

    names = {
        profile.user_id: profile.display_name
        for profile in (
            await session.execute(
                select(UserProfile).where(
                    UserProfile.user_id.in_({r.user_id for r in reservations})
                )
            )
        ).scalars()
    }

    return CollectionEnvelope(
        data=[
            AttendeeOut(
                reservation_id=r.id,
                name=names.get(r.user_id, "Someone"),
                party_size=r.party_size,
                note=r.note,
                reserved_at=r.created_at,
            )
            for r in reservations
        ]
    )
