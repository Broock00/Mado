"""Planning orchestration.

Sits between the planner - which is pure, synchronous and testable - and the
database. Keeping the algorithm free of I/O is the point: planning logic is where
the subtle bugs live, and it is far easier to trust when it can be exercised with
plain objects and no fixtures.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.domains.catalog import repository as catalog_repo
from app.domains.catalog.repository import Area
from app.domains.discovery.ranking import RankingContext
from app.domains.explorer.models import Itinerary, ItineraryStop
from app.domains.explorer.planning import (
    Plan,
    PlanRequest,
    Trip,
    TripRequest,
    build_plan,
    build_trip,
)
from app.integrations import weather

logger = get_logger("mado.planning.service")

# Wider than the number of stops by a large margin: the planner discards most
# candidates on feasibility, so a thin pool produces a thin plan.
PLANNING_POOL = 120


class PlanningService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def plan(self, request: PlanRequest, ctx: RankingContext) -> Plan:
        """Build a plan without saving it.

        Planning and saving are separate because most plans are never kept - an
        explorer asks the concierge for an evening, looks at it, and asks for a
        different one. Writing every draft would fill the table with noise.
        """
        candidates = await catalog_repo.query_experiences(
            self.session,
            area=Area(city_slug=request.city_slug) if request.city_slug else None,
            required_suitability=request.required_suitability,
            limit=PLANNING_POOL,
        )
        plan = build_plan(candidates, request, ctx)
        logger.info(
            "plan_built",
            city=request.city_slug,
            candidates=len(candidates),
            stops=len(plan.stops),
            travel_minutes=plan.total_travel_minutes,
            unmet=len(plan.unmet),
        )
        return plan

    async def plan_trip(self, request: TripRequest, ctx: RankingContext) -> Trip:
        """Build a multi-day trip without saving it.

        The forecast is fetched once for the whole stay rather than per day: it
        is one request that returns every day the provider can see, and asking
        per day would be five calls for the same response. Days past the horizon
        are simply absent from the map, which is what lets the planner tell the
        difference between "dry" and "not knowable yet".
        """
        candidates = await catalog_repo.query_experiences(
            self.session,
            area=Area(city_slug=request.city_slug) if request.city_slug else None,
            required_suitability=request.required_suitability,
            # A whole stay draws from this pool for every day, so it has to be
            # deeper than a single evening's - otherwise day four is planned from
            # whatever three days of de-duplication left behind.
            limit=PLANNING_POOL * 2,
        )

        forecast = await weather.forecast_covering(
            request.latitude,
            request.longitude,
            start=request.start,
            end=request.end,
        )
        weather_by_day = (
            {day.day: day for day in forecast.days} if forecast is not None else {}
        )

        trip = build_trip(candidates, request, ctx, weather_by_day=weather_by_day)
        logger.info(
            "trip_built",
            city=request.city_slug,
            days=len(trip.days),
            planned_days=trip.planned_days,
            candidates=len(candidates),
            forecast_days=len(weather_by_day),
            unmet=len(trip.unmet),
        )
        return trip

    async def save(
        self,
        plan: Plan,
        request: PlanRequest,
        *,
        title: str,
        user_id: uuid.UUID | None,
        anonymous_id: str | None,
    ) -> Itinerary:
        """Persist a plan as an itinerary."""
        itinerary = Itinerary(
            user_id=user_id,
            anonymous_id=None if user_id else anonymous_id,
            title=title,
            city_slug=request.city_slug,
            starts_at=plan.stops[0].arrive_at if plan.stops else request.start,
            ends_at=plan.stops[-1].depart_at if plan.stops else request.end,
            estimated_cost=plan.total_cost,
            total_travel_minutes=plan.total_travel_minutes,
            constraints={
                "budget": request.budget,
                "maxStops": request.max_stops,
                "categories": request.categories,
                "freeOnly": request.free_only,
                "window": [request.start.isoformat(), request.end.isoformat()],
            },
            rationale=plan.rationale,
            stops=[],
        )
        self.session.add(itinerary)
        await self.session.flush()

        for position, stop in enumerate(plan.stops):
            itinerary.stops.append(
                ItineraryStop(
                    itinerary_id=itinerary.id,
                    position=position,
                    experience_id=stop.experience.id,
                    event_instance_id=stop.event_instance_id,
                    title=stop.experience.title,
                    arrive_at=stop.arrive_at,
                    depart_at=stop.depart_at,
                    travel_minutes=stop.travel_minutes,
                    travel_km=stop.travel_km,
                    estimated_cost=stop.estimated_cost,
                    is_fixed_time=stop.is_fixed_time,
                    note=stop.note,
                )
            )
        await self.session.flush()
        logger.info("itinerary_saved", itinerary_id=str(itinerary.id), stops=len(plan.stops))
        return itinerary

    async def save_offered(
        self,
        offered: dict,
        *,
        title: str,
        user_id: uuid.UUID | None,
        anonymous_id: str | None,
    ) -> Itinerary:
        """Persist a plan exactly as it was shown to the explorer.

        Takes the stored plan rather than re-running the planner. Recomputing on
        accept would sometimes hand back a different evening - an event may have
        sold out, or ranking may have shifted between the offer and the answer -
        and an explorer who says "yes, that one" means the one they were shown.

        The plan comes from the conversation the platform itself wrote, not from
        the client, so the stops are trusted. Titles and timings are still bounded
        by the model column widths on the way in.
        """
        stops = offered.get("stops") or []
        if not stops:
            raise ValidationError("That plan has no stops to save.", code="EMPTY_PLAN")

        request = offered.get("request") or {}
        arrive_first = _parse(stops[0]["arriveAt"])
        depart_last = _parse(stops[-1]["departAt"])

        itinerary = Itinerary(
            user_id=user_id,
            anonymous_id=None if user_id else anonymous_id,
            title=title[:200],
            city_slug=request.get("city"),
            starts_at=arrive_first,
            ends_at=depart_last,
            estimated_cost=offered.get("totalCost"),
            total_travel_minutes=offered.get("totalTravelMinutes") or 0,
            constraints={
                "budget": request.get("budget"),
                "maxStops": request.get("maxStops"),
                "categories": request.get("categories") or [],
                "freeOnly": request.get("freeOnly", False),
                "window": [request.get("startsAt"), request.get("endsAt")],
                # Records that this came out of a conversation rather than the
                # manual builder, which is the difference the Plan page shows.
                "origin": "concierge",
            },
            rationale=offered.get("rationale"),
            stops=[],
        )
        self.session.add(itinerary)
        await self.session.flush()

        for position, stop in enumerate(stops):
            itinerary.stops.append(
                ItineraryStop(
                    itinerary_id=itinerary.id,
                    position=position,
                    experience_id=uuid.UUID(stop["experienceId"]),
                    event_instance_id=(
                        uuid.UUID(stop["eventInstanceId"])
                        if stop.get("eventInstanceId")
                        else None
                    ),
                    title=str(stop["title"])[:300],
                    arrive_at=_parse(stop["arriveAt"]),
                    depart_at=_parse(stop["departAt"]),
                    travel_minutes=int(stop.get("travelMinutes") or 0),
                    travel_km=stop.get("travelKm"),
                    estimated_cost=stop.get("estimatedCost"),
                    is_fixed_time=bool(stop.get("isFixedTime")),
                    note=stop.get("note"),
                )
            )

        await self.session.flush()
        logger.info(
            "itinerary_accepted_from_concierge",
            itinerary_id=str(itinerary.id),
            stops=len(stops),
        )
        return itinerary

    async def get(
        self,
        itinerary_id: uuid.UUID,
        *,
        user_id: uuid.UUID | None,
        anonymous_id: str | None,
    ) -> Itinerary:
        """Load one itinerary belonging to the caller.

        Returns 404 rather than 403 for someone else's plan, so the endpoint does
        not confirm that an id exists to whoever guesses it.
        """
        itinerary = await self.session.get(Itinerary, itinerary_id)
        if itinerary is None or itinerary.deleted_at is not None:
            raise NotFoundError("Itinerary not found.", code="ITINERARY_NOT_FOUND")

        owned = (
            itinerary.user_id == user_id
            if user_id is not None
            else itinerary.anonymous_id is not None and itinerary.anonymous_id == anonymous_id
        )
        if not owned:
            raise NotFoundError("Itinerary not found.", code="ITINERARY_NOT_FOUND")
        return itinerary

    async def list_for(
        self, *, user_id: uuid.UUID | None, anonymous_id: str | None, limit: int = 20
    ) -> list[Itinerary]:
        if user_id is None and anonymous_id is None:
            return []
        stmt = select(Itinerary).where(Itinerary.deleted_at.is_(None))
        stmt = (
            stmt.where(Itinerary.user_id == user_id)
            if user_id is not None
            else stmt.where(Itinerary.anonymous_id == anonymous_id)
        )
        result = await self.session.execute(
            stmt.order_by(Itinerary.created_at.desc()).limit(limit)
        )
        return list(result.scalars().unique().all())

    async def delete(
        self, itinerary_id: uuid.UUID, *, user_id: uuid.UUID | None, anonymous_id: str | None
    ) -> None:
        itinerary = await self.get(itinerary_id, user_id=user_id, anonymous_id=anonymous_id)
        # Soft delete: an itinerary is the explorer's own record of an evening, and
        # spec BUSINESS-07's "nothing is deleted automatically" applies to their
        # history too.
        itinerary.deleted_at = datetime.now(UTC)


def _parse(value: str) -> datetime:
    """Read a timestamp the platform itself wrote, defaulting to UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
