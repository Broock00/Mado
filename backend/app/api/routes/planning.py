"""Itinerary planning endpoints (spec 10.01.04, Journey Planner).

Planning and saving are separate operations. Most plans are looked at once and
discarded - an explorer asks for an evening, does not like it, and asks again - so
``POST /plans`` computes without persisting and ``POST /itineraries`` keeps one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query, Request
from pydantic import Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import AnonymousId, OptionalUser, SessionDep
from app.core import rate_limit
from app.core.config import get_settings
from app.core.envelope import CollectionEnvelope, Envelope
from app.core.errors import BadRequestError
from app.domains.catalog import repository as catalog_repo
from app.domains.catalog.models import Experience
from app.domains.catalog.schemas import CamelModel
from app.domains.discovery.service import build_context
from app.domains.explorer.learning import infer_preferences
from app.domains.explorer.planning import PlanRequest
from app.domains.explorer.planning_service import PlanningService
from app.domains.explorer.service import ExplorerService
from app.integrations import routing

router = APIRouter(tags=["planning"])
settings = get_settings()

# A plan is for an outing, not a holiday. Longer requests are almost always a
# client sending a bad window rather than a genuine 30-hour evening.
MAX_WINDOW_HOURS = 24


class PlanRequestIn(CamelModel):
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    city: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    budget: float | None = Field(default=None, ge=0)
    max_stops: int = Field(default=4, ge=1, le=6)
    categories: list[str] = Field(default_factory=list)
    free_only: bool = False

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        """Reject naive datetimes rather than guessing a zone.

        A plan is a sequence of wall-clock promises. Assuming UTC for a client that
        meant local time produces an itinerary that is silently hours wrong, which
        is far worse than a 422.
        """
        if value is not None and value.tzinfo is None:
            raise ValueError("Timestamps must include a timezone offset.")
        return value


class StopOut(CamelModel):
    # Present on persisted itinerary stops so the builder can remove by id.
    # Absent on ephemeral PlannedStop replies from POST /plans.
    id: uuid.UUID | None = None
    experience_id: uuid.UUID
    event_instance_id: uuid.UUID | None = None
    title: str
    arrive_at: datetime
    depart_at: datetime
    dwell_minutes: int
    travel_minutes: int
    travel_km: float | None = None
    estimated_cost: float
    is_fixed_time: bool
    note: str | None = None
    # 0-based day within a multi-day trip; outings are always 0.
    day_index: int = 0


class PlanOut(CamelModel):
    # Where the plan ended up. Sent back because the request need not have said:
    # an explorer who shared their location and chose no city still deserves a
    # plan that knows what to call itself.
    city_slug: str | None = None
    stops: list[StopOut]
    total_cost: float
    currency: str = "ETB"
    total_travel_minutes: int
    rationale: str
    # Constraints the planner could not satisfy, stated rather than hidden.
    unmet: list[str] = Field(default_factory=list)


class ItineraryOut(CamelModel):
    id: uuid.UUID
    title: str
    city_slug: str | None = None
    starts_at: datetime
    ends_at: datetime
    estimated_cost: float | None = None
    currency: str
    total_travel_minutes: int
    rationale: str | None = None
    # "draft" while the explorer is building, "kept" once they have saved it.
    status: str = "kept"
    # From constraints — drives day tabs in the builder.
    kind: str = "outing"
    timezone: str | None = None
    stops: list[StopOut]


class SaveItineraryIn(PlanRequestIn):
    title: str = Field(min_length=1, max_length=200)


# ---------------------------------------- draft builder input/output models


class CreateDraftIn(CamelModel):
    title: str = Field(default="Untitled plan", min_length=1, max_length=200)
    city: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    budget: float | None = Field(default=None, ge=0)
    free_only: bool = False
    # IANA timezone for multi-day day boundaries (defaults server-side to UTC).
    timezone: str | None = Field(default=None, max_length=64)
    # Explicit trip vs outing. Omitted → inferred from local calendar span.
    kind: str | None = Field(default=None, pattern="^(outing|trip)$")

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Timestamps must include a timezone offset.")
        return value


class PatchDraftIn(CamelModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    city: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    budget: float | None = Field(default=None, ge=0)
    free_only: bool | None = None
    timezone: str | None = Field(default=None, max_length=64)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Timestamps must include a timezone offset.")
        return value


class AppendStopIn(CamelModel):
    experience_id: uuid.UUID
    event_instance_id: uuid.UUID | None = None
    # Which day to append to (trips). None → last day, or 0 for outings.
    day_index: int | None = Field(default=None, ge=0, le=13)


class StopSpecIn(CamelModel):
    """One stop in a full replacement of the stop list."""

    experience_id: uuid.UUID
    event_instance_id: uuid.UUID | None = None
    is_fixed_time: bool = False
    arrive_at: datetime | None = None
    depart_at: datetime | None = None
    note: str | None = Field(default=None, max_length=300)
    day_index: int = Field(default=0, ge=0, le=13)

    @field_validator("arrive_at", "depart_at")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Timestamps must include a timezone offset.")
        return value


class KeepDraftIn(CamelModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)


class FillGapIn(CamelModel):
    after_index: int = Field(ge=-1)
    gap_start: datetime
    gap_end: datetime
    day_index: int = Field(default=0, ge=0, le=13)

    @field_validator("gap_start", "gap_end")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Timestamps must include a timezone offset.")
        return value


# ---------------------------------------- analysis output models


class ConflictOut(CamelModel):
    kind: str
    stop_indices: list[int]
    message: str
    resolutions: list[str]


class FreeGapOut(CamelModel):
    after_index: int
    starts_at: datetime
    ends_at: datetime
    free_minutes: int


class AnalysisOut(CamelModel):
    conflicts: list[ConflictOut]
    gaps: list[FreeGapOut]
    total_cost: float
    total_travel_minutes: int
    budget_overrun: float


def _window(payload: PlanRequestIn) -> tuple[datetime, datetime]:
    """Resolve the requested window, defaulting to the next few hours."""
    now = datetime.now(UTC)
    start = payload.starts_at or now
    end = payload.ends_at or (start + timedelta(hours=5))

    if end <= start:
        raise BadRequestError(
            "The end of the window must be after its start.", code="INVALID_WINDOW"
        )
    if (end - start) > timedelta(hours=MAX_WINDOW_HOURS):
        raise BadRequestError(
            f"Plans cover at most {MAX_WINDOW_HOURS} hours.", code="WINDOW_TOO_LONG"
        )
    return start, end


async def _plan_inputs(
    session, user, anonymous_id: str | None, payload: PlanRequestIn, http_request: Request
):
    # A plan is a full retrieval plus a solve, so it is heavier than a page view.
    await rate_limit.check(
        rate_limit.identify(http_request, str(user.id) if user else None), rate_limit.PLAN_LIMIT
    )
    start, end = _window(payload)

    preferences = user.profile.preferences if user and user.profile else {}
    privacy = user.profile.privacy if user and user.profile else None
    saved_ids = await ExplorerService(session).saved_experience_ids(user.id if user else None)
    inferred = (
        await infer_preferences(session, user_id=user.id, privacy=privacy) if user else None
    )

    ctx = build_context(
        latitude=payload.latitude,
        longitude=payload.longitude,
        preferences=preferences,
        saved_ids=saved_ids,
        now=start,
        inferred=inferred,
    )
    city_slug, _ = await catalog_repo.resolve_city_slug(
        session,
        city=payload.city,
        latitude=payload.latitude,
        longitude=payload.longitude,
    )
    if city_slug is None:
        # Unlike a feed, a plan cannot degrade to nothing: it is a sequence of
        # places with travel between them, and without a city there is nowhere
        # to draw them from. Asking is the only honest option.
        raise BadRequestError(
            "Choose a city, or share your location, and I will plan from there.",
            code="CITY_REQUIRED",
        )

    request = PlanRequest(
        start=start,
        end=end,
        city_slug=city_slug,
        latitude=payload.latitude,
        longitude=payload.longitude,
        budget=payload.budget,
        max_stops=payload.max_stops,
        categories=payload.categories,
        free_only=payload.free_only,
    )
    return request, ctx


def _to_stop_out(stop) -> StopOut:
    """Works for both PlannedStop (has .experience) and ItineraryStop (has .experience_id)."""
    if hasattr(stop, "experience"):
        # PlannedStop from the in-memory planner — no row id yet.
        return StopOut(
            id=None,
            experience_id=stop.experience.id,
            event_instance_id=stop.event_instance_id,
            title=stop.experience.title,
            arrive_at=stop.arrive_at,
            depart_at=stop.depart_at,
            dwell_minutes=stop.dwell_minutes,
            travel_minutes=stop.travel_minutes,
            travel_km=stop.travel_km,
            estimated_cost=stop.estimated_cost,
            is_fixed_time=stop.is_fixed_time,
            note=stop.note,
            day_index=getattr(stop, "day_index", 0) or 0,
        )
    # ItineraryStop from the database
    return StopOut(
        id=stop.id,
        experience_id=stop.experience_id,
        event_instance_id=stop.event_instance_id,
        title=stop.title,
        arrive_at=stop.arrive_at,
        depart_at=stop.depart_at,
        dwell_minutes=int((stop.depart_at - stop.arrive_at).total_seconds() / 60),
        travel_minutes=stop.travel_minutes,
        travel_km=float(stop.travel_km) if stop.travel_km is not None else None,
        estimated_cost=float(stop.estimated_cost or 0),
        is_fixed_time=stop.is_fixed_time,
        note=stop.note,
        day_index=int(getattr(stop, "day_index", 0) or 0),
    )


@router.post(
    "/plans",
    response_model=Envelope[PlanOut],
    summary="Plan an outing",
    description=(
        "Builds a feasible, ordered itinerary for a time window. Scheduled events "
        "are treated as fixed points and everything else is fitted around them, "
        "allowing for travel time and a buffer at each stop. Nothing is saved."
    ),
)
async def create_plan(
    payload: PlanRequestIn,
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
    http_request: Request,
) -> Envelope[PlanOut]:
    request, ctx = await _plan_inputs(session, user, anonymous_id, payload, http_request)
    plan = await PlanningService(session).plan(request, ctx)
    return Envelope(
        data=PlanOut(
            city_slug=request.city_slug,
            stops=[_to_stop_out(stop) for stop in plan.stops],
            total_cost=plan.total_cost,
            total_travel_minutes=plan.total_travel_minutes,
            rationale=plan.rationale,
            unmet=plan.unmet,
        )
    )


@router.post(
    "/itineraries",
    response_model=Envelope[ItineraryOut],
    status_code=201,
    summary="Plan and save an itinerary",
)
async def save_itinerary(
    payload: SaveItineraryIn,
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
    http_request: Request,
) -> Envelope[ItineraryOut]:
    request, ctx = await _plan_inputs(session, user, anonymous_id, payload, http_request)
    service = PlanningService(session)
    plan = await service.plan(request, ctx)
    if plan.is_empty:
        raise BadRequestError(
            "Nothing fitted that window, so there is no itinerary to save.",
            code="EMPTY_PLAN",
        )

    itinerary = await service.save(
        plan,
        request,
        title=payload.title,
        user_id=user.id if user else None,
        anonymous_id=anonymous_id,
    )
    await session.commit()
    return Envelope(data=_to_itinerary_out(itinerary))


@router.get(
    "/itineraries",
    response_model=CollectionEnvelope[ItineraryOut],
    summary="List saved itineraries",
)
async def list_itineraries(
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
    limit: int = Query(default=20, ge=1, le=50),
) -> CollectionEnvelope[ItineraryOut]:
    itineraries = await PlanningService(session).list_for(
        user_id=user.id if user else None, anonymous_id=anonymous_id, limit=limit
    )
    return CollectionEnvelope(data=[_to_itinerary_out(i) for i in itineraries])


def _default_window() -> tuple[datetime, datetime]:
    """Return a sensible default window when none is specified: today 09:00–22:00."""
    import datetime as _dt
    from datetime import UTC

    now = _dt.datetime.now(UTC)
    start = now.replace(hour=9, minute=0, second=0, microsecond=0)
    end = now.replace(hour=22, minute=0, second=0, microsecond=0)
    if end <= now:
        start = start + timedelta(days=1)
        end = end + timedelta(days=1)
    return start, end


@router.post(
    "/itineraries/drafts",
    response_model=Envelope[ItineraryOut],
    status_code=201,
    summary="Create an empty draft itinerary",
    description=(
        "Creates a draft workspace the explorer can add stops to. Nothing is solved. "
        "Drafts do not appear in the kept plans list until explicitly kept."
    ),
)
async def create_draft(
    payload: CreateDraftIn,
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
) -> Envelope[ItineraryOut]:
    default_start, default_end = _default_window()
    starts_at = payload.starts_at or default_start
    ends_at = payload.ends_at or default_end
    if ends_at <= starts_at:
        raise BadRequestError(
            "The end of the window must be after its start.", code="INVALID_WINDOW"
        )

    city_slug, _ = await catalog_repo.resolve_city_slug(
        session,
        city=payload.city,
        latitude=payload.latitude,
        longitude=payload.longitude,
    )

    service = PlanningService(session)
    draft = await service.create_draft(
        title=payload.title,
        city_slug=city_slug,
        starts_at=starts_at,
        ends_at=ends_at,
        budget=payload.budget,
        free_only=payload.free_only,
        user_id=user.id if user else None,
        anonymous_id=anonymous_id,
        timezone=payload.timezone or "UTC",
        kind=payload.kind,
    )
    await session.commit()
    return Envelope(data=_to_itinerary_out(draft))


@router.get(
    "/itineraries/drafts",
    response_model=CollectionEnvelope[ItineraryOut],
    summary="List draft itineraries",
)
async def list_drafts(
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
    limit: int = Query(default=10, ge=1, le=20),
) -> CollectionEnvelope[ItineraryOut]:
    drafts = await PlanningService(session).list_drafts(
        user_id=user.id if user else None, anonymous_id=anonymous_id, limit=limit
    )
    return CollectionEnvelope(data=[_to_itinerary_out(d) for d in drafts])


@router.get(
    "/itineraries/{itinerary_id}",
    response_model=Envelope[ItineraryOut],
    summary="Get one itinerary",
)
async def get_itinerary(
    itinerary_id: uuid.UUID,
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
) -> Envelope[ItineraryOut]:
    itinerary = await PlanningService(session).get(
        itinerary_id, user_id=user.id if user else None, anonymous_id=anonymous_id
    )
    return Envelope(data=_to_itinerary_out(itinerary))


@router.delete(
    "/itineraries/{itinerary_id}",
    status_code=204,
    summary="Delete an itinerary",
)
async def delete_itinerary(
    itinerary_id: uuid.UUID,
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
) -> None:
    await PlanningService(session).delete(
        itinerary_id, user_id=user.id if user else None, anonymous_id=anonymous_id
    )
    await session.commit()


def _to_itinerary_out(itinerary) -> ItineraryOut:
    constraints = itinerary.constraints or {}
    kind = constraints.get("kind") or "outing"
    if kind not in ("outing", "trip"):
        kind = "outing"
    ordered = sorted(
        itinerary.stops,
        key=lambda s: (int(getattr(s, "day_index", 0) or 0), s.position),
    )
    return ItineraryOut(
        id=itinerary.id,
        title=itinerary.title,
        city_slug=itinerary.city_slug,
        starts_at=itinerary.starts_at,
        ends_at=itinerary.ends_at,
        estimated_cost=float(itinerary.estimated_cost)
        if itinerary.estimated_cost is not None
        else None,
        currency=itinerary.currency,
        total_travel_minutes=itinerary.total_travel_minutes,
        rationale=itinerary.rationale,
        status=getattr(itinerary, "status", "kept"),
        kind=kind,
        timezone=constraints.get("timezone"),
        stops=[_to_stop_out(stop) for stop in ordered],
    )


# ------------------------------------------------------------- draft mutations
# Static /itineraries/drafts routes are registered above get_itinerary so FastAPI
# does not treat "drafts" as an itinerary_id UUID (which returns 422).


@router.patch(
    "/itineraries/{itinerary_id}",
    response_model=Envelope[ItineraryOut],
    summary="Update a draft's metadata",
    description="Change the title, window, or constraints of a draft. Only drafts can be patched.",
)
async def patch_itinerary(
    itinerary_id: uuid.UUID,
    payload: PatchDraftIn,
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
) -> Envelope[ItineraryOut]:
    city_slug: str | None = None
    if payload.city:
        city_slug, _ = await catalog_repo.resolve_city_slug(session, city=payload.city)

    service = PlanningService(session)
    itinerary = await service.update_draft_meta(
        itinerary_id,
        title=payload.title,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        city_slug=city_slug,
        budget=payload.budget,
        free_only=payload.free_only,
        user_id=user.id if user else None,
        anonymous_id=anonymous_id,
    )
    await session.commit()
    return Envelope(data=_to_itinerary_out(itinerary))


@router.post(
    "/itineraries/{itinerary_id}/stops",
    response_model=Envelope[ItineraryOut],
    status_code=201,
    summary="Append a stop to a draft",
    description=(
        "Appends one stop to the end of a draft. Travel time and arrival are computed "
        "from the previous stop's coordinates. Times can be adjusted with PUT /stops."
    ),
)
async def append_stop(
    itinerary_id: uuid.UUID,
    payload: AppendStopIn,
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
) -> Envelope[ItineraryOut]:
    service = PlanningService(session)
    itinerary = await service.append_stop(
        itinerary_id,
        payload.experience_id,
        payload.event_instance_id,
        user_id=user.id if user else None,
        anonymous_id=anonymous_id,
        day_index=payload.day_index,
    )
    await session.commit()
    return Envelope(data=_to_itinerary_out(itinerary))


@router.put(
    "/itineraries/{itinerary_id}/stops",
    response_model=Envelope[ItineraryOut],
    summary="Replace the stop list",
    description=(
        "Replaces the entire ordered stop list. Use for reordering, removing, or bulk time edits. "
        "Provide arriveAt/departAt to keep your chosen times; omit them to "
        "auto-compute from travel estimates."
    ),
)
async def replace_stops(
    itinerary_id: uuid.UUID,
    stops: list[StopSpecIn],
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
) -> Envelope[ItineraryOut]:
    # Manual builder limit — higher than the AI solver's MAX_STOPS (6). Without
    # this, adding a 7th stop via append then reordering/removing fails with
    # TOO_MANY_STOPS even though append itself allowed it.
    from app.domains.explorer.planning import MAX_BUILDER_STOPS

    if len(stops) > MAX_BUILDER_STOPS:
        raise BadRequestError(
            f"A plan may have at most {MAX_BUILDER_STOPS} stops.", code="TOO_MANY_STOPS"
        )
    spec_dicts = [
        {
            "experienceId": str(s.experience_id),
            "eventInstanceId": str(s.event_instance_id) if s.event_instance_id else None,
            "isFixedTime": s.is_fixed_time,
            "arriveAt": s.arrive_at.isoformat() if s.arrive_at else None,
            "departAt": s.depart_at.isoformat() if s.depart_at else None,
            "note": s.note,
            "dayIndex": s.day_index,
        }
        for s in stops
    ]
    service = PlanningService(session)
    itinerary = await service.replace_stops(
        itinerary_id,
        spec_dicts,
        user_id=user.id if user else None,
        anonymous_id=anonymous_id,
    )
    await session.commit()
    return Envelope(data=_to_itinerary_out(itinerary))


@router.delete(
    "/itineraries/{itinerary_id}/stops/{stop_id}",
    response_model=Envelope[ItineraryOut],
    summary="Remove a stop from a draft",
)
async def remove_stop(
    itinerary_id: uuid.UUID,
    stop_id: uuid.UUID,
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
) -> Envelope[ItineraryOut]:
    service = PlanningService(session)
    itinerary = await service.remove_stop(
        itinerary_id,
        stop_id,
        user_id=user.id if user else None,
        anonymous_id=anonymous_id,
    )
    await session.commit()
    return Envelope(data=_to_itinerary_out(itinerary))


@router.post(
    "/itineraries/{itinerary_id}/check",
    response_model=Envelope[AnalysisOut],
    summary="Check a draft for conflicts and gaps",
    description=(
        "Validates the explorer's chosen stop order and times without changing anything. "
        "Returns structured conflicts (with resolution options) and free gaps."
    ),
)
async def check_itinerary(
    itinerary_id: uuid.UUID,
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
) -> Envelope[AnalysisOut]:
    service = PlanningService(session)
    analysis = await service.check(
        itinerary_id, user_id=user.id if user else None, anonymous_id=anonymous_id
    )
    return Envelope(
        data=AnalysisOut(
            conflicts=[
                ConflictOut(
                    kind=c.kind,
                    stop_indices=c.stop_indices,
                    message=c.message,
                    resolutions=c.resolutions,
                )
                for c in analysis.conflicts
            ],
            gaps=[
                FreeGapOut(
                    after_index=g.after_index,
                    starts_at=g.starts_at,
                    ends_at=g.ends_at,
                    free_minutes=g.free_minutes,
                )
                for g in analysis.gaps
            ],
            total_cost=analysis.total_cost,
            total_travel_minutes=analysis.total_travel_minutes,
            budget_overrun=analysis.budget_overrun,
        )
    )


@router.post(
    "/itineraries/{itinerary_id}/fill-gap",
    response_model=Envelope[PlanOut],
    summary="Propose stops for a free gap",
    description=(
        "Finds existing Mado experiences that fit in a free gap in the explorer's plan. "
        "Returns proposals only — nothing is changed until the explorer adds a stop."
    ),
)
async def fill_gap(
    itinerary_id: uuid.UUID,
    payload: FillGapIn,
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
    http_request: Request,
) -> Envelope[PlanOut]:
    await rate_limit.check(
        rate_limit.identify(http_request, str(user.id) if user else None), rate_limit.PLAN_LIMIT
    )
    preferences = user.profile.preferences if user and user.profile else {}
    from app.domains.explorer.service import ExplorerService

    saved_ids = await ExplorerService(session).saved_experience_ids(user.id if user else None)
    ctx = build_context(
        preferences=preferences,
        saved_ids=saved_ids,
        now=payload.gap_start,
    )
    service = PlanningService(session)
    proposal = await service.propose_fill_gap(
        itinerary_id,
        after_index=payload.after_index,
        gap_start=payload.gap_start,
        gap_end=payload.gap_end,
        ctx=ctx,
        user_id=user.id if user else None,
        anonymous_id=anonymous_id,
    )
    return Envelope(
        data=PlanOut(
            stops=[_to_stop_out(s) for s in proposal.stops],
            total_cost=proposal.total_cost,
            total_travel_minutes=proposal.total_travel_minutes,
            rationale=proposal.rationale,
            unmet=proposal.unmet,
        )
    )


@router.post(
    "/itineraries/{itinerary_id}/optimize",
    response_model=Envelope[PlanOut],
    summary="Propose an optimized stop order",
    description=(
        "Analyzes the explorer's current order and proposes a more efficient sequence "
        "using 2-opt + retiming. Returns the proposal only — apply it with "
        "PUT /itineraries/{id}/stops if accepted."
    ),
)
async def optimize_itinerary(
    itinerary_id: uuid.UUID,
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
    http_request: Request,
) -> Envelope[PlanOut]:
    await rate_limit.check(
        rate_limit.identify(http_request, str(user.id) if user else None), rate_limit.PLAN_LIMIT
    )
    preferences = user.profile.preferences if user and user.profile else {}
    from app.domains.explorer.service import ExplorerService

    saved_ids = await ExplorerService(session).saved_experience_ids(user.id if user else None)
    ctx = build_context(preferences=preferences, saved_ids=saved_ids)
    service = PlanningService(session)
    proposal = await service.propose_optimize(
        itinerary_id,
        ctx,
        user_id=user.id if user else None,
        anonymous_id=anonymous_id,
    )
    return Envelope(
        data=PlanOut(
            stops=[_to_stop_out(s) for s in proposal.stops],
            total_cost=proposal.total_cost,
            total_travel_minutes=proposal.total_travel_minutes,
            rationale=proposal.rationale,
            unmet=proposal.unmet,
        )
    )


@router.post(
    "/itineraries/{itinerary_id}/keep",
    response_model=Envelope[ItineraryOut],
    summary="Keep a draft — promote it to a saved itinerary",
    description=(
        "Promotes the draft to a kept itinerary. The stops are saved exactly as they are. "
        "Kept itineraries are read-only; use the builder to create a new draft to edit."
    ),
)
async def keep_draft(
    itinerary_id: uuid.UUID,
    payload: KeepDraftIn,
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
) -> Envelope[ItineraryOut]:
    service = PlanningService(session)
    itinerary = await service.keep(
        itinerary_id,
        title=payload.title,
        user_id=user.id if user else None,
        anonymous_id=anonymous_id,
    )
    await session.commit()
    return Envelope(data=_to_itinerary_out(itinerary))


# ------------------------------------------------------------- route guidance


class RouteLegOut(CamelModel):
    from_index: int
    to_index: int
    mode: str
    duration_minutes: int
    distance_km: float
    # GeoJSON order, [[lon, lat], ...]. Ready for a map layer as-is.
    geometry: list[list[float]] = []
    # True when no router answered and this is a straight line. The map draws
    # those dashed - a solid line through three buildings claims a road exists.
    is_estimated: bool = False


class RoutePointOut(CamelModel):
    """Where a stop is, for placing a numbered marker.

    Returned here rather than added to StopOut: this is the one endpoint that
    already has to resolve venues, and putting coordinates on every itinerary
    read would load venues for the many callers that only render a timeline.
    Stops with no located venue are simply absent.
    """

    index: int
    latitude: float
    longitude: float


class RouteOut(CamelModel):
    legs: list[RouteLegOut]
    points: list[RoutePointOut] = []
    total_duration_minutes: int
    total_distance_km: float
    provider: str
    # True only when nothing was routed for real. `estimatedLegs` is what the
    # interface usually wants: a route can be mostly real roads with one hop the
    # router could not serve.
    is_estimated: bool
    estimated_legs: int
    # How much longer the real route takes than the plan assumed, in minutes.
    # Positive means the evening is tighter than it looked.
    drift_minutes: int
    # Set when the drift is large enough to act on, phrased for a person.
    warning: str | None = None


@router.get(
    "/itineraries/{itinerary_id}/route",
    response_model=Envelope[RouteOut],
    summary="How to get between the stops",
    description=(
        "Real road geometry and durations for a plan, fetched once for the "
        "sequence that was actually chosen. The planner uses a cheap estimate "
        "while solving - routing every candidate pair would be hundreds of "
        "calls - so this can disagree with the times on the plan, and says by "
        "how much rather than quietly replacing them."
    ),
)
async def itinerary_route(
    itinerary_id: uuid.UUID,
    session: SessionDep,
    user: OptionalUser,
    anonymous_id: AnonymousId,
    mode: str | None = Query(default=None, description="walk or drive; per-leg default otherwise"),
) -> Envelope[RouteOut]:
    itinerary = await PlanningService(session).get(
        itinerary_id, user_id=user.id if user else None, anonymous_id=anonymous_id
    )
    stops = sorted(itinerary.stops, key=lambda s: s.position)

    # Coordinates come from the venues, not the plan: a stop stores only what it
    # needs to render if the listing disappears, and a route needs where the
    # place actually is.
    coordinates = await _stop_coordinates(session, [s.experience_id for s in stops])
    points = [coordinates.get(stop.experience_id) for stop in stops]

    chosen = [mode] * max(0, len(points) - 1) if mode in routing.MODES else None
    route = await routing.route_plan(points, modes=chosen)

    planned = sum(stop.travel_minutes for stop in stops)
    drift = routing.drift_minutes(route, planned)

    warning = None
    if drift >= routing.MATERIAL_DRIFT_MINUTES:
        warning = (
            f"Getting between these takes about {drift} minutes longer than the plan "
            f"allowed. You may want to start earlier or drop a stop."
        )

    return Envelope(
        data=RouteOut(
            legs=[
                RouteLegOut(
                    from_index=leg.from_index,
                    to_index=leg.to_index,
                    mode=leg.mode,
                    duration_minutes=leg.duration_minutes,
                    distance_km=leg.distance_km,
                    geometry=leg.geometry,
                    is_estimated=leg.is_estimated,
                )
                for leg in route.legs
            ],
            points=[
                RoutePointOut(index=index, latitude=point[0], longitude=point[1])
                for index, point in enumerate(points)
                if point is not None
            ],
            total_duration_minutes=route.total_duration_minutes,
            total_distance_km=route.total_distance_km,
            provider=route.provider,
            is_estimated=route.is_estimated,
            estimated_legs=route.estimated_legs,
            drift_minutes=drift,
            warning=warning,
        )
    )


async def _stop_coordinates(
    session, experience_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[float, float] | None]:
    """Where each stop actually is, or None when the venue has no coordinates."""
    if not experience_ids:
        return {}

    result = await session.execute(
        select(Experience)
        .where(Experience.id.in_(experience_ids))
        .options(selectinload(Experience.venue))
    )
    found: dict[uuid.UUID, tuple[float, float] | None] = {}
    for experience in result.scalars().unique():
        venue = experience.venue
        found[experience.id] = (
            (venue.latitude, venue.longitude)
            if venue is not None and venue.latitude is not None and venue.longitude is not None
            else None
        )
    return found
