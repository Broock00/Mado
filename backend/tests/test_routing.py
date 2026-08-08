"""Route guidance tests (spec MAP-002).

The interesting decisions here are about which numbers to trust, not about
arithmetic. A router that returns a confident wrong duration is worse than one
that returns nothing, because the plan is built on it and somebody is late.
"""

from __future__ import annotations

import pytest

from app.integrations.routing import (
    DETOUR_FACTOR,
    DRIVE,
    DRIVE_SPEED_KMH,
    MATERIAL_DRIFT_MINUTES,
    MAX_WALK_KM,
    MODES,
    WALK,
    WALK_SPEED_KMH,
    Route,
    RouteLeg,
    StraightLineRouter,
    _minutes_for,
    drift_minutes,
    haversine_km,
    route_plan,
    suggest_mode,
)

pytestmark = pytest.mark.anyio

# Two real points in Addis: Piassa and Bole.
PIASSA = (9.0347, 38.7500)
BOLE = (8.9950, 38.7870)


def leg(**overrides) -> RouteLeg:
    return RouteLeg(
        **{
            "from_index": 0,
            "to_index": 1,
            "mode": DRIVE,
            "duration_minutes": 10,
            "distance_km": 3.0,
            "geometry": [[38.75, 9.03], [38.78, 9.00]],
            "provider": "osrm",
            **overrides,
        }
    )


class TestModeSuggestion:
    def test_a_short_hop_is_a_walk(self):
        assert suggest_mode(0.4) == WALK

    def test_across_the_city_is_not(self):
        assert suggest_mode(7.0) == DRIVE

    def test_the_boundary_is_a_distance_somebody_would_actually_walk(self):
        assert 1.0 <= MAX_WALK_KM <= 4.0
        assert suggest_mode(MAX_WALK_KM) == WALK
        assert suggest_mode(MAX_WALK_KM + 0.1) == DRIVE

    def test_only_two_modes_are_offered(self):
        """Cycling on these roads would be advice rather than a service."""
        assert set(MODES) == {WALK, DRIVE}


class TestCitySpeeds:
    def test_walking_is_a_walking_pace(self):
        assert 3.5 <= WALK_SPEED_KMH <= 6.0

    def test_driving_reflects_a_congested_city_not_an_empty_road(self):
        """OSRM answered 7 km across central Addis in 8 minutes - 53 km/h.

        Its public server models free flow with no traffic, no junctions and no
        minibuses. Trusting that would tell people they can cross the city in
        eight minutes.
        """
        assert 10.0 <= DRIVE_SPEED_KMH <= 30.0

    def test_the_same_distance_takes_longer_on_foot(self):
        assert _minutes_for(3.0, WALK) > _minutes_for(3.0, DRIVE)

    def test_a_trivial_distance_still_takes_a_minute(self):
        assert _minutes_for(0.001, WALK) >= 1


class TestTheOfflineEstimator:
    async def test_it_produces_a_usable_leg_with_no_network(self):
        result = await StraightLineRouter().leg(PIASSA, BOLE, mode=DRIVE)
        assert result is not None
        assert result.duration_minutes > 0
        assert result.distance_km > 0
        assert result.provider == "estimate"
        assert result.is_estimated

    async def test_it_allows_for_roads_not_being_straight(self):
        result = await StraightLineRouter().leg(PIASSA, BOLE, mode=DRIVE)
        straight = haversine_km(*PIASSA, *BOLE)
        assert result.distance_km == pytest.approx(straight * DETOUR_FACTOR, abs=0.05)

    async def test_the_geometry_is_two_points_in_geojson_order(self):
        """[lon, lat]. Reversed puts Addis in Somalia."""
        result = await StraightLineRouter().leg(PIASSA, BOLE, mode=WALK)
        assert result.geometry == [[PIASSA[1], PIASSA[0]], [BOLE[1], BOLE[0]]]
        assert 38 < result.geometry[0][0] < 39  # longitude
        assert 8 < result.geometry[0][1] < 10  # latitude


class TestRoutingAWholeSequence:
    async def test_an_unlocated_stop_breaks_the_legs_either_side(self):
        """Rather than drawing a journey from a place nobody knows."""
        points = [PIASSA, None, BOLE]
        route = await route_plan(points)
        assert route.legs == []

    async def test_a_single_stop_has_nothing_to_route(self):
        assert (await route_plan([PIASSA])).legs == []

    async def test_no_stops_at_all_is_not_an_error(self):
        route = await route_plan([])
        assert route.legs == []
        assert route.total_duration_minutes == 0


class TestHonestyAboutWhatWasRouted:
    def test_a_route_is_only_estimated_when_nothing_was_real(self):
        mixed = Route(
            legs=[leg(), leg(provider="estimate")],
            total_duration_minutes=20,
            total_distance_km=6.0,
            provider="osrm",
        )
        assert not mixed.is_estimated
        assert mixed.estimated_legs == 1

    def test_all_estimated_says_so(self):
        route = Route(
            legs=[leg(provider="estimate")],
            total_duration_minutes=10,
            total_distance_km=3.0,
            provider="estimate",
        )
        assert route.is_estimated

    def test_an_empty_route_is_not_claimed_as_estimated(self):
        """Nothing was estimated because nothing was routed."""
        empty = Route(
            legs=[], total_duration_minutes=0, total_distance_km=0, provider="osrm"
        )
        assert not empty.is_estimated

    def test_the_provider_is_named_honestly_alongside_the_fallback_count(self):
        """An earlier version set provider to "estimate" whenever any leg fell
        back, which then contradicted `is_estimated` on the same payload."""
        route = Route(
            legs=[leg(), leg(provider="estimate")],
            total_duration_minutes=20,
            total_distance_km=6.0,
            provider="osrm",
        )
        assert route.provider == "osrm"
        assert route.estimated_legs == 1
        assert not route.is_estimated


class TestDrift:
    def test_a_slower_real_route_is_positive(self):
        route = Route(legs=[], total_duration_minutes=30, total_distance_km=5.0, provider="osrm")
        assert drift_minutes(route, 18) == 12

    def test_a_faster_one_is_negative_and_not_warned_about(self):
        route = Route(legs=[], total_duration_minutes=12, total_distance_km=5.0, provider="osrm")
        assert drift_minutes(route, 18) == -6
        assert drift_minutes(route, 18) < MATERIAL_DRIFT_MINUTES

    def test_the_warning_threshold_is_larger_than_the_planner_buffer(self):
        """The planner already leaves ten minutes between stops, so a smaller
        difference than that is noise and warning about it is crying wolf."""
        from app.domains.explorer.planning import BUFFER_MINUTES

        assert MATERIAL_DRIFT_MINUTES >= BUFFER_MINUTES - 2


class TestVendorSelection:
    def test_a_key_is_never_put_in_a_url(self):
        """This project leaked a key that way once. The router sends a header."""
        import inspect

        from app.integrations.routing import GoogleRouter

        source = inspect.getsource(GoogleRouter)
        assert "X-Goog-Api-Key" in source
        assert "key=" not in source

    def test_osrm_declines_a_profile_it_does_not_serve(self):
        """The public server carries driving only. Asking it to walk returns a
        driving route with a walking label - wrong by a factor of four."""
        from app.integrations.routing import OsrmRouter

        router = OsrmRouter("http://example.invalid", profiles=frozenset({DRIVE}))
        assert WALK not in router.profiles
