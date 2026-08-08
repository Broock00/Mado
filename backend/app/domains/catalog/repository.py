"""Catalog queries.

Centralising the loader options matters more than it looks: an experience card
needs venue, neighbourhood, category, tags, media and publisher, and letting each
route assemble its own query is how N+1 problems get in (spec 80.02 s18).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domains.catalog.models import (
    MODERATION_APPROVED,
    MODERATION_PENDING,
    STATUS_PUBLISHED,
    Category,
    City,
    EventInstance,
    Experience,
    Tag,
    Venue,
)

# Pending content stays visible while it waits for a human: spec BUSINESS-07 treats
# automated suspicion as a reason to look, not a reason to hide. Flagged and
# rejected are withheld.
DISCOVERABLE_MODERATION_STATUSES = (MODERATION_APPROVED, MODERATION_PENDING)


def with_card_relations(stmt: Select) -> Select:
    """Eager-load everything `to_summary` touches.

    Public because any query feeding a card needs exactly this set, and a caller
    that assembles its own inevitably misses one - `venue.neighborhood` is the
    usual casualty. The miss is not a slow query under asyncio, it is a
    MissingGreenlet at request time with no application frame in the traceback.
    """
    return stmt.options(
        selectinload(Experience.venue).selectinload(Venue.neighborhood),
        selectinload(Experience.category),
        selectinload(Experience.tags),
        selectinload(Experience.media),
        selectinload(Experience.publisher),
        selectinload(Experience.city),
        selectinload(Experience.events),
    )


def published_experiences() -> Select:
    """Base query for anything an explorer is allowed to see.

    Two independent gates, and both must pass: the author published it, and
    moderation has not withheld it. Every discovery surface builds on this
    query, so an item flagged or rejected disappears from the feed, search,
    nearby and concierge answers at once - there is no path that forgets to
    check.
    """
    return with_card_relations(
        select(Experience).where(
            Experience.status == STATUS_PUBLISHED,
            Experience.deleted_at.is_(None),
            Experience.moderation_status.in_(DISCOVERABLE_MODERATION_STATUSES),
        )
    )


async def get_city_by_slug(session: AsyncSession, slug: str) -> City | None:
    result = await session.execute(select(City).where(City.slug == slug))
    return result.scalar_one_or_none()


async def list_cities(session: AsyncSession, *, live_only: bool = False) -> list[City]:
    stmt = select(City).order_by(City.name)
    if live_only:
        stmt = stmt.where(City.is_live.is_(True))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_categories(session: AsyncSession) -> list[Category]:
    result = await session.execute(select(Category).order_by(Category.sort_order, Category.name))
    return list(result.scalars().all())


async def get_experience(session: AsyncSession, experience_id: uuid.UUID) -> Experience | None:
    result = await session.execute(published_experiences().where(Experience.id == experience_id))
    return result.scalar_one_or_none()


async def get_experience_by_slug(session: AsyncSession, slug: str) -> Experience | None:
    result = await session.execute(published_experiences().where(Experience.slug == slug))
    return result.scalar_one_or_none()


async def get_experiences_by_ids(session: AsyncSession, ids: list[uuid.UUID]) -> list[Experience]:
    """Fetch a set of experiences, preserving the caller's ordering.

    Search returns candidates already ordered by the ranking layer; a plain IN query
    would silently reorder them by physical row order.
    """
    if not ids:
        return []
    result = await session.execute(published_experiences().where(Experience.id.in_(ids)))
    found = {exp.id: exp for exp in result.scalars().all()}
    return [found[i] for i in ids if i in found]


async def query_experiences(
    session: AsyncSession,
    *,
    city_slug: str | None = None,
    category_slugs: list[str] | None = None,
    tag_slugs: list[str] | None = None,
    experience_type: str | None = None,
    free_only: bool = False,
    indoor: bool | None = None,
    starts_between: tuple[datetime, datetime] | None = None,
    limit: int = 60,
) -> list[Experience]:
    """Filtered catalog read used by the feed modules and as the search fallback."""
    stmt = published_experiences()

    if city_slug:
        stmt = stmt.join(City, Experience.city_id == City.id).where(City.slug == city_slug)
    if category_slugs:
        stmt = stmt.join(Category, Experience.category_id == Category.id).where(
            Category.slug.in_(category_slugs)
        )
    if tag_slugs:
        stmt = stmt.where(Experience.tags.any(Tag.slug.in_(tag_slugs)))
    if experience_type:
        stmt = stmt.where(Experience.type == experience_type)
    if free_only:
        stmt = stmt.where(Experience.price_type == "free")
    if indoor is not None:
        stmt = stmt.where(Experience.is_indoor.is_(indoor))

    if starts_between is not None:
        start, end = starts_between
        # Restrict to experiences with at least one live occurrence in the window.
        stmt = stmt.where(
            Experience.events.any(
                and_(
                    EventInstance.start_time >= start,
                    EventInstance.start_time <= end,
                    EventInstance.status != "cancelled",
                )
            )
        )

    stmt = stmt.order_by(
        Experience.popularity_score.desc(), Experience.published_at.desc().nullslast()
    ).limit(limit)

    result = await session.execute(stmt)
    return list(result.scalars().unique().all())


async def nearby_experiences(
    session: AsyncSession,
    *,
    latitude: float,
    longitude: float,
    radius_km: float = 5.0,
    limit: int = 60,
) -> list[Experience]:
    """Radius search using the PostGIS geography column.

    ``ST_DWithin`` on a geography type takes metres and uses the GiST index, so this
    stays an index scan rather than a full-table distance computation.
    """
    point = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326).cast(Venue.geo.type)
    stmt = (
        published_experiences()
        .join(Venue, Experience.venue_id == Venue.id)
        .where(func.ST_DWithin(Venue.geo, point, radius_km * 1000))
        .order_by(func.ST_Distance(Venue.geo, point))
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().unique().all())


async def upcoming_events(
    session: AsyncSession,
    *,
    experience_id: uuid.UUID | None = None,
    city_slug: str | None = None,
    starts_after: datetime | None = None,
    starts_before: datetime | None = None,
    limit: int = 50,
) -> list[EventInstance]:
    stmt = (
        select(EventInstance)
        .join(Experience, EventInstance.experience_id == Experience.id)
        .where(
            Experience.status == STATUS_PUBLISHED,
            Experience.deleted_at.is_(None),
            Experience.moderation_status.in_(DISCOVERABLE_MODERATION_STATUSES),
            EventInstance.status != "cancelled",
        )
        .options(
            selectinload(EventInstance.experience)
            .selectinload(Experience.venue)
            .selectinload(Venue.neighborhood),
            selectinload(EventInstance.experience).selectinload(Experience.category),
            selectinload(EventInstance.experience).selectinload(Experience.media),
            selectinload(EventInstance.experience).selectinload(Experience.publisher),
            selectinload(EventInstance.experience).selectinload(Experience.tags),
            selectinload(EventInstance.experience).selectinload(Experience.city),
        )
        .order_by(EventInstance.start_time)
        .limit(limit)
    )
    if experience_id is not None:
        stmt = stmt.where(EventInstance.experience_id == experience_id)
    if city_slug:
        stmt = stmt.join(City, Experience.city_id == City.id).where(City.slug == city_slug)
    if starts_after is not None:
        stmt = stmt.where(EventInstance.start_time >= starts_after)
    if starts_before is not None:
        stmt = stmt.where(EventInstance.start_time <= starts_before)

    result = await session.execute(stmt)
    return list(result.scalars().unique().all())


async def get_event(session: AsyncSession, event_id: uuid.UUID) -> EventInstance | None:
    result = await session.execute(
        select(EventInstance)
        .where(EventInstance.id == event_id)
        .options(
            selectinload(EventInstance.experience)
            .selectinload(Experience.venue)
            .selectinload(Venue.neighborhood),
            selectinload(EventInstance.experience).selectinload(Experience.category),
            selectinload(EventInstance.experience).selectinload(Experience.media),
            selectinload(EventInstance.experience).selectinload(Experience.publisher),
            selectinload(EventInstance.experience).selectinload(Experience.tags),
            selectinload(EventInstance.experience).selectinload(Experience.city),
        )
    )
    return result.scalar_one_or_none()


async def similar_experiences(
    session: AsyncSession, experience: Experience, *, limit: int = 12
) -> list[Experience]:
    """Related items by shared category or tags, excluding the source.

    Uses the relational graph rather than embeddings so this works before the
    embedding backfill has run; the vector path replaces it once populated.
    """
    tag_ids = [tag.id for tag in (experience.tags or [])]
    conditions = []
    if experience.category_id is not None:
        conditions.append(Experience.category_id == experience.category_id)
    if tag_ids:
        conditions.append(Experience.tags.any(Tag.id.in_(tag_ids)))
    if not conditions:
        conditions.append(Experience.city_id == experience.city_id)

    stmt = (
        published_experiences()
        .where(or_(*conditions))
        .where(Experience.id != experience.id)
        .order_by(Experience.quality_score.desc(), Experience.popularity_score.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().unique().all())
