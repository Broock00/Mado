"""Deciding which city a request is about.

Mado is a worldwide platform, and for a long time it was not: every discovery
surface defaulted to one configured city, so an explorer opening the app
anywhere on earth was shown Addis Ababa and told so in the copy. These tests
pin the replacement, and most of them are about the case that used to be
handled by pretending - somebody in a place Mado does not cover.

The rule is short: an explicit choice wins, coordinates decide otherwise, and
when there is neither the answer is None. None is the important one. It is what
makes the interface ask instead of guess.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.domains.catalog.repository import (
    NEAREST_CITY_MAX_KM,
    RESOLVED_BY_CHOSEN,
    RESOLVED_BY_LOCATION,
    RESOLVED_BY_UNKNOWN,
    bounding_box,
    distance_km,
    nearest_live_city,
    resolve_city_slug,
)

pytestmark = pytest.mark.anyio

# Real coordinates, so the distances below are checkable against an atlas.
ADDIS = (9.0192, 38.7525)
NAIROBI = (-1.2864, 36.8172)
PARIS = (48.8566, 2.3522)
LONDON = (51.5072, -0.1276)
REYKJAVIK = (64.1466, -21.9426)


def city(slug: str, latitude: float, longitude: float, *, live: bool = True):
    return SimpleNamespace(slug=slug, latitude=latitude, longitude=longitude, is_live=live)


class FakeSession:
    """Answers the bounding-box query from a fixed list, filtering it in Python.

    The point of the test is the choice `nearest_live_city` makes, not
    SQLAlchemy's WHERE clause - so the candidates are handed over directly and
    the assertions are about which one comes back and whether it is close
    enough.
    """

    def __init__(self, cities) -> None:
        self.cities = list(cities)
        self.queried = False

    async def execute(self, _stmt):
        self.queried = True
        rows = self.cities

        class Scalars:
            def all(self_inner):
                return rows

        class Result:
            def scalars(self_inner):
                return Scalars()

        return Result()


class TestDistance:
    def test_a_known_distance_is_right(self):
        """Addis to Nairobi is about 1160 km. A flat-earth approximation gets
        this wrong by enough to matter."""
        assert 1130 < distance_km(*ADDIS, *NAIROBI) < 1190

    def test_london_to_paris(self):
        assert 330 < distance_km(*LONDON, *PARIS) < 350

    def test_a_place_is_no_distance_from_itself(self):
        assert distance_km(*PARIS, *PARIS) == pytest.approx(0.0, abs=1e-9)

    def test_it_is_symmetric(self):
        assert distance_km(*PARIS, *LONDON) == pytest.approx(distance_km(*LONDON, *PARIS))


class TestNearestCity:
    async def test_the_closest_one_wins(self):
        session = FakeSession([city("nairobi", *NAIROBI), city("addis-ababa", *ADDIS)])
        found = await nearest_live_city(session, *ADDIS)
        assert found is not None and found[0].slug == "addis-ababa"

    async def test_somewhere_uncovered_resolves_to_nothing(self):
        """The whole point. Paris is 5000 km from the nearest covered city, and
        the old behaviour was to show that city anyway."""
        session = FakeSession([city("addis-ababa", *ADDIS)])
        assert await nearest_live_city(session, *PARIS) is None

    async def test_just_outside_the_radius_is_refused(self):
        """A suburb is the same city; the next country is not. The boundary has
        to be somewhere and it has to be enforced."""
        # Roughly 1.5x the radius due north.
        far = (ADDIS[0] + (NEAREST_CITY_MAX_KM * 1.5) / 111.0, ADDIS[1])
        session = FakeSession([city("addis-ababa", *ADDIS)])
        assert await nearest_live_city(session, *far) is None

    async def test_just_inside_the_radius_is_accepted(self):
        near = (ADDIS[0] + (NEAREST_CITY_MAX_KM * 0.5) / 111.0, ADDIS[1])
        session = FakeSession([city("addis-ababa", *ADDIS)])
        found = await nearest_live_city(session, *near)
        assert found is not None and found[0].slug == "addis-ababa"

    async def test_no_covered_cities_at_all_is_not_a_crash(self):
        assert await nearest_live_city(FakeSession([]), *PARIS) is None

    async def test_the_returned_distance_is_the_real_one(self):
        session = FakeSession([city("nairobi", *NAIROBI)])
        found = await nearest_live_city(session, *NAIROBI, max_km=10)
        assert found is not None
        assert found[1] == pytest.approx(0.0, abs=1e-6)


class TestTheBoundingBox:
    """Tested directly rather than through a fake session.

    A fake cannot apply a SQL WHERE clause, so a test that went through one
    would return the candidate whatever the box said and pass for the wrong
    reason. The box is the part with the interesting arithmetic, so it is the
    part that gets asserted.
    """

    def test_it_covers_the_radius_at_the_equator(self):
        lat_span, lon_span = bounding_box(0.0, NEAREST_CITY_MAX_KM)
        assert lat_span == pytest.approx(NEAREST_CITY_MAX_KM / 111.0)
        assert lon_span == pytest.approx(lat_span, rel=0.01)

    def test_it_widens_towards_the_poles(self):
        """One degree of longitude is 111 km at the equator and about 48 km in
        Reykjavik. The same span at both would miss cities well inside the
        radius up there."""
        _, equator = bounding_box(0.0, NEAREST_CITY_MAX_KM)
        _, iceland = bounding_box(REYKJAVIK[0], NEAREST_CITY_MAX_KM)
        assert iceland > equator * 2

    def test_the_box_never_excludes_something_inside_the_radius(self):
        """The property that matters: false negatives are invisible, because a
        city that never reaches the distance check is simply never found."""
        import math

        for latitude in (0.0, 30.0, 51.5, REYKJAVIK[0], 78.0):
            lat_span, lon_span = bounding_box(latitude, NEAREST_CITY_MAX_KM)
            # Furthest a point can be due east and still be within the radius.
            widest = NEAREST_CITY_MAX_KM / (111.0 * max(math.cos(math.radians(latitude)), 0.01))
            assert lon_span >= widest - 1e-9, latitude
            assert lat_span >= NEAREST_CITY_MAX_KM / 111.0 - 1e-9

    def test_the_poles_do_not_produce_an_infinite_span(self):
        _, span = bounding_box(90.0, NEAREST_CITY_MAX_KM)
        assert span == pytest.approx(NEAREST_CITY_MAX_KM / 111.0 / 0.01)


class TestResolution:
    async def test_a_chosen_city_wins_over_location(self):
        """Planning a trip to a city you are not in yet is an ordinary thing to
        do, and the explicit choice is the whole reason the filter exists."""
        session = FakeSession([city("addis-ababa", *ADDIS)])
        slug, why = await resolve_city_slug(
            session, city="nairobi", latitude=ADDIS[0], longitude=ADDIS[1]
        )
        assert (slug, why) == ("nairobi", RESOLVED_BY_CHOSEN)

    async def test_a_chosen_city_needs_no_location_at_all(self):
        session = FakeSession([])
        slug, why = await resolve_city_slug(
            session, city="paris", latitude=None, longitude=None
        )
        assert (slug, why) == ("paris", RESOLVED_BY_CHOSEN)
        assert not session.queried, "it looked up a city it had already been given"

    async def test_location_decides_when_nothing_was_chosen(self):
        session = FakeSession([city("addis-ababa", *ADDIS)])
        slug, why = await resolve_city_slug(
            session, city=None, latitude=ADDIS[0], longitude=ADDIS[1]
        )
        assert (slug, why) == ("addis-ababa", RESOLVED_BY_LOCATION)

    async def test_neither_resolves_to_nothing(self):
        """Not to a default. This is the line that used to read
        `city: str = settings.default_city_slug`."""
        slug, why = await resolve_city_slug(
            FakeSession([]), city=None, latitude=None, longitude=None
        )
        assert (slug, why) == (None, RESOLVED_BY_UNKNOWN)

    async def test_being_somewhere_uncovered_resolves_to_nothing(self):
        session = FakeSession([city("addis-ababa", *ADDIS)])
        slug, why = await resolve_city_slug(
            session, city=None, latitude=PARIS[0], longitude=PARIS[1]
        )
        assert (slug, why) == (None, RESOLVED_BY_UNKNOWN)


class TestNothingDefaultsToOneCityAnyMore:
    def test_discovery_does_not_default_its_city(self):
        """The regression that started this. A default here is invisible: every
        surface answers confidently about a city the explorer has never been
        to."""
        import inspect

        from app.api.routes import discovery

        source = inspect.getsource(discovery.discovery_query)
        assert "default_city_slug" not in source
        assert "resolve_city_slug" in source

    def test_neither_does_the_concierge_or_the_planner(self):
        import inspect

        from app.api.routes import concierge, planning

        for module in (concierge, planning):
            assert "default_city_slug" not in inspect.getsource(module), module.__name__

    def test_the_assistant_no_longer_falls_back_to_one_timezone(self):
        """`resolve_city` ended with a hardcoded Africa/Addis_Ababa, so an
        explorer anywhere was answered in Ethiopian time."""
        import inspect

        from app.domains.ai.gateway import resolve_city

        source = inspect.getsource(resolve_city)
        assert "Africa/Addis_Ababa" not in source
        assert "resolve_city_slug" in source

    def test_a_feed_with_no_city_returns_nothing_rather_than_another_city(self):
        import inspect

        from app.api.routes import discovery

        for endpoint in (
            discovery.discover_now,
            discovery.discover_tonight,
            discovery.discover_weekend,
            discovery.discover_trending,
            discovery.for_you,
        ):
            source = inspect.getsource(endpoint)
            assert "if not params.city:" in source, endpoint.__name__

    def test_but_search_still_searches_everywhere(self):
        """Search is an explicit act, and the repository already treats a null
        city as unscoped. Refusing to search without a city would be a
        regression dressed as consistency."""
        import inspect

        from app.api.routes import discovery

        assert "if not params.city:" not in inspect.getsource(discovery.search)
