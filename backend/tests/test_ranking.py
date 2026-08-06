"""Ranking and time-window tests.

These cover the behaviours the specs are explicit about and that are easy to
regress silently: that context outweighs popularity, that explanations match the
signal that actually fired, and that "tonight" / "this weekend" mean what an
explorer means by them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from app.domains.discovery.ranking import (
    RankingContext,
    apply_diversity,
    haversine_km,
    rank,
    score_experience,
    tonight_window,
    weekend_window,
)

# --- lightweight stand-ins ---------------------------------------------------
# The ranker only reads attributes, so plain objects avoid needing a database for
# what is pure scoring logic.


@dataclass
class FakeCategory:
    slug: str
    name: str


@dataclass
class FakeTag:
    slug: str
    name: str


@dataclass
class FakeNeighborhood:
    name: str = "Kazanchis"


@dataclass
class FakeVenue:
    latitude: float
    longitude: float
    neighborhood: FakeNeighborhood = field(default_factory=FakeNeighborhood)


@dataclass
class FakeEvent:
    start_time: datetime
    status: str = "scheduled"


@dataclass
class FakeExperience:
    id: str = "exp-1"
    type: str = "place"
    title: str = "Test experience"
    category: FakeCategory | None = None
    tags: list = field(default_factory=list)
    venue: FakeVenue | None = None
    events: list = field(default_factory=list)
    price_type: str = "fixed"
    is_indoor: bool | None = None
    popularity_score: float = 0.5
    quality_score: float = 0.5
    trend_score: float = 0.0
    rating_average: float | None = None
    rating_count: int = 0
    publisher: object | None = None


# Meskel Square, Addis Ababa.
CENTRE = (9.0107, 38.7614)


def ctx_at(lat: float | None = None, lng: float | None = None, **kwargs) -> RankingContext:
    return RankingContext(now=datetime.now(UTC), latitude=lat, longitude=lng, **kwargs)


class TestDistance:
    def test_known_separation(self):
        # Meskel Square to Bole Medhanialem is roughly 2.5 km.
        distance = haversine_km(9.0107, 38.7614, 8.9998, 38.7810)
        assert 2.0 < distance < 3.0

    def test_identical_points_are_zero(self):
        assert haversine_km(9.0, 38.0, 9.0, 38.0) == pytest.approx(0.0)


class TestProximityBeatsPopularity:
    def test_near_unpopular_outranks_far_popular(self):
        """Spec PRODUCT-00 principle 4: context beats popularity."""
        near = FakeExperience(
            id="near",
            popularity_score=0.1,
            venue=FakeVenue(latitude=CENTRE[0], longitude=CENTRE[1]),
        )
        far = FakeExperience(
            id="far",
            popularity_score=1.0,
            # ~9 km away, still inside the horizon so it is not zeroed outright.
            venue=FakeVenue(latitude=9.09, longitude=38.76),
        )
        ranked = rank([far, near], ctx_at(*CENTRE), diversify=False)
        assert ranked[0].experience.id == "near"

    def test_without_location_proximity_is_neutral(self):
        """No location must not penalise everything - it should simply not apply."""
        experience = FakeExperience(venue=FakeVenue(latitude=9.0, longitude=38.7))
        scored = score_experience(experience, ctx_at())
        assert scored.signals["proximity"] == 0.5
        assert scored.distance_km is None


class TestTimingSignal:
    def test_imminent_event_scores_above_distant_one(self):
        now = datetime.now(UTC)
        soon = FakeExperience(id="soon", type="event", events=[FakeEvent(now + timedelta(hours=2))])
        later = FakeExperience(
            id="later", type="event", events=[FakeEvent(now + timedelta(days=20))]
        )
        assert (
            score_experience(soon, ctx_at()).signals["timing"]
            > score_experience(later, ctx_at()).signals["timing"]
        )

    def test_permanent_place_is_not_penalised_for_having_no_event(self):
        """A museum has no start time; that is not staleness."""
        place = score_experience(FakeExperience(type="place"), ctx_at())
        stale_event = score_experience(FakeExperience(type="event", events=[]), ctx_at())
        assert place.signals["timing"] > stale_event.signals["timing"]


class TestPersonalization:
    def test_disliked_category_is_excluded_outright(self):
        experience = FakeExperience(category=FakeCategory("nightlife", "Nightlife"))
        scored = score_experience(experience, ctx_at(disliked_categories={"nightlife"}))
        assert scored.signals["personalization"] == 0.0

    def test_preferred_category_lifts_the_score(self):
        experience = FakeExperience(category=FakeCategory("music", "Music"))
        neutral = score_experience(experience, ctx_at())
        preferred = score_experience(experience, ctx_at(preferred_categories={"music"}))
        assert preferred.signals["personalization"] > neutral.signals["personalization"]

    def test_rain_favours_indoor_over_outdoor(self):
        indoor = score_experience(FakeExperience(is_indoor=True), ctx_at(is_raining=True))
        outdoor = score_experience(FakeExperience(is_indoor=False), ctx_at(is_raining=True))
        assert indoor.signals["personalization"] > outdoor.signals["personalization"]


class TestExplanations:
    def test_walking_distance_is_explained_in_minutes(self):
        experience = FakeExperience(
            venue=FakeVenue(latitude=CENTRE[0] + 0.002, longitude=CENTRE[1])
        )
        scored = score_experience(experience, ctx_at(*CENTRE))
        assert scored.reason is not None
        assert "walk" in scored.reason.lower()

    def test_free_experience_says_so(self):
        experience = FakeExperience(price_type="free")
        assert score_experience(experience, ctx_at()).reason == "Free to attend"

    def test_preferred_category_is_named_in_the_reason(self):
        experience = FakeExperience(category=FakeCategory("music", "Music"))
        scored = score_experience(experience, ctx_at(preferred_categories={"music"}))
        assert scored.reason is not None
        assert "music" in scored.reason.lower()


class TestDiversity:
    def test_one_category_cannot_monopolise_the_head_of_a_rail(self):
        food = FakeCategory("food-drink", "Food & Drink")
        music = FakeCategory("music", "Music")
        items = [FakeExperience(id=f"food-{i}", category=food) for i in range(6)]
        items.append(FakeExperience(id="music-1", category=music))

        scored = rank(items, ctx_at(), diversify=False)
        diversified = apply_diversity(scored, max_per_category=3)

        head = [item.experience.id for item in diversified[:4]]
        assert "music-1" in head
        # Nothing is dropped - overflow is deferred, not discarded.
        assert len(diversified) == len(scored)


class TestTimeWindows:
    def test_tonight_extends_past_midnight(self):
        """A 01:00 event is part of tonight, not tomorrow."""
        evening = datetime(2026, 8, 5, 20, 0, tzinfo=UTC)
        start, end = tonight_window(evening)
        assert start == evening
        assert end.hour == 4
        assert end.day == 6

    def test_weekend_starts_friday_evening(self):
        wednesday = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
        start, end = weekend_window(wednesday)
        assert start.weekday() == 4  # Friday
        assert start.hour == 17
        assert end.weekday() == 6  # Sunday

    def test_on_saturday_the_weekend_is_the_current_one(self):
        """Asked on Saturday, "this weekend" is today - not six days away."""
        saturday = datetime(2026, 8, 8, 11, 0, tzinfo=UTC)
        start, end = weekend_window(saturday)
        assert (start - saturday) < timedelta(days=1)
        assert end.weekday() == 6
