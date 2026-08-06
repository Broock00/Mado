"""External feed ingestion.

Spec BUSINESS-03's Hybrid Intelligence Model describes three inflows of content.
Native publishing was built first and community contribution partly so; this is
the second: content from sources Mado trusts but does not control - a venue's own
events feed, a cultural institute's programme, a ticketing partner's listings.

The hard problem is not fetching. It is that the same concert arrives twice - once
from the venue, once from the ticketing partner, once more when a local posts
about it - and an explorer must see one listing, not three.

**Reconciliation, not deduplication.** Deduplication implies discarding a copy.
What actually happens is that several partial descriptions of one real-world thing
are merged into a single record whose fields come from whichever source is most
trustworthy for that field. A venue knows its own address; a ticketing partner
knows the price; a local knows whether it is any good.

Matching runs in three passes, cheapest and most certain first:

1. **Same source, same external id.** Definitive - it is the same record updated.
2. **Same venue, overlapping time, similar title.** Two feeds describing one
   concert. Time is the strong signal: two events at one venue starting within
   the hour are almost never distinct.
3. **Semantic similarity.** Catches "Azmari Night" and "Evening of Azmari Music"
   where no string comparison would. Only consulted when a venue and time already
   agree, because semantic closeness alone would merge a Tuesday jazz night into
   its Wednesday sibling.

**Provenance is recorded, never hidden.** Spec BUSINESS-03 requires an explorer to
be able to tell where something came from, and a moderator to be able to
distrust a whole source at once if it turns out to be bad.

**External content is not privileged.** It goes through the same screening as a
stranger's post. A trusted source is trusted more, not trusted blindly - a feed
is exactly the mechanism by which one compromised partner could inject a thousand
scams.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging import get_logger
from app.domains.catalog.models import (
    EventInstance,
    Experience,
    Venue,
)

logger = get_logger("mado.ingestion")

# Two events at the same venue starting within this window are treated as
# candidates for being the same event. An hour absorbs the disagreement between
# a "doors 20:00" feed and a "show 20:30" one without merging a matinee into an
# evening performance.
SAME_EVENT_WINDOW = timedelta(hours=1)

# Title overlap above which two listings at the same venue and time are the same
# thing. Applied only after venue and time already agree, which is what makes a
# threshold this permissive safe.
TITLE_SIMILARITY = 0.6

# How far ahead a feed may schedule. Anything beyond this is almost always a
# parsing error - a date field read as a year, or a recurring rule expanded
# without bound - and importing it fills the catalogue with phantoms.
MAX_FUTURE = timedelta(days=400)


@dataclass(slots=True)
class FeedItem:
    """One listing as an external source describes it.

    Deliberately not a catalogue model. A feed's shape is the feed's business;
    mapping it into Mado's taxonomy is this module's job, and keeping the two
    apart means a source changing its format touches an adapter and nothing else.
    """

    external_id: str
    title: str
    description: str
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    venue_name: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    price_amount: float | None = None
    is_free: bool = False
    category_slug: str | None = None
    image_url: str | None = None
    source_url: str | None = None


@dataclass(slots=True)
class IngestionReport:
    """What a run did. Every number here is something an operator asks about."""

    source: str
    created: int = 0
    updated: int = 0
    merged: int = 0
    skipped: int = 0
    withheld: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created + self.updated + self.merged + self.skipped


def _normalise(text: str) -> set[str]:
    """Tokens for comparing titles, minus the words every listing shares."""
    noise = {
        "the", "a", "an", "at", "in", "on", "of", "and", "with", "for",
        "night", "event", "live", "show", "presents", "featuring",
    }
    return {
        word.strip(".,!?:;'\"()").casefold()
        for word in text.split()
        if len(word) > 2
    } - noise


def title_similarity(left: str, right: str) -> float:
    """Word overlap, as a fraction of the *shorter* title.

    Word-based rather than character-based because feeds differ by whole words:
    "Azmari Night at Fendika" against "Fendika: Azmari Music Evening" is a word-set
    problem, and an edit distance would score it as barely related.

    The overlap coefficient rather than Jaccard, which was the first attempt and
    was wrong here. Jaccard divides by the union, so a verbose title is penalised
    for the words it adds: the pair above scored 0.5 and fell below the threshold
    despite naming the same concert. Dividing by the shorter set asks the question
    that actually matters - "is one title contained in the other" - which is
    exactly how the same event differs between a venue's listing and an
    aggregator's.

    Safe here only because this is consulted *after* venue and start time already
    agree. On its own it would merge a Tuesday jazz night into its Wednesday
    sibling.
    """
    a, b = _normalise(left), _normalise(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def is_plausible(item: FeedItem, *, now: datetime | None = None) -> tuple[bool, str | None]:
    """Reject items that cannot be describing a real thing.

    Runs before anything is written. A feed that has broken produces confident,
    well-formed nonsense, and the cheapest place to stop it is at the door.
    """
    now = now or datetime.now(UTC)

    if not item.title.strip():
        return False, "no title"
    if len(item.title) > 300:
        return False, "title implausibly long"
    if not item.external_id.strip():
        return False, "no external id"

    if item.starts_at is not None:
        starts = item.starts_at if item.starts_at.tzinfo else item.starts_at.replace(tzinfo=UTC)
        if starts > now + MAX_FUTURE:
            return False, f"starts {(starts - now).days} days from now"
        if item.ends_at is not None:
            ends = item.ends_at if item.ends_at.tzinfo else item.ends_at.replace(tzinfo=UTC)
            if ends < starts:
                return False, "ends before it starts"

    return True, None


async def find_existing(
    session: AsyncSession,
    item: FeedItem,
    *,
    source: str,
    venue: Venue | None,
) -> tuple[Experience | None, str]:
    """Locate the record this item describes, if we already hold one.

    Returns the match and how it was found, so a report can distinguish "this
    source sent us an update" from "two sources described the same concert" -
    which are different situations with different follow-ups.
    """
    # 1. The same source telling us about its own record again.
    result = await session.execute(
        select(Experience)
        .where(
            Experience.attributes["source"].astext == source,
            Experience.attributes["externalId"].astext == item.external_id,
            Experience.deleted_at.is_(None),
        )
        .options(selectinload(Experience.events))
        .limit(1)
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing, "external_id"

    # 2. Another description of the same event: same venue, overlapping time,
    #    similar title. Without a venue there is no reliable second pass - titles
    #    alone merge unrelated things across a city.
    if venue is None or item.starts_at is None:
        return None, "none"

    starts = item.starts_at if item.starts_at.tzinfo else item.starts_at.replace(tzinfo=UTC)
    window = (starts - SAME_EVENT_WINDOW, starts + SAME_EVENT_WINDOW)

    result = await session.execute(
        select(Experience)
        .join(EventInstance, EventInstance.experience_id == Experience.id)
        .where(
            Experience.venue_id == venue.id,
            Experience.deleted_at.is_(None),
            EventInstance.start_time.between(*window),
        )
        .options(selectinload(Experience.events))
        .distinct()
    )

    for candidate in result.scalars().unique():
        if title_similarity(candidate.title, item.title) >= TITLE_SIMILARITY:
            return candidate, "venue_time_title"

    return None, "none"


def merge_fields(existing: Experience, item: FeedItem, *, source_trust: float) -> list[str]:
    """Fill gaps in an existing record from a feed, without overwriting good data.

    The rule is that a feed may *add* what is missing and may only *replace* what
    it is more authoritative about. A venue's own feed knows its address better
    than a stranger's post does; neither knows better than a moderator who has
    already corrected it.

    Returns the field names actually changed, for the audit trail.
    """
    changed: list[str] = []

    # A human ruling freezes the record against automated edits entirely.
    if existing.moderation_locked:
        return changed

    if not existing.summary and item.description:
        existing.summary = item.description[:280]
        changed.append("summary")

    # Descriptions are only replaced when the incoming one is substantially
    # richer and the source is trusted. A feed's boilerplate should not overwrite
    # something a local took the trouble to write.
    if (
        item.description
        and len(item.description) > len(existing.description or "") * 1.5
        and source_trust >= 0.8
    ):
        existing.description = item.description
        changed.append("description")

    if item.is_free and existing.price_type != "free":
        existing.price_type = "free"
        existing.price_amount = None
        changed.append("price")
    elif item.price_amount is not None and existing.price_amount is None:
        existing.price_type = "fixed"
        existing.price_amount = item.price_amount
        changed.append("price")

    # Provenance accumulates rather than being replaced: a listing confirmed by
    # two independent sources is a stronger record than one confirmed by either,
    # and an explorer should be able to see both.
    attributes = dict(existing.attributes or {})
    contributors = set(attributes.get("contributingSources") or [])
    contributors.add(attributes.get("source", "")) if attributes.get("source") else None
    attributes["contributingSources"] = sorted(contributors)
    attributes["lastSeenAt"] = datetime.now(UTC).isoformat()
    existing.attributes = attributes

    return changed


def merge_occurrences(existing: Experience, item: FeedItem) -> bool:
    """Add a scheduled time the record does not already have.

    Two feeds describing the same event must not produce two occurrences of it,
    which is what makes "tonight" show the same concert twice.
    """
    if item.starts_at is None:
        return False

    starts = item.starts_at if item.starts_at.tzinfo else item.starts_at.replace(tzinfo=UTC)
    for occurrence in existing.events or []:
        known = occurrence.start_time
        if known.tzinfo is None:
            known = known.replace(tzinfo=UTC)
        if abs(known - starts) <= SAME_EVENT_WINDOW:
            return False

    existing.events.append(
        EventInstance(
            experience_id=existing.id,
            start_time=starts,
            end_time=item.ends_at,
            status="scheduled",
        )
    )
    return True


def provenance(item: FeedItem, *, source: str, source_trust: float) -> dict:
    """The attributes recorded on anything imported.

    Spec BUSINESS-03 requires an explorer to be able to see where a listing came
    from, and requires a moderator to be able to act on a whole source at once -
    both of which need this written at import time rather than inferred later.
    """
    return {
        "source": source,
        "externalId": item.external_id,
        "sourceUrl": item.source_url,
        "sourceTrust": source_trust,
        "importedAt": datetime.now(UTC).isoformat(),
        "lastSeenAt": datetime.now(UTC).isoformat(),
        # Distinguishes imported content from something a person wrote here,
        # which the interface needs in order to attribute it honestly.
        "origin": "external_feed",
    }
