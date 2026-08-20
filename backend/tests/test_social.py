"""Reposting.

The count is the thing most likely to go quietly wrong: it is denormalised onto
the listing, so a path that writes a row without recomputing leaves a number that
is wrong for ever and looks fine. Most of these assert the count, not just the
row.

Likes and comments were tested here too and were removed with the features -
reviews already carry a rating and a written opinion, and `test_reviews.py`
covers those.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from tests.conftest import requires_api

BASE_URL = os.environ.get("MADO_TEST_API_URL", "http://127.0.0.1:8000")
PASSWORD = "discover-addis-2026"
CITY = "addis-ababa"


@pytest.fixture(scope="module", autouse=True)
def _api_required() -> None:
    requires_api()


@pytest.fixture
def client():
    with httpx.Client(base_url=BASE_URL, timeout=30) as session:
        yield session


def account(client, who="explorer") -> dict:
    email = f"{who}-{uuid.uuid4().hex[:8]}@mado-qa.example.org"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "displayName": who.title()},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['data']['tokens']['accessToken']}"}


def published_listing(client, auth) -> str:
    """A listing anyone can see, which is what a repost attaches to."""
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
            "title": f"Somewhere worth sharing {uuid.uuid4().hex[:5]}",
            # At least 40 characters, or publishing refuses it as incomplete.
            "description": "A listing the tests can repost, long enough to publish.",
            "citySlug": CITY,
            "type": "place",
            "categorySlug": "food-drink",
            "venueId": venue.json()["data"]["id"],
        },
    )
    assert post.status_code == 201, post.text
    post_id = post.json()["data"]["id"]

    published = client.post(f"/api/v1/posts/{post_id}/publish", headers=auth)
    assert published.status_code == 200, published.text
    return post_id


def draft_listing(client, auth) -> str:
    post = client.post(
        "/api/v1/posts",
        headers=auth,
        json={
            "title": f"Not published yet {uuid.uuid4().hex[:5]}",
            "description": "Still a draft, and nobody should be able to reach it.",
            "citySlug": CITY,
            "type": "place",
        },
    )
    assert post.status_code == 201, post.text
    return post.json()["data"]["id"]


def card(client, experience_id: str, auth=None) -> dict:
    response = client.get(f"/api/v1/experiences/{experience_id}", headers=auth or {})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def repost(client, auth, experience_id: str, note=None) -> httpx.Response:
    return client.post(
        f"/api/v1/experiences/{experience_id}/repost", headers=auth, json={"note": note}
    )


class TestReposts:
    def test_reposting_toggles_and_counts(self, client):
        author = account(client, "author")
        reader = account(client, "reader")
        listing = published_listing(client, author)

        first = repost(client, reader, listing, "The reason to go is the roastery at the back.")
        assert first.status_code == 200, first.text
        assert first.json()["data"] == {"active": True, "count": 1}
        assert card(client, listing)["repostCount"] == 1

        undo = repost(client, reader, listing)
        assert undo.json()["data"] == {"active": False, "count": 0}
        assert card(client, listing)["repostCount"] == 0

    def test_two_people_both_count(self, client):
        author = account(client, "author")
        listing = published_listing(client, author)

        for who in ("one", "two"):
            repost(client, account(client, who), listing)

        assert card(client, listing)["repostCount"] == 2

    def test_the_card_says_whether_you_reposted(self, client):
        author = account(client, "author")
        reader = account(client, "reader")
        listing = published_listing(client, author)
        repost(client, reader, listing)

        assert card(client, listing, reader)["isReposted"] is True
        # Somebody else's repost is not yours.
        assert card(client, listing, account(client, "other"))["isReposted"] is False

    def test_signing_in_is_required(self, client):
        author = account(client, "author")
        listing = published_listing(client, author)
        response = client.post(
            f"/api/v1/experiences/{listing}/repost", json={"note": None}
        )
        assert response.status_code == 401

    def test_a_draft_cannot_be_reposted(self, client):
        """Otherwise an id is enough to circulate somebody's unpublished work."""
        author = account(client, "author")
        reader = account(client, "reader")
        draft = draft_listing(client, author)

        assert repost(client, reader, draft).status_code == 404

    def test_the_count_is_recomputed_not_incremented(self, client):
        """Recomputed from the rows, so a count cannot drift out of step with
        what is actually there."""
        author = account(client, "author")
        one = account(client, "one")
        two = account(client, "two")
        listing = published_listing(client, author)

        repost(client, one, listing)
        repost(client, two, listing)
        repost(client, one, listing)  # undo

        assert card(client, listing)["repostCount"] == 1


class TestLikesAndCommentsAreGone:
    """They were removed in favour of reviews. These assert the endpoints are
    actually gone rather than merely unlinked from the interface - a route left
    serving after its feature is withdrawn is a way back in that nobody is
    looking at."""

    def test_liking_is_not_an_endpoint(self, client):
        author = account(client, "author")
        reader = account(client, "reader")
        listing = published_listing(client, author)

        response = client.post(f"/api/v1/experiences/{listing}/like", headers=reader)
        assert response.status_code == 404, response.text

    def test_commenting_is_not_an_endpoint(self, client):
        author = account(client, "author")
        reader = account(client, "reader")
        listing = published_listing(client, author)

        posted = client.post(
            f"/api/v1/experiences/{listing}/comments",
            headers=reader,
            json={"body": "This should have nowhere to go."},
        )
        assert posted.status_code == 404, posted.text
        assert client.get(f"/api/v1/experiences/{listing}/comments").status_code == 404

    def test_the_card_carries_no_like_or_comment_counts(self, client):
        author = account(client, "author")
        listing = published_listing(client, author)
        body = card(client, listing)

        assert "likeCount" not in body
        assert "commentCount" not in body
        assert "isLiked" not in body
        # And the one that stayed is still there.
        assert body["repostCount"] == 0
