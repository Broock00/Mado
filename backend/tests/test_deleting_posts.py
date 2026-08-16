"""Deleting a post (spec PUB-001).

The delete itself is unremarkable. What these tests are about is the one case
where it must not happen: somebody has already booked. A listing that quietly
stops existing after money changed hands leaves the buyer holding a ticket for
an evening that no longer appears anywhere, and nobody has told them.

So the refusal is the feature, and cancelling the date - which does tell them -
is the way out.
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


def make_post(
    client, auth, *, with_date: bool = False, free: bool = True
) -> tuple[str, str | None]:
    venue = client.post(
        "/api/v1/posts/venues",
        headers=auth,
        json={
            "name": f"Room {uuid.uuid4().hex[:5]}",
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
            "title": f"Deletable {uuid.uuid4().hex[:5]}",
            "description": (
                "A listing used to check that deleting one works, and that it stops "
                "working the moment somebody has booked a place at it."
            ),
            "citySlug": CITY,
            "type": "event" if with_date else "place",
            "categorySlug": "music",
            "venueId": venue.json()["data"]["id"],
            "priceType": "free" if free else "fixed",
            "priceAmount": None if free else 300,
        },
    )
    assert post.status_code == 201, post.text
    experience_id = post.json()["data"]["id"]

    occurrence = None
    if with_date:
        when = datetime.now(UTC) + timedelta(days=4)
        response = client.post(
            f"/api/v1/posts/{experience_id}/events",
            headers=auth,
            json={"startTime": when.isoformat(), "capacity": 40},
        )
        assert response.status_code == 201, response.text
        occurrence = response.json()["data"]["id"]
    return experience_id, occurrence


class TestDeletingWhatNobodyHasBooked:
    def test_a_draft_can_be_deleted(self, client):
        auth = account(client)
        experience_id, _ = make_post(client, auth)

        assert client.delete(f"/api/v1/posts/{experience_id}", headers=auth).status_code == 204

    def test_it_leaves_your_list(self, client):
        auth = account(client)
        experience_id, _ = make_post(client, auth)
        client.delete(f"/api/v1/posts/{experience_id}", headers=auth)

        mine = client.get("/api/v1/posts", headers=auth).json()["data"]
        assert experience_id not in [post["id"] for post in mine]

    def test_a_published_post_can_be_deleted_too(self, client):
        """Unpublishing first is not required. Somebody deleting a live listing
        nobody has booked means it, and making them do it in two steps just
        leaves the thing on the internet for longer."""
        auth = account(client)
        experience_id, _ = make_post(client, auth)
        published = client.post(f"/api/v1/posts/{experience_id}/publish", headers=auth)
        assert published.status_code == 200, published.text

        assert client.delete(f"/api/v1/posts/{experience_id}", headers=auth).status_code == 204
        # And it is gone from where an explorer would find it.
        assert client.get(f"/api/v1/experiences/{experience_id}").status_code == 404


class TestOnceSomebodyHasBooked:
    def test_it_refuses_when_a_ticket_is_held(self, client):
        """The whole point. The buyer's evening does not disappear because the
        publisher tidied up."""
        auth = account(client)
        experience_id, occurrence_id = make_post(client, auth, with_date=True)
        client.post(f"/api/v1/posts/{experience_id}/publish", headers=auth)
        applied = client.put(
            f"/api/v1/posts/{experience_id}/tickets",
            headers=auth,
            json={"name": "Free entry", "priceMinor": 0, "quantity": 40},
        )
        assert applied.status_code == 200, applied.text

        guest = account(client, "guest")
        shown = client.get(
            f"/api/v1/experiences/{experience_id}/events/{occurrence_id}/ticketing"
        ).json()["data"]
        order = client.post(
            f"/api/v1/experiences/{experience_id}/events/{occurrence_id}/orders",
            headers=guest,
            json={"lines": [{"ticketTypeId": shown["ticketTypes"][0]["id"], "quantity": 2}]},
        )
        assert order.status_code == 201, order.text

        refused = client.delete(f"/api/v1/posts/{experience_id}", headers=auth)
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "TICKETS_ALREADY_SOLD"
        assert refused.json()["error"]["details"]["held"] == 2

        # And it is still there, which is the point of refusing.
        assert client.get(f"/api/v1/posts/{experience_id}", headers=auth).status_code == 200


class TestOnlyTheAuthor:
    def test_somebody_else_cannot_delete_your_post(self, client):
        auth = account(client)
        experience_id, _ = make_post(client, auth)

        stranger = account(client, "stranger")
        response = client.delete(f"/api/v1/posts/{experience_id}", headers=stranger)
        assert response.status_code in (403, 404), response.text
        assert client.get(f"/api/v1/posts/{experience_id}", headers=auth).status_code == 200
