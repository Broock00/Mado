"""Discovery Canvas, search and recommendations (spec 55.07)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import Field

from app.api.deps import AnonymousId, OptionalUser, SessionDep
from app.core.config import get_settings
from app.core.envelope import CollectionEnvelope, Envelope, clamp_limit
from app.domains.catalog.schemas import CamelModel, ExperienceSummary
from app.domains.discovery.service import DiscoveryService, build_context
from app.domains.explorer.learning import infer_preferences
from app.domains.explorer.service import ExplorerService

router = APIRouter(tags=["discovery"])
settings = get_settings()


class DiscoveryQuery(CamelModel):
    """Context shared by every discovery surface.

    Location is optional throughout: spec DISC-002 requires a good cold start, so
    nothing here may become a precondition for useful results.
    """

    city: str = settings.default_city_slug
    latitude: float | None = None
    longitude: float | None = None
    raining: bool = False
    limit: int = Field(default=12, ge=1, le=50)


async def discovery_query(
    city: str = Query(default=settings.default_city_slug),
    latitude: float | None = Query(default=None, alias="lat"),
    longitude: float | None = Query(default=None, alias="lng"),
    raining: bool = Query(default=False, description="Current weather signal from the client."),
    limit: int = Query(default=12, ge=1, le=50),
) -> DiscoveryQuery:
    return DiscoveryQuery(
        city=city, latitude=latitude, longitude=longitude, raining=raining, limit=limit
    )


QueryDep = Annotated[DiscoveryQuery, Depends(discovery_query)]


async def _context(session, user, params: DiscoveryQuery):
    saved_ids = await ExplorerService(session).saved_experience_ids(user.id if user else None)
    preferences = user.profile.preferences if user and user.profile else {}
    privacy = user.profile.privacy if user and user.profile else None

    # Behaviour is read once per request and handed to the ranker. Signed-in only:
    # the events an anonymous explorer generates are keyed to a client id that is
    # not a durable identity, and profiling one would be personalization without
    # anyone having agreed to it.
    inferred = None
    if user is not None:
        inferred = await infer_preferences(session, user_id=user.id, privacy=privacy)

    return build_context(
        latitude=params.latitude,
        longitude=params.longitude,
        preferences=preferences,
        saved_ids=saved_ids,
        is_raining=params.raining,
        inferred=inferred,
    )


class FeedModuleOut(CamelModel):
    key: str
    title: str
    subtitle: str | None = None
    layout: str
    items: list[ExperienceSummary]


class CanvasOut(CamelModel):
    city: str
    modules: list[FeedModuleOut]


@router.get(
    "/discover",
    response_model=Envelope[CanvasOut],
    summary="The Discovery Canvas",
    description=(
        "Returns the personalized, context-aware modules that make up the explorer "
        "home surface. Modules with no eligible content are omitted rather than "
        "returned empty."
    ),
)
async def discovery_canvas(
    session: SessionDep, user: OptionalUser, params: QueryDep
) -> Envelope[CanvasOut]:
    ctx = await _context(session, user, params)
    modules = await DiscoveryService(session).build_canvas(
        ctx, city_slug=params.city, module_limit=params.limit
    )
    return Envelope(
        data=CanvasOut(
            city=params.city,
            modules=[
                FeedModuleOut(
                    key=m.key, title=m.title, subtitle=m.subtitle, layout=m.layout, items=m.items
                )
                for m in modules
            ],
        )
    )


@router.get(
    "/discover/now",
    response_model=CollectionEnvelope[ExperienceSummary],
    summary="Happening right now",
)
async def discover_now(
    session: SessionDep, user: OptionalUser, params: QueryDep
) -> CollectionEnvelope[ExperienceSummary]:
    ctx = await _context(session, user, params)
    items = await DiscoveryService(session).happening_now(
        ctx, city_slug=params.city, limit=params.limit
    )
    return CollectionEnvelope(data=items)


@router.get(
    "/discover/tonight",
    response_model=CollectionEnvelope[ExperienceSummary],
    summary="On tonight",
)
async def discover_tonight(
    session: SessionDep, user: OptionalUser, params: QueryDep
) -> CollectionEnvelope[ExperienceSummary]:
    ctx = await _context(session, user, params)
    items = await DiscoveryService(session).tonight(ctx, city_slug=params.city, limit=params.limit)
    return CollectionEnvelope(data=items)


@router.get(
    "/discover/weekend",
    response_model=CollectionEnvelope[ExperienceSummary],
    summary="On this weekend",
)
async def discover_weekend(
    session: SessionDep, user: OptionalUser, params: QueryDep
) -> CollectionEnvelope[ExperienceSummary]:
    ctx = await _context(session, user, params)
    items = await DiscoveryService(session).weekend(ctx, city_slug=params.city, limit=params.limit)
    return CollectionEnvelope(data=items)


@router.get(
    "/discover/trending",
    response_model=CollectionEnvelope[ExperienceSummary],
    summary="Trending in the city",
)
async def discover_trending(
    session: SessionDep, user: OptionalUser, params: QueryDep
) -> CollectionEnvelope[ExperienceSummary]:
    ctx = await _context(session, user, params)
    items = await DiscoveryService(session).trending(ctx, city_slug=params.city, limit=params.limit)
    return CollectionEnvelope(data=items)


@router.get(
    "/discover/nearby",
    response_model=CollectionEnvelope[ExperienceSummary],
    summary="Near the explorer",
)
async def discover_nearby(
    session: SessionDep,
    user: OptionalUser,
    params: QueryDep,
    radius_km: float = Query(default=3.0, ge=0.2, le=25, alias="radiusKm"),
) -> CollectionEnvelope[ExperienceSummary]:
    ctx = await _context(session, user, params)
    items = await DiscoveryService(session).nearby(ctx, radius_km=radius_km, limit=params.limit)
    return CollectionEnvelope(data=items)


class SearchMeta(CamelModel):
    query: str
    total: int
    degraded: bool = False
    # Whether vector retrieval contributed, so the client can distinguish
    # "matched your words" from "understood what you meant" - and so a silent
    # degradation to keyword-only is visible rather than invisible.
    semantic: bool = False


class SearchOut(CamelModel):
    results: list[ExperienceSummary]
    meta: SearchMeta


@router.get(
    "/search",
    response_model=Envelope[SearchOut],
    summary="Search experiences",
    description=(
        "Full-text and natural-language search. Candidate retrieval runs against the "
        "search index; ranking, personalization and explanations are applied by the "
        "platform. Falls back to the database if the index is unavailable."
    ),
)
async def search(
    session: SessionDep,
    user: OptionalUser,
    params: QueryDep,
    anonymous_id: AnonymousId,
    q: str = Query(min_length=1, max_length=300),
    category: list[str] | None = Query(default=None),
    free: bool = Query(default=False),
    experience_type: str | None = Query(default=None, alias="type"),
) -> Envelope[SearchOut]:
    ctx = await _context(session, user, params)
    explorer = ExplorerService(session)

    outcome = await DiscoveryService(session).search_experiences(
        q,
        ctx,
        city_slug=params.city,
        category_slugs=category,
        free_only=free,
        experience_type=experience_type,
        limit=clamp_limit(params.limit * 2),
    )

    await explorer.record_interaction(
        user_id=user.id if user else None,
        anonymous_id=anonymous_id,
        action="search",
        context={"query": q, "resultCount": len(outcome.items)},
    )
    await session.commit()

    return Envelope(
        data=SearchOut(
            results=outcome.items,
            meta=SearchMeta(
                query=q,
                total=outcome.total,
                degraded=outcome.degraded,
                semantic=outcome.semantic,
            ),
        )
    )


@router.get("/search/suggestions", response_model=CollectionEnvelope[dict], summary="Autocomplete")
async def suggestions(
    session: SessionDep,
    q: str = Query(min_length=1, max_length=120),
    limit: int = Query(default=8, ge=1, le=20),
) -> CollectionEnvelope[dict]:
    items = await DiscoveryService(session).suggest(q, limit=limit)
    return CollectionEnvelope(data=items)


@router.get(
    "/recommendations/for-you",
    response_model=CollectionEnvelope[ExperienceSummary],
    summary="Personalized recommendations",
)
async def for_you(
    session: SessionDep, user: OptionalUser, params: QueryDep
) -> CollectionEnvelope[ExperienceSummary]:
    ctx = await _context(session, user, params)
    items = await DiscoveryService(session).for_you(ctx, city_slug=params.city, limit=params.limit)
    return CollectionEnvelope(data=items)


@router.post(
    "/recommendations/{experience_id}/feedback",
    summary="Record recommendation feedback",
    description=(
        "Captures 'not interested' and similar signals. Spec 58.06 requires negative "
        "feedback to visibly influence later recommendations."
    ),
)
async def recommendation_feedback(
    experience_id: str,
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
    action: str = Query(default="not_interested"),
) -> Envelope[dict]:
    import uuid as _uuid

    try:
        entity_id = _uuid.UUID(experience_id)
    except ValueError:
        entity_id = None

    await ExplorerService(session).record_interaction(
        user_id=user.id if user else None,
        anonymous_id=anonymous_id,
        action=action,
        entity_type="experience",
        entity_id=entity_id,
        # Negative signals need weight to matter against a stream of passive views.
        weight=3 if action in {"not_interested", "dismiss"} else 1,
    )
    await session.commit()
    return Envelope(data={"recorded": True, "action": action})
