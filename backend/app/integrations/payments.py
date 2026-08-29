"""Taking money, without ever handling a card (spec COM-003, vendor 82.01).

The vendor strategy names Chapa for Ethiopia and Stripe for international
payments, and the integration architecture is explicit that "payment processing
should remain isolated from business logic". So this module knows how to start
a payment and how to ask whether one happened, and nothing at all about tickets.

**Mado never sees a card number.** Every provider here is hosted-checkout: the
explorer is redirected to the provider's own page, types their details there,
and comes back. Nothing sensitive crosses this process, which is the difference
between a payments integration and a PCI compliance programme. There is
deliberately no code path that accepts a card, and adding one should be treated
as a change of business, not a feature.

**The browser coming back is not proof of payment.** A return URL is a link the
explorer's browser followed; anybody can follow it, twice, or with the query
string edited. So the return leg only tells the interface what to display, and
nothing is issued until :meth:`PaymentProvider.verify` has asked the provider
directly, or a signed webhook has arrived. This is the single most common way
payment integrations are robbed, and it is why `verify` exists on the protocol
rather than being an optional extra.

**Amounts are integers, in minor units.** ETB santim, USD cents. A price that
goes near a float acquires 0.30000000000000004 eventually, and the place it
surfaces is somebody's total. Conversion to the decimal string providers want
happens at the boundary, once, here.

Three implementations behind one protocol, matching the geocoding and routing
modules: Chapa where a key is configured, Stripe left as a named gap rather
than a half-written adapter, and a stub for development. The stub does not
invent successes - see :class:`StubPayments`.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

import httpx

from app.core import metrics, tracing
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("mado.payments")

REQUEST_TIMEOUT = 15.0

# What a payment can be, as far as Mado is concerned. Providers have richer
# vocabularies - authorised, captured, disputed - and mapping them down to
# these three keeps the states a ticket depends on small enough to reason about.
PENDING = "pending"
PAID = "paid"
FAILED = "failed"

# Minor units per major unit, by currency. Not universal - JPY has none, KWD has
# three - so the table is explicit rather than a hardcoded 100 that is wrong for
# a currency nobody tested.
MINOR_UNITS: dict[str, int] = {
    "ETB": 100,
    "USD": 100,
    "EUR": 100,
    "GBP": 100,
    "KES": 100,
    "NGN": 100,
    "ZAR": 100,
    # Zero-decimal. There is no such thing as a hundredth of a yen, and treating
    # one as though there were multiplies every Japanese price by a hundred -
    # which both Stripe and the customer would notice.
    "JPY": 1,
    "KRW": 1,
    "VND": 1,
    "UGX": 1,
    "RWF": 1,
}
DEFAULT_MINOR_UNITS = 100


class PaymentError(RuntimeError):
    """The provider could not be reached, or answered with something unusable."""


def _provider_message(response: httpx.Response) -> str:
    """The provider's own explanation of a refusal, if it gave one.

    Both Chapa and Stripe answer a rejected request with a body that names the
    problem - a field that failed validation, an amount below the minimum, a
    callback URL they will not accept. Logging only the status code turns a
    five-second fix into an afternoon, and the body is not sensitive: it
    describes the request we sent, and the credential never appears in it.
    """
    try:
        body = response.json()
    except ValueError:
        return (response.text or "")[:400]

    if isinstance(body, dict):
        for key in ("message", "error", "detail"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value[:400]
            if isinstance(value, dict):
                # Chapa nests per-field validation errors; Stripe puts its
                # message inside `error`.
                inner = value.get("message")
                if isinstance(inner, str) and inner:
                    return inner[:400]
                return str(value)[:400]
    return str(body)[:400]


def minor_to_major(amount_minor: int, currency: str) -> Decimal:
    """Santim to birr, cents to dollars. Exact, via Decimal."""
    factor = MINOR_UNITS.get(currency.upper(), DEFAULT_MINOR_UNITS)
    return (Decimal(amount_minor) / Decimal(factor)).quantize(
        Decimal(1).scaleb(-len(str(factor)) + 1), rounding=ROUND_HALF_UP
    )


def major_to_minor(amount: Decimal | str | int, currency: str) -> int:
    """The other direction, for a price a publisher typed in birr."""
    factor = MINOR_UNITS.get(currency.upper(), DEFAULT_MINOR_UNITS)
    return int((Decimal(str(amount)) * factor).quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass(frozen=True, slots=True)
class Checkout:
    """Where to send the explorer, and what the provider is calling this."""

    provider: str
    reference: str
    redirect_url: str
    # The provider's own handle for this attempt. Chapa is happy to be asked
    # about our reference; Stripe can only be asked about its session id, so it
    # has to be kept from the moment checkout starts.
    provider_reference: str | None = None


@dataclass(frozen=True, slots=True)
class PaymentStatus:
    """What the provider says about one payment, asked directly."""

    reference: str
    state: str
    amount_minor: int | None = None
    currency: str | None = None
    provider_reference: str | None = None
    # The provider's own word for it, kept for the log when a mapping surprises
    # somebody at three in the morning.
    raw_state: str = ""


class PaymentProvider(Protocol):
    name: str

    async def start(
        self,
        *,
        reference: str,
        amount_minor: int,
        currency: str,
        email: str,
        display_name: str,
        description: str,
        return_url: str,
        callback_url: str,
    ) -> Checkout: ...

    async def verify(
        self, reference: str, *, provider_reference: str | None = None
    ) -> PaymentStatus: ...

    def signature_is_valid(self, *, body: bytes, signature: str | None) -> bool: ...


# ------------------------------------------------------------------- Chapa


# What Chapa accepts in `customization.description`, in its own words: "letters,
# numbers, hyphens, underscores, spaces, and dots". Nothing else, and a request
# carrying anything else is refused outright with a 400.
_CHAPA_DESCRIPTION_ALLOWED = re.compile(r"[^A-Za-z0-9 ._-]")
# Stripped rather than spaced, because they sit inside words: "Mama's" should
# become "Mamas" and not "Mama s".
_CHAPA_DESCRIPTION_DROPPED = re.compile(r"['’\"]")


def _chapa_description(text: str) -> str:
    """Make a description Chapa will accept.

    Here rather than at the call sites, because it is a fact about Chapa and
    nowhere else - Stripe takes the same string untouched, and a caller that had
    to know this would be one caller away from forgetting.

    It matters because these descriptions carry things people typed. A promotion
    names the post it promotes, so a listing called "Mama's Kitchen: jazz &
    blues" refused the whole payment with a message about a field the publisher
    has never heard of - which is what "Payments are unavailable right now"
    turned out to mean.

    Truncated last, so a sanitised string is not cut to a hundred characters and
    then shortened again by the cleaning.
    """
    cleaned = _CHAPA_DESCRIPTION_DROPPED.sub("", text)
    cleaned = _CHAPA_DESCRIPTION_ALLOWED.sub(" ", cleaned)
    cleaned = " ".join(cleaned.split())
    # Never empty: a description of nothing but punctuation would otherwise send
    # a blank field, which Chapa also refuses.
    return cleaned[:100] or "Mado"


class ChapaPayments:
    """Chapa hosted checkout - the Ethiopian provider named in spec 82.01.

    Two facts shape this adapter. Chapa takes amounts as decimal strings in
    birr, so the conversion from santim happens on the way out. And its
    reference (`tx_ref`) is chosen by *us*, which is what makes the whole flow
    idempotent: the same order started twice carries the same reference and
    Chapa treats it as the same transaction rather than charging twice.

    The secret travels in an Authorization header. Never a query string - a URL
    is written to proxy logs, browser history and load-balancer traces, so a
    credential in one should be considered published.
    """

    name = "chapa"

    def __init__(self, secret_key: str, webhook_secret: str, base_url: str) -> None:
        self._secret = secret_key
        self._webhook_secret = webhook_secret
        self._base = base_url.rstrip("/")

    async def start(
        self,
        *,
        reference: str,
        amount_minor: int,
        currency: str,
        email: str,
        display_name: str,
        description: str,
        return_url: str,
        callback_url: str,
    ) -> Checkout:
        first, _, last = display_name.partition(" ")
        payload = {
            "amount": str(minor_to_major(amount_minor, currency)),
            "currency": currency.upper(),
            "email": email,
            "first_name": first or "Explorer",
            "last_name": last or "-",
            "tx_ref": reference,
            "callback_url": callback_url,
            "return_url": return_url,
            "customization": {
                "title": "Mado",
                "description": _chapa_description(description),
            },
        }
        data = await self._post("/transaction/initialize", payload)
        url = (data.get("data") or {}).get("checkout_url")
        if not url:
            raise PaymentError("Chapa did not return a checkout URL.")
        return Checkout(provider=self.name, reference=reference, redirect_url=url)

    async def verify(
        self, reference: str, *, provider_reference: str | None = None
    ) -> PaymentStatus:
        # Chapa is asked about our own tx_ref, so the provider's handle is not
        # needed here. Accepted for the protocol's sake.
        data = await self._get(f"/transaction/verify/{reference}")
        body = data.get("data") or {}
        raw = str(body.get("status") or data.get("status") or "").lower()
        state = {"success": PAID, "failed": FAILED, "pending": PENDING}.get(raw, PENDING)
        currency = body.get("currency") or "ETB"
        amount = body.get("amount")
        return PaymentStatus(
            reference=reference,
            state=state,
            amount_minor=major_to_minor(amount, currency) if amount is not None else None,
            currency=currency,
            provider_reference=str(body.get("reference") or "") or None,
            raw_state=raw,
        )

    def signature_is_valid(self, *, body: bytes, signature: str | None) -> bool:
        """HMAC-SHA256 of the raw body, compared in constant time.

        Refused outright when no webhook secret is configured. An unsigned
        callback is an unauthenticated request that issues tickets, and
        defaulting to "allow" would make a missing setting into a way to get
        into events for free.
        """
        if not self._webhook_secret or not signature:
            return False
        expected = hmac.new(
            self._webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature.strip())

    async def _post(self, path: str, payload: dict) -> dict:
        return await self._call("POST", path, json=payload)

    async def _get(self, path: str) -> dict:
        return await self._call("GET", path)

    async def _call(self, method: str, path: str, *, json: dict | None = None) -> dict:
        started = time.perf_counter()
        outcome = "error"
        try:
            with tracing.span("payment"):
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    response = await client.request(
                        method,
                        f"{self._base}{path}",
                        json=json,
                        headers={"Authorization": f"Bearer {self._secret}"},
                    )
                response.raise_for_status()
                body = response.json()
            outcome = "ok"
            return body
        except httpx.HTTPStatusError as exc:
            # The status line alone is useless here. Chapa answers a rejected
            # initialisation with 400 and a body naming the field it did not
            # like, and without it an operator is left guessing between a bad
            # key, an unreachable callback URL and an amount below the minimum.
            detail = _provider_message(exc.response)
            logger.warning(
                "chapa_request_failed",
                path=path,
                status=exc.response.status_code,
                detail=detail,
            )
            raise PaymentError(detail or str(exc)) from exc
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("chapa_request_failed", path=path, error=str(exc))
            raise PaymentError(str(exc)) from exc
        finally:
            # These are the real names. An earlier version called
            # `tracing.record_span` and `metrics.observe_external`, neither of
            # which exists - and being in a `finally`, it raised AttributeError
            # on every Chapa request, successful ones included. It never showed
            # up because every test and every verification ran on the stub
            # provider, which is exactly the blind spot a stub creates.
            metrics.dependency_calls.inc("payment", outcome)
            metrics.dependency_duration.observe(time.perf_counter() - started, "payment")


# ------------------------------------------------------------------ Stripe


class StripePayments:
    """Stripe Checkout - the vendor strategy's "Payments (Global)" row.

    Hosted, like Chapa: a Checkout Session is created server-side and the
    explorer is redirected to a page Stripe owns. No card detail crosses this
    process, which is the whole reason to use Checkout rather than payment
    intents with fields of our own.

    Three things differ from Chapa, and each is a way to get this wrong.

    **Amounts are already in the smallest unit.** `unit_amount` is santim, cents
    or - for yen - whole yen, which is exactly what this module carries
    everywhere. So there is no conversion here, and adding one would multiply
    every price by a hundred.

    **Stripe answers about its session, not our reference.**
    `client_reference_id` carries ours through and comes back on the webhook,
    but nothing looks a session up by it. So the session id is kept when
    checkout starts and used to verify afterwards.

    **The signature covers a timestamp as well as the body.** Stripe sends
    `t=<unix>,v1=<hex>` and signs `"{t}.{body}"`. Checking only the digest would
    accept a correctly-signed payload replayed a month later, which for a
    settlement webhook means issuing the tickets again.
    """

    name = "stripe"

    # How far out of date a signed callback may be. Stripe's own libraries
    # default to five minutes; the point is that a captured payload stops being
    # useful quickly.
    SIGNATURE_TOLERANCE_SECONDS = 300

    def __init__(self, secret_key: str, webhook_secret: str, base_url: str) -> None:
        self._secret = secret_key
        self._webhook_secret = webhook_secret
        self._base = base_url.rstrip("/")

    async def start(
        self,
        *,
        reference: str,
        amount_minor: int,
        currency: str,
        email: str,
        display_name: str,
        description: str,
        return_url: str,
        callback_url: str,
    ) -> Checkout:
        separator = "&" if "?" in return_url else "?"
        form = {
            "mode": "payment",
            # Stripe substitutes the id into this itself, so the return page can
            # identify the session without trusting a query string the explorer
            # could have edited.
            "success_url": f"{return_url}{separator}session={{CHECKOUT_SESSION_ID}}",
            "cancel_url": return_url,
            "client_reference_id": reference,
            "customer_email": email,
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": currency.lower(),
            "line_items[0][price_data][unit_amount]": str(amount_minor),
            "line_items[0][price_data][product_data][name]": description[:250] or "Tickets",
            "metadata[reference]": reference,
        }
        data = await self._call(
            "POST", "/checkout/sessions", form=form, idempotency_key=reference
        )
        session_id = (data or {}).get("id")
        url = (data or {}).get("url")
        if not session_id or not url:
            raise PaymentError("Stripe did not return a checkout session.")
        return Checkout(
            provider=self.name,
            reference=reference,
            redirect_url=url,
            provider_reference=str(session_id),
        )

    async def verify(
        self, reference: str, *, provider_reference: str | None = None
    ) -> PaymentStatus:
        if not provider_reference:
            # Nothing to ask about yet. Reported as pending rather than failed:
            # the order may simply not have reached Stripe, and failing it here
            # would release places somebody is in the middle of paying for.
            return PaymentStatus(reference=reference, state=PENDING, raw_state="no_session")

        data = await self._call("GET", f"/checkout/sessions/{provider_reference}")
        if data is None:
            return PaymentStatus(reference=reference, state=PENDING, raw_state="unreachable")

        raw = str(data.get("payment_status") or "").lower()
        state = {"paid": PAID, "no_payment_required": PAID, "unpaid": PENDING}.get(raw, PENDING)
        # An expired session will never be paid, and saying so releases the
        # places now instead of holding them until the sweep notices.
        if state == PENDING and str(data.get("status") or "").lower() == "expired":
            state = FAILED

        total = data.get("amount_total")
        return PaymentStatus(
            reference=reference,
            state=state,
            # Already minor units. No conversion, deliberately.
            amount_minor=int(total) if isinstance(total, int) else None,
            currency=str(data.get("currency") or "").upper() or None,
            provider_reference=str(data.get("payment_intent") or provider_reference),
            raw_state=raw or str(data.get("status") or ""),
        )

    def signature_is_valid(self, *, body: bytes, signature: str | None) -> bool:
        """Stripe's `t=...,v1=...` scheme, timestamp included.

        Refused outright with no secret configured, for the same reason as
        Chapa: an unauthenticated callback issues tickets.
        """
        if not self._webhook_secret or not signature:
            return False

        parts = dict(piece.split("=", 1) for piece in signature.split(",") if "=" in piece)
        timestamp = parts.get("t")
        sent = parts.get("v1")
        if not timestamp or not sent:
            return False

        try:
            age = abs(time.time() - int(timestamp))
        except ValueError:
            return False
        if age > self.SIGNATURE_TOLERANCE_SECONDS:
            # Correctly signed but stale. For a settlement webhook, accepting a
            # replay means issuing the tickets a second time.
            logger.warning("stripe_signature_too_old", age_seconds=int(age))
            return False

        expected = hmac.new(
            self._webhook_secret.encode(),
            f"{timestamp}.".encode() + body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, sent)

    async def _call(
        self,
        method: str,
        path: str,
        *,
        form: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict | None:
        started = time.perf_counter()
        outcome = "error"
        headers = {"Authorization": f"Bearer {self._secret}"}
        if idempotency_key:
            # Retrying a create must not open a second session and charge twice.
            headers["Idempotency-Key"] = idempotency_key
        try:
            with tracing.span("payment"):
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    response = await client.request(
                        method, f"{self._base}{path}", data=form, headers=headers
                    )
                response.raise_for_status()
                body = response.json()
            outcome = "ok"
            return body
        except httpx.HTTPStatusError as exc:
            detail = _provider_message(exc.response)
            logger.warning(
                "stripe_request_failed",
                path=path,
                status=exc.response.status_code,
                detail=detail,
            )
            if method == "POST":
                raise PaymentError(detail or str(exc)) from exc
            return None
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("stripe_request_failed", path=path, error=str(exc))
            # A failed create must stop the checkout; a failed read is answered
            # as "not yet", because the order may still be paid.
            if method == "POST":
                raise PaymentError(str(exc)) from exc
            return None
        finally:
            metrics.dependency_calls.inc("payment", outcome)
            metrics.dependency_duration.observe(time.perf_counter() - started, "payment")


# -------------------------------------------------------------------- stub



class StubPayments:
    """The development provider. It does not invent a payment.

    A stub that answers "paid" because nothing said otherwise is
    indistinguishable from a working integration, right up to the point where
    somebody ships it and the money never arrives. So every payment started
    here stays :data:`PENDING` until something explicitly settles it - in
    development, the simulated checkout page; in tests, a direct call.

    State lives in the process, which is exactly right for a stub: a restart
    forgetting everything is a clearer signal than a stub that persists and
    starts to look real.
    """

    name = "stub"

    def __init__(self) -> None:
        self._payments: dict[str, PaymentStatus] = {}

    async def start(
        self,
        *,
        reference: str,
        amount_minor: int,
        currency: str,
        email: str,
        display_name: str,
        description: str,
        return_url: str,
        callback_url: str,
    ) -> Checkout:
        self._payments[reference] = PaymentStatus(
            reference=reference,
            state=PENDING,
            amount_minor=amount_minor,
            currency=currency,
            raw_state="stub_started",
        )
        separator = "&" if "?" in return_url else "?"
        return Checkout(
            provider=self.name,
            reference=reference,
            # Straight back to the app's own simulated checkout, which is the
            # only thing that can settle this.
            redirect_url=f"{return_url}{separator}simulated=1",
        )

    async def verify(
        self, reference: str, *, provider_reference: str | None = None
    ) -> PaymentStatus:
        known = self._payments.get(reference)
        if known is None:
            return PaymentStatus(reference=reference, state=FAILED, raw_state="stub_unknown")
        return known

    def settle(self, reference: str, *, paid: bool) -> PaymentStatus:
        """Development only: say what the imaginary explorer did."""
        current = self._payments.get(reference)
        if current is None:
            raise PaymentError(f"No stub payment started for {reference}.")
        settled = PaymentStatus(
            reference=reference,
            state=PAID if paid else FAILED,
            amount_minor=current.amount_minor,
            currency=current.currency,
            provider_reference=f"stub_{uuid.uuid4().hex[:12]}",
            raw_state="stub_settled",
        )
        self._payments[reference] = settled
        return settled

    def signature_is_valid(self, *, body: bytes, signature: str | None) -> bool:
        """Still checked, so the verification path is exercised in development.

        A stub that waves signatures through means the first time the check runs
        for real is in production.
        """
        secret = get_settings().payment_webhook_secret
        if not secret or not signature:
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature.strip())


# ---------------------------------------------------------------- routing

# The currency Chapa settles in. Everything else is Stripe's, once Stripe is
# switched on - which is the whole reason the routing is by currency rather than
# by a single global setting.
CHAPA_CURRENCIES = frozenset({"ETB"})

_providers: dict[str, PaymentProvider] = {}


def _build(name: str) -> PaymentProvider:
    settings = get_settings()

    if name == "chapa":
        if not settings.chapa_secret_key:
            logger.warning("payment_provider_unconfigured", requested="chapa")
            return StubPayments()
        return ChapaPayments(
            settings.chapa_secret_key,
            settings.payment_webhook_secret,
            settings.chapa_base_url,
        )

    if name == "stripe":
        if not settings.stripe_secret_key:
            logger.warning("payment_provider_unconfigured", requested="stripe")
            return StubPayments()
        return StripePayments(
            settings.stripe_secret_key,
            settings.stripe_webhook_secret,
            settings.stripe_base_url,
        )

    return StubPayments()


def provider_named(name: str | None) -> PaymentProvider:
    """The provider an order was actually started with.

    Settlement must ask the provider that holds the money, not whichever one the
    configuration currently prefers. An order started on Chapa and verified
    against Stripe would be reported unpaid and have its places released while
    the payment sat there perfectly complete.
    """
    key = (name or "").lower() or "stub"
    if key not in _providers:
        _providers[key] = _build(key)
    return _providers[key]


def provider_for(currency: str) -> PaymentProvider:
    """Which provider should take this payment.

    Currency decides, because that is the fact that actually constrains it:
    Chapa settles birr and Stripe settles most other things, and a platform
    operating in more than one country cannot express that with one global
    setting.

    Stripe is only ever reached when `MADO_STRIPE_ENABLED` is true. Until then
    the answer is whatever `MADO_PAYMENT_PROVIDER` says, which keeps today's
    behaviour exactly as it is - the adapter is built and tested and simply not
    used.
    """
    settings = get_settings()
    code = (currency or "").upper()

    if settings.stripe_enabled and code not in CHAPA_CURRENCIES:
        return provider_named("stripe")
    if settings.stripe_enabled and code in CHAPA_CURRENCIES and settings.chapa_secret_key:
        return provider_named("chapa")
    return provider_named(settings.payment_provider)


def get_provider() -> PaymentProvider:
    """The default provider, for callers with no currency to hand.

    Kept because the simulated-checkout endpoint and a few tests need to know
    whether this deployment is running on the stub. Anything taking money should
    use :func:`provider_for`.
    """
    return provider_named(get_settings().payment_provider)


def reset_provider() -> None:
    """Drop the cached providers. For tests and for a settings change."""
    _providers.clear()
