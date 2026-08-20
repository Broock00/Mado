"""Business accounts and their teams.

Covers the feature spec's TEAM-*, BUS-* and the security section. Most of these
are about refusal rather than success, because a permission system is only worth
having if the denials work - and a denial that quietly does not fire looks
exactly like a feature working.

The account rule is asserted directly: an account is a person **or** a business,
never both. `test_the_account_becomes_the_business` and its two publishing
counterparts are the executable form of that - if a business account ever starts
publishing under a person's name, or a personal profile reappears beside the
business, they fail.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tests.conftest import requires_api

BASE_URL = os.environ.get("MADO_TEST_API_URL", "http://127.0.0.1:8000")
PASSWORD = "discover-addis-2026"


@pytest.fixture(scope="module", autouse=True)
def _api_required() -> None:
    requires_api()


@pytest.fixture
def client():
    with httpx.Client(base_url=BASE_URL, timeout=30) as session:
        yield session


def account(client, who="owner") -> tuple[dict, str]:
    """Register an explorer. Returns `(auth headers, email)`."""
    email = f"{who}-{uuid.uuid4().hex[:8]}@mado-qa.example.org"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "displayName": who.title()},
    )
    assert response.status_code == 201, response.text
    token = response.json()["data"]["tokens"]["accessToken"]
    return {"Authorization": f"Bearer {token}"}, email


def make_business(client, auth, **overrides) -> dict:
    """Turn an account into a business. There is no "create a business" any more."""
    payload = {
        "name": f"Test Hotel {uuid.uuid4().hex[:6]}",
        "businessType": "hotel",
        "description": "A place invented by the test suite.",
        **overrides,
    }
    response = client.post("/api/v1/me/account-type/business", headers=auth, json=payload)
    assert response.status_code == 201, response.text
    return response.json()["data"]["business"]


def invite(client, auth, business_id, email, role) -> dict:
    response = client.post(
        f"/api/v1/businesses/{business_id}/members",
        headers=auth,
        json={"email": email, "role": role},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def accept(client, auth, member_id) -> httpx.Response:
    return client.post(f"/api/v1/me/business-invitations/{member_id}/accept", headers=auth)


# --- USER-001..006, BUS-001/002: creating and editing --------------------------


class TestCreatingABusiness:
    def test_a_normal_account_can_create_one(self, client):
        auth, _ = account(client)
        business = make_business(client, auth)
        assert business["type"] == "organization"
        assert business["businessType"] == "hotel"
        assert business["businessTypeLabel"] == "Hotel"

    def test_a_new_business_is_not_verified(self, client):
        """Nobody has checked it. "pending" would imply a queued review."""
        auth, _ = account(client)
        assert make_business(client, auth)["verificationStatus"] == "unverified"

    def test_the_account_becomes_the_business(self, client):
        """The account model, made executable.

        One account, one identity. After converting, the account *is* the
        business - `/me` says so, and there is no personal profile alongside it.
        """
        auth, _ = account(client)

        before = client.get("/api/v1/me/account-type", headers=auth).json()["data"]
        assert before["accountType"] == "individual"
        assert before["chosen"] is False, "a fresh account has not answered yet"
        assert before["business"] is None

        business = make_business(client, auth)

        after = client.get("/api/v1/me/account-type", headers=auth).json()["data"]
        assert after["accountType"] == "business"
        assert after["chosen"] is True
        assert after["business"]["id"] == business["id"]

        # And `/me` carries it, so the shell knows on every screen without a
        # second request.
        me = client.get("/api/v1/me", headers=auth).json()["data"]
        assert me["accountType"] == "business"
        assert me["accountTypeChosen"] is True

    def test_a_business_account_publishes_as_the_business(self, client):
        """No "posting as" choice. The account is one thing, and that is whose
        name goes on the post."""
        auth, _ = account(client)
        business = make_business(client, auth)

        post = client.post(
            "/api/v1/posts",
            headers=auth,
            json={
                "title": f"Posted by the business {uuid.uuid4().hex[:6]}",
                "description": "No publisherId was sent; the account decides.",
                "citySlug": "addis-ababa",
                "type": "place",
            },
        )
        assert post.status_code == 201, post.text
        assert post.json()["data"]["publisher"]["id"] == business["id"]

    def test_an_individual_still_publishes_as_themselves(self, client):
        """The counterpart, so the test above is not passing because every post
        goes to the same place."""
        auth, _ = account(client)
        personal = client.get("/api/v1/posts/me", headers=auth).json()["data"]

        post = client.post(
            "/api/v1/posts",
            headers=auth,
            json={
                "title": f"Posted by a person {uuid.uuid4().hex[:6]}",
                "description": "An ordinary account posting under its own name.",
                "citySlug": "addis-ababa",
                "type": "place",
            },
        )
        assert post.status_code == 201, post.text
        assert post.json()["data"]["publisher"]["id"] == personal["id"]

    def test_converting_is_one_way_and_happens_once(self, client):
        auth, _ = account(client)
        make_business(client, auth)

        again = client.post(
            "/api/v1/me/account-type/business",
            headers=auth,
            json={"name": "Second Business"},
        )
        assert again.status_code == 409, again.text

        # And they cannot quietly go back to being a person.
        back = client.post("/api/v1/me/account-type/individual", headers=auth)
        assert back.status_code == 409, back.text

    def test_choosing_individual_records_the_answer(self, client):
        """So the interface asks once rather than treating a default as a
        decision."""
        auth, _ = account(client)
        response = client.post("/api/v1/me/account-type/individual", headers=auth)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["chosen"] is True

        me = client.get("/api/v1/me", headers=auth).json()["data"]
        assert me["accountType"] == "individual"
        assert me["accountTypeChosen"] is True

    def test_explorer_surfaces_still_work_for_a_business(self, client):
        """A business account is still somebody using the app: they browse,
        search, plan and save. Only their identity differs."""
        auth, _ = account(client)
        make_business(client, auth)

        assert client.get("/api/v1/me", headers=auth).status_code == 200
        assert client.get("/api/v1/me/saved", headers=auth).status_code == 200
        assert client.get("/api/v1/collections/mine", headers=auth).status_code == 200
        assert client.get("/api/v1/itineraries", headers=auth).status_code == 200

    def test_an_unknown_business_type_is_dropped_not_rejected(self, client):
        """It is a label. Losing the whole submission over it is a bad trade."""
        auth, _ = account(client)
        business = make_business(client, auth, businessType="teleportation-hub")
        assert business["businessType"] is None

    def test_the_owner_can_edit_it(self, client):
        auth, _ = account(client)
        business = make_business(client, auth)
        response = client.patch(
            f"/api/v1/businesses/{business['id']}",
            headers=auth,
            json={"description": "Now with a pool.", "website": "https://example.org"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["description"] == "Now with a pool."

    def test_javascript_urls_are_not_stored_as_social_links(self, client):
        """These render as anchors. A javascript: URL here is stored XSS."""
        auth, _ = account(client)
        business = make_business(
            client,
            auth,
            social={"instagram": "javascript:alert(1)", "facebook": "https://fb.example/x"},
        )
        assert "instagram" not in business["social"]
        assert business["social"]["facebook"] == "https://fb.example/x"


# --- SECURITY: one business must never reach another --------------------------


class TestBusinessIsolation:
    def test_a_stranger_cannot_read_a_business(self, client):
        """404 rather than 403: confirming it exists is itself a disclosure."""
        owner, _ = account(client, "owner")
        stranger, _ = account(client, "stranger")
        business = make_business(client, owner)

        response = client.get(f"/api/v1/businesses/{business['id']}", headers=stranger)
        assert response.status_code == 404, response.text

    def test_a_stranger_cannot_edit_a_business(self, client):
        owner, _ = account(client, "owner")
        stranger, _ = account(client, "stranger")
        business = make_business(client, owner)

        response = client.patch(
            f"/api/v1/businesses/{business['id']}",
            headers=stranger,
            json={"name": "Mine now"},
        )
        assert response.status_code == 404, response.text

    def test_a_stranger_cannot_list_the_team(self, client):
        owner, _ = account(client, "owner")
        stranger, _ = account(client, "stranger")
        business = make_business(client, owner)

        response = client.get(
            f"/api/v1/businesses/{business['id']}/members", headers=stranger
        )
        assert response.status_code == 404, response.text

    def test_an_admin_of_one_business_cannot_touch_another(self, client):
        """The cross-business attack: hold real permissions somewhere, then pass
        a member id belonging to somewhere else."""
        alice, _ = account(client, "alice")
        bob, bob_email = account(client, "bob")
        mine = make_business(client, alice)
        theirs = make_business(client, bob)

        # Bob is a genuine administrator of Alice's business.
        member = invite(client, alice, mine["id"], bob_email, "admin")
        assert accept(client, bob, member["id"]).status_code == 200

        # He may not use that to reach his own business through Alice's path,
        # nor Alice's business through his own.
        response = client.get(f"/api/v1/businesses/{theirs['id']}/members", headers=alice)
        assert response.status_code == 404

        # Nor remove a member of a business the member does not belong to.
        response = client.delete(
            f"/api/v1/businesses/{theirs['id']}/members/{member['id']}", headers=bob
        )
        assert response.status_code == 404, response.text


# --- TEAM-001..014 -------------------------------------------------------------


class TestTeamLifecycle:
    def test_owner_invites_and_member_accepts(self, client):
        owner, _ = account(client, "owner")
        staff, staff_email = account(client, "staff")
        business = make_business(client, owner)

        member = invite(client, owner, business["id"], staff_email, "editor")
        assert member["status"] == "invited"
        assert member["roleLabel"] == "Editor"

        pending = client.get("/api/v1/me/business-invitations", headers=staff)
        assert pending.status_code == 200
        assert [i["id"] for i in pending.json()["data"]] == [member["id"]]

        assert accept(client, staff, member["id"]).status_code == 200
        listed = client.get(f"/api/v1/businesses/{business['id']}/members", headers=owner)
        assert [m["status"] for m in listed.json()["data"]] == ["active"]

    def test_an_invitation_cannot_be_accepted_by_somebody_else(self, client):
        """A membership grants access to a stranger's business. A link anybody
        who receives it can redeem is a link that gets forwarded."""
        owner, _ = account(client, "owner")
        _, staff_email = account(client, "staff")
        interloper, _ = account(client, "interloper")
        business = make_business(client, owner)

        member = invite(client, owner, business["id"], staff_email, "editor")
        assert accept(client, interloper, member["id"]).status_code == 404

    def test_a_member_declines(self, client):
        owner, _ = account(client, "owner")
        staff, staff_email = account(client, "staff")
        business = make_business(client, owner)

        member = invite(client, owner, business["id"], staff_email, "editor")
        response = client.post(
            f"/api/v1/me/business-invitations/{member['id']}/decline", headers=staff
        )
        assert response.status_code == 204, response.text
        assert client.get("/api/v1/me/business-invitations", headers=staff).json()["data"] == []

    def test_owner_changes_a_role(self, client):
        owner, _ = account(client, "owner")
        staff, staff_email = account(client, "staff")
        business = make_business(client, owner)
        member = invite(client, owner, business["id"], staff_email, "analyst")
        accept(client, staff, member["id"])

        response = client.patch(
            f"/api/v1/businesses/{business['id']}/members/{member['id']}",
            headers=owner,
            json={"role": "editor"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["role"] == "editor"

    def test_owner_removes_a_member_and_access_stops(self, client):
        owner, _ = account(client, "owner")
        staff, staff_email = account(client, "staff")
        business = make_business(client, owner)
        member = invite(client, owner, business["id"], staff_email, "admin")
        accept(client, staff, member["id"])

        assert client.get(f"/api/v1/businesses/{business['id']}", headers=staff).status_code == 200

        removed = client.delete(
            f"/api/v1/businesses/{business['id']}/members/{member['id']}", headers=owner
        )
        assert removed.status_code == 204, removed.text

        # The revocation is the point. If this still returns 200, membership is
        # being read from somewhere that ignores status.
        assert client.get(f"/api/v1/businesses/{business['id']}", headers=staff).status_code == 404

    def test_re_inviting_reuses_the_row(self, client):
        """Two grants for one person is one grant too many to revoke."""
        owner, _ = account(client, "owner")
        staff, staff_email = account(client, "staff")
        business = make_business(client, owner)

        first = invite(client, owner, business["id"], staff_email, "analyst")
        client.post(f"/api/v1/me/business-invitations/{first['id']}/decline", headers=staff)
        second = invite(client, owner, business["id"], staff_email, "editor")

        assert second["id"] == first["id"]
        assert second["role"] == "editor"

    def test_inviting_an_active_member_again_conflicts(self, client):
        owner, _ = account(client, "owner")
        staff, staff_email = account(client, "staff")
        business = make_business(client, owner)
        member = invite(client, owner, business["id"], staff_email, "editor")
        accept(client, staff, member["id"])

        response = client.post(
            f"/api/v1/businesses/{business['id']}/members",
            headers=owner,
            json={"email": staff_email, "role": "admin"},
        )
        assert response.status_code == 409, response.text

    def test_the_owner_cannot_be_invited_to_their_own_business(self, client):
        owner, owner_email = account(client, "owner")
        business = make_business(client, owner)
        response = client.post(
            f"/api/v1/businesses/{business['id']}/members",
            headers=owner,
            json={"email": owner_email, "role": "admin"},
        )
        assert response.status_code == 422, response.text


# --- TEAM-010/012: the permission boundary ------------------------------------


class TestRolesAreEnforced:
    def _member_with(self, client, role):
        owner, _ = account(client, "owner")
        staff, staff_email = account(client, "staff")
        business = make_business(client, owner)
        member = invite(client, owner, business["id"], staff_email, role)
        accept(client, staff, member["id"])
        return owner, staff, business, member

    def test_an_analyst_cannot_edit_the_profile(self, client):
        _, staff, business, _ = self._member_with(client, "analyst")
        response = client.patch(
            f"/api/v1/businesses/{business['id']}", headers=staff, json={"name": "Renamed"}
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "INSUFFICIENT_BUSINESS_PERMISSION"

    def test_an_editor_cannot_manage_the_team(self, client):
        _, staff, business, _ = self._member_with(client, "editor")
        response = client.get(
            f"/api/v1/businesses/{business['id']}/members", headers=staff
        )
        assert response.status_code == 403, response.text

    def test_an_admin_can_manage_the_team(self, client):
        """The counterpart, so the test above is not passing because everything
        is forbidden."""
        _, staff, business, _ = self._member_with(client, "admin")
        assert (
            client.get(f"/api/v1/businesses/{business['id']}/members", headers=staff).status_code
            == 200
        )

    def test_nobody_can_promote_themselves(self, client):
        """An admin holds team:manage, so without the self-check they could grant
        themselves anything the table allows."""
        _, staff, business, member = self._member_with(client, "admin")
        response = client.patch(
            f"/api/v1/businesses/{business['id']}/members/{member['id']}",
            headers=staff,
            json={"role": "admin"},
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "CANNOT_CHANGE_OWN_ROLE"

    def test_owner_is_not_an_assignable_role(self, client):
        """Ownership is a property of the business, not a role to hand out."""
        owner, _ = account(client, "owner")
        _, staff_email = account(client, "staff")
        business = make_business(client, owner)
        response = client.post(
            f"/api/v1/businesses/{business['id']}/members",
            headers=owner,
            json={"email": staff_email, "role": "owner"},
        )
        assert response.status_code == 422, response.text

    def test_the_advertised_roles_are_exactly_the_assignable_ones(self, client):
        response = client.get("/api/v1/businesses/roles")
        assert response.status_code == 200
        values = {role["value"] for role in response.json()["data"]}
        assert "owner" not in values
        assert values == {"admin", "editor", "event_manager", "analyst"}

    def test_permissions_endpoint_matches_the_role(self, client):
        _, staff, business, _ = self._member_with(client, "analyst")
        response = client.get(
            f"/api/v1/businesses/{business['id']}/permissions", headers=staff
        )
        assert response.status_code == 200
        assert set(response.json()["data"]) == {"profile:view", "analytics:view"}

    def test_a_role_that_can_publish_can_also_set_a_date(self, client):
        """The invariant behind the editor bug, stated once.

        Publishing an event requires a date, so any role allowed to publish must
        be allowed to add one. A role holding `content:publish` without
        `events:manage` looks fine field-by-field and cannot complete the job.
        """
        from app.domains.publisher import permissions

        for role, held in permissions.ROLE_PERMISSIONS.items():
            if permissions.CONTENT_PUBLISH in held:
                assert permissions.EVENTS_MANAGE in held, (
                    f"'{role}' can publish but cannot add a date, so it cannot "
                    "publish an event at all"
                )


# --- BUS-006: posting as the business, and EXP-003: the public page -----------


class TestPublishingAsABusiness:
    def test_an_editor_can_post_for_the_business(self, client):
        owner, _ = account(client, "owner")
        staff, staff_email = account(client, "staff")
        business = make_business(client, owner)
        member = invite(client, owner, business["id"], staff_email, "editor")
        accept(client, staff, member["id"])

        response = client.post(
            "/api/v1/posts",
            headers=staff,
            json={
                "title": f"Business post {uuid.uuid4().hex[:6]}",
                "description": "Written by an editor on behalf of the business.",
                "citySlug": "addis-ababa",
                "type": "place",
                "publisherId": business["id"],
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["data"]["publisher"]["id"] == business["id"]

    def test_an_editor_can_take_an_event_all_the_way_to_published(self, client):
        """Caught by the first end-to-end run.

        An editor held `content:publish` but not `events:manage`, so they could
        write an event and then not add a date to it - and publishing refuses
        without one. Every individual permission check passed; the role was
        simply not a workable set. Only walking the whole flow finds that.
        """
        owner, _ = account(client, "owner")
        staff, staff_email = account(client, "staff")
        business = make_business(client, owner)
        member = invite(client, owner, business["id"], staff_email, "editor")
        accept(client, staff, member["id"])

        venue = client.post(
            "/api/v1/posts/venues",
            headers=staff,
            json={
                "name": f"Terrace {uuid.uuid4().hex[:5]}",
                "address": "Bole Road, Addis Ababa",
                "citySlug": "addis-ababa",
                "latitude": 9.0055,
                "longitude": 38.7810,
            },
        )
        assert venue.status_code == 201, venue.text

        post = client.post(
            "/api/v1/posts",
            headers=staff,
            json={
                "title": f"Editor's event {uuid.uuid4().hex[:6]}",
                "description": "An event created and published by an editor.",
                "citySlug": "addis-ababa",
                "type": "event",
                "categorySlug": "music",
                "venueId": venue.json()["data"]["id"],
                "publisherId": business["id"],
            },
        )
        assert post.status_code == 201, post.text
        post_id = post.json()["data"]["id"]

        when = (datetime.now(UTC) + timedelta(days=3)).isoformat()
        dated = client.post(
            f"/api/v1/posts/{post_id}/events", headers=staff, json={"startTime": when}
        )
        assert dated.status_code == 201, dated.text

        published = client.post(f"/api/v1/posts/{post_id}/publish", headers=staff)
        assert published.status_code == 200, published.text
        assert published.json()["data"]["status"] == "published"

    def test_an_analyst_cannot_post_for_the_business(self, client):
        owner, _ = account(client, "owner")
        staff, staff_email = account(client, "staff")
        business = make_business(client, owner)
        member = invite(client, owner, business["id"], staff_email, "analyst")
        accept(client, staff, member["id"])

        response = client.post(
            "/api/v1/posts",
            headers=staff,
            json={
                "title": f"Should not exist {uuid.uuid4().hex[:6]}",
                "description": "An analyst was given sight of the numbers, not a pen.",
                "citySlug": "addis-ababa",
                "type": "place",
                "publisherId": business["id"],
            },
        )
        assert response.status_code == 403, response.text

    def test_a_stranger_cannot_post_as_the_business(self, client):
        owner, _ = account(client, "owner")
        stranger, _ = account(client, "stranger")
        business = make_business(client, owner)

        response = client.post(
            "/api/v1/posts",
            headers=stranger,
            json={
                "title": f"Impersonation {uuid.uuid4().hex[:6]}",
                "description": "Posting under somebody else's name.",
                "citySlug": "addis-ababa",
                "type": "place",
                "publisherId": business["id"],
            },
        )
        assert response.status_code == 403, response.text

    def test_the_public_page_shows_only_published_listings(self, client):
        owner, _ = account(client, "owner")
        business = make_business(client, owner)

        draft = client.post(
            "/api/v1/posts",
            headers=owner,
            json={
                "title": f"Still a draft {uuid.uuid4().hex[:6]}",
                "description": "Nothing here should be discoverable yet.",
                "citySlug": "addis-ababa",
                "type": "place",
                "publisherId": business["id"],
            },
        )
        assert draft.status_code == 201, draft.text

        page = client.get(f"/api/v1/businesses/by-slug/{business['slug']}")
        assert page.status_code == 200, page.text
        body = page.json()["data"]
        assert body["name"] == business["name"]
        assert body["listings"] == []

    def test_a_personal_publisher_has_no_business_page(self, client):
        """Otherwise every explorer silently acquires a business profile."""
        auth, _ = account(client)
        personal = client.get("/api/v1/posts/me", headers=auth).json()["data"]
        response = client.get(f"/api/v1/businesses/by-slug/{personal['slug']}")
        assert response.status_code == 404, response.text
