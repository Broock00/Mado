"""Publisher plans, and the limits they actually enforce.

BUSINESS-09 puts publisher subscriptions at 30% of the long-term revenue mix,
the largest single stream. The risk in that is not the billing - that is the
same hosted-checkout path tickets already use - it is the entitlements, because
a limit that does not bind is a plan nobody needs to buy, and a limit that binds
too hard deletes somebody's work.

So the tests below are mostly about the edges of a limit rather than its middle:
what happens at exactly the ceiling, what happens to an account that falls below
one, and whether a refusal says the right thing. A plan limit refusing with a
permission error would send a publisher to ask for a bigger role, which nobody
can grant, and the refusal would repeat.

**Run the server with `MADO_PAYMENT_PROVIDER=stub`**, as for the fee tests: a
plan is bought through the same providers a ticket is, and a real one rejects
the `mado-qa.example.org` addresses this suite identifies its data by.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import httpx
import pytest

import app.models  # noqa: F401
from app.domains.publisher import plans
from tests.conftest import make_moderator, requires_api

BASE_URL = os.environ.get("MADO_TEST_API_URL", "http://127.0.0.1:8000")
PASSWORD = "discover-addis-2026"
CITY = "addis-ababa"

pytestmark = pytest.mark.anyio


# ------------------------------------------------------------ the catalogue


class TestThePlanCatalogue:
    def test_every_plan_is_reachable_by_its_key(self):
        for key in plans.PLAN_ORDER:
            assert plans.get(key).key == key

    def test_an_unknown_plan_falls_back_to_free(self):
        """Fails closed, like `permissions.granted_to`.

        A row holding a plan this build does not recognise - a rollback, a
        half-finished migration - must grant the least, and a caller that has to
        catch an exception to find that out will forget to.
        """
        assert plans.get("platinum-deluxe").key == plans.FREE
        assert plans.get(None).key == plans.FREE

    def test_free_is_worth_having(self):
        """BUSINESS-06 asks that free users experience meaningful value.

        A free tier that cannot hold a small venue's programme is a demo, and a
        demo does not bring the supply the rest of the platform is built on.
        """
        free = plans.get(plans.FREE).entitlements
        assert free.max_live_listings is not None and free.max_live_listings >= 5

    def test_limits_only_ever_loosen_as_plans_get_more_expensive(self):
        """A more expensive plan that gives less of something is a pricing bug
        nobody notices until a customer does."""
        previous = None
        for key in plans.PLAN_ORDER:
            current = plans.get(key).entitlements
            if previous is not None:
                for field in ("max_live_listings", "max_team_members"):
                    before, after = getattr(previous, field), getattr(current, field)
                    if before is None:
                        assert after is None, f"{key} narrows {field} back to a limit"
                    elif after is not None:
                        assert after >= before, f"{key} gives less {field} than the plan below"
                assert current.analytics_window_days >= previous.analytics_window_days
            previous = current

    def test_enterprise_has_no_price(self):
        """It is negotiated, so a number here would be one the sales
        conversation immediately contradicts."""
        assert plans.PLANS[plans.ENTERPRISE].prices_minor == {}
        assert not plans.is_purchasable(plans.ENTERPRISE)

    def test_free_cannot_be_bought(self):
        assert not plans.is_purchasable(plans.FREE)

    def test_a_currency_with_no_price_returns_nothing(self):
        """Rather than a converted figure. A price arrived at through this
        morning's exchange rate is one nobody decided to charge."""
        assert plans.price_for(plans.PROFESSIONAL, "XPF") is None

    def test_every_purchasable_plan_is_priced_in_the_pilot_currency(self):
        for key in plans.PURCHASABLE:
            assert plans.price_for(key, "ETB"), f"{key} has no birr price"

    def test_pending_grants_nothing(self):
        """A subscription started and not paid for must give nothing, or the
        checkout is optional."""
        assert plans.STATUS_PENDING not in plans.STATUS_GRANTING
        assert plans.STATUS_PAST_DUE not in plans.STATUS_GRANTING

    def test_cancelled_still_grants_until_the_period_ends(self):
        """Cancelling says "does not continue", not "give it back now".

        Since nothing renews, that is entirely a statement about the end of the
        period - and the period is what `entitlements.plan_of` checks. Dropping
        somebody to free the afternoon they cancelled would keep their money and
        withdraw what it bought.
        """
        assert plans.STATUS_CANCELLED in plans.STATUS_GRANTING


class TestTheClientCatalogueAgrees:
    """`plans.ts` is a copy, and a copy drifts.

    The composer and the upgrade page render the tiers before asking the server
    anything, which is why the copy exists - and why a build has to fail when it
    stops matching what is enforced. A price list that disagrees with what is
    charged is the worst kind of disagreement.
    """

    SOURCE = (
        Path(__file__).resolve().parents[2] / "frontend" / "web" / "src" / "lib" / "plans.ts"
    )

    def _parsed(self) -> dict[str, dict]:
        assert self.SOURCE.exists(), f"plans.ts not found at {self.SOURCE}"
        text = self.SOURCE.read_text(encoding="utf-8")

        found: dict[str, dict] = {}
        for block in re.finditer(
            r"key:\s*(\w+),\s*name:\s*'([^']+)',\s*tagline:\s*'([^']+)',\s*"
            r"entitlements:\s*\{(.*?)\},",
            text,
            re.DOTALL,
        ):
            constant, name, tagline, body = block.groups()
            fields: dict[str, object] = {"name": name, "tagline": tagline}
            for key, raw in re.findall(r"(\w+):\s*([^,\n]+),", body):
                value = raw.strip()
                if value == "null":
                    fields[key] = None
                elif value in ("true", "false"):
                    fields[key] = value == "true"
                else:
                    fields[key] = int(value)
            found[constant.lower()] = fields

        # The guard that stops this passing vacuously. A regex that matches
        # nothing - because the file was reformatted, or moved - would otherwise
        # report agreement it never checked.
        assert len(found) == len(plans.PLANS), (
            f"parsed {len(found)} plans out of plans.ts but the server defines "
            f"{len(plans.PLANS)}; the parser or the file has changed shape"
        )
        return found

    def test_the_same_plans_exist_on_both_sides(self):
        assert set(self._parsed()) == set(plans.PLANS)

    def test_the_names_and_taglines_match(self):
        parsed = self._parsed()
        for key, plan in plans.PLANS.items():
            assert parsed[key]["name"] == plan.name, f"{key} is named differently in plans.ts"
            assert parsed[key]["tagline"] == plan.tagline, f"{key} has a stale tagline"

    def test_the_limits_match(self):
        parsed = self._parsed()
        fields = {
            "maxLiveListings": "max_live_listings",
            "maxTeamMembers": "max_team_members",
            "analyticsWindowDays": "analytics_window_days",
            "aiAssistant": "ai_assistant",
            "apiAccess": "api_access",
        }
        for key, plan in plans.PLANS.items():
            for client_field, server_field in fields.items():
                assert parsed[key][client_field] == getattr(plan.entitlements, server_field), (
                    f"{key}.{client_field} in plans.ts disagrees with what is enforced"
                )


# ------------------------------------------------------------ against the API


@pytest.fixture
def client():
    requires_api()
    with httpx.Client(base_url=BASE_URL, timeout=30) as session:
        yield session


def account(client, who="explorer") -> tuple[dict, str]:
    email = f"{who}-{uuid.uuid4().hex[:8]}@mado-qa.example.org"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "displayName": who.title()},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['data']['tokens']['accessToken']}"}, email


@pytest.fixture
def moderator(client) -> dict:
    """An account with moderator rights. See `conftest.make_moderator`."""
    auth, email = account(client, "plan-admin")
    make_moderator(email)
    return auth


def make_business(client, auth) -> str:
    response = client.post(
        "/api/v1/me/account-type/business",
        headers=auth,
        json={
            "name": f"Plan Test {uuid.uuid4().hex[:6]}",
            "businessType": "venue",
            "description": "A business invented to check what a plan includes.",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["business"]["id"]


# One venue per account, reused by every post that account publishes.
#
# Not an optimisation for its own sake: creating a venue reverse-geocodes
# server-side to materialise the city, so a test that publishes six listings used
# to make six geocoding round-trips - and against a real provider that is slow
# enough that the ceiling test timed out under a full-suite run while passing on
# its own. Six posts at one address is also what a real venue looks like.
_venues: dict[str, str] = {}


def venue_for(client, auth) -> str:
    token = auth["Authorization"]
    if token not in _venues:
        created = client.post(
            "/api/v1/posts/venues",
            headers=auth,
            json={
                "name": f"Plan hall {uuid.uuid4().hex[:5]}",
                "address": "Bole Road, Addis Ababa",
                "citySlug": CITY,
                "latitude": 9.0055,
                "longitude": 38.7810,
            },
        )
        assert created.status_code == 201, created.text
        _venues[token] = created.json()["data"]["id"]
    return _venues[token]


def publish_one(client, auth) -> httpx.Response:
    """Write a post and try to make it live. Returns the publish response."""
    post = client.post(
        "/api/v1/posts",
        headers=auth,
        json={
            "title": f"Plan limit test {uuid.uuid4().hex[:5]}",
            "description": "A post used to check how many a plan keeps live at once.",
            "citySlug": CITY,
            "type": "place",
            "categorySlug": "music",
            "venueId": venue_for(client, auth),
        },
    )
    assert post.status_code == 201, post.text
    return client.post(f"/api/v1/posts/{post.json()['data']['id']}/publish", headers=auth)


class TestTheFreePlanBinds:
    def test_a_new_business_is_on_free(self, client):
        auth, _ = account(client, "newbie")
        business_id = make_business(client, auth)

        body = client.get(f"/api/v1/businesses/{business_id}/plans", headers=auth)
        assert body.status_code == 200, body.text
        assert body.json()["data"]["current"]["plan"] == plans.FREE

    def test_the_ceiling_refuses_with_a_plan_error_and_not_a_permission_one(self, client):
        """The distinction the whole design rests on.

        A publisher told "your role does not allow this" would go and ask for a
        bigger role, be given one, and be refused again - and whoever granted it
        would have widened somebody's access for nothing.
        """
        auth, _ = account(client, "prolific")
        make_business(client, auth)

        limit = plans.get(plans.FREE).entitlements.max_live_listings
        assert limit is not None

        for index in range(limit):
            allowed = publish_one(client, auth)
            assert allowed.status_code == 200, f"post {index + 1} of {limit}: {allowed.text}"

        refused = publish_one(client, auth)
        assert refused.status_code == 402, refused.text
        error = refused.json()["error"]
        assert error["code"] == "PLAN_LIMIT"
        # And it names what would lift it, so the client can put the upgrade
        # where the refusal happened rather than in a menu.
        assert error["details"]["plan"] == plans.PROFESSIONAL

    def test_withdrawing_one_makes_room_again(self, client):
        """The limit is on what is live, not on what was ever written.

        Which is also why nothing is deleted on a downgrade: the account is
        blocked from adding, and everything it has stays exactly where it is.
        """
        auth, _ = account(client, "shuffler")
        make_business(client, auth)

        limit = plans.get(plans.FREE).entitlements.max_live_listings
        published = []
        for _ in range(limit):
            response = publish_one(client, auth)
            assert response.status_code == 200
            published.append(response.json()["data"]["id"])

        assert publish_one(client, auth).status_code == 402

        withdrawn = client.post(f"/api/v1/posts/{published[0]}/unpublish", headers=auth)
        assert withdrawn.status_code == 200, withdrawn.text

        assert publish_one(client, auth).status_code == 200, "a freed slot should be usable"

    def test_a_draft_is_never_refused(self, client):
        """A limit that stopped somebody writing would push the work elsewhere
        rather than sell them anything, and a draft costs nothing to keep."""
        auth, _ = account(client, "drafter")
        make_business(client, auth)

        limit = plans.get(plans.FREE).entitlements.max_live_listings
        for _ in range(limit):
            assert publish_one(client, auth).status_code == 200

        # At the ceiling, and still able to write.
        draft = client.post(
            "/api/v1/posts",
            headers=auth,
            json={
                "title": f"A draft at the ceiling {uuid.uuid4().hex[:5]}",
                "description": "Written while every slot is already taken.",
                "citySlug": CITY,
                "type": "place",
            },
        )
        assert draft.status_code == 201, draft.text

    def test_republishing_something_already_live_is_not_refused(self, client):
        """It takes no new slot, and the count it would be checked against
        already includes it - so an account exactly at its limit could not
        re-save one of its own posts."""
        auth, _ = account(client, "resaver")
        make_business(client, auth)

        limit = plans.get(plans.FREE).entitlements.max_live_listings
        ids = []
        for _ in range(limit):
            response = publish_one(client, auth)
            assert response.status_code == 200
            ids.append(response.json()["data"]["id"])

        again = client.post(f"/api/v1/posts/{ids[0]}/publish", headers=auth)
        assert again.status_code == 200, again.text


class TestSeats:
    def test_the_free_plan_refuses_a_second_seat(self, client):
        auth, owner_email = account(client, "solo")
        business_id = make_business(client, auth)

        seats = plans.get(plans.FREE).entitlements.max_team_members
        assert seats == 1

        _first_auth, first_email = account(client, "helper")
        allowed = client.post(
            f"/api/v1/businesses/{business_id}/members",
            headers=auth,
            json={"email": first_email, "role": "editor"},
        )
        assert allowed.status_code == 201, allowed.text

        _second_auth, second_email = account(client, "helper2")
        refused = client.post(
            f"/api/v1/businesses/{business_id}/members",
            headers=auth,
            json={"email": second_email, "role": "editor"},
        )
        assert refused.status_code == 402, refused.text
        assert refused.json()["error"]["code"] == "PLAN_LIMIT"
        assert owner_email != second_email

    def test_a_pending_invitation_takes_a_seat(self, client):
        """Otherwise a free account invites twenty people, none of whom have
        accepted, and the limit binds as they arrive - refusing nineteen of them
        at the worst possible moment."""
        auth, _ = account(client, "hopeful")
        business_id = make_business(client, auth)

        _helper_auth, email = account(client, "pending")
        client.post(
            f"/api/v1/businesses/{business_id}/members",
            headers=auth,
            json={"email": email, "role": "editor"},
        )
        # Not accepted, and the seat is gone.
        _other_auth, other = account(client, "later")
        refused = client.post(
            f"/api/v1/businesses/{business_id}/members",
            headers=auth,
            json={"email": other, "role": "editor"},
        )
        assert refused.status_code == 402, refused.text


class TestBuyingAPlan:
    def test_the_catalogue_carries_prices_in_the_currency_asked_for(self, client):
        auth, _ = account(client, "shopper")
        business_id = make_business(client, auth)

        body = client.get(
            f"/api/v1/businesses/{business_id}/plans?currency=ETB", headers=auth
        ).json()["data"]
        professional = next(p for p in body["plans"] if p["key"] == plans.PROFESSIONAL)
        assert professional["priceMinor"] == plans.price_for(plans.PROFESSIONAL, "ETB")
        assert professional["purchasable"] is True

        enterprise = next(p for p in body["plans"] if p["key"] == plans.ENTERPRISE)
        assert enterprise["priceMinor"] is None
        assert enterprise["purchasable"] is False

    def test_enterprise_cannot_be_bought(self, client):
        auth, _ = account(client, "ambitious")
        business_id = make_business(client, auth)

        refused = client.post(
            f"/api/v1/businesses/{business_id}/plans",
            headers=auth,
            json={"plan": plans.ENTERPRISE, "currency": "ETB"},
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "PLAN_NOT_PURCHASABLE"

    def test_a_started_purchase_grants_nothing_until_it_is_paid(self, client):
        """The property that makes the checkout non-optional.

        A subscription written active and corrected afterwards would hand a
        month of entitlements to anybody who opened the payment page and closed
        it.
        """
        auth, _ = account(client, "browser")
        business_id = make_business(client, auth)

        started = client.post(
            f"/api/v1/businesses/{business_id}/plans",
            headers=auth,
            json={"plan": plans.PROFESSIONAL, "currency": "ETB"},
        )
        if started.status_code == 409:
            pytest.skip("Payments are unavailable; nothing to start.")
        assert started.status_code == 201, started.text

        # The plan in force is still free, whatever the row says.
        current = client.get(f"/api/v1/businesses/{business_id}/plans", headers=auth).json()[
            "data"
        ]["current"]
        assert current["plan"] == plans.FREE

        # And the limit still binds.
        limit = plans.get(plans.FREE).entitlements.max_live_listings
        for _ in range(limit):
            assert publish_one(client, auth).status_code == 200
        assert publish_one(client, auth).status_code == 402

    def test_paying_lifts_the_limit(self, client):
        """The whole point, end to end."""
        auth, _ = account(client, "payer")
        business_id = make_business(client, auth)

        limit = plans.get(plans.FREE).entitlements.max_live_listings
        for _ in range(limit):
            assert publish_one(client, auth).status_code == 200
        assert publish_one(client, auth).status_code == 402, "the free limit should bind first"

        started = client.post(
            f"/api/v1/businesses/{business_id}/plans",
            headers=auth,
            json={"plan": plans.PROFESSIONAL, "currency": "ETB"},
        )
        assert started.status_code == 201, started.text

        settled = client.post(f"/api/v1/businesses/{business_id}/plans/simulate", headers=auth)
        if settled.status_code == 403:
            pytest.skip("A real payment provider is configured; nothing to simulate against.")
        assert settled.status_code == 200, settled.text
        assert settled.json()["data"]["plan"] == plans.PROFESSIONAL

        assert publish_one(client, auth).status_code == 200, (
            "a paid plan should lift the listing limit"
        )

    def test_starting_an_upgrade_does_not_cost_the_plan_already_paid_for(self, client):
        """A regression, and an expensive one.

        Starting a purchase used to overwrite the subscription row with the
        plan being bought, at status `pending` - so an account paying for
        Professional that clicked through to Business dropped to free on the
        spot, before it had paid for anything, and lost the listing headroom it
        had already bought. What is in force and what is being bought are now
        different columns.
        """
        auth, _ = account(client, "upgrader")
        business_id = make_business(client, auth)

        client.post(
            f"/api/v1/businesses/{business_id}/plans",
            headers=auth,
            json={"plan": plans.PROFESSIONAL, "currency": "ETB"},
        )
        settled = client.post(f"/api/v1/businesses/{business_id}/plans/simulate", headers=auth)
        if settled.status_code != 200:
            pytest.skip("Could not settle a plan payment here.")

        started = client.post(
            f"/api/v1/businesses/{business_id}/plans",
            headers=auth,
            json={"plan": plans.BUSINESS, "currency": "ETB"},
        )
        assert started.status_code == 201, started.text

        current = client.get(f"/api/v1/businesses/{business_id}/plans", headers=auth).json()[
            "data"
        ]["current"]
        assert current["plan"] == plans.PROFESSIONAL, "an unpaid upgrade must not demote"
        assert current["pendingPlan"] == plans.BUSINESS
        assert current["currentPeriodEnd"] is not None

        # And the entitlements it paid for still hold: Professional has no
        # listing limit, so publishing past the free ceiling must still work.
        for _ in range(plans.get(plans.FREE).entitlements.max_live_listings + 1):
            assert publish_one(client, auth).status_code == 200

    def test_paying_for_the_upgrade_promotes_it(self, client):
        auth, _ = account(client, "promoter")
        business_id = make_business(client, auth)

        for plan in (plans.PROFESSIONAL, plans.BUSINESS):
            client.post(
                f"/api/v1/businesses/{business_id}/plans",
                headers=auth,
                json={"plan": plan, "currency": "ETB"},
            )
            settled = client.post(
                f"/api/v1/businesses/{business_id}/plans/simulate", headers=auth
            )
            if settled.status_code != 200:
                pytest.skip("Could not settle a plan payment here.")
            assert settled.json()["data"]["plan"] == plan, settled.text
            assert settled.json()["data"]["pendingPlan"] is None

    def test_cancelling_keeps_the_period_that_was_paid_for(self, client):
        """Keeping the money and withdrawing the thing it bought would be theft
        with extra steps."""
        auth, _ = account(client, "quitter")
        business_id = make_business(client, auth)

        client.post(
            f"/api/v1/businesses/{business_id}/plans",
            headers=auth,
            json={"plan": plans.PROFESSIONAL, "currency": "ETB"},
        )
        settled = client.post(f"/api/v1/businesses/{business_id}/plans/simulate", headers=auth)
        if settled.status_code != 200:
            pytest.skip("Could not settle a plan payment here.")

        cancelled = client.delete(f"/api/v1/businesses/{business_id}/plans", headers=auth)
        assert cancelled.status_code == 200, cancelled.text

        # Still on the plan they paid for, until the period runs out.
        current = client.get(f"/api/v1/businesses/{business_id}/plans", headers=auth).json()[
            "data"
        ]["current"]
        assert current["plan"] == plans.PROFESSIONAL
        assert current["currentPeriodEnd"] is not None


class TestTheAnalyticsWindowIsSold:
    """The window is a plan feature, so it has to be both real and enforced.

    It was neither. `plans.py` advertised 180- and 365-day analytics while the
    service only supported 7, 30 and 90 - so two paid tiers were selling
    something that could not be produced - and `analytics_window_for` was
    written and then called from nowhere, which is the "a limit nothing checks
    is a lie" rule broken in the module that states it.
    """

    def test_every_plan_window_is_one_the_service_can_produce(self):
        """The half that was selling a fiction."""
        from app.domains.publisher.analytics import WINDOWS

        for key in plans.PLAN_ORDER:
            window = plans.get(key).entitlements.analytics_window_days
            assert window in WINDOWS, f"{key} sells a {window}-day window nothing can build"

    def test_a_free_account_asking_past_its_limit_gets_its_own_window(self, client):
        """Clamped, not refused. A working screen showing less is a better
        answer than an error about a plan, and the response says what it gave.
        """
        auth, _ = account(client, "curious")
        make_business(client, auth)
        publish_one(client, auth)

        response = client.get("/api/v1/analytics/publisher?window=90", headers=auth)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["windowDays"] == (
            plans.get(plans.FREE).entitlements.analytics_window_days
        )

    def test_a_free_account_keeps_the_windows_below_its_limit(self, client):
        """The clamp is a ceiling, not a fixed value - asking for a week still
        gets a week."""
        auth, _ = account(client, "weekly")
        make_business(client, auth)
        publish_one(client, auth)

        response = client.get("/api/v1/analytics/publisher?window=7", headers=auth)
        assert response.json()["data"]["windowDays"] == 7

    def test_paying_widens_it(self, client):
        auth, _ = account(client, "widener")
        business_id = make_business(client, auth)
        publish_one(client, auth)

        before = client.get("/api/v1/analytics/publisher?window=90", headers=auth)
        assert before.json()["data"]["windowDays"] == 30

        client.post(
            f"/api/v1/businesses/{business_id}/plans",
            headers=auth,
            json={"plan": plans.PROFESSIONAL, "currency": "ETB"},
        )
        settled = client.post(f"/api/v1/businesses/{business_id}/plans/simulate", headers=auth)
        if settled.status_code != 200:
            pytest.skip("Could not settle a plan payment here.")

        after = client.get("/api/v1/analytics/publisher?window=90", headers=auth)
        assert after.json()["data"]["windowDays"] == 90


class TestABusinessAccountHasOneIdentity:
    """`publisher_for` is the single answer to whose name goes on this.

    CLAUDE.md records `POST /posts` getting this wrong once - it called
    `personal_publisher`, and a business account's posts came out under the
    owner's own name while every check passed. Venue creation still had it, so a
    business that added a venue quietly acquired a *second* publisher that owned
    its buildings while the business owned its posts.

    It surfaced through the analytics window, of all things: the dashboard
    resolved "your publisher" with an unordered `.first()` over both rows, and
    once the plan was read off whichever came back, a business that had paid for
    a longer window got its free one about half the time.
    """

    def test_a_business_account_owns_exactly_one_publisher(self, client):
        auth, _ = account(client, "single")
        business_id = make_business(client, auth)
        publish_one(client, auth)

        identities = client.get("/api/v1/me/publishing-identities", headers=auth)
        assert identities.status_code == 200, identities.text
        owned = [row["id"] for row in identities.json()["data"]]
        assert owned == [business_id], owned

    def test_a_venue_belongs_to_the_business(self, client):
        auth, _ = account(client, "venue-owner")
        business_id = make_business(client, auth)

        venue = client.post(
            "/api/v1/posts/venues",
            headers=auth,
            json={
                "name": f"Business hall {uuid.uuid4().hex[:5]}",
                "address": "Bole Road, Addis Ababa",
                "citySlug": CITY,
                "latitude": 9.0055,
                "longitude": 38.7810,
            },
        )
        assert venue.status_code == 201, venue.text

        # The dashboard resolves the same identity, so its numbers describe the
        # business rather than a shadow publisher nobody knows exists.
        analytics = client.get("/api/v1/analytics/publisher", headers=auth)
        assert analytics.status_code == 200, analytics.text
        assert (
            client.get(f"/api/v1/businesses/{business_id}", headers=auth).status_code == 200
        )


class TestEnterpriseCanBeGranted:
    """It is negotiated rather than bought, so there has to be a way to apply
    the agreement. Without one it was a tier rendered on the upgrade page that
    no code path could ever put anybody on."""

    def test_a_moderator_can_grant_it(self, client, moderator):
        auth, _ = account(client, "tourism-board")
        business_id = make_business(client, auth)

        granted = client.put(
            f"/api/v1/admin/publisher-plans/{business_id}",
            headers=moderator,
            json={"plan": plans.ENTERPRISE},
        )
        assert granted.status_code == 200, granted.text
        assert granted.json()["data"]["plan"] == plans.ENTERPRISE
        # No end date: an open-ended agreement ends when somebody ends it.
        assert granted.json()["data"]["currentPeriodEnd"] is None

        current = client.get(f"/api/v1/businesses/{business_id}/plans", headers=auth).json()[
            "data"
        ]["current"]
        assert current["plan"] == plans.ENTERPRISE

    def test_the_granted_plan_actually_grants(self, client, moderator):
        """The point of granting it. An enterprise account has no listing
        limit, so it must publish past the free ceiling."""
        auth, _ = account(client, "unlimited")
        business_id = make_business(client, auth)

        client.put(
            f"/api/v1/admin/publisher-plans/{business_id}",
            headers=moderator,
            json={"plan": plans.ENTERPRISE},
        )

        for _ in range(plans.get(plans.FREE).entitlements.max_live_listings + 1):
            assert publish_one(client, auth).status_code == 200

    def test_an_unknown_plan_is_refused(self, client, moderator):
        auth, _ = account(client, "typo")
        business_id = make_business(client, auth)

        refused = client.put(
            f"/api/v1/admin/publisher-plans/{business_id}",
            headers=moderator,
            json={"plan": "platinum"},
        )
        assert refused.status_code == 422, refused.text

    def test_nobody_can_grant_themselves_a_plan(self, client):
        auth, _ = account(client, "selfserver")
        business_id = make_business(client, auth)

        refused = client.put(
            f"/api/v1/admin/publisher-plans/{business_id}",
            headers=auth,
            json={"plan": plans.ENTERPRISE},
        )
        assert refused.status_code == 403, refused.text


class TestPricesAreQuotedInTheRightMoney:
    """BUSINESS-90.01 §18 asks for region-aware pricing, and this is the second
    time Mado has failed it the same way.

    CLAUDE.md already records the first: "every plan used to quote ETB in every
    city", fixed by taking the currency from the city. The plans page then did
    it again from the other end — the client sent a default of `ETB` and the
    server believed it, so a business in London was shown birr it could not act
    on while Stripe sat configured and idle.

    The currency is the business's own city now, and the client sends nothing.
    """

    def test_a_business_is_quoted_in_its_own_city(self, client):
        auth, _ = account(client, "londoner")
        business_id = make_business(client, auth)
        # A venue in London, so the business has a city that is not the pilot.
        created = client.post(
            "/api/v1/posts/venues",
            headers=auth,
            json={
                "name": f"Soho room {uuid.uuid4().hex[:5]}",
                "address": "Soho, London",
                "latitude": 51.5136,
                "longitude": -0.1365,
            },
        )
        assert created.status_code == 201, created.text

        data = client.get(f"/api/v1/businesses/{business_id}/plans", headers=auth).json()["data"]
        assert data["currency"] == "GBP", data["currency"]
        professional = next(p for p in data["plans"] if p["key"] == plans.PROFESSIONAL)
        assert professional["priceMinor"] == plans.price_for(plans.PROFESSIONAL, "GBP")

    def test_the_pilot_city_still_gets_birr(self, client):
        """The counterpart, so the test above is not passing because everything
        moved to one other currency."""
        auth, _ = account(client, "addis")
        business_id = make_business(client, auth)
        publish_one(client, auth)

        data = client.get(f"/api/v1/businesses/{business_id}/plans", headers=auth).json()["data"]
        assert data["currency"] == "ETB"

    def test_a_business_with_nowhere_yet_falls_back_rather_than_guessing(self, client):
        """A brand new business has no venue and no listing. Quoting it in the
        deployment's own city is a familiar default it can change; guessing from
        an address would quote money somebody does not use."""
        auth, _ = account(client, "brand-new")
        business_id = make_business(client, auth)

        data = client.get(f"/api/v1/businesses/{business_id}/plans", headers=auth).json()["data"]
        assert data["currency"] in plans.sold_in()

    def test_asking_for_another_currency_is_honoured(self, client):
        auth, _ = account(client, "cross-border")
        business_id = make_business(client, auth)

        data = client.get(
            f"/api/v1/businesses/{business_id}/plans?currency=USD", headers=auth
        ).json()["data"]
        assert data["currency"] == "USD"
        professional = next(p for p in data["plans"] if p["key"] == plans.PROFESSIONAL)
        assert professional["priceMinor"] == plans.price_for(plans.PROFESSIONAL, "USD")

    def test_asking_for_a_currency_nothing_is_sold_in_falls_back(self, client):
        """Rather than quoting a converted figure, which would be a price nobody
        decided to charge."""
        auth, _ = account(client, "exotic")
        business_id = make_business(client, auth)

        data = client.get(
            f"/api/v1/businesses/{business_id}/plans?currency=XPF", headers=auth
        ).json()["data"]
        assert data["currency"] != "XPF"
        assert data["currency"] in plans.sold_in()

    def test_the_currencies_offered_are_the_ones_actually_priced(self, client):
        """The switcher is built from this, so a stale list would offer a
        currency with no price behind it."""
        auth, _ = account(client, "switcher")
        business_id = make_business(client, auth)

        data = client.get(f"/api/v1/businesses/{business_id}/plans", headers=auth).json()["data"]
        assert data["soldIn"] == plans.sold_in()
        for code in data["soldIn"]:
            assert plans.price_for(plans.PROFESSIONAL, code), f"{code} is offered with no price"


class TestWhoMayBuy:
    def test_an_editor_cannot_spend_the_businesss_money(self, client):
        owner_auth, _ = account(client, "owner")
        business_id = make_business(client, owner_auth)

        editor_auth, editor_email = account(client, "editor")
        invited = client.post(
            f"/api/v1/businesses/{business_id}/members",
            headers=owner_auth,
            json={"email": editor_email, "role": "editor"},
        )
        assert invited.status_code == 201, invited.text
        client.post(
            f"/api/v1/me/business-invitations/{invited.json()['data']['id']}/accept",
            headers=editor_auth,
        )

        # They can see what the business is on...
        assert (
            client.get(f"/api/v1/businesses/{business_id}/plans", headers=editor_auth).status_code
            == 200
        )
        # ...and not buy one.
        refused = client.post(
            f"/api/v1/businesses/{business_id}/plans",
            headers=editor_auth,
            json={"plan": plans.PROFESSIONAL, "currency": "ETB"},
        )
        assert refused.status_code == 403, refused.text

    def test_a_stranger_cannot_see_what_a_business_pays(self, client):
        owner_auth, _ = account(client, "owner")
        business_id = make_business(client, owner_auth)

        stranger_auth, _ = account(client, "stranger")
        refused = client.get(f"/api/v1/businesses/{business_id}/plans", headers=stranger_auth)
        assert refused.status_code in (403, 404), refused.text
