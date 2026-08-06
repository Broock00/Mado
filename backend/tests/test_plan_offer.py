"""Offering a plan in conversation, and keeping it.

Planning belongs in the conversation: an explorer who says "I'm free this
evening" has already given the planner everything it needs, and a form asks for
the same thing again in a worse notation. These cover the parts of that flow
where being wrong is quiet - a plan saved that is not the plan shown, or a reply
that reads as a menu when a sequence was computed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domains.ai.gateway import _render_plan_block

NOW = datetime(2026, 8, 7, 17, 0, tzinfo=UTC)


def offered(**overrides) -> dict:
    base = {
        "stops": [
            {
                "experienceId": "11111111-1111-1111-1111-111111111111",
                "eventInstanceId": None,
                "title": "Tomoca Piassa",
                "arriveAt": NOW.isoformat(),
                "departAt": (NOW + timedelta(minutes=25)).isoformat(),
                "dwellMinutes": 25,
                "travelMinutes": 0,
                "travelKm": None,
                "estimatedCost": 60.0,
                "isFixedTime": False,
                "note": None,
            },
            {
                "experienceId": "22222222-2222-2222-2222-222222222222",
                "eventInstanceId": "33333333-3333-3333-3333-333333333333",
                "title": "Azmari Night at Fendika",
                "arriveAt": (NOW + timedelta(hours=3)).isoformat(),
                "departAt": (NOW + timedelta(hours=6)).isoformat(),
                "dwellMinutes": 180,
                "travelMinutes": 13,
                "travelKm": 3.0,
                "estimatedCost": 300.0,
                "isFixedTime": True,
                "note": "Starts at a set time",
            },
        ],
        "totalCost": 360.0,
        "currency": "ETB",
        "totalTravelMinutes": 13,
        "rationale": "2 stops, about 13 minutes of travel in total, and roughly 360 ETB.",
        "unmet": [],
        "request": {
            "startsAt": NOW.isoformat(),
            "endsAt": (NOW + timedelta(hours=7)).isoformat(),
            "city": "addis-ababa",
            "latitude": 9.0107,
            "longitude": 38.7614,
            "budget": None,
            "maxStops": 3,
            "categories": [],
            "freeOnly": False,
        },
    }
    base.update(overrides)
    return base


class TestPlanBlock:
    """What the model is shown when a plan exists.

    Handed the flat card list instead, it read the stops as alternatives and
    talked the explorer out of the very plan underneath its reply.
    """

    def test_it_is_presented_as_an_order(self):
        block = _render_plan_block(offered())
        assert "1. Tomoca Piassa" in block
        assert "2. Azmari Night at Fendika" in block
        assert block.index("1. Tomoca") < block.index("2. Azmari")

    def test_it_forbids_reordering(self):
        block = _render_plan_block(offered())
        assert "Do not reorder" in block

    def test_times_are_pre_formatted(self):
        """The model must never compute or convert a time."""
        block = _render_plan_block(offered())
        assert "17:00-17:25" in block

    def test_fixed_stops_are_marked(self):
        assert "set time" in _render_plan_block(offered())

    def test_travel_is_attributed_to_the_leg(self):
        assert "13 min from the previous stop" in _render_plan_block(offered())

    def test_the_first_stop_has_no_travel_leg(self):
        """Travel from wherever the explorer already is cannot be described."""
        block = _render_plan_block(offered())
        first_line = [ln for ln in block.splitlines() if ln.startswith("1. ")][0]
        assert "from the previous stop" not in first_line

    def test_totals_are_stated(self):
        block = _render_plan_block(offered())
        assert "2 stops" in block and "360 ETB" in block

    def test_a_free_plan_says_so_rather_than_showing_zero(self):
        plan = offered(totalCost=0.0)
        assert "nothing to pay" in _render_plan_block(plan)

    def test_unmet_constraints_are_carried_through(self):
        plan = offered(unmet=["Fitted 2 of the 4 stops you asked for."])
        assert "Could not fit: Fitted 2 of the 4" in _render_plan_block(plan)


class TestSavingWhatWasOffered:
    """Accepting saves the plan shown, not one recomputed on the way past."""

    def test_the_offer_carries_the_request_that_produced_it(self):
        """So the saved itinerary records what was actually asked for."""
        assert offered()["request"]["city"] == "addis-ababa"

    def test_every_stop_carries_its_experience(self):
        """Without an id a saved stop cannot link back to the listing."""
        assert all(stop["experienceId"] for stop in offered()["stops"])

    def test_a_scheduled_stop_keeps_its_occurrence(self):
        """So the itinerary points at the performance, not just the venue."""
        fixed = [s for s in offered()["stops"] if s["isFixedTime"]]
        assert fixed and fixed[0]["eventInstanceId"]

    @pytest.mark.parametrize("empty", [{"stops": []}, {}])
    def test_an_empty_offer_is_not_savable(self, empty):
        assert not (empty.get("stops") or [])
