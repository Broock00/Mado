"""Answering about somewhere the explorer is not.

The reported failure: sitting in Addis, asking "next week we have a plan to go to
newyork and is there any music night there", and being told about Azmari Night in
Kazanchis. The area came only from the client - device position or place picker -
so nothing in the sentence could move it, and the follow-up "how is the weather in
newyork this weekend" answered with Addis weather.

Every test here is about a way that can silently go wrong again, and most of them
assert on what is *absent*: no distance from a city you are not in, no reverting
to home on a follow-up, no forgetting a place that was named three turns ago.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from app.domains.ai.gateway import AIGateway, ConversationPlace
from app.domains.discovery.ranking import RankingContext, score_experience

ADDIS = (9.0107, 38.7614)
NEW_YORK = (40.7128, -74.0060)


@dataclass
class FakeVenue:
    latitude: float = NEW_YORK[0]
    longitude: float = NEW_YORK[1]
    name: str = "Venue"
    neighborhood: object | None = None
    facilities: list[str] = field(default_factory=list)


@dataclass
class FakeExperience:
    title: str = "Somewhere"
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    type: str = "place"
    category: object | None = None
    tags: list = field(default_factory=list)
    venue: FakeVenue | None = field(default_factory=FakeVenue)
    events: list = field(default_factory=list)
    price_type: str = "free"
    price_amount: float | None = None
    duration_minutes: int | None = 60
    is_indoor: bool | None = None
    suitability: list[str] = field(default_factory=list)
    popularity_score: float = 0.5
    quality_score: float = 0.6
    trend_score: float = 0.0
    rating_average: float | None = None
    rating_count: int = 0
    publisher: object | None = None


@dataclass
class FakeReading:
    """Just the parts of `Understanding` that `_locate` reads."""

    entities: dict = field(default_factory=dict)


@dataclass
class FakeConversation:
    state: dict | None = None


def a_place(**kwargs) -> ConversationPlace:
    defaults = {
        "label": "New York",
        "latitude": NEW_YORK[0],
        "longitude": NEW_YORK[1],
        "timezone": "America/New_York",
        "currency": "USD",
        "city_slug": "new-york",
        "country_code": "US",
        "kind": "city",
        "radius_km": 34.0,
        "bounding_box": (40.4, -74.3, 40.9, -73.7),
    }
    return ConversationPlace(**{**defaults, **kwargs})


# --- carrying a place across turns -------------------------------------------


class TestConversationPlaceSurvivesTheRoundTrip:
    def test_state_round_trip_preserves_everything(self):
        original = a_place()
        restored = ConversationPlace.from_state(original.as_state())
        assert restored == original

    def test_bounding_box_comes_back_as_a_tuple(self):
        """JSON turns a tuple into a list, and `Area` is compared by value - so a
        list here makes two identical areas unequal and quietly re-resolves."""
        restored = ConversationPlace.from_state(a_place().as_state())
        assert isinstance(restored.bounding_box, tuple)
        assert restored.area.has_box

    def test_a_country_scopes_by_code_not_by_shape(self):
        """A box around Kenya covers four neighbours; the one around the US spans
        the globe. Only the code is exact."""
        place = a_place(
            label="Kenya", kind="country", area_country_code="KE", bounding_box=None
        )
        assert place.area.country_code == "KE"
        assert not place.area.has_box


class TestPrecedence:
    """Which place wins when more than one is on the table."""

    @pytest.fixture
    def gateway(self):
        return AIGateway.__new__(AIGateway)  # no session needed for these paths

    async def test_nothing_named_and_nothing_remembered_leaves_it_alone(self, gateway):
        found = await gateway._locate(
            reading=FakeReading(),
            conversation=FakeConversation(),
            ctx=RankingContext(now=datetime.now(UTC)),
            chosen_label=None,
        )
        assert found is None

    async def test_a_remembered_place_survives_a_follow_up(self, gateway):
        """"What else is on while we are there" must not go home."""
        found = await gateway._locate(
            reading=FakeReading(),
            conversation=FakeConversation(state={"place": a_place().as_state()}),
            ctx=RankingContext(now=datetime.now(UTC)),
            chosen_label=None,
        )
        assert found is not None
        assert found.label == "New York"
        assert found.currency == "USD"

    async def test_picking_a_place_in_the_interface_beats_a_remembered_one(self, gateway):
        """Moving the picker is a deliberate act and should win over something
        said three turns ago."""
        found = await gateway._locate(
            reading=FakeReading(),
            conversation=FakeConversation(state={"place": a_place().as_state()}),
            ctx=RankingContext(now=datetime.now(UTC)),
            chosen_label="Nairobi",
        )
        assert found is None, "the caller's own area should stand"

    async def test_a_remembered_place_is_not_re_resolved(self, gateway):
        """Rehydrated from state rather than looked up again - a second question
        about New York must not spend a second place lookup."""
        called = False

        async def explode(*args, **kwargs):  # pragma: no cover - must not run
            nonlocal called
            called = True
            raise AssertionError("resolve_place was called for a remembered place")

        from app.domains.catalog import locate

        original = locate.resolve_place
        locate.resolve_place = explode
        try:
            await gateway._locate(
                reading=FakeReading(),
                conversation=FakeConversation(state={"place": a_place().as_state()}),
                ctx=RankingContext(now=datetime.now(UTC)),
                chosen_label=None,
            )
        finally:
            locate.resolve_place = original
        assert not called


# --- not pretending to be there ----------------------------------------------


class TestRemoteQuestionsDoNotClaimProximity:
    def test_no_walking_distance_from_another_continent(self):
        """The coordinates are the destination's, so distance is small and
        meaningless. It offered "about 9 min walk from you" for a bar in New York
        to somebody in Addis."""
        here = FakeExperience(venue=FakeVenue(NEW_YORK[0] + 0.001, NEW_YORK[1]))
        remote = score_experience(
            here,
            RankingContext(
                now=datetime.now(UTC),
                latitude=NEW_YORK[0],
                longitude=NEW_YORK[1],
                located_remotely=True,
            ),
        )
        assert "from you" not in (remote.reason or "")
        assert "walk" not in (remote.reason or "")

    def test_the_same_listing_does_say_it_when_they_are_there(self):
        """Otherwise the test above would pass by the reason never mentioning a
        walk at all, which would prove nothing."""
        here = FakeExperience(venue=FakeVenue(NEW_YORK[0] + 0.001, NEW_YORK[1]))
        local = score_experience(
            here,
            RankingContext(
                now=datetime.now(UTC),
                latitude=NEW_YORK[0],
                longitude=NEW_YORK[1],
                located_remotely=False,
            ),
        )
        assert "walk from you" in (local.reason or "")

    def test_the_prompt_forbids_distance_talk_when_remote(self):
        from app.domains.ai.prompts import build_context_notes

        notes = build_context_notes(
            has_location=True,
            preferences=None,
            time_label=None,
            constraints=None,
            located_remotely=True,
        )
        assert "NOT there" in notes
        assert "near you" in notes


# --- the comprehension contract ----------------------------------------------


class TestDestinationIsAskedFor:
    def test_the_schema_carries_a_destination(self):
        from app.domains.ai.prompts import UNDERSTANDING_RESPONSE_SCHEMA

        assert "destination" in UNDERSTANDING_RESPONSE_SCHEMA["properties"]

    def test_the_prompt_explains_when_to_set_it(self):
        from app.domains.ai.prompts import UNDERSTANDING_SYSTEM_PROMPT

        assert "destination" in UNDERSTANDING_SYSTEM_PROMPT
        # The two rules that matter most: somewhere else, and staying in force.
        assert "not** where they already" in UNDERSTANDING_SYSTEM_PROMPT
        assert "stays in force" in UNDERSTANDING_SYSTEM_PROMPT

    def test_an_overlong_destination_is_discarded(self):
        """A model asked for a place name occasionally returns a sentence, and
        that must not become a place search."""
        from app.domains.ai.understanding import MAX_DESTINATION_LENGTH, _from_payload

        reading = _from_payload(
            {"intent": "SEARCH_EVENTS", "destination": "x" * (MAX_DESTINATION_LENGTH + 1)},
            now=datetime.now(UTC),
            timezone="UTC",
        )
        assert "destination" not in reading.entities

    def test_a_normal_destination_is_kept_and_tidied(self):
        from app.domains.ai.understanding import _from_payload

        reading = _from_payload(
            {"intent": "SEARCH_EVENTS", "destination": "  New   York  "},
            now=datetime.now(UTC),
            timezone="UTC",
        )
        assert reading.entities["destination"] == "New York"
