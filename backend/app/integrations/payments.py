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
MINOR_UNITS: dict[str, int] = {"ETB": 100, "USD": 100, "EUR": 100, "GBP": 100}
DEFAULT_MINOR_UNITS = 100


class PaymentError(RuntimeError):
    """The provider could not be reached, or answered with something unusable."""


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

    async def verify(self, reference: str) -> PaymentStatus: ...

    def signature_is_valid(self, *, body: bytes, signature: str | None) -> bool: ...


# ------------------------------------------------------------------- Chapa


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
            "customization": {"title": "Mado", "description": description[:100]},
        }
        data = await self._post("/transaction/initialize", payload)
        url = (data.get("data") or {}).get("checkout_url")
        if not url:
            raise PaymentError("Chapa did not return a checkout URL.")
        return Checkout(provider=self.name, reference=reference, redirect_url=url)

    async def verify(self, reference: str) -> PaymentStatus:
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
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.request(
                    method,
                    f"{self._base}{path}",
                    json=json,
                    headers={"Authorization": f"Bearer {self._secret}"},
                )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("chapa_request_failed", path=path, error=str(exc))
            raise PaymentError(str(exc)) from exc
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            tracing.record_span("payment", elapsed)
            metrics.observe_external("chapa", elapsed)


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

    async def verify(self, reference: str) -> PaymentStatus:
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

_provider: PaymentProvider | None = None


def get_provider() -> PaymentProvider:
    """The configured provider, built once.

    Chapa needs a secret; without one it would fail on every call, so an
    unconfigured deployment gets the stub and says so in the log rather than
    presenting a checkout that cannot work.
    """
    global _provider
    if _provider is not None:
        return _provider

    settings = get_settings()
    choice = settings.payment_provider
    if choice == "chapa":
        if not settings.chapa_secret_key:
            logger.warning("payment_provider_unconfigured", requested="chapa")
            _provider = StubPayments()
        else:
            _provider = ChapaPayments(
                settings.chapa_secret_key,
                settings.payment_webhook_secret,
                settings.chapa_base_url,
            )
    else:
        _provider = StubPayments()

    logger.info("payment_provider_selected", provider=_provider.name)
    return _provider


def reset_provider() -> None:
    """Drop the cached provider. For tests and for a settings change."""
    global _provider
    _provider = None
