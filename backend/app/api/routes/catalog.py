"""Cities, categories, experiences and events (spec 55.04 / 55.06).

These are public reads: spec 55.01 s19 lists experience and event browsing as
publicly accessible, and spec 10.01.01 requires anonymous exploration. Signing in
changes personalization, not access.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query

from app.api.deps import AnonymousId, OptionalUser, SessionDep
from app.core.envelope import CollectionEnvelope, Envelope, clamp_limit
from app.core.errors import CityNotFoundError, EventNotFoundError, ExperienceNotFoundError
from app.domains.catalog import repository as catalog_repo
from app.domains.catalog.schemas import (
    CategoryOut,
    CityOut,
    EventOut,
    ExperienceDetail,
    ExperienceSummary,
    NeighborhoodOut,
)
from app.domains.catalog.serializers import to_detail, to_event
from app.domains.discovery.service import DiscoveryService, build_context
from app.domains.explorer.service import ExplorerService

router = APIRouter(tags=["catalog"])


@router.get("/cities", response_model=CollectionEnvelope[CityOut], summary="List cities")
async def list_cities(
    session: SessionDep,
    live_only: bool = Query(default=False, alias="liveOnly"),
) -> CollectionEnvelope[CityOut]:
    cities = await catalog_repo.list_cities(session, live_only=live_only)
    return CollectionEnvelope(data=[CityOut.model_validate(city) for city in cities])


@router.get("/cities/{slug}", response_model=Envelope[CityOut], summary="Get a city")
async def get_city(slug: str, session: SessionDep) -> Envelope[CityOut]:
    city = await catalog_repo.get_city_by_slug(session, slug)
    if city is None:
        raise CityNotFoundError()
    return Envelope(data=CityOut.model_validate(city))


@router.get(
    "/cities/{slug}/neighborhoods",
    response_model=CollectionEnvelope[NeighborhoodOut],
    summary="List a city's neighbourhoods",
)
async def list_neighborhoods(slug: str, session: SessionDep) -> CollectionEnvelope[NeighborhoodOut]:
    city = await catalog_repo.get_city_by_slug(session, slug)
    if city is None:
        raise CityNotFoundError()
    return CollectionEnvelope(data=[NeighborhoodOut.model_validate(n) for n in city.neighborhoods])


@router.get(
    "/categories", response_model=CollectionEnvelope[CategoryOut], summary="List categories"
)
async def list_categories(session: SessionDep) -> CollectionEnvelope[CategoryOut]:
    categories = await catalog_repo.list_categories(session)
    return CollectionEnvelope(data=[CategoryOut.model_validate(c) for c in categories])


@router.get(
    "/experiences",
    response_model=CollectionEnvelope[ExperienceSummary],
    summary="Browse experiences",
)
async def list_experiences(
    session: SessionDep,
    user: OptionalUser,
    city: str | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    tag: list[str] | None = Query(default=None),
    experience_type: str | None = Query(default=None, alias="type"),
    free: bool = Query(default=False),
    latitude: float | None = Query(default=None, alias="lat"),
    longitude: float | None = Query(default=None, alias="lng"),
    limit: int = Query(default=24, ge=1, le=100),
) -> CollectionEnvelope[ExperienceSummary]:
    explorer = ExplorerService(session)
    saved_ids = await explorer.saved_experience_ids(user.id if user else None)
    preferences = user.profile.preferences if user and user.profile else {}
    ctx = build_context(
        latitude=latitude, longitude=longitude, preferences=preferences, saved_ids=saved_ids
    )

    experiences = await catalog_repo.query_experiences(
        session,
        area=catalog_repo.Area(city_slug=city) if city else None,
        category_slugs=category,
        tag_slugs=tag,
        experience_type=experience_type,
        free_only=free,
        limit=80,
    )
    items = DiscoveryService(session).summarize(experiences, ctx, limit=clamp_limit(limit))
    return CollectionEnvelope(data=items)


@router.get(
    "/experiences/{experience_id}",
    response_model=Envelope[ExperienceDetail],
    summary="Get an experience",
)
async def get_experience(
    experience_id: uuid.UUID,
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
    latitude: float | None = Query(default=None, alias="lat"),
    longitude: float | None = Query(default=None, alias="lng"),
) -> Envelope[ExperienceDetail]:
    experience = await catalog_repo.get_experience(session, experience_id)
    if experience is None:
        raise ExperienceNotFoundError()

    explorer = ExplorerService(session)
    saved_ids = await explorer.saved_experience_ids(user.id if user else None)

    events = await catalog_repo.upcoming_events(
        session, experience_id=experience.id, starts_after=datetime.now(UTC), limit=20
    )

    distance_km = None
    if latitude is not None and longitude is not None and experience.venue is not None:
        from app.domains.discovery.ranking import haversine_km

        distance_km = round(
            haversine_km(
                latitude, longitude, experience.venue.latitude, experience.venue.longitude
            ),
            2,
        )

    await explorer.record_interaction(
        user_id=user.id if user else None,
        anonymous_id=anonymous_id,
        action="open_details",
        entity_type="experience",
        entity_id=experience.id,
        weight=2,
    )
    await session.commit()

    return Envelope(
        data=to_detail(
            experience,
            upcoming_events=events,
            is_saved=str(experience.id) in saved_ids,
            distance_km=distance_km,
        )
    )


@router.get(
    "/experiences/{experience_id}/events",
    response_model=CollectionEnvelope[EventOut],
    summary="List an experience's upcoming occurrences",
)
async def list_experience_events(
    experience_id: uuid.UUID,
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=100),
) -> CollectionEnvelope[EventOut]:
    experience = await catalog_repo.get_experience(session, experience_id)
    if experience is None:
        raise ExperienceNotFoundError()
    events = await catalog_repo.upcoming_events(
        session, experience_id=experience_id, starts_after=datetime.now(UTC), limit=limit
    )
    return CollectionEnvelope(data=[to_event(event) for event in events])


@router.get(
    "/experiences/{experience_id}/similar",
    response_model=CollectionEnvelope[ExperienceSummary],
    summary="Similar experiences",
)
async def similar_experiences(
    experience_id: uuid.UUID,
    session: SessionDep,
    user: OptionalUser,
    limit: int = Query(default=12, ge=1, le=50),
) -> CollectionEnvelope[ExperienceSummary]:
    experience = await catalog_repo.get_experience(session, experience_id)
    if experience is None:
        raise ExperienceNotFoundError()

    saved_ids = await ExplorerService(session).saved_experience_ids(user.id if user else None)
    ctx = build_context(
        preferences=user.profile.preferences if user and user.profile else {},
        saved_ids=saved_ids,
    )
    items = await DiscoveryService(session).similar_to(ctx, experience, limit=limit)
    return CollectionEnvelope(data=items)


@router.get("/events", response_model=CollectionEnvelope[EventOut], summary="Browse events")
async def list_events(
    session: SessionDep,
    city: str | None = Query(default=None),
    days: int = Query(default=14, ge=1, le=90, description="Look-ahead window in days."),
    limit: int = Query(default=50, ge=1, le=100),
) -> CollectionEnvelope[EventOut]:
    now = datetime.now(UTC)
    events = await catalog_repo.upcoming_events(
        session,
        area=catalog_repo.Area(city_slug=city) if city else None,
        starts_after=now,
        starts_before=now + timedelta(days=days),
        limit=limit,
    )
    return CollectionEnvelope(data=[to_event(event) for event in events])


@router.get("/events/{event_id}", response_model=Envelope[EventOut], summary="Get an event")
async def get_event(event_id: uuid.UUID, session: SessionDep) -> Envelope[EventOut]:
    event = await catalog_repo.get_event(session, event_id)
    if event is None:
        raise EventNotFoundError()
    return Envelope(data=to_event(event))
