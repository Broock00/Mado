"""Planning engine tests.

A plan makes a promise the rest of the product does not: that you could actually
do this, in this order, in this time. Every test here defends that promise. The
ones that matter most are the negative cases - a planner that quietly returns an
itinerary you cannot follow is worse than one that admits it found nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.domains.discovery.ranking import RankingContext
from app.domains.explorer.planning import (
    MIN_TRAVEL_MINUTES,
    PlanRequest,
    build_plan,
    travel_estimate,
)

# Meskel Square, Addis Ababa.
CENTRE = (9.0107, 38.7614)
# Roughly 2.5 km away.
BOLE = (8.9998, 38.7810)
# Far enough to be a different trip.
DISTANT = (9.2000, 38.9500)


@dataclass
class FakeCategory:
    slug: str = "music"
    name: str = "Music"


@dataclass
class FakeNeighborhood:
    name: str = "Kazanchis"


@dataclass
class FakeVenue:
    latitude: float | None
    longitude: float | None
    name: str = "Venue"
    neighborhood: FakeNeighborhood = field(default_factory=FakeNeighborhood)


@dataclass
class FakeEvent:
    start_time: datetime
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    status: str = "scheduled"


@dataclass
class FakeExperience:
    title: str = "Somewhere"
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    type: str = "place"
    category: FakeCategory | None = field(default_factory=FakeCategory)
    tags: list = field(default_factory=list)
    venue: FakeVenue | None = None
    events: list = field(default_factory=list)
    price_type: str = "fixed"
    price_amount: float | None = 100.0
    duration_minutes: int | None = 60
    is_indoor: bool | None = None
    popularity_score: float = 0.5
    quality_score: float = 0.6
    trend_score: float = 0.0
    rating_average: float | None = None
    rating_count: int = 0
    publisher: object | None = None


def place(name: str, coords: tuple[float, float], **kwargs) -> FakeExperience:
    return FakeExperience(title=name, venue=FakeVenue(*coords), **kwargs)


def evening(hours: int = 5) -> tuple[datetime, datetime]:
    start = datetime(2026, 8, 7, 17, 0, tzinfo=UTC)
    return start, start + timedelta(hours=hours)


def request_for(start, end, **kwargs) -> PlanRequest:
    kwargs.setdefault("city_slug", "addis-ababa")
    kwargs.setdefault("latitude", CENTRE[0])
    kwargs.setdefault("longitude", CENTRE[1])
    return PlanRequest(start=start, end=end, **kwargs)


def ctx_at(now: datetime) -> RankingContext:
    return RankingContext(now=now, latitude=CENTRE[0], longitude=CENTRE[1])


# --- travel estimation -------------------------------------------------------


class TestTravelEstimate:
    def test_distance_drives_duration(self):
        near, _ = travel_estimate(CENTRE, BOLE)
        far, _ = travel_estimate(CENTRE, DISTANT)
        assert far > near

    def test_no_hop_is_instant(self):
        """Even next door costs a door, a street and finding the place."""
        minutes, _ = travel_estimate(CENTRE, (CENTRE[0] + 0.0001, CENTRE[1]))
        assert minutes >= MIN_TRAVEL_MINUTES

    def test_unknown_location_is_not_free(self):
        """Otherwise the planner stacks unlocated stops at zero cost."""
        minutes, km = travel_estimate(CENTRE, None)
        assert minutes >= MIN_TRAVEL_MINUTES
        assert km is None

    def test_estimate_exceeds_straight_line(self):
        """Roads are not crow flights, and underestimating makes plans run late."""
        minutes, km = travel_estimate(CENTRE, DISTANT)
        # 18 km/h over a >20km straight line cannot come in under an hour.
        assert minutes > 60
        assert km is not None


# --- feasibility: the core promise -------------------------------------------


class TestFeasibility:
    def test_stops_never_overlap(self):
        start, end = evening()
        pool = [place(f"Stop {i}", CENTRE) for i in range(6)]
        plan = build_plan(pool, request_for(start, end, max_stops=4), ctx_at(start))

        for earlier, later in zip(plan.stops, plan.stops[1:], strict=False):
            assert later.arrive_at >= earlier.depart_at

    def test_travel_time_is_actually_allowed_for(self):
        """Departure plus travel must never exceed the next arrival."""
        start, end = evening()
        pool = [place("A", CENTRE), place("B", BOLE), place("C", (9.03, 38.80))]
        plan = build_plan(pool, request_for(start, end, max_stops=3), ctx_at(start))

        for earlier, later in zip(plan.stops, plan.stops[1:], strict=False):
            gap = (later.arrive_at - earlier.depart_at).total_seconds() / 60
            assert gap >= later.travel_minutes

    def test_plan_stays_inside_the_window(self):
        start, end = evening(hours=3)
        pool = [place(f"Stop {i}", CENTRE) for i in range(6)]
        plan = build_plan(pool, request_for(start, end, max_stops=6), ctx_at(start))

        assert plan.stops
        assert plan.stops[0].arrive_at >= start
        assert plan.stops[-1].depart_at <= end

    def test_short_window_is_refused_rather_than_faked(self):
        start = datetime(2026, 8, 7, 17, 0, tzinfo=UTC)
        plan = build_plan(
            [place("A", CENTRE)],
            request_for(start, start + timedelta(minutes=20)),
            ctx_at(start),
        )
        assert plan.is_empty
        assert plan.unmet

    def test_empty_catalogue_yields_an_honest_empty_plan(self):
        start, end = evening()
        plan = build_plan([], request_for(start, end), ctx_at(start))
        assert plan.is_empty
        assert plan.unmet


# --- scheduled events are constraints, not suggestions -----------------------


class TestFixedTimeEvents:
    def test_event_is_scheduled_at_its_own_start_time(self):
        start, end = evening()
        concert_time = start + timedelta(hours=2)
        concert = place("Concert", BOLE, events=[FakeEvent(start_time=concert_time)])

        plan = build_plan(
            [concert, place("Cafe", CENTRE)],
            request_for(start, end, max_stops=2),
            ctx_at(start),
        )
        fixed = [s for s in plan.stops if s.is_fixed_time]
        assert fixed, "the scheduled event should have been anchored"
        assert fixed[0].arrive_at == concert_time

    def test_event_outside_the_window_is_not_anchored(self):
        start, end = evening()
        late = place("Tomorrow", CENTRE, events=[FakeEvent(start_time=end + timedelta(days=1))])
        plan = build_plan([late], request_for(start, end), ctx_at(start))
        assert all(not stop.is_fixed_time for stop in plan.stops)

    def test_cancelled_occurrence_is_ignored(self):
        start, end = evening()
        cancelled = place(
            "Cancelled",
            CENTRE,
            events=[FakeEvent(start_time=start + timedelta(hours=1), status="cancelled")],
        )
        plan = build_plan([cancelled], request_for(start, end), ctx_at(start))
        assert all(not stop.is_fixed_time for stop in plan.stops)

    def test_plan_arrives_before_a_fixed_event_starts(self):
        """You may wait for a concert; you may not turn up after it began."""
        start, end = evening()
        concert_time = start + timedelta(hours=3)
        pool = [
            place("Concert", BOLE, events=[FakeEvent(start_time=concert_time)]),
            place("Dinner", CENTRE, duration_minutes=90),
        ]
        plan = build_plan(pool, request_for(start, end, max_stops=2), ctx_at(start))

        for index, stop in enumerate(plan.stops):
            if stop.is_fixed_time and index > 0:
                previous = plan.stops[index - 1]
                arrival = previous.depart_at + timedelta(minutes=stop.travel_minutes)
                assert arrival <= stop.arrive_at


# --- budget ------------------------------------------------------------------


class TestBudget:
    def test_plan_respects_the_budget(self):
        start, end = evening()
        pool = [place(f"Pricey {i}", CENTRE, price_amount=400.0) for i in range(4)]
        plan = build_plan(
            pool, request_for(start, end, max_stops=4, budget=500.0), ctx_at(start)
        )
        assert plan.total_cost <= 500.0

    def test_trimming_for_budget_is_disclosed(self):
        start, end = evening()
        pool = [place(f"Pricey {i}", CENTRE, price_amount=400.0) for i in range(4)]
        plan = build_plan(
            pool, request_for(start, end, max_stops=4, budget=500.0), ctx_at(start)
        )
        assert any("budget" in reason.lower() for reason in plan.unmet)

    def test_free_only_excludes_paid_stops(self):
        start, end = evening()
        pool = [
            place("Free thing", CENTRE, price_type="free", price_amount=None),
            place("Paid thing", CENTRE, price_amount=300.0),
        ]
        plan = build_plan(
            pool, request_for(start, end, max_stops=2, free_only=True), ctx_at(start)
        )
        assert plan.stops
        assert all(stop.estimated_cost == 0 for stop in plan.stops)

    def test_free_plan_costs_nothing(self):
        start, end = evening()
        pool = [place(f"Free {i}", CENTRE, price_type="free", price_amount=None) for i in range(3)]
        plan = build_plan(pool, request_for(start, end, max_stops=3), ctx_at(start))
        assert plan.total_cost == 0.0


# --- geography ---------------------------------------------------------------


class TestGeography:
    def test_plan_does_not_sprawl_across_the_region(self):
        start, end = evening()
        pool = [place("Near", CENTRE), place("Miles away", DISTANT)]
        plan = build_plan(pool, request_for(start, end, max_stops=2), ctx_at(start))

        titles = [stop.experience.title for stop in plan.stops]
        assert "Miles away" not in titles

    def test_nearby_options_are_preferred_over_distant_ones(self):
        start, end = evening()
        far_but_close_enough = (9.08, 38.83)
        pool = [place("Far", far_but_close_enough), place("Close", CENTRE)]
        plan = build_plan(pool, request_for(start, end, max_stops=1), ctx_at(start))
        assert plan.stops[0].experience.title == "Close"

    def test_unlocated_venue_does_not_crash_the_planner(self):
        start, end = evening()
        pool = [FakeExperience(title="No venue", venue=None), place("Located", CENTRE)]
        plan = build_plan(pool, request_for(start, end, max_stops=2), ctx_at(start))
        assert plan.stops  # produced something rather than raising


# --- output contract ---------------------------------------------------------


class TestPlanOutput:
    def test_stop_count_is_capped_by_the_request(self):
        start, end = evening(hours=8)
        pool = [place(f"Stop {i}", CENTRE) for i in range(10)]
        plan = build_plan(pool, request_for(start, end, max_stops=3), ctx_at(start))
        assert len(plan.stops) <= 3

    def test_no_experience_appears_twice(self):
        start, end = evening(hours=8)
        pool = [place(f"Stop {i}", CENTRE) for i in range(5)]
        plan = build_plan(pool, request_for(start, end, max_stops=5), ctx_at(start))
        ids = [stop.experience.id for stop in plan.stops]
        assert len(ids) == len(set(ids))

    def test_plan_explains_itself(self):
        """Spec PRODUCT-00 principle 5 applies to plans, not only to cards."""
        start, end = evening()
        plan = build_plan([place("A", CENTRE)], request_for(start, end), ctx_at(start))
        assert plan.rationale
        assert plan.rationale.endswith(".")

    def test_shortfall_against_the_request_is_stated(self):
        start, end = evening(hours=2)
        pool = [place("Only one", CENTRE, duration_minutes=90)]
        plan = build_plan(pool, request_for(start, end, max_stops=4), ctx_at(start))
        assert plan.unmet, "asking for 4 stops and getting 1 must be explained"

    def test_travel_total_matches_the_stops(self):
        start, end = evening()
        pool = [place("A", CENTRE), place("B", BOLE)]
        plan = build_plan(pool, request_for(start, end, max_stops=2), ctx_at(start))
        assert plan.total_travel_minutes == sum(s.travel_minutes for s in plan.stops)

    def test_dwell_time_follows_the_listing(self):
        start, end = evening()
        pool = [place("Long lunch", CENTRE, duration_minutes=120)]
        plan = build_plan(pool, request_for(start, end, max_stops=1), ctx_at(start))
        assert plan.stops[0].dwell_minutes == 120


class TestWakingHours:
    """A whole-day window must not start a plan at midnight.

    Regression cover: "plan me a day out tomorrow" parses to 00:00-23:59, which is
    correct for searching and produced an itinerary beginning at 00:05.
    """

    def test_whole_day_window_starts_at_a_plausible_hour(self):
        # Midnight to midnight, Addis local (UTC+3), expressed in UTC.
        start = datetime(2026, 8, 7, 21, 0, tzinfo=UTC)  # 00:00 next day in Addis
        end = start + timedelta(hours=24)
        pool = [place(f"Stop {i}", CENTRE) for i in range(4)]

        plan = build_plan(pool, request_for(start, end, max_stops=2), ctx_at(start))
        assert plan.stops

        local_hour = plan.stops[0].arrive_at.astimezone(
            ZoneInfo("Africa/Addis_Ababa")
        ).hour
        assert local_hour >= 9, f"plan started at {local_hour}:00 local"

    def test_a_deliberately_early_window_is_respected(self):
        """Only long windows are clamped - a chosen early slot is a real request."""
        start = datetime(2026, 8, 7, 3, 0, tzinfo=UTC)  # 06:00 Addis
        end = start + timedelta(hours=3)
        pool = [place("Sunrise walk", CENTRE, duration_minutes=60)]

        plan = build_plan(pool, request_for(start, end, max_stops=1), ctx_at(start))
        assert plan.stops
        assert plan.stops[0].arrive_at < datetime(2026, 8, 7, 6, 0, tzinfo=UTC)

    def test_clamping_never_inverts_the_window(self):
        """A long window ending before 09:00 local must not become negative."""
        start = datetime(2026, 8, 7, 19, 0, tzinfo=UTC)  # 22:00 Addis
        end = start + timedelta(hours=9)  # 07:00 Addis next day
        pool = [place("Late night", CENTRE, duration_minutes=60)]
        plan = build_plan(pool, request_for(start, end, max_stops=1), ctx_at(start))
        # Either a plan or an honest refusal - but never a crash or an inverted window.
        assert plan.stops or plan.unmet
