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
    StopInput,
    analyze_stops,
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


# ---------------------------------------------------------------------------
# analyze_stops - the validation engine for explorer-constructed plans
# ---------------------------------------------------------------------------

def _stop_input(
    name, coords, arrive_offset_min, dwell=60, cost=0.0, is_fixed=False, window_start=None
):
    """Build a StopInput with times relative to an evening window start."""
    ws = window_start or datetime(2026, 8, 7, 17, 0, tzinfo=UTC)
    arrive = ws + timedelta(minutes=arrive_offset_min)
    depart = arrive + timedelta(minutes=dwell)
    return StopInput(
        experience=place(name, coords, price_amount=cost),
        event_instance_id=None,
        is_fixed_time=is_fixed,
        arrive_at=arrive,
        depart_at=depart,
        estimated_cost=cost,
    )


class TestAnalyzeStops:
    """analyze_stops validates an explorer-ordered list and returns conflicts / gaps
    without mutating anything. The core contract: describe, never decide."""

    def _ws(self):
        return datetime(2026, 8, 7, 17, 0, tzinfo=UTC)

    def _we(self, hours=5):
        return self._ws() + timedelta(hours=hours)

    def test_clean_plan_has_no_conflicts(self):
        """A plan that fits comfortably should produce no conflicts.

        First stop starts 15 minutes in so there is enough time to travel from
        the origin. Second stop starts 90 minutes after the first begins (first
        departs at 75 min), leaving a comfortable travel gap.
        """
        stops = [
            _stop_input("A", CENTRE, 15, dwell=60),
            _stop_input("B", BOLE, 120, dwell=60),
        ]
        result = analyze_stops(stops, self._ws(), self._we(), origin=CENTRE)
        assert result.conflicts == [], result.conflicts

    def test_overlapping_stops_produce_conflict(self):
        """Stop B starting before stop A finishes is an overlap conflict."""
        stops = [
            _stop_input("A", CENTRE, 0, dwell=120),
            _stop_input("B", CENTRE, 60, dwell=60),  # starts while A still running
        ]
        result = analyze_stops(stops, self._ws(), self._we(), origin=CENTRE)
        kinds = [c.kind for c in result.conflicts]
        assert "overlap" in kinds

    def test_impossible_travel_produces_travel_gap_conflict(self):
        """Back-to-back stops with zero gap when travel takes >0 minutes is a conflict."""
        stops = [
            _stop_input("A", CENTRE, 0, dwell=60),
            # B starts exactly when A ends but is 2.5 km away - travel is nonzero.
            _stop_input("B", BOLE, 60, dwell=60),
        ]
        result = analyze_stops(stops, self._ws(), self._we(), origin=CENTRE)
        kinds = [c.kind for c in result.conflicts]
        assert "travel_gap" in kinds

    def test_budget_overrun_is_reported(self):
        stops = [
            _stop_input("Expensive A", CENTRE, 0, cost=300.0),
            _stop_input("Expensive B", CENTRE, 90, cost=300.0),
        ]
        result = analyze_stops(stops, self._ws(), self._we(), origin=CENTRE, budget=400.0)
        assert result.budget_overrun > 0
        kinds = [c.kind for c in result.conflicts]
        assert "budget_overrun" in kinds

    def test_no_budget_conflict_when_within_budget(self):
        stops = [
            _stop_input("A", CENTRE, 0, cost=50.0),
            _stop_input("B", CENTRE, 90, cost=50.0),
        ]
        result = analyze_stops(stops, self._ws(), self._we(), origin=CENTRE, budget=500.0)
        kinds = [c.kind for c in result.conflicts]
        assert "budget_overrun" not in kinds

    def test_free_gap_detected_between_stops(self):
        """A meaningful free slot between two stops should surface as a gap."""
        stops = [
            _stop_input("Morning", CENTRE, 0, dwell=60),
            _stop_input("Afternoon", CENTRE, 180, dwell=60),  # 2h gap after first
        ]
        result = analyze_stops(stops, self._ws(), self._we(), origin=CENTRE)
        assert len(result.gaps) >= 1
        assert any(g.free_minutes >= 60 for g in result.gaps)

    def test_empty_stop_list_produces_no_conflicts(self):
        """An empty plan is valid - no stops, no conflicts."""
        result = analyze_stops([], self._ws(), self._we(), origin=CENTRE)
        assert result.conflicts == []
        assert result.gaps == []
        assert result.total_cost == 0.0

    def test_single_stop_no_travel_conflict(self):
        """A single stop cannot conflict with itself."""
        stops = [_stop_input("Solo", CENTRE, 30, dwell=60)]
        result = analyze_stops(stops, self._ws(), self._we(), origin=CENTRE)
        assert not any(c.kind == "travel_gap" for c in result.conflicts)

    def test_conflict_names_involved_stop_indices(self):
        """Overlap conflict must reference the indices of the two stops involved."""
        stops = [
            _stop_input("A", CENTRE, 0, dwell=120),
            _stop_input("B", CENTRE, 60, dwell=60),
        ]
        result = analyze_stops(stops, self._ws(), self._we(), origin=CENTRE)
        overlap = next(c for c in result.conflicts if c.kind == "overlap")
        assert 0 in overlap.stop_indices
        assert 1 in overlap.stop_indices

    def test_each_conflict_has_resolutions(self):
        """Every conflict must offer at least one resolution the explorer can act on."""
        stops = [
            _stop_input("A", CENTRE, 0, dwell=120),
            _stop_input("B", CENTRE, 60, dwell=60),
        ]
        result = analyze_stops(stops, self._ws(), self._we(), origin=CENTRE)
        for c in result.conflicts:
            assert c.resolutions, f"Conflict {c.kind!r} has no resolutions"
            # "keep_as_is" is mandatory: Mado identifies conflicts, the explorer decides.
            assert "keep_as_is" in c.resolutions

    def test_analyze_does_not_mutate_stop_inputs(self):
        """The function must be a pure read; no field on input should change."""
        ws = datetime(2026, 8, 7, 17, 0, tzinfo=UTC)
        stops = [_stop_input("A", CENTRE, 0, dwell=60)]
        arrive_before = stops[0].arrive_at
        depart_before = stops[0].depart_at
        analyze_stops(stops, ws, ws + timedelta(hours=5), origin=CENTRE)
        assert stops[0].arrive_at == arrive_before
        assert stops[0].depart_at == depart_before

# ---------------------------------------------------------------------------
# Numeric cost coercion (Decimal from Postgres Numeric columns)
# ---------------------------------------------------------------------------

class TestMoneyCoercion:
    """Regression: Add Stop crashed when adding Decimal + float."""

    def test_decimal_plus_float_via_money_helper(self):
        from decimal import Decimal

        from app.domains.explorer.planning_service import _money

        assert _money(Decimal("12.50")) + 3.0 == 15.5
        assert _money(None) + 3.0 == 3.0
        assert _money(0) + 3.0 == 3.0

    def test_old_wrap_float_around_sum_still_raises(self):
        """Documents the bug: casting the *sum* is too late."""
        from decimal import Decimal

        import pytest

        with pytest.raises(TypeError):
            float((Decimal("12.50") or 0) + 3.0)

class TestMultiAreaTravel:
    """Builder allows stops across continents; travel_km must fit the column."""

    def test_intercontinental_travel_fits_column(self):
        # Addis Ababa -> Tokyo. Used to overflow Numeric(6,2) (max 9999.99).
        minutes, km = travel_estimate(CENTRE, (35.6812, 139.7671))
        assert km is not None
        assert km > 9999.99, "sanity: this hop must exceed the old column"
        assert km < 10**6, "must fit Numeric(8,2)"
        assert minutes > 0


class TestMultiDayBuilder:
    """V2: day_index grouping, trip inference, flatten offers."""

    def test_infer_outing_same_local_day(self):
        from app.domains.explorer.planning_service import _infer_plan_kind

        start = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)
        end = datetime(2026, 9, 19, 22, 0, tzinfo=UTC)
        assert _infer_plan_kind(start, end, timezone="UTC", kind=None) == "outing"

    def test_infer_trip_spans_local_days(self):
        from app.domains.explorer.planning_service import _infer_plan_kind

        start = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)
        end = datetime(2026, 9, 21, 22, 0, tzinfo=UTC)
        assert _infer_plan_kind(start, end, timezone="UTC", kind=None) == "trip"

    def test_explicit_kind_overrides_span(self):
        from app.domains.explorer.planning_service import _infer_plan_kind

        start = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)
        end = datetime(2026, 9, 21, 22, 0, tzinfo=UTC)
        assert _infer_plan_kind(start, end, timezone="UTC", kind="outing") == "outing"

    def test_flatten_outing_sets_day_zero(self):
        from app.domains.explorer.planning_service import _flatten_offered_stops

        offered = {
            "stops": [
                {"experienceId": "a", "arriveAt": "2026-09-19T09:00:00Z"},
                {"experienceId": "b", "arriveAt": "2026-09-19T11:00:00Z"},
            ]
        }
        flat = _flatten_offered_stops(offered)
        assert [s["dayIndex"] for s in flat] == [0, 0]

    def test_flatten_trip_preserves_day_index(self):
        from app.domains.explorer.planning_service import _flatten_offered_stops

        offered = {
            "days": [
                {
                    "dayIndex": 0,
                    "stops": [{"experienceId": "a", "arriveAt": "2026-09-19T09:00:00Z"}],
                },
                {
                    "dayIndex": 1,
                    "stops": [{"experienceId": "b", "arriveAt": "2026-09-20T09:00:00Z"}],
                },
            ]
        }
        flat = _flatten_offered_stops(offered)
        assert [s["experienceId"] for s in flat] == ["a", "b"]
        assert [s["dayIndex"] for s in flat] == [0, 1]

    def test_sorted_stops_orders_by_day_then_position(self):
        from types import SimpleNamespace

        from app.domains.explorer.planning_service import _sorted_stops

        stops = [
            SimpleNamespace(day_index=1, position=0),
            SimpleNamespace(day_index=0, position=1),
            SimpleNamespace(day_index=0, position=0),
        ]
        ordered = _sorted_stops(stops)
        assert [(s.day_index, s.position) for s in ordered] == [(0, 0), (0, 1), (1, 0)]

    def test_analyze_does_not_flag_cross_day_travel(self):
        """Two stops on different days must not invent overnight travel conflict."""
        day0 = StopInput(
            experience=place("Cafe", CENTRE),
            event_instance_id=None,
            is_fixed_time=False,
            arrive_at=datetime(2026, 9, 19, 10, 0, tzinfo=UTC),
            depart_at=datetime(2026, 9, 19, 11, 0, tzinfo=UTC),
            estimated_cost=0.0,
        )
        # Far away, next morning — would be an impossible hop if checked as one day.
        day1 = StopInput(
            experience=place("Museum", DISTANT),
            event_instance_id=None,
            is_fixed_time=False,
            arrive_at=datetime(2026, 9, 20, 10, 0, tzinfo=UTC),
            depart_at=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
            estimated_cost=0.0,
        )
        a0 = analyze_stops(
            [day0],
            datetime(2026, 9, 19, 9, 0, tzinfo=UTC),
            datetime(2026, 9, 19, 22, 0, tzinfo=UTC),
            None,
        )
        a1 = analyze_stops(
            [day1],
            datetime(2026, 9, 20, 9, 0, tzinfo=UTC),
            datetime(2026, 9, 20, 22, 0, tzinfo=UTC),
            None,
        )
        assert a0.conflicts == []
        assert a1.conflicts == []

    def test_max_trip_days_constant(self):
        from app.domains.explorer.planning import MAX_BUILDER_STOPS, MAX_TRIP_DAYS

        assert MAX_TRIP_DAYS == 14
        assert MAX_BUILDER_STOPS == 40
