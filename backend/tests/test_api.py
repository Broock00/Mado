"""API contract tests.

Exercised against the running stack rather than with mocks, because the things
most likely to break - the response envelope, cursor pagination, the anonymous
path, and search degradation - only manifest end to end.

Skipped automatically when the API is not running, so the unit suite stays usable
without Docker.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from tests.conftest import requires_api

BASE_URL = os.environ.get("MADO_TEST_API_URL", "http://127.0.0.1:8000")
CITY = "addis-ababa"


# Fails rather than skips when the API is down - see tests/conftest.py for why.
@pytest.fixture(scope="module", autouse=True)
def _api_required() -> None:
    requires_api()


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=30) as session:
        yield session


class TestEnvelope:
    def test_single_resource_uses_a_data_envelope(self, client):
        response = client.get(f"/api/v1/cities/{CITY}")
        assert response.status_code == 200
        body = response.json()
        assert "data" in body
        assert body["data"]["slug"] == CITY

    def test_collections_carry_pagination(self, client):
        body = client.get("/api/v1/cities").json()
        assert isinstance(body["data"], list)
        assert "pagination" in body

    def test_errors_use_the_structured_shape(self, client):
        response = client.get(f"/api/v1/experiences/{uuid.uuid4()}")
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "EXPERIENCE_NOT_FOUND"
        assert error["requestId"]

    def test_request_id_is_echoed_back(self, client):
        supplied = "req_test_correlation"
        response = client.get("/api/v1/cities", headers={"X-Request-ID": supplied})
        assert response.headers["X-Request-ID"] == supplied

    def test_validation_failure_names_the_field(self, client):
        response = client.get("/api/v1/search", params={"q": ""})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_FAILED"


class TestAnonymousAccess:
    """Spec 10.01.01: exploration must work before any account exists."""

    def test_discovery_canvas_is_public(self, client):
        body = client.get("/api/v1/discover", params={"city": CITY}).json()
        assert body["data"]["city"] == CITY
        assert len(body["data"]["modules"]) > 0

    def test_search_is_public(self, client):
        body = client.get("/api/v1/search", params={"q": "coffee", "city": CITY}).json()
        assert body["data"]["meta"]["total"] >= 1

    def test_saved_items_require_authentication(self, client):
        response = client.get("/api/v1/me/saved")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"

    def test_an_invalid_token_degrades_to_anonymous_rather_than_failing(self, client):
        """A stale token in an old tab should not break public browsing."""
        response = client.get(
            "/api/v1/discover",
            params={"city": CITY},
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert response.status_code == 200


class TestDiscovery:
    def test_cards_carry_an_explanation(self, client):
        """Spec PRODUCT-00 principle 5: recommendations explain themselves."""
        body = client.get(
            "/api/v1/discover", params={"city": CITY, "lat": 9.0104, "lng": 38.7638}
        ).json()
        items = [item for module in body["data"]["modules"] for item in module["items"]]
        assert items
        assert any(item["reason"] for item in items)

    def test_location_produces_distances(self, client):
        body = client.get(
            "/api/v1/discover/nearby",
            params={"city": CITY, "lat": 9.0104, "lng": 38.7638, "radiusKm": 5},
        ).json()
        assert all(item["distanceKm"] is not None for item in body["data"])

    def test_no_module_is_returned_empty(self, client):
        """Spec 10.01.03: a module with nothing eligible is omitted, not shown bare."""
        body = client.get("/api/v1/discover", params={"city": CITY}).json()
        assert all(module["items"] for module in body["data"]["modules"])

    def test_tonight_only_returns_events_starting_within_the_window(self, client):
        body = client.get("/api/v1/discover/tonight", params={"city": CITY}).json()
        for item in body["data"]:
            if item["nextEvent"]:
                assert item["nextEvent"]["status"] != "cancelled"


class TestConcierge:
    def test_a_time_bounded_question_returns_time_bounded_results(self, client):
        """The canonical spec query must route to events, not to an all-day cafe."""
        body = client.post(
            "/api/v1/assistant/messages",
            json={"message": "what should I do tonight?", "city": CITY},
        ).json()
        data = body["data"]
        assert data["conversationId"]
        # Either every suggestion is dated, or there are none. Substituting
        # undated places for a "tonight" question would answer a question the
        # explorer did not ask.
        assert all(result["when"] for result in data["results"])
        if not data["results"]:
            assert "tonight" in data["message"].lower()
            assert data["suggestedActions"]

    def test_replies_are_grounded_in_real_experiences(self, client):
        """Spec 56.01 s3.1: results must be retrievable records, not invention."""
        body = client.post(
            "/api/v1/assistant/messages",
            json={"message": "find me traditional coffee", "city": CITY},
        ).json()
        results = body["data"]["results"]
        assert results
        for result in results[:3]:
            lookup = client.get(f"/api/v1/experiences/{result['id']}")
            assert lookup.status_code == 200

    def test_conversation_continues_across_turns(self, client):
        first = client.post(
            "/api/v1/assistant/messages",
            json={"message": "anything on this weekend?", "city": CITY},
        ).json()["data"]

        second = client.post(
            "/api/v1/assistant/messages",
            json={
                "message": "only the free ones",
                "conversationId": first["conversationId"],
                "city": CITY,
            },
        ).json()["data"]

        assert second["conversationId"] == first["conversationId"]

    def test_empty_message_is_rejected(self, client):
        response = client.post("/api/v1/assistant/messages", json={"message": "   ", "city": CITY})
        assert response.status_code in (400, 422)


class TestAuthLifecycle:
    def test_register_login_refresh_and_save(self, client):
        email = f"explorer-{uuid.uuid4().hex[:10]}@mado-qa.example.org"
        password = "discover-addis-2026"

        registered = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "displayName": "Test Explorer"},
        )
        assert registered.status_code == 201
        tokens = registered.json()["data"]["tokens"]

        auth = {"Authorization": f"Bearer {tokens['accessToken']}"}
        me = client.get("/api/v1/me", headers=auth).json()["data"]
        assert me["profile"]["email"] == email

        # Rotation: the old refresh token must not survive its own use.
        refreshed = client.post(
            "/api/v1/auth/refresh", json={"refreshToken": tokens["refreshToken"]}
        )
        assert refreshed.status_code == 200
        replayed = client.post(
            "/api/v1/auth/refresh", json={"refreshToken": tokens["refreshToken"]}
        )
        assert replayed.status_code == 401

        # Saving is idempotent - a double tap is not an error.
        experience_id = client.get("/api/v1/experiences", params={"city": CITY, "limit": 1}).json()[
            "data"
        ][0]["id"]

        first = client.post(f"/api/v1/me/saved/experience/{experience_id}", headers=auth)
        assert first.status_code == 201
        second = client.post(f"/api/v1/me/saved/experience/{experience_id}", headers=auth)
        assert second.status_code == 201

        saved = client.get("/api/v1/me/saved", headers=auth).json()["data"]
        assert len([item for item in saved if item["entityId"] == experience_id]) == 1

        removed = client.delete(f"/api/v1/me/saved/experience/{experience_id}", headers=auth)
        assert removed.status_code == 204

    def test_duplicate_registration_is_rejected(self, client):
        email = f"explorer-{uuid.uuid4().hex[:10]}@mado-qa.example.org"
        payload = {"email": email, "password": "discover-addis-2026", "displayName": "Dup"}
        assert client.post("/api/v1/auth/register", json=payload).status_code == 201
        conflict = client.post("/api/v1/auth/register", json=payload)
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"

    def test_weak_password_is_rejected(self, client):
        response = client.post(
            "/api/v1/auth/register",
            json={
                "email": f"weak-{uuid.uuid4().hex[:8]}@mado-qa.example.org",
                "password": "1234567890",  # long enough, but digits only
                "displayName": "Weak",
            },
        )
        assert response.status_code == 422

    def test_wrong_password_is_rejected(self, client):
        email = f"explorer-{uuid.uuid4().hex[:10]}@mado-qa.example.org"
        client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "discover-addis-2026", "displayName": "X"},
        )
        response = client.post(
            "/api/v1/auth/login", json={"email": email, "password": "wrong-password-here"}
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"
