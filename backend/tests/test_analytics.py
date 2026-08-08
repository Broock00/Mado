"""Analytics tests.

A dashboard fails differently from a feature: it does not crash, it just tells
you something untrue and you act on it. So these tests are mostly about the
arithmetic and about the numbers the code declines to show.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.domains.explorer.summary import RECENT_DAYS, TOP_CATEGORIES, ExplorerSummary
from app.domains.publisher.analytics import (
    DEFAULT_WINDOW,
    MIN_RATE_SAMPLE,
    WINDOWS,
    DayPoint,
    ExperienceMetrics,
    PublisherAnalyticsService,
    PublisherOverview,
    weighted_rating,
)

pytestmark = pytest.mark.anyio


def metrics(views: int, net_saves: int, **overrides) -> ExperienceMetrics:
    return ExperienceMetrics(
        **{
            "experience_id": uuid.uuid4(),
            "title": "A listing",
            "status": "published",
            "moderation_status": "approved",
            "views": views,
            "unique_viewers": views,
            "saves": max(net_saves, 0),
            "net_saves": net_saves,
            "rating_average": None,
            "rating_count": 0,
            "report_count": 0,
            **overrides,
        }
    )


def overview(views: int, net_saves: int) -> PublisherOverview:
    return PublisherOverview(
        window_days=30,
        total_views=views,
        unique_viewers=views,
        total_saves=max(net_saves, 0),
        net_saves=net_saves,
        published_count=1,
        draft_count=0,
        withheld_count=0,
        review_count=0,
        rating_average=None,
        report_count=0,
    )


class TestRatesAreSuppressedUntilTheyMeanSomething:
    """Two saves from three views is not a 67% save rate. It is three views.

    A ratio over a tiny denominator is the fastest way to make a dashboard lie,
    and a publisher who reads 67% will draw a conclusion the data cannot carry.
    """

    def test_a_tiny_sample_reports_no_rate_at_all(self):
        assert metrics(views=3, net_saves=2).save_rate is None

    def test_it_is_none_rather_than_zero(self):
        """Zero would read as "nobody saves this", which is a different claim."""
        assert metrics(views=1, net_saves=0).save_rate is None

    def test_just_below_the_threshold_is_still_withheld(self):
        assert metrics(views=MIN_RATE_SAMPLE - 1, net_saves=5).save_rate is None

    def test_at_the_threshold_a_rate_appears(self):
        assert metrics(views=MIN_RATE_SAMPLE, net_saves=5).save_rate == pytest.approx(0.25)

    def test_the_same_rule_applies_to_the_publisher_total(self):
        assert overview(views=MIN_RATE_SAMPLE - 1, net_saves=9).save_rate is None
        assert overview(views=100, net_saves=25).save_rate == pytest.approx(0.25)

    def test_no_views_does_not_divide_by_zero(self):
        assert metrics(views=0, net_saves=0).save_rate is None


class TestSavesAreNetOfUnsaves:
    def test_a_listing_saved_and_then_unsaved_reads_as_neither(self):
        """Forty saves and thirty-nine unsaves is not a popular listing.

        Showing only the forty would tell a publisher their post works when what
        actually happened is that people changed their minds.
        """
        assert metrics(views=100, net_saves=1).save_rate == pytest.approx(0.01)

    def test_net_saves_can_go_negative_and_is_not_hidden(self):
        """More unsaves than saves in a window is real information."""
        assert metrics(views=100, net_saves=-5).save_rate == pytest.approx(-0.05)


class TestWeightedRating:
    """The publisher's rating is read from the same aggregates the cards show.

    Recomputing it from review rows produced a different number - the seed
    writes rating aggregates directly - and two different answers to "what is my
    rating" is how a publisher learns to distrust the dashboard.
    """

    def experience(self, average: float | None, count: int):
        return SimpleNamespace(rating_average=average, rating_count=count)

    def test_no_ratings_means_no_number(self):
        assert weighted_rating([self.experience(None, 0)]) is None

    def test_an_empty_publisher_has_no_rating(self):
        assert weighted_rating([]) is None

    def test_a_single_listing_carries_its_own_rating(self):
        assert weighted_rating([self.experience(4.5, 10)]) == pytest.approx(4.5)

    def test_it_weights_by_how_many_people_rated(self):
        """A listing rated by two hundred should not count the same as one rated
        by three - a mean of means lets a single obscure listing swing the lot."""
        rated = [self.experience(5.0, 3), self.experience(4.0, 200)]
        weighted = weighted_rating(rated)
        naive_mean = (5.0 + 4.0) / 2

        assert weighted == pytest.approx(4.01, abs=0.01)
        assert weighted < naive_mean

    def test_listings_with_a_rating_but_no_raters_are_ignored(self):
        rated = [self.experience(4.0, 10), self.experience(1.0, 0)]
        assert weighted_rating(rated) == pytest.approx(4.0)


class TestTheDailySeries:
    """Quiet days have to appear, or the chart draws a busy line through them."""

    async def test_every_day_in_the_window_is_present(self):
        service = PublisherAnalyticsService(session=None)  # type: ignore[arg-type]

        async def _execute(_statement):
            return iter(())

        service.session = SimpleNamespace(execute=_execute)  # type: ignore[assignment]

        series = await service._daily([uuid.uuid4()], datetime.now(UTC) - timedelta(days=7), 7)
        assert len(series) == 7
        assert all(point.views == 0 and point.saves == 0 for point in series)

    async def test_the_series_runs_forward_and_ends_today(self):
        service = PublisherAnalyticsService(session=None)  # type: ignore[arg-type]

        async def _execute(_statement):
            return iter(())

        service.session = SimpleNamespace(execute=_execute)  # type: ignore[assignment]

        series = await service._daily([uuid.uuid4()], datetime.now(UTC) - timedelta(days=5), 5)
        days = [point.day for point in series]
        assert days == sorted(days)
        assert days[-1] == datetime.now(UTC).date()


class TestWindows:
    def test_an_unknown_window_falls_back_rather_than_erroring(self):
        """A hand-edited query string should not 500 a dashboard."""
        assert DEFAULT_WINDOW in WINDOWS

    def test_the_longest_window_still_describes_the_present(self):
        """Beyond a season, a listing's numbers describe a post that has changed."""
        assert max(WINDOWS) <= 120


class TestExplorerSummaryStaysQuietWhenEmpty:
    def summary(self, **overrides) -> ExplorerSummary:
        return ExplorerSummary(
            **{
                "saved_count": 0,
                "collection_count": 0,
                "plan_count": 0,
                "review_count": 0,
                "explored_count": 0,
                "recent_days": RECENT_DAYS,
                **overrides,
            }
        )

    def test_a_brand_new_explorer_reads_as_empty(self):
        """So the interface can say something human instead of a wall of zeroes."""
        assert self.summary().is_empty

    def test_any_single_signal_is_enough_to_have_something_to_show(self):
        for field in (
            "saved_count",
            "collection_count",
            "plan_count",
            "review_count",
            "explored_count",
        ):
            assert not self.summary(**{field: 1}).is_empty, field

    def test_the_window_covers_a_normal_rhythm_of_going_out(self):
        assert 30 <= RECENT_DAYS <= 180

    def test_only_a_handful_of_categories_are_shown(self):
        assert 3 <= TOP_CATEGORIES <= 8


class TestNothingIdentifiesAPerson:
    def test_the_publisher_payload_carries_no_explorer_identity(self):
        """A publisher learns how many opened a listing, never which ones.

        Asserted on the shape rather than trusted to review: a future field
        called `viewers` or `recent_users` would fail here.
        """
        from app.api.routes.analytics import ExperienceMetricsOut, PublisherAnalyticsOut

        fields = set(PublisherAnalyticsOut.model_fields) | set(
            ExperienceMetricsOut.model_fields
        )
        forbidden = {"user_id", "user_ids", "viewers", "explorers", "emails", "recent_users"}
        assert not fields & forbidden

    def test_metrics_are_counts_not_collections_of_people(self):
        from app.api.routes.analytics import ExperienceMetricsOut

        for name, field in ExperienceMetricsOut.model_fields.items():
            if name.endswith(("_count", "views", "saves", "viewers")):
                assert field.annotation in (int, float, float | None), name


class TestDayPoint:
    def test_a_point_is_just_a_day_and_two_numbers(self):
        point = DayPoint(day=datetime.now(UTC).date(), views=3, saves=1)
        assert point.views == 3
        assert point.saves == 1
