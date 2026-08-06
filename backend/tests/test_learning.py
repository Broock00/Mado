"""Learning loop tests.

The loop turns behaviour into ranking influence, which makes it the easiest place
to do quiet harm: an inference that outranks a stated preference makes the
preference screen a lie, and a trend score that saturates makes the Trending rail
an arbitrary ordering. Both were real defects here and both are pinned below.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from app.domains.discovery.ranking import (
    INFERENCE_CEILING,
    RankingContext,
    score_experience,
)
from app.domains.explorer.learning import (
    ACTION_WEIGHTS,
    CONFIDENCE_SATURATION,
    SIGNAL_HALF_LIFE_DAYS,
    TREND_SENSITIVITY,
    InferredPreferences,
    _decay,
    _normalise_affinity,
    personalization_enabled,
)


@dataclass
class FakeCategory:
    slug: str
    name: str = "Music"


@dataclass
class FakeTag:
    slug: str
    name: str = "Tag"


@dataclass
class FakeExperience:
    id: str = "exp-1"
    type: str = "place"
    title: str = "Test"
    category: FakeCategory | None = None
    tags: list = field(default_factory=list)
    venue: None = None
    events: list = field(default_factory=list)
    price_type: str = "fixed"
    is_indoor: bool | None = None
    popularity_score: float = 0.5
    quality_score: float = 0.5
    trend_score: float = 0.0
    rating_average: float | None = None
    rating_count: int = 0
    publisher: object | None = None


def ctx(**kwargs) -> RankingContext:
    return RankingContext(now=datetime.now(UTC), **kwargs)


# --- action weights ----------------------------------------------------------


def test_deliberate_actions_outweigh_passive_ones():
    """A save is a decision; a view is barely evidence of anything."""
    assert ACTION_WEIGHTS["save"] > ACTION_WEIGHTS["open_details"] > ACTION_WEIGHTS["view"]


def test_explicit_rejection_is_the_strongest_signal():
    """Spec DISC-002: "not interested" is an instruction, not a data point."""
    strongest_negative = min(ACTION_WEIGHTS.values())
    assert ACTION_WEIGHTS["not_interested"] == strongest_negative
    assert abs(ACTION_WEIGHTS["not_interested"]) > ACTION_WEIGHTS["save"]


def test_a_single_view_cannot_saturate_confidence():
    assert ACTION_WEIGHTS["view"] / CONFIDENCE_SATURATION < 0.05


# --- decay -------------------------------------------------------------------


def test_signal_halves_over_its_half_life():
    now = datetime.now(UTC)
    assert _decay(now - timedelta(days=SIGNAL_HALF_LIFE_DAYS), now) == pytest.approx(0.5)


def test_recent_behaviour_counts_for_more():
    now = datetime.now(UTC)
    recent = _decay(now - timedelta(days=3), now)
    old = _decay(now - timedelta(days=200), now)
    assert recent > old


def test_naive_timestamps_do_not_break_decay():
    now = datetime.now(UTC)
    naive = (now - timedelta(days=10)).replace(tzinfo=None)
    assert 0.0 < _decay(naive, now) <= 1.0


# --- affinity normalisation --------------------------------------------------


def test_affinity_is_bounded_and_signed():
    # Inclusive bounds: tanh saturates to exactly +/-1.0 in floating point well
    # before the input gets extreme, which is the intended behaviour.
    assert -1.0 <= _normalise_affinity(-500.0) < 0.0
    assert 0.0 < _normalise_affinity(500.0) <= 1.0
    assert _normalise_affinity(0.0) == pytest.approx(0.0)


def test_affinity_saturates_so_no_burst_can_peg_a_category():
    """Doubling an already-strong signal must barely move it."""
    strong = _normalise_affinity(40.0)
    doubled = _normalise_affinity(80.0)
    assert doubled - strong < 0.05


# --- consent -----------------------------------------------------------------


def test_personalization_off_without_a_profile():
    assert personalization_enabled(None) is False


def test_personalization_opt_out_is_respected():
    assert personalization_enabled({"personalizationEnabled": False}) is False


def test_personalization_defaults_on():
    assert personalization_enabled({}) is True


# --- inferences must not outrank stated preferences --------------------------


class TestInferenceAuthority:
    """Spec 10.01.02: stated preferences outrank inferred ones."""

    def test_ceiling_is_below_a_stated_preference(self):
        # A stated category preference contributes 0.3 in _personalization_score.
        assert INFERENCE_CEILING < 0.3

    def test_stated_choice_beats_maximal_opposing_inference(self):
        stated = FakeExperience(id="stated", category=FakeCategory(slug="music"))
        inferred = FakeExperience(id="inferred", category=FakeCategory(slug="food"))
        context = ctx(
            preferred_categories={"music"},
            inferred_categories={"food": 1.0},
            inference_confidence=1.0,
        )
        assert (
            score_experience(stated, context).signals["personalization"]
            > score_experience(inferred, context).signals["personalization"]
        )

    def test_a_new_explorer_is_barely_personalised(self):
        """Low confidence must damp the whole inference, not just scale it."""
        experience = FakeExperience(category=FakeCategory(slug="music"))
        cold = ctx(inferred_categories={"music": 1.0}, inference_confidence=0.05)
        warm = ctx(inferred_categories={"music": 1.0}, inference_confidence=1.0)

        cold_score = score_experience(experience, cold).signals["personalization"]
        warm_score = score_experience(experience, warm).signals["personalization"]
        assert cold_score < warm_score
        assert cold_score == pytest.approx(0.5, abs=0.02)

    def test_behaviour_does_not_double_count_a_stated_preference(self):
        """A category both stated and observed must not stack to a higher score."""
        experience = FakeExperience(category=FakeCategory(slug="music"))
        stated_only = ctx(preferred_categories={"music"})
        stated_and_observed = ctx(
            preferred_categories={"music"},
            inferred_categories={"music": 1.0},
            inference_confidence=1.0,
        )
        assert (
            score_experience(experience, stated_and_observed).signals["personalization"]
            == score_experience(experience, stated_only).signals["personalization"]
        )

    def test_negative_affinity_pushes_a_listing_down(self):
        experience = FakeExperience(category=FakeCategory(slug="sports"))
        neutral = ctx()
        disliked = ctx(inferred_categories={"sports": -1.0}, inference_confidence=1.0)
        assert (
            score_experience(experience, disliked).signals["personalization"]
            < score_experience(experience, neutral).signals["personalization"]
        )

    def test_tag_count_does_not_inflate_a_match(self):
        """Averaging, not summing - otherwise heavily-tagged listings always win."""
        few = FakeExperience(id="few", tags=[FakeTag("jazz")])
        many = FakeExperience(
            id="many", tags=[FakeTag("jazz"), *(FakeTag(f"t{i}") for i in range(7))]
        )
        context = ctx(inferred_tags={"jazz": 1.0}, inference_confidence=1.0)
        assert score_experience(few, context).signals["personalization"] == pytest.approx(
            score_experience(many, context).signals["personalization"], abs=1e-9
        )


# --- inferred preferences container ------------------------------------------


def test_empty_inference_is_not_useful():
    assert InferredPreferences().is_useful is False


def test_inference_with_a_dislike_is_useful():
    assert InferredPreferences(disliked_categories={"sports"}).is_useful is True


# --- trend curve -------------------------------------------------------------


class TestTrendCurve:
    """The Trending rail has to rank, not just flag.

    A first attempt squashed every listing above baseline into 0.79-0.99, which
    ordered them essentially at random. These pin the usable spread.
    """

    @staticmethod
    def trend(ratio: float) -> float:
        return max(0.0, min(1.0, math.tanh((ratio - 1.0) / TREND_SENSITIVITY)))

    def test_baseline_traffic_does_not_trend(self):
        assert self.trend(1.0) == pytest.approx(0.0)

    def test_below_baseline_never_goes_negative(self):
        assert self.trend(0.4) == 0.0

    def test_curve_still_discriminates_at_moderate_excess(self):
        """Twice baseline and three times baseline must be clearly different."""
        assert self.trend(3.0) - self.trend(2.0) > 0.1

    def test_curve_is_monotonic(self):
        values = [self.trend(r) for r in (1.0, 1.5, 2.0, 3.0, 5.0)]
        assert values == sorted(values)
