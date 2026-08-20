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
    DETAILS_FIELD_MASK,
    NOMINATIM_MIN_INTERVAL,
    SEARCH_FIELD_MASK,
    GooglePlaces,
    NominatimPlaces,
    Place,
    StubPlaces,
    Suggestion,
    _coordinate_key,
    _place_from_google,
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


# --------------------------------------------------------------- Google (New)

# Brooklyn, as Places API (New) returns it for a details lookup. Trimmed to the
# fields the field mask actually asks for.
GOOGLE_BROOKLYN = {
    "id": "ChIJCSF8lBZEwokRhngABHRcdoI",
    "types": ["locality", "political"],
    "formattedAddress": "Brooklyn, NY, USA",
    "location": {"latitude": 40.6781784, "longitude": -73.9441579},
    "viewport": {
        "low": {"latitude": 40.551042, "longitude": -74.05663},
        "high": {"latitude": 40.739446, "longitude": -73.833365},
    },
    "displayName": {"text": "Brooklyn", "languageCode": "en"},
    "addressComponents": [
        {"longText": "Brooklyn", "shortText": "Brooklyn", "types": ["political", "locality"]},
        {"longText": "Kings County", "shortText": "Kings County",
         "types": ["administrative_area_level_2", "political"]},
        {"longText": "New York", "shortText": "NY",
         "types": ["administrative_area_level_1", "political"]},
        {"longText": "United States", "shortText": "US", "types": ["country", "political"]},
    ],
}

# A venue, which is the case where the address components carry no name at all.
GOOGLE_CAFE = {
    "id": "ChIJVXealLU_xokRja_At0z9AGY",
    "types": ["coffee_shop", "cafe", "point_of_interest", "establishment"],
    "formattedAddress": "300 Webster St, Oakland, CA 94607, USA",
    "location": {"latitude": 37.8007, "longitude": -122.2760},
    "displayName": {"text": "Blue Bottle Coffee", "languageCode": "en"},
    "addressComponents": [
        {"longText": "300", "shortText": "300", "types": ["street_number"]},
        {"longText": "Webster Street", "shortText": "Webster St", "types": ["route"]},
        {"longText": "Oakland", "shortText": "Oakland", "types": ["locality", "political"]},
        {"longText": "California", "shortText": "CA",
         "types": ["administrative_area_level_1", "political"]},
        {"longText": "United States", "shortText": "US", "types": ["country", "political"]},
    ],
    "attributions": [{"provider": "Some Data Partner", "providerUri": "https://example.test"}],
}

GOOGLE_AUTOCOMPLETE = {
    "suggestions": [
        {
            "placePrediction": {
                "place": "places/ChIJCSF8lBZEwokRhngABHRcdoI",
                "placeId": "ChIJCSF8lBZEwokRhngABHRcdoI",
                "text": {"text": "Brooklyn, NY, USA"},
                "structuredFormat": {
                    "mainText": {"text": "Brooklyn"},
                    "secondaryText": {"text": "NY, USA"},
                },
                "types": ["locality", "political", "geocode"],
                "distanceMeters": 8421,
            }
        },
        # A phrase with no place behind it. Google returns these freely and there
        # is nothing to resolve if somebody taps one.
        {"queryPrediction": {"text": {"text": "brooklyn pizza"}}},
    ]
}


class _Recorder:
    """Stands in for httpx, recording the request and replaying a canned body.

    Every assertion about field masks, headers and session tokens goes through
    this: they are the parts that decide what Google charges, and none of them
    is visible in the parsed result.
    """

    def __init__(self, body):
        self.body = body
        self.calls: list[dict] = []

    def factory(self, **_kwargs):
        recorder = self

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return recorder.body

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def request(self, method, url, *, params=None, json=None, headers=None):
                recorder.calls.append(
                    {
                        "method": method,
                        "url": url,
                        "params": params or {},
                        "json": json or {},
                        "headers": headers or {},
                    }
                )
                return Response()

        return Client()

    @property
    def last(self) -> dict:
        return self.calls[-1]


@pytest.fixture
def recorder(monkeypatch):
    def install(body):
        instance = _Recorder(body)
        monkeypatch.setattr(module.httpx, "AsyncClient", instance.factory)
        return instance

    module.reset_provider()
    yield install
    module.reset_provider()


class TestReadingWhatGoogleSent:
    def test_the_administrative_chain_is_unpacked(self):
        """Google spells its components `longText`/`shortText`, where the
        Geocoding API says `long_name`/`short_name`. Reading only one spelling
        empties every field for whichever service is not being tested."""
        place = _place_from_google(GOOGLE_BROOKLYN)
        assert place is not None
        assert place.locality == "Brooklyn"
        assert place.county == "Kings County"
        assert place.region == "New York"
        assert place.country == "United States"
        assert place.country_code == "US"

    def test_the_viewport_becomes_south_west_north_east(self):
        """Places (New) names the corners `low` and `high`. Read in the wrong
        order they put the search box in the sea."""
        south, west, north, east = _place_from_google(GOOGLE_BROOKLYN).bounding_box
        assert south < north
        assert west < east
        assert (south, west) == (40.551042, -74.05663)

    def test_a_venue_keeps_the_name_it_was_searched_for(self):
        """A venue's name is not an address component, so it arrives only as the
        display name. Without carrying it, "Blue Bottle Coffee" resolves to
        "Webster Street" and the explorer never sees what they typed."""
        place = _place_from_google(GOOGLE_CAFE)
        assert place.name == "Blue Bottle Coffee"
        assert place.label.startswith("Blue Bottle Coffee")

    def test_an_area_does_not_repeat_its_own_name(self):
        """Google returns a displayName for a city too, where it merely repeats
        the locality - so treating every displayName as a name gives
        "Brooklyn, Brooklyn"."""
        place = _place_from_google(GOOGLE_BROOKLYN)
        assert place.name == ""
        assert place.label == "Brooklyn, United States"

    def test_the_place_id_is_kept(self):
        """The one field Google's terms allow to be stored indefinitely, and the
        only reason a venue can be recognised as the same place twice."""
        assert _place_from_google(GOOGLE_BROOKLYN).place_id == GOOGLE_BROOKLYN["id"]

    def test_attributions_travel_with_the_place(self):
        """Required to be displayed wherever the place is. Dropping them is a
        licence breach rather than a missing detail."""
        assert _place_from_google(GOOGLE_CAFE).attributions == ("Some Data Partner",)

    def test_a_place_with_no_coordinates_is_refused(self):
        assert _place_from_google({"id": "x", "displayName": {"text": "Nowhere"}}) is None

    def test_a_google_kind_gets_a_sensible_radius(self):
        """The radius table has to speak both providers' vocabularies: Google
        says "locality" where OpenStreetMap says "city", and an unmapped kind
        would silently search the 5 km default around a whole city."""
        city = _place_from_google(GOOGLE_BROOKLYN).suggested_radius_km
        cafe = _place_from_google(GOOGLE_CAFE).suggested_radius_km
        assert cafe < city


class TestSpendingGooglesMoneyCarefully:
    async def test_search_sends_a_field_mask(self, recorder):
        """Places (New) refuses a request without one and bills by the most
        expensive field named. No mask is not a cheaper request; it is an
        error."""
        calls = recorder({"places": [GOOGLE_BROOKLYN]})
        await GooglePlaces("k").search("Brooklyn")
        assert calls.last["headers"]["X-Goog-FieldMask"] == SEARCH_FIELD_MASK

    async def test_details_sends_a_field_mask(self, recorder):
        calls = recorder(GOOGLE_BROOKLYN)
        await GooglePlaces("k").details("ChIJCSF8lBZEwokRhngABHRcdoI")
        assert calls.last["headers"]["X-Goog-FieldMask"] == DETAILS_FIELD_MASK

    def test_the_masks_ask_for_nothing_beyond_locating_a_place(self):
        """Every extra field moves the request up a billing tier. Opening hours,
        ratings, photos and phone numbers are all things Mado holds its own
        opinion of and must never be paid for here."""
        forbidden = {
            "regularOpeningHours",
            "rating",
            "userRatingCount",
            "photos",
            "nationalPhoneNumber",
            "internationalPhoneNumber",
            "reviews",
            "websiteUri",
            "priceLevel",
        }
        for mask in (DETAILS_FIELD_MASK, SEARCH_FIELD_MASK):
            named = {field.removeprefix("places.") for field in mask.split(",")}
            assert not named & forbidden, mask

    async def test_autocomplete_sends_no_field_mask(self, recorder):
        """It is billed per request rather than per field, and the API rejects a
        mask on it - so sending one turns every keystroke into an error."""
        calls = recorder(GOOGLE_AUTOCOMPLETE)
        await GooglePlaces("k").autocomplete("brook")
        assert "X-Goog-FieldMask" not in calls.last["headers"]

    async def test_the_session_token_reaches_both_halves_of_a_search(self, recorder):
        """The whole reason autocomplete and details are separate calls. Without
        the token on both, a nine-letter place is billed as nine lookups
        instead of one."""
        calls = recorder(GOOGLE_AUTOCOMPLETE)
        provider = GooglePlaces("k")
        await provider.autocomplete("brook", session_token="abc")
        assert calls.last["json"]["sessionToken"] == "abc"

        calls = recorder(GOOGLE_BROOKLYN)
        await provider.details("ChIJCSF8lBZEwokRhngABHRcdoI", session_token="abc")
        assert calls.last["params"]["sessionToken"] == "abc"

    async def test_the_key_travels_in_a_header_on_every_places_call(self, recorder):
        """This project leaked a key in a URL once. httpx renders the full URL
        into transport errors, so a key in the query string reaches the logs the
        first time a request fails."""
        for body, call in (
            ({"places": []}, lambda p: p.search("Brooklyn")),
            (GOOGLE_AUTOCOMPLETE, lambda p: p.autocomplete("brook")),
            (GOOGLE_BROOKLYN, lambda p: p.details("ChIJ123")),
        ):
            calls = recorder(body)
            await call(GooglePlaces("secret-key"))
            assert calls.last["headers"]["X-Goog-Api-Key"] == "secret-key"
            assert "secret-key" not in str(calls.last["params"])
            assert "key" not in calls.last["params"]


class TestReverseGeocodingIsADifferentService:
    """`describe` talks to the Geocoding API, which Places (New) does not do.

    It authenticates differently and reports failure differently from everything
    else in the class, and both differences fail silently when got wrong - which
    is what these tests exist to stop happening again.
    """

    async def test_the_key_goes_in_the_query_string(self, recorder):
        """Verified against the live API: the Geocoding service does not read
        `X-Goog-Api-Key`. Sent one, it answers HTTP 200, `REQUEST_DENIED`, and
        "You must use an API key" - so reverse geocoding returns nothing for
        every explorer while looking like a service that found no match."""
        calls = recorder({"status": "OK", "results": []})
        await GooglePlaces("secret-key").describe(40.7, -73.9)
        assert calls.last["params"]["key"] == "secret-key"
        assert "X-Goog-Api-Key" not in calls.last["headers"]

    async def test_a_refusal_is_not_mistaken_for_an_empty_answer(self, recorder):
        """The Geocoding API reports a rejected key with HTTP 200, so
        `raise_for_status` sees nothing wrong. Left unchecked, a
        misconfigured key is indistinguishable from the middle of the sea."""
        recorder(
            {
                "status": "REQUEST_DENIED",
                "error_message": "The provided API key is invalid.",
                "results": [],
            }
        )
        assert await GooglePlaces("k").describe(40.7, -73.9) is None

    async def test_a_refusal_is_not_cached(self, recorder):
        """A negative cache entry would make a configuration error look
        intermittent, and keep it looking fixed for an hour after it was."""
        calls = recorder({"status": "REQUEST_DENIED", "results": []})
        provider = GooglePlaces("k")
        await provider.describe(40.7, -73.9)
        await provider.describe(40.7, -73.9)
        assert len(calls.calls) == 2

    async def test_genuinely_nowhere_is_cached(self, recorder):
        """A boat in the Atlantic has a real answer, and asking again on every
        page view would spend a lookup to be told the same thing."""
        calls = recorder({"status": "ZERO_RESULTS", "results": []})
        provider = GooglePlaces("k")
        await provider.describe(0.0, -30.0)
        await provider.describe(0.0, -30.0)
        assert len(calls.calls) == 1

    async def test_a_geocoding_result_still_becomes_a_place(self, recorder):
        """Different shape from Places (New) - `geometry.location.lat` rather
        than `location.latitude`, `southwest`/`northeast` rather than
        `low`/`high` - and the same Place out of it."""
        recorder(
            {
                "status": "OK",
                "results": [
                    {
                        "place_id": "ChIJOwg_06VPwokRYv534QaPC8g",
                        "formatted_address": "New York, NY, USA",
                        "types": ["locality", "political"],
                        "geometry": {
                            "location": {"lat": 40.7127753, "lng": -74.0059728},
                            "viewport": {
                                "southwest": {"lat": 40.4773991, "lng": -74.2590899},
                                "northeast": {"lat": 40.9175771, "lng": -73.7002721},
                            },
                        },
                        "address_components": [
                            {"long_name": "New York", "short_name": "New York",
                             "types": ["locality", "political"]},
                            {"long_name": "United States", "short_name": "US",
                             "types": ["country", "political"]},
                        ],
                    }
                ],
            }
        )
        place = await GooglePlaces("k").describe(40.71, -74.00)
        assert place is not None
        assert place.locality == "New York"
        assert place.country_code == "US"
        assert place.place_id == "ChIJOwg_06VPwokRYv534QaPC8g"
        south, west, north, east = place.bounding_box
        assert south < north and west < east

    async def test_search_is_biased_and_never_restricted(self, recorder):
        """A restriction would make somebody in Manhattan unable to find
        Nairobi, which is a search box that only works for people who already
        know where they are."""
        calls = recorder({"places": []})
        await GooglePlaces("k").search("Brooklyn", near=(40.75, -73.98))
        assert "locationBias" in calls.last["json"]
        assert "locationRestriction" not in calls.last["json"]

    async def test_autocomplete_restricts_no_type(self, recorder):
        """The box has to find countries, cities, neighbourhoods, streets,
        landmarks and venues alike. Every restriction is a category somebody
        cannot find."""
        calls = recorder(GOOGLE_AUTOCOMPLETE)
        await GooglePlaces("k").autocomplete("brook")
        assert "includedPrimaryTypes" not in calls.last["json"]


class TestSuggestions:
    async def test_a_prediction_becomes_a_suggestion(self, recorder):
        calls = recorder(GOOGLE_AUTOCOMPLETE)
        found = await GooglePlaces("k").autocomplete("brook")
        assert calls.calls, "no request went out"
        assert len(found) == 1
        assert found[0].place_id == "ChIJCSF8lBZEwokRhngABHRcdoI"
        assert found[0].primary == "Brooklyn"
        assert found[0].secondary == "NY, USA"
        assert found[0].distance_metres == 8421

    async def test_a_query_prediction_is_dropped(self, recorder):
        """It has no place behind it, so a row for it would be a suggestion that
        does nothing when tapped."""
        recorder(GOOGLE_AUTOCOMPLETE)
        found = await GooglePlaces("k").autocomplete("brook")
        assert all(item.place_id for item in found)

    async def test_a_suggestion_carries_no_coordinates(self):
        """The point of the split. A suggestion with coordinates would mean
        every row in the dropdown had been resolved and billed, including the
        five nobody picks."""
        assert not hasattr(Suggestion(place_id="x", text="y"), "latitude")

    async def test_predictions_are_not_cached(self, recorder):
        """They belong to the session that asked for them. Serving a second
        explorer from the first one's cache would group their searches under one
        token and rank the second against the first's context."""
        calls = recorder(GOOGLE_AUTOCOMPLETE)
        provider = GooglePlaces("k")
        await provider.autocomplete("brook", session_token="one")
        await provider.autocomplete("brook", session_token="two")
        assert len(calls.calls) == 2


class TestResolvingAChoice:
    async def test_details_returns_a_place(self, recorder):
        recorder(GOOGLE_BROOKLYN)
        place = await GooglePlaces("k").details("ChIJCSF8lBZEwokRhngABHRcdoI")
        assert place is not None
        assert place.locality == "Brooklyn"
        assert place.provider == "google"

    async def test_an_openstreetmap_id_is_refused_rather_than_looked_up(self, recorder):
        """A provider switched under a client mid-search. Asking Google for an
        OSM id returns whatever place happens to share the number."""
        calls = recorder(GOOGLE_BROOKLYN)
        assert await GooglePlaces("k").details("osm:W12345") is None
        assert not calls.calls, "a request went out for a foreign identifier"

    async def test_a_google_id_is_refused_by_openstreetmap_too(self):
        provider = NominatimPlaces("Mado/test", "https://example.test")
        assert await provider.details("ChIJCSF8lBZEwokRhngABHRcdoI") is None

    async def test_the_keyless_provider_issues_ids_it_can_resolve(self):
        """A suggestion whose identifier its own provider cannot look up is a
        row that does nothing when tapped."""
        place = _place_from_nominatim({**BROOKLYN, "osm_type": "relation", "osm_id": 9691750})
        assert place.place_id == "osm:R9691750"

    async def test_a_failed_lookup_is_not_cached_as_a_missing_place(self, monkeypatch):
        """A network blip must not mean the place somebody just picked does not
        exist for the next hour - they retry and get the same nothing, which
        reads as a broken suggestion rather than a bad moment."""
        module.reset_provider()

        class NoNetwork:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                raise httpx.ConnectError("no network in tests")

            async def __aexit__(self, *_args):
                return False

        monkeypatch.setattr(module.httpx, "AsyncClient", NoNetwork)
        provider = GooglePlaces("k")
        assert await provider.details("ChIJ123") is None
        # Nothing was written, so a later successful call is not shadowed.
        assert module._cache.get("gd:ChIJ123") is None
        module.reset_provider()

    async def test_a_place_that_really_is_missing_is_cached(self, recorder):
        """The other half: Google answering "no such place" is a real answer,
        and asking again on every render would spend a lookup to hear it."""
        calls = recorder({})
        provider = GooglePlaces("k")
        await provider.details("ChIJ123")
        await provider.details("ChIJ123")
        assert len(calls.calls) == 1

    async def test_a_result_with_no_stable_id_is_not_offered(self):
        """Nominatim's own `place_id` is documented as unstable across imports,
        so a suggestion built on one resolves to something else after a
        reimport - or to nothing."""
        assert _place_from_nominatim(BROOKLYN).place_id is None


class TestTheStubStillResolvesNothing:
    async def test_it_suggests_nothing_either(self):
        stub = StubPlaces()
        assert await stub.autocomplete("Brook") == []
        assert await stub.details("anything") is None


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
        things near a searched-for street are the same code path - and so
        swapping providers stays a config change, which is the whole premise of
        `app/integrations`."""
        import inspect

        expected = {
            "describe": ["latitude", "longitude"],
            "search": ["query", "near", "limit"],
            "autocomplete": ["query", "near", "limit", "session_token"],
            "details": ["place_id", "session_token"],
        }
        for provider in (NominatimPlaces, GooglePlaces, StubPlaces):
            for method, parameters in expected.items():
                signature = inspect.signature(getattr(provider, method))
                assert list(signature.parameters)[1:] == parameters, (provider, method)
