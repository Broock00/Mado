"""Ranking and explanation.

This module is the part of discovery that spec 82.01 insists Mado must own outright:
the search vendor returns candidates, and everything below decides what the explorer
actually sees and why.

Three rules from the specs shape the design:

* **Context beats popularity** (spec PRODUCT-00 principle 4). Proximity, timing and
  fit are weighted above raw popularity, and popularity alone can never carry an
  item to the top.
* **No single signal dominates** (spec 21 s8). Every signal is normalised to 0-1 and
  contributes a bounded share, so one saturated score cannot swamp the rest.
* **Every recommendation explains itself** (spec PRODUCT-00 principle 5). Scoring
  records *which* signals fired, and the explanation is generated from that record
  rather than written independently - so the stated reason cannot drift from the
  real one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.domains.catalog.models import Experience

EARTH_RADIUS_KM = 6371.0

# Weights sum to 1.0. Tuning these is a product decision, so they live together,
# named, rather than scattered as literals through the scoring code.
WEIGHTS = {
    "relevance": 0.26,
    "proximity": 0.20,
    "timing": 0.18,
    "personalization": 0.14,
    "quality": 0.12,
    "popularity": 0.06,
    "trust": 0.04,
}

# Distance beyond which proximity stops discriminating: in a city this size,
# everything past ~12km is simply "across town".
PROXIMITY_HORIZON_KM = 12.0
# Walkable, per spec 57.02's "walking distance" filter.
WALKABLE_KM = 1.2


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


@dataclass(slots=True)
class RankingContext:
    """Everything known about the moment a request was made.

    Spec 56.01 s12 lists these as the context sources; gathering them into one
    object keeps the scoring functions pure and trivially testable.
    """

    now: datetime
    latitude: float | None = None
    longitude: float | None = None
    # The city's timezone, not the device's. Everything the explorer reads about
    # when something starts must be in the clock of the place it happens
    # (spec 56.02 s28).
    timezone: str = "Africa/Addis_Ababa"
    preferred_categories: set[str] = field(default_factory=set)
    preferred_tags: set[str] = field(default_factory=set)
    disliked_categories: set[str] = field(default_factory=set)
    budget: str | None = None
    is_raining: bool = False
    saved_experience_ids: set[str] = field(default_factory=set)

    @property
    def has_location(self) -> bool:
        return self.latitude is not None and self.longitude is not None


@dataclass(slots=True)
class ScoredExperience:
    experience: Experience
    score: float
    signals: dict[str, float]
    distance_km: float | None
    reason: str | None


def _proximity_score(experience: Experience, ctx: RankingContext) -> tuple[float, float | None]:
    """Return ``(score, distance_km)``.

    With no location the score is neutral rather than zero: an explorer who has not
    shared location should not have every result penalised.
    """
    venue = experience.venue
    if not ctx.has_location or venue is None:
        return 0.5, None

    distance = haversine_km(ctx.latitude, ctx.longitude, venue.latitude, venue.longitude)
    if distance >= PROXIMITY_HORIZON_KM:
        return 0.0, distance
    # Linear decay reads more predictably to users than an exponential one: twice
    # as far feels roughly half as convenient.
    return 1.0 - (distance / PROXIMITY_HORIZON_KM), distance


def _timing_score(experience: Experience, ctx: RankingContext) -> float:
    """Reward things that are actually available soon.

    Permanent places (a museum, a park) are not time-bound, so they receive a
    neutral score instead of being punished for having no next occurrence.
    """
    from app.domains.catalog.serializers import next_event_of

    if experience.type != "event":
        return 0.55

    next_event = next_event_of(experience, now=ctx.now)
    if next_event is None:
        # An event experience with nothing scheduled is stale by definition.
        return 0.1

    hours_away = (next_event.start_time - ctx.now).total_seconds() / 3600
    if hours_away < 0:
        return 0.0
    if hours_away <= 6:
        return 1.0
    if hours_away <= 24:
        return 0.85
    if hours_away <= 72:
        return 0.65
    if hours_away <= 24 * 7:
        return 0.45
    return 0.25


def _personalization_score(experience: Experience, ctx: RankingContext) -> float:
    """Fit against stated and inferred preferences.

    Starts neutral so a cold-start explorer sees a sane ordering rather than an
    arbitrary one (spec DISC-002 forbids an empty or low-quality cold start).
    """
    score = 0.5
    category_slug = experience.category.slug if experience.category else None

    if category_slug and category_slug in ctx.disliked_categories:
        # An explicit "not interested" is a strong negative, not a tiebreaker.
        return 0.0
    if category_slug and category_slug in ctx.preferred_categories:
        score += 0.3

    tag_slugs = {tag.slug for tag in (experience.tags or [])}
    overlap = tag_slugs & ctx.preferred_tags
    if overlap:
        score += min(0.2, 0.07 * len(overlap))

    if ctx.budget == "free" and experience.price_type == "free":
        score += 0.15
    elif ctx.budget in {"budget", "moderate"} and experience.price_type == "free":
        score += 0.05

    # Weather is transient context, not a preference, but it changes what is a
    # good idea right now (spec DISC-004).
    if ctx.is_raining and experience.is_indoor:
        score += 0.12
    elif ctx.is_raining and experience.is_indoor is False:
        score -= 0.18

    return max(0.0, min(1.0, score))


def _quality_score(experience: Experience) -> float:
    quality = float(experience.quality_score or 0.5)
    if experience.rating_average is not None and experience.rating_count >= 3:
        normalised_rating = (float(experience.rating_average) - 1.0) / 4.0
        # Ratings are evidence, but thin evidence; blend rather than replace.
        confidence = min(1.0, experience.rating_count / 25)
        quality = quality * (1 - confidence * 0.6) + normalised_rating * (confidence * 0.6)
    return max(0.0, min(1.0, quality))


def _trust_score(experience: Experience) -> float:
    publisher = experience.publisher
    if publisher is None:
        return 0.3
    # Spec BUSINESS-07 trust tiers 0-3, normalised.
    base = min(1.0, publisher.trust_level / 3)
    if publisher.verification_status != "verified":
        base *= 0.6
    return base


def _build_reason(
    experience: Experience,
    signals: dict[str, float],
    distance_km: float | None,
    ctx: RankingContext,
) -> str | None:
    """Explain the ranking in the explorer's language.

    Derived from the same signal dict that produced the score, and phrased as spec
    DISC-003 illustrates ("Five minutes from your hotel", "Great rainy-day option").
    Returns the single strongest reason - stacking several reads as justification,
    not explanation.
    """
    from app.domains.catalog.serializers import next_event_of

    if distance_km is not None and distance_km <= WALKABLE_KM:
        minutes = max(1, round(distance_km / 0.08))  # ~4.8 km/h walking pace
        return f"About {minutes} min walk from you"

    if experience.type == "event":
        next_event = next_event_of(experience, now=ctx.now)
        if next_event is not None:
            hours_away = (next_event.start_time - ctx.now).total_seconds() / 3600
            if 0 <= hours_away <= 3:
                return "Starting soon"
            if 0 <= hours_away <= 12:
                return "Happening tonight"
            if 0 <= hours_away <= 48:
                return (
                    "Happening this weekend"
                    if next_event.start_time.weekday() >= 4
                    else "Coming up tomorrow"
                )

    if ctx.is_raining and experience.is_indoor:
        return "Good rainy-day option"

    category_slug = experience.category.slug if experience.category else None
    if category_slug and category_slug in ctx.preferred_categories:
        return f"Matches your interest in {experience.category.name.lower()}"

    matching_tags = {tag.slug for tag in (experience.tags or [])} & ctx.preferred_tags
    if matching_tags:
        tag_name = next(tag.name for tag in experience.tags if tag.slug in matching_tags)
        return f"Because you like {tag_name.lower()}"

    if experience.price_type == "free":
        return "Free to attend"

    if signals.get("quality", 0) >= 0.75 and experience.rating_count >= 5:
        return f"Highly rated by {experience.rating_count} explorers"

    if distance_km is not None and distance_km <= 4:
        return f"{distance_km:.1f} km away"

    if signals.get("popularity", 0) >= 0.7:
        return "Popular in the city right now"

    return None


def score_experience(
    experience: Experience,
    ctx: RankingContext,
    *,
    relevance: float = 0.5,
) -> ScoredExperience:
    """Score one candidate. ``relevance`` is the retrieval score, already 0-1."""
    proximity, distance_km = _proximity_score(experience, ctx)
    signals = {
        "relevance": max(0.0, min(1.0, relevance)),
        "proximity": proximity,
        "timing": _timing_score(experience, ctx),
        "personalization": _personalization_score(experience, ctx),
        "quality": _quality_score(experience),
        "popularity": float(experience.popularity_score or 0.0),
        "trust": _trust_score(experience),
    }
    total = sum(WEIGHTS[name] * value for name, value in signals.items())
    reason = _build_reason(experience, signals, distance_km, ctx)
    return ScoredExperience(
        experience=experience,
        score=round(total, 6),
        signals=signals,
        distance_km=round(distance_km, 2) if distance_km is not None else None,
        reason=reason,
    )


def rank(
    experiences: list[Experience],
    ctx: RankingContext,
    *,
    relevance_by_id: dict[str, float] | None = None,
    diversify: bool = True,
    limit: int | None = None,
) -> list[ScoredExperience]:
    """Score, optionally diversify, and truncate."""
    relevance_by_id = relevance_by_id or {}
    scored = [
        score_experience(exp, ctx, relevance=relevance_by_id.get(str(exp.id), 0.5))
        for exp in experiences
    ]
    scored.sort(key=lambda item: item.score, reverse=True)
    if diversify:
        scored = apply_diversity(scored)
    return scored[:limit] if limit else scored


def apply_diversity(
    scored: list[ScoredExperience], *, max_per_category: int = 3
) -> list[ScoredExperience]:
    """Prevent one category from monopolising a feed.

    Spec PRODUCT-00 principle 14 asks for serendipity and spec 21 s3 for diversity.
    Rather than reordering by a penalty term - which makes scores hard to reason
    about - overflow items are deferred to the tail, so they stay reachable but
    stop crowding the top.
    """
    seen: dict[str, int] = {}
    primary: list[ScoredExperience] = []
    deferred: list[ScoredExperience] = []

    for item in scored:
        category = item.experience.category.slug if item.experience.category else "uncategorised"
        count = seen.get(category, 0)
        if count < max_per_category:
            primary.append(item)
            seen[category] = count + 1
        else:
            deferred.append(item)

    return primary + deferred


def freshness_decay(published_at: datetime | None, *, now: datetime | None = None) -> float:
    """Half-life decay used when refreshing stored popularity/trend scores."""
    if published_at is None:
        return 0.5
    reference = now or datetime.now(UTC)
    age_days = max(0.0, (reference - published_at).total_seconds() / 86400)
    return 0.5 ** (age_days / 30)


def within_window(value: datetime, start: datetime, end: datetime) -> bool:
    return start <= value <= end


def weekend_window(now: datetime) -> tuple[datetime, datetime]:
    """The upcoming weekend, as an explorer means it.

    Friday evening through Sunday close - not the calendar's Saturday-Sunday. On a
    Saturday, "this weekend" means the one in progress, not the next one.
    """
    weekday = now.weekday()  # Monday = 0
    days_until_friday = (4 - weekday) % 7
    if weekday >= 4:  # already Fri/Sat/Sun
        days_until_friday = 0
    friday = (now + timedelta(days=days_until_friday)).replace(
        hour=17, minute=0, second=0, microsecond=0
    )
    if weekday >= 4 and now > friday:
        friday = now
    sunday_end = (friday + timedelta(days=(6 - friday.weekday()))).replace(
        hour=23, minute=59, second=59, microsecond=0
    )
    return friday, sunday_end


def tonight_window(now: datetime) -> tuple[datetime, datetime]:
    """From now until the small hours - "tonight" survives past midnight."""
    end = (now + timedelta(days=1)).replace(hour=4, minute=0, second=0, microsecond=0)
    if now.hour < 4:
        end = now.replace(hour=4, minute=0, second=0, microsecond=0)
    return now, end
