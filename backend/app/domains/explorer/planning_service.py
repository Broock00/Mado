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

from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.domains.catalog import repository as catalog_repo
from app.domains.discovery.ranking import RankingContext
from app.domains.explorer.models import Itinerary, ItineraryStop
from app.domains.explorer.planning import Plan, PlanRequest, build_plan

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
            city_slug=request.city_slug,
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
