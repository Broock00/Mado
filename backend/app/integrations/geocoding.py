"""Geocoding.

Spec 82.01 s10 draws a boundary that this module exists to respect: **maps are
infrastructure, not a catalogue**. A map vendor turns an address into
coordinates and draws tiles. It does not decide what is worth visiting, does not
supply the listings, and does not rank anything. Mado's catalogue is Mado's, and
the nearby ordering is computed here in PostGIS from that catalogue.

The practical consequence is the shape of this file: a narrow interface with two
methods, several implementations behind it, and no vendor type reaching any
caller. Swapping providers is a configuration change.

**Why publishers needed this.** Creating a venue previously took raw latitude and
longitude. Nobody adding a cafe knows its coordinates, so in practice the field
was either left wrong or the venue was never created - and a listing with bad
coordinates is worse than one with none, because it appears confidently in
"near you" for the wrong neighbourhood.

Three providers ship:

* :class:`GoogleGeocoder` - the vendor spec 82.01 names, used when a key exists.
* :class:`NominatimGeocoder` - OpenStreetMap's service. Keyless, so development
  and self-hosting work without a billing account. Rate limited by its operators
  to roughly one request a second, which is fine for publishing and would not be
  for anything on a read path.
* :class:`LocalGeocoder` - resolves against the city's own neighbourhoods. Not a
  real geocoder; it exists so tests and offline development are deterministic and
  never depend on a third party being reachable.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger, redact

logger = get_logger("mado.geocoding")

# Confidence below which a result is not good enough to place a pin on. A vague
# match ("Ethiopia") is worse than no match: it puts a venue in the middle of the
# country and makes every distance calculation about it wrong.
MIN_CONFIDENCE = 0.4

REQUEST_TIMEOUT = 8.0


@dataclass(slots=True)
class GeocodeResult:
    latitude: float
    longitude: float
    # What the provider thinks the address actually is, which is usually tidier
    # than what was typed and worth showing back for confirmation.
    formatted_address: str
    # 0-1. Callers refuse anything below MIN_CONFIDENCE rather than guessing.
    confidence: float
    provider: str

    @property
    def is_usable(self) -> bool:
        return self.confidence >= MIN_CONFIDENCE


class Geocoder(Protocol):
    name: str

    async def geocode(self, address: str, *, city: str, country: str) -> GeocodeResult | None: ...


class GoogleGeocoder:
    """Google Geocoding API. The vendor named in spec 82.01."""

    name = "google"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def geocode(self, address: str, *, city: str, country: str) -> GeocodeResult | None:
        params = {
            "address": f"{address}, {city}, {country}",
            # Biasing to the country stops "Bole" resolving to somewhere on
            # another continent that happens to share the name.
            "region": country[:2].lower(),
        }
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.get(
                    "https://maps.googleapis.com/maps/api/geocode/json",
                    params=params,
                    # Header auth rather than a query parameter: httpx renders the
                    # full URL into transport errors, so a key in the query string
                    # ends up in the logs the first time a request fails.
                    headers={"X-Goog-Api-Key": self._api_key},
                )
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            logger.warning("google_geocode_failed", error=redact(str(exc)))
            return None

        results = body.get("results") or []
        if not results:
            return None

        best = results[0]
        location = best.get("geometry", {}).get("location", {})
        if "lat" not in location or "lng" not in location:
            return None

        return GeocodeResult(
            latitude=float(location["lat"]),
            longitude=float(location["lng"]),
            formatted_address=best.get("formatted_address", address),
            confidence=_google_confidence(best),
            provider=self.name,
        )


def _google_confidence(result: dict) -> float:
    """Map Google's location_type onto a 0-1 confidence.

    Google reports how it arrived at a point rather than how sure it is.
    ROOFTOP means it found the building; APPROXIMATE means it found the area and
    picked a middle, which is not good enough to pin a venue to.
    """
    location_type = result.get("geometry", {}).get("location_type", "")
    if result.get("partial_match"):
        return 0.3
    return {
        "ROOFTOP": 1.0,
        "RANGE_INTERPOLATED": 0.8,
        "GEOMETRIC_CENTER": 0.6,
        "APPROXIMATE": 0.35,
    }.get(location_type, 0.5)


class NominatimGeocoder:
    """OpenStreetMap's geocoder. Keyless, so it works with no billing account.

    Their usage policy requires an identifying User-Agent and roughly one request
    per second. Publishing is well inside that; this must never be put on a read
    path.
    """

    name = "nominatim"

    async def geocode(self, address: str, *, city: str, country: str) -> GeocodeResult | None:
        params = {
            "q": f"{address}, {city}, {country}",
            "format": "jsonv2",
            "limit": "1",
            "addressdetails": "1",
        }
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.get(
                    "https://nominatim.openstreetmap.org/search",
                    params=params,
                    headers={"User-Agent": "Mado/1.0 (city discovery platform)"},
                )
                response.raise_for_status()
                results = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("nominatim_geocode_failed", error=redact(str(exc)))
            return None

        if not results:
            return None

        best = results[0]
        try:
            latitude, longitude = float(best["lat"]), float(best["lon"])
        except (KeyError, TypeError, ValueError):
            return None

        return GeocodeResult(
            latitude=latitude,
            longitude=longitude,
            formatted_address=best.get("display_name", address),
            confidence=_nominatim_confidence(best),
            provider=self.name,
        )


# Administrative area types that are never precise enough to pin a venue to,
# whatever else the result says. A match on "Ethiopia" is a failure dressed up as
# a success: it returns real coordinates in the middle of the country.
_TOO_BROAD = frozenset(
    {"country", "state", "region", "province", "county", "city", "town", "municipality"}
)


def _nominatim_confidence(result: dict) -> float:
    """Estimate how precisely a result matched.

    Driven by `place_rank`, where **higher is more specific**: 30 is a building or
    point of interest and 4 is a country. Getting this backwards marked every
    named landmark unusable - "National Museum of Ethiopia" resolves to exactly
    the right point at rank 30, and was being rejected for it.

    `importance` is deliberately not used: it measures how well-known a place is,
    not how well the query matched, so a famous city outscores the exact street
    that was actually asked for.
    """
    if (result.get("addresstype") or "").lower() in _TOO_BROAD:
        return 0.2

    try:
        place_rank = int(result.get("place_rank", 0))
    except (TypeError, ValueError):
        return 0.4

    if place_rank >= 30:
        return 0.9  # building or point of interest
    if place_rank >= 26:
        return 0.75  # street or address level
    if place_rank >= 22:
        return 0.6  # neighbourhood or suburb
    if place_rank >= 16:
        return 0.4  # town or district - borderline
    return 0.2  # region or broader


class LocalGeocoder:
    """Offline stand-in resolving against known neighbourhoods.

    Not a geocoder. It matches an address against neighbourhood names already in
    the database and returns that neighbourhood's centre, which is deliberately
    reported at low confidence - the caller should treat it as "roughly here",
    because that is all it is. Exists so tests are deterministic and development
    never depends on a third party.
    """

    name = "local"

    def __init__(self, places: dict[str, tuple[float, float]] | None = None) -> None:
        self._places = places or {}

    async def geocode(self, address: str, *, city: str, country: str) -> GeocodeResult | None:
        needle = address.casefold()
        for name, (latitude, longitude) in self._places.items():
            if name.casefold() in needle:
                return GeocodeResult(
                    latitude=latitude,
                    longitude=longitude,
                    formatted_address=f"{address}, {city}",
                    # Just above the usability floor: a neighbourhood centre is
                    # good enough to show a map, not good enough to claim precision.
                    confidence=0.45,
                    provider=self.name,
                )
        return None


@lru_cache
def get_geocoder() -> Geocoder:
    """Select the geocoder.

    ``auto`` prefers Google when a key is configured and falls back to the
    keyless service otherwise, so a development machine works without a billing
    account and production uses the vendor the specs name.
    """
    settings = get_settings()
    choice = settings.geocoding_provider

    if choice == "auto":
        choice = "google" if settings.google_maps_api_key else "nominatim"

    if choice == "google":
        if not settings.google_maps_api_key:
            logger.warning("google_geocoder_selected_without_key_falling_back")
            return NominatimGeocoder()
        return GoogleGeocoder(settings.google_maps_api_key)
    if choice == "local":
        return LocalGeocoder()
    return NominatimGeocoder()
