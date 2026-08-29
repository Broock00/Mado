"""The platform's commission, and the debt it creates.

Mado sells through its own merchant account, so every santim of every ticket
arrives here and the publisher's share is money Mado is holding. That makes this
file about two things that are only visible when they are wrong:

- **arithmetic**, because a fee that rounds the wrong way is invisible per order
  and material over a year, and
- **the ledger's meaning**, because an entry written for an order that was never
  paid is a debt against money nobody has, and a missing entry is a publisher
  never paid at all.

The rate-is-copied tests are the important ones. A fee recomputed at settlement
rather than read back from the order would pass every "does it charge a
commission" test and silently restate what a publisher was owed the next time
the rate moved.

**Run the server with `MADO_PAYMENT_PROVIDER=stub`**, alongside the AI and
weather stubs the rest of the suite needs. A commission only exists on a paid
order, and against a real provider these tests cannot make one: Chapa rejects
the `mado-qa.example.org` addresses the teardown identifies test data by, so
every purchase comes back `PAYMENT_UNAVAILABLE` before there is anything to
charge a fee on.
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import app.models  # noqa: F401  (orders are built here, so mappers must resolve)
from app.domains.commerce import fees
from app.domains.commerce.models import ORDER_PAID, ORDER_PENDING, Order
from app.domains.publisher.models import Publisher
from tests.conftest import requires_api

BASE_URL = os.environ.get("MADO_TEST_API_URL", "http://127.0.0.1:8000")
PASSWORD = "discover-addis-2026"
CITY = "addis-ababa"

pytestmark = pytest.mark.anyio


# ----------------------------------------------------------------- arithmetic


class TestTheFeeItself:
    def test_it_is_a_share_of_the_order(self):
        # 5% of 150.00 birr is 7.50, in santim.
        assert fees.fee_for(15_000, 500) == 750

    def test_it_floors_rather_than_rounds(self):
        """The fraction of a santim goes to the publisher, not to Mado.

        333 * 5% is 16.65 santim. Rounding up would take the extra sixty-five
        hundredths on every order, which nobody would ever notice per sale and
        which adds up in exactly one direction.
        """
        assert fees.fee_for(333, 500) == 16

    def test_a_zero_decimal_currency_needs_no_special_case(self):
        """Yen has no minor unit, so ¥500 is 500, not 50000.

        The fee is a proportion of an integer, so it is right for both without
        knowing which it has - which is the reason it takes minor units rather
        than a price.
        """
        assert fees.fee_for(500, 500) == 25

    def test_nothing_is_taken_from_a_free_ticket(self):
        assert fees.fee_for(0, 500) == 0

    def test_no_rate_takes_nothing(self):
        assert fees.fee_for(15_000, 0) == 0

    def test_it_can_never_exceed_the_order(self):
        """A net below zero would be a bill, not a smaller payment.

        Unreachable through `rate_for`, which clamps. Held here as well because
        the constraint that depends on it is in the database, and a migration
        failing at 3am is a worse place to discover it.
        """
        assert fees.fee_for(15_000, 50_000) == 15_000

    def test_a_negative_amount_is_not_a_refund(self):
        assert fees.fee_for(-15_000, 500) == 0


class TestWhichRateApplies:
    def test_a_publisher_with_no_negotiated_rate_pays_the_default(self):
        assert fees.rate_for(Publisher(name="Ordinary", slug="ordinary")) == (
            fees.get_settings().platform_fee_bps
        )

    def test_a_negotiated_rate_wins(self):
        publisher = Publisher(name="Tourism board", slug="tourism-board", fee_bps=150)
        assert fees.rate_for(publisher) == 150

    def test_zero_is_a_rate_and_not_an_absence(self):
        """A publisher who pays nothing is a real arrangement.

        `fee_bps = 0` must not fall through to the default, or the one account
        that negotiated its way out of the commission would be charged it.
        """
        assert fees.rate_for(Publisher(name="Free", slug="free", fee_bps=0)) == 0

    def test_an_absurd_rate_is_clamped(self):
        """A typo in an admin field should not take half of somebody's takings.

        2500 meant as 2.5% is 25%, and the digit that made it so is invisible.
        """
        publisher = Publisher(name="Typo", slug="typo", fee_bps=90_000)
        assert fees.rate_for(publisher) == fees.MAX_FEE_BPS


# ---------------------------------------------------------------- the ledger


class FakeSession:
    """Enough of a session for `record_sale`, and nothing else.

    A real one would need a database, and the property being tested here has
    nothing to do with persistence: it is that the entry is built from what the
    order stored rather than from a fresh calculation.
    """

    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:  # noqa: ANN001
        self.added.append(obj)

    @asynccontextmanager
    async def begin_nested(self):
        yield self


def paid_order(**overrides) -> Order:
    defaults = {
        "id": uuid.uuid4(),
        "status": ORDER_PAID,
        "amount_minor": 15_000,
        "platform_fee_minor": 750,
        "fee_rate_bps": 500,
        "currency": "ETB",
        "publisher_id": uuid.uuid4(),
        "paid_at": datetime.now(UTC),
    }
    return Order(**{**defaults, **overrides})


class TestWhatReachesTheLedger:
    async def test_a_paid_sale_is_recorded_once(self):
        session = FakeSession()
        entry = await fees.record_sale(session, paid_order())

        assert entry is not None
        assert (entry.gross_minor, entry.fee_minor, entry.net_minor) == (15_000, 750, 14_250)
        assert entry.fee_minor + entry.net_minor == entry.gross_minor

    async def test_it_reads_the_rate_off_the_order_rather_than_recomputing(self):
        """The whole reason the rate is stored.

        This order was sold at 2.5% and the platform now charges 5%. Settling it
        must produce the fee that was agreed, not the one in force today -
        otherwise every unsettled order silently reprices whenever the rate
        moves, and a publisher's earnings change after the sale.
        """
        session = FakeSession()
        sold_at_half_rate = paid_order(fee_rate_bps=250, platform_fee_minor=375)

        entry = await fees.record_sale(session, sold_at_half_rate)

        assert entry is not None
        assert entry.fee_rate_bps == 250
        assert entry.fee_minor == 375, "the rate in the settings must not reach a settled order"
        assert entry.net_minor == 14_625

    async def test_a_free_ticket_earns_nothing_and_is_not_recorded(self):
        """No money arrived, so there is no debt.

        An entry of zero would be a payout line for nothing, and a ledger that
        records intentions rather than receipts cannot be reconciled against a
        bank statement.
        """
        session = FakeSession()
        free = paid_order(amount_minor=0, platform_fee_minor=0)
        assert await fees.record_sale(session, free) is None
        assert session.added == []

    async def test_an_order_with_no_publisher_is_not_recorded(self):
        session = FakeSession()
        assert await fees.record_sale(session, paid_order(publisher_id=None)) is None

    async def test_an_unpaid_order_is_refused_outright(self):
        """Not a silent skip. Nothing should be trying this, and a debt written
        against money that has not arrived is worth a stack trace."""
        session = FakeSession()
        with pytest.raises(ValueError):
            await fees.record_sale(session, paid_order(status=ORDER_PENDING))


# ------------------------------------------------------------ against the API


@pytest.fixture
def client():
    """The gate hangs off this rather than off the module.

    Half this file is arithmetic that needs no server at all, and an autouse
    module fixture would make `fee_for` fail because nothing was listening on
    port 8000 - which is a confusing way to be told the wrong thing.
    """
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


def business_selling_tickets(client, auth, *, price_minor: int) -> tuple[str, str, str]:
    """A business with one dated listing and one tier on sale.

    Returns `(business id, experience id, occurrence id)`.
    """
    business = client.post(
        "/api/v1/me/account-type/business",
        headers=auth,
        json={
            "name": f"Fee Test Venue {uuid.uuid4().hex[:6]}",
            "businessType": "venue",
            "description": "A business invented to check what the platform keeps.",
        },
    )
    assert business.status_code == 201, business.text
    business_id = business.json()["data"]["business"]["id"]

    venue = client.post(
        "/api/v1/posts/venues",
        headers=auth,
        json={
            "name": f"Fee hall {uuid.uuid4().hex[:5]}",
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
            "title": f"Commission test night {uuid.uuid4().hex[:5]}",
            "description": "An evening used to check the platform's cut of a ticket sale.",
            "citySlug": CITY,
            "type": "event",
            "categorySlug": "music",
            "venueId": venue.json()["data"]["id"],
        },
    )
    assert post.status_code == 201, post.text
    experience_id = post.json()["data"]["id"]

    when = datetime.now(UTC) + timedelta(days=4)
    occurrence = client.post(
        f"/api/v1/posts/{experience_id}/events",
        headers=auth,
        json={"startTime": when.isoformat(), "capacity": 50},
    )
    assert occurrence.status_code == 201, occurrence.text

    tier = client.put(
        f"/api/v1/posts/{experience_id}/tickets",
        headers=auth,
        json={"name": "General admission", "priceMinor": price_minor, "quantity": 50},
    )
    assert tier.status_code == 200, tier.text
    return business_id, experience_id, occurrence.json()["data"]["id"]


def buy(client, experience_id: str, occurrence_id: str, *, settle: bool = True) -> dict:
    """One explorer buying one ticket, settled through the stub on request."""
    buyer, _ = account(client, "buyer")
    shown = client.get(
        f"/api/v1/experiences/{experience_id}/events/{occurrence_id}/ticketing"
    ).json()["data"]
    order = client.post(
        f"/api/v1/experiences/{experience_id}/events/{occurrence_id}/orders",
        headers=buyer,
        json={"lines": [{"ticketTypeId": shown["ticketTypes"][0]["id"], "quantity": 1}]},
    )
    assert order.status_code == 201, order.text
    body = order.json()["data"]
    if not settle or body["status"] == "paid":
        return body

    settled = client.post(
        f"/api/v1/payments/simulate/{body['reference']}", headers=buyer
    )
    if settled.status_code == 403:
        pytest.skip("A real payment provider is configured; nothing to simulate against.")
    assert settled.status_code == 200, settled.text
    return settled.json()["data"]


def earnings(client, auth, business_id: str) -> dict:
    response = client.get(f"/api/v1/businesses/{business_id}/earnings", headers=auth)
    assert response.status_code == 200, response.text
    return response.json()["data"]


class TestEarningsAsThePublisherSeesThem:
    def test_a_business_with_no_sales_has_earned_nothing(self, client):
        auth, _ = account(client, "quiet")
        business_id, _, _ = business_selling_tickets(client, auth, price_minor=15_000)

        assert earnings(client, auth, business_id)["totals"] == []

    def test_a_paid_sale_appears_as_gross_fee_and_net(self, client):
        auth, _ = account(client, "seller")
        business_id, experience_id, occurrence_id = business_selling_tickets(
            client, auth, price_minor=15_000
        )

        paid = buy(client, experience_id, occurrence_id)
        assert paid["status"] == "paid", paid

        data = earnings(client, auth, business_id)
        line = data["totals"][0]

        # Computed from the rate the server reports rather than hard-coded, so
        # this keeps checking the arithmetic and not the current setting.
        expected_fee = 15_000 * data["feeRateBps"] // 10_000
        assert line["currency"] == "ETB"
        assert line["grossMinor"] == 15_000
        assert line["feeMinor"] == expected_fee
        assert line["netMinor"] == 15_000 - expected_fee
        assert line["sales"] == 1

    def test_what_is_owed_starts_as_the_whole_of_the_net(self, client):
        """Nothing has been paid out, so everything earned is still owed."""
        auth, _ = account(client, "unpaid")
        business_id, experience_id, occurrence_id = business_selling_tickets(
            client, auth, price_minor=20_000
        )
        buy(client, experience_id, occurrence_id)

        line = earnings(client, auth, business_id)["totals"][0]
        assert line["owingMinor"] == line["netMinor"]

    def test_an_unpaid_order_earns_nothing(self, client):
        """A hold is not a sale. Until the provider says the money arrived there
        is nothing to owe, and a ledger that counted holds would show earnings
        that evaporate when the cart is abandoned."""
        auth, _ = account(client, "abandoned")
        business_id, experience_id, occurrence_id = business_selling_tickets(
            client, auth, price_minor=15_000
        )

        held = buy(client, experience_id, occurrence_id, settle=False)
        assert held["status"] == "pending"

        assert earnings(client, auth, business_id)["totals"] == []

    def test_a_free_ticket_earns_nothing(self, client):
        """Free tickets settle immediately and without a provider, so this is
        the one paid order that must still produce no debt."""
        auth, _ = account(client, "free")
        business_id, experience_id, occurrence_id = business_selling_tickets(
            client, auth, price_minor=0
        )

        paid = buy(client, experience_id, occurrence_id)
        assert paid["status"] == "paid"

        assert earnings(client, auth, business_id)["totals"] == []


class TestWhoMaySeeTheMoney:
    def test_an_analyst_cannot(self, client):
        """Money is not analytics.

        An analyst is the marketing agency shown how the posts are doing. That
        is a different decision from showing them what the business took, and
        one permission covering both would force whoever grants the first to
        grant the second.
        """
        owner_auth, _ = account(client, "owner")
        business_id, _, _ = business_selling_tickets(client, owner_auth, price_minor=15_000)

        analyst_auth, analyst_email = account(client, "analyst")
        invited = client.post(
            f"/api/v1/businesses/{business_id}/members",
            headers=owner_auth,
            json={"email": analyst_email, "role": "analyst"},
        )
        assert invited.status_code == 201, invited.text
        accepted = client.post(
            f"/api/v1/me/business-invitations/{invited.json()['data']['id']}/accept",
            headers=analyst_auth,
        )
        assert accepted.status_code == 200, accepted.text

        # They can see the numbers they were invited for...
        assert (
            client.get(f"/api/v1/businesses/{business_id}", headers=analyst_auth).status_code
            == 200
        )
        # ...and not the money.
        refused = client.get(
            f"/api/v1/businesses/{business_id}/earnings", headers=analyst_auth
        )
        assert refused.status_code == 403, refused.text

    def test_an_administrator_can(self, client):
        """The counterpart, so the test above is not passing because the
        endpoint refuses everybody."""
        owner_auth, _ = account(client, "owner")
        business_id, _, _ = business_selling_tickets(client, owner_auth, price_minor=15_000)

        admin_auth, admin_email = account(client, "admin")
        invited = client.post(
            f"/api/v1/businesses/{business_id}/members",
            headers=owner_auth,
            json={"email": admin_email, "role": "admin"},
        )
        assert invited.status_code == 201, invited.text
        client.post(
            f"/api/v1/me/business-invitations/{invited.json()['data']['id']}/accept",
            headers=admin_auth,
        )

        allowed = client.get(f"/api/v1/businesses/{business_id}/earnings", headers=admin_auth)
        assert allowed.status_code == 200, allowed.text

    def test_a_stranger_cannot(self, client):
        owner_auth, _ = account(client, "owner")
        business_id, _, _ = business_selling_tickets(client, owner_auth, price_minor=15_000)

        stranger_auth, _ = account(client, "stranger")
        refused = client.get(
            f"/api/v1/businesses/{business_id}/earnings", headers=stranger_auth
        )
        assert refused.status_code in (403, 404), refused.text

class TestThePayoutQueueIsPrivileged:
    """Only the refusals, which is all this suite can reach.

    There is no endpoint that makes the first moderator - deliberately - so the
    granting half is exercised by hand against a running server rather than
    given a fixture here that would have to create one.

    The refusals are the half worth automating anyway: these endpoints read what
    every business on the platform has earned and mark money as sent.
    """

    def test_an_explorer_cannot_see_what_mado_owes(self, client):
        auth, _ = account(client, "nosy")
        refused = client.get("/api/v1/admin/payouts/owing", headers=auth)
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["code"] == "NOT_A_MODERATOR"

    def test_an_explorer_cannot_build_a_payout(self, client):
        auth, _ = account(client, "nosy")
        refused = client.post(
            "/api/v1/admin/payouts",
            headers=auth,
            json={"publisherId": str(uuid.uuid4()), "currency": "ETB"},
        )
        assert refused.status_code == 403, refused.text

    def test_a_business_owner_cannot_mark_their_own_payout_sent(self, client):
        """Being owed money is not authority to declare it paid."""
        auth, _ = account(client, "owner")
        business_selling_tickets(client, auth, price_minor=15_000)

        refused = client.post(
            f"/api/v1/admin/payouts/{uuid.uuid4()}/paid",
            headers=auth,
            json={"reference": "made-up"},
        )
        assert refused.status_code == 403, refused.text

    def test_signed_out_is_refused_too(self, client):
        refused = client.get("/api/v1/admin/payouts/owing")
        assert refused.status_code == 401, refused.text


class TestTheDashboardReadsWhatTheServerEnforces:
    def test_the_permission_is_listed_where_the_interface_reads_it(self, client):
        """The dashboard hides the section on this. It is a convenience and
        never the enforcement, but it has to agree with the enforcement."""
        auth, _ = account(client, "owner")
        business_id, _, _ = business_selling_tickets(client, auth, price_minor=15_000)

        held = client.get(
            f"/api/v1/businesses/{business_id}/permissions", headers=auth
        ).json()["data"]
        assert "finance:view" in held
