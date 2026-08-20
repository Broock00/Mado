"""Which time zone a point on the earth is in.

This exists because of one sentence in the concierge: "what is on tonight".
Answering it means knowing when tonight is *there*, which means the city row
carries an IANA zone, which means a city materialised from wherever somebody
dropped a pin has to get one from somewhere. Before this, cities were typed in by
a developer and the zone was typed in with them - which worked for ten cities and
is exactly the constraint being removed.

**Google's Time Zone API is primary.** It is the only one of these that answers
from coordinates, which is the only way to be right about a country with more
than one zone - and those are not rare countries: the United States has 29 zones,
Brazil 16, Russia 24. A country-level answer is wrong for a large fraction of the
world's population.

**The keyless fallback answers only when the country has exactly one zone.** That
is most countries, and where it is true the answer is exactly as good as Google's.
Where it is not, this returns None rather than picking one - guessing
`America/New_York` for a venue in Denver would put every event three hours out and
look completely normal on the screen.

Two traps in the Google API, both verified against the live service rather than
assumed, and both shared with the Geocoding API (see `places.py`):

* It does **not** read `X-Goog-Api-Key`. The key has to go in the query string;
  sent as a header, the service replies as though no key were sent at all.
* It reports refusals with **HTTP 200** and a `status` field, so
  `raise_for_status` sees nothing wrong. It also spells the message
  `errorMessage`, where the Geocoding API says `error_message`.
"""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Any, Protocol

import httpx

from app.core import metrics, tracing
from app.core.config import get_settings
from app.core.logging import get_logger, redact

logger = get_logger("mado.timezones")

REQUEST_TIMEOUT = 8.0
TIMEZONE_URL = "https://maps.googleapis.com/maps/api/timezone/json"

# What a caller gets when nothing can answer. UTC is not a guess dressed up as an
# answer - it is the absence of one, and it is what the platform already assumed
# before any of this existed.
FALLBACK_ZONE = "UTC"


class TimeZoneProvider(Protocol):
    name: str

    async def zone_for(
        self, latitude: float, longitude: float, *, country_code: str | None = None
    ) -> str | None:
        """The IANA zone at this point, or None if it cannot be established.

        `country_code` is a hint, not the question: a provider that can answer
        from coordinates should, and one that cannot may fall back to it.
        """
        ...


@lru_cache(maxsize=1)
def _zones_by_territory() -> dict[str, tuple[str, ...]]:
    """CLDR's zone list per country, inverted from its zone→territory map.

    Loaded once and cached: it is a few hundred entries of static reference data
    and rebuilding it per call would be the most expensive part of creating a
    city.
    """
    from babel.core import get_global

    grouped: dict[str, list[str]] = {}
    for zone, territory in get_global("zone_territories").items():
        grouped.setdefault(territory, []).append(zone)
    return {territory: tuple(sorted(zones)) for territory, zones in grouped.items()}


class TerritoryTimeZones:
    """CLDR, keyless, and honest about what it cannot answer.

    Answers only when a country has exactly one zone. For anywhere else it
    returns None, because the alternative - picking the first or the most
    populous - produces a confident wrong answer that shows up as every event in
    Denver being listed three hours out, with nothing on screen to suggest why.
    """

    name = "territory"

    async def zone_for(
        self, latitude: float, longitude: float, *, country_code: str | None = None
    ) -> str | None:
        if not country_code:
            return None
        zones = _zones_by_territory().get(country_code.upper(), ())
        if len(zones) == 1:
            return zones[0]
        if zones:
            logger.info(
                "timezone_ambiguous_without_coordinates",
                country=country_code.upper(),
                zones=len(zones),
            )
        return None


class GoogleTimeZones:
    """Google's Time Zone API. Answers from coordinates, so it is always exact.

    Called once per city that has never been seen before - which over the life of
    a deployment is a very small number of requests, and is why this is worth a
    lookup rather than an approximation.
    """

    name = "google"

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def zone_for(
        self, latitude: float, longitude: float, *, country_code: str | None = None
    ) -> str | None:
        started = time.perf_counter()
        outcome = "error"
        try:
            with tracing.span("timezone"):
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    response = await client.get(
                        TIMEZONE_URL,
                        params={
                            "location": f"{latitude},{longitude}",
                            # The zone id does not depend on the instant, but the
                            # API requires one to decide the offset it also
                            # returns. Now is the honest choice.
                            "timestamp": int(time.time()),
                            # In the query string because this service reads the
                            # key nowhere else. `redact` scrubs `key=` from
                            # anything reaching a log sink.
                            "key": self._key,
                        },
                    )
                response.raise_for_status()
                body: dict[str, Any] = response.json()
            outcome = "ok"
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("google_timezone_failed", error=redact(str(exc)))
            return None
        finally:
            metrics.dependency_calls.inc("timezone", outcome)
            metrics.dependency_duration.observe(time.perf_counter() - started, "timezone")

        status = str(body.get("status") or "")
        if status == "ZERO_RESULTS":
            # A real answer: the point is in the ocean or otherwise unzoned.
            return None
        if status != "OK":
            logger.warning(
                "google_timezone_refused",
                status=status,
                # Note the spelling: this API says `errorMessage` where the
                # Geocoding API says `error_message`.
                detail=redact(str(body.get("errorMessage") or "")),
            )
            return None

        return str(body.get("timeZoneId") or "") or None


class StubTimeZones:
    """Answers nothing, so a test never depends on a third party."""

    name = "stub"

    async def zone_for(
        self, latitude: float, longitude: float, *, country_code: str | None = None
    ) -> str | None:
        return None


class _WithTerritoryFallback:
    """Google first, CLDR second.

    Chained rather than either/or because the two fail differently: Google is
    exact and can be unreachable, CLDR is always available and cannot answer for
    a country with several zones. Between them almost everywhere resolves, and
    the combination degrades to "the country's only zone" rather than to nothing.
    """

    def __init__(self, primary: TimeZoneProvider) -> None:
        self._primary = primary
        self._fallback = TerritoryTimeZones()
        self.name = primary.name

    async def zone_for(
        self, latitude: float, longitude: float, *, country_code: str | None = None
    ) -> str | None:
        found = await self._primary.zone_for(latitude, longitude, country_code=country_code)
        if found:
            return found
        return await self._fallback.zone_for(latitude, longitude, country_code=country_code)


@lru_cache(maxsize=1)
def get_provider() -> TimeZoneProvider:
    settings = get_settings()
    choice = settings.timezone_provider
    if choice == "stub":
        return StubTimeZones()
    if choice in {"auto", "google"} and settings.google_maps_api_key:
        return _WithTerritoryFallback(GoogleTimeZones(settings.google_maps_api_key))
    if choice == "google":
        logger.warning("timezone_provider_unconfigured", requested="google")
    return TerritoryTimeZones()


def reset_provider() -> None:
    get_provider.cache_clear()
