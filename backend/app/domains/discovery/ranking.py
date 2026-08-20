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

from app.domains.catalog import suitability as suitability_vocab
from app.integrations.weather import DailyWeather

if TYPE_CHECKING:
    from app.domains.catalog.models import Experience

EARTH_RADIUS_KM = 6371.0

# Weights sum to 1.0. Tuning these is a product decision, so they live together,
# named, rather than scattered as literals through the scoring code.
#
# Browsing: the explorer has stated no intent, so context carries the feed. This is
# spec PRODUCT-00 principle 4 - what is near, open and fitting beats what is popular.
BROWSE_WEIGHTS = {
    "relevance": 0.24,
    "proximity": 0.18,
    "timing": 0.16,
    "fit": 0.12,
    "personalization": 0.12,
    "quality": 0.10,
    "popularity": 0.05,
    "trust": 0.03,
}

# Searching: the explorer has typed their intent, which is the strongest context
# signal available - stronger than anything inferred. Under the browse profile a
# query for "buna" returned a music event first, because a 0.26 relevance share
# could not outweigh a well-timed nearby item. That is correct for a feed and wrong
# for a search box.
#
# Relevance is capped below 0.50: the largest share that still cannot outweigh
# every other signal combined, which keeps spec 21 s8's "no single signal dominates"
# literally true while letting the query lead.
SEARCH_WEIGHTS = {
    "relevance": 0.46,
    "proximity": 0.11,
    "timing": 0.10,
    "fit": 0.11,
    "personalization": 0.08,
    "quality": 0.07,
    "popularity": 0.04,
    "trust": 0.03,
}

# Default profile, kept under the original name so existing callers are unaffected.
WEIGHTS = BROWSE_WEIGHTS

# The most a fully-confident behavioural inference may move the personalization
# signal. Below the 0.3 a stated category preference contributes, because spec
# 10.01.02 is explicit that stated preferences outrank inferred ones - and because
# an inference that can outvote a stated choice makes the preference screen a lie.
INFERENCE_CEILING = 0.22

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
    # The city's currency, for the same reason: a total shown to somebody in
    # Paris has to be in euros. Carried here rather than looked up wherever a
    # number is rendered, which is how a plan came to quote birr in every city.
    currency: str = "ETB"
    # True when the coordinates above are somewhere the explorer is asking about
    # rather than somewhere they are standing. Proximity still orders results -
    # what is central to New York is a better answer than what is not - but the
    # reply must not say "ten minutes from you" about a city on another
    # continent, which it will happily do if nothing tells it otherwise.
    located_remotely: bool = False
    preferred_categories: set[str] = field(default_factory=set)
    preferred_tags: set[str] = field(default_factory=set)
    disliked_categories: set[str] = field(default_factory=set)
    # Learned from behaviour, held apart from the stated sets above so the two can
    # never be confused. Signed affinities in -1..1; see explorer.learning.
    inferred_categories: dict[str, float] = field(default_factory=dict)
    inferred_tags: dict[str, float] = field(default_factory=dict)
    # How much behaviour those inferences rest on, 0-1. Scales their whole
    # contribution, so a new explorer is ranked almost entirely on stated
    # preferences and context.
    inference_confidence: float = 0.0
    budget: str | None = None
    is_raining: bool = False
    # The forecast for the day being asked about, when one is available. Set by
    # the caller from `integrations.weather`, and None whenever no provider is
    # configured or the day is past the provider's horizon - which is a real
    # answer and must stay distinguishable from "fine", not be defaulted away.
    weather: DailyWeather | None = None
    # Every day the question covers, when it covers more than one. Only the
    # single `weather` above scores a listing - a listing is either a good idea
    # on a given day or not - but a reply about "this weekend" has to be able to
    # describe both days, and answering with Saturday's forecast alone answers
    # half the question.
    weather_window: list[DailyWeather] = field(default_factory=list)
    # Claims the explorer said they need. Already applied as a hard filter in
    # `catalog.repository.require_suitability`, and repeated here because ranking
    # also runs over pools the database did not filter - the search index knows
    # nothing about these columns - and because the explanation on the card is
    # built from the same signals that produced the score.
    required_suitability: set[str] = field(default_factory=set)
    # Claims that would be nice. These never exclude anything; they sort.
    preferred_suitability: set[str] = field(default_factory=set)
    saved_experience_ids: set[str] = field(default_factory=set)
    # What this explorer has already reposted. Looked up once per request and
    # handed in, for the same reason `saved_experience_ids` is: a feed renders
    # dozens of cards and asking per card is an N+1 on the hottest query in
    # the product.
    reposted_experience_ids: set[str] = field(default_factory=set)

    @property
    def has_location(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def wants_indoors(self) -> bool:
        """Whether the day argues for being under a roof.

        `is_raining` is the older signal and stays authoritative when set: it is
        an observation of right now, and an observation beats a forecast for the
        same moment. The forecast answers for every other day, which is every day
        a plan is actually made for.
        """
        if self.is_raining:
            return True
        return self.weather is not None and self.weather.favours_indoors

    @property
    def wanted_suitability(self) -> set[str]:
        return self.required_suitability | self.preferred_suitability


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

    # Learned affinities, applied under everything stated above.
    #
    # Two separate dampeners keep an inference in its place. Each affinity is
    # already signed and bounded, and it is scaled again by INFERENCE_CEILING so
    # even a maximal one moves the score less than a stated preference does, then
    # by the explorer's overall evidence so a newcomer is not confidently profiled
    # on two taps. A category the explorer explicitly chose is skipped entirely -
    # it is already counted, and counting it twice would let behaviour amplify a
    # stated preference beyond what was stated.
    if ctx.inferred_categories or ctx.inferred_tags:
        strength = INFERENCE_CEILING * max(0.0, min(1.0, ctx.inference_confidence))

        if category_slug and category_slug not in ctx.preferred_categories:
            score += strength * ctx.inferred_categories.get(category_slug, 0.0)

        learned_tags = [
            ctx.inferred_tags[slug]
            for slug in tag_slugs
            if slug in ctx.inferred_tags and slug not in ctx.preferred_tags
        ]
        if learned_tags:
            # Mean, not sum: a listing with eight tags should not out-personalise
            # a better-matched listing with two purely by carrying more labels.
            score += strength * 0.6 * (sum(learned_tags) / len(learned_tags))

    if ctx.budget == "free" and experience.price_type == "free":
        score += 0.15
    elif ctx.budget in {"budget", "moderate"} and experience.price_type == "free":
        score += 0.05

    # Weather used to be scored here. It moved to `_fit_score`, because it is not
    # a preference: it is a fact about the day that changes what is a good idea
    # for everybody at once. Keeping it here also meant the forecast and the
    # stated indoor/outdoor constraint were weighed in two different places that
    # could disagree about the same listing.
    return max(0.0, min(1.0, score))


# How far a fully wrong weather call moves the fit signal, against a listing that
# is a perfect match on everything asked for. Asymmetric on purpose: being sent
# somewhere outdoors in a storm is a worse experience than being sent indoors on
# a nice day, so the penalty is larger than the reward.
WEATHER_REWARD = 0.2
WEATHER_PENALTY = 0.35


def _fit_score(experience: Experience, ctx: RankingContext) -> float:
    """Whether this suits the people asking and the day they are asking about.

    Two things the platform previously extracted and then dropped on the floor:
    what somebody said they need (a vegan main, a play area, step-free access)
    and what the weather will be doing. They share a signal because they answer
    the same question - is this a good idea for *these* people on *that* day -
    and because a listing can only be ordered once.

    Neutral at 0.5 when nothing was asked and no forecast exists, so an
    unconstrained feed in a city with no weather provider ranks exactly as it did
    before this signal existed.
    """
    score = 0.5

    wanted = ctx.wanted_suitability
    if wanted:
        assessment = suitability_vocab.assess(experience, wanted)
        # Share of what was asked for that this listing actually claims. Centred
        # so a listing claiming none of it is pushed below neutral rather than
        # merely failing to gain.
        score += 0.4 * (assessment.score(len(wanted)) - 0.5)

        # A required claim nobody has verified is a stronger negative than a
        # missing preference. The hard filter in the repository normally removes
        # these before ranking sees them; this matters for the pools it does not
        # filter, notably anything the search index returned.
        unverified_requirements = assessment.missing & ctx.required_suitability
        if unverified_requirements:
            score -= 0.3

    if ctx.wants_indoors:
        if experience.is_indoor:
            score += WEATHER_REWARD
        elif experience.is_indoor is False:
            score -= WEATHER_PENALTY
            # Unless the venue has done something about it. A heated, covered
            # terrace is not the same proposition as an open field, and treating
            # them alike is how a city's whole outdoor half disappears from the
            # results for one cold week.
            claimed = set(suitability_vocab.effective(experience))
            if claimed & _shelter_for(ctx):
                score += WEATHER_PENALTY * 0.6

    return max(0.0, min(1.0, score))


def _shelter_for(ctx: RankingContext) -> set[str]:
    """Which shelter claims actually answer today's problem.

    Shade does nothing about rain and a heater does nothing about heat, so the
    mitigation has to match the reason the day is difficult. Answering with the
    whole comfort vocabulary would have rated an air-conditioned rooftop as a
    fine idea in a thunderstorm.
    """
    weather = ctx.weather
    shelter: set[str] = set()

    if ctx.is_raining or (weather is not None and weather.is_wet):
        shelter |= {suitability_vocab.COVERED, suitability_vocab.INDOOR_SEATING}
    if weather is not None and weather.is_cold:
        shelter |= {
            suitability_vocab.HEATED,
            suitability_vocab.COVERED,
            suitability_vocab.INDOOR_SEATING,
        }
    if weather is not None and weather.is_hot:
        shelter |= {
            suitability_vocab.SHADED_SEATING,
            suitability_vocab.AIR_CONDITIONED,
            suitability_vocab.INDOOR_SEATING,
        }
    return shelter


def _quality_score(experience: Experience) -> float:
    quality = float(experience.quality_score or 0.5)
    if experience.rating_average is not None and experience.rating_count >= 3:
        normalised_rating = (float(experience.rating_average) - 1.0) / 4.0
        # Ratings are evidence, but thin evidence; blend rather than replace.
        confidence = min(1.0, experience.rating_count / 25)
        quality = quality * (1 - confidence * 0.6) + normalised_rating * (confidence * 0.6)
    return max(0.0, min(1.0, quality))


def _trust_score(experience: Experience) -> float:
    """How much the platform trusts whoever published this.

    Two halves, because verification and reputation answer different questions.
    The tier says a moderator confirmed who they are; the reputation says what
    they have done since (spec TRST-004). Blended rather than multiplied: a
    publisher who verified once and has been cancelling on people ever since
    should not outrank one who has quietly delivered forty events, and a
    publisher with a good record should not be held at the bottom because
    nobody has got round to verifying them.

    Weighted towards reputation, because it is the half backed by evidence.
    """
    publisher = experience.publisher
    if publisher is None:
        return 0.3

    # Spec BUSINESS-07 trust tiers 0-3, normalised.
    tier = min(1.0, publisher.trust_level / 3)
    if publisher.verification_status != "verified":
        tier *= 0.6

    reputation = float(getattr(publisher, "reputation_score", None) or 0.5)
    return max(0.0, min(1.0, tier * 0.4 + reputation * 0.6))


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

    # Ahead of proximity: somebody who said they need step-free access or a vegan
    # kitchen is asking a question that a five-minute walk does not answer, and
    # the reason on the card should name the thing they actually asked about.
    wanted = ctx.wanted_suitability
    if wanted:
        met = suitability_vocab.assess(experience, wanted).met
        if met:
            return "Has " + suitability_vocab.describe(sorted(met))

    # "From you" only when they are actually there. Asked about New York from
    # Addis, the coordinates are the destination's, and this cheerfully offered a
    # nine-minute walk to a bar five thousand kilometres away.
    if not ctx.located_remotely and distance_km is not None and distance_km <= WALKABLE_KM:
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

    if ctx.wants_indoors and experience.is_indoor:
        # Named for the reason it is a good idea, which is not always rain. Being
        # told "good rainy-day option" on a dry day at 38 degrees reads as a
        # system that has not understood the question.
        weather = ctx.weather
        if ctx.is_raining or (weather is not None and weather.is_wet):
            return "Good rainy-day option"
        if weather is not None and weather.is_cold:
            return "Warm indoors, and it will be cold"
        if weather is not None and weather.is_hot:
            return "Out of the heat"
        return "Indoors, which suits the forecast"

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

    if not ctx.located_remotely and distance_km is not None and distance_km <= 4:
        return f"{distance_km:.1f} km away"

    if signals.get("popularity", 0) >= 0.7:
        return "Popular in the city right now"

    return None


def score_experience(
    experience: Experience,
    ctx: RankingContext,
    *,
    relevance: float = 0.5,
    weights: dict[str, float] | None = None,
) -> ScoredExperience:
    """Score one candidate. ``relevance`` is the retrieval score, already 0-1."""
    weights = weights or BROWSE_WEIGHTS
    proximity, distance_km = _proximity_score(experience, ctx)
    signals = {
        "relevance": max(0.0, min(1.0, relevance)),
        "proximity": proximity,
        "timing": _timing_score(experience, ctx),
        "fit": _fit_score(experience, ctx),
        "personalization": _personalization_score(experience, ctx),
        "quality": _quality_score(experience),
        "popularity": float(experience.popularity_score or 0.0),
        "trust": _trust_score(experience),
    }
    total = sum(weights[name] * value for name, value in signals.items())
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
    weights: dict[str, float] | None = None,
) -> list[ScoredExperience]:
    """Score, optionally diversify, and truncate.

    ``weights`` selects the profile: :data:`SEARCH_WEIGHTS` when the explorer typed
    a query, :data:`BROWSE_WEIGHTS` (the default) when they did not.
    """
    relevance_by_id = relevance_by_id or {}
    scored = [
        score_experience(
            exp, ctx, relevance=relevance_by_id.get(str(exp.id), 0.5), weights=weights
        )
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
