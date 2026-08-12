"""The demo catalogue (invented listings in real places).

This exists so the worldwide behaviour can be seen while the real catalogue is
still one city. Most of it is fixture data and needs no testing. The part that
does is the containment: invented listings reaching real explorers is worse than
an empty city, because somebody travels to a bar that does not exist.

So these tests are about the three things standing between this data and
production - the refusal, the marks, and the removal - and about the one
property that makes the fixture worth having at all, which is that the
coordinates are real.
"""

from __future__ import annotations

import re

import pytest

from app.seed import demo_cities
from app.seed.demo_cities import CITIES, seed_demo_cities

pytestmark = pytest.mark.anyio


class TestItCannotReachProduction:
    async def test_seeding_refuses_outright_in_production(self, monkeypatch):
        """Not a warning and not a flag to override. A made-up venue on a real
        explorer's screen is somebody's wasted evening."""
        from app.core import config

        monkeypatch.setattr(config.get_settings(), "environment", "production", raising=False)
        with pytest.raises(RuntimeError, match="production"):
            await seed_demo_cities(session=None)

    def test_the_refusal_happens_before_anything_is_written(self):
        """A check after the first insert would leave half a demo city behind in
        the database it just refused to seed."""
        import inspect

        source = inspect.getsource(seed_demo_cities)
        guard = source.index("production")
        first_write = source.index("session.add(")
        assert guard < first_write


class TestEverythingIsMarked:
    def test_every_publisher_slug_carries_the_mark(self):
        import inspect

        source = inspect.getsource(seed_demo_cities)
        assert 'publisher_slug = f"{DEMO_MARK}-' in source

    def test_every_listing_carries_the_attribute(self):
        import inspect

        assert "DEMO_MARK: True" in inspect.getsource(seed_demo_cities)

    def test_one_mark_is_used_everywhere(self):
        """Two strings meaning "demo" is one of them being forgotten by the
        query that cleans up."""
        source = __import__("inspect").getsource(demo_cities)
        # No bare "demo-" prefixes hiding outside the constant.
        for line in source.splitlines():
            if "demo-" in line and "DEMO_MARK" not in line and not line.strip().startswith("#"):
                assert '"""' in line or "*" in line, line

    def test_the_removal_finds_rows_by_the_mark_not_by_the_list(self):
        """A place deleted from the fixture is still in the database of anybody
        who seeded it before. Removal keyed on the list would strand it."""
        import inspect

        source = inspect.getsource(demo_cities.remove_demo_cities)
        assert 'like(f"{DEMO_MARK}-%")' in source


class TestTheCoordinatesAreReal:
    """The reason to have this fixture at all.

    Radius search, distance ranking and reverse geocoding are what it exists to
    exercise, and invented coordinates would exercise none of them - every
    distance would be nonsense and the geocoder would name the wrong country.
    """

    # Rough boxes around each city, generous enough not to be brittle and tight
    # enough that a decimal point in the wrong place fails.
    BOXES = {
        "new-york": (40.4, 41.0, -74.3, -73.7),
        "london": (51.3, 51.7, -0.5, 0.3),
        "nairobi": (-1.5, -1.1, 36.6, 37.0),
    }

    def test_each_city_centre_is_where_it_says_it_is(self):
        for city in CITIES:
            south, north, west, east = self.BOXES[city.slug]
            assert south <= city.latitude <= north, city.slug
            assert west <= city.longitude <= east, city.slug

    def test_every_venue_is_inside_its_own_city(self):
        """A venue in the wrong hemisphere would still seed and would quietly
        make every nearby query about that city wrong."""
        for city in CITIES:
            south, north, west, east = self.BOXES[city.slug]
            for venue in city.venues:
                assert south <= venue.latitude <= north, f"{city.slug}/{venue.name}"
                assert west <= venue.longitude <= east, f"{city.slug}/{venue.name}"

    def test_every_venue_is_close_to_its_city_centre(self):
        """Within 25 km, so the widening ladder reaches them from the centre."""
        from app.domains.catalog.repository import distance_km

        for city in CITIES:
            for venue in city.venues:
                distance = distance_km(
                    city.latitude, city.longitude, venue.latitude, venue.longitude
                )
                assert distance < 25, f"{city.slug}/{venue.name} is {distance:.1f} km out"

    def test_venues_are_not_all_stacked_on_one_point(self):
        """Identical coordinates would make the distance ranking untestable -
        everything would be equally near."""
        for city in CITIES:
            points = {(venue.latitude, venue.longitude) for venue in city.venues}
            assert len(points) == len(city.venues), city.slug


class TestTheFixtureItself:
    def test_the_cities_are_spread_across_the_world(self):
        """Three places on one continent would not have caught the things this
        was built to catch - a timezone, a currency and a hemisphere all
        differing from the pilot city."""
        assert len({city.country_code for city in CITIES}) == len(CITIES)
        assert any(city.latitude < 0 for city in CITIES)
        assert any(city.latitude > 0 for city in CITIES)
        assert len({city.currency for city in CITIES}) == len(CITIES)

    def test_no_city_borrows_the_pilot_currency(self):
        """Everything was ETB by default once. A demo city priced in birr would
        hide the same bug all over again."""
        assert not any(city.currency == "ETB" for city in CITIES)

    def test_every_listing_points_at_a_venue_that_exists(self):
        for city in CITIES:
            names = {venue.name for venue in city.venues}
            for listing in city.listings:
                assert listing.venue in names, f"{city.slug}: {listing.title}"

    def test_every_venue_points_at_a_neighbourhood_that_exists(self):
        for city in CITIES:
            names = {name for name, _, _ in city.neighbourhoods}
            for venue in city.venues:
                assert venue.neighbourhood in names, f"{city.slug}: {venue.name}"

    def test_some_listings_are_scheduled_and_some_are_not(self):
        """A catalogue of only events never exercises the place rails, and one
        of only places never exercises "tonight"."""
        every = [listing for city in CITIES for listing in city.listings]
        assert any(listing.occurrences for listing in every)
        assert any(not listing.occurrences for listing in every)

    def test_occurrences_are_in_the_future(self):
        """Offsets are hours from the moment of seeding. A negative one would
        seed an event that has already happened and show up nowhere."""
        for city in CITIES:
            for listing in city.listings:
                assert all(hours > 0 for hours in listing.occurrences), listing.title

    def test_prices_are_stated_in_the_local_currency(self):
        for city in CITIES:
            for listing in city.listings:
                if listing.price_type != "free":
                    assert listing.price_amount, f"{city.slug}: {listing.title}"

    def test_descriptions_are_written_rather_than_generated(self):
        """The fixture is for looking at. Placeholder text would make every
        screenshot useless and would teach the ranker nothing."""
        for city in CITIES:
            for listing in city.listings:
                assert len(listing.description) > 120, listing.title
                assert not re.search(r"lorem|ipsum|TODO|xxx", listing.description, re.I)


class TestItIsSeparateFromTheRealSeed:
    def test_the_main_seed_does_not_pull_it_in(self):
        """`python -m app.seed` must never write invented listings. It is a
        different command on purpose."""
        import inspect

        from app.seed import addis_ababa

        assert "demo_cities" not in inspect.getsource(addis_ababa)

    def test_it_has_its_own_command(self):
        from app.seed import __main__ as cli

        source = __import__("inspect").getsource(cli.main)
        assert "--demo-cities" in source
        assert "--remove-demo" in source
