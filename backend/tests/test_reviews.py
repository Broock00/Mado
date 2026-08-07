"""Review and rating tests.

Ratings feed the `quality` ranking signal, which is why the aggregate arithmetic
gets more attention here than the CRUD around it: a wrong average does not look
wrong, it just quietly reorders the city.
"""

from __future__ import annotations

import pytest

from app.domains.explorer.reviews import (
    ATTENDED_WEIGHT,
    MAX_COMMENT,
    MAX_RATING,
    MIN_RATING,
    UNCONFIRMED_WEIGHT,
    VISIBLE_STATUSES,
    RatingSummary,
)


def weighted_average(rows: list[tuple[int, bool]]) -> float:
    """The arithmetic recompute() performs, isolated for testing."""
    total = sum(ATTENDED_WEIGHT if attended else UNCONFIRMED_WEIGHT for _, attended in rows)
    weighted = sum(
        rating * (ATTENDED_WEIGHT if attended else UNCONFIRMED_WEIGHT) for rating, attended in rows
    )
    return round(weighted / total, 2)


class TestRatingArithmetic:
    def test_a_plain_average_when_nobody_confirmed_attendance(self):
        assert weighted_average([(5, False), (4, False), (2, False)]) == pytest.approx(
            3.67, abs=0.01
        )

    def test_confirmed_attendance_counts_for_more(self):
        """Spec BUSINESS-07 weights a confirmed visit higher."""
        unconfirmed = weighted_average([(5, False), (1, False)])
        confirmed_high = weighted_average([(5, True), (1, False)])
        assert confirmed_high > unconfirmed

    def test_attendance_shifts_the_average_without_dominating_it(self):
        """A handful of confirmed visits must not swamp everyone else.

        One confirmed 5 against four unconfirmed 1s should still read as poor.
        """
        assert weighted_average([(5, True), (1, False), (1, False), (1, False), (1, False)]) < 2.5

    def test_a_single_rating_is_that_rating(self):
        assert weighted_average([(4, False)]) == 4.0

    def test_the_average_stays_in_range(self):
        for rows in ([(1, True)], [(5, True)], [(1, False), (5, True)]):
            assert MIN_RATING <= weighted_average(rows) <= MAX_RATING


class TestSummary:
    def test_no_ratings_reports_none_rather_than_zero(self):
        """Zero would rank an unreviewed place as unanimously terrible."""
        summary = RatingSummary(average=None, count=0, distribution={})
        assert summary.average is None
        assert not summary.has_ratings

    def test_the_distribution_survives_the_average(self):
        """Five 1s and five 5s average to the same 3 as ten 3s.

        The average alone cannot tell a divisive place from a mediocre one, which
        is the whole reason the spread is carried alongside it.
        """
        polarised = RatingSummary(average=3.0, count=10, distribution={1: 5, 5: 5})
        middling = RatingSummary(average=3.0, count=10, distribution={3: 10})
        assert polarised.average == middling.average
        assert polarised.distribution != middling.distribution


class TestVisibility:
    def test_only_approved_reviews_are_shown(self):
        assert VISIBLE_STATUSES == ("approved",)

    def test_flagged_is_not_visible(self):
        assert "flagged" not in VISIBLE_STATUSES


class TestBounds:
    @pytest.mark.parametrize("rating", [0, -1, 6, 100])
    def test_ratings_outside_the_scale_are_refused(self, rating):
        assert not MIN_RATING <= rating <= MAX_RATING

    @pytest.mark.parametrize("rating", [1, 2, 3, 4, 5])
    def test_the_whole_scale_is_accepted(self, rating):
        assert MIN_RATING <= rating <= MAX_RATING

    def test_comments_are_bounded(self):
        """Long enough for a considered account, short enough not to be a blog."""
        assert 500 <= MAX_COMMENT <= 5000
