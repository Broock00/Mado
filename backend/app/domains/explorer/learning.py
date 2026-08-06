"""The learning loop.

``InteractionEvent`` rows have been written on every card view, save and dismiss
since the platform started. Nothing read them. This module closes that loop: it
turns the behavioural stream into preferences that steer ranking, and into the
popularity and trend figures the catalog carries.

Spec 10.01.02 calls this "Progressive Learning" and attaches conditions to it that
shape the design:

* **Stated beats inferred.** Someone who picked "Music" during onboarding said so;
  someone who opened three music listings might have been killing time. Inferences
  are returned separately from stated preferences and carry less weight in
  scoring - they never overwrite what the explorer told us.

* **Recent behaviour counts for more.** Interest moves. A save from last week is
  worth more than a save from last spring, so every event is discounted by age
  before it is counted.

* **Confidence grows with evidence.** One tap is noise. The strength of an
  inference is a function of how much behaviour supports it, which is why volume
  is folded in rather than the raw ratio being used directly.

* **Personalization is opt-in** (spec PRODUCT-00 principle 9). Every read checks
  ``personalizationEnabled`` first.

Negative signals are treated asymmetrically. "Not interested" is an explicit
instruction and is trusted immediately; a passing view is weak evidence of
anything. The weights below encode that difference deliberately.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging import get_logger
from app.domains.catalog.models import Experience
from app.domains.explorer.models import InteractionEvent

logger = get_logger("mado.explorer.learning")

# What each action says about interest. Saving something is a deliberate act;
# scrolling past it in a feed is barely evidence at all. "not_interested" is the
# strongest negative because it is the explorer telling us directly, and spec
# DISC-002 requires that instruction to be honoured rather than averaged away.
ACTION_WEIGHTS: dict[str, float] = {
    "complete": 4.0,
    "save": 3.0,
    "share": 2.5,
    "open_details": 1.0,
    "view": 0.25,
    "unsave": -2.0,
    "dismiss": -2.0,
    "not_interested": -5.0,
}

# Days over which a signal loses half its weight. Roughly a season: long enough
# that a genuine interest survives a quiet month, short enough that last year's
# phase stops shaping today's feed.
SIGNAL_HALF_LIFE_DAYS = 60.0

# Events older than this are not read at all. Purely a cost control - they would
# be discounted into irrelevance anyway, and scanning them wastes the index.
LOOKBACK_DAYS = 240

# Decayed weight at which an inference is worth acting on. Below it, one stray tap
# would be enough to start steering results.
MIN_AFFINITY_EVIDENCE = 2.0

# Affinity at or below which a category is treated as actively unwanted.
DISLIKE_THRESHOLD = -2.0

# Evidence at which an inference reaches full confidence. Chosen so a handful of
# deliberate actions counts, while a single one clearly does not.
CONFIDENCE_SATURATION = 12.0


@dataclass(slots=True)
class InferredPreferences:
    """What behaviour suggests, kept apart from what the explorer stated.

    Categories and tags map to a signed affinity in roughly -1..1. Positive means
    "more of this", negative means "less". They are returned as weights rather
    than sets because an inference is a matter of degree - collapsing it to a
    boolean throws away the only thing that distinguishes a hunch from a pattern.
    """

    categories: dict[str, float] = field(default_factory=dict)
    tags: dict[str, float] = field(default_factory=dict)
    disliked_categories: set[str] = field(default_factory=set)
    # How much behaviour this is built on, 0-1. The ranker scales the whole
    # personalization signal by it, so a new explorer is not confidently
    # personalised on the strength of two taps.
    confidence: float = 0.0
    event_count: int = 0

    @property
    def is_useful(self) -> bool:
        return bool(self.categories or self.tags or self.disliked_categories)


def personalization_enabled(privacy: dict | None) -> bool:
    """Whether behaviour may be read back for this explorer."""
    if privacy is None:
        return False
    return bool(privacy.get("personalizationEnabled", True))


def _decay(occurred_at: datetime, now: datetime) -> float:
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    age_days = max(0.0, (now - occurred_at).total_seconds() / 86400.0)
    return 0.5 ** (age_days / SIGNAL_HALF_LIFE_DAYS)


async def infer_preferences(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    anonymous_id: str | None = None,
    privacy: dict | None = None,
    now: datetime | None = None,
) -> InferredPreferences:
    """Derive category and tag affinities from an explorer's behaviour."""
    if user_id is None and anonymous_id is None:
        return InferredPreferences()
    # Anonymous explorers have no profile to carry consent, so personalization is
    # session-shaped rather than persistent - and only when a caller opts them in.
    if user_id is not None and not personalization_enabled(privacy):
        return InferredPreferences()

    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=LOOKBACK_DAYS)

    stmt = select(InteractionEvent).where(
        InteractionEvent.occurred_at >= cutoff,
        InteractionEvent.entity_id.is_not(None),
        InteractionEvent.entity_type == "experience",
    )
    stmt = (
        stmt.where(InteractionEvent.user_id == user_id)
        if user_id is not None
        else stmt.where(InteractionEvent.anonymous_id == anonymous_id)
    )

    events = list((await session.execute(stmt)).scalars().all())
    if not events:
        return InferredPreferences()

    # One query for the experiences actually touched, rather than a join: the
    # events table deliberately carries no foreign key into catalog (see the model
    # docstring), and adding one to make this convenient would put a write-path
    # constraint on a hot append-only table.
    touched = {event.entity_id for event in events if event.entity_id}
    experiences = await _load_experiences(session, touched)

    category_scores: dict[str, float] = defaultdict(float)
    tag_scores: dict[str, float] = defaultdict(float)
    total_evidence = 0.0

    for event in events:
        experience = experiences.get(event.entity_id)
        if experience is None:
            continue
        weight = ACTION_WEIGHTS.get(event.action)
        if weight is None:
            continue

        signal = weight * _decay(event.occurred_at, now) * max(1, event.weight or 1)
        total_evidence += abs(signal)

        if experience.category is not None:
            category_scores[experience.category.slug] += signal
        for tag in experience.tags or []:
            # Tags are diluted relative to categories: a listing carries several,
            # so an interaction is much weaker evidence about any one of them.
            tag_scores[tag.slug] += signal * 0.4

    confidence = min(1.0, total_evidence / CONFIDENCE_SATURATION)

    categories = {
        slug: _normalise_affinity(score)
        for slug, score in category_scores.items()
        if abs(score) >= MIN_AFFINITY_EVIDENCE
    }
    tags = {
        slug: _normalise_affinity(score)
        for slug, score in tag_scores.items()
        if abs(score) >= MIN_AFFINITY_EVIDENCE
    }
    disliked = {slug for slug, score in category_scores.items() if score <= DISLIKE_THRESHOLD}

    return InferredPreferences(
        categories={s: v for s, v in categories.items() if s not in disliked},
        tags=tags,
        disliked_categories=disliked,
        confidence=confidence,
        event_count=len(events),
    )


def _normalise_affinity(score: float) -> float:
    """Squash an unbounded signal sum into -1..1.

    ``tanh`` rather than a hard clamp: it saturates smoothly, so the difference
    between strong and overwhelming interest stops mattering long before the
    number stops growing, and no single burst of activity can peg a category.
    """
    return math.tanh(score / 8.0)


async def _load_experiences(
    session: AsyncSession, ids: set[uuid.UUID]
) -> dict[uuid.UUID, Experience]:
    if not ids:
        return {}
    result = await session.execute(
        select(Experience)
        .where(Experience.id.in_(ids))
        .options(selectinload(Experience.tags), selectinload(Experience.category))
    )
    return {experience.id: experience for experience in result.scalars().unique().all()}


# --- catalog-level aggregates ------------------------------------------------


# Window over which popularity is measured. Long enough to be stable, short enough
# that a listing which stopped drawing anyone eventually stops being "popular".
POPULARITY_WINDOW_DAYS = 30
# Trend compares the last few days against that baseline.
TREND_WINDOW_DAYS = 7
# Below this many recent interactions, a trend ratio is noise: going from one
# interaction to three is a 200% rise and means nothing.
MIN_EVENTS_FOR_TREND = 5

# How much excess traffic counts as "fully trending". At 1.5, a listing needs
# roughly three times its baseline to approach the top of the scale, which leaves
# the middle of the range free to actually distinguish between candidates.
TREND_SENSITIVITY = 1.5


async def recompute_engagement_scores(
    session: AsyncSession, *, now: datetime | None = None
) -> dict[str, int]:
    """Rebuild ``popularity_score`` and ``trend_score`` from real behaviour.

    These columns were seeded with fixed values, which meant the "Trending" rail
    was showing whatever the seed author picked. They now describe what explorers
    actually did.

    The two measure different things on purpose:

    * **Popularity** is total weighted engagement over the window, scaled against
      the busiest listing. It answers "is this a big draw".
    * **Trend** is recent engagement against that same listing's own baseline. It
      answers "is this rising" - which is what a Trending rail should surface, and
      why a permanently busy landmark should not monopolise it.

    Run as a maintenance pass rather than per request: it is a full scan of the
    event window, and the numbers do not need to be current to the second.
    """
    now = now or datetime.now(UTC)
    popularity_cutoff = now - timedelta(days=POPULARITY_WINDOW_DAYS)
    trend_cutoff = now - timedelta(days=TREND_WINDOW_DAYS)

    # Negative actions are excluded rather than subtracted: a dismissed listing is
    # not "unpopular", it is unpopular *with that explorer*. Engagement here means
    # attention drawn.
    positive = [action for action, weight in ACTION_WEIGHTS.items() if weight > 0]

    totals = await _engagement_since(session, popularity_cutoff, positive)
    recent = await _engagement_since(session, trend_cutoff, positive)
    if not totals:
        logger.info("engagement_recompute_no_events")
        return {"updated": 0, "with_activity": 0}

    busiest = max(weight for weight, _ in totals.values()) or 1.0
    # The share of the window the trend sample covers, used to work out what
    # "normal" recent activity would look like for a listing.
    expected_share = TREND_WINDOW_DAYS / POPULARITY_WINDOW_DAYS

    experiences = list((await session.execute(select(Experience))).scalars().all())
    updated = 0

    for experience in experiences:
        total_weight, total_count = totals.get(experience.id, (0.0, 0))
        popularity = min(1.0, total_weight / busiest)

        recent_weight, _ = recent.get(experience.id, (0.0, 0))
        expected = total_weight * expected_share
        if total_count < MIN_EVENTS_FOR_TREND or expected <= 0:
            # Not enough history to say anything. Zero rather than a guess: a
            # listing with three interactions does not belong in a Trending rail
            # however steeply those three arrived.
            trend = 0.0
        else:
            # Observed recent activity against what this listing's own baseline
            # predicts, centred on 1.0 and squashed so nothing trends infinitely.
            #
            # Divided by TREND_SENSITIVITY before squashing. Without it tanh is far
            # too steep for this: a listing drawing twice its usual traffic scored
            # 0.8, the same as one drawing five times, and the Trending rail
            # flattened into an arbitrary ordering of near-identical scores. A
            # trend signal that cannot rank is not a signal.
            excess = (recent_weight / expected) - 1.0
            trend = max(0.0, min(1.0, math.tanh(excess / TREND_SENSITIVITY)))

        if (
            abs(float(experience.popularity_score or 0) - popularity) > 1e-4
            or abs(float(experience.trend_score or 0) - trend) > 1e-4
        ):
            experience.popularity_score = round(popularity, 4)
            experience.trend_score = round(trend, 4)
            updated += 1

    await session.flush()
    logger.info(
        "engagement_recompute_complete",
        updated=updated,
        experiences=len(experiences),
        with_activity=len(totals),
    )
    return {"updated": updated, "with_activity": len(totals)}


async def _engagement_since(
    session: AsyncSession, cutoff: datetime, actions: list[str]
) -> dict[uuid.UUID, tuple[float, int]]:
    """Weighted engagement and raw event count per experience since a cutoff.

    Both figures are needed and they answer different questions: the weight says
    how much interest was shown, the count says whether there is enough of it to
    draw a conclusion from. Aggregated in SQL rather than in Python because this
    reads the whole event window - the one query here big enough for the
    difference to matter.
    """
    weighted = func.sum(
        func.coalesce(InteractionEvent.weight, 1)
        * case(
            *[(InteractionEvent.action == action, ACTION_WEIGHTS[action]) for action in actions],
            else_=0.0,
        )
    )
    result = await session.execute(
        select(InteractionEvent.entity_id, weighted, func.count())
        .where(
            InteractionEvent.occurred_at >= cutoff,
            InteractionEvent.entity_type == "experience",
            InteractionEvent.entity_id.is_not(None),
            InteractionEvent.action.in_(actions),
        )
        .group_by(InteractionEvent.entity_id)
    )
    return {row[0]: (float(row[1] or 0.0), int(row[2] or 0)) for row in result}
