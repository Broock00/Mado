"""Metering API calls, and the allowance they are counted against.

BUSINESS-09 puts enterprise and API at 25% of the long-term revenue mix and
BUSINESS-90.01 §13 asks for usage-based pricing. Keys and scopes already
existed; nothing counted a call, so there was no figure to bill from and no way
to distinguish a runaway integration from a successful one.

Two properties matter more than the counting itself, and both are here:

- **A quota is not a rate limit.** They have different remedies - "buy more"
  against "slow down" - so they must be distinguishable by code. A developer who
  cannot tell them apart backs off from one and never fixes the other.
- **Metering must never fail a call.** Losing a count costs a fraction of an
  invoice; losing the request costs the customer's integration. So a counter that
  cannot be written is logged and swallowed.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime

import httpx
import pytest

import app.models  # noqa: F401
from app.domains.developer import usage
from app.domains.developer.keys import SCOPE_EXPERIENCES_READ
from tests.conftest import make_moderator, requires_api

BASE_URL = os.environ.get("MADO_TEST_API_URL", "http://127.0.0.1:8000")
PASSWORD = "discover-addis-2026"

pytestmark = pytest.mark.anyio


# --------------------------------------------------------------- the plans


class TestDeveloperPlans:
    def test_an_unknown_plan_falls_back_to_free(self):
        assert usage.plan_for("gold").key == usage.PLAN_FREE
        assert usage.plan_for(None).key == usage.PLAN_FREE

    def test_the_free_tier_is_enough_to_build_something(self):
        """A free tier that runs out during an afternoon's integration work
        teaches a developer that the API is a liability, and they do not come
        back to buy the paid one."""
        assert usage.plan_for(usage.PLAN_FREE).monthly_calls >= 10_000

    def test_enterprise_is_unmetered_rather_than_generous(self):
        """None, not a very large number. The agreement is the limit, and a
        number here would be one somebody eventually hits at 3am."""
        assert usage.plan_for(usage.PLAN_ENTERPRISE).monthly_calls is None

    def test_allowances_only_grow_with_the_plan(self):
        free = usage.plan_for(usage.PLAN_FREE).monthly_calls
        pro = usage.plan_for(usage.PLAN_PRO).monthly_calls
        assert pro > free


class TestTheBillingMonth:
    def test_it_starts_on_the_first(self):
        assert usage.month_start(datetime(2026, 8, 28, tzinfo=UTC)) == date(2026, 8, 1)

    def test_it_resets_on_the_first_of_the_next_month(self):
        assert usage.next_month_start(datetime(2026, 8, 28, tzinfo=UTC)) == date(2026, 9, 1)

    def test_december_rolls_into_january(self):
        """By arithmetic on the month number, not by adding thirty days - which
        lands in the wrong month twice a year and never in February."""
        assert usage.next_month_start(datetime(2026, 12, 9, tzinfo=UTC)) == date(2027, 1, 1)

    def test_february_is_not_special(self):
        assert usage.next_month_start(datetime(2028, 2, 29, tzinfo=UTC)) == date(2028, 3, 1)


class TestQuotaIsItsOwnFailure:
    def test_it_is_not_the_rate_limit_error(self):
        """The distinction the whole design turns on. Same status, different
        code, because the remedies are opposite."""
        from app.core.errors import RateLimitError

        assert usage.QuotaExceeded.status_code == RateLimitError.status_code == 429
        assert usage.QuotaExceeded.code != RateLimitError.code
        assert usage.QuotaExceeded.code == "QUOTA_EXCEEDED"


# ---------------------------------------------------------- against the API


@pytest.fixture
def client():
    requires_api()
    with httpx.Client(base_url=BASE_URL, timeout=30) as session:
        yield session


def account(client, who="developer") -> tuple[dict, str]:
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
    auth, email = account(client, "quota-admin")
    make_moderator(email)
    return auth


def mint_key(client, auth) -> str:
    response = client.post(
        "/api/v1/developer/keys",
        headers=auth,
        json={
            "name": f"Metering test {uuid.uuid4().hex[:5]}",
            "scopes": [SCOPE_EXPERIENCES_READ],
        },
    )
    assert response.status_code == 201, response.text
    # The plaintext is in this response and nowhere else.
    return response.json()["data"]["secret"]


class TestCallsAreCounted:
    def test_a_new_account_has_used_nothing(self, client):
        auth, _ = account(client, "fresh")
        body = client.get("/api/v1/developer/usage", headers=auth)
        assert body.status_code == 200, body.text
        data = body.json()["data"]
        assert data["callsThisMonth"] == 0
        assert data["plan"] == usage.PLAN_FREE
        assert data["monthlyAllowance"] == usage.plan_for(usage.PLAN_FREE).monthly_calls

    def test_a_call_through_a_key_is_counted(self, client):
        auth, _ = account(client, "counter")
        key = mint_key(client, auth)

        before = client.get("/api/v1/developer/usage", headers=auth).json()["data"][
            "callsThisMonth"
        ]
        who = client.get("/api/v1/developer/whoami", headers={"X-Mado-Api-Key": key})
        assert who.status_code == 200, who.text
        after = client.get("/api/v1/developer/usage", headers=auth).json()["data"][
            "callsThisMonth"
        ]

        assert after == before + 1

    def test_several_calls_accumulate(self, client):
        auth, _ = account(client, "busy")
        key = mint_key(client, auth)

        for _ in range(3):
            client.get("/api/v1/developer/whoami", headers={"X-Mado-Api-Key": key})

        assert (
            client.get("/api/v1/developer/usage", headers=auth).json()["data"]["callsThisMonth"]
            == 3
        )

    def test_two_keys_bill_to_one_account(self, client):
        """The plan belongs to the account, not the key. Otherwise minting a
        second key mints a second allowance, which is a free upgrade in the
        shape of a button that already exists."""
        auth, _ = account(client, "twokeys")
        first, second = mint_key(client, auth), mint_key(client, auth)

        client.get("/api/v1/developer/whoami", headers={"X-Mado-Api-Key": first})
        client.get("/api/v1/developer/whoami", headers={"X-Mado-Api-Key": second})

        assert (
            client.get("/api/v1/developer/usage", headers=auth).json()["data"]["callsThisMonth"]
            == 2
        )

    def test_a_browser_session_is_not_metered(self, client):
        """The allowance prices machine access. Charging somebody's own use of
        the website against it would meter the product itself."""
        auth, _ = account(client, "human")
        for _ in range(3):
            client.get("/api/v1/developer/usage", headers=auth)

        assert (
            client.get("/api/v1/developer/usage", headers=auth).json()["data"]["callsThisMonth"]
            == 0
        )

    def test_a_rejected_call_still_counts(self, client):
        """It still consumed the allowance, which is what makes a failing
        integration visible on a bill rather than free to run forever."""
        auth, _ = account(client, "wrongscope")
        key = mint_key(client, auth)

        # A read-scoped key, used to write.
        refused = client.post(
            "/api/v1/posts",
            headers={"X-Mado-Api-Key": key},
            json={
                "title": "Nope",
                "description": "A write attempted with a read-only key.",
                "citySlug": "addis-ababa",
            },
        )
        assert refused.status_code in (401, 403, 422), refused.text

        assert (
            client.get("/api/v1/developer/usage", headers=auth).json()["data"]["callsThisMonth"]
            >= 1
        )

    def test_an_invalid_key_is_not_counted_against_anybody(self, client):
        """There is nobody to count it against, and inventing an owner for an
        unknown credential is how one account gets billed for another's
        mistakes."""
        auth, _ = account(client, "innocent")
        bad = client.get(
            "/api/v1/developer/whoami", headers={"X-Mado-Api-Key": "mado_sk_not-a-real-key"}
        )
        assert bad.status_code == 401

        assert (
            client.get("/api/v1/developer/usage", headers=auth).json()["data"]["callsThisMonth"]
            == 0
        )

    def test_the_breakdown_shows_today(self, client):
        auth, _ = account(client, "daily")
        key = mint_key(client, auth)
        client.get("/api/v1/developer/whoami", headers={"X-Mado-Api-Key": key})

        data = client.get("/api/v1/developer/usage", headers=auth).json()["data"]
        assert data["daily"], "a call today should appear in the breakdown"
        assert data["daily"][-1]["day"] == datetime.now(UTC).date().isoformat()
        assert data["resetsOn"] == usage.next_month_start().isoformat()

    def test_running_out_says_so_in_its_own_words(self, client, moderator):
        """The refusal that makes the metering worth having.

        A tiny allowance is granted rather than ten thousand calls being made,
        which is the same code path and finishes this decade.
        """
        auth, _ = account(client, "spender")
        key = mint_key(client, auth)
        me = client.get("/api/v1/me", headers=auth).json()["data"]

        granted = client.put(
            f"/api/v1/admin/developer-plans/{me['id']}",
            headers=moderator,
            json={"plan": usage.PLAN_FREE, "monthlyCallsOverride": 2},
        )
        assert granted.status_code == 200, granted.text
        assert granted.json()["data"]["monthlyAllowance"] == 2

        for index in range(2):
            allowed = client.get("/api/v1/developer/whoami", headers={"X-Mado-Api-Key": key})
            assert allowed.status_code == 200, f"call {index + 1} of 2: {allowed.text}"

        spent = client.get("/api/v1/developer/whoami", headers={"X-Mado-Api-Key": key})
        assert spent.status_code == 429, spent.text
        error = spent.json()["error"]
        # Not RATE_LIMIT_EXCEEDED. Waiting fixes that one and does nothing at
        # all about this one, so a developer has to be able to tell them apart.
        assert error["code"] == "QUOTA_EXCEEDED", error
        assert error["details"]["allowance"] == 2

    def test_being_refused_does_not_itself_spend_the_allowance(self, client, moderator):
        """Checked before it is counted, so a caller who is over is not charged
        for being told so - and cannot be pushed further over by asking."""
        auth, _ = account(client, "overspender")
        key = mint_key(client, auth)
        me = client.get("/api/v1/me", headers=auth).json()["data"]
        client.put(
            f"/api/v1/admin/developer-plans/{me['id']}",
            headers=moderator,
            json={"plan": usage.PLAN_FREE, "monthlyCallsOverride": 1},
        )

        client.get("/api/v1/developer/whoami", headers={"X-Mado-Api-Key": key})
        for _ in range(3):
            assert (
                client.get(
                    "/api/v1/developer/whoami", headers={"X-Mado-Api-Key": key}
                ).status_code
                == 429
            )

        assert (
            client.get("/api/v1/developer/usage", headers=auth).json()["data"]["callsThisMonth"]
            == 1
        )

    def test_an_unmetered_plan_never_runs_out(self, client, moderator):
        auth, _ = account(client, "enterprise")
        key = mint_key(client, auth)
        me = client.get("/api/v1/me", headers=auth).json()["data"]

        granted = client.put(
            f"/api/v1/admin/developer-plans/{me['id']}",
            headers=moderator,
            json={"plan": usage.PLAN_ENTERPRISE},
        )
        assert granted.status_code == 200, granted.text
        # Null, not a very large number: the agreement is the limit.
        assert granted.json()["data"]["monthlyAllowance"] is None

        for _ in range(3):
            assert (
                client.get(
                    "/api/v1/developer/whoami", headers={"X-Mado-Api-Key": key}
                ).status_code
                == 200
            )

    def test_only_a_moderator_may_grant_an_api_plan(self, client):
        auth, _ = account(client, "selfserver")
        me = client.get("/api/v1/me", headers=auth).json()["data"]

        refused = client.put(
            f"/api/v1/admin/developer-plans/{me['id']}",
            headers=auth,
            json={"plan": usage.PLAN_ENTERPRISE},
        )
        assert refused.status_code == 403, refused.text

    def test_usage_needs_a_session_and_not_a_key(self, client):
        """Reading somebody's bill is an account question, and a key is a
        machine credential that should not be able to see it."""
        auth, _ = account(client, "curious")
        key = mint_key(client, auth)

        refused = client.get("/api/v1/developer/usage", headers={"X-Mado-Api-Key": key})
        assert refused.status_code == 401, refused.text
