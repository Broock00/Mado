"""Publishing endpoints.

Mounted under ``/api/v1/posts`` rather than ``/publishers/{id}/experiences``. The
URL is part of the product argument: an explorer posts, they do not administer an
organization's catalog. The publisher record exists underneath for accountability,
but nobody has to think about it.
"""

from __future__ import annotations

import contextlib
import uuid

from fastapi import APIRouter, File, Form, Query, Request, Response, UploadFile, status
from pydantic import Field

from app.api.deps import (
    CurrentUser,
    ExperienceReader,
    ExperienceWriter,
    SessionDep,
    VerifiedExperienceWriter,
)
from app.core import rate_limit
from app.core.envelope import CollectionEnvelope, Envelope
from app.core.errors import (
    ConflictError,
    PermissionDeniedError,
    RateLimitError,
    ValidationError,
)
from app.domains.catalog import repository as catalog_repo
from app.domains.catalog.models import Experience
from app.domains.catalog.schemas import CamelModel, EventInstanceOut
from app.domains.catalog.serializers import to_detail
from app.domains.commerce import plan as commerce_plan
from app.domains.commerce.bookings import sold_for_experience
from app.domains.discovery.embedding_service import embed_experience
from app.domains.discovery.indexer import index_experience, remove_experience
from app.domains.publisher.assistant import ContentAssistant
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
from app.domains.trust.flags import flag_enabled
from app.domains.trust.service import TrustService
from app.integrations import media_storage
from app.integrations.geocoding import get_geocoder

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
    user: ExperienceReader,
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
    payload: CreateExperienceRequest,
    user: ExperienceWriter,
    session: SessionDep,
    request: Request,
) -> Envelope[OwnExperienceOut]:
    await rate_limit.check(rate_limit.identify(request, str(user.id)), rate_limit.DRAFT_LIMIT)
    service = PublishingService(session)
    trust = TrustService(session)

    # `publisher_for`, not `personal_publisher`: a business account publishes as
    # the business. This route resolves the publisher itself because the daily
    # limit below is counted per publisher, and then passes the id on - so it and
    # `create_experience` have to answer the question the same way. They did not,
    # and the service's answer lost: a business account's posts came out under
    # the owner's own name while every check passed.
    publisher = (
        await service.assert_can_publish_as(user, payload.publisher_id)
        if payload.publisher_id
        else await service.publisher_for(user)
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
        suitability=payload.suitability,
        external_ticket_url=payload.external_ticket_url,
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
    experience_id: uuid.UUID, user: ExperienceReader, session: SessionDep
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
    user: ExperienceWriter,
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
    experience_id: uuid.UUID,
    # Confirming an address is required here and nowhere else in this file:
    # drafting, editing and unpublishing all stay open to an unverified account,
    # because none of them put anything in front of an explorer.
    user: VerifiedExperienceWriter,
    session: SessionDep,
    request: Request,
) -> Envelope[OwnExperienceOut]:
    # Checked before any work: publishing writes to the catalogue, reindexes,
    # embeds and runs two screeners including a model call.
    await rate_limit.check(rate_limit.identify(request, str(user.id)), rate_limit.PUBLISH_LIMIT)
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
    experience_id: uuid.UUID, user: ExperienceWriter, session: SessionDep
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
    experience_id: uuid.UUID, user: ExperienceWriter, session: SessionDep
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
    experience_id: uuid.UUID, user: ExperienceWriter, session: SessionDep
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


@router.post(
    "/{experience_id}/media/upload",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[OwnExperienceOut],
    summary="Upload an image",
    description=(
        "Accepts a JPEG, PNG or WebP file. The image is decoded and re-encoded "
        "server-side, which validates it is genuinely an image, strips EXIF "
        "(including any GPS coordinates) and resizes it for serving."
    ),
)
async def upload_media(
    experience_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    file: UploadFile = File(...),
    alt_text: str | None = Form(default=None),
) -> Envelope[OwnExperienceOut]:
    await rate_limit.check(rate_limit.identify(request, str(user.id)), rate_limit.UPLOAD_LIMIT)

    service = PublishingService(session)
    # Ownership is checked before a byte is read: an upload endpoint that
    # processes the file first does the expensive work for anyone who asks.
    await service.get_own_experience(user, experience_id)

    # Bounded read. Without a cap the whole file lands in memory before any size
    # check could run, which makes the size check decorative.
    data = await file.read(media_storage.MAX_UPLOAD_BYTES + 1)
    if len(data) > media_storage.MAX_UPLOAD_BYTES:
        raise ValidationError(
            f"Images must be under {media_storage.MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
            code="UPLOAD_TOO_LARGE",
        )

    stored = media_storage.store(data, owner_id=user.id)
    await service.add_media(user, experience_id, url=stored.url, alt_text=alt_text)

    experience = await service.get_own_experience(user, experience_id)
    view = _own_view(experience)
    await session.commit()
    return Envelope(data=view)


@router.delete(
    "/{experience_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a post",
    description=(
        "Removes the listing from discovery, from search and from your own "
        "list. Refused once anybody holds a ticket for it - cancel the date "
        "instead, which tells them, rather than having the thing they paid for "
        "quietly stop existing."
    ),
)
async def delete_post(
    experience_id: uuid.UUID,
    user: ExperienceWriter,
    session: SessionDep,
) -> Response:
    service = PublishingService(session)
    experience = await service.get_own_experience(user, experience_id)

    # Asked of commerce rather than worked out here, so this route does not read
    # another domain's tables. Pending counts as well as paid: a held seat is
    # somebody's plan for the evening either way.
    held = await sold_for_experience(session, experience_id)
    if held:
        raise ConflictError(
            f"{held} {'ticket has' if held == 1 else 'tickets have'} been booked for this. "
            "Cancel the date instead - that tells whoever is holding them.",
            code="TICKETS_ALREADY_SOLD",
            details={"held": held},
        )

    await service.delete(user, experience_id)
    # Out of the index too. A listing that is gone from the database and still
    # in search is worse than either state on its own: it appears, and then
    # 404s the person who tapped it.
    with contextlib.suppress(Exception):
        await remove_experience(str(experience.id))
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
    user: ExperienceWriter,
    session: SessionDep,
) -> Envelope[EventInstanceOut]:
    event = await PublishingService(session).add_event(
        user,
        experience_id,
        start_time=payload.start_time,
        end_time=payload.end_time,
        capacity=payload.capacity,
    )
    # The new date inherits whatever the listing already sells. Composed here
    # rather than inside the publishing service, so the publisher domain does
    # not have to know commerce exists - the route is the layer allowed to know
    # about both. Without it, "define the tickets once" would hold only until
    # somebody added another night, and that night would go on sale empty.
    await commerce_plan.materialise_for(
        session, experience_id=experience_id, occurrence=event
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
    user: ExperienceWriter,
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


class GeocodeRequest(CamelModel):
    address: str = Field(min_length=3, max_length=300)
    city_slug: str


class GeocodeOut(CamelModel):
    latitude: float
    longitude: float
    # Shown back to the publisher so they can confirm the pin is where they meant.
    # This is the real safeguard: a confidence score cannot tell that someone typed
    # their country instead of their street, because the geocoder will happily find
    # a precise point for a poor query. A person looking at the resolved address can.
    formatted_address: str
    confidence: float
    provider: str


class LocateRequest(CamelModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class LocateOut(CamelModel):
    latitude: float
    longitude: float
    # What is at this point, in words. Purely a label: the coordinates are the
    # truth because a person put the pin there deliberately, and this only helps
    # them confirm it is the right there.
    label: str | None = None


@router.post(
    "/venues/locate",
    response_model=Envelope[LocateOut],
    summary="Describe a point on the map",
    description=(
        "Reverse geocodes a dropped pin so the publisher can see what is there. "
        "The coordinates are authoritative - this only labels them, and a failed "
        "lookup returns the point with no label rather than an error."
    ),
)
async def locate_point(
    payload: LocateRequest,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
) -> Envelope[LocateOut]:
    await rate_limit.check(rate_limit.identify(request, str(user.id)), rate_limit.GEOCODE_LIMIT)

    # Deliberately never fails the request. The publisher has already chosen this
    # point; being unable to name it is a missing label, not a missing location.
    label = None
    with contextlib.suppress(Exception):
        label = await get_geocoder().reverse(payload.latitude, payload.longitude)

    return Envelope(
        data=LocateOut(
            latitude=payload.latitude, longitude=payload.longitude, label=label
        )
    )


@router.post(
    "/venues/geocode",
    response_model=Envelope[GeocodeOut],
    summary="Resolve an address to coordinates",
    description=(
        "Turns a street address into coordinates so a publisher never has to know "
        "them. Always confirm the returned address with the publisher before "
        "saving - a precise-looking result can still be the wrong place."
    ),
)
async def geocode_address(
    payload: GeocodeRequest,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
) -> Envelope[GeocodeOut]:
    await rate_limit.check(rate_limit.identify(request, str(user.id)), rate_limit.GEOCODE_LIMIT)

    city = await catalog_repo.get_city_by_slug(session, payload.city_slug)
    if city is None:
        raise ValidationError("Unknown city.", code="UNKNOWN_CITY")

    result = await get_geocoder().geocode(
        payload.address, city=city.name, country=city.country
    )
    if result is None or not result.is_usable:
        raise ValidationError(
            "We could not place that address. Try adding a landmark or the neighbourhood.",
            code="ADDRESS_NOT_FOUND",
        )

    return Envelope(
        data=GeocodeOut(
            latitude=result.latitude,
            longitude=result.longitude,
            formatted_address=result.formatted_address,
            confidence=round(result.confidence, 2),
            provider=result.provider,
        )
    )


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
        facilities=payload.facilities,
        place_id=payload.place_id,
    )
    view = VenueOut.model_validate(venue)
    await session.commit()
    return Envelope(data=view)


# ------------------------------------------------------------- writing help


class AssistRequest(CamelModel):
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(min_length=1, max_length=8000)
    summary: str | None = Field(default=None, max_length=400)


class AssistResponse(CamelModel):
    # Every field is optional. A suggestion that failed the grounding check is
    # simply absent - the publisher is not told the assistant tried to invent a
    # price, because that is our problem rather than theirs.
    summary: str | None = None
    description: str | None = None
    category_slug: str | None = None
    tags: list[str] = []
    # Questions a reader would still have. The most useful part: the model is
    # far better at noticing an omission than at filling one in.
    missing: list[str] = []
    # False when there is no model configured. The client hides the control
    # rather than offering a button that quietly does nothing.
    available: bool = True


@router.post(
    "/assist",
    response_model=Envelope[AssistResponse],
    summary="Suggestions for a draft",
    description=(
        "Suggests a summary, a tightened description, a category and tags, and "
        "asks what a reader would still want to know. Nothing is applied - every "
        "suggestion comes back for you to accept or ignore.\n\n"
        "The assistant may not add facts. Suggestions are checked against your "
        "draft before being offered, and any that introduce a number or a price "
        "you did not write are discarded rather than shown.\n\n"
        "Behind the `publisher.assistant` feature flag (spec ADM-003), because "
        "it is the most expensive thing a publisher can press and turning it "
        "off should not need a deploy."
    ),
)
async def assist(
    payload: AssistRequest,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
) -> Envelope[AssistResponse]:
    # Checked on the server as well as hidden in the composer. The client hides
    # the control so nobody presses a button that will fail; this is what makes
    # the flag actually mean something, because a client that has not reloaded
    # since the flag changed will still try.
    if not await flag_enabled(session, "publisher.assistant", user):
        raise PermissionDeniedError(
            "The writing assistant is switched off at the moment.",
            code="FEATURE_UNAVAILABLE",
        )

    # A model call per press, so it shares the concierge's budget rather than
    # the free draft limit.
    await rate_limit.check(
        rate_limit.identify(request, str(user.id)), rate_limit.CONCIERGE_LIMIT
    )
    suggestions = await ContentAssistant(session).suggest(
        title=payload.title, description=payload.description, summary=payload.summary
    )
    return Envelope(
        data=AssistResponse(
            summary=suggestions.summary,
            description=suggestions.description,
            category_slug=suggestions.category_slug,
            tags=suggestions.tags,
            missing=suggestions.missing,
            available=suggestions.available,
        )
    )
