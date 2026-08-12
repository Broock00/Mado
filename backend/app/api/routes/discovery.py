"""Discovery Canvas, search and recommendations (spec 55.07)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from pydantic import Field

from app.api.deps import AnonymousId, CurrentUser, OptionalUser, SessionDep
from app.core import rate_limit
from app.core.config import get_settings
from app.core.envelope import CollectionEnvelope, Envelope, clamp_limit
from app.core.errors import PermissionDeniedError
from app.domains.catalog import repository as catalog_repo
from app.domains.catalog.repository import RESOLVED_BY_UNKNOWN
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

    `city` is resolved rather than defaulted. It used to fall back to a single
    configured city, which meant an explorer opening the app anywhere on earth
    was told about that city as though they were standing in it. Now an explicit
    choice wins, coordinates decide when there is no choice, and when neither is
    available the answer is None - the interface asks instead of guessing.
    """

    city: str | None = None
    resolved_by: str = RESOLVED_BY_UNKNOWN
    latitude: float | None = None
    longitude: float | None = None
    raining: bool = False
    limit: int = Field(default=12, ge=1, le=50)


async def discovery_query(
    session: SessionDep,
    city: str | None = Query(default=None),
    latitude: float | None = Query(default=None, alias="lat"),
    longitude: float | None = Query(default=None, alias="lng"),
    raining: bool = Query(default=False, description="Current weather signal from the client."),
    limit: int = Query(default=12, ge=1, le=50),
) -> DiscoveryQuery:
    """Decide which city this request is about.

    An explicit `city` is a filter the explorer chose and always wins - somebody
    planning a trip to a city they are not in yet is an ordinary thing to do.
    Otherwise their coordinates decide, which is the common case and the one
    that should need no interaction at all.
    """
    city, resolved_by = await catalog_repo.resolve_city_slug(
        session, city=city, latitude=latitude, longitude=longitude
    )
    return DiscoveryQuery(
        city=city,
        resolved_by=resolved_by,
        latitude=latitude,
        longitude=longitude,
        raining=raining,
        limit=limit,
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
    """`city` is null when Mado does not cover where the explorer is.

    An empty canvas with `resolvedBy: "unknown"` is a different thing from a
    city with nothing on tonight, and the interface has to be able to tell them
    apart: one asks for a location or a city, the other says it is a quiet
    night.
    """

    city: str | None = None
    resolved_by: str = RESOLVED_BY_UNKNOWN
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
    modules = (
        await DiscoveryService(session).build_canvas(
            ctx, city_slug=params.city, module_limit=params.limit
        )
        if params.city
        else []
    )
    return Envelope(
        data=CanvasOut(
            city=params.city,
            resolved_by=params.resolved_by,
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
    if not params.city:
        # Nowhere resolved: an empty list rather than another city's evening.
        return CollectionEnvelope(data=[])
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
    if not params.city:
        # Nowhere resolved: an empty list rather than another city's evening.
        return CollectionEnvelope(data=[])
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
    if not params.city:
        # Nowhere resolved: an empty list rather than another city's evening.
        return CollectionEnvelope(data=[])
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
    if not params.city:
        # Nowhere resolved: an empty list rather than another city's evening.
        return CollectionEnvelope(data=[])
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
    if not params.city:
        # Recommendations are about a place. Without one they would be a list of
        # things somewhere the explorer is not.
        return CollectionEnvelope(data=[])
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


class LookOut(CamelModel):
    """What the model made of the photograph, shown to the explorer verbatim."""

    description: str
    terms: list[str]
    confidence: float
    """True when the subject could not be made out; results will be empty."""
    unclear: bool


class VisualSearchOut(CamelModel):
    look: LookOut
    results: list[ExperienceSummary]
    meta: SearchMeta


@router.post(
    "/search/visual",
    response_model=Envelope[VisualSearchOut],
    summary="Search by photograph",
    description=(
        "Answers what a photograph is *like*, not what it is. The model says "
        "what kind of place or thing it shows and the catalogue decides what "
        "exists - it is never allowed to name a venue, and any name it produces "
        "anyway is stripped before the search runs. A model that misidentifies "
        "a building sends somebody across the city to the wrong place.\n\n"
        "The photograph is not stored. It is decoded to check it is an image, "
        "re-encoded to drop its metadata, sent to the model and dropped.\n\n"
        "An unreadable photograph comes back with `unclear` set and no results, "
        "rather than a page of things chosen by a guess."
    ),
)
async def visual_search(
    session: SessionDep,
    user: CurrentUser,
    params: QueryDep,
    request: Request,
    image: UploadFile = File(description="A photograph. Not stored."),
) -> Envelope[VisualSearchOut]:
    from app.domains.discovery.visual import look_at
    from app.domains.trust.flags import flag_enabled

    if not await flag_enabled(session, "search.visual", user):
        raise PermissionDeniedError(
            "Searching by photograph is not switched on for you yet.",
            code="FEATURE_UNAVAILABLE",
        )

    # A model call per photograph, and a bigger one than a text turn, so it
    # shares the concierge's budget rather than the free search path.
    await rate_limit.check(
        rate_limit.identify(request, str(user.id)), rate_limit.CONCIERGE_LIMIT
    )

    look = await look_at(await image.read())
    if look.unclear:
        return Envelope(
            data=VisualSearchOut(
                look=LookOut(
                    description=look.description,
                    terms=look.terms,
                    confidence=look.confidence,
                    unclear=True,
                ),
                results=[],
                meta=SearchMeta(query=look.query, total=0, degraded=False, semantic=False),
            )
        )

    # The ordinary search path. A photograph is an input method, not a second
    # retrieval stack: whatever ranking and personalisation the text search
    # gained yesterday, this gets today.
    ctx = await _context(session, user, params)
    outcome = await DiscoveryService(session).search_experiences(
        look.query, ctx, city_slug=params.city, limit=clamp_limit(params.limit)
    )

    return Envelope(
        data=VisualSearchOut(
            look=LookOut(
                description=look.description,
                terms=look.terms,
                confidence=look.confidence,
                unclear=False,
            ),
            results=outcome.items,
            meta=SearchMeta(
                query=look.query,
                total=outcome.total,
                degraded=outcome.degraded,
                semantic=outcome.semantic,
            ),
        )
    )
