"""Model -> schema conversion for the catalog domain.

Kept out of the route handlers so that the discovery, search and concierge surfaces
all emit byte-identical experience cards. A card rendered by the feed and the same
card returned inside an AI answer must not drift.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.domains.catalog import suitability
from app.domains.catalog.models import EventInstance, Experience
from app.domains.catalog.schemas import (
    CategoryOut,
    EventInstanceOut,
    EventOut,
    ExperienceDetail,
    ExperienceSummary,
    MediaOut,
    NeighborhoodOut,
    PriceOut,
    PublisherSummary,
    TagOut,
    VenueSummary,
)


def _price(experience: Experience) -> PriceOut:
    return PriceOut(
        type=experience.price_type,
        amount=float(experience.price_amount) if experience.price_amount is not None else None,
        max_amount=float(experience.price_max) if experience.price_max is not None else None,
        currency=experience.currency,
    )


def _venue(experience: Experience) -> VenueSummary | None:
    venue = experience.venue
    if venue is None:
        return None
    neighborhood = None
    if venue.neighborhood is not None:
        neighborhood = NeighborhoodOut.model_validate(venue.neighborhood)
    return VenueSummary(
        id=venue.id,
        name=venue.name,
        slug=venue.slug,
        address=venue.address,
        latitude=venue.latitude,
        longitude=venue.longitude,
        neighborhood=neighborhood,
        accessibility=venue.accessibility or {},
        facilities=suitability.normalise(venue.facilities),
        opening_hours=venue.opening_hours or {},
    )


def _media(experience: Experience) -> list[MediaOut]:
    ordered = sorted(experience.media or [], key=lambda m: m.sort_order)
    return [MediaOut.model_validate(item) for item in ordered]


def _publisher(experience: Experience) -> PublisherSummary | None:
    publisher = experience.publisher
    if publisher is None:
        return None
    from app.domains.publisher import business as business_vocab

    business_type = getattr(publisher, "business_type", None)
    return PublisherSummary(
        id=publisher.id,
        name=publisher.name,
        slug=publisher.slug,
        logo_url=publisher.logo_url,
        verification_status=publisher.verification_status,
        trust_level=publisher.trust_level,
        type=publisher.type,
        business_type=business_type,
        business_type_label=(
            business_vocab.label(business_type) if business_type else None
        ),
    )


def next_event_of(experience: Experience, *, now: datetime | None = None) -> EventInstance | None:
    """Soonest non-cancelled future occurrence, if any.

    Requires ``experience.events`` to be loaded; callers that do not need event
    timing should avoid loading it rather than paying for it silently.
    """
    reference = now or datetime.now(UTC)
    upcoming = [
        event
        for event in (experience.events or [])
        if event.status != "cancelled" and event.start_time >= reference
    ]
    if not upcoming:
        return None
    return min(upcoming, key=lambda event: event.start_time)


def to_summary(
    experience: Experience,
    *,
    reason: str | None = None,
    distance_km: float | None = None,
    is_saved: bool = False,
    include_next_event: bool = True,
    wanted: list[str] | None = None,
    is_reposted: bool = False,
) -> ExperienceSummary:
    """Card payload.

    `wanted` is what the explorer asked to be true of a listing. Passing it fills
    `unverified` with the claims this one has not made, so the card can say so in
    the same breath as showing it. Omitting it leaves that empty, which is right
    for a feed nobody constrained.
    """
    next_event = next_event_of(experience) if include_next_event else None
    assessment = suitability.assess(experience, wanted)
    return ExperienceSummary(
        id=experience.id,
        title=experience.title,
        slug=experience.slug,
        summary=experience.summary,
        type=experience.type,
        category=CategoryOut.model_validate(experience.category) if experience.category else None,
        tags=[TagOut.model_validate(tag) for tag in (experience.tags or [])],
        venue=_venue(experience),
        city_slug=experience.city.slug if experience.city else None,
        price=_price(experience),
        media=_media(experience),
        publisher=_publisher(experience),
        rating_average=float(experience.rating_average)
        if experience.rating_average is not None
        else None,
        rating_count=experience.rating_count,
        next_event=EventInstanceOut.model_validate(next_event) if next_event else None,
        duration_minutes=experience.duration_minutes,
        is_indoor=experience.is_indoor,
        suitability=suitability.effective(experience),
        unverified=sorted(assessment.missing),
        reason=reason,
        distance_km=distance_km,
        is_saved=is_saved,
        repost_count=experience.repost_count or 0,
        is_reposted=is_reposted,
    )


def to_detail(
    experience: Experience,
    *,
    upcoming_events: list[EventInstance] | None = None,
    is_saved: bool = False,
    distance_km: float | None = None,
    is_reposted: bool = False,
) -> ExperienceDetail:
    summary = to_summary(
        experience,
        is_saved=is_saved,
        distance_km=distance_km,
        include_next_event=False,
        is_reposted=is_reposted,
    )
    events = upcoming_events or []
    # The caller has already ordered and filtered the occurrences, so the first is
    # the next one; deriving it here avoids a second pass over experience.events.
    fields = summary.model_dump(by_alias=False)
    fields["next_event"] = EventInstanceOut.model_validate(events[0]) if events else None

    return ExperienceDetail(
        **fields,
        description=experience.description,
        accessibility=experience.accessibility or {},
        attributes=experience.attributes or {},
        upcoming_events=[EventInstanceOut.model_validate(event) for event in events],
        published_at=experience.published_at,
        updated_at=experience.updated_at,
    )


def to_event(event: EventInstance) -> EventOut:
    experience = event.experience
    return EventOut(
        id=event.id,
        experience_id=experience.id,
        title=experience.title,
        summary=experience.summary,
        start_time=event.start_time,
        end_time=event.end_time,
        status=event.status,
        capacity=event.capacity,
        remaining=event.remaining,
        venue=_venue(experience),
        category=CategoryOut.model_validate(experience.category) if experience.category else None,
        price=_price(experience),
        media=_media(experience),
        publisher=_publisher(experience),
    )
