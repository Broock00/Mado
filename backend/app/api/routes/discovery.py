"""Discovery Canvas, search and recommendations (spec 55.07)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from pydantic import Field

from app.api.deps import AnonymousId, CurrentUser, OptionalUser, SessionDep
from app.core import rate_limit
from app.core.config import get_settings
from app.core.envelope import CollectionEnvelope, Envelope, clamp_limit
from app.core.errors import PermissionDeniedError, ValidationError
from app.domains.catalog import locate
from app.domains.catalog import repository as catalog_repo
from app.domains.catalog import suitability as suitability_vocab
from app.domains.catalog.repository import (
    RADIUS_STEPS_KM,
    RESOLVED_BY_CHOSEN,
    RESOLVED_BY_UNKNOWN,
    Area,
)
from app.domains.catalog.schemas import CamelModel, ExperienceSummary
from app.domains.discovery.service import DiscoveryService, build_context
from app.domains.explorer.learning import infer_preferences
from app.domains.explorer.service import ExplorerService
from app.integrations import weather

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
    radius_km: float | None = None
    # Where the query is about, as a point and a radius. This is the real
    # scoping key; `city` survives for an explicit choice and for the label.
    area: Area | None = None
    # Where the point came from, in words, for "near you in Brooklyn".
    place_label: str | None = None
    raining: bool = False
    # Normalised to the vocabulary at the boundary, so a typo never reaches a
    # query as a requirement nothing can satisfy - which would silently return
    # an empty feed and look like a city with nothing in it.
    requires: list[str] = Field(default_factory=list)
    prefers: list[str] = Field(default_factory=list)
    limit: int = Field(default=12, ge=1, le=50)


async def discovery_query(
    session: SessionDep,
    city: str | None = Query(default=None),
    latitude: float | None = Query(default=None, alias="lat"),
    longitude: float | None = Query(default=None, alias="lng"),
    radius_km: float | None = Query(default=None, alias="radiusKm", ge=0.2, le=200),
    country: str | None = Query(
        default=None,
        min_length=2,
        max_length=2,
        description=(
            "ISO country code, for when the explorer picked a whole country. "
            "Exact where a bounding box is not - one around Kenya covers four "
            "neighbours, and the one around the United States spans the globe."
        ),
    ),
    bbox: str | None = Query(
        default=None,
        description=(
            "south,west,north,east. How a place with real extent is searched - "
            "a country is not a circle. Sent by the client for a place it "
            "already resolved, so the geocoder is not asked twice."
        ),
    ),
    place: str | None = Query(
        default=None,
        description=(
            "Anywhere by name - a neighbourhood, a street, a city. Resolved "
            "through a geocoding service, so it need not exist in Mado."
        ),
    ),
    raining: bool = Query(
        default=False,
        description=(
            "Observed weather from the client, for right now. The server fetches "
            "its own forecast for anything further ahead; this stays because an "
            "observation of the current moment beats a prediction of it."
        ),
    ),
    requires: list[str] | None = Query(
        default=None,
        description=(
            "Suitability claims a listing must have made to be returned - "
            "'vegan', 'step_free_access', 'childrens_play_area'. Listings that "
            "have claimed nothing are excluded, because unverified is not a "
            "'maybe' for somebody who needs it."
        ),
    ),
    prefers: list[str] | None = Query(
        default=None,
        description="Suitability claims that sort results without excluding any.",
    ),
    limit: int = Query(default=12, ge=1, le=50),
) -> DiscoveryQuery:
    """Work out where this request is about.

    Three ways, in order of how explicit they are. A named `place` wins: typing
    "Brooklyn" means Brooklyn even from Manhattan, and it is resolved through a
    geocoder rather than looked up in a table, so it works for a street or a
    neighbourhood nobody has ever added. Then an explicit `city`, which is a
    filter the explorer chose. Otherwise their own coordinates, which is the
    common case and should need no interaction at all.

    The result is a point and a radius. That is the real scoping key now - a
    curated city row is a label, not a precondition for asking what is nearby.
    """
    place_label: str | None = None
    area: Area | None = None

    if country:
        area = Area(latitude=latitude, longitude=longitude, country_code=country)

    if area is None and bbox and latitude is not None and longitude is not None:
        try:
            south, west, north, east = (float(part) for part in bbox.split(","))
        except (TypeError, ValueError):
            raise ValidationError(
                "A bounding box is four numbers: south,west,north,east.",
                code="INVALID_BOUNDING_BOX",
            ) from None
        if south > north or west > east:
            raise ValidationError(
                "That bounding box is inside out.", code="INVALID_BOUNDING_BOX"
            )
        area = Area(
            latitude=latitude,
            longitude=longitude,
            bounding_box=(south, west, north, east),
        )

    if area is None and place:
        # Resolved through `catalog.locate`, which the concierge also uses. The
        # rules about countries, boxes and radius-by-kind used to live here and
        # nowhere else, which is why the assistant could not answer a question
        # about a place the map could show perfectly well.
        near = (latitude, longitude) if latitude is not None and longitude is not None else None
        resolved = await locate.resolve_place(place, near=near, radius_km=radius_km)
        if resolved is not None:
            area = resolved.area
            place_label = resolved.label

    if area is None and city:
        area = Area(city_slug=city)

    if area is None and latitude is not None and longitude is not None:
        area = Area(
            latitude=latitude,
            longitude=longitude,
            radius_km=radius_km or RADIUS_STEPS_KM[0],
        )

    if place_label is not None:
        # A named place answers "where is this showing" by itself, and it may be
        # somewhere with no city row at all. Resolving the explorer's own city
        # here would label a Brooklyn search with the city they are sitting in.
        resolved_by = RESOLVED_BY_CHOSEN
        city = None
    else:
        # The city is still resolved, because the interface says where it is
        # showing and the seeded catalogue is organised that way. It no longer
        # decides what is searched.
        city, resolved_by = await catalog_repo.resolve_city_slug(
            session, city=city, latitude=latitude, longitude=longitude
        )
    return DiscoveryQuery(
        city=city,
        resolved_by=resolved_by,
        latitude=latitude,
        longitude=longitude,
        radius_km=radius_km,
        area=area,
        place_label=place_label,
        raining=raining,
        requires=suitability_vocab.normalise(requires),
        prefers=suitability_vocab.normalise(prefers),
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

    # Today's forecast for wherever the query is about, not wherever the explorer
    # is sitting: somebody in Addis browsing Brooklyn wants Brooklyn's weather.
    # None whenever no provider is configured or there are no coordinates, and
    # that stays None rather than becoming "fine".
    today = await weather.forecast_for(params.latitude, params.longitude, days=1)

    return build_context(
        latitude=params.latitude,
        longitude=params.longitude,
        preferences=preferences,
        saved_ids=saved_ids,
        is_raining=params.raining,
        weather=today.on(datetime.now(UTC).date()) if today else None,
        required_suitability=set(params.requires),
        preferred_suitability=set(params.prefers),
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
    # What to call where this is showing. Set when a place was searched for by
    # name, which may be nowhere the explorer has ever been and may have no
    # city row at all.
    area_label: str | None = None
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
            ctx, area=params.area, module_limit=params.limit
        )
        if params.area is not None
        else []
    )
    return Envelope(
        data=CanvasOut(
            city=params.city,
            area_label=params.place_label,
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
    if params.area is None:
        # Nowhere resolved: an empty list rather than another city's evening.
        return CollectionEnvelope(data=[])
    ctx = await _context(session, user, params)
    items = await DiscoveryService(session).happening_now(
        ctx, area=params.area, limit=params.limit
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
    if params.area is None:
        # Nowhere resolved: an empty list rather than another city's evening.
        return CollectionEnvelope(data=[])
    ctx = await _context(session, user, params)
    items = await DiscoveryService(session).tonight(ctx, area=params.area, limit=params.limit)
    return CollectionEnvelope(data=items)


@router.get(
    "/discover/weekend",
    response_model=CollectionEnvelope[ExperienceSummary],
    summary="On this weekend",
)
async def discover_weekend(
    session: SessionDep, user: OptionalUser, params: QueryDep
) -> CollectionEnvelope[ExperienceSummary]:
    if params.area is None:
        # Nowhere resolved: an empty list rather than another city's evening.
        return CollectionEnvelope(data=[])
    ctx = await _context(session, user, params)
    items = await DiscoveryService(session).weekend(ctx, area=params.area, limit=params.limit)
    return CollectionEnvelope(data=items)


@router.get(
    "/discover/trending",
    response_model=CollectionEnvelope[ExperienceSummary],
    summary="Trending in the city",
)
async def discover_trending(
    session: SessionDep, user: OptionalUser, params: QueryDep
) -> CollectionEnvelope[ExperienceSummary]:
    if params.area is None:
        # Nowhere resolved: an empty list rather than another city's evening.
        return CollectionEnvelope(data=[])
    ctx = await _context(session, user, params)
    items = await DiscoveryService(session).trending(ctx, area=params.area, limit=params.limit)
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
    items = await DiscoveryService(session).nearby(
        ctx, area=params.area, radius_km=radius_km, limit=params.limit
    )
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
    if params.area is None:
        # Recommendations are about a place. Without one they would be a list of
        # things somewhere the explorer is not.
        return CollectionEnvelope(data=[])
    ctx = await _context(session, user, params)
    items = await DiscoveryService(session).for_you(ctx, area=params.area, limit=params.limit)
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
