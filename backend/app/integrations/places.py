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

**Four questions, one vocabulary.** `describe` turns coordinates into a place;
`search` turns "Brooklyn" or "5th Avenue" into places; `autocomplete` turns a
half-typed word into things somebody might have meant; `details` turns one of
those back into a full place. `search` and `details` both return the same
:class:`Place`, so a caller ranking things near an explorer and a caller ranking
things near a searched-for street are the same code path with a different point.

**Google Places API (New) is the primary provider.** It is what a person expects
a location box to feel like: type three letters, get the neighbourhood you meant,
ranked by a corpus nobody else has. Autocomplete then Place Details is the pair
that makes that work, and the pair is also how it is billed - see
:class:`GooglePlaces` for the field masks and session tokens that keep the bill
proportional to what is actually shown.

Google's terms shape two things here. Place data may be cached for up to thirty
days; the TTL below is an hour, so that ceiling is never approached. The one
identifier that may be kept indefinitely is `place_id`, which is why it is the
only part of a Google result the platform stores (on `catalog.venues`) and why
:class:`Place` carries it. `attributions` travels with a place because some
results carry third-party data Google requires be credited wherever it is shown.

**Nominatim stays as the keyless fallback**, so development, self-hosting and a
deployment with no billing account all still work. OpenStreetMap's operators ask
for an identifying User-Agent, at most one request a second, and that results be
cached; all three are honoured below - the rate limit by a lock, the caching by a
TTL map keyed on coordinates rounded to about a hundred metres, which is finer
than any answer this returns. Without the cache this would be unusable on a read
path, and a read path is exactly where it sits.

Provider selection is a setting, and the :class:`PlaceProvider` protocol is the
whole contract, so a supplemental source - GeoNames for administrative
hierarchies, Overpass for boundaries - is a new class here and a config value,
not a change to any caller.

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
    # What the place is called, when it is the kind of thing that has a name
    # rather than only an address - a venue, a park, a landmark. Not an address
    # component in either provider's vocabulary, which is why it is its own
    # field: without it "Blue Bottle Coffee" resolves to "Webster Street" and the
    # explorer never sees the words they typed.
    name: str = ""
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
    # The provider's stable identifier for this place - Google's `place_id`, or
    # `osm:N123` for Nominatim. The only part of a Google result the platform is
    # permitted to store indefinitely, and therefore the only part it does.
    place_id: str | None = None
    # Credits the provider requires be shown wherever this place is displayed.
    # Google returns these for results carrying third-party data; dropping them
    # is a licence breach rather than a cosmetic omission.
    attributions: tuple[str, ...] = ()
    provider: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def label(self) -> str:
        """The shortest thing a person would say to mean this place."""
        parts = [
            # A named place is called by its name. Falling through to the street
            # would answer "Blue Bottle Coffee" with "Webster Street", which is
            # true and is not what was asked.
            self.name or self.road or self.neighbourhood or self.district,
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
        return _KIND_RADIUS_KM.get((self.kind or "").lower(), 5.0)


# How wide to look around a place of each kind, when there is no bounding box to
# measure instead. Two vocabularies share the table because the two providers
# name things differently and a caller should never have to know which answered:
# OpenStreetMap says "suburb" and "city", Google says "sublocality" and
# "locality". Keeping them in one map is what makes `kind` comparable across
# providers rather than a string only its author can read.
_KIND_RADIUS_KM = {
    # OpenStreetMap
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
    # Google Places
    "premise": 0.5,
    "subpremise": 0.5,
    "street_address": 1.0,
    "route": 1.5,
    "point_of_interest": 1.0,
    "establishment": 1.0,
    "neighborhood": 2.0,
    "sublocality": 4.0,
    "sublocality_level_1": 4.0,
    "postal_code": 4.0,
    "locality": 20.0,
    "postal_town": 15.0,
    "administrative_area_level_3": 20.0,
    "administrative_area_level_2": 40.0,
    "administrative_area_level_1": 60.0,
    "country": 60.0,
}


@dataclass(slots=True, frozen=True)
class Suggestion:
    """One row of a location box, while somebody is still typing.

    Deliberately not a :class:`Place`. A suggestion has no coordinates, because
    the provider has not been asked for any yet - that is the entire point of the
    autocomplete/details split, and it is what keeps the cost of a search
    proportional to the one place the explorer picks rather than to the number of
    keystrokes it took them to find it.

    Split into `primary` and `secondary` rather than one string because a
    dropdown that reads "Brooklyn" in bold above "NY, USA" in grey is scannable
    at a glance, and "Brooklyn, NY, USA" repeated eight times is not.
    """

    place_id: str
    # The whole thing, for a client that would rather render one line.
    text: str
    primary: str = ""
    secondary: str = ""
    kinds: tuple[str, ...] = ()
    # How far the suggestion is from the coordinates the search was biased
    # towards, when there were any. Google computes it; showing it is what makes
    # two identically-named streets distinguishable.
    distance_metres: int | None = None
    provider: str = ""


class PlaceProvider(Protocol):
    name: str

    async def describe(self, latitude: float, longitude: float) -> Place | None: ...

    async def search(
        self, query: str, *, near: tuple[float, float] | None = None, limit: int = 5
    ) -> list[Place]: ...

    async def autocomplete(
        self,
        query: str,
        *,
        near: tuple[float, float] | None = None,
        limit: int = 5,
        session_token: str | None = None,
        cities_only: bool = False,
    ) -> list[Suggestion]:
        """Things somebody might have meant, from a partial query.

        `session_token` groups the keystrokes of one search with the `details`
        call that ends it. Google bills the group as a single lookup instead of
        one per keystroke, so passing it through is worth roughly an order of
        magnitude on the bill. Providers that do not bill this way ignore it.

        `cities_only` narrows the predictions to inhabited places. Off by
        default and deliberately so - a general location box that cannot find a
        street or a landmark is a box with a category missing. It exists for the
        one question that is genuinely about cities and nothing else: which city
        a post is filed under, where "Bole Road" is not an answer.
        """
        ...

    async def details(
        self, place_id: str, *, session_token: str | None = None
    ) -> Place | None:
        """Everything about one suggestion, once it has been chosen.

        The token must be the same one used for the autocomplete calls that led
        here, and must not be reused afterwards.
        """
        ...


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
        # Nominatim reports a name for areas as well as landmarks, where it
        # repeats the locality - harmless, because `label` drops duplicates.
        name=str(body.get("name") or ""),
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
        place_id=_nominatim_place_id(body),
        provider="nominatim",
        raw=body,
    )


def _nominatim_place_id(body: dict[str, Any]) -> str | None:
    """A stable identifier in the same shape Google's `place_id` has.

    Nominatim's own `place_id` is explicitly documented as unstable across
    imports, so it is useless for anything kept longer than a request. The
    OSM type and id are stable, and are also what `/lookup` takes, so the same
    string that identifies a place can be used to fetch it again. Prefixed with
    the provider because these ids share a column with Google's, and one that
    could be mistaken for the other would be resolved against the wrong service.
    """
    osm_type = str(body.get("osm_type") or "")
    osm_id = body.get("osm_id")
    if not osm_type or osm_id is None:
        return None
    return f"osm:{osm_type[:1].upper()}{osm_id}"


# What OpenStreetMap calls the kinds of place a post can be filed under. Which
# of these a given settlement is called varies by country and by how the local
# mappers tagged it, so all of them count - a "town" in one country is a "city"
# in another with the same population, and picking only "city" would mean whole
# countries where the box finds nothing.
_SETTLEMENT_KINDS = frozenset({"city", "town", "village", "municipality", "hamlet"})


def _is_settlement(place: Place) -> bool:
    """Whether this is somewhere people live, rather than an area containing it.

    Read from the kind where OpenStreetMap gave one, and from the address
    otherwise: a result tagged only `administrative` is still a city if its own
    address names it as the locality.
    """
    kind = (place.kind or "").lower()
    if kind in _SETTLEMENT_KINDS:
        return True
    # Plenty of places carry only the generic `administrative` boundary type,
    # which a county has too. What separates them is whether the thing is its
    # own locality: Nairobi is tagged administrative and its address says the
    # locality is Nairobi, while Nairobi County's address does not.
    locality = (place.locality or "").strip().lower()
    return bool(locality) and locality == (place.name or "").strip().lower()


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
        return await self._search(query, near=near, limit=limit)

    async def _search(
        self,
        query: str,
        *,
        near: tuple[float, float] | None = None,
        limit: int = 5,
        feature_type: str | None = None,
    ) -> list[Place]:
        query = query.strip()
        if not query:
            return []

        key = f"s:{query.lower()}:{near}:{limit}:{feature_type}"
        cached = _cache.get(key)
        if cached is not None:
            return list(cached)

        params: dict[str, Any] = {
            "q": query,
            "format": "jsonv2",
            "addressdetails": 1,
            "limit": max(1, min(limit, 10)),
        }
        if feature_type is not None:
            # Nominatim's own narrowing, which is cheaper and better than
            # discarding rows after the fact - it changes what is ranked, not
            # just what survives. Documented as approximate, though, so the
            # caller filters what comes back as well.
            params["featureType"] = feature_type
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

    async def autocomplete(
        self,
        query: str,
        *,
        near: tuple[float, float] | None = None,
        limit: int = 5,
        session_token: str | None = None,
        cities_only: bool = False,
    ) -> list[Suggestion]:
        """Search results, presented as suggestions.

        Nominatim has no prediction endpoint - it matches whole names, so a
        half-typed word usually returns nothing until it becomes a word. This is
        therefore a worse autocomplete than Google's rather than an equivalent
        one, which is the honest trade for being keyless. The interface is the
        same so the fallback needs no separate client code, and the one request
        a second the operators allow is why the client debounces.

        `session_token` is accepted and ignored: nothing here is billed, so
        there is no session to group.
        """
        found = await self._search(
            query,
            near=near,
            limit=limit,
            feature_type="settlement" if cities_only else None,
        )
        if cities_only:
            # `featureType` is documented as a hint rather than a filter, and a
            # county or a region does come back through it. Dropping those here
            # matters more than it looks: the box asks which city, and a row
            # that answers "Nairobi County" files every post in it under a name
            # no explorer will ever search for.
            found = [place for place in found if _is_settlement(place)]
        return [
            Suggestion(
                place_id=place.place_id or "",
                text=place.display_name or place.label,
                primary=place.label.split(", ")[0],
                # Everything after the leading name, which is the part that
                # tells two identically-named streets apart.
                secondary=", ".join(place.label.split(", ")[1:]),
                kinds=(place.kind,) if place.kind else (),
                provider=self.name,
            )
            for place in found
            if place.place_id
        ]

    async def details(
        self, place_id: str, *, session_token: str | None = None
    ) -> Place | None:
        """Resolve an `osm:` identifier back to a place.

        Refuses anything else rather than guessing: an id issued by Google
        arriving here means the provider changed under a client mid-search, and
        looking it up anyway would return whatever OSM object happened to share
        the number.
        """
        if not place_id.startswith("osm:"):
            logger.warning("nominatim_details_foreign_id", place_id=place_id[:24])
            return None

        key = f"d:{place_id}"
        cached = _cache.get(key)
        if cached is not None:
            return cached or None

        body = await self._call(
            "/lookup",
            {"osm_ids": place_id[4:], "format": "jsonv2", "addressdetails": 1},
        )
        if body is None:
            # Failed rather than answered. Caching it would turn one network
            # blip into an hour of this place not existing.
            return None

        place = None
        if isinstance(body, list) and body and isinstance(body[0], dict):
            place = _place_from_nominatim(body[0])
        _cache.put(key, place or False)
        return place

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


PLACES_BASE = "https://places.googleapis.com/v1"

# Reverse geocoding is not part of the Places API. Text Search, Autocomplete and
# Place Details all answer "what is called X"; none of them answers "what is at
# this latitude and longitude", which is the Geocoding API's job and is billed as
# its own product. So `describe` below talks to a different host from everything
# else in this class - not an oversight, and not worth hiding behind a wrapper
# that would make the two look like one service with one price.
GEOCODING_URL = "https://maps.googleapis.com/maps/api/geocode/json"

# Radius of the circle a search is biased towards, in metres. 50 km is the
# maximum Google accepts for a bias, and a bias is not a filter: somewhere
# further away still comes back if nothing nearer matches, which is what makes
# "Brooklyn" typed in Addis Ababa still find New York.
BIAS_RADIUS_M = 50_000.0

# Which fields to ask Places (New) for. This is the whole cost control: the API
# refuses a request without a field mask, and bills each one by the most
# expensive field it names. Everything below is Essentials tier - the cheapest
# that carries coordinates - except `displayName`, which is Pro.
#
# `displayName` is kept anyway, and it is the one deliberate expense here. For a
# country or a city the address components already spell the name out, but for a
# venue they do not: without it a coffee shop resolves to "300 Webster Street"
# and the explorer never sees the name they searched for. Everything else Pro and
# Enterprise offer - opening hours, ratings, photos, phone numbers - is absent,
# because Mado holds its own opinion of a place and has no use for Google's.
_PLACE_FIELDS = (
    "id",
    "location",
    "formattedAddress",
    "displayName",
    "types",
    "viewport",
    "addressComponents",
    "attributions",
)
# Details returns one place at the top level; Search wraps them in `places`.
DETAILS_FIELD_MASK = ",".join(_PLACE_FIELDS)
SEARCH_FIELD_MASK = ",".join(f"places.{name}" for name in _PLACE_FIELDS)

# Google's component types, mapped onto Place's fields. Ordered by preference
# within each field, and shared by Places (New) and the Geocoding API, which
# report the same types under different key spellings.
_GOOGLE_COMPONENTS = {
    "country": ("country",),
    "region": ("administrative_area_level_1",),
    "county": ("administrative_area_level_2",),
    "locality": ("locality", "postal_town"),
    "district": ("sublocality_level_1", "administrative_area_level_3"),
    "neighbourhood": ("neighborhood", "sublocality"),
    "road": ("route",),
    "postcode": ("postal_code",),
}


def _component_text(component: dict[str, Any], short: bool = False) -> str:
    """Read a component's name under either spelling.

    Places (New) says `longText`/`shortText`; the Geocoding API says
    `long_name`/`short_name`. Both feed the same mapping below, and handling the
    difference here means the mapping itself does not have to know which service
    answered.
    """
    keys = ("shortText", "short_name") if short else ("longText", "long_name")
    for key in keys:
        value = component.get(key)
        if value:
            return str(value)
    return ""


def _google_components(components: list[Any]) -> tuple[dict[str, str], str | None]:
    """Unpack an address component list into named fields and a country code."""
    found: dict[str, str] = {}
    country_code: str | None = None
    for component in components:
        if not isinstance(component, dict):
            continue
        types = set(component.get("types") or [])
        if "country" in types:
            country_code = _component_text(component, short=True).upper() or None
        for field_name, wanted in _GOOGLE_COMPONENTS.items():
            if field_name in found:
                continue
            if types & set(wanted):
                text = _component_text(component)
                if text:
                    found[field_name] = text
    return found, country_code


def _place_from_google(body: dict[str, Any]) -> Place | None:
    """Read one Places API (New) place resource."""
    location = body.get("location") or {}
    try:
        latitude = float(location["latitude"])
        longitude = float(location["longitude"])
    except (KeyError, TypeError, ValueError):
        return None

    found, country_code = _google_components(body.get("addressComponents") or [])

    # Places (New) names the viewport corners `low` and `high` rather than
    # southwest and northeast. They are the same two corners; a viewport read as
    # (south, west, north, east) in the wrong order puts the search box in the
    # sea, which is why this is unpacked by name and not by position.
    bounding: tuple[float, float, float, float] | None = None
    viewport = body.get("viewport") or {}
    low, high = viewport.get("low") or {}, viewport.get("high") or {}
    try:
        bounding = (
            float(low["latitude"]),
            float(low["longitude"]),
            float(high["latitude"]),
            float(high["longitude"]),
        )
    except (KeyError, TypeError, ValueError):
        bounding = None

    types = [str(value) for value in (body.get("types") or [])]
    display_name = (body.get("displayName") or {}).get("text") or ""
    formatted = str(body.get("formattedAddress") or "")

    return Place(
        latitude=latitude,
        longitude=longitude,
        # The address, not the name: `display_name` is what `label` falls back to
        # when the components are empty, and "Brooklyn" alone would not tell
        # anybody which Brooklyn.
        display_name=formatted or display_name,
        # Only for things that have a name of their own. Google returns a
        # displayName for an area too, where it merely repeats the locality
        # component and would make `label` read "Brooklyn, Brooklyn".
        name=display_name if _is_establishment(types) else "",
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
        place_id=str(body.get("id") or "") or None,
        attributions=_attributions(body.get("attributions")),
        provider="google",
        raw=body,
    )


# Types that mean "a thing at an address" rather than "an area". Google tags
# these alongside the specific type, and the specific type is not a fixed list -
# there are hundreds and they add more - so this asks what a place *is like*
# rather than trying to enumerate them.
_ESTABLISHMENT_TYPES = frozenset({"establishment", "point_of_interest", "premise"})


def _is_establishment(types: list[str]) -> bool:
    return bool(_ESTABLISHMENT_TYPES.intersection(types))


def _attributions(value: Any) -> tuple[str, ...]:
    """Provider credits, flattened to the strings that get displayed."""
    if not isinstance(value, list):
        return ()
    credits = [
        str(item.get("provider") or "").strip()
        for item in value
        if isinstance(item, dict) and item.get("provider")
    ]
    return tuple(dict.fromkeys(credit for credit in credits if credit))


class GooglePlaces:
    """Google Places API (New). The primary provider.

    Four operations, three of them on `places.googleapis.com`:

    * `autocomplete` - `places:autocomplete`, what the location box calls while
      somebody types.
    * `details` - `places/{id}`, what it calls once they choose.
    * `search` - `places:searchText`, for a caller that has a whole query and no
      interface to put suggestions in. The concierge is the reason this exists.
    * `describe` - the Geocoding API, because Places does not answer it.

    **Session tokens are the reason the first two are separate.** Autocomplete
    is billed per request; a nine-letter place typed at speed is nine of them.
    Passing the same token through every keystroke and then through the details
    call collapses the lot into one billed lookup. A token used twice, or reused
    after its details call, silently loses the discount rather than failing - so
    the client mints one per search and drops it after, and nothing here reuses
    one.
    """

    name = "google"

    def __init__(self, api_key: str, *, language: str = "en") -> None:
        self._key = api_key
        self._language = language

    # --------------------------------------------------------- autocomplete

    async def autocomplete(
        self,
        query: str,
        *,
        near: tuple[float, float] | None = None,
        limit: int = 5,
        session_token: str | None = None,
        cities_only: bool = False,
    ) -> list[Suggestion]:
        query = query.strip()
        if not query:
            return []

        # Deliberately uncached, unlike everything else here. Predictions are
        # per-session by construction: caching them across sessions would hand a
        # second explorer results billed to the first one's token, and would
        # break the session grouping that makes the pair cheap in the first
        # place. Debouncing on the client is what keeps the volume down.
        body = await self._post(
            "places:autocomplete",
            {
                "input": query,
                # No `includedPrimaryTypes` by default: the box is meant to find
                # countries, cities, neighbourhoods, streets, landmarks and
                # venues alike, and every restriction here is a category
                # somebody cannot find.
                #
                # `cities_only` is the one caller that wants the opposite - the
                # box asking which city a post is filed under, where a street is
                # not an answer. It asks for `(cities)`, Google's own
                # collection, rather than a list of types: what a city is called
                # differs by country - a locality in most of the world, an
                # administrative_area_level_3 in others - and a hand-written
                # list is wrong in whichever country nobody tested. Somewhere
                # Google does not class as a city is still reachable through the
                # unrestricted search under the map, which is the field that
                # actually decides where the post is.
                **({"includedPrimaryTypes": ["(cities)"]} if cities_only else {}),
                "languageCode": self._language,
                **self._bias(near),
                **({"sessionToken": session_token} if session_token else {}),
            },
            # Autocomplete is billed per request rather than per field, and the
            # API rejects a field mask on it.
            field_mask=None,
        )

        suggestions: list[Suggestion] = []
        for item in (body or {}).get("suggestions") or []:
            if not isinstance(item, dict):
                continue
            # The other kind is `queryPrediction`, which is a search phrase with
            # no place behind it - there is nothing to resolve, so a row for it
            # would be a suggestion that does nothing when tapped.
            prediction = item.get("placePrediction")
            if not isinstance(prediction, dict):
                continue
            place_id = str(prediction.get("placeId") or "")
            if not place_id:
                continue
            structured = prediction.get("structuredFormat") or {}
            distance = prediction.get("distanceMeters")
            suggestions.append(
                Suggestion(
                    place_id=place_id,
                    text=str((prediction.get("text") or {}).get("text") or ""),
                    primary=str((structured.get("mainText") or {}).get("text") or ""),
                    secondary=str((structured.get("secondaryText") or {}).get("text") or ""),
                    kinds=tuple(str(kind) for kind in (prediction.get("types") or [])),
                    distance_metres=int(distance) if isinstance(distance, (int, float)) else None,
                    provider=self.name,
                )
            )
            if len(suggestions) >= limit:
                break
        return suggestions

    # -------------------------------------------------------------- details

    async def details(
        self, place_id: str, *, session_token: str | None = None
    ) -> Place | None:
        place_id = place_id.strip()
        if not place_id:
            return None
        if place_id.startswith("osm:"):
            # An id minted by the fallback provider. Google would return 404 for
            # it, but saying so here makes a provider switch mid-search legible
            # in the logs instead of looking like an outage.
            logger.warning("google_details_foreign_id", place_id=place_id[:24])
            return None

        key = f"gd:{place_id}"
        cached = _cache.get(key)
        if cached is not None:
            return cached or None

        body = await self._get(
            f"places/{place_id}",
            # The session token belongs on the details call too: it is what
            # closes the session and makes the preceding keystrokes free.
            {
                "languageCode": self._language,
                **({"sessionToken": session_token} if session_token else {}),
            },
            field_mask=DETAILS_FIELD_MASK,
        )
        if body is None:
            # A transport failure, not an answer. Caching it would make one
            # network blip mean this place does not exist for the next hour -
            # and the explorer who picked it sees a suggestion that resolves to
            # nothing, retries, and gets the same nothing.
            return None

        place = _place_from_google(body)
        # Cached for the TTL at the top of this module - an hour, against the
        # thirty days Google's terms permit. A place's coordinates do not move,
        # and a shorter window is the safer side of a policy to be wrong on.
        _cache.put(key, place or False)
        return place

    # --------------------------------------------------------------- search

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

        body = await self._post(
            "places:searchText",
            {
                "textQuery": query,
                "languageCode": self._language,
                "pageSize": max(1, min(limit, 20)),
                **self._bias(near),
            },
            field_mask=SEARCH_FIELD_MASK,
        )
        if body is None:
            # Failed, rather than found nothing. An empty list cached for an
            # hour is a query that stays broken long after the outage ended.
            return []

        places = [
            place
            for place in (
                _place_from_google(item)
                for item in body.get("places") or []
                if isinstance(item, dict)
            )
            if place is not None
        ][:limit]
        _cache.put(cache_key, places)
        return places

    # ------------------------------------------------------------- describe

    async def describe(self, latitude: float, longitude: float) -> Place | None:
        key = _coordinate_key(latitude, longitude)
        cached = _cache.get(key)
        if cached is not None:
            return cached or None

        body = await self._geocode({"latlng": f"{latitude},{longitude}"})
        if body is None:
            # A transport failure. Not cached: the next request should try again
            # rather than be told for an hour that the world has no name.
            return None

        # The Geocoding API answers HTTP 200 and puts the real outcome in
        # `status`, so `raise_for_status` sees nothing wrong with a rejected key.
        # Left unchecked this fails completely silently - every explorer told
        # "we could not work out where that is", no error anywhere, and a
        # negative cache entry making it look intermittent.
        status = str(body.get("status") or "")
        if status not in {"OK", "ZERO_RESULTS"}:
            logger.warning(
                "google_geocode_refused",
                status=status,
                # Google puts the reason here, and it is a configuration problem
                # every time: a key without the Geocoding API enabled, a referrer
                # restriction on a server-side key, or billing not set up.
                detail=redact(str(body.get("error_message") or "")),
            )
            return None

        results = body.get("results") or []
        place = self._from_geocoding(results[0]) if results else None
        # Genuine "nowhere" is cached, so a boat in the Atlantic does not re-ask
        # on every page view. Only reached when Google actually answered.
        _cache.put(key, place or False)
        return place

    def _from_geocoding(self, result: dict[str, Any]) -> Place | None:
        """Read a Geocoding API result.

        Kept separate from `_place_from_google` rather than normalised into it:
        the two services genuinely differ in shape - `geometry.location.lat`
        against `location.latitude`, `southwest`/`northeast` against
        `low`/`high` - and a single reader taking either would be a pile of
        fallbacks in which a field silently missing from one looks the same as a
        field the service did not return.
        """
        location = ((result.get("geometry") or {}).get("location")) or {}
        try:
            latitude = float(location["lat"])
            longitude = float(location["lng"])
        except (KeyError, TypeError, ValueError):
            return None

        found, country_code = _google_components(result.get("address_components") or [])

        bounding: tuple[float, float, float, float] | None = None
        viewport = (result.get("geometry") or {}).get("viewport") or {}
        southwest, northeast = viewport.get("southwest") or {}, viewport.get("northeast") or {}
        try:
            bounding = (
                float(southwest["lat"]),
                float(southwest["lng"]),
                float(northeast["lat"]),
                float(northeast["lng"]),
            )
        except (KeyError, TypeError, ValueError):
            bounding = None

        types = [str(value) for value in (result.get("types") or [])]
        return Place(
            latitude=latitude,
            longitude=longitude,
            display_name=str(result.get("formatted_address") or ""),
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
            # Geocoding returns a place_id for the matched feature, and it is the
            # same identifier space Places uses - so a point resolved from
            # coordinates can be handed straight to `details`.
            place_id=str(result.get("place_id") or "") or None,
            provider="google",
            raw=result,
        )

    # ------------------------------------------------------------ transport

    def _bias(self, near: tuple[float, float] | None) -> dict[str, Any]:
        """Prefer results near the explorer without excluding anywhere else.

        A bias, never a restriction. `locationRestriction` would make somebody
        in Manhattan unable to find Nairobi, which is a search box that only
        works for people who already know where they are.
        """
        if near is None:
            return {}
        latitude, longitude = near
        return {
            "locationBias": {
                "circle": {
                    "center": {"latitude": latitude, "longitude": longitude},
                    "radius": BIAS_RADIUS_M,
                }
            }
        }

    def _headers(self, field_mask: str | None, authenticated_by_header: bool) -> dict[str, str]:
        headers = {"X-Goog-Api-Key": self._key} if authenticated_by_header else {}
        if field_mask:
            headers["X-Goog-FieldMask"] = field_mask
        return headers

    async def _post(
        self, path: str, payload: dict[str, Any], *, field_mask: str | None
    ) -> dict[str, Any] | None:
        return await self._call(
            "POST", f"{PLACES_BASE}/{path}", json=payload, field_mask=field_mask
        )

    async def _get(
        self, path: str, params: dict[str, Any], *, field_mask: str | None
    ) -> dict[str, Any] | None:
        return await self._call(
            "GET", f"{PLACES_BASE}/{path}", params=params, field_mask=field_mask
        )

    async def _geocode(self, params: dict[str, Any]) -> dict[str, Any] | None:
        """The one call where the key travels in the query string.

        Everywhere else in this project a key goes in a header, and for good
        reason - this codebase leaked one in a URL once. The Geocoding API is
        the exception because it accepts no other form: sent as
        `X-Goog-Api-Key` it is not read at all, and the service answers "You
        must use an API key" as though none had been sent. Verified against the
        live API rather than assumed, because the failure is silent - HTTP 200,
        empty results, and reverse geocoding that simply never works.

        What makes it safe is that `redact` in `app/core/logging.py` scrubs
        `key=` out of any string reaching a log sink, which is where a URL ends
        up when httpx renders one into a transport error.
        """
        return await self._call(
            "GET",
            GEOCODING_URL,
            params={**params, "key": self._key},
            field_mask=None,
            authenticated_by_header=False,
        )

    async def _call(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        field_mask: str | None,
        authenticated_by_header: bool = True,
    ) -> dict[str, Any] | None:
        started = time.perf_counter()
        outcome = "error"
        try:
            with tracing.span("places"):
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    # The key travels in a header for everything on
                    # `places.googleapis.com`, and only in the query string for
                    # the Geocoding API, which reads it nowhere else. httpx
                    # renders the full URL into transport errors, so a key in a
                    # query string reaches the logs the first time a request
                    # fails - which is exactly how this project leaked one
                    # before. `redact` below is what makes the exception safe.
                    response = await client.request(
                        method,
                        url,
                        params=params,
                        json=json,
                        headers=self._headers(field_mask, authenticated_by_header),
                    )
                response.raise_for_status()
                body = response.json()
            outcome = "ok"
            return body if isinstance(body, dict) else None
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "google_places_failed",
                # The path identifies which of the four operations failed;
                # the query string is dropped because it can carry a session
                # token, and the host is constant.
                operation=url.rsplit("/", 1)[-1].split("?")[0],
                error=redact(str(exc)),
            )
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

    async def autocomplete(
        self,
        query: str,
        *,
        near: tuple[float, float] | None = None,
        limit: int = 5,
        session_token: str | None = None,
        cities_only: bool = False,
    ) -> list[Suggestion]:
        return []

    async def details(
        self, place_id: str, *, session_token: str | None = None
    ) -> Place | None:
        return None


@lru_cache(maxsize=1)
def get_provider() -> PlaceProvider:
    """The configured provider.

    Google Places is primary and is chosen whenever a key exists, because it is
    the only one of these that makes a location box feel like one. OpenStreetMap
    is the fallback rather than the default: it keeps a deployment with no
    billing account working, at a noticeably worse standard of suggestion.

    The stub is returned only when asked for explicitly. An unconfigured
    deployment should still be able to tell somebody where they are, because
    that is the first thing the app does.
    """
    settings = get_settings()
    choice = settings.places_provider
    if choice == "stub":
        return StubPlaces()
    if choice in {"auto", "google"} and settings.google_maps_api_key:
        return GooglePlaces(
            settings.google_maps_api_key, language=settings.places_language
        )
    if choice == "google":
        logger.warning("places_provider_unconfigured", requested="google")
    return NominatimPlaces(settings.nominatim_user_agent, settings.nominatim_base_url)


def reset_provider() -> None:
    get_provider.cache_clear()
    _cache._entries.clear()
