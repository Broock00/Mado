"""Planning orchestration.

Sits between the planner - which is pure, synchronous and testable - and the
database. Keeping the algorithm free of I/O is the point: planning logic is where
the subtle bugs live, and it is far easier to trust when it can be exercised with
plain objects and no fixtures.

The service now manages two kinds of itinerary: *drafts* and *kept* plans.

A draft is a workspace. It is created when the explorer opens the builder, and
every stop mutation (add, remove, reorder, edit times) writes to it immediately.
It becomes a kept plan only when the explorer explicitly saves — "Keep this plan".
Kept plans are read-only through the builder; the explorer can open a kept plan
and view its route guidance but cannot re-edit it (they open a new draft instead).

Draft mutations write the explorer's exact choices rather than re-solving. Only the
assistant operations (`propose_fill_gap`, `propose_optimize`) run the engine, and
even those return proposals rather than applying changes — the explorer accepts each
explicitly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.domains.catalog import repository as catalog_repo
from app.domains.catalog.models import Experience
from app.domains.catalog.repository import Area
from app.domains.discovery.ranking import RankingContext
from app.domains.explorer.models import Itinerary, ItineraryStop
from app.domains.explorer.planning import (
    Analysis,
    Plan,
    PlannedStop,
    PlanRequest,
    StopInput,
    Trip,
    TripRequest,
    _coords,
    _cost_of,
    _dwell_minutes,
    analyze_stops,
    build_optimize_proposal,
    build_plan,
    build_trip,
    compute_stop_times,
    travel_estimate,
)
from app.integrations import weather

logger = get_logger("mado.planning.service")

# Wider than the number of stops by a large margin: the planner discards most
# candidates on feasibility, so a thin pool produces a thin plan.
PLANNING_POOL = 120

# Status constants — never raw strings in callers.
STATUS_DRAFT = "draft"
STATUS_KEPT = "kept"


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

    # ------------------------------------------------------------------ drafts

    async def create_draft(
        self,
        *,
        title: str,
        city_slug: str | None,
        starts_at: datetime,
        ends_at: datetime,
        budget: float | None,
        free_only: bool,
        user_id: uuid.UUID | None,
        anonymous_id: str | None,
        timezone: str = "UTC",
        kind: str | None = None,
    ) -> Itinerary:
        """Create an empty draft itinerary (outing or multi-day trip).

        A draft is a workspace. It becomes a kept plan only when the explorer
        explicitly calls `keep`. Kind is inferred from the local calendar span
        when not provided: more than one local day → trip.
        """
        from app.domains.explorer.planning import MAX_TRIP_DAYS

        plan_kind = _infer_plan_kind(starts_at, ends_at, timezone=timezone, kind=kind)
        if plan_kind == "trip":
            from zoneinfo import ZoneInfo

            try:
                tz = ZoneInfo(timezone)
            except Exception:
                tz = ZoneInfo("UTC")
            days = (ends_at.astimezone(tz).date() - starts_at.astimezone(tz).date()).days + 1
            if days > MAX_TRIP_DAYS:
                raise ValidationError(
                    f"A trip may span at most {MAX_TRIP_DAYS} days.",
                    code="TRIP_TOO_LONG",
                )

        itinerary = Itinerary(
            user_id=user_id,
            anonymous_id=None if user_id else anonymous_id,
            title=title[:200],
            city_slug=city_slug,
            starts_at=starts_at,
            ends_at=ends_at,
            estimated_cost=0.0,
            total_travel_minutes=0,
            status=STATUS_DRAFT,
            constraints={
                "budget": budget,
                "freeOnly": free_only,
                "window": [starts_at.isoformat(), ends_at.isoformat()],
                "origin": "manual",
                "kind": plan_kind,
                "timezone": timezone,
            },
            rationale=None,
            stops=[],
        )
        self.session.add(itinerary)
        await self.session.flush()
        logger.info(
            "draft_created",
            itinerary_id=str(itinerary.id),
            kind=plan_kind,
        )
        return itinerary

    async def create_draft_from_offer(
        self,
        offered: dict,
        *,
        title: str,
        user_id: uuid.UUID | None,
        anonymous_id: str | None,
    ) -> Itinerary:
        """Materialise a concierge-offered plan (outing or trip) as an editable draft."""
        return await self.save_offered(
            offered,
            title=title,
            user_id=user_id,
            anonymous_id=anonymous_id,
            status=STATUS_DRAFT,
        )

    async def update_draft_meta(
        self,
        itinerary_id: uuid.UUID,
        *,
        title: str | None,
        starts_at: datetime | None,
        ends_at: datetime | None,
        city_slug: str | None,
        budget: float | None,
        free_only: bool | None,
        user_id: uuid.UUID | None,
        anonymous_id: str | None,
    ) -> Itinerary:
        """Update an itinerary's title, window, or constraints.

        Works for drafts and kept plans. Stops are not re-solved — only metadata.
        """
        itinerary = await self.get(itinerary_id, user_id=user_id, anonymous_id=anonymous_id)
        _require_editable(itinerary, user_id=user_id, anonymous_id=anonymous_id)

        if title is not None:
            itinerary.title = title[:200]
        if starts_at is not None:
            itinerary.starts_at = starts_at
        if ends_at is not None:
            itinerary.ends_at = ends_at
        if city_slug is not None:
            itinerary.city_slug = city_slug

        constraints = dict(itinerary.constraints or {})
        if budget is not None:
            constraints["budget"] = budget
        if free_only is not None:
            constraints["freeOnly"] = free_only
        if starts_at is not None or ends_at is not None:
            ws = starts_at or itinerary.starts_at
            we = ends_at or itinerary.ends_at
            constraints["window"] = [ws.isoformat(), we.isoformat()]
            tz = constraints.get("timezone") or "UTC"
            constraints["kind"] = _infer_plan_kind(ws, we, timezone=tz, kind=None)
        itinerary.constraints = constraints

        await self.session.flush()
        return itinerary

    async def append_stop(
        self,
        itinerary_id: uuid.UUID,
        experience_id: uuid.UUID,
        event_instance_id: uuid.UUID | None,
        *,
        user_id: uuid.UUID | None,
        anonymous_id: str | None,
        day_index: int | None = None,
    ) -> Itinerary:
        """Append one stop to a day, computing times from the last stop *on that day*.

        Travel does not chain across midnight — the first stop of a day has zero
        travel from the previous day's last stop. That is what keeps multi-day
        plans from inventing overnight road trips.
        """
        itinerary = await self.get(itinerary_id, user_id=user_id, anonymous_id=anonymous_id)
        _require_editable(itinerary, user_id=user_id, anonymous_id=anonymous_id)

        experience = await self._load_single_experience(experience_id)
        if experience is None:
            raise NotFoundError("Experience not found.", code="EXPERIENCE_NOT_FOUND")

        from app.domains.explorer.planning import MAX_BUILDER_STOPS, MAX_TRIP_DAYS

        existing = _sorted_stops(list(itinerary.stops))
        if len(existing) >= MAX_BUILDER_STOPS:
            raise ValidationError(
                f"A plan may have at most {MAX_BUILDER_STOPS} stops.",
                code="TOO_MANY_STOPS",
            )

        # Duplicate experience across any day of a trip is forbidden — same
        # invariant as build_trip. Outings also forbid duplicates.
        if any(s.experience_id == experience_id for s in existing):
            raise ValidationError(
                "That stop is already in this plan.",
                code="DUPLICATE_STOP",
            )

        if day_index is None:
            day_index = max((int(s.day_index or 0) for s in existing), default=0)
        day_index = int(day_index)
        if day_index < 0 or day_index >= MAX_TRIP_DAYS:
            raise ValidationError(
                f"dayIndex must be between 0 and {MAX_TRIP_DAYS - 1}.",
                code="INVALID_DAY",
            )

        day_stops = _stops_on_day(existing, day_index)

        if day_stops:
            last = day_stops[-1]
            last_exp = await self._load_single_experience(last.experience_id)
            last_coords = _coords(last_exp) if last_exp else None
            here = _coords(experience)
            travel_minutes, travel_km = travel_estimate(last_coords, here)
            if travel_km is not None:
                travel_km = round(float(travel_km), 2)
            arrive_at = last.depart_at + timedelta(minutes=travel_minutes)
        else:
            travel_minutes = 0
            travel_km = None
            arrive_at = _day_anchor(itinerary, day_index)

        dwell = _dwell_minutes(experience)
        depart_at = arrive_at + timedelta(minutes=dwell)
        is_fixed = event_instance_id is not None
        position = len(day_stops)

        stop = ItineraryStop(
            itinerary_id=itinerary.id,
            day_index=day_index,
            position=position,
            experience_id=experience_id,
            event_instance_id=event_instance_id,
            title=experience.title[:300],
            arrive_at=arrive_at,
            depart_at=depart_at,
            travel_minutes=travel_minutes,
            travel_km=travel_km,
            estimated_cost=_cost_of(experience),
            is_fixed_time=is_fixed,
        )
        self.session.add(stop)

        if depart_at > itinerary.ends_at:
            itinerary.ends_at = depart_at
        itinerary.estimated_cost = _money(itinerary.estimated_cost) + _cost_of(experience)
        itinerary.total_travel_minutes = (itinerary.total_travel_minutes or 0) + travel_minutes

        await self.session.flush()
        logger.info(
            "stop_appended",
            itinerary_id=str(itinerary_id),
            experience_id=str(experience_id),
            day_index=day_index,
            position=position,
        )
        return itinerary

    async def replace_stops(
        self,
        itinerary_id: uuid.UUID,
        stop_specs: list[dict],
        *,
        user_id: uuid.UUID | None,
        anonymous_id: str | None,
    ) -> Itinerary:
        """Replace the entire stop list with a new ordered set.

        Used for reorder, remove, and bulk-edit operations. Each spec dict holds
        at minimum ``experienceId``; optionally ``eventInstanceId``, ``arriveAt``,
        ``departAt``, and ``note``. When arrive/depart are not supplied they are
        computed from travel estimates (forward-only, no re-solve).

        The explorer's explicit times are trusted without re-solving. If the
        result has conflicts, the check endpoint will surface them.
        """
        itinerary = await self.get(itinerary_id, user_id=user_id, anonymous_id=anonymous_id)
        _require_editable(itinerary, user_id=user_id, anonymous_id=anonymous_id)

        if not stop_specs:
            # Clearing all stops is valid: the explorer is starting fresh.
            for stop in list(itinerary.stops):
                await self.session.delete(stop)
            itinerary.estimated_cost = 0.0
            itinerary.total_travel_minutes = 0
            await self.session.flush()
            return itinerary

        experience_ids = [uuid.UUID(s["experienceId"]) for s in stop_specs]
        exp_map = await self._load_experiences(experience_ids)

        # Build rows grouped by day. Travel only chains within a day.
        from collections import defaultdict

        from app.domains.explorer.planning import MAX_TRIP_DAYS

        by_day: dict[int, list[tuple[StopInput, str | None, bool]]] = defaultdict(list)
        # (input, note, times_explicit)

        seen_exp: set[uuid.UUID] = set()
        for spec in stop_specs:
            exp_id = uuid.UUID(spec["experienceId"])
            exp = exp_map.get(exp_id)
            if exp is None:
                continue
            if exp_id in seen_exp:
                # Skip duplicates rather than violating the trip invariant.
                continue
            seen_exp.add(exp_id)

            day_i = int(spec.get("dayIndex") or 0)
            if day_i < 0 or day_i >= MAX_TRIP_DAYS:
                raise ValidationError(
                    f"dayIndex must be between 0 and {MAX_TRIP_DAYS - 1}.",
                    code="INVALID_DAY",
                )

            event_inst_id = (
                uuid.UUID(spec["eventInstanceId"]) if spec.get("eventInstanceId") else None
            )
            is_fixed = event_inst_id is not None or bool(spec.get("isFixedTime"))
            arrive_raw = spec.get("arriveAt")
            depart_raw = spec.get("departAt")
            times_explicit = bool(arrive_raw and depart_raw)
            arrive_at = _parse(arrive_raw) if arrive_raw else _day_anchor(itinerary, day_i)
            depart_at = (
                _parse(depart_raw)
                if depart_raw
                else arrive_at + timedelta(minutes=_dwell_minutes(exp))
            )

            by_day[day_i].append(
                (
                    StopInput(
                        experience=exp,
                        event_instance_id=event_inst_id,
                        is_fixed_time=is_fixed,
                        arrive_at=arrive_at,
                        depart_at=depart_at,
                        estimated_cost=_cost_of(exp),
                    ),
                    spec.get("note"),
                    times_explicit,
                )
            )

        # Recompute times within each day when the explorer omitted them.
        for day_i, rows in by_day.items():
            needs = [i for i, (_, _, explicit) in enumerate(rows) if not explicit]
            if not needs:
                continue
            inputs_only = [inp for inp, _, _ in rows]
            computed = compute_stop_times(
                inputs_only, _day_anchor(itinerary, day_i), None
            )
            for i, (arrive, depart, _tm, _tk) in zip(needs, computed, strict=False):
                old_inp, note, _ = rows[i]
                rows[i] = (
                    StopInput(
                        experience=old_inp.experience,
                        event_instance_id=old_inp.event_instance_id,
                        is_fixed_time=old_inp.is_fixed_time,
                        arrive_at=arrive,
                        depart_at=depart,
                        estimated_cost=old_inp.estimated_cost,
                    ),
                    note,
                    False,
                )

        for stop in list(itinerary.stops):
            await self.session.delete(stop)
        await self.session.flush()

        total_cost = 0.0
        total_travel = 0
        last_depart: datetime | None = None
        for day_i in sorted(by_day.keys()):
            rows = by_day[day_i]
            for position, (stop_input, note, _) in enumerate(rows):
                if position == 0:
                    travel_minutes, travel_km = 0, None
                else:
                    prev_exp = rows[position - 1][0].experience
                    travel_minutes, travel_km = travel_estimate(
                        _coords(prev_exp), _coords(stop_input.experience)
                    )
                    if travel_km is not None:
                        travel_km = round(float(travel_km), 2)
                total_travel += travel_minutes
                total_cost += stop_input.estimated_cost
                last_depart = stop_input.depart_at

                self.session.add(
                    ItineraryStop(
                        itinerary_id=itinerary.id,
                        day_index=day_i,
                        position=position,
                        experience_id=stop_input.experience.id,
                        event_instance_id=stop_input.event_instance_id,
                        title=stop_input.experience.title[:300],
                        arrive_at=stop_input.arrive_at,
                        depart_at=stop_input.depart_at,
                        travel_minutes=travel_minutes,
                        travel_km=travel_km,
                        estimated_cost=stop_input.estimated_cost,
                        is_fixed_time=stop_input.is_fixed_time,
                        note=note,
                    )
                )

        if last_depart is not None and last_depart > itinerary.ends_at:
            itinerary.ends_at = last_depart
        itinerary.estimated_cost = round(total_cost, 2)
        itinerary.total_travel_minutes = total_travel

        await self.session.flush()
        logger.info(
            "stops_replaced",
            itinerary_id=str(itinerary_id),
            count=sum(len(v) for v in by_day.values()),
            days=len(by_day),
        )
        return itinerary

    async def remove_stop(
        self,
        itinerary_id: uuid.UUID,
        stop_id: uuid.UUID,
        *,
        user_id: uuid.UUID | None,
        anonymous_id: str | None,
    ) -> Itinerary:
        """Remove one stop and renumber positions within its day."""
        itinerary = await self.get(itinerary_id, user_id=user_id, anonymous_id=anonymous_id)
        _require_editable(itinerary, user_id=user_id, anonymous_id=anonymous_id)

        existing = _sorted_stops(list(itinerary.stops))
        target = next((s for s in existing if s.id == stop_id), None)
        if target is None:
            raise NotFoundError("Stop not found.", code="STOP_NOT_FOUND")

        day_i = int(target.day_index or 0)
        await self.session.delete(target)
        await self.session.flush()

        remaining = [s for s in existing if s.id != stop_id]
        day_remaining = [s for s in remaining if int(s.day_index or 0) == day_i]
        for i, stop in enumerate(day_remaining):
            stop.position = i

        itinerary.estimated_cost = round(
            sum(_money(s.estimated_cost) for s in remaining), 2
        )
        itinerary.total_travel_minutes = sum(s.travel_minutes for s in remaining)
        if remaining:
            itinerary.ends_at = max(s.depart_at for s in remaining)
        await self.session.flush()
        logger.info("stop_removed", itinerary_id=str(itinerary_id), stop_id=str(stop_id))
        return itinerary

    async def check(
        self,
        itinerary_id: uuid.UUID,
        *,
        user_id: uuid.UUID | None,
        anonymous_id: str | None,
    ) -> Analysis:
        """Validate stops day-by-day without changing anything.

        Each day is analysed against its own window so overnight gaps are not
        reported as free time to fill. Budget is shared across the whole stay.
        """
        itinerary = await self.get(itinerary_id, user_id=user_id, anonymous_id=anonymous_id)
        stops = _sorted_stops(list(itinerary.stops))
        if not stops:
            return Analysis(
                conflicts=[], gaps=[], total_cost=0.0, total_travel_minutes=0, budget_overrun=0.0
            )

        exp_ids = [s.experience_id for s in stops]
        exp_map = await self._load_experiences(exp_ids)

        constraints = itinerary.constraints or {}
        budget = constraints.get("budget")
        remaining_budget = float(budget) if budget is not None else None

        all_conflicts = []
        all_gaps = []
        total_cost = 0.0
        total_travel = 0
        global_offset = 0

        days = sorted({int(s.day_index or 0) for s in stops})
        for day_i in days:
            day_stops = [s for s in stops if int(s.day_index or 0) == day_i]
            stop_inputs: list[StopInput] = []
            for stop in day_stops:
                exp = exp_map.get(stop.experience_id)
                if exp is None:
                    continue
                stop_inputs.append(
                    StopInput(
                        experience=exp,
                        event_instance_id=stop.event_instance_id,
                        is_fixed_time=stop.is_fixed_time,
                        arrive_at=stop.arrive_at,
                        depart_at=stop.depart_at,
                        estimated_cost=_money(stop.estimated_cost),
                    )
                )
            if not stop_inputs:
                global_offset += len(day_stops)
                continue

            day_start = _day_anchor(itinerary, day_i)
            # End of active day ≈ next day's anchor, or itinerary end.
            if day_i + 1 in days or day_i + 1 <= max(days):
                try:
                    day_end = _day_anchor(itinerary, day_i + 1)
                except Exception:
                    day_end = itinerary.ends_at
                if day_end <= day_start:
                    day_end = itinerary.ends_at
            else:
                day_end = itinerary.ends_at

            analysis = analyze_stops(
                stops=stop_inputs,
                window_start=day_start,
                window_end=day_end,
                origin=None,
                # Only charge budget overrun on the last day so we do not
                # multiply the same shared budget across every day.
                budget=None,
            )
            for c in analysis.conflicts:
                all_conflicts.append(
                    type(c)(
                        kind=c.kind,
                        stop_indices=[i + global_offset for i in c.stop_indices],
                        message=c.message,
                        resolutions=c.resolutions,
                    )
                )
            for g in analysis.gaps:
                all_gaps.append(
                    type(g)(
                        after_index=(
                            g.after_index + global_offset if g.after_index >= 0 else -1
                        ),
                        starts_at=g.starts_at,
                        ends_at=g.ends_at,
                        free_minutes=g.free_minutes,
                    )
                )
            total_cost += analysis.total_cost
            total_travel += analysis.total_travel_minutes
            global_offset += len(day_stops)

        budget_overrun = 0.0
        if remaining_budget is not None and total_cost > remaining_budget:
            budget_overrun = round(total_cost - remaining_budget, 2)
            from app.domains.explorer.planning import Conflict

            all_conflicts.append(
                Conflict(
                    kind="budget_overrun",
                    stop_indices=[],
                    message=(
                        f"This plan costs about {total_cost:.0f}, "
                        f"which is over the budget of {remaining_budget:.0f}."
                    ),
                    resolutions=["remove_stop", "keep_as_is"],
                )
            )

        return Analysis(
            conflicts=all_conflicts,
            gaps=all_gaps,
            total_cost=round(total_cost, 2),
            total_travel_minutes=total_travel,
            budget_overrun=budget_overrun,
        )

    async def propose_fill_gap(
        self,
        itinerary_id: uuid.UUID,
        after_index: int,
        gap_start: datetime,
        gap_end: datetime,
        ctx: RankingContext,
        *,
        user_id: uuid.UUID | None,
        anonymous_id: str | None,
    ) -> Plan:
        """Propose stops that fit in a free gap — without changing the draft.

        The gap is defined by `after_index` (which existing stop it follows) and
        the free time window. The existing stops are pinned as avoid_experience_ids
        so the proposal never suggests something already in the plan. The proposal
        is returned; it is only applied when the explorer accepts individual stops.
        """
        itinerary = await self.get(itinerary_id, user_id=user_id, anonymous_id=anonymous_id)
        # Canonical order so after_index from check lines up with the list
        # the builder splices into when accepting a fill.
        stops = _sorted_stops(list(itinerary.stops))
        avoid_ids = [s.experience_id for s in stops]

        area = Area(city_slug=itinerary.city_slug) if itinerary.city_slug else None
        candidates = await catalog_repo.query_experiences(
            self.session,
            area=area,
            limit=PLANNING_POOL,
        )

        request = PlanRequest(
            start=gap_start,
            end=gap_end,
            city_slug=itinerary.city_slug or "",
            avoid_experience_ids=avoid_ids,
            currency=(itinerary.constraints or {}).get("currency", "ETB"),
        )

        return build_plan(candidates, request, ctx)

    async def propose_optimize(
        self,
        itinerary_id: uuid.UUID,
        ctx: RankingContext,
        *,
        user_id: uuid.UUID | None,
        anonymous_id: str | None,
    ) -> Plan:
        """Propose a reordered sequence using 2-opt + retiming — without applying it.

        Multi-day trips are optimized per day: overnight is not a hop, and a
        stop must not migrate to another day. Returns one flat Plan with
        ``day_index`` set on each stop so accept can PUT the list back intact.
        """
        itinerary = await self.get(itinerary_id, user_id=user_id, anonymous_id=anonymous_id)
        stops = _sorted_stops(list(itinerary.stops))
        if len(stops) < 2:
            return Plan([], 0.0, 0, "Nothing to optimize in a single-stop plan.")

        exp_map = await self._load_experiences([s.experience_id for s in stops])
        constraints = itinerary.constraints or {}
        currency = constraints.get("currency", "ETB")

        days = sorted({int(s.day_index or 0) for s in stops})
        proposed: list = []
        total_cost = 0.0
        total_travel = 0
        day_notes: list[str] = []

        for day_i in days:
            day_rows = _stops_on_day(stops, day_i)
            stop_inputs = [
                StopInput(
                    experience=exp_map[s.experience_id],
                    event_instance_id=s.event_instance_id,
                    is_fixed_time=s.is_fixed_time,
                    arrive_at=s.arrive_at,
                    depart_at=s.depart_at,
                    estimated_cost=_money(s.estimated_cost),
                )
                for s in day_rows
                if s.experience_id in exp_map
            ]
            if len(stop_inputs) < 2:
                # Leave the day alone — still emit stops so accept keeps them.
                for s in day_rows:
                    exp = exp_map.get(s.experience_id)
                    if exp is None:
                        continue
                    proposed.append(
                        PlannedStop(
                            experience=exp,
                            arrive_at=s.arrive_at,
                            depart_at=s.depart_at,
                            travel_minutes=s.travel_minutes,
                            travel_km=float(s.travel_km) if s.travel_km is not None else None,
                            estimated_cost=_money(s.estimated_cost),
                            is_fixed_time=s.is_fixed_time,
                            event_instance_id=s.event_instance_id,
                            note=s.note,
                            day_index=day_i,
                        )
                    )
                    total_cost += _money(s.estimated_cost)
                    total_travel += s.travel_minutes or 0
                continue

            day_start = _day_anchor(itinerary, day_i)
            try:
                day_end = _day_anchor(itinerary, day_i + 1)
            except Exception:
                day_end = itinerary.ends_at
            if day_end <= day_start:
                day_end = itinerary.ends_at

            day_plan = build_optimize_proposal(
                stops=stop_inputs,
                window_start=day_start,
                window_end=day_end,
                origin=None,
                currency=currency,
            )
            for stop in day_plan.stops:
                stop.day_index = day_i
            proposed.extend(day_plan.stops)
            total_cost += day_plan.total_cost
            total_travel += day_plan.total_travel_minutes
            if day_plan.rationale:
                day_notes.append(day_plan.rationale)

        if not proposed:
            return Plan([], 0.0, 0, "Nothing to optimize.", currency=currency)

        rationale = (
            "Each day reordered for shorter travel."
            if len(days) > 1
            else (day_notes[0] if day_notes else "Reordered for shorter travel.")
        )
        return Plan(
            stops=proposed,
            total_cost=round(total_cost, 2),
            total_travel_minutes=total_travel,
            rationale=rationale,
            currency=currency,
        )

    async def keep(
        self,
        itinerary_id: uuid.UUID,
        *,
        title: str | None,
        user_id: uuid.UUID | None,
        anonymous_id: str | None,
    ) -> Itinerary:
        """Promote a draft to a kept itinerary.

        Kept plans remain editable — promoting only marks them as saved so they
        appear in the kept list. Stops are not re-solved.
        """
        itinerary = await self.get(itinerary_id, user_id=user_id, anonymous_id=anonymous_id)
        _require_draft(itinerary)

        if not itinerary.stops:
            raise ValidationError(
                "A plan with no stops cannot be kept.", code="EMPTY_PLAN"
            )

        if title is not None:
            itinerary.title = title[:200]

        itinerary.status = STATUS_KEPT
        await self.session.flush()
        logger.info(
            "itinerary_kept",
            itinerary_id=str(itinerary_id),
            stops=len(itinerary.stops),
        )
        return itinerary

    async def list_kept(
        self, *, user_id: uuid.UUID | None, anonymous_id: str | None, limit: int = 20
    ) -> list[Itinerary]:
        """List the explorer's kept itineraries (not drafts)."""
        if user_id is None and anonymous_id is None:
            return []
        stmt = select(Itinerary).where(
            Itinerary.deleted_at.is_(None),
            Itinerary.status == STATUS_KEPT,
        )
        stmt = (
            stmt.where(Itinerary.user_id == user_id)
            if user_id is not None
            else stmt.where(Itinerary.anonymous_id == anonymous_id)
        )
        result = await self.session.execute(
            stmt.order_by(Itinerary.created_at.desc()).limit(limit)
        )
        return list(result.scalars().unique().all())

    async def list_drafts(
        self, *, user_id: uuid.UUID | None, anonymous_id: str | None, limit: int = 20
    ) -> list[Itinerary]:
        """List the explorer's draft itineraries (not yet kept)."""
        if user_id is None and anonymous_id is None:
            return []
        stmt = select(Itinerary).where(
            Itinerary.deleted_at.is_(None),
            Itinerary.status == STATUS_DRAFT,
        )
        stmt = (
            stmt.where(Itinerary.user_id == user_id)
            if user_id is not None
            else stmt.where(Itinerary.anonymous_id == anonymous_id)
        )
        result = await self.session.execute(
            stmt.order_by(Itinerary.created_at.desc()).limit(limit)
        )
        return list(result.scalars().unique().all())

    # ------------------------------------------------------------------ persistence (kept)

    async def save(
        self,
        plan: Plan,
        request: PlanRequest,
        *,
        title: str,
        user_id: uuid.UUID | None,
        anonymous_id: str | None,
    ) -> Itinerary:
        """Persist a plan as a kept itinerary (legacy solver path).

        This path is kept for the old form-based flow and the AI concierge's
        accept path. New code should prefer create_draft / keep.
        """
        itinerary = Itinerary(
            user_id=user_id,
            anonymous_id=None if user_id else anonymous_id,
            title=title,
            city_slug=request.city_slug,
            starts_at=plan.stops[0].arrive_at if plan.stops else request.start,
            ends_at=plan.stops[-1].depart_at if plan.stops else request.end,
            estimated_cost=plan.total_cost,
            total_travel_minutes=plan.total_travel_minutes,
            status=STATUS_KEPT,
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
        status: str = STATUS_KEPT,
    ) -> Itinerary:
        """Persist a plan exactly as shown — outing ``stops`` or trip ``days``."""
        stops = _flatten_offered_stops(offered)
        if not stops:
            raise ValidationError("That plan has no stops to save.", code="EMPTY_PLAN")

        request = offered.get("request") or {}
        arrive_first = _parse(stops[0]["arriveAt"])
        depart_last = _parse(stops[-1]["departAt"])
        timezone = request.get("timezone") or "UTC"
        kind = _infer_plan_kind(
            arrive_first,
            depart_last,
            timezone=timezone,
            kind="trip" if offered.get("days") else "outing",
        )

        # Position is per-day; group first so numbering is correct.
        from collections import defaultdict

        by_day: dict[int, list[dict]] = defaultdict(list)
        for stop in stops:
            by_day[int(stop.get("dayIndex") or 0)].append(stop)

        total_travel = int(offered.get("totalTravelMinutes") or 0)
        if not total_travel:
            total_travel = sum(int(s.get("travelMinutes") or 0) for s in stops)

        itinerary = Itinerary(
            user_id=user_id,
            anonymous_id=None if user_id else anonymous_id,
            title=title[:200],
            city_slug=request.get("city"),
            starts_at=_parse(request["startsAt"]) if request.get("startsAt") else arrive_first,
            ends_at=_parse(request["endsAt"]) if request.get("endsAt") else depart_last,
            estimated_cost=offered.get("totalCost"),
            total_travel_minutes=total_travel,
            status=status,
            constraints={
                "budget": request.get("budget"),
                "maxStops": request.get("maxStops") or request.get("stopsPerDay"),
                "categories": request.get("categories") or [],
                "freeOnly": request.get("freeOnly", False),
                "window": [request.get("startsAt"), request.get("endsAt")],
                "origin": "concierge",
                "kind": kind,
                "timezone": timezone,
            },
            rationale=offered.get("rationale"),
            stops=[],
        )
        self.session.add(itinerary)
        await self.session.flush()

        for day_i in sorted(by_day.keys()):
            for position, stop in enumerate(by_day[day_i]):
                itinerary.stops.append(
                    ItineraryStop(
                        itinerary_id=itinerary.id,
                        day_index=day_i,
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
            kind=kind,
        )
        return itinerary

    async def get(
        self,
        itinerary_id: uuid.UUID,
        *,
        user_id: uuid.UUID | None,
        anonymous_id: str | None,
    ) -> Itinerary:
        """Load one itinerary the caller may see.

        Owners always can. Anyone holding the link can when the plan is kept and
        unlisted/public. Private plans answer 404 to strangers so guessing an id
        does not confirm the row exists.
        """
        itinerary = await self.session.get(Itinerary, itinerary_id)
        if itinerary is None or itinerary.deleted_at is not None:
            raise NotFoundError("Itinerary not found.", code="ITINERARY_NOT_FOUND")

        if _is_owner(itinerary, user_id=user_id, anonymous_id=anonymous_id):
            return itinerary
        if itinerary.is_shareable:
            return itinerary
        raise NotFoundError("Itinerary not found.", code="ITINERARY_NOT_FOUND")

    async def set_visibility(
        self,
        itinerary_id: uuid.UUID,
        visibility: str,
        *,
        user_id: uuid.UUID | None,
        anonymous_id: str | None,
    ) -> Itinerary:
        """Change who can open this plan by link. Owner only; kept plans only."""
        from app.domains.explorer.models import (
            VISIBILITY_PRIVATE,
            VISIBILITY_PUBLIC,
            VISIBILITY_UNLISTED,
        )

        allowed = {VISIBILITY_PRIVATE, VISIBILITY_UNLISTED, VISIBILITY_PUBLIC}
        if visibility not in allowed:
            raise ValidationError("Unknown visibility.", code="INVALID_VISIBILITY")

        itinerary = await self.get(itinerary_id, user_id=user_id, anonymous_id=anonymous_id)
        _require_editable(itinerary, user_id=user_id, anonymous_id=anonymous_id)
        if itinerary.status != STATUS_KEPT:
            raise ValidationError(
                "Keep this plan before sharing it.",
                code="PLAN_NOT_KEPT",
            )
        itinerary.visibility = visibility
        await self.session.flush()
        logger.info(
            "itinerary_visibility_set",
            itinerary_id=str(itinerary_id),
            visibility=visibility,
        )
        return itinerary

    async def list_for(
        self, *, user_id: uuid.UUID | None, anonymous_id: str | None, limit: int = 20
    ) -> list[Itinerary]:
        """List kept itineraries. Backward-compatible alias for list_kept."""
        return await self.list_kept(user_id=user_id, anonymous_id=anonymous_id, limit=limit)

    async def delete(
        self, itinerary_id: uuid.UUID, *, user_id: uuid.UUID | None, anonymous_id: str | None
    ) -> None:
        itinerary = await self.get(itinerary_id, user_id=user_id, anonymous_id=anonymous_id)
        _require_editable(itinerary, user_id=user_id, anonymous_id=anonymous_id)
        # Soft delete: an itinerary is the explorer's own record of an evening, and
        # spec BUSINESS-07's "nothing is deleted automatically" applies to their
        # history too.
        itinerary.deleted_at = datetime.now(UTC)
        if itinerary.user_id is not None:
            from app.domains.explorer.reminders import cancel_plan_reminders

            await cancel_plan_reminders(self.session, itinerary.id)


    # ------------------------------------------------------------------ private

    async def _load_experiences(
        self, experience_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, Experience]:
        """Load experiences with venue and events for planning analysis."""
        if not experience_ids:
            return {}
        result = await self.session.execute(
            select(Experience)
            .where(Experience.id.in_(experience_ids))
            .options(
                selectinload(Experience.venue),
                selectinload(Experience.events),
            )
        )
        return {exp.id: exp for exp in result.scalars().unique()}

    async def _load_single_experience(
        self, experience_id: uuid.UUID
    ) -> Experience | None:
        result = await self.session.execute(
            select(Experience)
            .where(Experience.id == experience_id)
            .options(
                selectinload(Experience.venue),
                selectinload(Experience.events),
            )
        )
        return result.scalar_one_or_none()


def _is_owner(
    itinerary: Itinerary,
    *,
    user_id: uuid.UUID | None,
    anonymous_id: str | None,
) -> bool:
    if user_id is not None:
        return itinerary.user_id == user_id
    return (
        itinerary.anonymous_id is not None
        and anonymous_id is not None
        and itinerary.anonymous_id == anonymous_id
    )


def _require_editable(
    itinerary: Itinerary,
    *,
    user_id: uuid.UUID | None = None,
    anonymous_id: str | None = None,
) -> None:
    """Raise if soft-deleted or the caller does not own the plan.

    Shared viewers may read via ``get``; they must not mutate. Ownership is
    checked whenever caller ids are supplied.
    """
    if itinerary.deleted_at is not None:
        raise NotFoundError("Itinerary not found.", code="ITINERARY_NOT_FOUND")
    if (user_id is not None or anonymous_id is not None) and not _is_owner(
        itinerary, user_id=user_id, anonymous_id=anonymous_id
    ):
        raise NotFoundError("Itinerary not found.", code="ITINERARY_NOT_FOUND")


def _require_draft(itinerary: Itinerary) -> None:
    """Raise if the itinerary is not a draft.

    Used only by `keep`, which promotes draft → kept. Mutations themselves use
    `_require_editable` so a kept plan can still be edited.
    """
    if itinerary.status != STATUS_DRAFT:
        raise ValidationError(
            "This plan has already been kept.",
            code="PLAN_NOT_DRAFT",
        )


def _money(value: object) -> float:
    """Coerce a Numeric/Decimal/None cost to float.

    SQLAlchemy returns ``Decimal`` for Numeric columns. Adding that to the
    float from ``_cost_of`` raises TypeError — the bug that broke Add Stop.
    Always go through here before cost arithmetic.
    """
    if value is None:
        return 0.0
    return float(value)  # type: ignore[arg-type]


def _parse(value: str) -> datetime:
    """Read a timestamp the platform itself wrote, defaulting to UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _flatten_offered_stops(offered: dict) -> list[dict]:
    """Normalise outing or trip offers into a flat stop list with dayIndex.

    Concierge ``plan_outing`` writes top-level ``stops``. ``plan_trip`` writes
    ``days[].stops``. Accept/open-draft both need one shape.
    """
    if offered.get("stops"):
        out = []
        for stop in offered["stops"]:
            row = dict(stop)
            row.setdefault("dayIndex", 0)
            out.append(row)
        return out

    days = offered.get("days") or []
    out = []
    for day_i, day in enumerate(days):
        for stop in day.get("stops") or []:
            row = dict(stop)
            row["dayIndex"] = int(day.get("dayIndex", day_i))
            out.append(row)
    return out


def _infer_plan_kind(
    starts_at: datetime,
    ends_at: datetime,
    *,
    timezone: str,
    kind: str | None,
) -> str:
    """Outing = one local calendar day; trip = more than one."""
    if kind in ("outing", "trip"):
        return kind
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    start_day = starts_at.astimezone(tz).date()
    end_day = ends_at.astimezone(tz).date()
    return "trip" if end_day > start_day else "outing"


def _day_anchor(itinerary: Itinerary, day_index: int) -> datetime:
    """Start time for the first stop of a day (local 09:00 clipped to window)."""
    from zoneinfo import ZoneInfo

    from app.domains.explorer.planning import ACTIVE_DAY_START_HOUR

    constraints = itinerary.constraints or {}
    tz_name = constraints.get("timezone") or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    base_local = itinerary.starts_at.astimezone(tz)
    day_date = base_local.date() + timedelta(days=day_index)
    local_start = datetime(
        day_date.year,
        day_date.month,
        day_date.day,
        ACTIVE_DAY_START_HOUR,
        0,
        0,
        tzinfo=tz,
    )
    # Never start before the trip window opens.
    window_start = itinerary.starts_at.astimezone(tz)
    if local_start < window_start:
        local_start = window_start
    return local_start.astimezone(UTC)


def _sorted_stops(stops: list) -> list:
    """Order by day then position — the canonical itinerary order."""
    return sorted(stops, key=lambda s: (int(getattr(s, "day_index", 0) or 0), s.position))


def _stops_on_day(stops: list, day_index: int) -> list:
    return [s for s in _sorted_stops(stops) if int(getattr(s, "day_index", 0) or 0) == day_index]
