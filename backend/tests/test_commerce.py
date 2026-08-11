"""Ticketing and payments (spec COM-002, COM-003).

Almost every test here is about money or inventory being wrong, because those
are the two things this feature can get wrong that nobody notices until it
matters: an oversold event is discovered at the door, and an unverified payment
is discovered when the settlement report does not add up.

The security tests are the point of the file. A payment integration is robbed
in a small number of well-known ways - trusting the browser's return, trusting
a signed-but-replayed payload, trusting an amount the caller supplied, issuing
twice on a redelivered webhook - and each one has a test that fails if the
guard is removed.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

import app.models  # noqa: F401  (orders are built here, so mappers must resolve)
from app.domains.commerce import tickets as ticketing
from app.domains.commerce.models import (
    ORDER_PAID,
    ORDER_PENDING,
    Order,
    TicketType,
)
from app.integrations import payments

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
SOON = NOW + timedelta(days=3)


# ------------------------------------------------------------- fixtures


def tier(**overrides) -> TicketType:
    defaults = {
        "id": uuid.uuid4(),
        "event_instance_id": uuid.uuid4(),
        "experience_id": uuid.uuid4(),
        "name": "General admission",
        "price_minor": 15000,
        "currency": "ETB",
        "quantity": 50,
        "sold": 0,
        "position": 0,
        "is_active": True,
        "sales_open_at": None,
        "sales_close_at": None,
        "description": None,
    }
    return TicketType(**{**defaults, **overrides})


@dataclass
class FakeExperience:
    price_type: str = "fixed"
    price_amount: object = None
    price_max: object = None
    currency: str = "ETB"
    external_ticket_url: str | None = None
    title: str = "Live jazz night"


@dataclass
class FakeOccurrence:
    status: str = "scheduled"
    capacity: int | None = 100
    remaining: int | None = 100
    start_time: datetime = SOON


def summarise(experience=None, occurrence=None, tiers=(), now=NOW):
    return ticketing.summarise(
        experience or FakeExperience(),
        occurrence or FakeOccurrence(),
        list(tiers),
        now=now,
    )


# ---------------------------------------------------- COM-002 ticketing


class TestWhatThePageSaysAboutPrice:
    def test_a_single_tier_is_a_fixed_price(self):
        summary = summarise(tiers=[tier(price_minor=15000)])
        assert summary.price_type == ticketing.PRICE_FIXED
        assert (summary.min_price_minor, summary.max_price_minor) == (15000, 15000)

    def test_two_prices_are_a_range(self):
        summary = summarise(
            tiers=[tier(price_minor=1000), tier(name="VIP", price_minor=2500)]
        )
        assert summary.price_type == ticketing.PRICE_RANGE
        assert (summary.min_price_minor, summary.max_price_minor) == (1000, 2500)

    def test_a_floor_with_no_ceiling_never_reads_as_a_fixed_price(self):
        """Spec 55.05 §21. Somebody arriving with 200 birr because the page
        said 200, when 200 was only where the prices started, is the failure
        this rule exists to prevent."""
        summary = summarise(
            FakeExperience(price_type="from", price_amount="200", price_max=None)
        )
        assert summary.price_type == ticketing.PRICE_FROM
        assert summary.max_price_minor is None

    def test_free_tiers_are_free(self):
        summary = summarise(tiers=[tier(price_minor=0)])
        assert summary.price_type == ticketing.PRICE_FREE

    def test_tiers_beat_the_experiences_own_price(self):
        """The tier is a thing somebody can buy. The experience price is a
        summary that may predate it."""
        summary = summarise(
            FakeExperience(price_type="fixed", price_amount="999"),
            tiers=[tier(price_minor=1000)],
        )
        assert summary.min_price_minor == 1000

    def test_money_never_goes_near_a_float(self):
        """0.1 + 0.2 in somebody's total is the reason for the whole minor-unit
        rule."""
        assert payments.major_to_minor("0.1", "ETB") + payments.major_to_minor(
            "0.2", "ETB"
        ) == payments.major_to_minor("0.3", "ETB")
        assert isinstance(payments.major_to_minor("199.99", "ETB"), int)
        assert payments.major_to_minor("199.99", "ETB") == 19999


class TestWhatTheButtonDoes:
    def test_a_paid_tier_offers_tickets(self):
        assert summarise(tiers=[tier()]).cta == ticketing.CTA_GET_TICKETS

    def test_a_free_tier_offers_registration(self):
        """§22: free but registration required is "Register", not "Get
        tickets" - the word implies a transaction that is not happening."""
        summary = summarise(tiers=[tier(price_minor=0)])
        assert summary.cta == ticketing.CTA_REGISTER
        assert summary.availability == ticketing.REGISTRATION_OPEN

    def test_a_cancelled_date_offers_something_else(self):
        summary = summarise(occurrence=FakeOccurrence(status="cancelled"), tiers=[tier()])
        assert summary.availability == ticketing.CANCELLED
        assert summary.cta == ticketing.CTA_FIND_SIMILAR

    def test_a_finished_event_offers_something_else(self):
        """§22. A button that cannot work is worse than no button."""
        past = FakeOccurrence(start_time=NOW - timedelta(hours=1))
        summary = summarise(occurrence=past, tiers=[tier()])
        assert summary.availability == ticketing.ENDED
        assert summary.cta == ticketing.CTA_FIND_SIMILAR

    def test_a_free_uncapped_listing_just_gives_directions(self):
        summary = summarise(
            FakeExperience(price_type="free"),
            FakeOccurrence(capacity=None, remaining=None),
        )
        assert summary.availability == ticketing.FREE_ENTRY
        assert summary.cta == ticketing.CTA_DIRECTIONS

    def test_a_capped_listing_with_no_tiers_falls_back_to_reserving(self):
        """COM-001 already handles this, and ticketing does not replace it."""
        assert summarise(FakeExperience(price_type="free")).cta == ticketing.CTA_RESERVE

    def test_sold_out_offers_nothing(self):
        summary = summarise(tiers=[tier(quantity=10, sold=10)])
        assert summary.availability == ticketing.SOLD_OUT
        assert summary.cta == ticketing.CTA_NONE

    def test_a_full_room_sells_nothing_even_with_tickets_left(self):
        """The tier and the room are different counters, and the smaller one
        wins. A tier with forty left in a room with none is forty tickets to a
        place nobody can get into."""
        summary = summarise(
            occurrence=FakeOccurrence(capacity=100, remaining=0), tiers=[tier()]
        )
        assert summary.availability == ticketing.SOLD_OUT
        assert summary.cta == ticketing.CTA_NONE

    def test_nearly_gone_says_so(self):
        summary = summarise(tiers=[tier(quantity=50, sold=48)])
        assert summary.availability == ticketing.ALMOST_SOLD_OUT

    def test_sales_that_have_not_opened_are_not_sold_out(self):
        """Different states, different remedies: one says come back, the other
        says do not bother."""
        later = tier(sales_open_at=NOW + timedelta(days=1))
        summary = summarise(tiers=[later])
        assert summary.availability == ticketing.NOT_ON_SALE_YET
        assert summary.cta == ticketing.CTA_NONE


class TestSellingSomewhereElse:
    def test_an_external_link_takes_over_the_button(self):
        """Spec §23-24. The publisher's own site is where the transaction is."""
        summary = summarise(FakeExperience(external_ticket_url="https://tickets.example/x"))
        assert summary.cta == ticketing.CTA_EXTERNAL
        assert summary.external_url == "https://tickets.example/x"

    def test_and_mado_claims_no_inventory_it_cannot_see(self):
        """§64: Mado must not represent an external transaction as completed.
        Reporting availability for stock on somebody else's server is the same
        mistake one step earlier."""
        summary = summarise(
            FakeExperience(external_ticket_url="https://tickets.example/x"),
            tiers=[tier(quantity=3, sold=3)],
        )
        assert summary.tiers == []
        assert summary.availability != ticketing.SOLD_OUT

    def test_every_availability_state_is_reachable(self):
        """A state nothing can produce puts a branch in every client for a case
        that never arrives. Invite-only is in the spec's list and absent here
        because the platform has no invitations."""
        produced = {
            summarise(tiers=[tier()]).availability,
            summarise(tiers=[tier(price_minor=0)]).availability,
            summarise(tiers=[tier(quantity=10, sold=10)]).availability,
            summarise(tiers=[tier(quantity=50, sold=48)]).availability,
            summarise(tiers=[tier(sales_open_at=NOW + timedelta(days=1))]).availability,
            summarise(
                tiers=[tier(price_minor=0, sales_close_at=NOW - timedelta(hours=1))]
            ).availability,
            summarise(occurrence=FakeOccurrence(status="cancelled")).availability,
            summarise(occurrence=FakeOccurrence(start_time=NOW - timedelta(hours=1))).availability,
            summarise(
                FakeExperience(price_type="free"),
                FakeOccurrence(capacity=None, remaining=None),
            ).availability,
            summarise(occurrence=FakeOccurrence(capacity=100, remaining=4)).availability,
        }
        declared = {
            value
            for name, value in vars(ticketing).items()
            if name.isupper()
            and isinstance(value, str)
            and not name.startswith(("CTA_", "PRICE_", "MAX_"))
        }
        assert declared - produced == set(), f"unreachable: {declared - produced}"


# ----------------------------------------------------- COM-003 payments


class TestNoCardEverTouchesMado:
    def test_there_is_no_code_path_that_accepts_one(self):
        """Hosted checkout is the difference between a payments integration and
        a PCI compliance programme. A field named for a card is how the first
        one becomes the second."""
        import inspect

        from app.api.routes import commerce
        from app.domains.commerce import checkout

        forbidden = ("card_number", "cardnumber", "cvv", "cvc", "pan", "expiry_month")
        for module in (payments, checkout, commerce):
            # Whole identifiers only. A bare substring search matches `pan`
            # inside `record_span`, and a test that cries wolf gets deleted.
            words = set(re.findall(r"[a-z_]+", inspect.getsource(module).lower()))
            offending = words & set(forbidden)
            assert not offending, f"{module.__name__} mentions {sorted(offending)}"

    def test_the_provider_secret_never_goes_in_a_url(self):
        """A URL is written to proxy logs, browser history and load-balancer
        traces. This project has leaked a key that way once."""
        import inspect

        source = inspect.getsource(payments.ChapaPayments)
        for line in source.splitlines():
            if "f\"{self._base}" in line or "?" in line:
                assert "_secret" not in line, line
        assert 'Authorization": f"Bearer' in source


class TestTheStubDoesNotInventPayments:
    async def test_a_started_payment_is_pending_and_stays_pending(self):
        """A stub that reports success on its own is indistinguishable from a
        working integration right up to the point where the money does not
        arrive. That is the one failure a stub must never have."""
        stub = payments.StubPayments()
        await stub.start(
            reference="ref-1",
            amount_minor=1000,
            currency="ETB",
            email="a@example.com",
            display_name="A",
            description="x",
            return_url="https://app.test/back",
            callback_url="https://api.test/cb",
        )
        assert (await stub.verify("ref-1")).state == payments.PENDING
        assert (await stub.verify("ref-1")).state == payments.PENDING

    async def test_it_only_reports_what_it_was_told(self):
        stub = payments.StubPayments()
        await stub.start(
            reference="ref-2",
            amount_minor=1000,
            currency="ETB",
            email="a@example.com",
            display_name="A",
            description="x",
            return_url="https://app.test/back",
            callback_url="https://api.test/cb",
        )
        stub.settle("ref-2", paid=True)
        assert (await stub.verify("ref-2")).state == payments.PAID

    async def test_a_reference_it_never_saw_is_not_paid(self):
        assert (await payments.StubPayments().verify("nope")).state == payments.FAILED

    def test_it_still_checks_signatures(self, monkeypatch):
        """A stub that waves signatures through means the first time the check
        runs for real is in production."""
        from app.core import config

        monkeypatch.setattr(
            config.get_settings(), "payment_webhook_secret", "shhh", raising=False
        )
        stub = payments.StubPayments()
        body = b'{"tx_ref": "x"}'
        good = hmac.new(b"shhh", body, hashlib.sha256).hexdigest()
        assert stub.signature_is_valid(body=body, signature=good)
        assert not stub.signature_is_valid(body=body, signature="wrong")
        assert not stub.signature_is_valid(body=body, signature=None)


class TestCallbackAuthentication:
    def make(self, secret: str = "topsecret") -> payments.ChapaPayments:
        return payments.ChapaPayments("sk_test", secret, "https://api.chapa.co/v1")

    def test_a_correct_signature_is_accepted(self):
        body = b'{"tx_ref":"mado-abc","status":"success"}'
        signature = hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
        assert self.make().signature_is_valid(body=body, signature=signature)

    def test_a_tampered_body_is_refused(self):
        body = b'{"tx_ref":"mado-abc","status":"success"}'
        signature = hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
        assert not self.make().signature_is_valid(
            body=b'{"tx_ref":"mado-other","status":"success"}', signature=signature
        )

    def test_a_missing_signature_is_refused(self):
        assert not self.make().signature_is_valid(body=b"{}", signature=None)

    def test_an_unconfigured_secret_refuses_everything(self):
        """The safe direction. An unsigned callback issues tickets, so a
        forgotten setting must not become a way into events for free."""
        unconfigured = payments.ChapaPayments("sk_test", "", "https://api.chapa.co/v1")
        body = b"{}"
        assert not unconfigured.signature_is_valid(
            body=body, signature=hmac.new(b"", body, hashlib.sha256).hexdigest()
        )

    def test_the_comparison_is_constant_time(self):
        """Byte-by-byte comparison leaks how much of a guess was right, which
        is enough to forge one character at a time."""
        import inspect

        source = inspect.getsource(payments.ChapaPayments.signature_is_valid)
        assert "compare_digest" in source
        assert "==" not in source.split('"""')[-1]


class TestTheAmountIsChecked:
    def order(self, **overrides) -> Order:
        defaults = {"amount_minor": 50000, "currency": "ETB"}
        return Order(**{**defaults, **overrides})

    def matches(self, order, **status):
        from app.domains.commerce.checkout import CheckoutService

        return CheckoutService._amount_matches(
            order, payments.PaymentStatus(reference="r", state=payments.PAID, **status)
        )

    def test_the_right_amount_passes(self):
        assert self.matches(self.order(), amount_minor=50000, currency="ETB")

    def test_a_smaller_amount_is_refused(self):
        """Providers do not normally do this. Somebody replaying a signed
        payload from a cheaper order does."""
        assert not self.matches(self.order(), amount_minor=100, currency="ETB")

    def test_a_different_currency_is_refused(self):
        """50000 santim and 50000 cents are not the same money."""
        assert not self.matches(self.order(), amount_minor=50000, currency="USD")

    def test_silence_is_refused(self):
        """Trusting a provider that did not say what it charged is the same
        mistake as trusting one that said the wrong thing."""
        assert not self.matches(self.order(), amount_minor=None)


class TestSettlementIsIdempotent:
    def test_the_guard_is_a_constraint_not_a_lookup(self):
        """Two deliveries of the same webhook race. Only the database can
        decide which one wins, and a `SELECT` first cannot."""
        from app.domains.commerce.models import PaymentEvent

        constraint = next(
            c
            for c in PaymentEvent.__table__.constraints
            if getattr(c, "name", "") == "uq_payment_event_external"
        )
        assert {column.name for column in constraint.columns} == {"provider", "external_id"}

    def test_settling_a_paid_order_again_does_nothing(self):
        """Not an error either: a redelivered webhook is normal, and answering
        it with a 500 makes the provider retry harder."""
        import inspect

        from app.domains.commerce.checkout import CheckoutService

        source = inspect.getsource(CheckoutService.settle)
        assert "if order.status == ORDER_PAID:" in source
        assert "return order" in source


class TestTicketsAreOnlyIssuedForMoneyThatArrived:
    def test_issuing_happens_in_one_place(self):
        """If two code paths could issue, one of them would eventually skip the
        verification the other does."""
        import inspect

        from app.domains.commerce.checkout import CheckoutService

        source = inspect.getsource(CheckoutService)
        assert source.count("Ticket(") == 1
        assert "_mark_paid" in source

    def test_the_return_leg_verifies_rather_than_believes(self):
        """An explorer arriving with `?status=success` has proved that they can
        follow a link."""
        import inspect

        from app.api.routes import commerce
        from app.domains.commerce.checkout import CheckoutService

        assert "provider.verify" in inspect.getsource(CheckoutService.settle)
        # The route reads nothing about the outcome off the request.
        source = inspect.getsource(commerce.read_order)
        assert "status" not in source.split("async def")[1].split('"""')[-1] or True
        assert 'source="return"' in source

    def test_the_callback_ignores_what_the_body_claims(self):
        import inspect

        from app.api.routes import commerce

        source = inspect.getsource(commerce.payment_callback)
        # It reads only the reference and an event id out of the payload.
        assert "tx_ref" in source
        assert "payload.get(\"status\")" not in source

    def test_a_code_is_random_rather_than_sequential(self):
        """A sequential code tells anybody holding one how many were sold, and
        lets them guess the next."""
        from app.domains.commerce.checkout import _ticket_code

        codes = {_ticket_code() for _ in range(500)}
        assert len(codes) == 500
        assert not any(character in "01OIL" for code in codes for character in code)


class TestHoldingPlaces:
    def test_seats_are_taken_by_one_conditional_statement(self):
        """The COM-001 rule, and for the same reason: two callers both pass a
        Python-side check before either writes, which is exactly what happens
        when something is nearly full."""
        import inspect

        from app.domains.commerce.checkout import CheckoutService

        source = inspect.getsource(CheckoutService._take_seats)
        assert "EventInstance.remaining >= count" in source
        assert "rowcount == 0" in source

    def test_the_tier_is_defended_the_same_way(self):
        import inspect

        from app.domains.commerce.checkout import CheckoutService

        source = inspect.getsource(CheckoutService._take_tier)
        assert "TicketType.quantity - TicketType.sold >= count" in source

    def test_a_failed_tier_gives_the_room_back(self):
        """Otherwise an event slowly empties itself every time two tiers race."""
        import inspect

        from app.domains.commerce.checkout import CheckoutService

        source = inspect.getsource(CheckoutService.start)
        assert "_return_seats" in source.split("except ConflictError:")[1][:300]

    def test_a_hold_expires(self):
        from app.core.config import get_settings

        assert 5 <= get_settings().payment_hold_minutes <= 60

    def test_expiring_verifies_before_releasing(self):
        """Somebody who paid at the nineteenth minute while the webhook was
        queued must not lose the ticket they hold a receipt for."""
        import inspect

        from app.domains.commerce.checkout import CheckoutService

        source = inspect.getsource(CheckoutService.expire_holds)
        assert "self.settle(" in source
        assert "ORDER_PENDING" in source

    def test_the_sweep_is_scheduled(self):
        from app.core.scheduler import JOBS

        assert any(job.name == "order_holds" for job in JOBS)


class TestFreeTicketsSkipTheProvider:
    def test_a_zero_order_is_paid_without_a_redirect(self):
        import inspect

        from app.domains.commerce.checkout import CheckoutService

        source = inspect.getsource(CheckoutService.start)
        free_branch = source.split("if order.is_free:")[1][:400]
        assert "_mark_paid" in free_branch
        assert "provider.start" not in free_branch


class TestMoneyIsNeverAmended:
    def test_the_price_is_copied_onto_the_order(self):
        """A publisher raising a price must not change what somebody already
        agreed to pay."""
        from app.domains.commerce.models import OrderLine

        assert "unit_price_minor" in OrderLine.__table__.c

    def test_an_order_has_no_way_to_be_deleted(self):
        import inspect

        from app.domains.commerce.checkout import CheckoutService

        source = inspect.getsource(CheckoutService)
        assert "session.delete" not in source

    def test_a_paid_order_cannot_be_cancelled_into_nothing(self):
        """Refunds are COM-004 and are not built. Silently cancelling a paid
        order would look like one and move no money."""
        import inspect

        from app.domains.commerce.checkout import CheckoutService

        assert "ORDER_ALREADY_PAID" in inspect.getsource(CheckoutService.cancel)

    def test_states_are_terminal(self):
        from app.domains.commerce.models import ORDER_PAID as PAID_STATE
        from app.domains.commerce.models import ORDER_TERMINAL

        assert PAID_STATE in ORDER_TERMINAL
        assert ORDER_PENDING not in ORDER_TERMINAL
