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
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.logging import get_logger
from app.domains.catalog import suitability as suitability_vocab
from app.domains.catalog.models import Experience
from app.domains.discovery.ranking import RankingContext, haversine_km, rank
from app.integrations.weather import DailyWeather

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
# Manual builder allows more stops than the AI solver. Multi-day trips need
# headroom (e.g. 5 days × 4 stops) without inviting unbounded lists.
MAX_BUILDER_STOPS = 40
# Soft cap on calendar days in a manually built trip.
MAX_TRIP_DAYS = 14

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
    # The city's currency, carried so the plan can state a total in the money the
    # explorer will actually spend. It used to be the literal string "ETB"
    # wherever a plan mentioned a number, which was correct in exactly one
    # country and quietly wrong in every other - a Paris itinerary priced in birr.
    currency: str = "ETB"
    # Category slugs the plan should be built around, if any were stated.
    categories: list[str] = field(default_factory=list)
    free_only: bool = False
    # Claims every stop must have made - a vegan kitchen, step-free access. Hard,
    # and applied to the candidate pool before anything is ordered: a plan is a
    # promise about a whole evening, so one unusable stop ruins all of it rather
    # than costing a place in a list.
    required_suitability: list[str] = field(default_factory=list)
    # Claims that sort rather than exclude. These reach the ranker through the
    # context and need no handling here.
    preferred_suitability: list[str] = field(default_factory=list)
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
    # 0-based day within a multi-day trip; outings stay at 0.
    day_index: int = 0

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
    currency: str = "ETB"

    @property
    def is_empty(self) -> bool:
        return not self.stops


@dataclass(slots=True)
class TripRequest:
    """A stay of several days, rather than one evening.

    The single-day planner answers "plan my evening". It cannot answer "we are in
    Paris from the 14th to the 18th", and the difference is not just arithmetic:
    across days you must not repeat a stop, the budget is spent over the whole
    stay rather than per outing, and the weather is different on each day - which
    is the fact that decides which day the outdoor thing belongs on.
    """

    start: datetime
    end: datetime
    city_slug: str
    timezone: str = "UTC"
    latitude: float | None = None
    longitude: float | None = None
    # For the whole stay, not per day. Divided as the trip is built, so an
    # expensive first day genuinely leaves less for the rest instead of every day
    # quietly getting the full allowance.
    budget: float | None = None
    currency: str = "ETB"
    stops_per_day: int = 3
    categories: list[str] = field(default_factory=list)
    free_only: bool = False
    required_suitability: list[str] = field(default_factory=list)
    preferred_suitability: list[str] = field(default_factory=list)
    avoid_experience_ids: list[uuid.UUID] = field(default_factory=list)

    @property
    def nights(self) -> int:
        return max(0, (self.end.date() - self.start.date()).days)


@dataclass(slots=True)
class TripDay:
    day: date
    plan: Plan
    # The forecast this day was planned against, or None when it is past the
    # provider's horizon. Carried so the reply can say "planned for rain" or
    # admit that it could not see that far, rather than quietly implying it knew.
    weather: DailyWeather | None = None

    @property
    def is_empty(self) -> bool:
        return self.plan.is_empty


@dataclass(slots=True)
class Trip:
    days: list[TripDay]
    total_cost: float
    currency: str
    rationale: str
    unmet: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return all(day.is_empty for day in self.days)

    @property
    def planned_days(self) -> int:
        return sum(1 for day in self.days if not day.is_empty)


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
                    unmet=["The time window was under 45 minutes."],
                    currency=request.currency)

    # Hard requirements are applied before ranking rather than after. A stop the
    # explorer cannot use is not a low-scoring stop, it is not a candidate - and
    # leaving it in the pool to be out-ranked means it still appears whenever the
    # alternatives are thin, which is exactly when it does the most damage.
    if request.required_suitability:
        candidates = [
            experience
            for experience in candidates
            if suitability_vocab.assess(experience, request.required_suitability).is_fully_met
        ]

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
            currency=request.currency,
        )

    total_cost = sum(stop.estimated_cost for stop in stops)
    if request.budget is not None and total_cost > request.budget:
        stops, total_cost = _trim_to_budget(stops, request.budget)
        if not stops:
            return Plan(
                [], 0.0, 0,
                "Nothing in that budget fits the time you have.",
                unmet=[f"Budget of {request.budget:.0f} {request.currency} could not be met."],
                currency=request.currency,
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
        currency=request.currency,
    )


# The hours a day out is planned between, in the city's own clock. The start
# matches ACTIVE_DAY_START_HOUR, which already existed for the same reason; the
# end is late enough for dinner and a show and early enough that the planner does
# not schedule breakfast for 23:40 on the theory that the day has not ended.
ACTIVE_DAY_END_HOUR = 22


def build_trip(
    candidates: list[Experience],
    request: TripRequest,
    ctx: RankingContext,
    *,
    weather_by_day: dict[date, DailyWeather] | None = None,
) -> Trip:
    """Plan a stay of several days as a sequence of day plans.

    Days are built in order, and each one is planned against **its own** weather
    and against what the earlier days have already used up. Both matter:

    * **No repeats.** A stop chosen on Tuesday is excluded from Wednesday. Without
      this the same highest-ranked museum appears on all five days, because
      nothing about ranking one day knows another day happened.
    * **Its own forecast.** The ranker's `fit` signal reads `ctx.weather`, so
      rebuilding the context per day is the whole mechanism by which the outdoor
      market lands on the dry day and the gallery on the wet one. It is greedy
      rather than a global assignment - each day takes the best thing available to
      it - which is explainable and, when a five-day forecast has two wet days,
      gets the same answer a search would.
    * **One budget.** What a day spends is deducted before the next is planned.

    Beyond the forecast horizon `weather_by_day` simply has no entry, the day is
    planned with no weather signal, and :attr:`TripDay.weather` stays None so the
    reply can say so instead of implying knowledge it does not have.
    """
    weather_by_day = weather_by_day or {}

    try:
        zone = ZoneInfo(request.timezone)
    except Exception:  # noqa: BLE001 - an unknown zone must not break planning
        zone = UTC

    days: list[TripDay] = []
    used: set[uuid.UUID] = set(request.avoid_experience_ids)
    remaining_budget = request.budget
    unmet: list[str] = []

    for day in _dates_between(request.start, request.end, zone):
        window = _day_window(day, request, zone)
        if window is None:
            continue
        day_start, day_end = window

        forecast = weather_by_day.get(day)
        # A fresh context per day, because `weather` is the input that makes each
        # day's ranking different from the others'.
        day_ctx = replace(ctx, now=day_start, weather=forecast)

        day_request = PlanRequest(
            start=day_start,
            end=day_end,
            city_slug=request.city_slug,
            latitude=request.latitude,
            longitude=request.longitude,
            budget=remaining_budget,
            max_stops=request.stops_per_day,
            currency=request.currency,
            categories=request.categories,
            free_only=request.free_only,
            required_suitability=request.required_suitability,
            preferred_suitability=request.preferred_suitability,
            avoid_experience_ids=sorted(used, key=str),
        )

        plan = build_plan(candidates, day_request, day_ctx)
        days.append(TripDay(day=day, plan=plan, weather=forecast))

        for stop in plan.stops:
            used.add(stop.experience.id)
        if remaining_budget is not None:
            remaining_budget = max(0.0, remaining_budget - plan.total_cost)

    total_cost = round(sum(day.plan.total_cost for day in days), 2)
    empty_days = [day for day in days if day.is_empty]
    if empty_days and len(empty_days) < len(days):
        # Named rather than hidden. A blank Thursday in the middle of a trip is
        # something the explorer has to know about to do anything about.
        unmet.append(
            "Nothing in the catalogue fitted "
            + ", ".join(day.day.strftime("%a %d %b") for day in empty_days)
            + "."
        )

    return Trip(
        days=days,
        total_cost=total_cost,
        currency=request.currency,
        rationale=_explain_trip(days, request, total_cost),
        unmet=unmet,
    )


def _dates_between(start: datetime, end: datetime, zone: ZoneInfo) -> list[date]:
    """Every local calendar day the stay touches.

    Computed in the city's zone, not UTC. A trip landing at 22:00 local on the
    14th is on the 14th; in UTC it may already be the 15th, and a plan that
    silently skipped the arrival evening would be wrong in a way nobody would
    think to check.
    """
    first = start.astimezone(zone).date()
    last = end.astimezone(zone).date()
    if last < first:
        return []
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def _day_window(
    day: date, request: TripRequest, zone: ZoneInfo
) -> tuple[datetime, datetime] | None:
    """The planning window for one day of the stay.

    The active hours of that day, clipped to the trip itself, so the first day
    does not start before the explorer arrives and the last does not run past
    their flight. Returns None when the intersection is too short to plan in -
    an arrival at 23:00 gets no plan rather than a frantic one.
    """
    opens = datetime.combine(day, time(hour=ACTIVE_DAY_START_HOUR), tzinfo=zone)
    closes = datetime.combine(day, time(hour=ACTIVE_DAY_END_HOUR), tzinfo=zone)

    start = max(opens.astimezone(UTC), request.start)
    end = min(closes.astimezone(UTC), request.end)
    if (end - start) < timedelta(minutes=45):
        return None
    return start, end


def _explain_trip(days: list[TripDay], request: TripRequest, total_cost: float) -> str:
    planned = [day for day in days if not day.is_empty]
    if not planned:
        return "Nothing in the catalogue fitted those dates."

    stops = sum(len(day.plan.stops) for day in planned)
    parts = [
        f"{stops} stop{'s' if stops != 1 else ''} across "
        f"{len(planned)} day{'s' if len(planned) != 1 else ''}"
    ]

    # Naming the weather reasoning is the point of doing it. A plan that quietly
    # put the walking tour on the only dry day looks arbitrary; saying so is what
    # makes it read as a decision.
    #
    # Only claimed for days where it is actually true. A thin catalogue can leave
    # the planner choosing between an outdoor stop and an empty day, and it takes
    # the stop - so the wet days are not always all-indoor, and saying they are
    # would be the plan describing itself wrongly. That is worse than saying
    # nothing: the explorer packs for the sentence, not for the itinerary.
    sheltered = [
        day
        for day in planned
        if day.weather is not None
        and day.weather.is_wet
        and all(stop.experience.is_indoor for stop in day.plan.stops)
    ]
    if sheltered:
        parts.append(
            "with indoor stops on "
            + ", ".join(day.day.strftime("%a") for day in sheltered)
            + ", which look wet"
        )

    if total_cost == 0:
        parts.append("and nothing to pay")
    else:
        parts.append(f"and roughly {total_cost:.0f} {request.currency} in total")

    sentence = ", ".join(parts)
    return sentence[0].upper() + sentence[1:] + "."


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
        parts.append(f"and roughly {cost:.0f} {request.currency}")

    # Upper-case the first character only. str.capitalize() would lower-case the
    # rest, turning "Azmari Night" into "azmari night" and "ETB" into "etb".
    sentence = ", ".join(parts)
    return sentence[0].upper() + sentence[1:] + "."


# --------------- explorer-constructed plan analysis --------------------------
#
# When an explorer builds a plan manually (choosing stops, reordering, editing
# times), Mado validates rather than solves. `analyze_stops` is the function
# that runs on the explorer's order and times to find problems — without
# changing anything. Every conflict comes with explicit resolution options so
# the interface can present them rather than deciding for the explorer.


# A gap shorter than this is not worth surfacing as a fill opportunity: the
# explorer has left intentional breathing room, not a scheduling gap.
MIN_GAP_MINUTES = 30


@dataclass(slots=True)
class StopInput:
    """One stop from a draft itinerary, ready for analysis.

    Carries the experience (for coordinates and metadata) alongside the
    explorer's chosen timings, so the analysis can compare them against what
    the geometry allows.
    """

    experience: Experience
    event_instance_id: uuid.UUID | None
    is_fixed_time: bool
    arrive_at: datetime
    depart_at: datetime
    estimated_cost: float


@dataclass(slots=True)
class Conflict:
    """A scheduling problem found in an explorer-constructed plan.

    Every conflict names the stops involved, states what is wrong in plain
    language, and lists the resolutions the explorer can choose from. The
    interface presents these; nothing is changed without explicit acceptance.
    """

    kind: str  # "overlap" | "travel_gap" | "fixed_time_miss" | "window_overrun" | "budget_overrun"
    stop_indices: list[int]
    message: str
    # Resolution tokens shown as buttons in the conflict UI.
    resolutions: list[str]  # "move_stop" | "change_duration" | "remove_stop" | "keep_as_is"


@dataclass(slots=True)
class FreeGap:
    """A meaningful block of free time where a stop could be inserted.

    `after_index` of -1 means the gap is before the first stop (between the
    window start and the first arrival). An index equal to len(stops) means
    it is after the last stop — unlikely in a manual plan but surfaced so the
    explorer can ask "fill the end of my evening" explicitly.
    """

    after_index: int
    starts_at: datetime
    ends_at: datetime
    free_minutes: int


@dataclass(slots=True)
class Analysis:
    """Result of validating an explorer-constructed stop list.

    Does not change anything. The caller uses conflicts to decide what to show
    and gaps to decide where to offer a fill action.
    """

    conflicts: list[Conflict]
    gaps: list[FreeGap]
    total_cost: float
    total_travel_minutes: int
    budget_overrun: float  # 0 when none


def analyze_stops(
    stops: list[StopInput],
    window_start: datetime | None,
    window_end: datetime | None,
    origin: tuple[float, float] | None,
    budget: float | None = None,
) -> Analysis:
    """Validate an explorer-constructed stop list without re-solving.

    Each conflict names the problem and the resolution options so the interface
    can present them. Nothing is changed. The explorer stays in control of order
    and times.
    """
    conflicts: list[Conflict] = []
    gaps: list[FreeGap] = []
    total_cost = 0.0
    total_travel_minutes = 0

    for i, stop in enumerate(stops):
        total_cost += stop.estimated_cost

        # Determine the cursor: when does the previous stop release us?
        if i == 0:
            prev_depart: datetime | None = window_start
            prev_coords = origin
            # Gap before the first stop.
            if window_start is not None:
                free = int((stop.arrive_at - window_start).total_seconds() / 60)
                if free >= MIN_GAP_MINUTES:
                    gaps.append(
                        FreeGap(
                            after_index=-1,
                            starts_at=window_start,
                            ends_at=stop.arrive_at,
                            free_minutes=free,
                        )
                    )
        else:
            prev = stops[i - 1]
            prev_depart = prev.depart_at
            prev_coords = _coords(prev.experience)

        here = _coords(stop.experience)
        travel_minutes, _ = travel_estimate(prev_coords, here)
        total_travel_minutes += travel_minutes

        if prev_depart is not None:
            earliest = prev_depart + timedelta(minutes=travel_minutes)

            if stop.arrive_at < prev_depart:
                # The next stop begins before the previous one has even ended.
                conflicts.append(
                    Conflict(
                        kind="overlap",
                        stop_indices=[i - 1, i] if i > 0 else [i],
                        message=(
                            f"\u201c{stop.experience.title}\u201d starts before "
                            f"\u201c{stops[i-1].experience.title}\u201d finishes."
                            if i > 0
                            else (
                                f"\u201c{stop.experience.title}\u201d "
                                "starts before the window opens."
                            )
                        ),
                        resolutions=["move_stop", "change_duration", "remove_stop", "keep_as_is"],
                    )
                )
            elif stop.arrive_at < earliest:
                # Not enough time to travel between them.
                scheduled = int((stop.arrive_at - prev_depart).total_seconds() / 60)
                conflicts.append(
                    Conflict(
                        kind="travel_gap",
                        stop_indices=[i - 1, i] if i > 0 else [i],
                        message=(
                            f"About {travel_minutes} min of travel is needed before "
                            f"\u201c{stop.experience.title}\u201d, but only {scheduled} min "
                            "are scheduled."
                        ),
                        resolutions=["move_stop", "change_duration", "remove_stop", "keep_as_is"],
                    )
                )

        # Fixed-time events: verify the explorer can arrive before the event starts.
        if stop.is_fixed_time and stop.event_instance_id is not None:
            for event in stop.experience.events or []:
                if event.id == stop.event_instance_id:
                    event_start = event.start_time
                    if event_start.tzinfo is None:
                        event_start = event_start.replace(tzinfo=UTC)
                    if stop.arrive_at > event_start:
                        conflicts.append(
                            Conflict(
                                kind="fixed_time_miss",
                                stop_indices=[i],
                                message=(
                                    f"\u201c{stop.experience.title}\u201d starts at a fixed time "
                                    "but your arrival is scheduled after it begins."
                                ),
                                resolutions=["move_stop", "remove_stop", "keep_as_is"],
                            )
                        )
                    break

        # Window overrun: the stop finishes after the planned end.
        if window_end is not None and stop.depart_at > window_end:
            conflicts.append(
                Conflict(
                    kind="window_overrun",
                    stop_indices=[i],
                    message=(
                        f"\u201c{stop.experience.title}\u201d runs past your planned end time."
                    ),
                    resolutions=["change_duration", "remove_stop", "keep_as_is"],
                )
            )

        # Gap between this stop and the next (after accounting for travel to the next).
        if i < len(stops) - 1:
            next_stop = stops[i + 1]
            next_coords = _coords(next_stop.experience)
            next_travel, _ = travel_estimate(here, next_coords)
            free_start = stop.depart_at + timedelta(minutes=next_travel)
            free = int((next_stop.arrive_at - free_start).total_seconds() / 60)
            if free >= MIN_GAP_MINUTES:
                gaps.append(
                    FreeGap(
                        after_index=i,
                        starts_at=free_start,
                        ends_at=next_stop.arrive_at,
                        free_minutes=free,
                    )
                )
        elif window_end is not None:
            # Gap between the last stop and the end of the window.
            free = int((window_end - stop.depart_at).total_seconds() / 60)
            if free >= MIN_GAP_MINUTES:
                gaps.append(
                    FreeGap(
                        after_index=i,
                        starts_at=stop.depart_at,
                        ends_at=window_end,
                        free_minutes=free,
                    )
                )

    # Budget: report a single overrun rather than one per stop, because removing
    # a stop is what resolves it and the explorer picks which one.
    budget_overrun = 0.0
    if budget is not None and total_cost > budget:
        budget_overrun = round(total_cost - budget, 2)
        conflicts.append(
            Conflict(
                kind="budget_overrun",
                stop_indices=list(range(len(stops))),
                message=(
                    f"Estimated total exceeds your budget by {budget_overrun:.0f}."
                ),
                resolutions=["remove_stop", "keep_as_is"],
            )
        )

    return Analysis(
        conflicts=conflicts,
        gaps=gaps,
        total_cost=round(total_cost, 2),
        total_travel_minutes=total_travel_minutes,
        budget_overrun=budget_overrun,
    )


def compute_stop_times(
    stops: list[StopInput],
    window_start: datetime | None,
    origin: tuple[float, float] | None,
) -> list[tuple[datetime, datetime, int, float | None]]:
    """Compute (arrive_at, depart_at, travel_minutes, travel_km) for each stop.

    Used when the service assembles the draft from a full stop replacement
    and the client has not supplied explicit times. This assigns forward-only
    times (no backtrack, no re-solve): each stop arrives as soon as travel from
    the previous allows, using the draft window start or now as the origin.

    Returns one tuple per stop in the same order. Fixed-time stops use their
    event start where it is earlier than the computed earliest arrival.
    """
    cursor = window_start or datetime.now(UTC)
    previous = origin
    result: list[tuple[datetime, datetime, int, float | None]] = []

    for stop in stops:
        here = _coords(stop.experience)
        travel_minutes, travel_km = travel_estimate(previous, here)
        earliest = cursor + timedelta(minutes=travel_minutes)

        if stop.is_fixed_time and stop.event_instance_id is not None:
            # Find the scheduled start among the experience's events.
            event_start: datetime | None = None
            for event in stop.experience.events or []:
                if event.id == stop.event_instance_id:
                    event_start = event.start_time
                    if event_start.tzinfo is None:
                        event_start = event_start.replace(tzinfo=UTC)
                    break
            # Arrive at the event's start if we can make it, otherwise as early as possible.
            arrive = (
                event_start if (event_start is not None and event_start >= earliest) else earliest
            )
        else:
            arrive = earliest

        dwell = _dwell_minutes(stop.experience)
        depart = arrive + timedelta(minutes=dwell)

        result.append((arrive, depart, travel_minutes, travel_km))
        cursor = depart + timedelta(minutes=BUFFER_MINUTES)
        previous = here

    return result


def build_optimize_proposal(
    stops: list[StopInput],
    window_start: datetime,
    window_end: datetime,
    origin: tuple[float, float] | None,
    currency: str = "ETB",
) -> Plan:
    """Propose a reordering of the explorer's stops using 2-opt + retime.

    Returns a Plan containing the proposed sequence and times. Nothing is
    applied until the explorer explicitly accepts the proposal — the caller
    is responsible for presenting the diff and applying only on confirm.

    Fixed-time stops are never moved. If the proposed order is infeasible,
    the tail is trimmed rather than failing outright, so at minimum the
    fixed-time anchors are preserved.
    """
    # Convert StopInputs to PlannedStops with placeholder times for 2-opt.
    planned = [
        PlannedStop(
            experience=s.experience,
            arrive_at=s.arrive_at,
            depart_at=s.depart_at,
            travel_minutes=s.event_instance_id is not None and s.is_fixed_time and 0 or 0,
            travel_km=None,
            estimated_cost=s.estimated_cost,
            is_fixed_time=s.is_fixed_time,
            event_instance_id=s.event_instance_id,
        )
        for s in stops
    ]

    # Build a minimal PlanRequest so _two_opt and _retime can check feasibility.
    request = PlanRequest(
        start=window_start,
        end=window_end,
        city_slug="",  # not used by scheduling helpers
        latitude=origin[0] if origin else None,
        longitude=origin[1] if origin else None,
    )

    optimized = _two_opt(planned, request, origin)
    retimed = _retime(optimized, request, origin)

    if not retimed:
        return Plan([], 0.0, 0, "The proposed order could not be scheduled.", currency=currency)

    total_cost = round(sum(s.estimated_cost for s in retimed), 2)
    return Plan(
        stops=retimed,
        total_cost=total_cost,
        total_travel_minutes=sum(s.travel_minutes for s in retimed),
        rationale=_explain(retimed, request),
        currency=currency,
    )
