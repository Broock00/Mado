"""Routing between stops (spec MAP-002, vendor strategy 82.01 §10).

A plan says "Fendika at 19:00, then Tomoca at 21:00". Route guidance answers the
question that follows: how do I actually get from one to the other, and how long
does it really take?

**The vendor routes; Mado plans.** Spec 82.01 treats maps and routing as
infrastructure - the provider knows about roads and one-way streets, and Mado
knows which stops are worth visiting. Nothing about ordering or selection is
delegated here; this returns geometry and durations for a sequence the planner
already chose.

That split extends to durations, and not symmetrically. OSRM's distances and
geometry are real roads and are used as given; its *durations* are free-flow and
were measured returning 7 km across central Addis in 8 minutes, which is 53 km/h
through a city where that is not possible. So an OSRM leg keeps the vendor's
distance and gets its duration from this city's own speeds. Google Routes models
traffic, so its duration is used as returned.

**Why the planner does not use this.** Building an itinerary evaluates travel
between many candidate pairs - insertion costs, 2-opt swaps - and routing every
one of them would be hundreds of calls per plan, taking seconds and costing
money to answer questions about stops that never make the final list. So the
planner keeps its cheap haversine estimate for *solving*, and real routing is
fetched once for the sequence that survives.

That split has an honest consequence: the real duration will sometimes disagree
with the estimate the plan was built on. When it does, the real one is shown -
:func:`route_plan` reports the difference so the interface can say the timings
moved rather than quietly presenting new numbers as though they were always
there.

Three providers behind one protocol, matching the geocoding module: Google
Routes where a key is configured, OSRM otherwise (keyless, and what makes this
work on a development machine), and a straight-line estimator that never leaves
the process. The estimator is not a mock - it is the same arithmetic the planner
uses, so an outage degrades to the numbers the plan already assumed rather than
to nothing.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from app.core import metrics, tracing
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("mado.routing")

REQUEST_TIMEOUT = 8.0

# Modes offered. Addis is a walking and taxi city; cycling is rare enough on
# these roads that offering it would be advice rather than a service.
WALK = "walk"
DRIVE = "drive"
MODES = (WALK, DRIVE)

# Beyond this, walking stops being a suggestion and becomes a bad one.
MAX_WALK_KM = 2.5

# Speeds for the offline estimator, in km/h. The driving figure matches the
# planner's own CITY_SPEED_KMH so a degraded route agrees with the plan it
# belongs to; walking is a real walking pace, not a brisk one.
WALK_SPEED_KMH = 4.5
DRIVE_SPEED_KMH = 18.0

# Straight line to road distance. Same factor the planner applies.
DETOUR_FACTOR = 1.35

# A duration this much longer than the plan assumed is worth telling someone
# about. Below it, the difference is noise against a 10-minute buffer.
MATERIAL_DRIFT_MINUTES = 8


@dataclass(slots=True)
class RouteLeg:
    """One hop between two consecutive stops."""

    from_index: int
    to_index: int
    mode: str
    duration_minutes: int
    distance_km: float
    # [[lon, lat], ...] - GeoJSON order, ready for a map layer without
    # re-ordering on the client, where getting it backwards puts Addis in Somalia.
    geometry: list[list[float]] = field(default_factory=list)
    provider: str = "estimate"

    @property
    def is_estimated(self) -> bool:
        """True when no router answered and this is arithmetic, not a road."""
        return self.provider == "estimate"


@dataclass(slots=True)
class Route:
    legs: list[RouteLeg]
    total_duration_minutes: int
    total_distance_km: float
    # The router that answered. Named honestly even when some legs fell back,
    # with `estimated_legs` carrying that instead - an earlier version set this
    # to "estimate" whenever any leg was estimated, which then contradicted
    # `is_estimated` on the same payload.
    provider: str

    @property
    def estimated_legs(self) -> int:
        return sum(1 for leg in self.legs if leg.is_estimated)

    @property
    def is_estimated(self) -> bool:
        """True only when nothing was routed for real."""
        return bool(self.legs) and self.estimated_legs == len(self.legs)


class Router(Protocol):
    name: str

    async def leg(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        *,
        mode: str,
    ) -> RouteLeg | None: ...


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    return radius * 2 * math.asin(math.sqrt(a))


def suggest_mode(distance_km: float) -> str:
    """Walk when walking is reasonable, otherwise assume a taxi.

    Purely about distance. Weather, luggage and whether somebody is wearing the
    right shoes are all real and none of them are knowable here, so the
    interface lets them switch rather than pretending this is a judgement.
    """
    return WALK if distance_km <= MAX_WALK_KM else DRIVE


def _minutes_for(road_km: float, mode: str) -> int:
    """How long that distance takes in this city, at this mode.

    Used for any router whose own duration cannot be trusted, and by the offline
    estimator. Keeping one function means a plan and a route never disagree
    about how fast walking is.
    """
    speed = WALK_SPEED_KMH if mode == WALK else DRIVE_SPEED_KMH
    return max(1, int(round((road_km / speed) * 60)))


class StraightLineRouter:
    """No network. The planner's own arithmetic, shaped as a route.

    Deliberately not a mock returning zeros. When a router is unreachable the
    honest degradation is the estimate the plan was already built on, and the
    geometry is a straight line - which the interface draws dashed, because a
    solid line through three buildings is a claim about roads that nobody made.
    """

    name = "estimate"

    async def leg(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        *,
        mode: str,
    ) -> RouteLeg | None:
        straight = haversine_km(origin[0], origin[1], destination[0], destination[1])
        road_km = straight * DETOUR_FACTOR
        return RouteLeg(
            from_index=0,
            to_index=0,
            mode=mode,
            duration_minutes=_minutes_for(road_km, mode),
            distance_km=round(road_km, 2),
            geometry=[[origin[1], origin[0]], [destination[1], destination[0]]],
            provider=self.name,
        )


class OsrmRouter:
    """OpenStreetMap routing. Keyless, which is why development works.

    The public demo server is rate limited and explicitly not for production;
    `MADO_OSRM_URL` points at a self-hosted instance when there is one. Failure
    returns None so the caller falls back rather than losing the leg.
    """

    name = "osrm"

    # OSRM profiles. The public server only carries `driving`, so a walking
    # request against it comes back as a driving route with a walking duration
    # computed from it - which is wrong by a factor of four. Walking is
    # therefore requested only from a self-hosted instance that advertises the
    # profile, and estimated otherwise.
    _PROFILES = {WALK: "foot", DRIVE: "driving"}

    def __init__(self, base_url: str, *, profiles: frozenset[str]) -> None:
        self.base_url = base_url.rstrip("/")
        self.profiles = profiles

    async def leg(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        *,
        mode: str,
    ) -> RouteLeg | None:
        if mode not in self.profiles:
            return None

        profile = self._PROFILES[mode]
        coordinates = f"{origin[1]},{origin[0]};{destination[1]},{destination[0]}"
        url = f"{self.base_url}/route/v1/{profile}/{coordinates}"

        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.get(
                    url, params={"overview": "full", "geometries": "geojson"}
                )
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("osrm_route_failed", error=str(exc))
            return None

        routes = body.get("routes") or []
        if not routes:
            return None

        route = routes[0]
        geometry = (route.get("geometry") or {}).get("coordinates") or []
        distance_km = round(float(route.get("distance", 0)) / 1000, 2)

        # The distance and the shape come from OSRM; the duration does not.
        #
        # Measured on a real Piassa-to-Bole trip, OSRM returned 7.06 km in 8
        # minutes - 53 km/h across central Addis. Its public server models
        # free-flow speeds with no traffic, no junctions and no minibuses, so
        # its duration is confidently wrong in the direction that makes people
        # late. Google Routes models traffic and is trusted; OSRM is not.
        #
        # This is the vendor/platform split the specs describe, applied to the
        # thing the vendor is actually weak at here: it knows the roads, and
        # Mado knows how fast you move through this city.
        return RouteLeg(
            from_index=0,
            to_index=0,
            mode=mode,
            duration_minutes=_minutes_for(distance_km, mode),
            distance_km=distance_km,
            geometry=[[float(c[0]), float(c[1])] for c in geometry],
            provider=self.name,
        )


class GoogleRouter:
    """Google Routes. The vendor the specs name, where a key is configured."""

    name = "google"

    _MODES = {WALK: "WALK", DRIVE: "DRIVE"}
    _URL = "https://routes.googleapis.com/directions/v2:computeRoutes"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def leg(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        *,
        mode: str,
    ) -> RouteLeg | None:
        payload = {
            "origin": {"location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}},
            "destination": {
                "location": {"latLng": {"latitude": destination[0], "longitude": destination[1]}}
            },
            "travelMode": self._MODES.get(mode, "DRIVE"),
            "polylineEncoding": "GEO_JSON_LINESTRING",
        }
        headers = {
            # In a header, never a query string. A key in a URL ends up in
            # transport error messages and access logs - which is exactly how
            # this project leaked one before.
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.polyline",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.post(self._URL, json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("google_route_failed", error=str(exc))
            return None

        routes = body.get("routes") or []
        if not routes:
            return None

        route = routes[0]
        # Durations arrive as "1234s".
        raw = str(route.get("duration", "0s")).rstrip("s")
        try:
            seconds = float(raw)
        except ValueError:
            seconds = 0.0

        geometry = ((route.get("polyline") or {}).get("geoJsonLinestring") or {}).get(
            "coordinates"
        ) or []
        return RouteLeg(
            from_index=0,
            to_index=0,
            mode=mode,
            duration_minutes=max(1, int(round(seconds / 60))),
            distance_km=round(float(route.get("distanceMeters", 0)) / 1000, 2),
            geometry=[[float(c[0]), float(c[1])] for c in geometry],
            provider=self.name,
        )


def get_router() -> Router:
    """Select the router.

    `auto` prefers Google when a key is set and falls back to OSRM, so a
    development machine routes for free and production uses the named vendor.
    """
    settings = get_settings()
    choice = settings.routing_provider

    if choice == "auto":
        choice = "google" if settings.google_maps_api_key else "osrm"

    if choice == "google":
        if not settings.google_maps_api_key:
            logger.warning("google_router_selected_without_key_falling_back")
            return OsrmRouter(settings.osrm_url, profiles=_osrm_profiles(settings))
        return GoogleRouter(settings.google_maps_api_key)
    if choice == "estimate":
        return StraightLineRouter()
    return OsrmRouter(settings.osrm_url, profiles=_osrm_profiles(settings))


def _osrm_profiles(settings) -> frozenset[str]:
    """Which modes this OSRM instance can actually answer.

    The public demo server carries driving only. Asking it for a walking route
    silently returns a driving one, and a 25-minute drive presented as a
    25-minute walk is a worse answer than an honest estimate.
    """
    return frozenset(settings.osrm_profiles)


async def route_plan(
    points: list[tuple[float, float] | None],
    *,
    modes: list[str] | None = None,
) -> Route:
    """Route a whole sequence, leg by leg.

    `points` is the stops in order; a None entry is a stop with no coordinates,
    and the legs either side of it are skipped rather than guessed at. Returning
    a straight line from an unknown place would be a drawn claim about a journey
    nobody can take.

    Falls back per leg, not per route. One unroutable hop degrades to an estimate
    while the rest keep their real geometry, because losing the whole route over
    a single failure is a worse answer than a mixed one - and `is_estimated` on
    each leg lets the interface show which is which.
    """
    router = get_router()
    fallback = StraightLineRouter()
    legs: list[RouteLeg] = []

    for index in range(len(points) - 1):
        origin, destination = points[index], points[index + 1]
        if origin is None or destination is None:
            continue

        mode = (
            modes[index]
            if modes and index < len(modes) and modes[index] in MODES
            else suggest_mode(haversine_km(origin[0], origin[1], destination[0], destination[1]))
        )

        # Timed, then counted on the returned value rather than on whether an
        # exception escaped. `leg` reports failure by returning None - it never
        # raises - so wrapping it in the usual dependency helper would count
        # every outage as a success and draw a graph saying the routing vendor
        # was perfectly healthy at the moment it stopped answering.
        #
        # The vendor call only: the straight-line fallback below is arithmetic,
        # and counting it would hide the same thing a second way.
        started = time.perf_counter()
        with tracing.span("routing"):
            leg = await router.leg(origin, destination, mode=mode)
        metrics.dependency_calls.inc("routing", "ok" if leg is not None else "error")
        metrics.dependency_duration.observe(time.perf_counter() - started, "routing")

        if leg is None:
            leg = await fallback.leg(origin, destination, mode=mode)
        if leg is None:
            continue

        leg.from_index = index
        leg.to_index = index + 1
        legs.append(leg)

    return Route(
        legs=legs,
        total_duration_minutes=sum(leg.duration_minutes for leg in legs),
        total_distance_km=round(sum(leg.distance_km for leg in legs), 2),
        provider=router.name,
    )


def drift_minutes(route: Route, planned_travel_minutes: int) -> int:
    """How much longer the real route takes than the plan assumed.

    Positive means the evening is tighter than it looked. Reported rather than
    silently applied: an explorer who has already read the times deserves to be
    told they moved, not to find different numbers on the same page.
    """
    return route.total_duration_minutes - planned_travel_minutes
