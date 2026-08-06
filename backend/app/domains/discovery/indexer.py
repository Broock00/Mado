"""Search index projection.

Spec 21 s13 / 54.09 s12 make indexing event-driven: a catalog change publishes a
domain event and a worker projects it. The monolith runs that projection inline
for now, but the projection itself is written as a pure function of an Experience
so moving it behind a queue later changes the caller, not the logic.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.domains.catalog import repository as catalog_repo
from app.domains.catalog.models import Experience
from app.domains.catalog.serializers import next_event_of
from app.integrations.search import get_search_client

logger = get_logger("mado.indexer")


def to_document(experience: Experience) -> dict:
    """Flatten an Experience into a search document.

    Denormalised on purpose: the index answers "which candidates match", and a
    flat document keeps filters cheap. Authority stays in Postgres - nothing is
    served to an explorer straight from here without a database read.
    """
    venue = experience.venue
    neighborhood = venue.neighborhood if venue else None
    publisher = experience.publisher
    next_event = next_event_of(experience)

    return {
        "id": str(experience.id),
        "title": experience.title,
        "slug": experience.slug,
        "summary": experience.summary or "",
        "description": experience.description,
        "type": experience.type,
        "city_slug": experience.city.slug if experience.city else None,
        "category_slug": experience.category.slug if experience.category else None,
        "category_name": experience.category.name if experience.category else None,
        "tags": [tag.slug for tag in (experience.tags or [])],
        "tag_names": [tag.name for tag in (experience.tags or [])],
        "venue_name": venue.name if venue else None,
        "neighborhood_slug": neighborhood.slug if neighborhood else None,
        "neighborhood_name": neighborhood.name if neighborhood else None,
        "publisher_name": publisher.name if publisher else None,
        "publisher_verified": bool(publisher and publisher.verification_status == "verified"),
        "price_type": experience.price_type,
        "is_free": experience.price_type == "free",
        "is_indoor": experience.is_indoor,
        "latitude": venue.latitude if venue else None,
        "longitude": venue.longitude if venue else None,
        "popularity_score": float(experience.popularity_score or 0),
        "quality_score": float(experience.quality_score or 0),
        # Epoch seconds: Meilisearch cannot filter or sort on ISO strings.
        "next_start_timestamp": int(next_event.start_time.timestamp()) if next_event else None,
        "published_timestamp": int(experience.published_at.timestamp())
        if experience.published_at
        else None,
        "indexed_at": datetime.now(UTC).isoformat(),
    }


async def index_experience(experience: Experience) -> None:
    await get_search_client().index_documents([to_document(experience)])


async def remove_experience(experience_id: str) -> None:
    await get_search_client().delete_document(experience_id)


async def reindex_all(session: AsyncSession, *, batch_size: int = 200) -> int:
    """Rebuild the index from the database.

    Postgres is the source of truth, so a full rebuild is always safe and is the
    recovery path if the index is lost or a mapping changes.
    """
    client = get_search_client()
    await client.ensure_indexes()

    experiences = await catalog_repo.query_experiences(session, limit=10_000)
    total = 0
    for start in range(0, len(experiences), batch_size):
        batch = experiences[start : start + batch_size]
        await client.index_documents([to_document(exp) for exp in batch])
        total += len(batch)

    logger.info("reindex_complete", indexed=total)
    return total
