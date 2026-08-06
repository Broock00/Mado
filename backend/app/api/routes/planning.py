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

from app.api.deps import AnonymousId, OptionalUser, SessionDep
from app.core import rate_limit
from app.core.config import get_settings
from app.core.envelope import CollectionEnvelope, Envelope
from app.core.errors import BadRequestError
from app.domains.catalog.schemas import CamelModel
from app.domains.discovery.service import build_context
from app.domains.explorer.learning import infer_preferences
from app.domains.explorer.planning import PlanRequest
from app.domains.explorer.planning_service import PlanningService
from app.domains.explorer.service import ExplorerService

router = APIRouter(tags=["planning"])
settings = get_settings()

# A plan is for an outing, not a holiday. Longer requests are almost always a
# client sending a bad window rather than a genuine 30-hour evening.
MAX_WINDOW_HOURS = 24


class PlanRequestIn(CamelModel):
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    city: str = settings.default_city_slug
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


class PlanOut(CamelModel):
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
    stops: list[StopOut]


class SaveItineraryIn(PlanRequestIn):
    title: str = Field(min_length=1, max_length=200)


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
    request = PlanRequest(
        start=start,
        end=end,
        city_slug=payload.city,
        latitude=payload.latitude,
        longitude=payload.longitude,
        budget=payload.budget,
        max_stops=payload.max_stops,
        categories=payload.categories,
        free_only=payload.free_only,
    )
    return request, ctx


def _to_stop_out(stop) -> StopOut:
    return StopOut(
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
        stops=[
            StopOut(
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
            )
            for stop in itinerary.stops
        ],
    )
