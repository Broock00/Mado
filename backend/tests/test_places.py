"""Resolving the world without storing it.

Geography is not Mado's data. Countries, cities, districts and streets are
resolved from OpenStreetMap or Google when somebody asks, which is what lets an
explorer in Brooklyn be told where they are without a developer having typed
"Brooklyn" into a table first.

The payloads below are trimmed copies of real Nominatim and Google responses.
Parsing them is most of the risk in this module: the administrative chain is
named differently in every country, and a mapping that works in one is quietly
wrong in the next - a London borough reported as a state, a city that vanishes
because the provider called it a "town".
"""

from __future__ import annotations

import time

import httpx
import pytest

from app.integrations import places as module
from app.integrations.places import (
    CACHE_PRECISION,
    NOMINATIM_MIN_INTERVAL,
    GooglePlaces,
    NominatimPlaces,
    Place,
    StubPlaces,
    _coordinate_key,
    _place_from_nominatim,
    _TtlCache,
)

pytestmark = pytest.mark.anyio


# Times Square, as Nominatim actually answers it.
MANHATTAN = {
    "lat": "40.7579747",
    "lon": "-73.9855426",
    "display_name": "7th Avenue, Manhattan, New York County, New York, 10036, United States",
    "addresstype": "road",
    "address": {
        "road": "7th Avenue",
        "neighbourhood": "Manhattan Community Board 5",
        "city": "New York",
        "county": "New York County",
        "state": "New York",
        "postcode": "10036",
        "country": "United States",
        "country_code": "us",
    },
    "boundingbox": ["40.7577", "40.7583", "-73.9860", "-73.9850"],
}

# A country that has no counties and calls its cities towns, to catch a mapping
# that only ever ran against one country.
SMALL_TOWN = {
    "lat": "51.7520",
    "lon": "-1.2577",
    "display_name": "High Street, Oxford, Oxfordshire, England, United Kingdom",
    "addresstype": "road",
    "address": {
        "road": "High Street",
        "town": "Oxford",
        "state": "England",
        "country": "United Kingdom",
        "country_code": "gb",
    },
}

BROOKLYN = {
    "lat": "40.6501038",
    "lon": "-73.9495823",
    "display_name": "Brooklyn, Kings County, New York, United States",
    "addresstype": "suburb",
    "address": {
        "suburb": "Brooklyn",
        "county": "Kings County",
        "state": "New York",
        "country": "United States",
        "country_code": "us",
    },
    "boundingbox": ["40.5507", "40.7395", "-74.0563", "-73.8331"],
}


class TestReadingWhatTheProviderSent:
    def test_the_administrative_chain_is_unpacked(self):
        place = _place_from_nominatim(MANHATTAN)
        assert place is not None
        assert place.country == "United States"
        assert place.country_code == "US"
        assert place.region == "New York"
        assert place.county == "New York County"
        assert place.locality == "New York"
        assert place.road == "7th Avenue"
        assert place.postcode == "10036"

    def test_a_country_that_says_town_still_has_a_locality(self):
        """Nominatim spreads "the city" across city/town/village/municipality
        depending on how the place is administered. Reading only `city` works
        in New York and loses Oxford."""
        place = _place_from_nominatim(SMALL_TOWN)
        assert place is not None
        assert place.locality == "Oxford"
        assert place.region == "England"
        assert place.country_code == "GB"

    def test_a_country_code_is_upper_case(self):
        """Nominatim sends "us"; every other part of the platform says "US"."""
        assert _place_from_nominatim(MANHATTAN).country_code == "US"

    def test_a_bounding_box_becomes_south_west_north_east(self):
        """Nominatim orders it south, north, west, east - which is not the
        order anybody else uses, and getting it wrong puts a search box in the
        sea."""
        place = _place_from_nominatim(BROOKLYN)
        assert place is not None
        south, west, north, east = place.bounding_box
        assert south < north
        assert west < east
        assert (south, north) == (40.5507, 40.7395)
        assert (west, east) == (-74.0563, -73.8331)

    def test_a_response_with_no_coordinates_is_refused(self):
        assert _place_from_nominatim({"display_name": "Somewhere"}) is None
        assert _place_from_nominatim({"lat": "not-a-number", "lon": "0"}) is None

    def test_missing_parts_stay_missing(self):
        """A country with no county should report none, not an empty string
        that renders as a stray comma."""
        place = _place_from_nominatim(SMALL_TOWN)
        assert place.county is None
        assert place.district is None


class TestSayingWhereSomewhereIs:
    def test_a_label_reads_the_way_somebody_would_say_it(self):
        assert _place_from_nominatim(MANHATTAN).label == "7th Avenue, New York, United States"

    def test_the_area_is_the_wider_place(self):
        """"Near you in New York", not "near you in 7th Avenue"."""
        assert _place_from_nominatim(MANHATTAN).area_label == "New York"

    def test_a_label_never_repeats_itself(self):
        """Nominatim frequently reports the same name as city, county and
        country. "Singapore, Singapore, Singapore" is what a naive join gives."""
        place = Place(
            latitude=0,
            longitude=0,
            locality="Singapore",
            county="Singapore",
            country="Singapore",
        )
        assert place.label == "Singapore"

    def test_an_unnamed_place_falls_back_to_the_display_name(self):
        place = Place(latitude=0, longitude=0, display_name="Somewhere in the sea")
        assert place.label == "Somewhere in the sea"


class TestHowFarToLookAround:
    def test_a_street_is_a_short_walk(self):
        assert _place_from_nominatim(MANHATTAN).suggested_radius_km <= 2.0

    def test_a_borough_is_wider_than_a_street(self):
        """Searching "Brooklyn" and searching "5th Avenue" must not look at the
        same area - one is a borough and the other is a road."""
        street = _place_from_nominatim(MANHATTAN).suggested_radius_km
        borough = _place_from_nominatim(BROOKLYN).suggested_radius_km
        assert borough > street * 3

    def test_it_is_taken_from_the_box_when_there_is_one(self):
        # Brooklyn's box is about 21 km north to south.
        assert 5.0 < _place_from_nominatim(BROOKLYN).suggested_radius_km < 30.0

    def test_something_enormous_is_capped(self):
        """A country's bounding box is thousands of kilometres, and searching
        that radius is not a search."""
        huge = Place(latitude=0, longitude=0, bounding_box=(-40.0, -70.0, 5.0, -35.0))
        assert huge.suggested_radius_km <= 60.0

    def test_an_unknown_kind_gets_a_sensible_default(self):
        assert Place(latitude=0, longitude=0, kind="something-new").suggested_radius_km > 0


class TestCaching:
    def test_a_repeated_lookup_does_not_ask_twice(self):
        cache = _TtlCache()
        cache.put("k", "value")
        assert cache.get("k") == "value"

    def test_an_entry_expires(self, monkeypatch):
        cache = _TtlCache()
        cache.put("k", "value")
        # The real clock is captured first: `module.time` is the stdlib module,
        # so a lambda calling `time.monotonic()` would call its own replacement.
        real = time.monotonic
        monkeypatch.setattr(
            module.time, "monotonic", lambda: real() + module.CACHE_TTL_SECONDS + 1
        )
        assert cache.get("k") is None

    def test_it_does_not_grow_without_bound(self):
        cache = _TtlCache()
        for index in range(module.CACHE_MAX_ENTRIES + 50):
            cache.put(f"k{index}", index)
        assert len(cache._entries) <= module.CACHE_MAX_ENTRIES

    def test_the_key_is_too_coarse_to_track_somebody(self):
        """Rounded to about a hundred metres. Fine enough for a street, and
        deliberately not a movement log."""
        assert CACHE_PRECISION <= 3
        # Two points a few metres apart share an entry.
        assert _coordinate_key(40.75797, -73.98554) == _coordinate_key(40.75801, -73.98559)
        # Two points a kilometre apart do not.
        assert _coordinate_key(40.758, -73.985) != _coordinate_key(40.768, -73.985)


class TestTheStubResolvesNothing:
    async def test_it_names_nowhere(self):
        """A geocoder that confidently names somewhere it has never heard of
        sends real people to the wrong address, and looks like it works until
        then."""
        stub = StubPlaces()
        assert await stub.describe(40.7580, -73.9855) is None
        assert await stub.search("Brooklyn") == []


class TestBeingAGoodCitizenOfOpenStreetMap:
    def test_requests_are_spaced_at_least_a_second_apart(self):
        """Their usage policy, and traffic that ignores it gets blocked for
        everybody sharing the deployment's address."""
        assert NOMINATIM_MIN_INTERVAL >= 1.0

    async def test_the_spacing_is_actually_enforced(self, monkeypatch):
        provider = NominatimPlaces("Mado/test", "https://example.test")
        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        class NoNetwork:
            """Fails the moment the request would go out, so the test observes
            the wait and never touches the network."""

            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                raise httpx.ConnectError("no network in tests")

            async def __aexit__(self, *_args):
                return False

        monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(module.httpx, "AsyncClient", NoNetwork)

        # Pretend a request just went out, so the next one must wait.
        provider._last_call = time.monotonic()
        assert await provider._call("/reverse", {}) is None
        assert slept, "a second request went out with no wait at all"
        assert slept[0] > 0

    def test_it_identifies_itself(self):
        """Anonymous traffic is blocked outright."""
        from app.core.config import get_settings

        assert "Mado" in get_settings().nominatim_user_agent


class TestChoosingAProvider:
    def test_google_is_used_when_a_key_exists(self, monkeypatch):
        from app.core import config

        module.reset_provider()
        monkeypatch.setattr(config.get_settings(), "places_provider", "auto", raising=False)
        monkeypatch.setattr(config.get_settings(), "google_maps_api_key", "k", raising=False)
        assert isinstance(module.get_provider(), GooglePlaces)
        module.reset_provider()

    def test_openstreetmap_is_the_keyless_default(self, monkeypatch):
        from app.core import config

        module.reset_provider()
        monkeypatch.setattr(config.get_settings(), "places_provider", "auto", raising=False)
        monkeypatch.setattr(config.get_settings(), "google_maps_api_key", "", raising=False)
        assert isinstance(module.get_provider(), NominatimPlaces)
        module.reset_provider()

    def test_an_unconfigured_deployment_still_resolves_places(self, monkeypatch):
        """This is on the read path now - it is the first thing the app does -
        so falling back to the stub would leave a fresh install unable to tell
        anybody where they are."""
        from app.core import config

        module.reset_provider()
        monkeypatch.setattr(config.get_settings(), "places_provider", "auto", raising=False)
        monkeypatch.setattr(config.get_settings(), "google_maps_api_key", "", raising=False)
        assert not isinstance(module.get_provider(), StubPlaces)
        module.reset_provider()


class TestNoGeographyIsStored:
    def test_there_is_no_table_of_places(self):
        """The rule this module exists to keep. A place table is what limited
        the platform to one city for months."""
        import inspect

        source = inspect.getsource(module)
        assert "Base" not in source
        assert "mapped_column" not in source

    def test_google_and_openstreetmap_return_the_same_shape(self):
        """So a caller ranking things near an explorer and a caller ranking
        things near a searched-for street are the same code path."""
        import inspect

        for provider in (NominatimPlaces, GooglePlaces, StubPlaces):
            described = inspect.signature(provider.describe)
            assert list(described.parameters)[1:] == ["latitude", "longitude"], provider
            searched = inspect.signature(provider.search)
            assert list(searched.parameters)[1:] == ["query", "near", "limit"], provider
