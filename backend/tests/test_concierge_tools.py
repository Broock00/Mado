"""The concierge, asked real questions (spec AI-002).

This file exists because of a bug it would have caught. A refactor changed
`query_experiences` to take an area instead of a city slug, and two tool bodies
were updated in opposite wrong directions: one began passing `area=` to a
service that takes `city_slug`, the other kept passing `city_slug=` to a
repository that no longer accepts it. Both raised TypeError on every call.

Nothing noticed. The gateway catches tool failures deliberately - spec 56.01
s3.8 asks for safe failure, so one broken tool must not fail the turn - and
answers with whatever else it has. So the concierge kept replying in whole
sentences, using a fallback that ignores the explorer's question. It read like a
thin catalogue rather than a broken search, which is the most expensive kind of
bug: it argues for the wrong fix.

Tested through the running API rather than by calling the tools directly. That
is where the swallowing happens, and a test that reached past it would pass
while the concierge stayed broken. It is also the only place the whole path -
classify, plan, execute, compose - is exercised at once.
"""

from __future__ import annotations

import os

import httpx
import pytest

from tests.conftest import requires_api

BASE_URL = os.environ.get("MADO_TEST_API_URL", "http://127.0.0.1:8000")

# Coordinates in the seeded city, so a working search has hundreds of listings
# and dozens of events to find. An empty answer here is a bug, not a quiet night.
LATITUDE = 9.03
LONGITUDE = 38.74


@pytest.fixture(scope="module", autouse=True)
def _api_required() -> None:
    requires_api()


@pytest.fixture
def client():
    with httpx.Client(base_url=BASE_URL, timeout=60.0) as session:
        yield session


@pytest.fixture(scope="module")
def deterministic() -> bool:
    """Whether the server is running the offline provider.

    A live model writes its own words, so anything asserting on phrasing - or on
    a model deciding a message was unclear - is a coin toss against it. Those
    assertions are worth keeping and are only meaningful against the stub, whose
    replies are composed by code.
    """
    try:
        checks = httpx.get(f"{BASE_URL}/health/ready", timeout=20.0).json()["checks"]
    except Exception:  # noqa: BLE001 - the readiness probe is not the test
        return False
    return any(c["name"] == "ai" and c["status"] == "disabled" for c in checks)


def ask(client: httpx.Client, message: str, **overrides) -> dict:
    body = {"message": message, "latitude": LATITUDE, "longitude": LONGITUDE, **overrides}
    response = client.post("/api/v1/assistant/messages", json=body)
    response.raise_for_status()
    return response.json()["data"]


class TestItFindsThings:
    def test_a_plain_search_returns_results(self, client):
        """The simplest thing that was broken. With the tool raising, this came
        back empty or with an unrelated fallback list."""
        answer = ask(client, "jazz")
        assert answer["results"], "the concierge found nothing in a city full of listings"

    def test_asking_what_is_on_returns_events(self, client):
        answer = ask(client, "what is happening this week")
        assert answer["results"]

    def test_asking_for_somewhere_nearby_returns_something(self, client):
        answer = ask(client, "something near me")
        assert answer["results"]


class TestItListensToTheQuestion:
    def test_two_different_questions_get_two_different_answers(self, client):
        """The symptom the bug actually produced, and the thing a "no results"
        test would have missed. Search was failing, the gateway fell back to an
        unfiltered browse, and every question got the same generic list -
        fluently, and wrongly."""
        jazz = {item["id"] for item in ask(client, "live music")["results"]}
        museums = {item["id"] for item in ask(client, "museums and galleries")["results"]}

        assert jazz and museums
        assert jazz != museums, (
            "two unrelated questions returned identical results - the query is "
            "being ignored and something upstream is answering instead"
        )

    def test_an_unparseable_question_is_never_answered_confidently(
        self, client, deterministic
    ):
        """The concierge deliberately shows something rather than an empty page,
        which is a reasonable choice and must not be a silent one.

        So a message it could not read has to carry one of two honest signals: a
        reply saying nothing matched, or a question asking what was meant.
        Generic results presented as an answer, with neither, is the shape a
        broken search takes - and did, for a while.
        """
        if not deterministic:
            pytest.skip("a live model decides for itself whether a message is unclear")

        answer = ask(client, "zqxjkv wpfmgh")
        message = answer.get("message") or ""
        honest = (
            bool(answer.get("clarification"))
            or "Nothing matched" in message
            or "could not find" in message
        )
        assert honest, (
            "an unreadable question got confident-looking results with no "
            "clarification and no admission that nothing matched"
        )

    def test_a_question_it_understands_is_answered_without_hedging(
        self, client, deterministic
    ):
        """The other side: a clear question should not be met with a clarifying
        question. One that always asks is as useless as one that never does."""
        if not deterministic:
            pytest.skip("a live model decides for itself whether to ask")

        answer = ask(client, "jazz")
        assert answer["results"]
        assert not answer.get("clarification")


class TestItKnowsWhereItIs:
    def test_it_answers_about_the_explorers_own_city(self, client):
        answer = ask(client, "what is on this week")
        assert answer["results"]
        # Named somewhere in the reply. Which words surround it are the
        # model's business; that it is the right place is not.
        assert "Addis" in (answer.get("message") or ""), (
            "the reply does not name the city the explorer is standing in"
        )

    def test_it_does_not_answer_about_somewhere_else(self, client):
        """Coordinates in a place with nothing published must not fall back to
        the seeded city. Somebody in Paris being shown Addis Ababa is the bug
        this whole area of the codebase was rebuilt to remove."""
        answer = ask(client, "what is on this week", latitude=48.8566, longitude=2.3522)
        assert not answer["results"]
        assert "Addis" not in (answer.get("message") or "")

    def test_with_no_location_at_all_it_asks_rather_than_guesses(self, client):
        answer = ask(client, "what is on tonight", latitude=None, longitude=None)
        assert not answer["results"]
        assert "Addis" not in (answer.get("message") or "")
