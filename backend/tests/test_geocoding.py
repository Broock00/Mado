"""Geocoding tests.

No network. These cover the mapping from a provider's response onto our notion of
confidence, which is where the judgement lives - the HTTP call itself is
uninteresting and testing it would only assert that a third party is up.

The confidence mapping earned its own tests: a first attempt had Nominatim's
`place_rank` inverted, which marked every named landmark unusable. "National
Museum of Ethiopia" resolves to exactly the right point and was being rejected
for it, and nothing in the type system or the happy path noticed.
"""

from __future__ import annotations

import pytest

from app.integrations.geocoding import (
    MIN_CONFIDENCE,
    GeocodeResult,
    LocalGeocoder,
    _google_confidence,
    _nominatim_confidence,
)


class TestNominatimConfidence:
    """Higher place_rank means more specific: 30 is a building, 4 is a country."""

    def test_a_point_of_interest_is_confident(self):
        # What "National Museum of Ethiopia" actually returns.
        result = {"place_rank": 30, "addresstype": "tourism", "category": "tourism"}
        assert _nominatim_confidence(result) >= 0.9

    def test_a_street_is_usable(self):
        assert _nominatim_confidence({"place_rank": 26, "addresstype": "road"}) >= MIN_CONFIDENCE

    def test_a_park_is_usable(self):
        # "Meskel Square" comes back as a park at rank 24.
        assert _nominatim_confidence({"place_rank": 24, "addresstype": "park"}) >= MIN_CONFIDENCE

    def test_a_country_is_refused(self):
        """Returning the middle of Ethiopia is a failure wearing a success's clothes."""
        result = {"place_rank": 4, "addresstype": "country", "category": "boundary"}
        assert _nominatim_confidence(result) < MIN_CONFIDENCE

    def test_a_city_is_refused_whatever_its_rank(self):
        """The addresstype guard fires before the rank is even considered."""
        assert _nominatim_confidence({"place_rank": 30, "addresstype": "city"}) < MIN_CONFIDENCE

    def test_a_missing_rank_does_not_raise(self):
        assert 0.0 <= _nominatim_confidence({}) <= 1.0

    def test_a_malformed_rank_does_not_raise(self):
        assert 0.0 <= _nominatim_confidence({"place_rank": "very precise"}) <= 1.0

    def test_confidence_rises_with_specificity(self):
        ranks = [4, 16, 22, 26, 30]
        scores = [_nominatim_confidence({"place_rank": r}) for r in ranks]
        assert scores == sorted(scores)


class TestGoogleConfidence:
    def test_a_rooftop_match_is_certain(self):
        assert _google_confidence({"geometry": {"location_type": "ROOFTOP"}}) == 1.0

    def test_an_approximate_match_is_refused(self):
        """APPROXIMATE means Google found the area and picked a middle."""
        result = {"geometry": {"location_type": "APPROXIMATE"}}
        assert _google_confidence(result) < MIN_CONFIDENCE

    def test_a_partial_match_is_refused(self):
        result = {"geometry": {"location_type": "ROOFTOP"}, "partial_match": True}
        assert _google_confidence(result) < MIN_CONFIDENCE

    def test_an_unknown_location_type_is_mid(self):
        assert 0.0 < _google_confidence({"geometry": {}}) <= 1.0


class TestUsabilityGate:
    def test_a_low_confidence_result_is_not_usable(self):
        result = GeocodeResult(9.0, 38.7, "Somewhere", confidence=0.2, provider="test")
        assert not result.is_usable

    def test_a_confident_result_is_usable(self):
        result = GeocodeResult(9.0, 38.7, "Somewhere", confidence=0.9, provider="test")
        assert result.is_usable


class TestLocalGeocoder:
    """The offline stand-in. Deterministic, and honest about being approximate."""

    @pytest.mark.anyio
    async def test_matches_a_known_neighbourhood(self):
        geocoder = LocalGeocoder({"Piassa": (9.0348, 38.7503)})
        result = await geocoder.geocode(
            "Wawel Street, Piassa", city="Addis Ababa", country="Ethiopia"
        )
        assert result is not None
        assert result.latitude == pytest.approx(9.0348)

    @pytest.mark.anyio
    async def test_a_neighbourhood_centre_is_only_just_usable(self):
        """It is "roughly here", and the confidence should say so."""
        geocoder = LocalGeocoder({"Piassa": (9.0348, 38.7503)})
        result = await geocoder.geocode("Piassa", city="Addis Ababa", country="Ethiopia")
        assert result is not None
        assert result.is_usable
        assert result.confidence < 0.6

    @pytest.mark.anyio
    async def test_an_unknown_place_returns_nothing(self):
        geocoder = LocalGeocoder({"Piassa": (9.0348, 38.7503)})
        assert (
            await geocoder.geocode("Atlantis", city="Addis Ababa", country="Ethiopia") is None
        )


class TestReverseGeocoding:
    """Naming a dropped pin.

    The publisher already chose the point, so this is a label rather than an
    answer. Everything here is about degrading to "no label" instead of failing.
    """

    @pytest.mark.anyio
    async def test_the_local_geocoder_names_the_nearest_known_place(self):
        geocoder = LocalGeocoder(
            {"Piassa": (9.0348, 38.7503), "Bole": (8.9945, 38.7896)}
        )
        assert "Piassa" in (await geocoder.reverse(9.0350, 38.7500) or "")
        assert "Bole" in (await geocoder.reverse(8.9950, 38.7890) or "")

    @pytest.mark.anyio
    async def test_it_returns_nothing_rather_than_guessing(self):
        assert await LocalGeocoder({}).reverse(9.0, 38.7) is None

    def test_the_address_is_trimmed_to_what_a_person_would_say(self):
        """Nominatim returns the whole administrative chain; nobody talks that way."""
        from app.integrations.geocoding import _short_address

        label = _short_address(
            {
                "address": {
                    "amenity": "Tomoca Coffee",
                    "road": "Wawel Street",
                    "suburb": "Piassa",
                    "city": "Addis Ababa",
                    "state": "Addis Ababa",
                    "postcode": "1000",
                    "country": "Ethiopia",
                    "country_code": "et",
                }
            }
        )
        assert label == "Tomoca Coffee, Wawel Street, Piassa, Addis Ababa"
        assert "Ethiopia" not in label
        assert "1000" not in label

    def test_duplicate_names_in_the_chain_appear_once(self):
        """A city whose suburb shares its name should not be repeated."""
        from app.integrations.geocoding import _short_address

        label = _short_address(
            {"address": {"road": "Main Street", "suburb": "Addis Ababa", "city": "Addis Ababa"}}
        )
        assert label.count("Addis Ababa") == 1

    def test_it_falls_back_to_the_full_name_when_unstructured(self):
        from app.integrations.geocoding import _short_address

        assert _short_address({"display_name": "Somewhere, Ethiopia"}) == "Somewhere, Ethiopia"

    def test_an_empty_response_yields_nothing(self):
        from app.integrations.geocoding import _short_address

        assert _short_address({}) is None
