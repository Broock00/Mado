"""Publishing endpoints.

Mounted under ``/api/v1/posts`` rather than ``/publishers/{id}/experiences``. The
URL is part of the product argument: an explorer posts, they do not administer an
organization's catalog. The publisher record exists underneath for accountability,
but nobody has to think about it.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, status

from app.api.deps import CurrentUser, SessionDep
from app.core.envelope import CollectionEnvelope, Envelope
from app.core.errors import RateLimitError
from app.domains.catalog.models import Experience
from app.domains.catalog.schemas import EventInstanceOut
from app.domains.catalog.serializers import to_detail
from app.domains.discovery.embedding_service import embed_experience
from app.domains.discovery.indexer import index_experience, remove_experience
from app.domains.publisher.schemas import (
    AddEventRequest,
    AddMediaRequest,
    CancelEventRequest,
    CreateExperienceRequest,
    CreateVenueRequest,
    OwnExperienceOut,
    PublisherOut,
    UpdateExperienceRequest,
    VenueOut,
)
from app.domains.publisher.service import PublishingService
from app.domains.trust.service import TrustService

router = APIRouter(prefix="/posts", tags=["publishing"])

# A new account posting more than this in a day is almost certainly not a person
# describing things they know about (spec BUSINESS-07 fraud prevention).
DAILY_PUBLISH_LIMIT = 20


def _own_view(experience: Experience) -> OwnExperienceOut:
    """Serialize an author's own post, including editorial state."""
    upcoming = sorted(
        (e for e in (experience.events or []) if e.status != "cancelled"),
        key=lambda e: e.start_time,
    )
    detail = to_detail(experience, upcoming_events=upcoming)
    return OwnExperienceOut(
        **detail.model_dump(by_alias=False),
        status=experience.status,
        moderation_status=experience.moderation_status,
        moderation_notes=experience.moderation_notes,
        report_count=experience.report_count or 0,
        readiness_problems=PublishingService.readiness_problems(experience),
    )


@router.get(
    "/me",
    response_model=Envelope[PublisherOut],
    summary="Get or create your publishing identity",
    description=(
        "Returns the personal publisher derived from your profile, creating it on "
        "first call. Explorers never fill in an organization form to post."
    ),
)
async def my_publisher(user: CurrentUser, session: SessionDep) -> Envelope[PublisherOut]:
    publisher = await PublishingService(session).personal_publisher(user)
    view = PublisherOut.model_validate(publisher)
    await session.commit()
    return Envelope(data=view)


@router.get(
    "",
    response_model=CollectionEnvelope[OwnExperienceOut],
    summary="List your posts",
)
async def list_my_posts(
    user: CurrentUser,
    session: SessionDep,
    post_status: str | None = Query(default=None, alias="status"),
) -> CollectionEnvelope[OwnExperienceOut]:
    experiences = await PublishingService(session).list_own_experiences(user, status=post_status)
    return CollectionEnvelope(data=[_own_view(e) for e in experiences])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[OwnExperienceOut],
    summary="Create a post",
    description="Creates a draft. Nothing is discoverable until you publish it.",
)
async def create_post(
    payload: CreateExperienceRequest, user: CurrentUser, session: SessionDep
) -> Envelope[OwnExperienceOut]:
    service = PublishingService(session)
    trust = TrustService(session)

    publisher = (
        await service.assert_can_publish_as(user, payload.publisher_id)
        if payload.publisher_id
        else await service.personal_publisher(user)
    )
    recent = await trust.recent_publish_count(publisher.id)
    if recent >= DAILY_PUBLISH_LIMIT:
        raise RateLimitError(
            "You have reached today's posting limit. Try again tomorrow.",
            code="PUBLISH_RATE_LIMIT",
            details={"limit": DAILY_PUBLISH_LIMIT, "windowHours": 24},
        )

    experience = await service.create_experience(
        user,
        title=payload.title,
        description=payload.description,
        city_slug=payload.city_slug,
        experience_type=payload.type,
        summary=payload.summary,
        category_slug=payload.category_slug,
        venue_id=payload.venue_id,
        tags=payload.tags,
        price_type=payload.price_type,
        price_amount=payload.price_amount,
        price_max=payload.price_max,
        currency=payload.currency,
        duration_minutes=payload.duration_minutes,
        is_indoor=payload.is_indoor,
        accessibility=payload.accessibility,
        publisher_id=publisher.id,
    )
    view = _own_view(experience)
    await session.commit()
    return Envelope(data=view)


@router.get(
    "/{experience_id}",
    response_model=Envelope[OwnExperienceOut],
    summary="Get one of your posts",
)
async def get_my_post(
    experience_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Envelope[OwnExperienceOut]:
    experience = await PublishingService(session).get_own_experience(user, experience_id)
    return Envelope(data=_own_view(experience))


@router.patch(
    "/{experience_id}",
    response_model=Envelope[OwnExperienceOut],
    summary="Edit a post",
)
async def update_post(
    experience_id: uuid.UUID,
    payload: UpdateExperienceRequest,
    user: CurrentUser,
    session: SessionDep,
) -> Envelope[OwnExperienceOut]:
    service = PublishingService(session)
    experience = await service.update_experience(
        user, experience_id, payload.model_dump(exclude_unset=True)
    )
    # Keep both retrievers in step with an edit that is already live.
    if experience.is_discoverable:
        await embed_experience(session, experience)
        await index_experience(experience)
    view = _own_view(experience)
    await session.commit()
    return Envelope(data=view)


@router.post(
    "/{experience_id}/publish",
    response_model=Envelope[OwnExperienceOut],
    summary="Publish a post",
    description=(
        "Makes the post discoverable after a readiness check and automated "
        "screening. High-risk submissions are withheld pending human review rather "
        "than rejected outright."
    ),
)
async def publish_post(
    experience_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Envelope[OwnExperienceOut]:
    service = PublishingService(session)
    trust = TrustService(session)

    experience = await service.publish(user, experience_id)
    await trust.screen_on_publish(experience)

    if experience.is_discoverable:
        # Both retrievers are refreshed together so a new post is reachable by
        # words and by meaning from the moment it goes live.
        await embed_experience(session, experience)
        await index_experience(experience)
    view = _own_view(experience)
    await session.commit()
    return Envelope(data=view)


@router.post(
    "/{experience_id}/unpublish",
    response_model=Envelope[OwnExperienceOut],
    summary="Take a post back to draft",
)
async def unpublish_post(
    experience_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Envelope[OwnExperienceOut]:
    experience = await PublishingService(session).unpublish(user, experience_id)
    await remove_experience(str(experience.id))
    view = _own_view(experience)
    await session.commit()
    return Envelope(data=view)


@router.post(
    "/{experience_id}/archive",
    response_model=Envelope[OwnExperienceOut],
    summary="Archive a post",
)
async def archive_post(
    experience_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Envelope[OwnExperienceOut]:
    experience = await PublishingService(session).archive(user, experience_id)
    await remove_experience(str(experience.id))
    view = _own_view(experience)
    await session.commit()
    return Envelope(data=view)


@router.post(
    "/{experience_id}/restore",
    response_model=Envelope[OwnExperienceOut],
    summary="Restore an archived post to draft",
)
async def restore_post(
    experience_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Envelope[OwnExperienceOut]:
    experience = await PublishingService(session).restore(user, experience_id)
    view = _own_view(experience)
    await session.commit()
    return Envelope(data=view)


# ------------------------------------------------------------------------ media


@router.post(
    "/{experience_id}/media",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[OwnExperienceOut],
    summary="Add an image",
)
async def add_media(
    experience_id: uuid.UUID,
    payload: AddMediaRequest,
    user: CurrentUser,
    session: SessionDep,
) -> Envelope[OwnExperienceOut]:
    service = PublishingService(session)
    await service.add_media(user, experience_id, url=payload.url, alt_text=payload.alt_text)
    experience = await service.get_own_experience(user, experience_id)
    view = _own_view(experience)
    await session.commit()
    return Envelope(data=view)


@router.delete(
    "/{experience_id}/media/{media_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an image",
)
async def remove_media(
    experience_id: uuid.UUID,
    media_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
) -> None:
    await PublishingService(session).remove_media(user, experience_id, media_id)
    await session.commit()


# ----------------------------------------------------------------------- dates


@router.post(
    "/{experience_id}/events",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[EventInstanceOut],
    summary="Add a date",
)
async def add_event(
    experience_id: uuid.UUID,
    payload: AddEventRequest,
    user: CurrentUser,
    session: SessionDep,
) -> Envelope[EventInstanceOut]:
    event = await PublishingService(session).add_event(
        user,
        experience_id,
        start_time=payload.start_time,
        end_time=payload.end_time,
        capacity=payload.capacity,
    )
    view = EventInstanceOut.model_validate(event)
    await session.commit()
    return Envelope(data=view)


@router.post(
    "/{experience_id}/events/{event_id}/cancel",
    response_model=Envelope[EventInstanceOut],
    summary="Cancel a date",
    description=(
        "Cancels rather than deletes, so explorers who planned around it see that "
        "it was called off instead of finding it silently gone."
    ),
)
async def cancel_event(
    experience_id: uuid.UUID,
    event_id: uuid.UUID,
    payload: CancelEventRequest,
    user: CurrentUser,
    session: SessionDep,
) -> Envelope[EventInstanceOut]:
    event = await PublishingService(session).cancel_event(
        user, experience_id, event_id, reason=payload.reason
    )
    view = EventInstanceOut.model_validate(event)
    await session.commit()
    return Envelope(data=view)


@router.delete(
    "/{experience_id}/events/{event_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a date from a draft",
)
async def delete_event(
    experience_id: uuid.UUID,
    event_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
) -> None:
    await PublishingService(session).delete_event(user, experience_id, event_id)
    await session.commit()


# ---------------------------------------------------------------------- venues


@router.post(
    "/venues",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[VenueOut],
    summary="Add a location",
    description=(
        "Creates a venue to attach posts to. Explorers can add places that are not "
        "in the catalog yet, which is how small local venues get in at all."
    ),
)
async def create_venue(
    payload: CreateVenueRequest, user: CurrentUser, session: SessionDep
) -> Envelope[VenueOut]:
    venue = await PublishingService(session).create_venue(
        user,
        name=payload.name,
        address=payload.address,
        city_slug=payload.city_slug,
        latitude=payload.latitude,
        longitude=payload.longitude,
        neighborhood_id=payload.neighborhood_id,
        accessibility=payload.accessibility,
    )
    view = VenueOut.model_validate(venue)
    await session.commit()
    return Envelope(data=view)
