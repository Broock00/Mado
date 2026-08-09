"""Publisher reputation tests (spec TRST-004).

A reputation system is a machine for making judgements about people, and the
ways it goes wrong are all quiet: it is confident on no evidence, it punishes
somebody permanently for one bad month, it rewards the publisher who games it
over the one who turns up. These tests are about those, not about the
arithmetic.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.domains.trust import reputation as module
from app.domains.trust.reputation import (
    BAND_EXCELLENT,
    BAND_POOR,
    BAND_PROVISIONAL,
    MIN_COMPLETED_DATES,
    NEUTRAL,
    Reputation,
    band_for,
    compute,
)

NOW = datetime(2026, 8, 9, tzinfo=UTC)


def publisher(**overrides):
    return SimpleNamespace(
        **{
            "id": uuid.uuid4(),
            "verification_status": "unverified",
            "created_at": NOW - timedelta(days=400),
            **overrides,
        }
    )


def experience(*, rating=None, ratings=0, reports=0, status="published", moderation="approved"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        moderation_status=moderation,
        rating_average=rating,
        rating_count=ratings,
        report_count=reports,
        deleted_at=None,
        publisher_id=None,
    )


def date_at(days_ago: float, *, cancelled: bool = False):
    return SimpleNamespace(
        id=uuid.uuid4(),
        experience_id=uuid.uuid4(),
        start_time=NOW - timedelta(days=days_ago),
        status="cancelled" if cancelled else "scheduled",
    )


def score_of(*, dates=(), experiences=(), pub=None) -> Reputation:
    return compute(pub or publisher(), list(experiences), list(dates), now=NOW)


class TestThinEvidence:
    def test_a_brand_new_publisher_is_unproven_not_bad(self):
        """Starting everybody at zero makes a first listing impossible to get
        seen, which is a reputation system preventing the behaviour it exists to
        measure."""
        result = score_of()
        assert result.band == BAND_PROVISIONAL
        assert result.score == pytest.approx(NEUTRAL, abs=0.15)

    def test_two_glowing_reviews_do_not_make_an_excellent_publisher(self):
        """Rendering thin evidence as a high score is the platform inventing
        confidence it does not have."""
        result = score_of(experiences=[experience(rating=5.0, ratings=2)])
        assert result.is_provisional

    def test_evidence_clears_the_floor_once_there_is_a_record(self):
        result = score_of(
            dates=[date_at(d) for d in (10, 40, 70, 100)],
            experiences=[experience(rating=4.6, ratings=30)],
        )
        assert not result.is_provisional

    def test_the_floor_is_reachable_by_either_route(self):
        """Somebody running dates with nobody reviewing, and somebody with a
        place people review but no ticketed dates, are both proven."""
        by_dates = score_of(dates=[date_at(d) for d in range(1, MIN_COMPLETED_DATES + 1)])
        by_ratings = score_of(experiences=[experience(rating=4.2, ratings=40)])
        assert not by_dates.is_provisional
        assert not by_ratings.is_provisional


class TestReliabilityIsTheHeaviestSignal:
    def test_turning_up_is_rewarded(self):
        result = score_of(dates=[date_at(d) for d in (5, 20, 40, 60)])
        assert result.score > NEUTRAL
        assert any(s.key == "reliability" and s.direction > 0 for s in result.signals)

    def test_cancelling_on_people_costs_more_than_turning_up_earns(self):
        """Turning up is the baseline expectation. A system that pays as much
        for meeting it as it charges for failing lets somebody cancel every
        other date and stay average.

        Measured on a long record, because on a short one the smoothing pulls
        both directions towards the baseline and neither extreme is reachable -
        which is the smoothing working, not the asymmetry failing.
        """
        clean = score_of(dates=[date_at(5 + i * 4) for i in range(40)])
        half_cancelled = score_of(
            dates=[date_at(5 + i * 4, cancelled=(i % 2 == 0)) for i in range(40)]
        )
        earned = clean.score - NEUTRAL
        lost = NEUTRAL - half_cancelled.score
        assert lost > earned

    def test_a_future_date_is_a_promise_not_a_delivery(self):
        """Counting scheduled dates as completed lets a publisher who has run
        nothing at all look reliable by listing a lot."""
        result = score_of(dates=[date_at(-30), date_at(-60), date_at(-90)])
        assert result.completed_dates == 0
        assert result.is_provisional

    def test_persistent_cancelling_lands_in_the_poor_band(self):
        result = score_of(
            dates=[date_at(d, cancelled=True) for d in (5, 15, 25)]
            + [date_at(d) for d in (35, 45, 55)],
            experiences=[experience(rating=2.0, ratings=20, reports=6)],
        )
        assert result.band == BAND_POOR


class TestOneBadNightIsNotAPattern:
    """The cancellation rate is smoothed towards a baseline.

    Taken raw, one cancellation out of eight is the same 12.5% as a hundred out
    of eight hundred - but the first is one bad night and the second is a proven
    pattern, and only the second says anything about what happens next. Without
    smoothing, a publisher's first cancellation lands them in the poor band on a
    sample of eight.
    """

    def spread(self, total: int, every: int) -> list:
        # Evenly spaced inside the recent window, so this measures the smoothing
        # rather than the recency weighting.
        return [
            date_at(5 + (i * 170 / total), cancelled=(i % every == 0)) for i in range(total)
        ]

    def test_a_first_cancellation_is_barely_a_dent(self):
        result = score_of(dates=self.spread(8, 8))
        assert result.band != BAND_POOR
        assert result.score > 0.45

    def test_the_same_rate_proven_over_time_is_taken_seriously(self):
        thin = score_of(dates=self.spread(8, 8))
        proven = score_of(dates=self.spread(160, 8))
        assert proven.score < thin.score

    def test_confidence_converges_rather_than_swinging(self):
        """More evidence at a steady rate should settle, not keep falling - past
        a point the rate is known and extra dates say nothing new."""
        scores = [score_of(dates=self.spread(n, 8)).score for n in (8, 24, 80, 160)]
        assert scores == sorted(scores, reverse=True)
        assert abs(scores[-1] - scores[-2]) < abs(scores[1] - scores[0])

    def test_a_spotless_record_earns_more_the_longer_it_is(self):
        """Nobody gets full credit for turning up twice."""
        short = score_of(dates=[date_at(5 + i * 10) for i in range(3)])
        long = score_of(dates=[date_at(5 + i * 3) for i in range(60)])
        assert long.score > short.score

    def test_the_wording_follows_what_happened_not_the_model(self):
        """A publisher who cancelled nothing should not be told their rate is
        4% - true of the arithmetic, false of their year, and uncheckable."""
        clean = score_of(dates=[date_at(5 + i * 10) for i in range(6)])
        reliability = next(s for s in clean.signals if s.key == "reliability")
        assert "none cancelled" in reliability.detail


class TestRecovery:
    def test_recent_behaviour_outweighs_old(self):
        """Spec §20 asks for reputation recovery by name. A record with no way
        back gives nobody a reason to improve."""
        reformed = score_of(
            dates=[date_at(d, cancelled=True) for d in (600, 620, 640)]
            + [date_at(d) for d in (10, 20, 30, 40, 50, 60)]
        )
        still_bad = score_of(
            dates=[date_at(d, cancelled=True) for d in (10, 20, 30)]
            + [date_at(d) for d in (600, 620, 640, 660, 680, 700)]
        )
        assert reformed.score > still_bad.score

    def test_every_improvement_shows_up_in_the_score(self):
        """The curve must keep falling across the whole range rather than
        flattening at a knee.

        It did flatten, at 20%: a publisher cancelling half their dates scored
        exactly the same as one cancelling a fifth, so halving your cancellation
        rate registered as no improvement at all - and the recovery the spec
        asks for by name had no gradient to climb in precisely the range where
        somebody most needs one.
        """
        scores = []
        for cancels in range(0, 21):
            dates = [date_at(5 + i, cancelled=(i < cancels)) for i in range(20)]
            scores.append(compute(publisher(), [], dates, now=NOW).score)

        assert scores == sorted(scores, reverse=True)
        assert len(set(scores)) == len(scores), "two different records score identically"

    def test_evidence_older_than_the_window_is_dropped_entirely(self):
        """Two years on, a publisher is answering for a different business."""
        ancient = score_of(dates=[date_at(2000, cancelled=True) for _ in range(10)])
        assert ancient.cancelled_dates == 0


class TestVerificationIsNotTheScore:
    def test_verifying_helps_but_does_not_decide(self):
        """The whole point of TRST-004: a publisher who verified once and has
        been cancelling ever since should not outrank one who has quietly
        delivered forty events."""
        verified_but_unreliable = score_of(
            pub=publisher(verification_status="verified"),
            dates=[date_at(d, cancelled=True) for d in (5, 15, 25, 35)]
            + [date_at(d) for d in (45, 55, 65, 75)],
        )
        unverified_but_reliable = score_of(
            dates=[date_at(d) for d in range(5, 200, 15)],
            experiences=[experience(rating=4.7, ratings=60)],
        )
        assert unverified_but_reliable.score > verified_but_unreliable.score

    def test_verification_still_counts_for_something(self):
        plain = score_of(dates=[date_at(d) for d in (5, 20, 40)])
        verified = score_of(
            pub=publisher(verification_status="verified"),
            dates=[date_at(d) for d in (5, 20, 40)],
        )
        assert verified.score > plain.score


class TestRatings:
    def test_ratings_are_weighted_by_how_many_people_said_it(self):
        """One listing with two hundred reviews should not be outvoted by three
        with two each - the same rule the publisher dashboard uses, so the two
        numbers cannot disagree."""
        loud_and_good = score_of(
            dates=[date_at(d) for d in (5, 20, 40)],
            experiences=[experience(rating=4.8, ratings=200), experience(rating=2.0, ratings=2)],
        )
        assert any(s.key == "ratings" and s.direction > 0 for s in loud_and_good.signals)

    def test_a_publisher_with_no_ratings_is_not_penalised_for_it(self):
        """Absence of praise is not evidence of a problem. A new venue nobody
        has reviewed is unproven, not poor."""
        result = score_of(dates=[date_at(d) for d in (5, 20, 40, 60)])
        assert not any(s.key == "ratings" for s in result.signals)
        assert result.score >= NEUTRAL


class TestComplaintsCannotBeWeaponised:
    def test_reports_lower_the_score(self):
        result = score_of(
            dates=[date_at(d) for d in (5, 20, 40)],
            experiences=[experience(reports=8, rating=4.0, ratings=10)],
        )
        assert any(s.key == "reports" and s.direction < 0 for s in result.signals)

    def test_a_brigade_cannot_bottom_out_a_publisher_on_its_own(self):
        """A coordinated campaign should reach the moderation queue and a person
        reading it, not silently destroy somebody's ranking."""
        brigaded = score_of(
            dates=[date_at(d) for d in (5, 20, 40, 60, 80)],
            experiences=[experience(reports=5000, rating=4.5, ratings=50)],
        )
        assert brigaded.score > 0.2


class TestWhatItCannotDo:
    def test_there_is_no_way_to_suspend_from_here(self):
        """Spec BUSINESS-07: automated systems detect, humans decide. A poor
        reputation lowers ranking; it never withholds, suspends or rejects."""
        exported = {name for name in dir(module) if not name.startswith("_")}
        assert not exported & {"suspend", "withhold", "reject", "ban", "block", "delete"}

    def test_the_score_never_leaves_its_range(self):
        catastrophic = score_of(
            dates=[date_at(d, cancelled=True) for d in range(1, 60)],
            experiences=[experience(reports=999, moderation="rejected") for _ in range(20)],
        )
        perfect = score_of(
            pub=publisher(verification_status="verified", created_at=NOW - timedelta(days=2000)),
            dates=[date_at(d) for d in range(1, 120)],
            experiences=[experience(rating=5.0, ratings=500)],
        )
        assert 0.0 <= catastrophic.score <= 1.0
        assert 0.0 <= perfect.score <= 1.0
        assert perfect.band == BAND_EXCELLENT


class TestExplaining:
    def test_every_score_comes_with_its_reasons(self):
        """A number somebody cannot act on is a grievance rather than feedback,
        and 'which of these do I fix' is the recovery path the spec asks for."""
        result = score_of(
            dates=[date_at(5, cancelled=True), *[date_at(d) for d in (20, 40, 60)]],
            experiences=[experience(rating=3.0, ratings=20, reports=3)],
        )
        assert result.signals
        assert all(s.detail for s in result.signals)

    def test_the_worst_signal_is_listed_first(self):
        """Somebody opening this wants to know what to fix, not to be
        congratulated first."""
        result = score_of(
            pub=publisher(verification_status="verified"),
            dates=[date_at(d, cancelled=True) for d in (5, 15)] + [date_at(d) for d in (25, 35)],
        )
        directions = [s.direction for s in result.signals]
        assert directions == sorted(directions)

    def test_the_detail_says_what_happened_rather_than_what_it_scored(self):
        result = score_of(dates=[date_at(5, cancelled=True), date_at(20), date_at(40)])
        reliability = next(s for s in result.signals if s.key == "reliability")
        assert "cancelled" in reliability.detail
        assert "0." not in reliability.detail  # no raw score leaking into prose


class TestBands:
    @pytest.mark.parametrize(
        ("score", "expected"),
        [(0.95, "excellent"), (0.7, "good"), (0.5, "mixed"), (0.2, "poor")],
    )
    def test_bands_map_as_documented(self, score, expected):
        assert band_for(score, provisional=False) == expected

    def test_provisional_overrides_every_band(self):
        """Including a high one. "Excellent, based on nothing" is the failure
        this guards against."""
        assert band_for(0.99, provisional=True) == BAND_PROVISIONAL


class TestWhoCanSeeIt:
    def test_the_serialised_form_carries_its_signals(self):
        result = score_of(dates=[date_at(d) for d in (5, 20, 40)])
        payload = result.as_dict()
        assert "score" in payload
        assert payload["signals"]

    def test_reputation_is_not_on_the_public_experience_card(self):
        """Explorers see the verified badge and the ratings other people left,
        which are things they can interpret. A trust number on a card invites
        working out what moves it."""
        from app.domains.catalog import schemas

        public = (
            schemas.ExperienceSummary,
            schemas.ExperienceDetail,
            schemas.PublisherSummary,
        )
        for shape in public:
            fields = set(shape.model_fields)
            assert not {f for f in fields if "reputation" in f}, shape.__name__
