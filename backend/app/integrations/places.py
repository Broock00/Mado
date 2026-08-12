"""The world's geography, borrowed rather than stored.

Mado stores its own things - events, venues, explorers, preferences, the
intelligence that ranks them. It does not store the world. Countries, states,
cities, districts, neighbourhoods, streets and landmarks are resolved when
somebody asks, from services that already know them, and are never curated into
a table.

That is a scaling decision before it is an architectural one. A platform that
keeps its own list of places can only work where a developer has already typed
that place in, which is why this codebase spent its first months able to show
exactly one city. Nothing here needs a row to exist before an explorer in
Brooklyn can be told where they are.

**Two directions, one vocabulary.** `describe` turns coordinates into a place;
`search` turns "Brooklyn" or "5th Avenue" into places. Both return the same
:class:`Place`, so a caller ranking things near an explorer and a caller ranking
things near a searched-for street are the same code path with a different point.

**Nominatim is the default because it is keyless.** OpenStreetMap's operators
ask for an identifying User-Agent, at most one request a second, and that
results be cached. All three are honoured below - the rate limit by a lock, the
caching by a TTL map keyed on coordinates rounded to about a hundred metres,
which is finer than any answer this returns. Without the cache this would be
unusable on a read path, and a read path is exactly where it sits.

**A stub that resolves nothing.** :class:`StubPlaces` returns None rather than a
plausible-looking place. A geocoder that invents "New York" for coordinates in
the sea is indistinguishable from a working one until somebody is sent there.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol

import httpx

from app.core import metrics, tracing
from app.core.config import get_settings
from app.core.logging import get_logger, redact

logger = get_logger("mado.places")

REQUEST_TIMEOUT = 8.0

# OpenStreetMap's usage policy: one request a second, and cache what you get.
NOMINATIM_MIN_INTERVAL = 1.05
# How long a resolved place stays usable. Streets and city boundaries do not
# move; an hour is about keeping memory bounded rather than about freshness.
CACHE_TTL_SECONDS = 3600
CACHE_MAX_ENTRIES = 4096

# Coordinates are rounded to this many decimals before they become a cache key.
# Three decimals is roughly 110 metres, which is finer than the neighbourhood
# this resolves to and coarse enough that two people on the same street share an
# entry. It also means the cache cannot be used to track somebody precisely.
CACHE_PRECISION = 3


@dataclass(slots=True, frozen=True)
class Place:
    """Somewhere, described the way a person would recognise it.

    Every field is optional except the coordinates, because the administrative
    chain differs by country and pretending otherwise is how a geocoder ends up
    calling a London borough a "state". A caller wanting a label should use
    :attr:`label` rather than assembling one.
    """

    latitude: float
    longitude: float
    display_name: str = ""
    country: str | None = None
    country_code: str | None = None
    region: str | None = None
    county: str | None = None
    locality: str | None = None
    district: str | None = None
    neighbourhood: str | None = None
    road: str | None = None
    postcode: str | None = None
    # What kind of thing this is, in the provider's words: city, suburb, road,
    # restaurant. Useful for deciding how wide to search around it.
    kind: str | None = None
    # South, west, north, east. Present for searched places, which is what makes
    # "Brooklyn" a sensible area rather than a single point.
    bounding_box: tuple[float, float, float, float] | None = None
    provider: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def label(self) -> str:
        """The shortest thing a person would say to mean this place."""
        parts = [
            self.road or self.neighbourhood or self.district,
            self.locality or self.county,
            self.country,
        ]
        trimmed = [part for part in parts if part]
        return ", ".join(dict.fromkeys(trimmed)) or self.display_name

    @property
    def area_label(self) -> str:
        """The wider area, for "things near you in X"."""
        return self.locality or self.district or self.county or self.region or self.country or ""

    @property
    def suggested_radius_km(self) -> float:
        """How far to look around this place before widening.

        A street is a few minutes' walk; a city is the whole evening. Taken from
        the kind of place rather than from a constant, so searching "5th Avenue"
        and searching "New York" do not return the same thing.
        """
        if self.bounding_box is not None:
            south, west, north, east = self.bounding_box
            # Half the diagonal, which is the radius that covers the box.
            from app.domains.catalog.repository import distance_km

            span = distance_km(south, west, north, east) / 2
            if span > 0:
                # Rounded, because this is a search radius rather than a
                # measurement and "14.128540438158016 km" invites a precision
                # nobody meant.
                return round(max(1.0, min(span, 60.0)), 1)
        return {
            "road": 1.5,
            "residential": 1.5,
            "pedestrian": 1.5,
            "neighbourhood": 2.0,
            "suburb": 4.0,
            "quarter": 3.0,
            "district": 8.0,
            "city": 20.0,
            "town": 10.0,
            "village": 5.0,
            "county": 40.0,
            "state": 60.0,
        }.get((self.kind or "").lower(), 5.0)


class PlaceProvider(Protocol):
    name: str

    async def describe(self, latitude: float, longitude: float) -> Place | None: ...

    async def search(
        self, query: str, *, near: tuple[float, float] | None = None, limit: int = 5
    ) -> list[Place]: ...


# ------------------------------------------------------------------ caching


class _TtlCache:
    """Small, bounded, and shared by every provider instance.

    Not Redis: this is reference data about the shape of the world, identical
    for every explorer, cheap to recompute and useless to persist across a
    deployment. A process-local map avoids a network hop on the read path that
    the cache exists to make fast.
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        found = self._entries.get(key)
        if found is None:
            return None
        stored_at, value = found
        if time.monotonic() - stored_at > CACHE_TTL_SECONDS:
            self._entries.pop(key, None)
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        if len(self._entries) >= CACHE_MAX_ENTRIES:
            # Oldest first. A strict LRU would need a second structure to track
            # reads, and the access pattern here is dominated by a small set of
            # popular areas that will simply be re-fetched.
            oldest = min(self._entries, key=lambda k: self._entries[k][0])
            self._entries.pop(oldest, None)
        self._entries[key] = (time.monotonic(), value)


_cache = _TtlCache()


def _coordinate_key(latitude: float, longitude: float) -> str:
    return f"r:{round(latitude, CACHE_PRECISION)}:{round(longitude, CACHE_PRECISION)}"


# --------------------------------------------------------------- Nominatim


def _first(address: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = address.get(key)
        if value:
            return str(value)
    return None


def _place_from_nominatim(body: dict[str, Any]) -> Place | None:
    try:
        latitude = float(body["lat"])
        longitude = float(body["lon"])
    except (KeyError, TypeError, ValueError):
        return None

    address = body.get("address") or {}
    box = body.get("boundingbox")
    bounding: tuple[float, float, float, float] | None = None
    if isinstance(box, list) and len(box) == 4:
        try:
            south, north, west, east = (float(value) for value in box)
            bounding = (south, west, north, east)
        except (TypeError, ValueError):
            bounding = None

    return Place(
        latitude=latitude,
        longitude=longitude,
        display_name=str(body.get("display_name") or ""),
        country=_first(address, "country"),
        country_code=(_first(address, "country_code") or "").upper() or None,
        region=_first(address, "state", "province", "region"),
        county=_first(address, "county"),
        # Nominatim spreads "the city" across several keys depending on how the
        # country is administered, and picking one would work in some countries
        # and silently fail in others.
        locality=_first(address, "city", "town", "village", "municipality", "hamlet"),
        district=_first(address, "city_district", "district", "borough"),
        neighbourhood=_first(address, "neighbourhood", "suburb", "quarter"),
        road=_first(address, "road", "pedestrian", "footway"),
        postcode=_first(address, "postcode"),
        kind=str(body.get("addresstype") or body.get("type") or "") or None,
        bounding_box=bounding,
        provider="nominatim",
        raw=body,
    )


class NominatimPlaces:
    """OpenStreetMap. Keyless, and rate limited by its operators."""

    name = "nominatim"

    def __init__(self, user_agent: str, base_url: str) -> None:
        self._user_agent = user_agent
        self._base = base_url.rstrip("/")
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def describe(self, latitude: float, longitude: float) -> Place | None:
        key = _coordinate_key(latitude, longitude)
        cached = _cache.get(key)
        if cached is not None:
            return cached or None

        body = await self._call(
            "/reverse",
            {
                "lat": f"{latitude:.6f}",
                "lon": f"{longitude:.6f}",
                "format": "jsonv2",
                "addressdetails": 1,
                # 18 is roughly building level. Anything coarser loses the street,
                # which is the part that makes "where am I" feel answered.
                "zoom": 18,
            },
        )
        place = _place_from_nominatim(body) if isinstance(body, dict) else None
        # Negative results are cached too, so a boat in the Atlantic does not
        # re-ask on every page view.
        _cache.put(key, place or False)
        return place

    async def search(
        self, query: str, *, near: tuple[float, float] | None = None, limit: int = 5
    ) -> list[Place]:
        query = query.strip()
        if not query:
            return []

        key = f"s:{query.lower()}:{near}:{limit}"
        cached = _cache.get(key)
        if cached is not None:
            return list(cached)

        params: dict[str, Any] = {
            "q": query,
            "format": "jsonv2",
            "addressdetails": 1,
            "limit": max(1, min(limit, 10)),
        }
        if near is not None:
            # Bias towards the explorer without excluding anywhere else: someone
            # typing "Brooklyn" in Manhattan means the one next door, and someone
            # typing it in Addis probably still means New York.
            latitude, longitude = near
            params["viewbox"] = (
                f"{longitude - 1:.4f},{latitude + 1:.4f},{longitude + 1:.4f},{latitude - 1:.4f}"
            )

        body = await self._call("/search", params)
        places = [
            place
            for place in (
                _place_from_nominatim(item) for item in body if isinstance(item, dict)
            )
            if place is not None
        ] if isinstance(body, list) else []
        _cache.put(key, places)
        return places

    async def _call(self, path: str, params: dict[str, Any]) -> Any:
        async with self._lock:
            # One request a second, measured from the last one rather than slept
            # unconditionally, so a cache miss after a quiet minute is not
            # delayed for nothing.
            wait = NOMINATIM_MIN_INTERVAL - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

        started = time.perf_counter()
        outcome = "error"
        try:
            with tracing.span("places"):
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    response = await client.get(
                        f"{self._base}{path}",
                        params=params,
                        # Identifying the application is a condition of use, not
                        # a courtesy - anonymous traffic gets blocked.
                        headers={"User-Agent": self._user_agent, "Accept-Language": "en"},
                    )
                response.raise_for_status()
                body = response.json()
            outcome = "ok"
            return body
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("nominatim_places_failed", path=path, error=redact(str(exc)))
            return None
        finally:
            metrics.dependency_calls.inc("places", outcome)
            metrics.dependency_duration.observe(time.perf_counter() - started, "places")


# ------------------------------------------------------------------- Google


class GooglePlaces:
    """Google's geocoder, used when a key is configured.

    Same shape as Nominatim, different vocabulary: Google reports the
    administrative chain as typed components rather than named keys, so the
    mapping is by component type.
    """

    name = "google"

    # Google's component types, mapped onto the fields above. Ordered by
    # preference within each field.
    _COMPONENTS = {
        "country": ("country",),
        "region": ("administrative_area_level_1",),
        "county": ("administrative_area_level_2",),
        "locality": ("locality", "postal_town"),
        "district": ("sublocality_level_1", "administrative_area_level_3"),
        "neighbourhood": ("neighborhood", "sublocality"),
        "road": ("route",),
        "postcode": ("postal_code",),
    }

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def describe(self, latitude: float, longitude: float) -> Place | None:
        key = _coordinate_key(latitude, longitude)
        cached = _cache.get(key)
        if cached is not None:
            return cached or None

        body = await self._call({"latlng": f"{latitude},{longitude}"})
        results = (body or {}).get("results") or []
        place = self._from_result(results[0]) if results else None
        _cache.put(key, place or False)
        return place

    async def search(
        self, query: str, *, near: tuple[float, float] | None = None, limit: int = 5
    ) -> list[Place]:
        query = query.strip()
        if not query:
            return []
        cache_key = f"gs:{query.lower()}:{near}:{limit}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return list(cached)

        params: dict[str, Any] = {"address": query}
        if near is not None:
            latitude, longitude = near
            params["bounds"] = (
                f"{latitude - 1:.4f},{longitude - 1:.4f}|{latitude + 1:.4f},{longitude + 1:.4f}"
            )
        body = await self._call(params)
        places = [
            place
            for place in ((self._from_result(item)) for item in (body or {}).get("results", []))
            if place is not None
        ][:limit]
        _cache.put(cache_key, places)
        return places

    def _from_result(self, result: dict[str, Any]) -> Place | None:
        location = ((result.get("geometry") or {}).get("location")) or {}
        try:
            latitude = float(location["lat"])
            longitude = float(location["lng"])
        except (KeyError, TypeError, ValueError):
            return None

        components = result.get("address_components") or []
        found: dict[str, str] = {}
        country_code: str | None = None
        for component in components:
            types = set(component.get("types") or [])
            if "country" in types:
                country_code = (component.get("short_name") or "").upper() or None
            for field_name, wanted in self._COMPONENTS.items():
                if field_name in found:
                    continue
                if types & set(wanted):
                    found[field_name] = component.get("long_name") or ""

        viewport = (result.get("geometry") or {}).get("viewport") or {}
        bounding = None
        if viewport:
            southwest = viewport.get("southwest") or {}
            northeast = viewport.get("northeast") or {}
            try:
                bounding = (
                    float(southwest["lat"]),
                    float(southwest["lng"]),
                    float(northeast["lat"]),
                    float(northeast["lng"]),
                )
            except (KeyError, TypeError, ValueError):
                bounding = None

        types = result.get("types") or []
        return Place(
            latitude=latitude,
            longitude=longitude,
            display_name=result.get("formatted_address") or "",
            country=found.get("country"),
            country_code=country_code,
            region=found.get("region"),
            county=found.get("county"),
            locality=found.get("locality"),
            district=found.get("district"),
            neighbourhood=found.get("neighbourhood"),
            road=found.get("road"),
            postcode=found.get("postcode"),
            kind=types[0] if types else None,
            bounding_box=bounding,
            provider="google",
            raw=result,
        )

    async def _call(self, params: dict[str, Any]) -> dict[str, Any] | None:
        started = time.perf_counter()
        outcome = "error"
        try:
            with tracing.span("places"):
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    # The key travels as a query parameter because Google's
                    # geocoding API accepts it no other way. `redact` keeps it
                    # out of the log line below.
                    response = await client.get(
                        "https://maps.googleapis.com/maps/api/geocode/json",
                        params={**params, "key": self._key},
                    )
                response.raise_for_status()
                body = response.json()
            outcome = "ok"
            return body
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("google_places_failed", error=redact(str(exc)))
            return None
        finally:
            metrics.dependency_calls.inc("places", outcome)
            metrics.dependency_duration.observe(time.perf_counter() - started, "places")


# --------------------------------------------------------------------- stub


class StubPlaces:
    """Resolves nothing, on purpose.

    Used in tests and offline development. It would be easy to return a
    plausible place for any coordinates, and that is exactly the failure a stub
    must not have: a geocoder that confidently names somewhere it has never
    heard of sends real people to the wrong address, and looks like it works
    until then.
    """

    name = "stub"

    async def describe(self, latitude: float, longitude: float) -> Place | None:
        return None

    async def search(
        self, query: str, *, near: tuple[float, float] | None = None, limit: int = 5
    ) -> list[Place]:
        return []


@lru_cache(maxsize=1)
def get_provider() -> PlaceProvider:
    """The configured provider.

    Google when a key exists, OpenStreetMap otherwise, and the stub only when
    asked for explicitly - an unconfigured deployment should still be able to
    tell somebody where they are, because that is now the first thing the app
    does.
    """
    settings = get_settings()
    choice = settings.places_provider
    if choice == "stub":
        return StubPlaces()
    if choice in {"auto", "google"} and settings.google_maps_api_key:
        return GooglePlaces(settings.google_maps_api_key)
    if choice == "google":
        logger.warning("places_provider_unconfigured", requested="google")
    return NominatimPlaces(settings.nominatim_user_agent, settings.nominatim_base_url)


def reset_provider() -> None:
    get_provider.cache_clear()
    _cache._entries.clear()
