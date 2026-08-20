"""Multi-day trips, suitability constraints and weather.

These cover the three things that turned "plan my evening" into "we are in Paris
next week, my father is vegan and my son feels the cold". Each has a failure mode
that a "does it return a plan" test passes straight through:

* a trip that repeats the same museum on all five days
* a constraint that ranks a step-free venue higher but still returns the stairs
* weather that is quietly treated as fine because nobody could see that far

So the assertions here are mostly about what is *absent* from a plan.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import pytest

from app.domains.catalog import suitability
from app.domains.discovery.ranking import RankingContext, score_experience
from app.domains.explorer.planning import (
    PlanRequest,
    TripRequest,
    build_plan,
    build_trip,
)
from app.integrations.weather import (
    CLEAR,
    RAIN,
    DailyWeather,
)

CENTRE = (48.8566, 2.3522)  # Paris


@dataclass
class FakeCategory:
    slug: str = "food"
    name: str = "Food"


@dataclass
class FakeVenue:
    latitude: float | None = CENTRE[0]
    longitude: float | None = CENTRE[1]
    name: str = "Venue"
    neighborhood: object | None = None
    facilities: list[str] = field(default_factory=list)


@dataclass
class FakeExperience:
    title: str = "Somewhere"
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    type: str = "place"
    category: FakeCategory | None = field(default_factory=FakeCategory)
    tags: list = field(default_factory=list)
    venue: FakeVenue | None = field(default_factory=FakeVenue)
    events: list = field(default_factory=list)
    price_type: str = "free"
    price_amount: float | None = None
    duration_minutes: int | None = 60
    is_indoor: bool | None = None
    suitability: list[str] = field(default_factory=list)
    popularity_score: float = 0.5
    quality_score: float = 0.6
    trend_score: float = 0.0
    rating_average: float | None = None
    rating_count: int = 0
    publisher: object | None = None


def spread(count: int, **kwargs) -> list[FakeExperience]:
    """Distinct listings a few hundred metres apart, so travel is not the binding
    constraint and a thin plan means the planner chose to make it thin."""
    return [
        FakeExperience(
            title=f"Place {index}",
            venue=FakeVenue(
                latitude=CENTRE[0] + index * 0.002, longitude=CENTRE[1] + index * 0.002
            ),
            **kwargs,
        )
        for index in range(count)
    ]


def ctx_at(now: datetime, **kwargs) -> RankingContext:
    return RankingContext(now=now, latitude=CENTRE[0], longitude=CENTRE[1], **kwargs)


def trip_request(days: int = 3, **kwargs) -> TripRequest:
    start = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)
    kwargs.setdefault("city_slug", "paris")
    kwargs.setdefault("latitude", CENTRE[0])
    kwargs.setdefault("longitude", CENTRE[1])
    kwargs.setdefault("currency", "EUR")
    return TripRequest(start=start, end=start + timedelta(days=days - 1, hours=12), **kwargs)


def forecast(day: date, condition: str, high: float, wet: float = 0.0) -> DailyWeather:
    return DailyWeather(
        day=day,
        condition=condition,
        temperature_max_c=high,
        temperature_min_c=high - 6,
        precipitation_probability=wet,
        provider="test",
    )


# --- multi-day ---------------------------------------------------------------


class TestTripSpansDays:
    def test_each_day_gets_its_own_plan(self):
        trip = build_trip(spread(20), trip_request(days=3), ctx_at(datetime.now(UTC)))
        assert len(trip.days) == 3
        assert trip.planned_days == 3

    def test_a_stop_is_never_repeated_across_days(self):
        """The failure this exists for: the best-ranked place on all five days."""
        trip = build_trip(spread(20), trip_request(days=4), ctx_at(datetime.now(UTC)))
        seen = [stop.experience.id for day in trip.days for stop in day.plan.stops]
        assert len(seen) == len(set(seen))

    def test_a_thin_catalogue_runs_out_rather_than_repeating(self):
        """Two listings cannot fill four days, and inventing a third is worse."""
        trip = build_trip(spread(2), trip_request(days=4), ctx_at(datetime.now(UTC)))
        seen = [stop.experience.id for day in trip.days for stop in day.plan.stops]
        assert len(seen) == len(set(seen))
        assert len(seen) <= 2
        assert trip.unmet, "an unfillable day must be reported, not left blank in silence"

    def test_budget_is_spent_across_the_whole_trip(self):
        paid = spread(20, price_type="fixed", price_amount=40.0)
        trip = build_trip(paid, trip_request(days=3, budget=100.0), ctx_at(datetime.now(UTC)))
        assert trip.total_cost <= 100.0

    def test_days_are_local_calendar_days(self):
        """A late arrival still belongs to the day it lands on, in the city's clock."""
        request = trip_request(days=1)
        trip = build_trip(spread(10), request, ctx_at(datetime.now(UTC)))
        assert [day.day for day in trip.days] == [date(2026, 9, 7)]

    def test_currency_follows_the_request(self):
        trip = build_trip(
            spread(10, price_type="fixed", price_amount=10.0),
            trip_request(days=2),
            ctx_at(datetime.now(UTC)),
        )
        assert trip.currency == "EUR"
        assert "ETB" not in trip.rationale


# --- weather -----------------------------------------------------------------


class TestWeatherShapesTheTrip:
    def test_indoor_stops_land_on_the_wet_day(self):
        indoor = spread(8, is_indoor=True)
        outdoor = spread(8, is_indoor=False)
        request = trip_request(days=2, stops_per_day=2)

        wet_day, dry_day = date(2026, 9, 7), date(2026, 9, 8)
        trip = build_trip(
            indoor + outdoor,
            request,
            ctx_at(datetime.now(UTC)),
            weather_by_day={
                wet_day: forecast(wet_day, RAIN, 16.0, wet=0.9),
                dry_day: forecast(dry_day, CLEAR, 24.0),
            },
        )

        by_day = {day.day: day for day in trip.days}
        wet_indoor = [s.experience.is_indoor for s in by_day[wet_day].plan.stops]
        # `all([])` is True, so an empty day would pass the assertion below while
        # testing nothing at all - which is exactly the vacuous-pass this suite is
        # supposed to catch elsewhere.
        assert wet_indoor, "no stops were planned for the wet day; the test proves nothing"
        assert all(wet_indoor), "an outdoor stop was scheduled into a 90% chance of rain"

    def test_the_rationale_names_the_weather_reasoning(self):
        wet_day = date(2026, 9, 7)
        trip = build_trip(
            spread(10, is_indoor=True),
            trip_request(days=1),
            ctx_at(datetime.now(UTC)),
            weather_by_day={wet_day: forecast(wet_day, RAIN, 14.0, wet=0.8)},
        )
        assert "wet" in trip.rationale.lower()

    def test_it_does_not_claim_indoor_stops_it_did_not_make(self):
        """Caught end to end: the rationale said "indoor stops on Wed" about a day
        holding an outdoor stop, because a thin catalogue left the planner
        choosing between that and an empty day. It takes the stop - which is
        right - so the sentence has to stop claiming otherwise."""
        wet_day = date(2026, 9, 7)
        trip = build_trip(
            spread(6, is_indoor=False),
            trip_request(days=1),
            ctx_at(datetime.now(UTC)),
            weather_by_day={wet_day: forecast(wet_day, RAIN, 15.0, wet=0.9)},
        )
        assert trip.days[0].plan.stops, "the planner should still fill the day"
        assert "indoor" not in trip.rationale.lower()

    def test_a_day_past_the_horizon_carries_no_forecast(self):
        """The whole point of the design: unknown must not read as fine."""
        trip = build_trip(spread(10), trip_request(days=2), ctx_at(datetime.now(UTC)))
        assert all(day.weather is None for day in trip.days)

    def test_cold_favours_indoors_even_when_dry(self):
        day = date(2026, 9, 7)
        cold = forecast(day, CLEAR, 4.0)
        indoor = score_experience(
            FakeExperience(is_indoor=True), ctx_at(datetime.now(UTC), weather=cold)
        )
        outdoor = score_experience(
            FakeExperience(is_indoor=False), ctx_at(datetime.now(UTC), weather=cold)
        )
        assert indoor.signals["fit"] > outdoor.signals["fit"]

    def test_shelter_offsets_the_penalty_it_actually_answers(self):
        """A heated terrace is not an open field on a cold day - but shade is."""
        day = date(2026, 9, 7)
        cold = ctx_at(datetime.now(UTC), weather=forecast(day, CLEAR, 3.0))

        heated = score_experience(
            FakeExperience(is_indoor=False, suitability=["heated"]), cold
        )
        shaded = score_experience(
            FakeExperience(is_indoor=False, suitability=["shaded_seating"]), cold
        )
        assert heated.signals["fit"] > shaded.signals["fit"]


# --- suitability -------------------------------------------------------------


class TestSuitabilityConstrains:
    def test_a_requirement_excludes_listings_that_never_claimed_it(self):
        """Unknown is not a maybe. This is the safety property."""
        claimed = spread(4, suitability=["vegan"])
        silent = spread(6)
        plan = build_plan(
            claimed + silent,
            PlanRequest(
                start=datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
                end=datetime(2026, 9, 7, 20, 0, tzinfo=UTC),
                city_slug="paris",
                required_suitability=["vegan"],
                max_stops=6,
            ),
            ctx_at(datetime(2026, 9, 7, 12, 0, tzinfo=UTC)),
        )
        assert plan.stops
        for stop in plan.stops:
            assert "vegan" in suitability.effective(stop.experience)

    def test_a_venue_claim_satisfies_the_requirement(self):
        """The play area belongs to the building, not to tonight's performance."""
        experience = FakeExperience(
            venue=FakeVenue(facilities=["childrens_play_area"]),
        )
        assessment = suitability.assess(experience, ["childrens_play_area"])
        assert assessment.is_fully_met

    def test_vegan_satisfies_a_vegetarian_request_but_not_the_reverse(self):
        vegan = FakeExperience(suitability=["vegan"])
        vegetarian = FakeExperience(suitability=["vegetarian"])
        assert suitability.assess(vegan, ["vegetarian"]).is_fully_met
        assert not suitability.assess(vegetarian, ["vegan"]).is_fully_met

    def test_unmet_claims_are_reported_as_unknown_not_denied(self):
        assessment = suitability.assess(FakeExperience(), ["halal"])
        assert assessment.missing == {"halal"}
        assert not assessment.conflicts
        assert not assessment.is_safe, "an unverified safety claim must not read as safe"

    def test_a_preference_sorts_without_excluding(self):
        matching = score_experience(
            FakeExperience(suitability=["childrens_play_area"]),
            ctx_at(datetime.now(UTC), preferred_suitability={"childrens_play_area"}),
        )
        silent = score_experience(
            FakeExperience(),
            ctx_at(datetime.now(UTC), preferred_suitability={"childrens_play_area"}),
        )
        assert matching.signals["fit"] > silent.signals["fit"]

    def test_the_reason_names_what_was_asked_for(self):
        scored = score_experience(
            FakeExperience(suitability=["step_free_access"]),
            ctx_at(datetime.now(UTC), required_suitability={"step_free_access"}),
        )
        assert "step-free access" in (scored.reason or "")

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (["Vegan"], ["vegan"]),
            (["step-free access"], ["step_free_access"]),
            (["vegann"], []),
            (None, []),
        ],
    )
    def test_normalisation_drops_what_it_cannot_query(self, raw, expected):
        assert suitability.normalise(raw) == expected


class TestVocabularyDoesNotDrift:
    def test_the_prompt_lists_exactly_the_slugs_that_exist(self):
        """The comprehension prompt names the vocabulary so the model can use it.

        Two copies of one list, in different files, is a drift waiting to happen -
        and it fails quietly in both directions. A slug added to the module but not
        the prompt is one the model never emits; a slug in the prompt but not the
        module is dropped by `normalise`, so the model is asked for something that
        can never take effect. Neither shows up as an error anywhere.
        """
        from app.domains.ai.prompts import UNDERSTANDING_SYSTEM_PROMPT

        block = UNDERSTANDING_SYSTEM_PROMPT.split(
            "Both suitability lists are drawn from this vocabulary and nothing else:"
        )[1].split("Which list something goes in")[0]

        named = {word.strip(" ,\n") for word in block.replace("\n", " ").split()}
        named = {word for word in named if word}

        assert named == suitability.ALL

    def test_every_slug_has_wording_for_a_sentence(self):
        missing = {slug for slug in suitability.ALL if slug not in suitability.LABELS}
        assert not missing, f"no human wording for {sorted(missing)}"
