"""Choosing whether a paid listing may appear, and buying the right to try.

The selection rules are the whole of this module's value, so they are stated
before any code:

**1. Sponsorship is never a ranking signal.** Nothing is added to
`BROWSE_WEIGHTS` or `SEARCH_WEIGHTS`. The ranker's explanation is generated from
the signals that produced the score, so a paid weight would have to be explained
as "because they paid" or hidden - and hiding it is the first thing
BUSINESS-90.01 §7 forbids. A promotion is a separate, labelled slot beside the
ranking, not a thumb on it.

**2. One slot, and never the first.** At most one promoted listing per result
set, placed second. Whatever the platform genuinely thinks is the best answer is
what an explorer sees first, every time, whoever has paid.

**3. A promotion never bypasses a requirement.** The listing has to qualify on
its own: in the area, matching the filters, and passing every hard suitability
requirement - unknown excluded along with contradicted, exactly as
`DiscoveryService.summarize` already does. A paid slot showing a kitchen that
has never answered "nut-free" to somebody with a nut allergy is precisely the
harm that rule exists to prevent, and money is not a reason to make an exception
to it.

**4. It says what it is.** The card carries `sponsored` and a reason that names
the promotion. Never an organic-sounding "popular near you", which would be a
lie told in the platform's own voice.

**5. Nothing is promoted into the concierge.** The gateway phrases facts that
came back from tools, and a paid item in that stream becomes the assistant's own
recommendation with no label surviving generation. The spec permits sponsored AI
recommendations; Mado has no mechanism that keeps the label attached through a
model, so it does not do it yet.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.domains.catalog.models import STATUS_PUBLISHED, Experience
from app.domains.catalog.repository import Area, with_card_relations
from app.domains.identity.models import User
from app.domains.promotion.models import (
    DEFAULT_RADIUS_KM,
    MAX_DAYS,
    MIN_DAYS,
    STATUS_ACTIVE,
    STATUS_ENDED,
    STATUS_PENDING,
    STATUS_REFUSED,
    Promotion,
    PromotionDay,
)
from app.domains.publisher.models import Publisher
from app.integrations import payments

logger = get_logger("mado.promotions")

# What a day of one slot costs, per currency. A price list rather than a rate
# card: this is inventory Mado sells, and there is nothing to negotiate against
# at this size.
DAILY_PRICE_MINOR: dict[str, int] = {
    "ETB": 20_000,
    "USD": 500,
    "EUR": 500,
    "GBP": 400,
    "KES": 50_000,
}


def _reference() -> str:
    return f"mado-promo-{uuid.uuid4().hex}"


def price_for(days: int, currency: str) -> int | None:
    """What a run of this length costs, or None where it is not sold.

    None rather than a converted figure, for the reason the plan prices give:
    an amount arrived at through this morning's exchange rate is one nobody
    decided to charge.
    """
    daily = DAILY_PRICE_MINOR.get(currency.upper())
    return None if daily is None else daily * days


class PromotionService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------ serving

    async def slot_for(
        self,
        *,
        area: Area | None,
        eligible_ids: set[uuid.UUID],
        required_suitability: set[str] | frozenset[str] | None = None,
        category_slugs: list[str] | None = None,
        experience_type: str | None = None,
        exclude_ids: set[uuid.UUID] | None = None,
        now: datetime | None = None,
    ) -> tuple[Promotion, Experience] | None:
        """The one promoted listing that may appear here, if any.

        Returns None far more often than not, and every one of those refusals is
        deliberate. A promotion is shown only when the listing would have been
        shown anyway - the payment buys position, never eligibility and never
        relevance.

        `eligible_ids` is what the caller's own query retrieved, and is the
        strongest of the rules here: a promotion can only lift something that
        was already an answer to this question.
        """
        now = now or datetime.now(UTC)
        exclude_ids = exclude_ids or set()
        if not eligible_ids:
            return None

        running = (
            await self.session.execute(
                select(Promotion).where(
                    Promotion.status == STATUS_ACTIVE,
                    Promotion.starts_at <= now,
                    Promotion.ends_at > now,
                )
            )
        ).scalars().all()
        if not running:
            return None

        # Narrowed to this place before anything is loaded. A promotion bought
        # for Addis has no business being considered for a search in Nairobi,
        # and checking that first keeps the common case one query.
        candidates = [p for p in running if self._reaches(p, area)]
        if not candidates:
            return None

        # Intersected with what this query found, then minus whatever is already
        # the top result: there is nothing to sell somebody who is already
        # first, and a label there would cost them credibility while buying no
        # position.
        wanted = ({p.experience_id for p in candidates} & eligible_ids) - exclude_ids
        if not wanted:
            return None

        # Through the catalogue's own loader rather than a bare select. A
        # promoted listing goes through `to_summary` exactly as an organic one
        # does, and that touches the venue's neighbourhood, the category, the
        # tags, the media and the publisher - each of which is a MissingGreenlet
        # at request time if it was not loaded here, with no application frame
        # in the traceback to say so.
        experiences = (
            await self.session.execute(
                with_card_relations(
                    select(Experience).where(
                        Experience.id.in_(wanted),
                        Experience.status == STATUS_PUBLISHED,
                        Experience.deleted_at.is_(None),
                    )
                )
            )
        ).scalars().all()

        by_id = {experience.id: experience for experience in experiences}
        for promotion in sorted(candidates, key=lambda p: p.starts_at):
            experience = by_id.get(promotion.experience_id)
            if experience is None:
                # Withdrawn, deleted or never published since it was bought. It
                # does not run, and the campaign is not silently swapped for
                # another of the publisher's listings - they paid for this one.
                continue
            if not self._matches_filters(experience, category_slugs, experience_type):
                continue
            if not self._meets(experience, required_suitability):
                continue
            return promotion, experience

        return None

    @staticmethod
    def _reaches(promotion: Promotion, area: Area | None) -> bool:
        """Whether this promotion covers where the explorer is looking.

        Conservative in both directions. A promotion with no place reaches
        nowhere rather than everywhere, and a request with no area matches
        nothing rather than everything - an unscoped search is usually the
        concierge or a country-wide question, and neither is somewhere one
        business should be standing in front of.
        """
        if area is None:
            return False
        if promotion.city_slug:
            return bool(area.city_slug) and area.city_slug == promotion.city_slug
        if promotion.latitude is None or promotion.longitude is None:
            return False
        if area.latitude is None or area.longitude is None:
            # A box or a country against a point-and-radius promotion. Refused
            # rather than approximated: a bounding box for a region is far
            # larger than the neighbourhood somebody bought.
            return False

        radius = promotion.radius_km or DEFAULT_RADIUS_KM
        return _distance_km(
            promotion.latitude, promotion.longitude, area.latitude, area.longitude
        ) <= radius + (area.radius_km or 0)

    @staticmethod
    def _matches_filters(
        experience: Experience,
        category_slugs: list[str] | None,
        experience_type: str | None,
    ) -> bool:
        if experience_type and experience.type != experience_type:
            return False
        if category_slugs:
            category = getattr(experience, "category", None)
            slug = getattr(category, "slug", None)
            if slug not in category_slugs:
                return False
        return True

    @staticmethod
    def _meets(
        experience: Experience, required: set[str] | frozenset[str] | None
    ) -> bool:
        """Every hard requirement, applied to a paid listing exactly as to a free one.

        Imported here rather than at module scope only to keep this module's
        import graph shallow; the logic is the catalogue's, deliberately, so a
        promoted card can never be assessed by a second opinion that drifts from
        the one every other card gets.
        """
        if not required:
            return True
        from app.domains.catalog import suitability

        assessment = suitability.assess(experience, sorted(required))
        # Unknown is excluded along with contradicted. "We do not know whether
        # this kitchen can do nut-free" is not a maybe worth selling.
        return not assessment.missing

    # ----------------------------------------------------------- counting

    async def record_impression(
        self, promotion_id: uuid.UUID, *, now: datetime | None = None
    ) -> None:
        await self._bump(promotion_id, "impressions", now=now)

    async def record_click(
        self, promotion_id: uuid.UUID, *, now: datetime | None = None
    ) -> None:
        await self._bump(promotion_id, "clicks", now=now)

    async def _bump(
        self, promotion_id: uuid.UUID, column: str, *, now: datetime | None = None
    ) -> None:
        """Add one to today's tally, and never fail the page for it.

        Reporting, not billing - so a counter that cannot be written is worth a
        log line and nothing more. A search that 500s because a statistic would
        not save is a far worse outcome than a number being one short.
        """
        today = (now or datetime.now(UTC)).date()
        values = {"promotion_id": promotion_id, "day": today, column: 1}
        statement = (
            insert(PromotionDay)
            .values(**values)
            .on_conflict_do_update(
                constraint="uq_promotion_day",
                set_={column: getattr(PromotionDay, column) + 1},
            )
        )
        try:
            await self.session.execute(statement)
        except Exception as exc:  # noqa: BLE001 - a statistic must not fail a request
            logger.warning("promotion_count_failed", error=str(exc), column=column)

    # ------------------------------------------------------------- buying

    async def start(
        self,
        user: User,
        publisher: Publisher,
        *,
        experience_id: uuid.UUID,
        days: int,
        currency: str,
        city_slug: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        radius_km: float | None = None,
        return_url: str,
        callback_url: str,
        now: datetime | None = None,
    ) -> Promotion:
        """Buy a run. It shows nothing until the money arrives."""
        now = now or datetime.now(UTC)
        currency = (currency or "").upper()

        if not MIN_DAYS <= days <= MAX_DAYS:
            raise ValidationError(
                f"A promotion runs for between {MIN_DAYS} and {MAX_DAYS} days.",
                code="INVALID_PROMOTION_LENGTH",
            )

        experience = await self.session.get(Experience, experience_id)
        if experience is None or experience.deleted_at is not None:
            raise NotFoundError("That listing does not exist.", code="EXPERIENCE_NOT_FOUND")
        if experience.publisher_id != publisher.id:
            # Not found rather than forbidden: confirming a listing exists to
            # somebody guessing ids tells them they guessed right.
            raise NotFoundError("That listing does not exist.", code="EXPERIENCE_NOT_FOUND")
        if experience.status != STATUS_PUBLISHED:
            # Promoting a draft would put an id into circulation for something
            # nobody has published, which is the same objection that stops a
            # draft being reposted.
            raise ConflictError(
                "Publish the post before promoting it.", code="EXPERIENCE_NOT_PUBLISHED"
            )

        if not city_slug and (latitude is None or longitude is None):
            raise ValidationError(
                "Say where the promotion should reach - a city, or a point on the map.",
                code="PROMOTION_PLACE_REQUIRED",
            )

        amount_minor = price_for(days, currency)
        if amount_minor is None:
            raise ValidationError(
                f"Promotions are not sold in {currency} yet.",
                code="PROMOTION_NOT_SOLD_IN_CURRENCY",
            )

        promotion = Promotion(
            publisher_id=publisher.id,
            experience_id=experience.id,
            city_slug=city_slug,
            latitude=latitude,
            longitude=longitude,
            radius_km=radius_km if latitude is not None else None,
            # The run begins when it is paid for, not when it was bought. Dating
            # it from now would sell somebody a week and give them six days
            # because they finished the payment tomorrow morning.
            starts_at=now,
            ends_at=now + timedelta(days=days),
            status=STATUS_PENDING,
            amount_minor=amount_minor,
            currency=currency,
            reference=_reference(),
        )
        self.session.add(promotion)
        await self.session.flush()

        provider = payments.provider_for(currency)
        try:
            checkout = await provider.start(
                reference=promotion.reference,
                amount_minor=amount_minor,
                currency=currency,
                email=self._email_of(user),
                display_name=publisher.name,
                description=f"Promoting '{experience.title}' for {days} days",
                return_url=return_url,
                callback_url=callback_url,
            )
        except payments.PaymentError as exc:
            promotion.status = STATUS_REFUSED
            promotion.outcome_reason = "The payment could not be started."
            await self.session.flush()
            logger.warning("promotion_start_failed", error=str(exc))
            raise ConflictError(
                "Payments are unavailable right now. Nothing has been charged.",
                code="PAYMENT_UNAVAILABLE",
            ) from exc

        promotion.provider = checkout.provider
        promotion.provider_reference = checkout.provider_reference
        promotion.checkout_url = checkout.redirect_url
        await self.session.flush()
        logger.info(
            "promotion_started",
            promotion_id=str(promotion.id),
            days=days,
            amount_minor=amount_minor,
            currency=currency,
        )
        return promotion

    async def settle(
        self, promotion: Promotion, *, now: datetime | None = None
    ) -> Promotion:
        """Verify with the provider, then let it run."""
        now = now or datetime.now(UTC)
        if promotion.status != STATUS_PENDING or promotion.reference is None:
            return promotion

        provider = payments.provider_named(promotion.provider)
        status = await provider.verify(
            promotion.reference, provider_reference=promotion.provider_reference
        )
        if status.state != payments.PAID:
            return promotion
        if status.amount_minor != promotion.amount_minor:
            logger.error(
                "promotion_amount_mismatch",
                promotion_id=str(promotion.id),
                expected_minor=promotion.amount_minor,
                reported_minor=status.amount_minor,
            )
            raise ConflictError(
                "That payment does not match the promotion.", code="PROMOTION_AMOUNT_MISMATCH"
            )

        # The run starts now, and lasts what was bought - so a payment finished
        # a day late still buys the full week.
        length = promotion.ends_at - promotion.starts_at
        promotion.starts_at = now
        promotion.ends_at = now + length
        promotion.status = STATUS_ACTIVE
        promotion.paid_at = now
        promotion.checkout_url = None
        await self.session.flush()

        logger.info("promotion_active", promotion_id=str(promotion.id))
        return promotion

    async def by_reference(self, reference: str) -> Promotion | None:
        return (
            await self.session.execute(
                select(Promotion).where(Promotion.reference == reference)
            )
        ).scalar_one_or_none()

    async def for_publisher(self, publisher_id: uuid.UUID) -> list[Promotion]:
        result = await self.session.execute(
            select(Promotion)
            .where(Promotion.publisher_id == publisher_id)
            .order_by(Promotion.created_at.desc())
        )
        return list(result.scalars().all())

    async def performance(self, promotion_id: uuid.UUID) -> tuple[int, int]:
        row = (
            await self.session.execute(
                select(
                    func.coalesce(func.sum(PromotionDay.impressions), 0),
                    func.coalesce(func.sum(PromotionDay.clicks), 0),
                ).where(PromotionDay.promotion_id == promotion_id)
            )
        ).one()
        return int(row[0]), int(row[1])

    async def end_finished(self, *, now: datetime | None = None) -> int:
        """Move runs that are over to `ended`.

        Cosmetic rather than load-bearing: `slot_for` already filters on the
        dates, so a promotion whose run has finished stops being served whether
        or not this has run. It exists so a publisher's list reads correctly,
        which is why nothing depends on it happening promptly.
        """
        now = now or datetime.now(UTC)
        stale = (
            await self.session.execute(
                select(Promotion).where(
                    Promotion.status == STATUS_ACTIVE, Promotion.ends_at <= now
                )
            )
        ).scalars().all()
        for promotion in stale:
            promotion.status = STATUS_ENDED
        if stale:
            await self.session.flush()
        return len(stale)

    def _email_of(self, user: User) -> str:
        profile = getattr(user, "profile", None)
        email = (getattr(profile, "email", None) or "").strip()
        if not email:
            raise ValidationError(
                "Add an email address to your profile first - the payment provider "
                "sends the receipt there.",
                code="EMAIL_REQUIRED_FOR_PAYMENT",
            )
        return email


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance, good enough to decide whether a slot reaches.

    Plain haversine rather than PostGIS. The comparison is against a radius
    somebody chose by dragging a circle on a map, so metre-accuracy would be
    precision about an approximation - and doing it here keeps the whole
    eligibility decision in one readable place instead of half in SQL.
    """
    import math

    radius_of_earth_km = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    return radius_of_earth_km * 2 * math.asin(math.sqrt(a))


__all__ = ["DAILY_PRICE_MINOR", "PromotionService", "price_for"]
