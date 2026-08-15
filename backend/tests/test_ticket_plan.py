"""Tickets defined once, sold on every date (spec COM-002).

Tiers are stored per occurrence because inventory is per occurrence: last Friday
sold out and next Friday has not. Authoring them per occurrence was a different
claim and a wrong one - the publisher of a six-night run was asked to type the
same VIP tier six times, and any night they missed went on sale with nothing but
general admission, silently.

Driven through the API rather than against the functions, because the part worth
protecting is the whole path: the plan is grouped from rows on several dates,
applied outward to all of them, and inherited by a date added afterwards. A unit
test of the grouping would have passed while any one of those was broken.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tests.conftest import requires_api

BASE_URL = os.environ.get("MADO_TEST_API_URL", "http://127.0.0.1:8000")
CITY = "addis-ababa"
PASSWORD = "discover-addis-2026"


@pytest.fixture(scope="module", autouse=True)
def _api_required() -> None:
    requires_api()


@pytest.fixture
def client():
    with httpx.Client(base_url=BASE_URL, timeout=30) as session:
        yield session


def account(client) -> dict:
    email = f"host-{uuid.uuid4().hex[:8]}@mado-qa.example.org"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "displayName": "Host"},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['data']['tokens']['accessToken']}"}


def listing_with_dates(client, auth, *, dates: int = 3) -> tuple[str, list[str]]:
    venue = client.post(
        "/api/v1/posts/venues",
        headers=auth,
        json={
            "name": f"Rooftop {uuid.uuid4().hex[:5]}",
            "address": "Bole Road, Addis Ababa",
            "citySlug": CITY,
            "latitude": 9.0055,
            "longitude": 38.7810,
        },
    )
    assert venue.status_code == 201, venue.text

    post = client.post(
        "/api/v1/posts",
        headers=auth,
        json={
            "title": f"A run of nights {uuid.uuid4().hex[:5]}",
            "description": (
                "A short run of evenings used to check that a ticket defined once "
                "is sold on every night of it."
            ),
            "citySlug": CITY,
            "type": "event",
            "categorySlug": "music",
            "venueId": venue.json()["data"]["id"],
            "priceType": "range",
            "priceAmount": 300,
        },
    )
    assert post.status_code == 201, post.text
    experience_id = post.json()["data"]["id"]

    added = []
    for offset in range(1, dates + 1):
        when = datetime.now(UTC) + timedelta(days=offset * 7)
        response = client.post(
            f"/api/v1/posts/{experience_id}/events",
            headers=auth,
            json={"startTime": when.isoformat(), "capacity": 100},
        )
        assert response.status_code == 201, response.text
        added.append(response.json()["data"]["id"])
    return experience_id, added


def sell(client, auth, experience_id: str, **tier) -> httpx.Response:
    return client.put(
        f"/api/v1/posts/{experience_id}/tickets", headers=auth, json=tier
    )


class TestDefinedOnceSoldEverywhere:
    def test_one_tier_reaches_every_date(self, client):
        auth = account(client)
        experience_id, dates = listing_with_dates(client, auth, dates=3)

        response = sell(
            client,
            auth,
            experience_id,
            name="VIP",
            priceMinor=80000,
            quantity=20,
            description="Table on the mezzanine.",
        )
        assert response.status_code == 200, response.text

        # One line in the plan...
        plan = response.json()["data"]
        assert [entry["name"] for entry in plan] == ["VIP"]
        assert plan[0]["dates"] == 3

        # ...and a real tier on each night, which is what an explorer buys.
        for occurrence_id in dates:
            shown = client.get(
                f"/api/v1/experiences/{experience_id}/events/{occurrence_id}/ticketing"
            ).json()["data"]
            assert [t["name"] for t in shown["ticketTypes"]] == ["VIP"]
            assert shown["ticketTypes"][0]["description"] == "Table on the mezzanine."

    def test_applying_the_same_tier_twice_does_not_double_it(self, client):
        """A publisher correcting a price is applying the same tier again. If
        that added a second row, the listing would sell two VIP tiers at
        different prices and the explorer would choose between them."""
        auth = account(client)
        experience_id, dates = listing_with_dates(client, auth, dates=2)

        sell(client, auth, experience_id, name="VIP", priceMinor=80000, quantity=20)
        corrected = sell(
            client, auth, experience_id, name="VIP", priceMinor=90000, quantity=20
        )

        plan = corrected.json()["data"]
        assert len(plan) == 1
        assert plan[0]["priceMinor"] == 90000

        shown = client.get(
            f"/api/v1/experiences/{experience_id}/events/{dates[0]}/ticketing"
        ).json()["data"]
        assert len(shown["ticketTypes"]) == 1
        assert shown["ticketTypes"][0]["priceMinor"] == 90000

    def test_a_date_added_afterwards_inherits_the_tickets(self, client):
        """The failure this whole thing exists to remove. Without inheritance,
        "define the tickets once" holds until somebody adds another night, and
        that night goes on sale empty without saying so."""
        auth = account(client)
        experience_id, _ = listing_with_dates(client, auth, dates=1)
        sell(client, auth, experience_id, name="VIP", priceMinor=80000, quantity=20)
        sell(client, auth, experience_id, name="General", priceMinor=30000, quantity=60)

        later = datetime.now(UTC) + timedelta(days=90)
        added = client.post(
            f"/api/v1/posts/{experience_id}/events",
            headers=auth,
            json={"startTime": later.isoformat(), "capacity": 100},
        )
        assert added.status_code == 201, added.text
        new_date = added.json()["data"]["id"]

        shown = client.get(
            f"/api/v1/experiences/{experience_id}/events/{new_date}/ticketing"
        ).json()["data"]
        assert sorted(t["name"] for t in shown["ticketTypes"]) == ["General", "VIP"]
        assert shown["cta"] == "get_tickets"


class TestWithdrawing:
    def test_withdrawing_clears_it_from_every_date(self, client):
        auth = account(client)
        experience_id, dates = listing_with_dates(client, auth, dates=2)
        sell(client, auth, experience_id, name="VIP", priceMinor=80000, quantity=20)
        sell(client, auth, experience_id, name="General", priceMinor=30000, quantity=60)

        gone = client.post(
            f"/api/v1/posts/{experience_id}/tickets/withdraw",
            headers=auth,
            json={"name": "VIP"},
        )
        assert gone.status_code == 200, gone.text
        assert [entry["name"] for entry in gone.json()["data"]] == ["General"]

        for occurrence_id in dates:
            shown = client.get(
                f"/api/v1/experiences/{experience_id}/events/{occurrence_id}/ticketing"
            ).json()["data"]
            assert [t["name"] for t in shown["ticketTypes"]] == ["General"]


class TestItStillNeedsADate:
    def test_a_listing_with_no_dates_says_so(self, client):
        """A ticket is sold for a date. Reporting success while storing nothing
        is the failure worth guarding - the publisher would leave believing the
        listing was on sale."""
        auth = account(client)
        experience_id, _ = listing_with_dates(client, auth, dates=0)

        response = sell(client, auth, experience_id, name="VIP", priceMinor=80000)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "NO_DATES_TO_SELL"


class TestOnlyThePublisher:
    def test_somebody_else_cannot_read_or_change_the_plan(self, client):
        auth = account(client)
        experience_id, _ = listing_with_dates(client, auth, dates=1)
        sell(client, auth, experience_id, name="VIP", priceMinor=80000)

        stranger = account(client)
        assert client.get(
            f"/api/v1/posts/{experience_id}/tickets", headers=stranger
        ).status_code == 403
        assert sell(
            client, stranger, experience_id, name="Free for me", priceMinor=0
        ).status_code == 403
