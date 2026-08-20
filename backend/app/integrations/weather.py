"""Weather.

Weather is the one piece of context that changes what is a good idea without
changing anything about the catalogue. An open-air jazz night is the best answer
in the city until it rains, and then it is the worst one. Ranking has had a
`raining` flag since spec DISC-004, but nothing ever supplied it: the only writer
was a boolean query parameter the browser asserted about the current moment, so
the signal was dark on every path that mattered and completely absent from
planning, which is the surface that actually needs it. A plan is made in advance,
and "in advance" is precisely when a forecast exists and an observation does not.

**This is a forecast service, not a climate service, and the difference is the
whole design.** A provider answers about a coordinate on a date inside its
horizon - about sixteen days for Open-Meteo, ten for Google. Outside it there is
no forecast, and this module returns nothing rather than falling back to seasonal
averages. "Paris is usually 8 degrees in January" is a true sentence that is not
an answer to "will my son be cold on the 14th", and dressing a climate normal up
as a forecast is exactly the failure the stub rule in this codebase exists to
prevent: it is indistinguishable from a working integration until somebody packs
for it. :attr:`Forecast.horizon_days` lets a caller say "I cannot see that far
yet" in words.

Three providers behind one protocol, matching :mod:`app.integrations.routing`:

* :class:`OpenMeteoWeather` - keyless, no billing account, sixteen days of daily
  forecast. This is the workhorse and what makes development work.
* :class:`GoogleWeather` - the Google Weather API, where a key is configured, for
  consistency with the other Google surfaces this project already bills.
* :class:`NoWeather` - answers nothing, always. Not a mock returning a pleasant
  afternoon; a stub that invents plausible weather would silently plan an outdoor
  evening in a storm.

Both keyed providers put the key in the **query string**, like Geocoding and Time
Zone and unlike Places and Routes. `redact()` scrubs `key=` before anything
reaches a log sink, which is what makes that safe.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Protocol

import httpx

from app.core import tracing
from app.core.config import get_settings
from app.core.logging import get_logger, redact

logger = get_logger("mado.weather")

REQUEST_TIMEOUT = 8.0

# Conditions, as a controlled vocabulary. Every provider maps onto these rather
# than passing its own codes through, because the ranking and the planner branch
# on them and a vendor string reaching either would be a vendor type escaping its
# interface - the thing `app/integrations/` exists to stop.
CLEAR = "clear"
CLOUDY = "cloudy"
RAIN = "rain"
SNOW = "snow"
STORM = "storm"
FOG = "fog"
UNKNOWN = "unknown"

# Conditions that make being outdoors actively unpleasant rather than merely
# grey. Cloud is not one of them: half of northern Europe would be indoors all
# year.
WET_CONDITIONS = frozenset({RAIN, SNOW, STORM})

# Product thresholds, named because tuning them is a product decision and
# scattering them as literals through the scoring code is how they drift apart.
#
# Deliberately not personalised. "Cold" means something different to somebody
# from Nairobi and somebody from Oslo, and the honest way to carry that is the
# explorer telling us - which they do, as a stated constraint that travels
# separately. These are the fallback for when nobody has said.
COLD_C = 12.0
HOT_C = 32.0
# Probability above which rain is worth planning around. Below it, an umbrella is
# a better answer than rearranging somebody's day.
WET_PROBABILITY = 0.4

# How long a forecast stays usable. Providers refresh hourly at best, so a
# shorter TTL spends requests without gaining accuracy.
CACHE_TTL_SECONDS = 3600
CACHE_MAX_ENTRIES = 512

# Coordinates are rounded to this many decimals for the cache key. One decimal is
# roughly 11 km, which is far finer than daily weather varies - two points inside
# the same city get the same forecast, which is both correct and the difference
# between one request per trip and one per venue.
CACHE_PRECISION = 1


@dataclass(slots=True)
class DailyWeather:
    """What one day looks like at one coordinate.

    Every measurement is optional. A provider that answers about temperature but
    not precipitation must not be turned into one that claims it will be dry, so
    an absent field stays None all the way to the caller and the predicates below
    return False rather than guessing.
    """

    day: date
    condition: str = UNKNOWN
    temperature_min_c: float | None = None
    temperature_max_c: float | None = None
    # 0-1, not 0-100. Providers report percentages; converting at the boundary
    # means nothing downstream has to remember which scale it is holding.
    precipitation_probability: float | None = None
    provider: str = "none"

    @property
    def is_wet(self) -> bool:
        """Whether rain is likely enough to plan around."""
        if self.condition in WET_CONDITIONS:
            return True
        probability = self.precipitation_probability
        return probability is not None and probability >= WET_PROBABILITY

    @property
    def is_cold(self) -> bool:
        """Cold by the daytime high, not the overnight low.

        Everywhere is cold at 4am and nobody plans an outing then. Using the
        minimum marked half of temperate Europe unsuitable for anyone who said
        they feel the cold, which is not what they meant.
        """
        return self.temperature_max_c is not None and self.temperature_max_c < COLD_C

    @property
    def is_hot(self) -> bool:
        return self.temperature_max_c is not None and self.temperature_max_c > HOT_C

    @property
    def favours_indoors(self) -> bool:
        return self.is_wet or self.is_cold or self.is_hot

    def describe(self) -> str:
        """One short phrase, for a prompt or a card.

        Rendered here rather than by the model: the model may phrase a reply, and
        it may not decide what the weather is (spec 56.01 s3.1).
        """
        parts: list[str] = []
        if self.condition != UNKNOWN:
            parts.append({
                CLEAR: "clear",
                CLOUDY: "cloudy",
                RAIN: "rain",
                SNOW: "snow",
                STORM: "storms",
                FOG: "fog",
            }.get(self.condition, self.condition))
        if self.temperature_max_c is not None:
            if self.temperature_min_c is not None:
                parts.append(
                    f"{self.temperature_min_c:.0f} to {self.temperature_max_c:.0f}C"
                )
            else:
                parts.append(f"up to {self.temperature_max_c:.0f}C")
        if self.precipitation_probability is not None and self.precipitation_probability > 0.1:
            parts.append(f"{self.precipitation_probability * 100:.0f}% chance of rain")
        return ", ".join(parts) or "no forecast"


@dataclass(slots=True)
class Forecast:
    """A run of days at one coordinate, and an honest edge to it."""

    latitude: float
    longitude: float
    days: list[DailyWeather] = field(default_factory=list)
    provider: str = "none"

    @property
    def is_empty(self) -> bool:
        return not self.days

    @property
    def horizon(self) -> date | None:
        """The last day this forecast can speak about."""
        return max((day.day for day in self.days), default=None)

    @property
    def horizon_days(self) -> int:
        horizon = self.horizon
        if horizon is None:
            return 0
        return max(0, (horizon - datetime.now(UTC).date()).days)

    def on(self, day: date) -> DailyWeather | None:
        """The forecast for one day, or None when it is beyond the horizon.

        None is a real answer here and callers must render it as one. Returning
        the nearest available day instead would report next Tuesday's weather for
        a trip in March.
        """
        for entry in self.days:
            if entry.day == day:
                return entry
        return None

    def between(self, start: date, end: date) -> list[DailyWeather]:
        return [entry for entry in self.days if start <= entry.day <= end]


class WeatherProvider(Protocol):
    name: str

    async def forecast(
        self, latitude: float, longitude: float, *, days: int
    ) -> Forecast | None: ...


# --- WMO code mapping --------------------------------------------------------

# Open-Meteo reports WMO 4677 weather codes. Grouped rather than mapped one to
# one: the difference between "light drizzle" and "moderate drizzle" changes
# nothing about whether to suggest an outdoor table.
_WMO_CONDITIONS: dict[range, str] = {
    range(0, 1): CLEAR,
    range(1, 4): CLOUDY,
    range(45, 49): FOG,
    range(51, 68): RAIN,
    range(71, 78): SNOW,
    range(80, 83): RAIN,
    range(85, 87): SNOW,
    range(95, 100): STORM,
}


def _condition_from_wmo(code: int | None) -> str:
    if code is None:
        return UNKNOWN
    for codes, condition in _WMO_CONDITIONS.items():
        if code in codes:
            return condition
    return UNKNOWN


class OpenMeteoWeather:
    """Open-Meteo. Keyless, which is why this works on a development machine.

    Free for non-commercial use and rate limited by its operators; a commercial
    deployment wants either their paid tier or the Google provider below. Sixteen
    days of daily forecast, which covers "next week" comfortably and "in the
    spring" not at all.
    """

    name = "open-meteo"

    # Their documented ceiling. Asking for more returns an error rather than
    # silently truncating, so it is clamped here.
    MAX_DAYS = 16

    _URL = "https://api.open-meteo.com/v1/forecast"

    async def forecast(
        self, latitude: float, longitude: float, *, days: int
    ) -> Forecast | None:
        params = {
            "latitude": f"{latitude:.4f}",
            "longitude": f"{longitude:.4f}",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max",
            # UTC throughout. The caller knows the city's zone and converts; a
            # provider guessing at a zone from a coordinate is a second, quieter
            # copy of the logic in integrations/timezones.py that could disagree
            # with it.
            "timezone": "UTC",
            "forecast_days": str(max(1, min(days, self.MAX_DAYS))),
        }

        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.get(self._URL, params=params)
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("open_meteo_forecast_failed", error=redact(str(exc)))
            return None

        # Open-Meteo reports its own refusals in the body with HTTP 400, which
        # `raise_for_status` catches - but it also uses this shape for partial
        # problems, so it is checked explicitly.
        if body.get("error"):
            logger.warning("open_meteo_refused", detail=redact(str(body.get("reason") or "")))
            return None

        daily = body.get("daily") or {}
        dates = daily.get("time") or []
        if not dates:
            return None

        codes = daily.get("weather_code") or []
        highs = daily.get("temperature_2m_max") or []
        lows = daily.get("temperature_2m_min") or []
        wet = daily.get("precipitation_probability_max") or []

        entries: list[DailyWeather] = []
        for index, raw_day in enumerate(dates):
            parsed = _parse_day(raw_day)
            if parsed is None:
                continue
            entries.append(
                DailyWeather(
                    day=parsed,
                    condition=_condition_from_wmo(_at(codes, index, cast=int)),
                    temperature_max_c=_at(highs, index),
                    temperature_min_c=_at(lows, index),
                    precipitation_probability=_as_probability(_at(wet, index)),
                    provider=self.name,
                )
            )

        return Forecast(
            latitude=latitude, longitude=longitude, days=entries, provider=self.name
        )


class GoogleWeather:
    """Google Weather API, where a key is configured.

    Ten days of daily forecast, paginated. Only the first page is read: ten days
    arrive in one response at the default page size, and following pages to reach
    a horizon this provider does not have would spend requests for nothing.

    **The key goes in the query string.** Like Geocoding and Time Zone, and
    unlike Places and Routes, this service does not read `X-Goog-Api-Key`. It is
    also better behaved than those two about refusals - it answers with a real
    HTTP status and a JSON `error` object rather than a 200 - but both are
    checked, because a refusal reported as an empty forecast would look exactly
    like a coordinate in the middle of the ocean.
    """

    name = "google"

    MAX_DAYS = 10

    _URL = "https://weather.googleapis.com/v1/forecast/days:lookup"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def forecast(
        self, latitude: float, longitude: float, *, days: int
    ) -> Forecast | None:
        params = {
            "key": self._api_key,
            "location.latitude": f"{latitude:.4f}",
            "location.longitude": f"{longitude:.4f}",
            "days": str(max(1, min(days, self.MAX_DAYS))),
            "unitsSystem": "METRIC",
        }

        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.get(self._URL, params=params)
                body = response.json()
                response.raise_for_status()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("google_weather_failed", error=redact(str(exc)))
            return None

        if isinstance(body, dict) and body.get("error"):
            logger.warning(
                "google_weather_refused",
                detail=redact(str((body.get("error") or {}).get("message") or "")),
            )
            return None

        entries: list[DailyWeather] = []
        for record in (body.get("forecastDays") or []):
            parsed = _google_day(record)
            if parsed is not None:
                entries.append(parsed)

        if not entries:
            return None
        return Forecast(
            latitude=latitude, longitude=longitude, days=entries, provider=self.name
        )


# Google reports a condition type per half-day. Mapped onto the same vocabulary
# every other provider uses; anything unrecognised becomes UNKNOWN rather than a
# guess, so a new vendor code degrades to "we do not know" instead of "clear".
_GOOGLE_CONDITIONS: dict[str, str] = {
    "CLEAR": CLEAR,
    "MOSTLY_CLEAR": CLEAR,
    "PARTLY_CLOUDY": CLOUDY,
    "MOSTLY_CLOUDY": CLOUDY,
    "CLOUDY": CLOUDY,
    "WINDY": CLOUDY,
    "WIND_AND_RAIN": RAIN,
    "LIGHT_RAIN_SHOWERS": RAIN,
    "CHANCE_OF_SHOWERS": RAIN,
    "SCATTERED_SHOWERS": RAIN,
    "RAIN_SHOWERS": RAIN,
    "HEAVY_RAIN_SHOWERS": RAIN,
    "LIGHT_TO_MODERATE_RAIN": RAIN,
    "MODERATE_TO_HEAVY_RAIN": RAIN,
    "RAIN": RAIN,
    "LIGHT_RAIN": RAIN,
    "HEAVY_RAIN": RAIN,
    "RAIN_PERIODICALLY_HEAVY": RAIN,
    "LIGHT_SNOW_SHOWERS": SNOW,
    "CHANCE_OF_SNOW_SHOWERS": SNOW,
    "SCATTERED_SNOW_SHOWERS": SNOW,
    "SNOW_SHOWERS": SNOW,
    "HEAVY_SNOW_SHOWERS": SNOW,
    "LIGHT_TO_MODERATE_SNOW": SNOW,
    "MODERATE_TO_HEAVY_SNOW": SNOW,
    "SNOW": SNOW,
    "LIGHT_SNOW": SNOW,
    "HEAVY_SNOW": SNOW,
    "SNOWSTORM": STORM,
    "SNOW_PERIODICALLY_HEAVY": SNOW,
    "HEAVY_SNOW_STORM": STORM,
    "BLOWING_SNOW": SNOW,
    "RAIN_AND_SNOW": SNOW,
    "HAIL": STORM,
    "HAIL_SHOWERS": STORM,
    "THUNDERSTORM": STORM,
    "THUNDERSHOWER": STORM,
    "LIGHT_THUNDERSTORM_RAIN": STORM,
    "SCATTERED_THUNDERSTORMS": STORM,
    "HEAVY_THUNDERSTORM": STORM,
}


def _google_day(record: dict) -> DailyWeather | None:
    display = record.get("displayDate") or {}
    try:
        parsed = date(
            int(display["year"]), int(display["month"]), int(display["day"])
        )
    except (KeyError, TypeError, ValueError):
        return None

    daytime = record.get("daytimeForecast") or {}
    condition_code = ((daytime.get("weatherCondition") or {}).get("type") or "").upper()

    maximum = (record.get("maxTemperature") or {}).get("degrees")
    minimum = (record.get("minTemperature") or {}).get("degrees")

    # Rain probability is reported per half-day. The daytime figure is the one
    # that matters for an outing; taking the maximum of both would call a day wet
    # because of overnight rain nobody will be out in.
    probability = ((daytime.get("precipitation") or {}).get("probability") or {}).get(
        "percent"
    )

    return DailyWeather(
        day=parsed,
        condition=_GOOGLE_CONDITIONS.get(condition_code, UNKNOWN),
        temperature_max_c=_as_float(maximum),
        temperature_min_c=_as_float(minimum),
        precipitation_probability=_as_probability(_as_float(probability)),
        provider="google",
    )


class NoWeather:
    """Answers nothing, always.

    The offline provider, and deliberately not a mock returning a mild afternoon.
    A stub that invents weather is indistinguishable from a working integration
    right up until it plans somebody's picnic in a storm, which is the failure
    mode every stub in this codebase is written to avoid.
    """

    name = "none"

    async def forecast(
        self, latitude: float, longitude: float, *, days: int
    ) -> Forecast | None:
        return None


# --- parsing helpers ---------------------------------------------------------


def _parse_day(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _at(values: list, index: int, *, cast=float):
    """Read one element of a parallel array, tolerating a short or gappy one.

    Open-Meteo returns nulls inside these arrays where a variable is unavailable
    for a day rather than omitting the day, so this has to distinguish "no value"
    from "index missing" and return None for both.
    """
    if index >= len(values):
        return None
    value = values[index]
    if value is None:
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_probability(percent: float | None) -> float | None:
    """Percent to 0-1, clamped.

    Converted once, at the boundary. Every provider reports a percentage and
    nothing downstream should have to remember that.
    """
    if percent is None:
        return None
    return max(0.0, min(1.0, percent / 100.0))


# --- caching and selection ---------------------------------------------------


class _TtlCache:
    """Small time-boxed cache, keyed on rounded coordinates.

    Weather sits on a read path - discovery, planning and every concierge turn
    that mentions a day - and a request per card would be both slow and, on a
    keyless community service, abusive. Not an LRU: entries expire on time, and
    the eviction below is only about keeping memory bounded.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple, tuple[float, Forecast]] = {}

    def get(self, key: tuple) -> Forecast | None:
        found = self._entries.get(key)
        if found is None:
            return None
        stored_at, value = found
        if time.monotonic() - stored_at > CACHE_TTL_SECONDS:
            self._entries.pop(key, None)
            return None
        return value

    def put(self, key: tuple, value: Forecast) -> None:
        if len(self._entries) >= CACHE_MAX_ENTRIES:
            oldest = min(self._entries, key=lambda k: self._entries[k][0])
            self._entries.pop(oldest, None)
        self._entries[key] = (time.monotonic(), value)

    def clear(self) -> None:
        self._entries.clear()


_cache = _TtlCache()


@lru_cache
def get_provider() -> WeatherProvider:
    """Select the weather provider.

    ``auto`` prefers Google where a key is configured and falls back to the
    keyless service, matching how routing and geocoding choose. ``none`` is the
    explicit opt-out, and what the test suite runs on so a forecast can never
    make an assertion depend on the actual weather in Paris.
    """
    settings = get_settings()
    choice = settings.weather_provider

    if choice == "auto":
        choice = "google" if settings.google_maps_api_key else "open-meteo"

    if choice == "google":
        if not settings.google_maps_api_key:
            logger.warning("google_weather_selected_without_key_falling_back")
            return OpenMeteoWeather()
        return GoogleWeather(settings.google_maps_api_key)
    if choice == "none":
        return NoWeather()
    return OpenMeteoWeather()


# How many days ahead to request when a caller does not say. Covers "this
# weekend" and "next week", which is what almost every request means.
DEFAULT_DAYS = 10


async def forecast_for(
    latitude: float | None,
    longitude: float | None,
    *,
    days: int = DEFAULT_DAYS,
) -> Forecast | None:
    """Cached forecast for a coordinate, or None.

    Never raises and never fabricates. A caller that gets None must say it does
    not know rather than proceeding as though the weather were fine - which is
    the whole reason this returns an optional instead of an empty forecast that
    reads as "nothing to worry about".
    """
    if latitude is None or longitude is None:
        return None

    key = (round(latitude, CACHE_PRECISION), round(longitude, CACHE_PRECISION), days)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    provider = get_provider()
    with tracing.dependency("weather"):
        result = await provider.forecast(latitude, longitude, days=days)

    if result is None or result.is_empty:
        return None

    _cache.put(key, result)
    return result


async def forecast_covering(
    latitude: float | None,
    longitude: float | None,
    *,
    start: datetime,
    end: datetime,
) -> Forecast | None:
    """Forecast sized to the window a caller actually cares about.

    Requesting a fixed ten days for a trip that starts in nine wastes nothing,
    but requesting ten for a trip that ends tomorrow costs a larger response on
    every concierge turn. The span is derived from the request and clamped to the
    provider's horizon by the provider itself.
    """
    span = (end.date() - datetime.now(UTC).date()).days + 1
    if span < 1:
        return None
    return await forecast_for(latitude, longitude, days=min(span, 16))


def days_until(target: date) -> int:
    return (target - datetime.now(UTC).date()).days


# Kept module-level for the tests, which need a way to drop cached forecasts
# between cases without reaching into a private name.
def reset_cache() -> None:
    _cache.clear()
    get_provider.cache_clear()


__all__ = [
    "CLEAR",
    "CLOUDY",
    "COLD_C",
    "FOG",
    "HOT_C",
    "RAIN",
    "SNOW",
    "STORM",
    "UNKNOWN",
    "WET_PROBABILITY",
    "DailyWeather",
    "Forecast",
    "GoogleWeather",
    "NoWeather",
    "OpenMeteoWeather",
    "WeatherProvider",
    "forecast_covering",
    "forecast_for",
    "get_provider",
    "reset_cache",
]
