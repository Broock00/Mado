"""Publisher reputation (spec TRST-004, Trust Architecture §7-10, §20).

Verification says somebody proved who they are once. Reputation says what they
have done since. The spec is explicit that trust should be "continuously
recalculated using historical behavior instead of relying solely on initial
verification", and until now `trust_level` was only the first half of that: a
tier a moderator set and nothing ever changed.

**Reputation is not shown to explorers as a number.** They see the verified
badge and the ratings other people left, which are things they can interpret.
"Trust: 0.68" on a card is three bad ideas at once - it is meaningless without
the scale, it is an invitation to work out what moves it, and it compounds: a
low score suppresses a publisher in ranking, which costs them the bookings and
reviews they would need to earn a better one. The number steers ranking and is
shown in full to the publisher it describes, and to moderators. Nobody else.

**It cannot punish on its own.** Spec BUSINESS-07: automated systems detect,
humans decide. A poor reputation lowers a publisher's ranking; it never
withholds, suspends or rejects. The moderation queue is where consequences
happen, with a person in the loop.

**Thin evidence is reported as thin, not as a score.** Two five-star reviews is
not an excellent publisher, it is an unproven one, and rendering that as 0.95
would be the platform inventing confidence it does not have. Below the evidence
floor the reputation is `provisional` and the interface says so instead of
showing a flattering figure.

**Recent behaviour counts for more.** Not to be generous, but because the
question a reputation answers is "what will happen if I book with them next
week", and a cancellation two years ago is weak evidence about that. It also
means recovery is possible, which the spec asks for by name (§20) - a publisher
with no way back has no reason to improve.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.domains.catalog.models import (
    MODERATION_REJECTED,
    STATUS_PUBLISHED,
    EventInstance,
    Experience,
)
from app.domains.publisher.models import Publisher

logger = get_logger("mado.reputation")

# How far back evidence is read. Two years is long enough to see a pattern and
# short enough that a publisher is not answering for a different business.
WINDOW = timedelta(days=730)

# Evidence inside this window counts at full weight; older evidence decays
# towards half. Not a cliff: a cancellation does not stop mattering on its
# birthday.
RECENT = timedelta(days=180)
OLD_WEIGHT = 0.5

# Below this much evidence the score is provisional. Chosen so that a publisher
# who has actually run a season of events clears it and somebody with a single
# listing does not.
MIN_COMPLETED_DATES = 3
MIN_RATINGS = 5

# The prior the cancellation rate is smoothed towards: six dates' worth of a
# typical publisher, cancelling one date in twenty. Six because it is roughly a
# season of a monthly night - enough that a single cancellation early on does
# not define somebody, and light enough that a real pattern shows through within
# a couple of dozen dates.
PRIOR_DATES = 6.0
PRIOR_RATE = 0.05

# Where a publisher starts. Deliberately mid-scale rather than zero: a new
# publisher is unknown, not bad, and starting everybody at the bottom would make
# the first listing impossible to get seen.
NEUTRAL = 0.5

BAND_EXCELLENT = "excellent"
BAND_GOOD = "good"
BAND_MIXED = "mixed"
BAND_POOR = "poor"
BAND_PROVISIONAL = "provisional"


@dataclass(slots=True)
class Signal:
    """One thing that moved the score, in words.

    The score is never shown without these. A number somebody cannot act on is
    a grievance rather than feedback, and the recovery path the spec asks for
    (§20) is exactly "which of these do I fix".
    """

    key: str
    label: str
    # -1 to 1. Negative pulls the score down.
    direction: float
    detail: str


@dataclass(slots=True)
class Reputation:
    score: float
    band: str
    signals: list[Signal] = field(default_factory=list)
    completed_dates: int = 0
    cancelled_dates: int = 0
    ratings: int = 0
    reports: int = 0
    withheld_listings: int = 0

    @property
    def is_provisional(self) -> bool:
        return self.band == BAND_PROVISIONAL

    def as_dict(self) -> dict:
        return {
            "score": round(self.score, 3),
            "band": self.band,
            "completedDates": self.completed_dates,
            "cancelledDates": self.cancelled_dates,
            "ratings": self.ratings,
            "reports": self.reports,
            "withheldListings": self.withheld_listings,
            "signals": [
                {
                    "key": s.key,
                    "label": s.label,
                    "direction": round(s.direction, 3),
                    "detail": s.detail,
                }
                for s in self.signals
            ],
        }


def _weight(when: datetime | None, now: datetime) -> float:
    """How much a piece of evidence from `when` counts."""
    if when is None:
        return OLD_WEIGHT
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return 1.0 if now - when <= RECENT else OLD_WEIGHT


def band_for(score: float, *, provisional: bool) -> str:
    if provisional:
        return BAND_PROVISIONAL
    if score >= 0.8:
        return BAND_EXCELLENT
    if score >= 0.6:
        return BAND_GOOD
    if score >= 0.4:
        return BAND_MIXED
    return BAND_POOR


def compute(
    publisher: Publisher,
    experiences: list[Experience],
    occurrences: list[EventInstance],
    *,
    now: datetime | None = None,
) -> Reputation:
    """Score a publisher from what they have actually done.

    Pure: everything it needs is passed in, so the arithmetic is testable
    without a database and the query that gathers the evidence lives in one
    place (`refresh_all`).
    """
    now = now or datetime.now(UTC)
    cutoff = now - WINDOW

    published = [e for e in experiences if e.status == STATUS_PUBLISHED]
    withheld = [e for e in experiences if e.moderation_status == MODERATION_REJECTED]

    # Only dates that have already happened. A future date is a promise, and
    # counting promises as delivery is how a publisher who has run nothing at
    # all ends up looking reliable.
    past = [o for o in occurrences if _at(o.start_time) < now and _at(o.start_time) >= cutoff]
    cancelled = [o for o in past if o.status == "cancelled"]
    completed = [o for o in past if o.status != "cancelled"]

    ratings_total = sum(e.rating_count or 0 for e in published)
    reports_total = sum(e.report_count or 0 for e in published)

    signals: list[Signal] = []
    score = NEUTRAL

    # --- reliability, the heaviest signal ---------------------------------
    #
    # Cancelling is the specific harm a publisher can do to somebody who
    # planned an evening around them, and it is the one thing a reputation
    # most needs to predict.
    if past:
        weighted_cancelled = sum(_weight(o.start_time, now) for o in cancelled)
        weighted_past = sum(_weight(o.start_time, now) for o in past)

        # Smoothed towards the expected rate rather than taken raw. An
        # unsmoothed ratio treats one cancellation out of eight exactly like a
        # hundred out of eight hundred - but the first is one bad night and the
        # second is a proven pattern, and only the second is evidence about what
        # happens next. The prior is worth PRIOR_DATES dates at PRIOR_RATE, so a
        # publisher with a short record sits near the baseline and has to earn
        # their way away from it in either direction.
        cancellation_rate = (weighted_cancelled + PRIOR_DATES * PRIOR_RATE) / (
            weighted_past + PRIOR_DATES
        )

        # A rate of 0 is worth +0.25, falling to -0.35 by 20% and continuing
        # down to -0.50 at 100%. Not symmetric on purpose: turning up is the
        # baseline expectation, so doing it earns less than failing to costs.
        #
        # The curve keeps falling past 20% rather than flattening there, and
        # that matters more than it looks. Saturating at the knee makes a
        # publisher who cancels half their dates score exactly the same as one
        # who cancels a fifth - so halving your cancellation rate shows up as
        # no improvement at all, and the recovery the spec asks for by name
        # (§20) has no gradient to climb in the range where it is most needed.
        if cancellation_rate <= 0.20:
            movement = 0.25 - (cancellation_rate / 0.20) * 0.60
        else:
            movement = -0.35 - ((cancellation_rate - 0.20) / 0.80) * 0.15

        # The wording follows what actually happened, not the smoothed figure a
        # publisher never sees and could not check. Telling somebody who
        # cancelled nothing that their rate is 4% would be true of the model and
        # false of their year.
        detail = (
            f"{len(completed)} dates went ahead, none cancelled."
            if not cancelled
            else f"{len(cancelled)} of {len(past)} recent dates were cancelled."
        )

        score += movement
        signals.append(
            Signal("reliability", "Dates that went ahead", movement / 0.35, detail)
        )

    # --- what people thought ----------------------------------------------
    #
    # Weighted by how many people said it, so one listing with 200 reviews is
    # not outvoted by three with two each - the same rule the publisher
    # dashboard already uses so the two numbers cannot disagree.
    rated = [e for e in published if e.rating_average is not None and (e.rating_count or 0) > 0]
    if ratings_total >= MIN_RATINGS and rated:
        weighted = sum(float(e.rating_average) * (e.rating_count or 0) for e in rated)
        mean = weighted / sum(e.rating_count or 0 for e in rated)
        # 1-5 onto -0.25..+0.25, with 3.5 as the neutral point rather than 3:
        # people rate things they chose to go to, so the honest midpoint of the
        # distribution sits above the midpoint of the scale.
        movement = max(-0.25, min(0.25, (mean - 3.5) / 1.5 * 0.25))
        score += movement
        signals.append(
            Signal(
                "ratings",
                "What people thought",
                movement / 0.25,
                f"{mean:.1f} out of 5 across {ratings_total} ratings.",
            )
        )

    # --- complaints --------------------------------------------------------
    if published:
        per_listing = reports_total / len(published)
        if per_listing > 0:
            # One report on one listing is noise; a report on every listing is
            # a pattern. Capped so a brigading campaign cannot bottom out a
            # publisher on its own - the moderation queue handles that, with a
            # person reading the reports.
            movement = -min(0.25, per_listing * 0.12)
            score += movement
            signals.append(
                Signal(
                    "reports",
                    "Reports from readers",
                    movement / 0.25,
                    f"{reports_total} reports across {len(published)} listings.",
                )
            )

    # --- policy ------------------------------------------------------------
    if withheld:
        movement = -min(0.30, 0.15 * len(withheld))
        score += movement
        signals.append(
            Signal(
                "policy",
                "Listings withheld by moderation",
                movement / 0.30,
                f"{len(withheld)} withheld after review.",
            )
        )

    # --- identity ----------------------------------------------------------
    #
    # A bonus, not the score. Making verification decisive would recreate the
    # thing TRST-004 exists to fix: a publisher who verified once and has been
    # cancelling on people ever since outranking one who has quietly delivered
    # forty events.
    if publisher.verification_status == "verified":
        score += 0.10
        signals.append(
            Signal("verified", "Identity verified", 1.0, "A moderator confirmed who they are.")
        )

    # --- tenure ------------------------------------------------------------
    #
    # Small and capped. Time is not virtue, but an account that has been around
    # for two years is a worse target for somebody buying a reputation.
    tenure_days = (now - _at(publisher.created_at)).days if publisher.created_at else 0
    if tenure_days >= 180:
        movement = min(0.05, tenure_days / 730 * 0.05)
        score += movement
        signals.append(
            Signal(
                "tenure",
                "Time on Mado",
                movement / 0.05,
                f"Publishing here for {tenure_days // 30} months.",
            )
        )

    score = max(0.0, min(1.0, score))
    provisional = len(completed) < MIN_COMPLETED_DATES and ratings_total < MIN_RATINGS

    return Reputation(
        score=score,
        band=band_for(score, provisional=provisional),
        signals=sorted(signals, key=lambda s: s.direction),
        completed_dates=len(completed),
        cancelled_dates=len(cancelled),
        ratings=ratings_total,
        reports=reports_total,
        withheld_listings=len(withheld),
    )


def _at(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def for_publisher(session: AsyncSession, publisher: Publisher) -> Reputation:
    """Compute one publisher's reputation from the database."""
    experiences = list(
        (
            await session.execute(
                select(Experience).where(
                    Experience.publisher_id == publisher.id, Experience.deleted_at.is_(None)
                )
            )
        ).scalars()
    )
    occurrences: list[EventInstance] = []
    if experiences:
        occurrences = list(
            (
                await session.execute(
                    select(EventInstance).where(
                        EventInstance.experience_id.in_([e.id for e in experiences])
                    )
                )
            ).scalars()
        )
    return compute(publisher, experiences, occurrences)


async def refresh_all(session: AsyncSession) -> dict[str, int]:
    """Recompute every publisher's reputation.

    On a timer rather than on a request, for the same reason popularity and
    trend are: ranking reads this on every discovery call, and computing it per
    request would be a query per publisher per page.

    Stored alongside the signals that produced it, so the dashboard and the
    moderation console explain the number from the same evidence rather than
    each deriving their own explanation and disagreeing.
    """
    publishers = list((await session.execute(select(Publisher))).scalars())
    if not publishers:
        return {"publishers": 0, "provisional": 0}

    experiences = list(
        (await session.execute(select(Experience).where(Experience.deleted_at.is_(None)))).scalars()
    )
    occurrences = list((await session.execute(select(EventInstance))).scalars())

    by_publisher: dict[uuid.UUID, list[Experience]] = {}
    for experience in experiences:
        by_publisher.setdefault(experience.publisher_id, []).append(experience)

    by_experience: dict[uuid.UUID, list[EventInstance]] = {}
    for occurrence in occurrences:
        by_experience.setdefault(occurrence.experience_id, []).append(occurrence)

    now = datetime.now(UTC)
    provisional = 0
    for publisher in publishers:
        owned = by_publisher.get(publisher.id, [])
        dates = [o for e in owned for o in by_experience.get(e.id, [])]
        reputation = compute(publisher, owned, dates, now=now)

        publisher.reputation_score = reputation.score
        publisher.reputation_signals = reputation.as_dict()
        publisher.reputation_computed_at = now
        if reputation.is_provisional:
            provisional += 1

    logger.info(
        "reputation_recomputed", publishers=len(publishers), provisional=provisional
    )
    return {"publishers": len(publishers), "provisional": provisional}
