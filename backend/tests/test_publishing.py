"""Publishing and moderation API tests.

Covers the property the product now rests on: an ordinary explorer with no
organization, no verification and no approval step can post something and have it
reach other people - while the things that protect those people still hold.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tests.conftest import requires_api

BASE_URL = os.environ.get("MADO_TEST_API_URL", "http://127.0.0.1:8000")
CITY = "addis-ababa"
PASSWORD = "discover-addis-2026"

# Meilisearch indexes asynchronously, so assertions about search results need a
# moment. The author's own view comes from Postgres and is immediate.
INDEX_SETTLE_SECONDS = 1.5


# Fails rather than skips when the API is down - see tests/conftest.py for why.
@pytest.fixture(scope="module", autouse=True)
def _api_required() -> None:
    requires_api()


@pytest.fixture
def client():
    with httpx.Client(base_url=BASE_URL, timeout=30) as session:
        yield session


def make_account(client, name="Explorer") -> tuple[dict, str]:
    email = f"{name.lower()}-{uuid.uuid4().hex[:8]}@mado-qa.example.org"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "displayName": name},
    )
    assert response.status_code == 201
    token = response.json()["data"]["tokens"]["accessToken"]
    return {"Authorization": f"Bearer {token}"}, email


def make_venue(client, auth, name="Test Venue") -> dict:
    response = client.post(
        "/api/v1/posts/venues",
        headers=auth,
        json={
            "name": f"{name} {uuid.uuid4().hex[:4]}",
            "address": "Bole Road, Addis Ababa",
            "citySlug": CITY,
            "latitude": 9.0055,
            "longitude": 38.7810,
        },
    )
    assert response.status_code == 201
    return response.json()["data"]


def make_post(client, auth, **overrides) -> dict:
    venue = overrides.pop("venue", None) or make_venue(client, auth)
    payload = {
        "title": "An Evening of Spoken Word",
        "description": (
            "A small evening of poetry above Bole, with tea served throughout. "
            "Bring something to read, or just come and listen."
        ),
        "citySlug": CITY,
        "type": "place",
        "categorySlug": "arts-culture",
        "venueId": venue["id"],
    }
    payload.update(overrides)
    response = client.post("/api/v1/posts", headers=auth, json=payload)
    assert response.status_code == 201, response.text
    return response.json()["data"]


class TestAnyoneCanPublish:
    def test_a_personal_publisher_is_created_automatically(self, client):
        """No organization form stands between an explorer and posting."""
        auth, _ = make_account(client, "Amina")
        publisher = client.get("/api/v1/posts/me", headers=auth).json()["data"]
        assert publisher["type"] == "individual"
        assert publisher["name"] == "Amina"
        # Unverified, not "pending" - nothing is queued, and that is honest.
        assert publisher["verificationStatus"] == "unverified"

    def test_publishing_identity_is_stable(self, client):
        auth, _ = make_account(client)
        first = client.get("/api/v1/posts/me", headers=auth).json()["data"]
        second = client.get("/api/v1/posts/me", headers=auth).json()["data"]
        assert first["id"] == second["id"]

    def test_a_post_starts_as_a_draft(self, client):
        auth, _ = make_account(client)
        post = make_post(client, auth)
        assert post["status"] == "draft"

    def test_drafts_are_not_discoverable(self, client):
        auth, _ = make_account(client)
        post = make_post(client, auth)
        assert client.get(f"/api/v1/experiences/{post['id']}").status_code == 404

    def test_publish_then_anyone_can_see_it(self, client):
        auth, _ = make_account(client)
        post = make_post(client, auth)
        published = client.post(f"/api/v1/posts/{post['id']}/publish", headers=auth)
        assert published.status_code == 200
        assert published.json()["data"]["status"] == "published"
        # Signed out, no headers at all.
        assert client.get(f"/api/v1/experiences/{post['id']}").status_code == 200


class TestReadinessChecks:
    def test_incomplete_posts_cannot_publish(self, client):
        auth, _ = make_account(client)
        post = make_post(client, auth, categorySlug=None)
        response = client.post(f"/api/v1/posts/{post['id']}/publish", headers=auth)
        assert response.status_code == 422
        problems = response.json()["error"]["details"]["problems"]
        assert any("category" in p.lower() for p in problems)

    def test_an_event_needs_a_date(self, client):
        auth, _ = make_account(client)
        post = make_post(client, auth, type="event")
        response = client.post(f"/api/v1/posts/{post['id']}/publish", headers=auth)
        assert response.status_code == 422
        assert any("date" in p.lower() for p in response.json()["error"]["details"]["problems"])

    def test_adding_a_date_unblocks_publishing(self, client):
        auth, _ = make_account(client)
        post = make_post(client, auth, type="event")
        when = datetime.now(UTC) + timedelta(days=3)
        added = client.post(
            f"/api/v1/posts/{post['id']}/events",
            headers=auth,
            json={"startTime": when.isoformat(), "capacity": 20},
        )
        assert added.status_code == 201
        assert client.post(f"/api/v1/posts/{post['id']}/publish", headers=auth).status_code == 200

    def test_a_date_in_the_past_is_rejected(self, client):
        auth, _ = make_account(client)
        post = make_post(client, auth, type="event")
        past = datetime.now(UTC) - timedelta(days=1)
        response = client.post(
            f"/api/v1/posts/{post['id']}/events", headers=auth, json={"startTime": past.isoformat()}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "EVENT_IN_PAST"

    def test_a_priced_post_needs_a_price(self, client):
        auth, _ = make_account(client)
        response = client.post(
            "/api/v1/posts",
            headers=auth,
            json={
                "title": "Priced Without A Price",
                "description": "A description long enough to satisfy the readiness check here.",
                "citySlug": CITY,
                "type": "place",
                "priceType": "fixed",
            },
        )
        assert response.status_code == 422


class TestOwnership:
    def test_you_cannot_edit_someone_elses_post(self, client):
        author, _ = make_account(client, "Author")
        stranger, _ = make_account(client, "Stranger")
        post = make_post(client, author)

        response = client.patch(
            f"/api/v1/posts/{post['id']}", headers=stranger, json={"title": "Hijacked Title"}
        )
        # 404 rather than 403: confirming someone else's draft exists is itself
        # a disclosure.
        assert response.status_code == 404

    def test_you_cannot_publish_someone_elses_post(self, client):
        author, _ = make_account(client, "Author")
        stranger, _ = make_account(client, "Stranger")
        post = make_post(client, author)
        assert (
            client.post(f"/api/v1/posts/{post['id']}/publish", headers=stranger).status_code == 404
        )

    def test_your_list_contains_only_your_posts(self, client):
        author, _ = make_account(client, "Author")
        stranger, _ = make_account(client, "Stranger")
        mine = make_post(client, author)
        theirs = make_post(client, stranger)

        listed = client.get("/api/v1/posts", headers=author).json()["data"]
        ids = {item["id"] for item in listed}
        assert mine["id"] in ids
        assert theirs["id"] not in ids

    def test_publishing_requires_an_account(self, client):
        assert client.get("/api/v1/posts").status_code == 401


class TestLifecycle:
    def test_unpublish_removes_it_from_discovery(self, client):
        auth, _ = make_account(client)
        post = make_post(client, auth)
        client.post(f"/api/v1/posts/{post['id']}/publish", headers=auth)
        assert client.get(f"/api/v1/experiences/{post['id']}").status_code == 200

        client.post(f"/api/v1/posts/{post['id']}/unpublish", headers=auth)
        assert client.get(f"/api/v1/experiences/{post['id']}").status_code == 404

    def test_archive_and_restore(self, client):
        auth, _ = make_account(client)
        post = make_post(client, auth)
        archived = client.post(f"/api/v1/posts/{post['id']}/archive", headers=auth).json()["data"]
        assert archived["status"] == "archived"

        restored = client.post(f"/api/v1/posts/{post['id']}/restore", headers=auth).json()["data"]
        # Back to draft, never straight to live - the author decides.
        assert restored["status"] == "draft"

    def test_a_live_date_is_cancelled_not_deleted(self, client):
        """People may have planned around it, so it must not vanish silently."""
        auth, _ = make_account(client)
        post = make_post(client, auth, type="event")
        when = datetime.now(UTC) + timedelta(days=3)
        event = client.post(
            f"/api/v1/posts/{post['id']}/events", headers=auth, json={"startTime": when.isoformat()}
        ).json()["data"]
        client.post(f"/api/v1/posts/{post['id']}/publish", headers=auth)

        deleted = client.delete(f"/api/v1/posts/{post['id']}/events/{event['id']}", headers=auth)
        assert deleted.status_code == 409
        assert deleted.json()["error"]["code"] == "CANCEL_INSTEAD_OF_DELETE"

        cancelled = client.post(
            f"/api/v1/posts/{post['id']}/events/{event['id']}/cancel",
            headers=auth,
            json={"reason": "Venue double-booked"},
        ).json()["data"]
        assert cancelled["status"] == "cancelled"


class TestModeration:
    def test_spam_is_withheld_pending_review(self, client):
        auth, _ = make_account(client)
        post = make_post(
            client,
            auth,
            title="EARN MONEY FAST GUARANTEED",
            description=(
                "100% free investment opportunity! Make money from home with crypto. "
                "Click here now. WhatsApp +251911223344 or telegram me. Act now!"
            ),
        )
        published = client.post(f"/api/v1/posts/{post['id']}/publish", headers=auth).json()["data"]

        # "flagged", not "pending". This assertion used to read "pending" and pass
        # while the content stayed fully discoverable - the test name described
        # withholding that was not happening. The status is only meaningful via
        # DISCOVERABLE_MODERATION_STATUSES, which is what the next assertion checks.
        from app.domains.catalog.repository import DISCOVERABLE_MODERATION_STATUSES

        assert published["moderationStatus"] not in DISCOVERABLE_MODERATION_STATUSES
        assert published["moderationNotes"]
        # Withheld, not deleted - the author still has it.
        assert published["status"] == "published"

    def test_withheld_spam_is_absent_from_discovery(self, client):
        """The property that actually matters: a stranger cannot reach it."""
        auth, _ = make_account(client)
        post = make_post(
            client,
            auth,
            title="EARN MONEY FAST GUARANTEED",
            description=(
                "100% free investment opportunity! Make money from home with crypto. "
                "Click here now. WhatsApp +251911223344 or telegram me. Act now!"
            ),
        )
        client.post(f"/api/v1/posts/{post['id']}/publish", headers=auth)

        # Not readable by id, and not present in search.
        assert client.get(f"/api/v1/experiences/{post['id']}").status_code == 404
        results = client.get("/api/v1/search", params={"q": "EARN MONEY GUARANTEED"})
        titles = [r["title"] for r in results.json()["data"]["results"]]
        assert "EARN MONEY FAST GUARANTEED" not in titles

    def test_reports_withhold_content_at_the_threshold(self, client):
        author, _ = make_account(client, "Author")
        post = make_post(client, author)
        client.post(f"/api/v1/posts/{post['id']}/publish", headers=author)
        assert client.get(f"/api/v1/experiences/{post['id']}").status_code == 200

        for index in range(3):
            reporter, _ = make_account(client, f"Reporter{index}")
            response = client.post(
                f"/api/v1/experiences/{post['id']}/report",
                headers=reporter,
                json={"reason": "inaccurate", "detail": "This place has closed."},
            )
            assert response.status_code == 201

        assert client.get(f"/api/v1/experiences/{post['id']}").status_code == 404

        # The author keeps their copy and can see what happened.
        own = client.get(f"/api/v1/posts/{post['id']}", headers=author).json()["data"]
        assert own["moderationStatus"] == "flagged"
        assert own["reportCount"] == 3

    def test_a_single_report_does_not_take_content_down(self, client):
        """Otherwise one disgruntled person could remove a competitor's listing."""
        author, _ = make_account(client, "Author")
        reporter, _ = make_account(client, "Reporter")
        post = make_post(client, author)
        client.post(f"/api/v1/posts/{post['id']}/publish", headers=author)

        client.post(
            f"/api/v1/experiences/{post['id']}/report", headers=reporter, json={"reason": "spam"}
        )
        assert client.get(f"/api/v1/experiences/{post['id']}").status_code == 200

    def test_a_scam_report_acts_immediately(self, client):
        """Waiting for a threshold is the wrong trade when money is involved."""
        author, _ = make_account(client, "Author")
        reporter, _ = make_account(client, "Reporter")
        post = make_post(client, author)
        client.post(f"/api/v1/posts/{post['id']}/publish", headers=author)

        client.post(
            f"/api/v1/experiences/{post['id']}/report", headers=reporter, json={"reason": "scam"}
        )
        assert client.get(f"/api/v1/experiences/{post['id']}").status_code == 404

    def test_the_same_person_cannot_report_twice(self, client):
        author, _ = make_account(client, "Author")
        reporter, _ = make_account(client, "Reporter")
        post = make_post(client, author)
        client.post(f"/api/v1/posts/{post['id']}/publish", headers=author)

        first = client.post(
            f"/api/v1/experiences/{post['id']}/report", headers=reporter, json={"reason": "spam"}
        )
        second = client.post(
            f"/api/v1/experiences/{post['id']}/report",
            headers=reporter,
            json={"reason": "inaccurate"},
        )
        assert first.status_code == 201
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "ALREADY_REPORTED"

    def test_an_unknown_reason_is_rejected(self, client):
        author, _ = make_account(client, "Author")
        reporter, _ = make_account(client, "Reporter")
        post = make_post(client, author)
        client.post(f"/api/v1/posts/{post['id']}/publish", headers=author)

        response = client.post(
            f"/api/v1/experiences/{post['id']}/report",
            headers=reporter,
            json={"reason": "i-just-dont-like-it"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_REPORT_REASON"


class TestModerationIsPrivileged:
    def test_ordinary_explorers_cannot_see_the_queue(self, client):
        auth, _ = make_account(client)
        response = client.get("/api/v1/moderation/queue", headers=auth)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "NOT_A_MODERATOR"

    def test_ordinary_explorers_cannot_decide(self, client):
        auth, _ = make_account(client)
        response = client.post(
            f"/api/v1/moderation/{uuid.uuid4()}/decide", headers=auth, json={"approve": True}
        )
        assert response.status_code == 403

    def test_moderator_rights_cannot_be_self_granted(self, client):
        """The flag is a column, not a key in the preferences the explorer writes."""
        auth, _ = make_account(client)
        client.patch("/api/v1/me/preferences", headers=auth, json={"isModerator": True})
        assert client.get("/api/v1/moderation/queue", headers=auth).status_code == 403


class TestPublishToDiscover:
    def test_a_post_reaches_search_and_the_concierge(self, client):
        """The loop the whole product depends on."""
        auth, _ = make_account(client, "Local")
        marker = uuid.uuid4().hex[:6]
        post = make_post(
            client,
            auth,
            title=f"Kolo and Coffee Circle {marker}",
            description=(
                "A weekly gathering in Piassa over roasted barley and buna. "
                "Newcomers welcome, no need to bring anything."
            ),
            categorySlug="food-drink",
        )
        client.post(f"/api/v1/posts/{post['id']}/publish", headers=auth)
        time.sleep(INDEX_SETTLE_SECONDS)

        found = client.get(
            "/api/v1/search", params={"q": f"kolo coffee circle {marker}", "city": CITY}
        ).json()["data"]
        assert any(marker in item["title"] for item in found["results"])

        reply = client.post(
            "/api/v1/assistant/messages",
            json={"message": "where can I find a coffee gathering?", "city": CITY},
        ).json()["data"]
        assert reply["results"], "the concierge returned nothing at all"
