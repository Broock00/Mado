"""Paid placement, and everything it is not allowed to do.

BUSINESS-90.01 §7 permits sponsored discovery on one condition - that it never
compromises recommendation integrity - and most of what follows is that
condition made executable. The happy path gets two tests; the refusals get the
rest, because a promotion that quietly starts outranking things is a failure
nobody sees from the outside and the publisher who paid has no reason to report
it.

The load-bearing ones:

- `test_the_first_result_is_never_sold` - position one is what the platform
  actually thinks, whoever has paid.
- `test_a_promotion_cannot_bypass_a_requirement` - money is not a reason to show
  somebody with a nut allergy a kitchen that has never answered the question.
- `test_the_concierge_returns_nothing_sponsored` - the gateway phrases facts
  that came back from tools, and a paid item in that stream becomes the
  assistant's own recommendation with no label surviving generation.

**Run the server with `MADO_PAYMENT_PROVIDER=stub`**, as for the other revenue
tests: nothing runs until a payment settles.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import app.models  # noqa: F401
from app.domains.catalog.repository import Area
from app.domains.promotion import service as promotion_service
from app.domains.promotion.models import (
    DEFAULT_RADIUS_KM,
    MAX_DAYS,
    MIN_DAYS,
    STATUS_ACTIVE,
    STATUS_PENDING,
    Promotion,
)
from tests.conftest import requires_api

BASE_URL = os.environ.get("MADO_TEST_API_URL", "http://127.0.0.1:8000")
PASSWORD = "discover-addis-2026"
CITY = "addis-ababa"

pytestmark = pytest.mark.anyio


# ------------------------------------------------------------------ pricing


class TestWhatAPromotionCosts:
    def test_it_is_priced_by_the_day(self):
        daily = promotion_service.DAILY_PRICE_MINOR["ETB"]
        assert promotion_service.price_for(7, "ETB") == daily * 7

    def test_a_currency_it_is_not_sold_in_returns_nothing(self):
        """Rather than a converted figure, which would be a price nobody
        decided to charge, arrived at through this morning's exchange rate."""
        assert promotion_service.price_for(7, "XPF") is None

    def test_the_pilot_currency_is_priced(self):
        assert promotion_service.price_for(1, "ETB")


# ----------------------------------------------------------- who it reaches


def promotion(**overrides) -> Promotion:
    defaults = {
        "id": uuid.uuid4(),
        "publisher_id": uuid.uuid4(),
        "experience_id": uuid.uuid4(),
        "status": STATUS_ACTIVE,
        "starts_at": datetime.now(UTC) - timedelta(days=1),
        "ends_at": datetime.now(UTC) + timedelta(days=1),
        "amount_minor": 20_000,
        "currency": "ETB",
    }
    return Promotion(**{**defaults, **overrides})


class TestWhereAPromotionReaches:
    """`_reaches` is conservative in both directions, deliberately.

    A promotion with no place reaches nowhere rather than everywhere, and a
    request with no area matches nothing rather than everything - an unscoped
    search is usually a country-wide question, and that is not somewhere one
    business should be standing in front of.
    """

    reaches = staticmethod(promotion_service.PromotionService._reaches)

    def test_a_city_promotion_reaches_that_city(self):
        assert self.reaches(promotion(city_slug=CITY), Area(city_slug=CITY))

    def test_a_city_promotion_does_not_reach_another_city(self):
        assert not self.reaches(promotion(city_slug=CITY), Area(city_slug="nairobi"))

    def test_a_promotion_with_no_place_reaches_nowhere(self):
        assert not self.reaches(promotion(), Area(city_slug=CITY))

    def test_a_request_with_no_area_matches_nothing(self):
        assert not self.reaches(promotion(city_slug=CITY), None)

    def test_a_point_promotion_reaches_somewhere_inside_its_radius(self):
        # Bole, and a point about a kilometre away.
        near = promotion(latitude=9.0055, longitude=38.7810, radius_km=5)
        assert self.reaches(near, Area(latitude=9.0100, longitude=38.7850))

    def test_a_point_promotion_does_not_reach_the_next_country(self):
        near = promotion(latitude=9.0055, longitude=38.7810, radius_km=5)
        assert not self.reaches(near, Area(latitude=-1.2921, longitude=36.8219))

    def test_a_point_promotion_is_refused_against_a_whole_country(self):
        """A bounding box for a region is far larger than the neighbourhood
        somebody bought, so it is refused rather than approximated."""
        near = promotion(latitude=9.0055, longitude=38.7810, radius_km=5)
        assert not self.reaches(near, Area(country_code="ET"))

    def test_the_default_radius_is_a_neighbourhood_not_a_continent(self):
        assert 1 <= DEFAULT_RADIUS_KM <= 25


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


def make_business(client, auth) -> str:
    response = client.post(
        "/api/v1/me/account-type/business",
        headers=auth,
        json={
            "name": f"Promo Venue {uuid.uuid4().hex[:6]}",
            "businessType": "cafe",
            "description": "A business invented to check what a paid slot may do.",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["business"]["id"]


def publish(client, auth, *, marker: str, suitability: list[str] | None = None) -> str:
    """A published listing carrying a rare word, so a search can find just it."""
    venue = client.post(
        "/api/v1/posts/venues",
        headers=auth,
        json={
            "name": f"Promo hall {uuid.uuid4().hex[:5]}",
            "address": "Bole Road, Addis Ababa",
            "citySlug": CITY,
            "latitude": 9.0055,
            "longitude": 38.7810,
        },
    )
    assert venue.status_code == 201, venue.text

    payload = {
        "title": f"{marker} evening {uuid.uuid4().hex[:5]}",
        "description": f"An evening of {marker}, used to check paid placement.",
        "citySlug": CITY,
        "type": "place",
        "categorySlug": "music",
        "venueId": venue.json()["data"]["id"],
    }
    if suitability:
        payload["suitability"] = suitability

    post = client.post("/api/v1/posts", headers=auth, json=payload)
    assert post.status_code == 201, post.text
    experience_id = post.json()["data"]["id"]

    live = client.post(f"/api/v1/posts/{experience_id}/publish", headers=auth)
    assert live.status_code == 200, live.text
    return experience_id


def buy_slot(client, auth, business_id: str, experience_id: str, **overrides) -> dict:
    """Buy and settle a promotion, so it is actually running."""
    payload = {
        "experienceId": experience_id,
        "days": 7,
        "currency": "ETB",
        "citySlug": CITY,
        **overrides,
    }
    started = client.post(
        f"/api/v1/businesses/{business_id}/promotions", headers=auth, json=payload
    )
    if started.status_code == 409:
        pytest.skip("Payments are unavailable; nothing to promote.")
    assert started.status_code == 201, started.text
    promotion_id = started.json()["data"]["id"]
    assert started.json()["data"]["status"] == STATUS_PENDING

    settled = client.post(
        f"/api/v1/businesses/{business_id}/promotions/{promotion_id}/simulate", headers=auth
    )
    if settled.status_code == 403:
        pytest.skip("A real payment provider is configured; nothing to simulate against.")
    assert settled.status_code == 200, settled.text
    assert settled.json()["data"]["status"] == STATUS_ACTIVE
    return settled.json()["data"]


def promote_lowest(client, auth, business_id: str, marker: str) -> str:
    """Promote whichever matching listing currently ranks last.

    Deliberately not "promote the one just published": nothing here controls
    where a new listing lands in the ranking, and promoting one that happens to
    be top already is a no-op by design (there is no position to buy). Picking
    the lowest makes these tests about the slot rather than about the ranker's
    mood.
    """
    ranked = search(client, marker)
    assert len(ranked) >= 2, "need something above it for the slot to sit below"
    lowest = ranked[-1]["id"]
    buy_slot(client, auth, business_id, lowest)
    return lowest


def search(client, term: str, **params) -> list[dict]:
    response = client.get(
        "/api/v1/search", params={"q": term, "city": CITY, **params}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["results"]


class TestAPaidSlotAppears:
    def test_a_running_promotion_is_shown_and_labelled(self, client):
        auth, _ = account(client, "advertiser")
        business_id = make_business(client, auth)

        # Two listings sharing a rare word, so a search returns both and there
        # is an organic result for the paid one to sit behind.
        marker = f"kompa{uuid.uuid4().hex[:6]}"
        publish(client, auth, marker=marker)
        publish(client, auth, marker=marker)
        promoted = promote_lowest(client, auth, business_id, marker)

        results = search(client, marker)
        sponsored = [r for r in results if r["sponsored"]]
        assert len(sponsored) == 1, [r["title"] for r in results]
        assert sponsored[0]["id"] == promoted
        # And it says what it is, rather than borrowing an organic explanation.
        assert "promoted" in (sponsored[0]["reason"] or "").lower()
        assert sponsored[0]["promotionId"]

    def test_only_one_slot_is_ever_sold(self, client):
        """However many promotions are running, an explorer sees one."""
        auth, _ = account(client, "greedy")
        business_id = make_business(client, auth)

        marker = f"zemen{uuid.uuid4().hex[:6]}"
        for _ in range(4):
            publish(client, auth, marker=marker)
        ranked = [row["id"] for row in search(client, marker)]
        for listing in ranked[1:]:
            buy_slot(client, auth, business_id, listing)

        results = search(client, marker)
        assert len([r for r in results if r["sponsored"]]) == 1


class TestWhatAPaidSlotMayNotDo:
    def test_the_first_result_is_never_sold(self, client):
        """The card inserts second, so whatever the platform genuinely thinks
        is the best answer is what an explorer sees first - however much
        anybody has paid."""
        auth, _ = account(client, "hopeful")
        business_id = make_business(client, auth)

        marker = f"tizita{uuid.uuid4().hex[:6]}"
        for _ in range(3):
            publish(client, auth, marker=marker)

        organic_first = search(client, marker)[0]["id"]
        promote_lowest(client, auth, business_id, marker)

        after = search(client, marker)
        assert after[0]["id"] == organic_first, "a promotion must not take first place"
        assert after[0]["sponsored"] is False
        assert after[1]["sponsored"] is True, "and it should be immediately below"

    def test_it_does_not_reorder_the_other_results(self, client):
        """Sponsorship is a slot beside the ranking, not a weight on it.

        The one listing that was paid for moves; everything else keeps its
        order relative to everything else. If money could push a third party
        down a place, it would be deciding the ranking after all.
        """
        auth, _ = account(client, "steady")
        business_id = make_business(client, auth)

        marker = f"shashe{uuid.uuid4().hex[:6]}"
        for _ in range(4):
            publish(client, auth, marker=marker)

        before = [r["id"] for r in search(client, marker)]
        promoted = before[-1]

        buy_slot(client, auth, business_id, promoted)

        after = [r["id"] for r in search(client, marker) if not r["sponsored"]]
        assert after == [i for i in before if i != promoted], "the organic order changed"

    def test_a_promotion_cannot_bypass_a_requirement(self, client):
        """The one that matters most.

        A hard requirement excludes unknown along with contradicted - "we do not
        know whether this kitchen can do nut-free" is not a maybe worth showing
        somebody with a nut allergy. Money is not a reason to make an exception,
        so a promoted listing that has not made the claim must not appear even
        though somebody paid for it to.
        """
        auth, _ = account(client, "unclaimed")
        business_id = make_business(client, auth)

        marker = f"gebeta{uuid.uuid4().hex[:6]}"
        # One listing that has claimed it, so the search is not empty.
        publish(client, auth, marker=marker, suitability=["nut_free"])
        # And the promoted one, which has said nothing about nuts.
        promoted = publish(client, auth, marker=marker)
        buy_slot(client, auth, business_id, promoted)

        results = search(client, marker, requires="nut_free")
        assert promoted not in [r["id"] for r in results], (
            "a paid slot must not carry a listing past a hard requirement"
        )

    def test_it_still_appears_when_it_does_meet_the_requirement(self, client):
        """The counterpart, so the test above is not passing because
        requirements switch promotions off entirely."""
        auth, _ = account(client, "claimed")
        business_id = make_business(client, auth)

        marker = f"doro{uuid.uuid4().hex[:6]}"
        publish(client, auth, marker=marker, suitability=["nut_free"])
        publish(client, auth, marker=marker, suitability=["nut_free"])
        promoted = promote_lowest(client, auth, business_id, marker)

        results = search(client, marker, requires="nut_free")
        assert promoted in [r["id"] for r in results]

    def test_it_does_not_reach_another_city(self, client):
        auth, _ = account(client, "local")
        business_id = make_business(client, auth)

        marker = f"kefet{uuid.uuid4().hex[:6]}"
        publish(client, auth, marker=marker)
        promoted = publish(client, auth, marker=marker)
        buy_slot(client, auth, business_id, promoted)

        elsewhere = client.get(
            "/api/v1/search", params={"q": marker, "city": "nairobi"}
        ).json()["data"]["results"]
        assert not [r for r in elsewhere if r["sponsored"]]

    def test_an_unpaid_promotion_shows_nothing(self, client):
        """A promotion that ran while the payment page was open would be free
        advertising for anybody who opened one."""
        auth, _ = account(client, "freeloader")
        business_id = make_business(client, auth)

        marker = f"anchi{uuid.uuid4().hex[:6]}"
        publish(client, auth, marker=marker)
        promoted = publish(client, auth, marker=marker)

        started = client.post(
            f"/api/v1/businesses/{business_id}/promotions",
            headers=auth,
            json={
                "experienceId": promoted,
                "days": 7,
                "currency": "ETB",
                "citySlug": CITY,
            },
        )
        if started.status_code == 409:
            pytest.skip("Payments are unavailable.")
        assert started.status_code == 201

        assert not [r for r in search(client, marker) if r["sponsored"]]

    def test_a_promotion_never_appears_in_a_search_it_does_not_match(self, client):
        """A promotion buys position, never relevance.

        The strongest of the rules, and the one that keeps this from being
        advertising: a paid slot may lift a listing the explorer's own query
        already found and ranked low, and may never introduce one the query did
        not find at all. Without it a campaign for a nightclub turns up under a
        search for a quiet cafe, which inverts the "advertising should feel like
        discovery" principle it is sold under.
        """
        auth, _ = account(client, "irrelevant")
        business_id = make_business(client, auth)

        mine = f"birabiro{uuid.uuid4().hex[:6]}"
        publish(client, auth, marker=mine)
        publish(client, auth, marker=mine)
        promote_lowest(client, auth, business_id, mine)

        # A different search entirely, with its own results to sit in.
        theirs = f"lekim{uuid.uuid4().hex[:6]}"
        publish(client, auth, marker=theirs)
        publish(client, auth, marker=theirs)

        results = search(client, theirs)
        assert results, "the unrelated search should still return its own listings"
        assert not [r for r in results if r["sponsored"]]

    async def test_a_query_that_found_nothing_gets_no_sponsor(self):
        """"We found nothing, but here is somebody who paid" is the worst
        possible answer to a search, and a page whose only content is an
        advertisement is not discovery.

        Asserted directly rather than by searching for gibberish: semantic
        retrieval answers a nonsense query with loose matches rather than with
        nothing, so an HTTP test cannot reliably produce the empty case it means
        to describe. The guard returns before touching the session, which is
        what passing something unusable proves.
        """

        class ExplodingSession:
            def __getattr__(self, name):  # noqa: ANN001
                raise AssertionError(f"the session was used ({name}) for an empty query")

        chosen = await promotion_service.PromotionService(ExplodingSession()).slot_for(
            area=Area(city_slug=CITY), eligible_ids=set()
        )
        assert chosen is None

    def test_a_promoted_listing_appears_once(self, client):
        """It is *moved* into the paid slot, never copied into it.

        A listing that ranked seventh and appears in the sponsored slot must not
        also still be seventh: the explorer would see the same thing twice and
        the publisher would have bought a duplicate.
        """
        auth, _ = account(client, "duplicate")
        business_id = make_business(client, auth)

        marker = f"yene{uuid.uuid4().hex[:6]}"
        for _ in range(4):
            publish(client, auth, marker=marker)
        promoted = promote_lowest(client, auth, business_id, marker)

        ids = [r["id"] for r in search(client, marker)]
        assert ids.count(promoted) == 1, ids

    def test_nothing_is_sold_to_whatever_is_already_first(self, client):
        """There is no position to buy, and a label would only cost them
        credibility."""
        auth, _ = account(client, "already-top")
        business_id = make_business(client, auth)

        marker = f"bekur{uuid.uuid4().hex[:6]}"
        publish(client, auth, marker=marker)
        top = search(client, marker)[0]["id"]

        buy_slot(client, auth, business_id, top)

        results = search(client, marker)
        assert results[0]["id"] == top
        assert not [r for r in results if r["sponsored"]]

    def test_a_draft_cannot_be_promoted(self, client):
        auth, _ = account(client, "premature")
        business_id = make_business(client, auth)

        draft = client.post(
            "/api/v1/posts",
            headers=auth,
            json={
                "title": f"Unpublished {uuid.uuid4().hex[:5]}",
                "description": "Still a draft, and not for sale as a slot.",
                "citySlug": CITY,
                "type": "place",
            },
        )
        assert draft.status_code == 201

        refused = client.post(
            f"/api/v1/businesses/{business_id}/promotions",
            headers=auth,
            json={
                "experienceId": draft.json()["data"]["id"],
                "days": 7,
                "currency": "ETB",
                "citySlug": CITY,
            },
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "EXPERIENCE_NOT_PUBLISHED"

    def test_somebody_elses_listing_cannot_be_promoted(self, client):
        mine_auth, _ = account(client, "owner")
        business_id = make_business(client, mine_auth)

        theirs_auth, _ = account(client, "stranger")
        make_business(client, theirs_auth)
        theirs = publish(client, theirs_auth, marker=f"other{uuid.uuid4().hex[:6]}")

        refused = client.post(
            f"/api/v1/businesses/{business_id}/promotions",
            headers=mine_auth,
            json={
                "experienceId": theirs,
                "days": 7,
                "currency": "ETB",
                "citySlug": CITY,
            },
        )
        # Not found rather than forbidden: confirming a listing exists to
        # somebody guessing ids tells them they guessed right.
        assert refused.status_code == 404, refused.text


class TestTheConciergeIsNotForSale:
    def test_the_concierge_returns_nothing_sponsored(self, client):
        """The gateway phrases facts that came back from tools, so a paid item
        in that stream becomes the assistant's own recommendation with no label
        surviving generation. BUSINESS-90.01 §8 permits sponsored AI
        recommendations; Mado has no mechanism that keeps the label attached
        through a model, so it does not do it.
        """
        auth, _ = account(client, "asker")
        business_id = make_business(client, auth)

        marker = f"melody{uuid.uuid4().hex[:6]}"
        publish(client, auth, marker=marker)
        publish(client, auth, marker=marker)
        promote_lowest(client, auth, business_id, marker)

        reply = client.post(
            "/api/v1/assistant/messages",
            headers=auth,
            json={
                "message": f"anything on with {marker}?",
                "latitude": 9.0055,
                "longitude": 38.7810,
            },
        )
        assert reply.status_code == 200, reply.text

        cards = reply.json()["data"].get("results") or []
        # It found the listings - so this is not passing because the concierge
        # returned nothing at all.
        assert cards, "the concierge found nothing, so this proves nothing"
        assert not [c for c in cards if c.get("sponsored")], (
            "a promotion reached the concierge, where its label cannot survive"
        )


class TestBuyingIsPrivileged:
    def test_an_editor_cannot_spend_on_promotion(self, client):
        owner_auth, _ = account(client, "owner")
        business_id = make_business(client, owner_auth)
        listing = publish(client, owner_auth, marker=f"edit{uuid.uuid4().hex[:6]}")

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

        refused = client.post(
            f"/api/v1/businesses/{business_id}/promotions",
            headers=editor_auth,
            json={
                "experienceId": listing,
                "days": 7,
                "currency": "ETB",
                "citySlug": CITY,
            },
        )
        assert refused.status_code == 403, refused.text

    def test_the_run_length_is_bounded(self, client):
        auth, _ = account(client, "forever")
        business_id = make_business(client, auth)
        listing = publish(client, auth, marker=f"long{uuid.uuid4().hex[:6]}")

        for days in (0, MAX_DAYS + 1):
            refused = client.post(
                f"/api/v1/businesses/{business_id}/promotions",
                headers=auth,
                json={
                    "experienceId": listing,
                    "days": days,
                    "currency": "ETB",
                    "citySlug": CITY,
                },
            )
            assert refused.status_code == 422, f"{days} days was accepted"

        assert MIN_DAYS >= 1


class TestReporting:
    def test_an_impression_is_recorded(self, client):
        """Counted for the publisher's report and used for nothing else - a
        counter that fed back into placement would be a reason to show a listing
        to somebody it does not suit."""
        auth, _ = account(client, "reporter")
        business_id = make_business(client, auth)

        marker = f"count{uuid.uuid4().hex[:6]}"
        publish(client, auth, marker=marker)
        publish(client, auth, marker=marker)
        promote_lowest(client, auth, business_id, marker)

        search(client, marker)
        search(client, marker)

        listed = client.get(
            f"/api/v1/businesses/{business_id}/promotions", headers=auth
        ).json()["data"]
        assert listed[0]["impressions"] >= 2, listed
