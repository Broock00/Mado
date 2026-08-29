"""Buying a plan, and the record of having bought it.

Shaped after `commerce/checkout.py`, because the failure modes are identical and
they were learned once already:

- **The price is copied**, onto the subscription and the invoice. A price list
  edited next month must not change what somebody agreed to pay this month.
- **The browser is not a witness.** An account returning with `?status=success`
  has proved it can follow a link. Both settlement paths verify with the
  provider that holds the money before anything is granted.
- **Settling twice grants once**, by a unique constraint on the invoice
  reference rather than by looking first, because two deliveries race.

Where it differs is the shape of the product, and the difference is deliberate.

**A period is prepaid, not auto-renewing.** Chapa has no recurring billing and
Stripe is off behind `MADO_STRIPE_ENABLED`, so nothing in this deployment can
charge anybody a second time. A subscription that renewed itself would move to a
new period that nothing had paid for - the account would read "active", the
entitlements would be granted, and no money would ever arrive. That is the stub
rule wearing a subscription's clothes, and it fails exactly the same way:
indistinguishable from working until somebody looks at the bank.

So a period ends, the entitlements stop being granted the moment it does (see
`entitlements.plan_of`, which reads through the date rather than trusting the
status), and the business is asked whether it wants another. Auto-renew arrives
with the Stripe path, as a thing that genuinely charges.

**Nothing is deleted on a downgrade or a lapse.** The row stays, the posts stay,
the team stays. `entitlements.py` refuses additions and removes nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.domains.identity.models import User
from app.domains.publisher import plans
from app.domains.publisher.models import Publisher, Subscription, SubscriptionInvoice
from app.integrations import payments

logger = get_logger("mado.subscriptions")

# One month, fixed. Calendar months would make the price per day depend on
# which month somebody happened to buy in, and a plan costing 3% more in
# February is a support question nobody wants to answer.
PERIOD_DAYS = 30


def _reference() -> str:
    """Ours, unguessable, and generated before the provider is called.

    Enumerable references let somebody ask about other accounts' invoices, and
    the same value is what makes starting the same purchase twice idempotent at
    the provider.
    """
    return f"mado-sub-{uuid.uuid4().hex}"


class SubscriptionService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------- reading

    async def of(self, publisher_id: uuid.UUID) -> Subscription | None:
        return (
            await self.session.execute(
                select(Subscription).where(Subscription.publisher_id == publisher_id)
            )
        ).scalar_one_or_none()

    async def invoices_for(
        self, publisher_id: uuid.UUID, *, limit: int = 24
    ) -> list[SubscriptionInvoice]:
        result = await self.session.execute(
            select(SubscriptionInvoice)
            .where(SubscriptionInvoice.publisher_id == publisher_id)
            .order_by(SubscriptionInvoice.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def by_reference(self, reference: str) -> Subscription | None:
        return (
            await self.session.execute(
                select(Subscription).where(Subscription.reference == reference)
            )
        ).scalar_one_or_none()

    # -------------------------------------------------------------- buying

    async def start(
        self,
        user: User,
        publisher: Publisher,
        *,
        plan_key: str,
        currency: str,
        return_url: str,
        callback_url: str,
        now: datetime | None = None,
    ) -> Subscription:
        """Begin a purchase, and get somewhere to send them to pay.

        The subscription row is written `pending` and grants nothing until the
        provider confirms. Writing it active and correcting it afterwards would
        hand out a month of entitlements to anybody who opened the checkout page
        and closed it.
        """
        now = now or datetime.now(UTC)
        currency = (currency or "").upper()

        if not plans.is_purchasable(plan_key):
            # Free needs no transaction; enterprise is negotiated, and quoting a
            # price for it would be a number the sales conversation contradicts.
            raise ValidationError(
                "That plan is not one you can buy here.", code="PLAN_NOT_PURCHASABLE"
            )

        price_minor = plans.price_for(plan_key, currency)
        if price_minor is None:
            raise ValidationError(
                f"{plans.get(plan_key).name} is not sold in {currency} yet.",
                code="PLAN_NOT_SOLD_IN_CURRENCY",
                details={"currency": currency},
            )

        subscription = await self.of(publisher.id)
        if subscription is None:
            # Nothing in force yet, which is what `pending` means on a row that
            # has never been paid for. `plan` stays free until money arrives.
            subscription = Subscription(
                publisher_id=publisher.id, plan=plans.FREE, status=plans.STATUS_PENDING
            )
            self.session.add(subscription)

        # `plan`, `status` and the period are deliberately untouched. An account
        # already paying for something keeps it while it buys the next thing -
        # clicking upgrade must not cost somebody the plan they are still paying
        # for, which is exactly what overwriting this row used to do.
        subscription.pending_plan = plan_key
        subscription.currency = currency
        subscription.price_minor = price_minor
        subscription.reference = _reference()
        await self.session.flush()

        email = self._email_of(user)
        provider = payments.provider_for(currency)
        try:
            checkout = await provider.start(
                reference=subscription.reference,
                amount_minor=price_minor,
                currency=currency,
                email=email,
                display_name=publisher.name,
                description=f"Mado {plans.get(plan_key).name} - {PERIOD_DAYS} days",
                return_url=return_url,
                callback_url=callback_url,
            )
        except payments.PaymentError as exc:
            # Left pending rather than rolled back to whatever it was. A plan
            # that silently reverted would look to the publisher as though the
            # button did nothing, and they would press it again.
            logger.warning(
                "subscription_start_failed", publisher_id=str(publisher.id), error=str(exc)
            )
            raise ConflictError(
                "Payments are unavailable right now. Nothing has been charged.",
                code="PAYMENT_UNAVAILABLE",
            ) from exc

        subscription.provider = checkout.provider
        subscription.provider_reference = checkout.provider_reference
        subscription.checkout_url = checkout.redirect_url
        await self.session.flush()

        logger.info(
            "subscription_started",
            publisher_id=str(publisher.id),
            plan=plan_key,
            amount_minor=price_minor,
            currency=currency,
        )
        return subscription

    async def settle(
        self, subscription: Subscription, *, now: datetime | None = None
    ) -> Subscription:
        """Ask the provider what happened, and grant the period exactly once."""
        now = now or datetime.now(UTC)
        if subscription.pending_plan is None or subscription.reference is None:
            # Nothing is being bought. Not an error: this is the shape of a
            # redelivered webhook arriving after the purchase already settled,
            # and of the return leg being reloaded.
            return subscription

        # The provider that holds the money, not whichever the configuration
        # currently prefers - a purchase started on Chapa and verified against
        # Stripe comes back unpaid while the payment sits there complete.
        provider = payments.provider_named(subscription.provider)
        status = await provider.verify(
            subscription.reference, provider_reference=subscription.provider_reference
        )

        if status.state != payments.PAID:
            return subscription

        if status.amount_minor is None or status.amount_minor != subscription.price_minor:
            # Never grant on a mismatch. Providers do not normally do this;
            # a replayed payload from a cheaper plan does.
            logger.error(
                "subscription_amount_mismatch",
                publisher_id=str(subscription.publisher_id),
                expected_minor=subscription.price_minor,
                reported_minor=status.amount_minor,
            )
            raise ConflictError(
                "That payment does not match the plan.", code="SUBSCRIPTION_AMOUNT_MISMATCH"
            )

        period_start = now
        period_end = now + timedelta(days=PERIOD_DAYS)

        # The invoice is the idempotency mechanism, not a check first: the
        # webhook and the return race, and only the database can settle it.
        invoice = SubscriptionInvoice(
            publisher_id=subscription.publisher_id,
            plan=subscription.pending_plan,
            reference=subscription.reference,
            amount_minor=subscription.price_minor or 0,
            currency=subscription.currency or "ETB",
            period_start=period_start,
            period_end=period_end,
            provider=subscription.provider,
            provider_reference=status.provider_reference,
            paid_at=now,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(invoice)
        except IntegrityError:
            logger.info(
                "subscription_already_settled", publisher_id=str(subscription.publisher_id)
            )
            await self.session.refresh(subscription)
            return subscription

        # Only now does what was bought become what is in force.
        subscription.plan = subscription.pending_plan
        subscription.status = plans.STATUS_ACTIVE
        subscription.current_period_start = period_start
        subscription.current_period_end = period_end
        subscription.pending_plan = None
        # Cleared so a stale link cannot be followed into a second charge.
        subscription.checkout_url = None
        await self.session.flush()

        logger.info(
            "subscription_active",
            publisher_id=str(subscription.publisher_id),
            plan=subscription.plan,
            until=period_end.isoformat(),
        )
        return subscription

    async def cancel(self, publisher_id: uuid.UUID) -> Subscription:
        """Stop it renewing - which, since nothing renews, means stop it now.

        The period that was paid for is **not** cut short. Somebody who bought a
        month and cancelled on day three has paid for the month, and taking it
        back would be keeping their money and withdrawing the thing they bought.
        The status moves to cancelled and `current_period_end` is left alone;
        `entitlements.plan_of` reads through the date, so they keep what they
        paid for until it runs out.
        """
        subscription = await self.of(publisher_id)
        if subscription is None:
            raise NotFoundError("There is nothing to cancel.", code="NO_SUBSCRIPTION")
        subscription.status = plans.STATUS_CANCELLED
        if subscription.current_period_end is None:
            # An open-ended grant being cancelled. It has to end somewhere, and
            # now is the only honest answer - without a date, a cancelled
            # subscription would keep granting forever, because `plan_of` reads
            # a missing end as "until somebody ends it" and this *is* somebody
            # ending it.
            subscription.current_period_end = datetime.now(UTC)
        await self.session.flush()
        logger.info("subscription_cancelled", publisher_id=str(publisher_id))
        return subscription

    async def grant(
        self,
        publisher_id: uuid.UUID,
        *,
        plan_key: str,
        days: int | None = None,
        now: datetime | None = None,
    ) -> Subscription:
        """Put an account on a plan without a payment.

        For enterprise agreements, which are negotiated and invoiced outside
        Mado, and for putting something right. `days=None` leaves no end date,
        which is what an open-ended agreement actually is - and which
        `plan_of` treats as granting until somebody changes it.
        """
        now = now or datetime.now(UTC)
        if plan_key not in plans.PLANS:
            raise ValidationError(f"There is no {plan_key} plan.", code="UNKNOWN_PLAN")

        subscription = await self.of(publisher_id)
        if subscription is None:
            subscription = Subscription(publisher_id=publisher_id, plan=plan_key, status="")
            self.session.add(subscription)

        subscription.plan = plan_key
        subscription.status = plans.STATUS_ACTIVE
        subscription.pending_plan = None
        subscription.current_period_start = now
        subscription.current_period_end = None if days is None else now + timedelta(days=days)
        # No price and no provider: nothing was charged here, and leaving a
        # stale amount would make the invoices disagree with the row.
        subscription.price_minor = None
        subscription.currency = None
        subscription.provider = None
        subscription.provider_reference = None
        subscription.reference = None
        subscription.checkout_url = None
        await self.session.flush()

        logger.info("subscription_granted", publisher_id=str(publisher_id), plan=plan_key)
        return subscription

    # ---------------------------------------------------------- internals

    def _email_of(self, user: User) -> str:
        """Where the provider sends the receipt.

        Refused rather than invented, for the reason checkout learned: a made-up
        address is rejected by any real provider, and the account is told
        "payments are unavailable" - which is both wrong and unactionable.
        """
        profile = getattr(user, "profile", None)
        email = (getattr(profile, "email", None) or "").strip()
        if not email:
            raise ValidationError(
                "Add an email address to your profile first - the payment provider "
                "sends the receipt there.",
                code="EMAIL_REQUIRED_FOR_PAYMENT",
            )
        return email


__all__ = ["PERIOD_DAYS", "SubscriptionService"]
