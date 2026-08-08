"""The planning engine.

Turns "plan me an evening in Piassa under 500 birr" into an ordered, timed,
feasible sequence of stops. This is the piece spec 10.01.04 calls the Journey
Planner, and the thing that makes Mado a planner rather than a list of listings.

**The problem.** Choose a subset of candidate experiences and an order for them,
maximising how good the evening is, subject to: a time window, travel time between
stops, fixed start times for scheduled events, opening reality, and a budget. That
is the Orienteering Problem with time windows - NP-hard, and no exact solver
belongs on a request path.

**The approach.** Greedy insertion against a value-per-cost ratio, followed by a
local improvement pass:

1. **Anchor the fixed points.** A concert at 20:00 happens at 20:00; it is a
   constraint, not a preference. Scheduled events are placed first, in time order,
   and everything else is fitted around them.
2. **Fill the gaps greedily.** Each remaining candidate is scored by how much it
   adds relative to the time it consumes - dwell plus the *detour* it creates,
   not its raw distance. A great venue that happens to sit between two stops is
   nearly free; the same venue across the city is not.
3. **Improve locally.** A 2-opt pass over the movable stops removes the crossings
   greedy insertion characteristically leaves behind, without disturbing the
   anchors.

Greedy-then-improve is chosen over a fuller search deliberately. It is fast enough
to run inline, it degrades gracefully (a partial plan is still useful), and every
decision it makes can be explained to the explorer - which a black-box optimiser's
output cannot be, and spec PRODUCT-00 principle 5 requires.

**Travel** is estimated, not routed. A straight-line distance with a city-speed
factor and a detour allowance is honest about its own precision; calling a routing
API per candidate pair would cost hundreds of requests to plan one evening. The
estimate is deliberately conservative, because a plan that runs late is worse than
one with slack in it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.logging import get_logger
from app.domains.catalog.models import Experience
from app.domains.discovery.ranking import RankingContext, haversine_km, rank

logger = get_logger("mado.planning")

# Average door-to-door speed across a dense city, in km/h. Well below a vehicle's
# cruising speed because it has to absorb traffic, parking and the walk at each
# end - the parts of a journey that actually make people late.
CITY_SPEED_KMH = 18.0

# Straight-line distance underestimates road distance. Roughly the ratio of street
# network distance to crow-flight in a city that is not laid out on a grid.
ROUTE_DETOUR_FACTOR = 1.35

# No hop is instant, even between neighbours: there is always a door, a street and
# a moment of finding the place.
MIN_TRAVEL_MINUTES = 5

# Slack left after each stop. A plan packed to the minute is a plan that fails on
# contact with a slow bill or a queue.
BUFFER_MINUTES = 10

# Fallback dwell when a listing does not declare one.
DEFAULT_DWELL_MINUTES = 75
# Beyond this, a single stop is eating the whole evening.
MAX_DWELL_MINUTES = 240

# A plan longer than this stops being a plan and becomes a schedule.
MAX_STOPS = 6

# How far a plan may wander. Past this, "an evening out" has become a road trip.
MAX_SPREAD_KM = 15.0

# The earliest hour a plan may start, in the city's own clock. "Tomorrow" is a
# perfectly good search window meaning all of tomorrow, but as a *plan* it produced
# itineraries beginning at 00:05, because midnight is where the day technically
# starts. Nobody plans a day out from midnight.
ACTIVE_DAY_START_HOUR = 9

# Only applied to windows long enough to be a whole day. A short window is one the
# explorer chose deliberately - if someone asks for a plan between 06:00 and 08:00,
# that is a request, not an artefact of how "tomorrow" was parsed.
LONG_WINDOW_HOURS = 8


@dataclass(slots=True)
class PlanRequest:
    """What the explorer asked for."""

    start: datetime
    end: datetime
    city_slug: str
    latitude: float | None = None
    longitude: float | None = None
    budget: float | None = None
    max_stops: int = 4
    # Category slugs the plan should be built around, if any were stated.
    categories: list[str] = field(default_factory=list)
    free_only: bool = False
    # Refinement (spec AI-004). Experiences the explorer already accepted and
    # ones they rejected.
    #
    # `keep` is what makes refining feel like refining. Asked to make an evening
    # cheaper, a planner that re-solves from scratch hands back a different
    # evening - and the two stops the explorer had already agreed to are gone
    # for no reason they can see. Keeping them is not an optimisation, it is
    # the difference between a conversation and a slot machine.
    keep_experience_ids: list[uuid.UUID] = field(default_factory=list)
    avoid_experience_ids: list[uuid.UUID] = field(default_factory=list)

    @property
    def available_minutes(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds() / 60.0)


@dataclass(slots=True)
class PlannedStop:
    experience: Experience
    arrive_at: datetime
    depart_at: datetime
    travel_minutes: int
    travel_km: float | None
    estimated_cost: float
    is_fixed_time: bool
    event_instance_id: uuid.UUID | None = None
    note: str | None = None

    @property
    def dwell_minutes(self) -> int:
        return int((self.depart_at - self.arrive_at).total_seconds() / 60)


@dataclass(slots=True)
class Plan:
    stops: list[PlannedStop]
    total_cost: float
    total_travel_minutes: int
    rationale: str
    # Constraints that could not be met, stated plainly rather than silently
    # dropped. An explorer who asked for four stops and got two deserves to know
    # which limit bit (spec PRODUCT-00 principle 5).
    unmet: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.stops


# --- geometry and timing -----------------------------------------------------


def _coords(experience: Experience) -> tuple[float, float] | None:
    venue = experience.venue
    if venue is None or venue.latitude is None or venue.longitude is None:
        return None
    return (venue.latitude, venue.longitude)


def travel_estimate(
    origin: tuple[float, float] | None, destination: tuple[float, float] | None
) -> tuple[int, float | None]:
    """Estimated ``(minutes, km)`` between two points.

    Returns the minimum hop when either end is unknown rather than zero: an
    unlocated venue still takes time to reach, and treating it as free would let
    the planner stack unlocated stops without limit.
    """
    if origin is None or destination is None:
        return MIN_TRAVEL_MINUTES, None

    straight = haversine_km(origin[0], origin[1], destination[0], destination[1])
    road_km = straight * ROUTE_DETOUR_FACTOR
    minutes = (road_km / CITY_SPEED_KMH) * 60.0
    return max(MIN_TRAVEL_MINUTES, int(round(minutes))), round(straight, 2)


def _dwell_minutes(experience: Experience) -> int:
    declared = experience.duration_minutes
    if not declared:
        return DEFAULT_DWELL_MINUTES
    return max(20, min(MAX_DWELL_MINUTES, int(declared)))


def _cost_of(experience: Experience) -> float:
    if experience.price_type == "free":
        return 0.0
    return float(experience.price_amount or 0.0)


def _fixed_start(experience: Experience, request: PlanRequest) -> tuple[datetime, uuid.UUID] | None:
    """The scheduled start inside the window, if this is a time-bound experience.

    An Event is an Experience with occurrences (the domain taxonomy), so this is
    what distinguishes a constraint from a choice: a place can be visited whenever,
    an event cannot.
    """
    best: tuple[datetime, uuid.UUID] | None = None
    for occurrence in experience.events or []:
        if occurrence.status != "scheduled":
            continue
        start = occurrence.start_time
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if request.start <= start <= request.end and (best is None or start < best[0]):
            best = (start, occurrence.id)
    return best


# --- the planner -------------------------------------------------------------


def build_plan(
    candidates: list[Experience],
    request: PlanRequest,
    ctx: RankingContext,
) -> Plan:
    """Assemble the best feasible itinerary from a candidate pool.

    ``candidates`` should already be a retrieved, relevant pool - the planner
    decides ordering and feasibility, not what is worth considering. Quality
    scoring is delegated to the ranker so a plan and a feed agree about what is
    good, rather than each having its own private opinion.
    """
    request = _clamp_to_waking_hours(request, ctx.timezone)

    if request.available_minutes < 45:
        return Plan([], 0.0, 0, "That window is too short to plan around.",
                    unmet=["The time window was under 45 minutes."])

    scored = rank(candidates, ctx, diversify=False)
    value_by_id = {str(item.experience.id): item.score for item in scored}
    pool = [item.experience for item in scored]

    # Rejected outright. "Not that one" has to be honoured even if the ranker
    # still thinks it is the best thing in the city - especially then.
    if request.avoid_experience_ids:
        avoided = set(request.avoid_experience_ids)
        pool = [exp for exp in pool if exp.id not in avoided]

    if request.free_only:
        # A kept stop survives a filter it would otherwise fail. The explorer
        # accepted it knowing what it cost; dropping it while answering "make
        # the rest cheaper" would be obeying the letter of the request and
        # ignoring the point.
        kept = set(request.keep_experience_ids)
        pool = [exp for exp in pool if exp.price_type == "free" or exp.id in kept]
    if request.categories:
        wanted = set(request.categories)
        preferred = [e for e in pool if e.category and e.category.slug in wanted]
        # Requested categories lead, but the rest stay available: an evening built
        # only from one category is usually worse than one anchored in it.
        pool = preferred + [e for e in pool if e not in preferred]

    limit = max(1, min(MAX_STOPS, request.max_stops))
    origin = (
        (request.latitude, request.longitude)
        if request.latitude is not None and request.longitude is not None
        else None
    )

    unmet: list[str] = []
    stops = _place_anchors(pool, request, limit, value_by_id)
    stops = _place_kept(stops, pool, request, limit, origin)
    stops = _fill_gaps(stops, pool, request, limit, value_by_id, origin, unmet)
    stops = _two_opt(stops, request, origin)
    stops = _retime(stops, request, origin)

    if not stops:
        return Plan(
            [], 0.0, 0,
            "Nothing in the catalogue fits that window.",
            unmet=unmet or ["No candidate fitted the time window."],
        )

    total_cost = sum(stop.estimated_cost for stop in stops)
    if request.budget is not None and total_cost > request.budget:
        stops, total_cost = _trim_to_budget(stops, request.budget)
        if not stops:
            return Plan(
                [], 0.0, 0,
                "Nothing in that budget fits the time you have.",
                unmet=[f"Budget of {request.budget:.0f} could not be met."],
            )
        unmet.append("Some stops were dropped to stay inside the budget.")
        stops = _retime(stops, request, origin)

    if len(stops) < request.max_stops:
        unmet.append(
            f"Fitted {len(stops)} of the {request.max_stops} stops you asked for - "
            "the rest would not fit the time or the distance."
        )

    return Plan(
        stops=stops,
        total_cost=round(total_cost, 2),
        total_travel_minutes=sum(stop.travel_minutes for stop in stops),
        rationale=_explain(stops, request),
        unmet=unmet,
    )


def _clamp_to_waking_hours(request: PlanRequest, timezone: str) -> PlanRequest:
    """Move a whole-day window's start to a plausible hour, in the city's clock.

    Applies only to long windows. "Tomorrow" parses to 00:00-23:59, which is right
    for *searching* - things do happen at 01:00 - but wrong for planning, where it
    put breakfast at five past midnight. A window the explorer picked deliberately
    is left exactly as asked.
    """
    if (request.end - request.start) < timedelta(hours=LONG_WINDOW_HOURS):
        return request

    try:
        zone = ZoneInfo(timezone)
    except Exception:  # noqa: BLE001 - an unknown zone must not break planning
        return request

    local_start = request.start.astimezone(zone)
    if local_start.hour >= ACTIVE_DAY_START_HOUR:
        return request

    adjusted = local_start.replace(
        hour=ACTIVE_DAY_START_HOUR, minute=0, second=0, microsecond=0
    ).astimezone(UTC)
    if adjusted >= request.end:
        return request

    return replace(request, start=adjusted)


def _place_anchors(
    pool: list[Experience],
    request: PlanRequest,
    limit: int,
    value_by_id: dict[str, float],
) -> list[PlannedStop]:
    """Seat the scheduled events first - they are the only immovable stops."""
    anchors: list[PlannedStop] = []
    for experience in pool:
        if len(anchors) >= limit:
            break
        fixed = _fixed_start(experience, request)
        if fixed is None:
            continue
        start, occurrence_id = fixed
        dwell = _dwell_minutes(experience)
        end = start + timedelta(minutes=dwell)
        if end > request.end:
            continue
        if any(_overlaps(start, end, stop) for stop in anchors):
            continue
        anchors.append(
            PlannedStop(
                experience=experience,
                arrive_at=start,
                depart_at=end,
                travel_minutes=0,
                travel_km=None,
                estimated_cost=_cost_of(experience),
                is_fixed_time=True,
                event_instance_id=occurrence_id,
                note="Starts at a set time",
            )
        )
    anchors.sort(key=lambda stop: stop.arrive_at)
    return anchors


def _place_kept(
    stops: list[PlannedStop],
    pool: list[Experience],
    request: PlanRequest,
    limit: int,
    origin: tuple[float, float] | None,
) -> list[PlannedStop]:
    """Seat the stops the explorer already agreed to, before anything else competes.

    Placed after the fixed-time anchors and before gap filling, which is the only
    order that works. Anchors cannot move at all - a concert starts when it
    starts - so they win any conflict. Everything else is discretionary, and a
    stop the explorer has already said yes to should outrank one the ranker
    merely likes.

    Kept stops are given provisional times here and properly timed by `_retime`
    along with everything else, so a refined plan is internally consistent rather
    than a new plan with old timings pasted in.
    """
    if not request.keep_experience_ids:
        return stops

    seated = {stop.experience.id for stop in stops}
    by_id = {experience.id: experience for experience in pool}

    # In the order the explorer saw them, not the order the ranker prefers.
    for experience_id in request.keep_experience_ids:
        if len(stops) >= limit or experience_id in seated:
            continue
        experience = by_id.get(experience_id)
        if experience is None:
            # It fell out of the candidate pool - unpublished, withheld, or the
            # event has passed. Silently dropping it is right; `unmet` in the
            # caller reports the shortfall.
            continue

        fixed = _fixed_start(experience, request)
        if fixed is not None:
            start, occurrence_id = fixed
        else:
            start, occurrence_id = request.start, None

        stops.append(
            PlannedStop(
                experience=experience,
                arrive_at=start,
                depart_at=start + timedelta(minutes=_dwell_minutes(experience)),
                travel_minutes=0,
                travel_km=None,
                estimated_cost=_cost_of(experience),
                is_fixed_time=fixed is not None,
                event_instance_id=occurrence_id,
                note="Kept from your plan",
            )
        )
        seated.add(experience_id)

    stops.sort(key=lambda stop: stop.arrive_at)
    return _retime(stops, request, origin)


def _overlaps(start: datetime, end: datetime, stop: PlannedStop) -> bool:
    return start < stop.depart_at and end > stop.arrive_at


def _fill_gaps(
    stops: list[PlannedStop],
    pool: list[Experience],
    request: PlanRequest,
    limit: int,
    value_by_id: dict[str, float],
    origin: tuple[float, float] | None,
    unmet: list[str],
) -> list[PlannedStop]:
    """Insert flexible stops where they fit, best value-per-minute first.

    Cost is the *marginal* time an insertion adds - its dwell plus the extra
    travel it creates at that position - not its standalone distance. A venue
    between two existing stops is nearly free; the same venue as a detour is not,
    and scoring on raw distance cannot tell those apart.
    """
    chosen_ids = {stop.experience.id for stop in stops}

    while len(stops) < limit:
        best: tuple[float, int, Experience, int, float | None] | None = None

        for experience in pool:
            if experience.id in chosen_ids:
                continue
            if _fixed_start(experience, request) is not None:
                # Time-bound: it was either anchored already or does not fit.
                continue

            value = value_by_id.get(str(experience.id), 0.0)
            dwell = _dwell_minutes(experience)

            for position in range(len(stops) + 1):
                added, travel_minutes, travel_km = _insertion_cost(
                    stops, position, experience, dwell, origin
                )
                if added is None:
                    continue
                if not _fits(stops, position, experience, dwell, travel_minutes, request, origin):
                    continue
                if _spread_exceeded(stops, experience, origin):
                    continue

                ratio = value / max(1.0, added)
                if best is None or ratio > best[0]:
                    best = (ratio, position, experience, travel_minutes, travel_km)

        if best is None:
            break

        _, position, experience, travel_minutes, travel_km = best
        stops.insert(
            position,
            PlannedStop(
                experience=experience,
                # Placeholder times; _retime assigns the real schedule once the
                # order is final. Computing them here would only be thrown away.
                arrive_at=request.start,
                depart_at=request.start + timedelta(minutes=_dwell_minutes(experience)),
                travel_minutes=travel_minutes,
                travel_km=travel_km,
                estimated_cost=_cost_of(experience),
                is_fixed_time=False,
            ),
        )
        chosen_ids.add(experience.id)

    return stops


def _insertion_cost(
    stops: list[PlannedStop],
    position: int,
    experience: Experience,
    dwell: int,
    origin: tuple[float, float] | None,
) -> tuple[float | None, int, float | None]:
    """Marginal minutes of inserting ``experience`` at ``position``."""
    here = _coords(experience)
    before = _coords(stops[position - 1].experience) if position > 0 else origin
    after = _coords(stops[position].experience) if position < len(stops) else None

    to_minutes, to_km = travel_estimate(before, here)
    if after is None:
        return to_minutes + dwell + BUFFER_MINUTES, to_minutes, to_km

    from_minutes, _ = travel_estimate(here, after)
    direct_minutes, _ = travel_estimate(before, after)
    # Only the detour counts: the leg being replaced was already being paid for.
    detour = max(0, to_minutes + from_minutes - direct_minutes)
    return detour + dwell + BUFFER_MINUTES, to_minutes, to_km


def _fits(
    stops: list[PlannedStop],
    position: int,
    experience: Experience,
    dwell: int,
    travel_minutes: int,
    request: PlanRequest,
    origin: tuple[float, float] | None,
) -> bool:
    """Whether inserting here still leaves every anchor reachable on time."""
    trial = list(stops)
    trial.insert(
        position,
        PlannedStop(
            experience=experience,
            arrive_at=request.start,
            depart_at=request.start + timedelta(minutes=dwell),
            travel_minutes=travel_minutes,
            travel_km=None,
            estimated_cost=_cost_of(experience),
            is_fixed_time=False,
        ),
    )
    return _schedule(trial, request, origin) is not None


def _spread_exceeded(
    stops: list[PlannedStop], experience: Experience, origin: tuple[float, float] | None
) -> bool:
    """Reject a stop that stretches the plan beyond a plausible night out."""
    here = _coords(experience)
    if here is None:
        return False
    points = [c for c in (_coords(s.experience) for s in stops) if c is not None]
    if origin is not None:
        points.append(origin)
    return any(haversine_km(here[0], here[1], p[0], p[1]) > MAX_SPREAD_KM for p in points)


def _schedule(
    stops: list[PlannedStop],
    request: PlanRequest,
    origin: tuple[float, float] | None,
) -> list[PlannedStop] | None:
    """Assign arrival and departure times, or None if the order is infeasible.

    A single forward pass: each stop starts when you can actually get there, and a
    fixed-time stop must not have been missed by the time you arrive. Returning
    None rather than a late plan is the point - a plan that cannot be followed is
    worse than a shorter one that can.
    """
    scheduled: list[PlannedStop] = []
    cursor = request.start
    previous = origin

    for stop in stops:
        here = _coords(stop.experience)
        travel_minutes, travel_km = travel_estimate(previous, here)
        earliest = cursor + timedelta(minutes=travel_minutes)

        if stop.is_fixed_time:
            # You may wait for a scheduled event, but you cannot arrive after it
            # has started.
            if earliest > stop.arrive_at:
                return None
            arrive = stop.arrive_at
        else:
            arrive = earliest

        dwell = _dwell_minutes(stop.experience)
        depart = arrive + timedelta(minutes=dwell)
        if depart > request.end:
            return None

        scheduled.append(
            PlannedStop(
                experience=stop.experience,
                arrive_at=arrive,
                depart_at=depart,
                travel_minutes=travel_minutes if scheduled or origin is not None else 0,
                travel_km=travel_km,
                estimated_cost=stop.estimated_cost,
                is_fixed_time=stop.is_fixed_time,
                event_instance_id=stop.event_instance_id,
                note=stop.note,
            )
        )
        cursor = depart + timedelta(minutes=BUFFER_MINUTES)
        previous = here

    return scheduled


def _retime(
    stops: list[PlannedStop], request: PlanRequest, origin: tuple[float, float] | None
) -> list[PlannedStop]:
    """Schedule the final order, dropping the tail if it will not fit."""
    scheduled = _schedule(stops, request, origin)
    while scheduled is None and stops:
        # Drop the last movable stop and retry rather than failing outright: a
        # shorter plan that works beats no plan at all.
        for index in range(len(stops) - 1, -1, -1):
            if not stops[index].is_fixed_time:
                stops.pop(index)
                break
        else:
            stops.pop()
        scheduled = _schedule(stops, request, origin)
    return scheduled or []


def _two_opt(
    stops: list[PlannedStop], request: PlanRequest, origin: tuple[float, float] | None
) -> list[PlannedStop]:
    """Remove route crossings left behind by greedy insertion.

    Classic 2-opt: reverse each segment in turn, keep the reversal if it shortens
    total travel and is still feasible. Fixed-time stops are not moved - reordering
    around a 20:00 concert would only produce infeasible candidates, and checking
    them wastes the pass.
    """
    if len(stops) < 3:
        return stops

    best = stops
    best_travel = _total_travel(best, origin)
    improved = True

    while improved:
        improved = False
        for i in range(len(best) - 1):
            for j in range(i + 1, len(best)):
                if any(stop.is_fixed_time for stop in best[i : j + 1]):
                    continue
                candidate = best[:i] + best[i : j + 1][::-1] + best[j + 1 :]
                travel = _total_travel(candidate, origin)
                if travel < best_travel and _schedule(candidate, request, origin) is not None:
                    best, best_travel, improved = candidate, travel, True
    return best


def _total_travel(stops: list[PlannedStop], origin: tuple[float, float] | None) -> int:
    total = 0
    previous = origin
    for stop in stops:
        here = _coords(stop.experience)
        minutes, _ = travel_estimate(previous, here)
        total += minutes
        previous = here
    return total


def _trim_to_budget(
    stops: list[PlannedStop], budget: float
) -> tuple[list[PlannedStop], float]:
    """Drop the worst value-for-money stops until the plan is affordable.

    Cheapest-first would gut the evening of everything memorable; this drops the
    most expensive movable stop each round, which preserves the shape of the plan
    while bringing the total down.
    """
    kept = list(stops)
    total = sum(stop.estimated_cost for stop in kept)

    while total > budget and kept:
        movable = [stop for stop in kept if not stop.is_fixed_time]
        target = max(movable or kept, key=lambda stop: stop.estimated_cost)
        if target.estimated_cost == 0:
            # Everything left is free and it still does not fit; the budget cannot
            # be the reason, so stop rather than emptying the plan.
            break
        kept.remove(target)
        total -= target.estimated_cost

    return kept, total


def _explain(stops: list[PlannedStop], request: PlanRequest) -> str:
    """State why the plan looks like this, in plain language."""
    if not stops:
        return "Nothing fitted that window."

    parts = [f"{len(stops)} stop{'s' if len(stops) > 1 else ''}"]
    anchored = [stop for stop in stops if stop.is_fixed_time]
    if anchored:
        first = anchored[0]
        parts.append(f"built around {first.experience.title}, which starts at a set time")

    travel = sum(stop.travel_minutes for stop in stops)
    parts.append(f"about {travel} minutes of travel in total")

    cost = sum(stop.estimated_cost for stop in stops)
    if cost == 0:
        parts.append("and nothing to pay")
    else:
        parts.append(f"and roughly {cost:.0f} ETB")

    # Upper-case the first character only. str.capitalize() would lower-case the
    # rest, turning "Azmari Night" into "azmari night" and "ETB" into "etb".
    sentence = ", ".join(parts)
    return sentence[0].upper() + sentence[1:] + "."
