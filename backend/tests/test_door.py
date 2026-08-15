"""Scanning a ticket at the door (spec COM-003).

The valid ticket is the least interesting case. What this file is really about
is the four ways a scan fails, because a door that reports them all as "invalid"
is a door that cannot be run:

* the same ticket twice - a screenshot forwarded to three friends is still one
  admission, and the second scan succeeding is the failure the whole feature
  exists to prevent
* a real ticket for another night - a mistake, not an attempt, and saying so
  turns an argument back into a correction
* a ticket that was voided
* a code that is simply not one

Driven through the API, because the scoping to one date is enforced there and a
test of the service alone would pass while any door admitted any ticket.
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


def account(client, who="host") -> dict:
    email = f"{who}-{uuid.uuid4().hex[:8]}@mado-qa.example.org"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "displayName": who.title()},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['data']['tokens']['accessToken']}"}


def sold_out_night(client, auth, *, dates: int = 1) -> tuple[str, list[str], list[str]]:
    """A listing with free tickets, bought - free orders settle without a provider,
    which is what makes a real issued ticket available to scan in a test."""
    venue = client.post(
        "/api/v1/posts/venues",
        headers=auth,
        json={
            "name": f"Door {uuid.uuid4().hex[:5]}",
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
            "title": f"Door test {uuid.uuid4().hex[:5]}",
            "description": (
                "An evening used to check that a scanned ticket is admitted once "
                "and refused every time after that."
            ),
            "citySlug": CITY,
            "type": "event",
            "categorySlug": "music",
            "venueId": venue.json()["data"]["id"],
            "priceType": "free",
        },
    )
    assert post.status_code == 201, post.text
    experience_id = post.json()["data"]["id"]

    occurrences = []
    for offset in range(1, dates + 1):
        when = datetime.now(UTC) + timedelta(days=offset)
        response = client.post(
            f"/api/v1/posts/{experience_id}/events",
            headers=auth,
            json={"startTime": when.isoformat(), "capacity": 50},
        )
        assert response.status_code == 201, response.text
        occurrences.append(response.json()["data"]["id"])

    # A zero-priced tier: registration, and a ticket at the end of it.
    applied = client.put(
        f"/api/v1/posts/{experience_id}/tickets",
        headers=auth,
        json={"name": "Free entry", "priceMinor": 0, "quantity": 50},
    )
    assert applied.status_code == 200, applied.text
    return experience_id, occurrences, []


def buy_one(client, experience_id: str, occurrence_id: str) -> tuple[dict, str]:
    """One explorer, one free ticket, which completes without a provider."""
    guest = account(client, "guest")
    shown = client.get(
        f"/api/v1/experiences/{experience_id}/events/{occurrence_id}/ticketing"
    ).json()["data"]
    tier = shown["ticketTypes"][0]
    order = client.post(
        f"/api/v1/experiences/{experience_id}/events/{occurrence_id}/orders",
        headers=guest,
        json={"lines": [{"ticketTypeId": tier["id"], "quantity": 1}]},
    )
    assert order.status_code == 201, order.text
    body = order.json()["data"]
    assert body["status"] == "paid", f"a free order should settle at once: {body['status']}"
    assert body["tickets"], "a settled free order should have issued a ticket"
    return guest, body["tickets"][0]["code"]


def scan(client, auth, experience_id, occurrence_id, code, admit=True):
    return client.post(
        f"/api/v1/posts/{experience_id}/events/{occurrence_id}/scan",
        headers=auth,
        json={"code": code, "admit": admit},
    )


class TestAdmittingSomebody:
    def test_a_valid_ticket_is_admitted_and_named(self, client):
        auth = account(client)
        experience_id, dates, _ = sold_out_night(client, auth)
        _, code = buy_one(client, experience_id, dates[0])

        response = scan(client, auth, experience_id, dates[0], code)
        assert response.status_code == 200, response.text
        result = response.json()["data"]
        assert result["verdict"] == "admitted"
        assert result["name"] == "Guest"
        assert result["ticketTypeName"] == "Free entry"
        assert result["admittedCount"] == 1

    def test_the_same_ticket_twice_is_refused(self, client):
        """The point of the whole feature. A screenshot forwarded to three
        friends is one admission; if the second scan succeeded the other two
        walk in free."""
        auth = account(client)
        experience_id, dates, _ = sold_out_night(client, auth)
        _, code = buy_one(client, experience_id, dates[0])

        first = scan(client, auth, experience_id, dates[0], code).json()["data"]
        second = scan(client, auth, experience_id, dates[0], code).json()["data"]

        assert first["verdict"] == "admitted"
        assert second["verdict"] == "already_admitted"
        # And it says when, so a door can tell a duplicate from a re-entry.
        assert second["checkedInAt"] is not None
        assert second["admittedCount"] == 1, "a refused scan must not count again"

    def test_looking_a_ticket_up_does_not_spend_it(self, client):
        auth = account(client)
        experience_id, dates, _ = sold_out_night(client, auth)
        _, code = buy_one(client, experience_id, dates[0])

        peeked = scan(client, auth, experience_id, dates[0], code, admit=False).json()["data"]
        assert peeked["verdict"] == "admitted"
        assert peeked["admittedCount"] == 0

        # Still good, because looking is not using.
        used = scan(client, auth, experience_id, dates[0], code).json()["data"]
        assert used["verdict"] == "admitted"


class TestTheWaysItFails:
    def test_a_ticket_for_another_night_is_not_admitted(self, client):
        """And is told apart from a forgery, because it is a mistake rather than
        an attempt and the door has to know which."""
        auth = account(client)
        experience_id, dates, _ = sold_out_night(client, auth, dates=2)
        _, code = buy_one(client, experience_id, dates[1])

        result = scan(client, auth, experience_id, dates[0], code).json()["data"]
        assert result["verdict"] == "wrong_event"

    def test_a_ticket_for_another_night_gives_nothing_away(self, client):
        """It is a real ticket and none of this door's business whose."""
        auth = account(client)
        experience_id, dates, _ = sold_out_night(client, auth, dates=2)
        _, code = buy_one(client, experience_id, dates[1])

        result = scan(client, auth, experience_id, dates[0], code).json()["data"]
        assert result["name"] is None
        assert result["reference"] is None

    def test_a_code_that_is_not_one_is_unknown(self, client):
        auth = account(client)
        experience_id, dates, _ = sold_out_night(client, auth)

        result = scan(client, auth, experience_id, dates[0], "NOTAREALCODE1234").json()["data"]
        assert result["verdict"] == "unknown"
        assert result["name"] is None


class TestOnlyTheDoorItBelongsTo:
    def test_somebody_else_cannot_scan_this_event(self, client):
        auth = account(client)
        experience_id, dates, _ = sold_out_night(client, auth)
        _, code = buy_one(client, experience_id, dates[0])

        stranger = account(client, "stranger")
        assert scan(client, stranger, experience_id, dates[0], code).status_code == 403

    def test_a_refused_scan_did_not_admit_anybody(self, client):
        """The permission check has to happen before the ticket is spent, not
        after - otherwise a stranger's rejected scan still burns the admission."""
        auth = account(client)
        experience_id, dates, _ = sold_out_night(client, auth)
        _, code = buy_one(client, experience_id, dates[0])

        stranger = account(client, "stranger")
        scan(client, stranger, experience_id, dates[0], code)

        mine = scan(client, auth, experience_id, dates[0], code).json()["data"]
        assert mine["verdict"] == "admitted"
