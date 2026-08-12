"""Stripe (spec COM-003, vendor 82.01 "Payments (Global)").

Built and switched off. `MADO_STRIPE_ENABLED` is false, so the router never
reaches for it and every order still goes to Chapa or the stub - turning it on
later is a deployment decision rather than a code change. That makes these tests
the only thing standing between the adapter and its first real charge, so they
cover the parts that are wrong quietly.

Three of those are specific to Stripe. Amounts are already in the smallest
currency unit, so the conversion that Chapa needs would multiply every price by
a hundred here. Verification is against the session id rather than our own
reference, so losing it means never being able to confirm a payment. And the
signature covers a timestamp, so checking only the digest accepts a replay -
which for a settlement webhook means issuing the tickets twice.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from app.integrations import payments
from app.integrations.payments import (
    FAILED,
    PAID,
    PENDING,
    ChapaPayments,
    StripePayments,
    StubPayments,
    major_to_minor,
    minor_to_major,
)

pytestmark = pytest.mark.anyio

SECRET = "whsec_test_secret"


def stripe(webhook_secret: str = SECRET) -> StripePayments:
    return StripePayments("sk_test_x", webhook_secret, "https://api.stripe.com/v1")


def signed(body: bytes, *, secret: str = SECRET, at: int | None = None) -> str:
    timestamp = at if at is not None else int(time.time())
    digest = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


class TestTheSignatureCoversTheTimestamp:
    def test_a_correct_signature_is_accepted(self):
        body = b'{"id":"evt_1","type":"checkout.session.completed"}'
        assert stripe().signature_is_valid(body=body, signature=signed(body))

    def test_a_tampered_body_is_refused(self):
        body = b'{"id":"evt_1"}'
        assert not stripe().signature_is_valid(
            body=b'{"id":"evt_2"}', signature=signed(body)
        )

    def test_a_replay_from_last_month_is_refused(self):
        """The reason the timestamp is signed at all. A captured payload stays
        cryptographically valid forever, and replaying a settlement issues the
        tickets again."""
        body = b'{"id":"evt_1"}'
        old = int(time.time()) - 60 * 60 * 24 * 30
        assert not stripe().signature_is_valid(body=body, signature=signed(body, at=old))

    def test_a_signature_from_just_now_is_inside_the_window(self):
        body = b'{"id":"evt_1"}'
        recent = int(time.time()) - 30
        assert stripe().signature_is_valid(body=body, signature=signed(body, at=recent))

    def test_a_future_timestamp_is_refused_too(self):
        """Clock skew cuts both ways, and an attacker controls the timestamp
        they send."""
        body = b'{"id":"evt_1"}'
        ahead = int(time.time()) + 60 * 60
        assert not stripe().signature_is_valid(body=body, signature=signed(body, at=ahead))

    def test_the_wrong_secret_is_refused(self):
        body = b'{"id":"evt_1"}'
        assert not stripe().signature_is_valid(
            body=body, signature=signed(body, secret="whsec_someone_elses")
        )

    def test_a_malformed_header_is_refused_rather_than_raising(self):
        body = b"{}"
        for header in ("", "garbage", "t=,v1=", "v1=abc", "t=notanumber,v1=abc"):
            assert not stripe().signature_is_valid(body=body, signature=header), header

    def test_no_configured_secret_refuses_everything(self):
        """The safe direction, as with Chapa: an unauthenticated callback issues
        tickets, so a forgotten setting must not become free entry."""
        body = b"{}"
        assert not stripe("").signature_is_valid(body=body, signature=signed(body, secret=""))

    def test_the_comparison_is_constant_time(self):
        import inspect

        assert "compare_digest" in inspect.getsource(StripePayments.signature_is_valid)


class TestAmountsAreAlreadyInTheSmallestUnit:
    def test_no_conversion_happens_on_the_way_out(self):
        """Stripe's `unit_amount` is exactly what this module carries. Adding
        the conversion Chapa needs would charge a hundred times too much."""
        import inspect

        source = inspect.getsource(StripePayments.start)
        assert "minor_to_major" not in source
        assert "str(amount_minor)" in source

    def test_nor_on_the_way_back(self):
        import inspect

        source = inspect.getsource(StripePayments.verify)
        assert "major_to_minor" not in source

    def test_a_zero_decimal_currency_is_not_multiplied(self):
        """There is no hundredth of a yen. Treating one as though there were
        makes every Japanese price a hundred times too big, and the default of
        100 would have done exactly that."""
        assert major_to_minor("500", "JPY") == 500
        assert minor_to_major(500, "JPY") == 500

    def test_an_ordinary_currency_still_has_cents(self):
        assert major_to_minor("5.00", "USD") == 500
        assert minor_to_major(500, "USD") == 5


class TestVerifyingAgainstTheSession:
    async def test_without_a_session_id_it_reports_pending_not_failed(self):
        """Failing here would release places somebody is in the middle of
        paying for. The order may simply not have reached Stripe yet."""
        status = await stripe().verify("mado-abc")
        assert status.state == PENDING

    async def test_the_session_id_is_kept_from_the_start(self):
        import inspect

        source = inspect.getsource(StripePayments.start)
        assert "provider_reference=" in source

    async def test_a_paid_session_is_paid(self, monkeypatch):
        provider = stripe()

        async def fake_call(*_args, **_kwargs):
            return {
                "id": "cs_test_1",
                "payment_status": "paid",
                "status": "complete",
                "amount_total": 19999,
                "currency": "usd",
                "payment_intent": "pi_1",
            }

        monkeypatch.setattr(provider, "_call", fake_call)
        status = await provider.verify("mado-abc", provider_reference="cs_test_1")
        assert status.state == PAID
        assert status.amount_minor == 19999
        assert status.currency == "USD"
        assert status.provider_reference == "pi_1"

    async def test_an_unpaid_session_is_pending(self, monkeypatch):
        provider = stripe()

        async def fake_call(*_args, **_kwargs):
            return {"id": "cs_1", "payment_status": "unpaid", "status": "open"}

        monkeypatch.setattr(provider, "_call", fake_call)
        assert (await provider.verify("r", provider_reference="cs_1")).state == PENDING

    async def test_an_expired_session_has_failed(self, monkeypatch):
        """It will never be paid, and saying so releases the places now instead
        of holding them until the sweep."""
        provider = stripe()

        async def fake_call(*_args, **_kwargs):
            return {"id": "cs_1", "payment_status": "unpaid", "status": "expired"}

        monkeypatch.setattr(provider, "_call", fake_call)
        assert (await provider.verify("r", provider_reference="cs_1")).state == FAILED

    async def test_an_unreachable_stripe_is_pending_rather_than_failed(self, monkeypatch):
        provider = stripe()

        async def fake_call(*_args, **_kwargs):
            return None

        monkeypatch.setattr(provider, "_call", fake_call)
        assert (await provider.verify("r", provider_reference="cs_1")).state == PENDING


class TestStartingACheckout:
    async def test_it_sends_our_reference_and_asks_for_idempotency(self, monkeypatch):
        """Retrying a create must not open a second session and charge twice."""
        provider = stripe()
        seen: dict = {}

        async def fake_call(method, path, *, form=None, idempotency_key=None):
            seen.update(method=method, path=path, form=form, key=idempotency_key)
            return {"id": "cs_1", "url": "https://checkout.stripe.com/c/pay/cs_1"}

        monkeypatch.setattr(provider, "_call", fake_call)
        checkout = await provider.start(
            reference="mado-abc",
            amount_minor=19999,
            currency="USD",
            email="a@example.com",
            display_name="A Explorer",
            description="Live jazz night",
            return_url="https://app.test/orders/1",
            callback_url="https://api.test/cb",
        )

        assert seen["key"] == "mado-abc"
        assert seen["form"]["client_reference_id"] == "mado-abc"
        assert seen["form"]["line_items[0][price_data][unit_amount]"] == "19999"
        assert seen["form"]["line_items[0][price_data][currency]"] == "usd"
        assert checkout.provider == "stripe"
        assert checkout.provider_reference == "cs_1"
        assert checkout.redirect_url.startswith("https://checkout.stripe.com/")

    async def test_a_response_with_no_url_is_an_error(self, monkeypatch):
        provider = stripe()

        async def fake_call(*_args, **_kwargs):
            return {"id": "cs_1"}

        monkeypatch.setattr(provider, "_call", fake_call)
        with pytest.raises(payments.PaymentError):
            await provider.start(
                reference="r",
                amount_minor=100,
                currency="USD",
                email="a@example.com",
                display_name="A",
                description="x",
                return_url="https://app.test/orders/1",
                callback_url="https://api.test/cb",
            )

    def test_no_card_field_appears_anywhere(self):
        """Hosted checkout is the whole reason this is not a PCI programme."""
        import inspect
        import re

        words = set(re.findall(r"[a-z_]+", inspect.getsource(StripePayments).lower()))
        assert not words & {"card_number", "cardnumber", "cvv", "cvc", "pan"}


class TestRoutingByCurrency:
    def setup_method(self):
        payments.reset_provider()

    def teardown_method(self):
        payments.reset_provider()

    def test_stripe_is_never_used_while_the_flag_is_off(self, monkeypatch):
        """The point of shipping it disabled. Nothing reaches for Stripe until
        somebody turns it on, so today's behaviour is exactly unchanged."""
        settings = payments.get_settings()
        monkeypatch.setattr(settings, "stripe_enabled", False, raising=False)
        monkeypatch.setattr(settings, "stripe_secret_key", "sk_test", raising=False)
        monkeypatch.setattr(settings, "payment_provider", "stub", raising=False)
        for currency in ("USD", "EUR", "ETB", "JPY"):
            assert not isinstance(payments.provider_for(currency), StripePayments), currency

    def test_with_the_flag_on_birr_still_goes_to_chapa(self, monkeypatch):
        settings = payments.get_settings()
        monkeypatch.setattr(settings, "stripe_enabled", True, raising=False)
        monkeypatch.setattr(settings, "stripe_secret_key", "sk_test", raising=False)
        monkeypatch.setattr(settings, "chapa_secret_key", "chapa_test", raising=False)
        assert isinstance(payments.provider_for("ETB"), ChapaPayments)

    def test_and_everything_else_goes_to_stripe(self, monkeypatch):
        settings = payments.get_settings()
        monkeypatch.setattr(settings, "stripe_enabled", True, raising=False)
        monkeypatch.setattr(settings, "stripe_secret_key", "sk_test", raising=False)
        monkeypatch.setattr(settings, "chapa_secret_key", "chapa_test", raising=False)
        for currency in ("USD", "EUR", "GBP", "JPY"):
            assert isinstance(payments.provider_for(currency), StripePayments), currency

    def test_an_unconfigured_stripe_does_not_break_checkout(self, monkeypatch):
        """Enabling the flag without a key should degrade to the stub rather
        than 500 on every purchase."""
        settings = payments.get_settings()
        monkeypatch.setattr(settings, "stripe_enabled", True, raising=False)
        monkeypatch.setattr(settings, "stripe_secret_key", "", raising=False)
        assert isinstance(payments.provider_for("USD"), StubPayments)

    def test_settlement_asks_the_provider_that_holds_the_money(self):
        """An order started on Chapa and verified against Stripe comes back
        unpaid, and its places are released while the payment sits there
        complete."""
        import inspect

        from app.domains.commerce.checkout import CheckoutService

        source = inspect.getsource(CheckoutService.settle)
        assert "provider_named(order.provider)" in source
        assert "provider_reference=order.provider_reference" in source

    def test_and_checkout_picks_by_currency(self):
        import inspect

        from app.domains.commerce.checkout import CheckoutService

        assert "provider_for(order.currency)" in inspect.getsource(CheckoutService.start)


class TestTheCallbackKnowsWhoIsCalling:
    def test_it_picks_the_provider_from_the_signature_header(self):
        """Both providers post to one endpoint with different schemes and
        different secrets. Verifying with the wrong one refuses every genuine
        callback."""
        import inspect

        from app.api.routes import commerce

        source = inspect.getsource(commerce.payment_callback)
        assert "stripe-signature" in source
        assert 'provider_named("stripe")' in source
        assert 'provider_named("chapa")' in source

    def test_it_reads_our_reference_out_of_stripes_envelope(self):
        import inspect

        from app.api.routes import commerce

        assert "client_reference_id" in inspect.getsource(commerce.payment_callback)


class TestTheInstrumentationActuallyExists:
    """The bug this class exists for.

    Both adapters recorded timings in a `finally`, calling `tracing.record_span`
    and `metrics.observe_external` - neither of which is a real function. Being
    in a `finally`, it raised AttributeError on every Chapa request, successful
    ones included. Nothing caught it because every test and every end-to-end
    check ran on the stub provider, which is the blind spot a stub creates.
    """

    def test_the_functions_the_adapters_call_are_real(self):
        from app.core import metrics, tracing

        assert hasattr(tracing, "span")
        assert hasattr(metrics, "dependency_calls")
        assert hasattr(metrics, "dependency_duration")
        assert not hasattr(tracing, "record_span")
        assert not hasattr(metrics, "observe_external")

    @pytest.mark.parametrize("adapter", [ChapaPayments, StripePayments])
    def test_no_adapter_calls_a_function_that_does_not_exist(self, adapter):
        import inspect

        # Comments are stripped first: the Chapa adapter now carries a note
        # naming the two dead functions so nobody reinstates them, and a test
        # that reads prose would fail on the explanation.
        code = "\n".join(
            line.split("#", 1)[0] for line in inspect.getsource(adapter).splitlines()
        )
        assert "record_span" not in code
        assert "observe_external" not in code

    async def test_a_failing_request_still_completes_its_finally_block(self, monkeypatch):
        """The shape of the original bug: the exception from `finally` replaced
        the real error, so even the logging was wrong."""
        import httpx

        provider = stripe()

        class NoNetwork:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                raise httpx.ConnectError("down")

            async def __aexit__(self, *_args):
                return False

        monkeypatch.setattr(payments.httpx, "AsyncClient", NoNetwork)
        # A read returns None rather than raising; the point is that it gets
        # there at all instead of dying in the finally.
        assert await provider._call("GET", "/checkout/sessions/cs_1") is None

    async def test_and_a_failing_chapa_request_raises_the_payment_error(self, monkeypatch):
        import httpx

        provider = ChapaPayments("sk", "whsec", "https://api.chapa.co/v1")

        class NoNetwork:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                raise httpx.ConnectError("down")

            async def __aexit__(self, *_args):
                return False

        monkeypatch.setattr(payments.httpx, "AsyncClient", NoNetwork)
        with pytest.raises(payments.PaymentError):
            await provider._call("GET", "/transaction/verify/x")
