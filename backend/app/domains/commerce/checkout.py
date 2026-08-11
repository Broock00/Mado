"""Buying tickets (spec COM-003).

The order of operations is the whole design, and it is this:

1. Take the seats, in the database, before anybody is sent to a payment page.
2. Write the order with the price agreed now.
3. Ask the provider for a checkout URL.
4. Issue nothing until the provider, asked directly or arriving signed, says
   the money is there.
5. Give the seats back if that never happens.

Each step exists because the obvious alternative fails a specific way.

**Seats first, money second.** Sending somebody to a payment page and taking the
seat when they come back oversells every event that matters - the last four
tickets go to nine people, six of whom have paid. So checkout takes the places
immediately, with the same single conditional UPDATE COM-001 uses, and holds
them for :data:`app.core.config.Settings.payment_hold_minutes`. An abandoned
cart costs the publisher twenty minutes of one seat; the alternative costs them
a refund and an argument at the door.

**The price is copied, not referenced.** A publisher raising a price must not
change what somebody already agreed to pay, and reading the tier back at
settlement would do exactly that.

**The browser is not a witness.** An explorer returning with `?status=success`
has proved only that they can follow a link. Both settlement paths - the signed
webhook and the return - end in :meth:`Checkout.settle`, which asks the
provider what actually happened before it issues anything. The return leg is
kept because a webhook can be minutes late and somebody staring at a spinner
deserves an answer, not because it is evidence.

**The amount is checked.** A settlement claiming payment of one birr against a
five-hundred-birr order is refused and logged rather than trusted. Providers do
not normally do this; attackers replaying an old signed payload for a cheap
order do.

**Settling twice must issue once.** A provider will redeliver a webhook - that
is documented behaviour, not a fault - and the return can arrive for the same
payment. Idempotency is a unique constraint on the payment event, not a
`SELECT` first, because two deliveries race and only the database can decide.

**Free tickets skip the provider entirely.** There is nothing to charge, so
there is no redirect, no webhook and nothing to verify: the order is paid the
moment it is made. Sending a zero-value transaction to Chapa to keep one code
path would fail at Chapa, for good reason.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.domains.catalog.models import EventInstance, Experience
from app.domains.commerce.models import (
    MAX_PER_ORDER,
    ORDER_CANCELLED,
    ORDER_EXPIRED,
    ORDER_FAILED,
    ORDER_PAID,
    ORDER_PENDING,
    Order,
    OrderLine,
    PaymentEvent,
    Ticket,
    TicketType,
)
from app.domains.commerce.tickets import sales_window
from app.domains.identity.models import User
from app.integrations import payments

logger = get_logger("mado.checkout")

# The alphabet a ticket code is drawn from. No 0/O, no 1/I/L: a code is read
# aloud at a door and typed in by somebody who is late.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 10


@dataclass(slots=True)
class Line:
    """One request to buy: this many of this tier."""

    ticket_type_id: uuid.UUID
    quantity: int


def _reference() -> str:
    """Ours, and unguessable.

    It reaches the provider and comes back in a callback, so it must not be
    enumerable - a sequential reference lets somebody ask about other people's
    orders.
    """
    return f"mado-{uuid.uuid4().hex}"


def _ticket_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


class CheckoutService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.settings = get_settings()

    # ------------------------------------------------------------- buying

    async def start(
        self,
        user: User,
        *,
        occurrence_id: uuid.UUID,
        lines: list[Line],
        return_url_base: str,
        callback_url: str,
        now: datetime | None = None,
    ) -> Order:
        """Hold the places, write the order, and get somewhere to send them."""
        now = now or datetime.now(UTC)
        requested = {line.ticket_type_id: line.quantity for line in lines if line.quantity > 0}
        if not requested:
            raise ValidationError("Choose at least one ticket.", code="NO_TICKETS_SELECTED")

        total_quantity = sum(requested.values())
        if total_quantity > MAX_PER_ORDER:
            raise ValidationError(
                f"You can buy at most {MAX_PER_ORDER} tickets at once.",
                code="TOO_MANY_TICKETS",
            )

        occurrence = await self.session.get(EventInstance, occurrence_id)
        if occurrence is None:
            raise NotFoundError("That date does not exist.", code="OCCURRENCE_NOT_FOUND")
        if occurrence.status == "cancelled":
            raise ConflictError("That date has been cancelled.", code="OCCURRENCE_CANCELLED")

        starts = occurrence.start_time
        if starts.tzinfo is None:
            starts = starts.replace(tzinfo=UTC)
        if starts <= now:
            raise ConflictError("That date has already happened.", code="OCCURRENCE_PAST")

        experience = await self.session.get(Experience, occurrence.experience_id)
        if experience is None:
            raise NotFoundError("That listing does not exist.", code="EXPERIENCE_NOT_FOUND")
        if (experience.external_ticket_url or "").strip():
            # §64. Selling here as well would be claiming inventory we do not
            # control and cannot count.
            raise ConflictError(
                "This organiser sells tickets on their own site.",
                code="EXTERNAL_TICKETING",
            )

        tiers = {
            tier.id: tier
            for tier in (
                await self.session.execute(
                    select(TicketType).where(TicketType.id.in_(requested))
                )
            ).scalars()
        }

        order_lines: list[OrderLine] = []
        amount_minor = 0
        currency = experience.currency
        for tier_id, quantity in requested.items():
            tier = tiers.get(tier_id)
            if tier is None or tier.event_instance_id != occurrence.id:
                raise NotFoundError(
                    "That ticket type is not on this date.", code="TICKET_TYPE_NOT_FOUND"
                )
            on_sale, why = sales_window(tier, now)
            if not on_sale:
                raise ConflictError(
                    f"'{tier.name}' is not on sale.",
                    code=f"TICKET_{(why or 'unavailable').upper()}",
                )
            if tier.remaining is not None and quantity > tier.remaining:
                raise ConflictError(
                    f"Only {tier.remaining} left of '{tier.name}'.", code="TICKET_TYPE_SOLD_OUT"
                )
            currency = tier.currency
            amount_minor += tier.price_minor * quantity
            order_lines.append(
                OrderLine(
                    ticket_type_id=tier.id,
                    ticket_type_name=tier.name,
                    quantity=quantity,
                    unit_price_minor=tier.price_minor,
                )
            )

        # Places first. Both counters move under the database's lock, and the
        # per-tier one is checked the same conditional way as the room - a tier
        # with three left must refuse the fourth even when the room is empty.
        await self._take_seats(occurrence, total_quantity)
        try:
            for tier_id, quantity in requested.items():
                await self._take_tier(tiers[tier_id], quantity)
        except ConflictError:
            # Give the room back before failing, or an event slowly empties
            # itself every time two tiers race.
            await self._return_seats(occurrence, total_quantity)
            raise

        order = Order(
            user_id=user.id,
            event_instance_id=occurrence.id,
            experience_id=experience.id,
            experience_title=experience.title,
            starts_at=starts,
            reference=_reference(),
            status=ORDER_PENDING,
            amount_minor=amount_minor,
            currency=currency,
            quantity=total_quantity,
            expires_at=now + timedelta(minutes=self.settings.payment_hold_minutes),
        )
        order.lines = order_lines
        # Set explicitly, not left to lazy loading. The serialiser reads it
        # outside the async context, and an unloaded collection there raises
        # MissingGreenlet rather than returning nothing - a 500 on the happy
        # path of the whole feature.
        order.tickets = []
        self.session.add(order)
        await self.session.flush()

        if order.is_free:
            # Nothing to charge. Paid immediately, with no provider involved and
            # nothing to verify - the honest representation of a free ticket.
            await self._mark_paid(order, provider="none", provider_reference=None, now=now)
            logger.info(
                "order_free_completed", order_id=str(order.id), quantity=total_quantity
            )
            return order

        # Built here rather than by the caller, because the order has to exist
        # before it has an id and the explorer has to come back to a page that
        # can look it up. A generic "payment pending" page would then have to
        # guess which order it was about.
        return_url = f"{return_url_base.rstrip('/')}/{order.id}"

        provider = payments.get_provider()
        try:
            checkout = await provider.start(
                reference=order.reference,
                amount_minor=order.amount_minor,
                currency=order.currency,
                email=self._email_of(user),
                display_name=self._name_of(user),
                description=f"{experience.title}",
                return_url=return_url,
                callback_url=callback_url,
            )
        except payments.PaymentError as exc:
            # The provider could not be reached. Release everything now rather
            # than leaving a hold that only expires in twenty minutes for an
            # order that was never going to be payable.
            await self._release(order, ORDER_FAILED, reason="Payment could not be started.")
            logger.warning("checkout_start_failed", order_id=str(order.id), error=str(exc))
            raise ConflictError(
                "Payments are unavailable right now. Nothing has been charged.",
                code="PAYMENT_UNAVAILABLE",
            ) from exc

        order.provider = checkout.provider
        order.checkout_url = checkout.redirect_url
        await self.session.flush()
        logger.info(
            "checkout_started",
            order_id=str(order.id),
            provider=checkout.provider,
            amount_minor=order.amount_minor,
            quantity=total_quantity,
        )
        return order

    # --------------------------------------------------------- settlement

    async def settle(
        self,
        order: Order,
        *,
        source: str,
        external_id: str | None = None,
        payload: dict | None = None,
        now: datetime | None = None,
    ) -> Order:
        """Ask the provider what happened, and act on it exactly once.

        `source` is `webhook`, `return` or `sweep`. It changes nothing about the
        decision - all three verify - and is recorded so the log can answer
        which signal arrived first.
        """
        now = now or datetime.now(UTC)
        if order.status == ORDER_PAID:
            # Already done. Not an error: this is the normal shape of a
            # redelivered webhook, and returning the order is what both callers
            # want anyway.
            return order
        if order.status in (ORDER_CANCELLED, ORDER_FAILED, ORDER_EXPIRED):
            return order

        provider = payments.get_provider()
        status = await provider.verify(order.reference)

        # Recorded before acting, and unique per (provider, external_id), so a
        # second delivery of the same signal loses the race and does nothing.
        marker = external_id or f"{source}:{order.reference}:{status.state}"
        try:
            async with self.session.begin_nested():
                self.session.add(
                    PaymentEvent(
                        order_id=order.id,
                        provider=order.provider or provider.name,
                        external_id=marker[:160],
                        source=source,
                        state=status.state,
                        amount_minor=status.amount_minor,
                        currency=status.currency,
                        payload=payload or {},
                    )
                )
        except IntegrityError:
            logger.info(
                "payment_event_already_seen", order_id=str(order.id), external_id=marker[:160]
            )
            await self.session.refresh(order)
            return order

        if status.state == payments.PAID:
            if not self._amount_matches(order, status):
                # Never issue on a mismatch. A provider does not normally send
                # one; a replayed payload from a cheaper order does.
                logger.error(
                    "payment_amount_mismatch",
                    order_id=str(order.id),
                    expected_minor=order.amount_minor,
                    reported_minor=status.amount_minor,
                    expected_currency=order.currency,
                    reported_currency=status.currency,
                )
                await self._release(
                    order, ORDER_FAILED, reason="The payment did not match the order."
                )
                return order

            await self._mark_paid(
                order,
                provider=order.provider or provider.name,
                provider_reference=status.provider_reference,
                now=now,
            )
            logger.info("order_paid", order_id=str(order.id), source=source)
            return order

        if status.state == payments.FAILED:
            await self._release(order, ORDER_FAILED, reason="The payment did not go through.")
            logger.info("order_failed", order_id=str(order.id), source=source)

        # Still pending. Left alone: the hold has not expired, and the sweep or
        # a later webhook will decide.
        return order

    async def expire_holds(self, *, now: datetime | None = None, limit: int = 200) -> int:
        """Give back the places of orders nobody finished paying for.

        Verified before releasing, not just released. Somebody who paid at the
        nineteenth minute while the webhook was queued must not lose the ticket
        they hold a receipt for.
        """
        now = now or datetime.now(UTC)
        stale = list(
            (
                await self.session.execute(
                    select(Order)
                    .where(Order.status == ORDER_PENDING, Order.expires_at <= now)
                    .order_by(Order.expires_at)
                    .limit(limit)
                )
            ).scalars()
        )

        released = 0
        for order in stale:
            settled = await self.settle(order, source="sweep", now=now)
            if settled.status == ORDER_PENDING:
                await self._release(
                    order, ORDER_EXPIRED, reason="The payment was not completed in time."
                )
                released += 1

        if stale:
            logger.info("order_holds_swept", considered=len(stale), released=released)
        return released

    async def cancel(self, user: User, order_id: uuid.UUID) -> Order:
        """The explorer changing their mind before paying."""
        order = await self.owned(user, order_id)
        if order.status == ORDER_PAID:
            raise ConflictError(
                "That order is paid. Cancelling a paid ticket is a refund, which is "
                "not something Mado does yet.",
                code="ORDER_ALREADY_PAID",
            )
        if order.status != ORDER_PENDING:
            return order
        await self._release(order, ORDER_CANCELLED, reason="You cancelled this order.")
        return order

    # ------------------------------------------------------------ reading

    async def mine(self, user: User, *, upcoming_only: bool = True) -> list[Order]:
        stmt = select(Order).where(Order.user_id == user.id, Order.status == ORDER_PAID)
        if upcoming_only:
            stmt = stmt.where(Order.starts_at >= datetime.now(UTC))
        result = await self.session.execute(stmt.order_by(Order.starts_at))
        return list(result.scalars().all())

    async def by_reference(self, reference: str) -> Order | None:
        result = await self.session.execute(select(Order).where(Order.reference == reference))
        return result.scalar_one_or_none()

    async def owned(self, user: User, order_id: uuid.UUID) -> Order:
        order = await self.session.get(Order, order_id)
        if order is None:
            raise NotFoundError("Order not found.", code="ORDER_NOT_FOUND")
        if order.user_id != user.id:
            # Not found rather than forbidden: confirming an order exists tells
            # somebody guessing ids that they guessed right.
            raise NotFoundError("Order not found.", code="ORDER_NOT_FOUND")
        return order

    # ---------------------------------------------------------- internals

    @staticmethod
    def _amount_matches(order: Order, status: payments.PaymentStatus) -> bool:
        if status.amount_minor is None:
            # The provider did not say. Trusting silence is the same mistake as
            # trusting a wrong number.
            return False
        if status.currency and status.currency.upper() != order.currency.upper():
            return False
        return status.amount_minor == order.amount_minor

    def _email_of(self, user: User) -> str:
        profile = getattr(user, "profile", None)
        return (getattr(profile, "email", None) or "").strip() or "explorer@mado.local"

    def _name_of(self, user: User) -> str:
        profile = getattr(user, "profile", None)
        return (getattr(profile, "display_name", None) or "Explorer").strip()

    async def _mark_paid(
        self,
        order: Order,
        *,
        provider: str,
        provider_reference: str | None,
        now: datetime,
    ) -> None:
        order.status = ORDER_PAID
        order.paid_at = now
        order.provider = provider
        order.provider_reference = provider_reference
        order.expires_at = None

        # Appended to the relationship rather than added to the session, so the
        # order carries its tickets without a second query - and so the caller
        # can serialise it without touching an unloaded collection.
        for line in order.lines:
            for _ in range(line.quantity):
                order.tickets.append(
                    Ticket(
                        user_id=order.user_id,
                        event_instance_id=order.event_instance_id,
                        ticket_type_id=line.ticket_type_id,
                        ticket_type_name=line.ticket_type_name,
                        code=_ticket_code(),
                    )
                )
        await self.session.flush()

    async def _release(self, order: Order, status: str, *, reason: str) -> None:
        """Put the places back and close the order."""
        occurrence = await self.session.get(EventInstance, order.event_instance_id)
        if occurrence is not None:
            await self._return_seats(occurrence, order.quantity)
        for line in order.lines:
            # By statement, like the take, and floored at zero so a double
            # release can never drive the count negative and make a sold-out
            # tier look available.
            await self.session.execute(
                update(TicketType)
                .where(TicketType.id == line.ticket_type_id)
                .values(sold=func.greatest(TicketType.sold - line.quantity, 0))
            )
        order.status = status
        order.outcome_reason = reason
        order.expires_at = None
        await self.session.flush()

    async def _take_seats(self, occurrence: EventInstance, count: int) -> None:
        """The same single conditional UPDATE COM-001 uses.

        Deliberately not a read-then-write: two callers both pass a Python-side
        check before either writes, which is exactly what happens when something
        is nearly full - the only time the count matters.
        """
        if occurrence.remaining is None:
            return
        result = await self.session.execute(
            update(EventInstance)
            .where(EventInstance.id == occurrence.id, EventInstance.remaining >= count)
            .values(remaining=EventInstance.remaining - count)
        )
        if result.rowcount == 0:
            raise ConflictError("There are not enough places left.", code="NOT_ENOUGH_PLACES")
        await self.session.refresh(occurrence, ["remaining"])

    async def _return_seats(self, occurrence: EventInstance, count: int) -> None:
        if occurrence.remaining is None:
            return
        await self.session.execute(
            update(EventInstance)
            .where(EventInstance.id == occurrence.id)
            .values(
                remaining=EventInstance.remaining + count
                if occurrence.capacity is None
                else func.least(EventInstance.remaining + count, occurrence.capacity)
            )
        )
        await self.session.refresh(occurrence, ["remaining"])

    async def _take_tier(self, tier: TicketType, count: int) -> None:
        if tier.quantity is None:
            # Unlimited, so nothing can be refused - but `sold` is still the
            # answer to "how many of these went", which a publisher asks about
            # a free tier as often as a capped one.
            await self.session.execute(
                update(TicketType)
                .where(TicketType.id == tier.id)
                .values(sold=TicketType.sold + count)
            )
            await self.session.refresh(tier, ["sold"])
            return
        result = await self.session.execute(
            update(TicketType)
            .where(TicketType.id == tier.id, TicketType.quantity - TicketType.sold >= count)
            .values(sold=TicketType.sold + count)
        )
        if result.rowcount == 0:
            raise ConflictError(
                f"'{tier.name}' sold out while you were choosing.", code="TICKET_TYPE_SOLD_OUT"
            )
        await self.session.refresh(tier, ["sold"])
