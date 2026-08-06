"""Feed sources and the ingestion run.

A source is a place Mado has decided to trust, with a trust level that governs
how much authority its data carries when it disagrees with what we already hold.
Trust is per-source and explicit, because "trusted feed" is not a binary: a
venue's own calendar is authoritative about that venue and says nothing reliable
about anywhere else.

Adapters live here too. Each turns one external format into
:class:`~app.domains.catalog.ingestion.FeedItem`, and nothing downstream knows
what shape the source was in.

The run itself is deliberately conservative:

* Nothing is imported that fails :func:`is_plausible`. A broken feed emits
  confident nonsense, and the cheapest place to stop it is before it is written.
* Everything imported is screened exactly as a stranger's post is. A trusted
  source is trusted *more*, not trusted blindly - a feed is precisely the
  mechanism by which one compromised partner injects a thousand scams at once.
* An import never deletes. An item disappearing from a feed marks the record
  stale for a human to look at; it does not remove content, per spec BUSINESS-07.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.domains.catalog.ingestion import (
    FeedItem,
    IngestionReport,
    find_existing,
    is_plausible,
    merge_fields,
    merge_occurrences,
    provenance,
)
from app.domains.catalog.models import (
    STATUS_PUBLISHED,
    TYPE_EVENT,
    TYPE_PLACE,
    Category,
    City,
    EventInstance,
    Experience,
    Venue,
)
from app.domains.publisher.models import Publisher
from app.domains.trust.service import TrustService
from app.integrations.geocoding import get_geocoder

logger = get_logger("mado.feeds")


@dataclass(frozen=True, slots=True)
class FeedSource:
    """A source Mado has chosen to trust, and how much.

    ``trust`` governs whether this source may overwrite existing data, not
    whether it may be screened. Screening applies regardless.
    """

    slug: str
    name: str
    # 0-1. A venue publishing its own calendar is authoritative about itself
    # (0.9); an aggregator repeating what it scraped is not (0.5).
    trust: float
    # The publisher imported content is attributed to. Attribution is never
    # anonymous - an explorer can see who stands behind a listing.
    publisher_slug: str
    city_slug: str

    @property
    def may_overwrite(self) -> bool:
        return self.trust >= 0.8


async def ingest(
    session: AsyncSession,
    source: FeedSource,
    items: list[FeedItem],
    *,
    now: datetime | None = None,
) -> IngestionReport:
    """Import a batch of items from one source.

    Takes already-fetched items rather than a URL: fetching is the adapter's
    problem, and separating them means this - the part with all the judgement in
    it - is testable without a network.
    """
    now = now or datetime.now(UTC)
    report = IngestionReport(source=source.slug)

    city = (
        await session.execute(select(City).where(City.slug == source.city_slug))
    ).scalar_one_or_none()
    publisher = (
        await session.execute(
            select(Publisher).where(Publisher.slug == source.publisher_slug)
        )
    ).scalar_one_or_none()

    if city is None or publisher is None:
        report.problems.append(
            f"source {source.slug} names an unknown "
            f"{'city' if city is None else 'publisher'}"
        )
        return report

    trust = TrustService(session)

    for item in items:
        ok, reason = is_plausible(item, now=now)
        if not ok:
            report.skipped += 1
            report.problems.append(f"{item.external_id}: {reason}")
            continue

        venue = await _resolve_venue(session, item, city=city, publisher=publisher)
        existing, how = await find_existing(session, item, source=source.slug, venue=venue)

        if existing is not None:
            changed = merge_fields(existing, item, source_trust=source.trust)
            added_occurrence = merge_occurrences(existing, item)

            if how == "external_id":
                report.updated += 1
            else:
                report.merged += 1
                logger.info(
                    "feed_item_merged",
                    source=source.slug,
                    external_id=item.external_id,
                    into=str(existing.id),
                    matched_by=how,
                )

            if changed or added_occurrence:
                # Re-screen only when something actually changed. Screening costs
                # a model call, and re-running it on an unchanged record every
                # night would be the largest line on the bill for no benefit.
                await trust.screen_on_publish(existing)
                if not existing.is_discoverable:
                    report.withheld += 1
            continue

        experience = await _create(
            session, item, source=source, city=city, publisher=publisher, venue=venue
        )
        await trust.screen_on_publish(experience)
        if not experience.is_discoverable:
            report.withheld += 1
        report.created += 1

    await session.flush()
    logger.info(
        "feed_ingested",
        source=source.slug,
        created=report.created,
        updated=report.updated,
        merged=report.merged,
        skipped=report.skipped,
        withheld=report.withheld,
    )
    return report


async def _resolve_venue(
    session: AsyncSession,
    item: FeedItem,
    *,
    city: City,
    publisher: Publisher,
) -> Venue | None:
    """Find or create the venue an item happens at.

    Matched by name within the city first. Feeds are inconsistent about venue
    naming but consistent about which venue they mean, and creating a second
    "Fendika" every time a feed writes it differently would fragment the
    catalogue exactly where it most needs to be whole.
    """
    if not item.venue_name:
        return None

    result = await session.execute(
        select(Venue).where(Venue.city_id == city.id, Venue.name.ilike(item.venue_name.strip()))
    )
    venue = result.scalars().first()
    if venue is not None:
        return venue

    latitude, longitude = item.latitude, item.longitude
    if latitude is None or longitude is None:
        # Geocode only what the feed could not place itself. A feed that supplies
        # coordinates is more reliable about its own venue than a general
        # geocoder is, and every lookup is a request to somebody else's service.
        located = await get_geocoder().geocode(
            item.address or item.venue_name, city=city.name, country=city.country
        )
        if located is None or not located.is_usable:
            # A venue without coordinates cannot be ranked by proximity or drawn
            # on a map, and a wrong pin is worse than none - so the experience is
            # imported without a venue rather than with a guessed one.
            logger.info("feed_venue_unlocatable", venue=item.venue_name, source=item.external_id)
            return None
        latitude, longitude = located.latitude, located.longitude

    venue = Venue(
        publisher_id=publisher.id,
        city_id=city.id,
        name=item.venue_name.strip(),
        slug=_slugify(item.venue_name),
        address=item.address or item.venue_name,
        latitude=latitude,
        longitude=longitude,
    )
    session.add(venue)
    await session.flush()
    return venue


async def _create(
    session: AsyncSession,
    item: FeedItem,
    *,
    source: FeedSource,
    city: City,
    publisher: Publisher,
    venue: Venue | None,
) -> Experience:
    category = None
    if item.category_slug:
        category = (
            await session.execute(select(Category).where(Category.slug == item.category_slug))
        ).scalar_one_or_none()

    experience = Experience(
        title=item.title.strip()[:300],
        slug=f"{_slugify(item.title)}-{item.external_id[:8]}",
        summary=(item.description or "")[:280] or None,
        description=item.description or item.title,
        type=TYPE_EVENT if item.starts_at else TYPE_PLACE,
        status=STATUS_PUBLISHED,
        published_at=datetime.now(UTC),
        city_id=city.id,
        venue_id=venue.id if venue else None,
        publisher_id=publisher.id,
        category_id=category.id if category else None,
        price_type="free" if item.is_free else ("fixed" if item.price_amount else "free"),
        price_amount=item.price_amount,
        currency=city.currency,
        attributes=provenance(item, source=source.slug, source_trust=source.trust),
        events=[],
        media=[],
        tags=[],
    )
    session.add(experience)
    await session.flush()

    if item.starts_at:
        starts = item.starts_at if item.starts_at.tzinfo else item.starts_at.replace(tzinfo=UTC)
        experience.events.append(
            EventInstance(
                experience_id=experience.id,
                start_time=starts,
                end_time=item.ends_at,
                status="scheduled",
            )
        )

    return experience


def _slugify(text: str) -> str:
    cleaned = "".join(char if char.isalnum() or char.isspace() else " " for char in text.lower())
    return "-".join(cleaned.split())[:80] or "untitled"


# --- adapters ----------------------------------------------------------------


def from_ics(text: str, *, external_id_prefix: str = "") -> list[FeedItem]:
    """Parse an iCalendar feed.

    ICS is the format a venue is most likely to already publish, because any
    calendar produces it. Only the fields Mado uses are read; the specification
    is enormous and most of it describes scheduling behaviour that a listing does
    not need.
    """
    items: list[FeedItem] = []
    current: dict[str, str] = {}
    in_event = False

    # Unfold continuation lines first: ICS wraps long values onto following lines
    # beginning with a space, and parsing line-by-line without rejoining them
    # truncates every description at 75 characters.
    unfolded: list[str] = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and unfolded:
            unfolded[-1] += raw[1:]
        else:
            unfolded.append(raw)

    for line in unfolded:
        if line.startswith("BEGIN:VEVENT"):
            in_event, current = True, {}
            continue
        if line.startswith("END:VEVENT"):
            in_event = False
            item = _ics_item(current, external_id_prefix)
            if item is not None:
                items.append(item)
            continue
        if not in_event or ":" not in line:
            continue

        key, _, value = line.partition(":")
        # Strip parameters: "DTSTART;TZID=Africa/Addis_Ababa" is still DTSTART.
        current[key.split(";")[0].upper()] = value.strip()

    return items


def _ics_item(fields: dict[str, str], prefix: str) -> FeedItem | None:
    summary = fields.get("SUMMARY")
    uid = fields.get("UID")
    if not summary or not uid:
        return None

    return FeedItem(
        external_id=f"{prefix}{uid}",
        title=summary,
        description=fields.get("DESCRIPTION", summary).replace("\\n", "\n").replace("\\,", ","),
        starts_at=_ics_datetime(fields.get("DTSTART")),
        ends_at=_ics_datetime(fields.get("DTEND")),
        venue_name=fields.get("LOCATION"),
        address=fields.get("LOCATION"),
        source_url=fields.get("URL"),
    )


def _ics_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = value.strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None
